#!/usr/bin/env python3
"""Operator CLI for the DRAFT per-asset historical media clearance ledger.

Draft infrastructure ONLY (PR262). This script has no scheduled or production
caller; it is a human operator tool that exercises
``SupabaseMediaStore.list_assets`` / ``list_historical_clearances`` /
``record_historical_clearance`` / ``revoke_historical_clearance`` against the
UNAPPLIED DRAFT migration ``migrations/DRAFT_historical_media_clearance_20261003.sql``.

Every command is exact (gym_id, asset_id) scoped and FAILS CLOSED:
  * an absent/unapplied DRAFT migration makes the store raise MediaStoreError
    and the command exits non-zero — never a silent empty result;
  * a stale content_hash (asset bytes changed in Drive after review) is refused;
  * record requires an explicit --reviewer, an explicit --decision of
    cleared|known_used|held, the exact CURRENT content_hash, and structured
    --evidence (inline JSON or a JSON file) that binds gym_id, asset_id,
    source_id and content_hash to the live asset row;
  * revoke requires the exact CURRENT content_hash, --reviewer and --reason;
* a 'cleared' decision requires concrete evidence (method, observed_at,
  result and a reviewer assertion/proof reference) — identity-only evidence
  can never clear an asset;
* LIST and HISTORY never print raw evidence: default output shows a bounded,
  redacted summary (key names only); full evidence requires an exact --asset
  opt-in (--show-evidence);
  * nothing is ever inferred unused and nothing is ever auto-cleared.

RECORD and REVOKE prompt for interactive confirmation before a live write
unless --yes is given (for scripted operator runs). LIST is read-only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.media_source_store import MediaStoreError, default_store

_HASH_RE = re.compile(r"^[0-9a-f]{32}$|^[0-9a-f]{64}$")
DECISIONS = ("cleared", "known_used", "held")


class CliError(RuntimeError):
    """Operator-facing fail-closed refusal. Exit code 2."""


def _require_gym_asset(args):
    gym_id = (args.gym or "").strip()
    asset_id = (args.asset or "").strip()
    if not gym_id:
        raise CliError("gym_id is required (tenant isolation)")
    if not asset_id:
        raise CliError("asset_id is required (exact per-asset scoping)")
    return gym_id, asset_id


def _require_hash(value, label):
    h = (value or "").strip().lower()
    if not _HASH_RE.match(h):
        raise CliError(f"{label} must be an MD5(32) or SHA256(64) lowercase hex string")
    return h


def _load_evidence(spec, path):
    try:
        if path:
            with open(path, "r", encoding="utf-8") as fh:
                evidence = json.load(fh)
        else:
            evidence = json.loads(spec or "")
    except (OSError, ValueError) as exc:
        raise CliError(f"evidence must be valid JSON: {exc}")
    if not isinstance(evidence, dict) or not evidence:
        raise CliError("evidence must be a non-empty JSON object")
    return evidence


def _assert_evidence_binds(evidence, gym_id, asset_id, source_id, content_hash):
    """The receipt is only as trustworthy as its binding. Evidence MUST name
    the exact gym, asset Drive file ID, source ID and current content_hash it
    reviews; anything else fails closed."""
    expected = {"gym_id": gym_id, "asset_id": asset_id,
                "source_id": source_id, "content_hash": content_hash}
    for key, want in expected.items():
        got = evidence.get(key)
        if str(got or "") != str(want):
            raise CliError(
                f"evidence must bind {key}={want!r} (got {got!r}); "
                "rebuild evidence against the live asset row")


_CLEARED_REQUIRED_FIELDS = ("method", "observed_at", "result")
_CLEARED_PROOF_KEYS = ("proof_ref", "reviewer_assertion", "assertion_ref")


def _assert_cleared_evidence(evidence, decision):
    """A 'cleared' decision must rest on concrete review work: the method
    used, when it was observed, the result, and a reviewer assertion/proof
    reference. Identity-only evidence (four binding IDs) can never record a
    clearance."""
    if decision != "cleared":
        return
    missing = [k for k in _CLEARED_REQUIRED_FIELDS
               if not str(evidence.get(k) or "").strip()]
    if missing:
        raise CliError(
            "cleared decision requires concrete evidence fields "
            f"{missing} (method, observed_at, result); identity-only "
            "evidence cannot record 'cleared'")
    if not any(str(evidence.get(k) or "").strip() for k in _CLEARED_PROOF_KEYS):
        raise CliError(
            "cleared decision requires a reviewer assertion/proof reference "
            f"(one of {_CLEARED_PROOF_KEYS})")


def _evidence_summary(evidence):
    """Bounded, redacted summary of an arbitrary evidence object: key names
    and count only, NEVER values (evidence may carry sensitive detail)."""
    if not isinstance(evidence, dict):
        return None
    return {"keys": sorted(str(k) for k in evidence), "field_count": len(evidence)}


def _receipt_view(receipt, show_evidence=False):
    """Bounded per-version receipt view; full evidence is only attached on
    exact-asset opt-in (--show-evidence)."""
    view = {"version": receipt.get("version"),
            "decision": receipt.get("decision"),
            "reviewer": receipt.get("reviewer"),
            "content_hash": receipt.get("content_hash"),
            "recorded_at": receipt.get("recorded_at"),
            "revoked_at": receipt.get("revoked_at"),
            "evidence_summary": _evidence_summary(receipt.get("evidence"))}
    if show_evidence:
        view["evidence"] = receipt.get("evidence")
    return view


def _sources_by_id(store, gym_id):
    return {str(s.get("id")): s for s in store.list_sources(gym_id)}


def _active_receipt(receipts, gym_id, asset_id):
    """The single ACTIVE (unrevoked) receipt version for this exact
    (gym_id, asset_id), or None. Ignores revoked history versions and any row
    that does not match exactly."""
    active = None
    for row in receipts or []:
        if str(row.get("gym_id")) != gym_id or str(row.get("asset_id")) != asset_id:
            continue
        if row.get("revoked_at"):
            continue
        if active is not None:
            raise CliError("store returned multiple active receipts for one asset")
        active = row
    return active


def _cmd_list(store, args, out):
    gym_id = (args.gym or "").strip()
    if not gym_id:
        raise CliError("gym_id is required (tenant isolation)")
    asset_filter = (getattr(args, "asset", "") or "").strip()
    show_evidence = bool(getattr(args, "show_evidence", False))
    if show_evidence and not asset_filter:
        raise CliError("--show-evidence requires --asset (exact-asset opt-in only); "
                       "list output is otherwise redacted")
    sources = _sources_by_id(store, gym_id)
    receipts = store.list_historical_clearances(gym_id)
    rows = []
    for asset in store.list_assets(gym_id):
        asset_id = str(asset.get("id") or "")
        if asset_filter and asset_id != asset_filter:
            continue
        source = sources.get(str(asset.get("source_id") or ""), {})
        receipt = _active_receipt(receipts, gym_id, asset_id)
        rows.append({
            "gym_id": gym_id,
            "asset_id": asset_id,
            "content_hash": asset.get("content_hash"),
            "source_id": asset.get("source_id"),
            "drive_source": {"kind": source.get("kind"), "name": source.get("name"),
                             "folder_id": source.get("folder_id")},
            "first_indexed_at": asset.get("first_indexed_at"),
            "indexed_at": asset.get("indexed_at"),
            "used_count": asset.get("used_count"),
            "active_receipt": (_receipt_view(receipt, show_evidence)
                               if receipt else None),
        })
    if asset_filter and not rows:
        raise CliError(f"no asset {asset_filter!r} for gym {gym_id!r} (exact scoping)")
    json.dump({"gym_id": gym_id, "reviewable_assets": rows}, out, indent=2,
              sort_keys=True, default=str)
    out.write("\n")
    return 0


def _cmd_history(store, args, out):
    """Bounded version/history view for ONE exact (gym_id, asset_id): every
    receipt version with a redacted evidence summary; full evidence only via
    --show-evidence on this exact asset."""
    gym_id, asset_id = _require_gym_asset(args)
    show_evidence = bool(getattr(args, "show_evidence", False))
    asset = store.get_asset(asset_id)
    if not asset or str(asset.get("gym_id")) != gym_id:
        raise CliError(f"no asset {asset_id!r} for gym {gym_id!r} (exact scoping)")
    receipts = store.list_historical_clearances(gym_id)
    versions = [_receipt_view(r, show_evidence) for r in receipts or []
                if str(r.get("gym_id")) == gym_id
                and str(r.get("asset_id")) == asset_id]
    versions.sort(key=lambda v: (v.get("version") is None, v.get("version")))
    active = [v for v in versions if not v.get("revoked_at")]
    json.dump({"gym_id": gym_id, "asset_id": asset_id,
               "current_content_hash": asset.get("content_hash"),
               "active_version": active[-1]["version"] if active else None,
               "versions": versions}, out, indent=2, sort_keys=True, default=str)
    out.write("\n")
    return 0


def _confirm(args, prompt):
    if getattr(args, "yes", False):
        return
    if not sys.stdin.isatty():
        raise CliError("refusing live write without a TTY: pass --yes for scripted runs")
    answer = input(f"{prompt} [type the asset_id to confirm]: ").strip()
    if answer != getattr(args, "asset", ""):
        raise CliError("confirmation did not match asset_id; aborted")


def _cmd_record(store, args, out):
    gym_id, asset_id = _require_gym_asset(args)
    content_hash = _require_hash(args.content_hash, "--content-hash")
    decision = (args.decision or "").strip().lower()
    if decision not in DECISIONS:
        raise CliError(f"--decision must be one of {DECISIONS}")
    reviewer = (args.reviewer or "").strip()
    if not reviewer:
        raise CliError("--reviewer (named human operator) is required")
    evidence = _load_evidence(args.evidence, args.evidence_file)

    asset = store.get_asset(asset_id)
    if not asset or str(asset.get("gym_id")) != gym_id:
        raise CliError(f"no asset {asset_id!r} for gym {gym_id!r} (exact scoping)")
    current = str(asset.get("content_hash") or "").lower()
    if current != content_hash:
        raise CliError(
            f"stale hash: asset current content_hash is {current!r}, "
            f"review supplied {content_hash!r}; re-review the CURRENT bytes")
    _assert_evidence_binds(evidence, gym_id, asset_id,
                           str(asset.get("source_id") or ""), content_hash)
    _assert_cleared_evidence(evidence, decision)

    _confirm(args, f"RECORD {decision} receipt for {gym_id}/{asset_id}")
    result = store.record_historical_clearance(
        gym_id, asset_id, content_hash, decision, reviewer, evidence)
    json.dump({"recorded": result}, out, indent=2, sort_keys=True, default=str)
    out.write("\n")
    return 0


def _cmd_revoke(store, args, out):
    gym_id, asset_id = _require_gym_asset(args)
    content_hash = _require_hash(args.content_hash, "--content-hash")
    reviewer = (args.reviewer or "").strip()
    reason = (args.reason or "").strip()
    if not reviewer:
        raise CliError("--reviewer is required")
    if not reason:
        raise CliError("--reason is required")

    asset = store.get_asset(asset_id)
    if not asset or str(asset.get("gym_id")) != gym_id:
        raise CliError(f"no asset {asset_id!r} for gym {gym_id!r} (exact scoping)")
    current = str(asset.get("content_hash") or "").lower()
    if current != content_hash:
        raise CliError(
            f"stale hash: asset current content_hash is {current!r}, "
            f"revoke supplied {content_hash!r}; revoke against the CURRENT hash only")

    _confirm(args, f"REVOKE active clearance receipt for {gym_id}/{asset_id}")
    confirmed = store.revoke_historical_clearance(
        gym_id, asset_id, content_hash, reviewer, reason)
    if confirmed is not True:
        raise CliError("store did not confirm revocation")
    # Truthful result: the RPC confirms with a boolean only — it returns no
    # receipt/version payload, so none is fabricated here.
    json.dump({"revoked": {"confirmed": True, "gym_id": gym_id,
                           "asset_id": asset_id, "content_hash": content_hash,
                           "reviewer": reviewer, "reason_submitted": reason,
                           "note": "RPC confirmed revocation; no version returned"}},
              out, indent=2, sort_keys=True)
    out.write("\n")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="read-only: reviewable older approved assets with current hash, Drive source, first_indexed_at, used_count and a REDACTED active-receipt summary (full evidence only via exact --asset --show-evidence)")
    p_list.add_argument("--gym", required=True)
    p_list.add_argument("--asset", help="restrict to one exact asset (enables --show-evidence)")
    p_list.add_argument("--show-evidence", action="store_true",
                        help="include full receipt evidence; exact --asset opt-in only")
    p_list.set_defaults(func=_cmd_list)

    p_hist = sub.add_parser("history", help="read-only: bounded version/history view for ONE exact asset (redacted evidence summaries; --show-evidence for full evidence)")
    p_hist.add_argument("--gym", required=True)
    p_hist.add_argument("--asset", required=True)
    p_hist.add_argument("--show-evidence", action="store_true")
    p_hist.set_defaults(func=_cmd_history)

    p_rec = sub.add_parser("record", help="record one explicit clearance receipt (CAS-bound to the CURRENT content_hash)")
    p_rec.add_argument("--gym", required=True)
    p_rec.add_argument("--asset", required=True)
    p_rec.add_argument("--content-hash", required=True)
    p_rec.add_argument("--decision", required=True, choices=DECISIONS)
    p_rec.add_argument("--reviewer", required=True)
    p_rec.add_argument("--evidence", help="inline JSON object binding gym_id/asset_id/source_id/content_hash")
    p_rec.add_argument("--evidence-file", help="path to a JSON evidence object")
    p_rec.add_argument("--yes", action="store_true", help="skip the interactive confirmation (scripted operator runs)")
    p_rec.set_defaults(func=_cmd_record)

    p_rev = sub.add_parser("revoke", help="revoke the ACTIVE receipt version for the exact current hash")
    p_rev.add_argument("--gym", required=True)
    p_rev.add_argument("--asset", required=True)
    p_rev.add_argument("--content-hash", required=True)
    p_rev.add_argument("--reviewer", required=True)
    p_rev.add_argument("--reason", required=True)
    p_rev.add_argument("--yes", action="store_true")
    p_rev.set_defaults(func=_cmd_revoke)
    return parser


def main(argv=None, store=None, out=None, prog=None):
    parser = build_parser()
    if prog:
        parser.prog = prog
    args = parser.parse_args(argv)
    out = out or sys.stdout
    try:
        if store is None:
            store = default_store()
        return args.func(store, args, out)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except MediaStoreError as exc:
        # Fail closed: absent/unapplied DRAFT migration, stale hash, illegal
        # decision, bad evidence — the store is the authority and any refusal
        # is surfaced, never treated as success.
        print(f"error: store refused: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
