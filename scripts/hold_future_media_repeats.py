#!/usr/bin/env python3
"""Exact-row containment for the audited future cross-date media repeats.

Dry-run is the default. Apply requires ECHO_CROSS_DATE_MEDIA_REPEAT_HOLD_ENABLED=true,
the exact dry-run digest, and a new mode-0600 write-ahead receipt. The only
calendar mutation is media_not_ready_reason; status, caption, slot, and media
remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.portal_calendar_store import SupabaseCalendarStore
from agent.media_source_store import SupabaseMediaStore

REASON = "cross_date_media_repeat_needs_new_visual"
_URL_FIELD_HASHES = ("image_url", "source_media_url")
_ROW_FIELDS = (
    "id", "gym_id", "post_date", "status", "variant_status", "account",
    "format", "caption", "image_url", "source_media_url",
    "source_media_asset_id", "media_not_ready_reason", "created_at",
    "published_at", "late_post_id", "publish_claim_token",
    "publish_reservation_day",
)
_EARLIER_STATUSES = {"pending", "coach_review", "approved", "publishing", "published"}


def _spec(row_id, gym_id, day, account, status, image_md5, source_md5,
          asset_id, content_hash, earlier):
    return {
        "id": row_id, "gym_id": gym_id, "post_date": day,
        "account": account, "status": status,
        "image_url_md5": image_md5, "source_media_url_md5": source_md5,
        "source_media_asset_id": asset_id, "content_hash": content_hash,
        "earlier": earlier,
    }


# Privacy-safe identity manifest from the read-only 2026-10-03 audit. URL
# fingerprints bind the live row to that snapshot; runtime proof also compares
# URL values directly between each later row and its earlier source row.
TARGETS = (
    _spec("b802f95e-6918-4a84-94d5-f6df997dd573", "crossfitlocal", "2026-10-14", "instagram", "pending",
          "ffc5ac883ae972235725eefd643eb822", "bd27f8cd5c40db5ede9cabfaf5740a60", None, None,
          (("c6bcfcbc-05f0-458e-803a-266ffab33da9", "2026-10-13", ("source_media_url",)),)),
    _spec("8cc7dc2d-6c30-426e-a42f-98906780e6fe", "crossfitlocal", "2026-10-25", "facebook", "pending",
          "e90cfe38f659f4e514e853876c9f71f8", None, None, None,
          (("2f43a767-c421-49e0-a04c-745f47b74116", "2026-10-24", ("image_url",)),)),
    _spec("72c8572f-5790-43fd-bf31-73d6ed1c6cee", "crossfitreverb30b5b2", "2026-11-01", "facebook", "pending",
          "4b93ae38ef5fa5191234d207d8ccf83c", None, "19gm1zTXiYtXHBllcBdS0LY3PubxfgYj_", "9f1ca4865bd55e0bd3e7bab9d8b833c1",
          (("d653b651-be1d-44c7-9d47-c551828c5738", "2026-10-31", ("asset_hash",)),)),
    _spec("f003758b-a3c4-43ec-910b-d2d1a0e75512", "crossfitreverb30b5b2", "2026-11-02", "instagram", "pending",
          "4b93ae38ef5fa5191234d207d8ccf83c", None, "19gm1zTXiYtXHBllcBdS0LY3PubxfgYj_", "9f1ca4865bd55e0bd3e7bab9d8b833c1",
          (("d653b651-be1d-44c7-9d47-c551828c5738", "2026-10-31", ("asset_hash",)),
           ("72c8572f-5790-43fd-bf31-73d6ed1c6cee", "2026-11-01", ("image_url", "asset_hash")))),
    _spec("2e9d7d26-4acb-49ac-9251-f5dbd351cc03", "hillcountry", "2026-11-01", "instagram", "approved",
          "83081173343ad3165950e2e6e76010be", "41edc6c6f0d544aeec278f63f2b66f70", "1nKcYFVn4DlhtqN5Y4VRl769ypeLGESWO", "6546a4fd9bbd5c21d2533751a0f5d9f6",
          (("b676f382-e81f-46db-acd2-320bf4da8e97", "2026-10-31", ("asset_hash",)),)),
    _spec("86c63b8b-4a77-4b0a-94b5-14e72c60bd7b", "hillcountry", "2026-11-03", "instagram", "approved",
          "584dc17952a85e8bd969713cb58626fd", None, "1nKcYFVn4DlhtqN5Y4VRl769ypeLGESWO", "6546a4fd9bbd5c21d2533751a0f5d9f6",
          (("b676f382-e81f-46db-acd2-320bf4da8e97", "2026-10-31", ("asset_hash",)),
           ("2e9d7d26-4acb-49ac-9251-f5dbd351cc03", "2026-11-01", ("asset_hash",)))),
    _spec("ac52c83c-7a16-4091-840c-9b9961c9a8a4", "piercefitness", "2026-10-10", "instagram", "approved",
          "a0bf3fdf177d4d62972653e17589d373", None, "1FLIsFV35DdQIIlv_4ZJ9Ii4XtQsXHL1E", "e9fd407aa8c35a5cfbdb80c95a9ace16",
          (("7e384922-4241-4baf-8484-655cb126d174", "2026-10-04", ("image_url", "asset_hash")),)),
    _spec("aedd6faf-7c80-4a2b-b397-282a9df9c6e3", "piercefitness", "2026-10-11", "instagram", "approved",
          "33aa5f78d3a5fb36c42904570adf7fa1", "a0bf3fdf177d4d62972653e17589d373", "1FLIsFV35DdQIIlv_4ZJ9Ii4XtQsXHL1E", "e9fd407aa8c35a5cfbdb80c95a9ace16",
          (("7e384922-4241-4baf-8484-655cb126d174", "2026-10-04", ("asset_hash",)),
           ("ac52c83c-7a16-4091-840c-9b9961c9a8a4", "2026-10-10", ("asset_hash",)))),
    _spec("9e0a0f88-efdf-4a87-b1db-b899f32d0827", "theboltonclub", "2026-10-12", "facebook", "pending",
          "0e793248fe3a347d0de38913d0074c29", None, "1VjKiqtdQYRwU0zdgXzG0Zs7v8NVtZyf4", "77dc7e7bad7dbb1fe420d5d6a5c3285c",
          (("572a7e2b-3a7c-4481-9d5c-fad91ef857e9", "2026-10-10", ("asset_hash",)),)),
    _spec("ddbb5661-7aa8-46ed-8e24-e1b0b66245f4", "theboltonclub", "2026-10-13", "instagram", "pending",
          "75fd7ab7464c7b4ae97a45a3485d1a3b", None, "1VjKiqtdQYRwU0zdgXzG0Zs7v8NVtZyf4", "77dc7e7bad7dbb1fe420d5d6a5c3285c",
          (("572a7e2b-3a7c-4481-9d5c-fad91ef857e9", "2026-10-10", ("asset_hash",)),
           ("9e0a0f88-efdf-4a87-b1db-b899f32d0827", "2026-10-12", ("asset_hash",)))),
    _spec("3a00b42f-73df-4803-952c-39007a47b75f", "topfuel", "2026-10-06", "instagram", "pending",
          "c47268e82160e3c586cbcde2e141b7d5", None, "1IAWXjtLNtFF1FpBRbfvogMDnKoCVLP-y", "3726008822d3feb913352651fdaf6f3e",
          (("a26aa60b-066b-4ae9-b8ea-1a648f0117cc", "2026-10-04", ("image_url", "asset_hash")),)),
    _spec("63aad1f3-df9f-4e6b-b40b-f4ca5ea73603", "topfuel", "2026-10-07", "instagram", "pending",
          "9e5887594504e94ce997482862e896cd", "b420dac500ab3f8f908e35b7d38e4755", "1IAWXjtLNtFF1FpBRbfvogMDnKoCVLP-y", "3726008822d3feb913352651fdaf6f3e",
          (("a26aa60b-066b-4ae9-b8ea-1a648f0117cc", "2026-10-04", ("asset_hash",)),
           ("3a00b42f-73df-4803-952c-39007a47b75f", "2026-10-06", ("asset_hash",)))),
    _spec("53221dd1-29e4-43b6-af00-24ea5211d6ae", "toughtemple52040e", "2026-10-12", "facebook", "pending",
          "83bc2f7c222357022b055bf91050acce", None, "10X6Z4swzX724C9nk8KTm1tNNDKtDJtp9", "85ce9046698c88a214e1a9bd6b516502",
          (("d9531da0-24a1-41b1-a48c-4de85f730567", "2026-10-11", ("image_url", "asset_hash")),)),
)


def _url_md5(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return None
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _canonical_hash(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot(row):
    if not isinstance(row, dict) or any(field not in row for field in _ROW_FIELDS):
        raise ValueError("calendar row is missing a required snapshot field")
    return {field: row.get(field) for field in _ROW_FIELDS}


def _safe_proof(row):
    snap = _snapshot(row)
    return {
        "id": snap["id"], "gym_id": snap["gym_id"],
        "post_date": snap["post_date"], "status": snap["status"],
        "account": snap["account"], "variant_status": snap["variant_status"],
        "source_media_asset_id": snap["source_media_asset_id"],
        "image_url_md5": _url_md5(snap["image_url"]),
        "source_media_url_md5": _url_md5(snap["source_media_url"]),
        "row_sha256": _canonical_hash(snap),
    }


def _verify_asset(media_store, gym_id, asset_id, expected_hash, cache):
    if not asset_id or not expected_hash:
        return False
    if asset_id not in cache:
        cache[asset_id] = media_store.get_asset(asset_id)
    asset = cache[asset_id]
    return bool(asset and str(asset.get("id")) == str(asset_id)
                and str(asset.get("gym_id")) == str(gym_id)
                and str(asset.get("content_hash") or "") == expected_hash)


def _target_from_live(store, media_store, spec, *, today, horizon_end,
                      asset_cache, book_by_id=None):
    row = store.get_row(spec["gym_id"], spec["id"])
    if row is None:
        raise ValueError(f"target row missing: {spec['id']}")
    if book_by_id is not None:
        listed = book_by_id.get(spec["id"])
        if listed is None or _snapshot(listed) != _snapshot(row):
            raise ValueError(f"target changed during complete-book read: {spec['id']}")
    if (str(row.get("id")) != spec["id"] or row.get("gym_id") != spec["gym_id"]
            or str(row.get("post_date", ""))[:10] != spec["post_date"]
            or not today <= spec["post_date"] <= horizon_end
            or row.get("account") != spec["account"]
            or row.get("status") != spec["status"]
            or row.get("variant_status") != "active"
            or row.get("media_not_ready_reason") is not None
            or row.get("published_at") is not None
            or row.get("late_post_id") is not None
            or row.get("publish_claim_token") is not None
            or row.get("publish_reservation_day") is not None
            or _url_md5(row.get("image_url")) != spec["image_url_md5"]
            or _url_md5(row.get("source_media_url")) != spec["source_media_url_md5"]
            or row.get("source_media_asset_id") != spec["source_media_asset_id"]):
        raise ValueError(f"target no longer matches audited future snapshot: {spec['id']}")

    proofs = []
    for prior_id, prior_day, keys in spec["earlier"]:
        prior = store.get_row(spec["gym_id"], prior_id)
        if book_by_id is not None:
            listed_prior = book_by_id.get(prior_id)
            if listed_prior is None or _snapshot(listed_prior) != _snapshot(prior):
                raise ValueError(f"earlier row changed during complete-book read: {prior_id}")
        if (not isinstance(prior, dict) or str(prior.get("id")) != prior_id
                or prior.get("gym_id") != spec["gym_id"]
                or str(prior.get("post_date", ""))[:10] != prior_day
                or prior_day >= spec["post_date"]
                or prior.get("variant_status") not in (None, "active")
                or prior.get("status") not in _EARLIER_STATUSES):
            raise ValueError(f"earlier active row evidence changed: {prior_id}")
        matched = True
        if "image_url" in keys:
            matched = matched and bool(row.get("image_url")) and row["image_url"] == prior.get("image_url")
        if "source_media_url" in keys:
            matched = matched and bool(row.get("source_media_url")) and row["source_media_url"] == prior.get("source_media_url")
        if "asset_hash" in keys:
            matched = (matched and row.get("source_media_asset_id") == spec["source_media_asset_id"]
                       and prior.get("source_media_asset_id") == spec["source_media_asset_id"]
                       and _verify_asset(media_store, spec["gym_id"], spec["source_media_asset_id"],
                                         spec["content_hash"], asset_cache))
        if not matched:
            raise ValueError(f"prior media identity no longer matches: {prior_id}")
        proofs.append({
            "prior": _safe_proof(prior), "keys": list(keys),
            "asset_content_hash": spec["content_hash"] if "asset_hash" in keys else None,
        })
    return row, {"target": _safe_proof(row), "earlier_matches": proofs}


def _hold_exact(store, current):
    """PATCH only the reason, with every relevant row field in the CAS filter."""
    snap = _snapshot(current)
    def expected(value):
        return "is.null" if value is None else f"eq.{value}"
    params = {field: expected(value) for field, value in snap.items()}
    response = store._client().patch(
        store._rest("content_calendar"), params=params,
        headers=store._headers({"Content-Type": "application/json",
                                "Prefer": "return=representation"}),
        json={"media_not_ready_reason": REASON}, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError("future repeat hold CAS failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1:
        return None
    after = rows[0]
    expected_after = {**snap, "media_not_ready_reason": REASON}
    if any(after.get(key) != value for key, value in expected_after.items()):
        return None
    return after


def _receipt(path, value, *, create=False):
    target = Path(path)
    if not target.parent.is_dir():
        raise ValueError("receipt parent directory does not exist")
    payload = (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode()
    if create:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def run(*, store=None, media_store=None, today=None, apply=False,
        expected_digest=None, receipt_path=None):
    today = today or datetime.now(timezone.utc).date().isoformat()
    horizon_end = (date.fromisoformat(today) + timedelta(days=365)).isoformat()
    # Include the audited prior dates even after they become historical. Target
    # rows still must be future relative to today before they can be held.
    query_start = min(today, *(prior_day for spec in TARGETS
                               for _, prior_day, _ in spec["earlier"]))
    store = store or SupabaseCalendarStore()
    media_store = media_store or SupabaseMediaStore()
    try:
        # Force one complete, error-failing read of the bounded calendar range.
        book = store.list_future_media_maintenance_rows(query_start, horizon_end)
        if not isinstance(book, list):
            raise ValueError("calendar read was not a complete row list")
        book_by_id = {str(row.get("id")): row for row in book if isinstance(row, dict)}
        if len(book_by_id) != len(book):
            raise ValueError("calendar book contains malformed or duplicate IDs")
        asset_cache = {}
        before, evidence = [], []
        for spec in TARGETS:
            row, proof = _target_from_live(store, media_store, spec, today=today,
                                          horizon_end=horizon_end,
                                          asset_cache=asset_cache,
                                          book_by_id=book_by_id)
            before.append(row)
            evidence.append(proof)
        if len({str(row["id"]) for row in before}) != len(TARGETS):
            raise ValueError("target manifest contains duplicate row IDs")
    except Exception as exc:
        return {"ok": False, "dry_run": not apply,
                "reason": f"preflight failed:{type(exc).__name__}",
                "detail": str(exc)[:160]}

    before = sorted(before, key=lambda row: (row["post_date"], row["gym_id"], row["id"]))
    evidence.sort(key=lambda item: (item["target"]["post_date"],
                                    item["target"]["gym_id"], item["target"]["id"]))
    proofs = [_safe_proof(row) for row in before]
    digest = _canonical_hash({"target_proofs": proofs, "match_evidence": evidence})
    summary = {
        "operation": "hold_future_media_repeats", "today": today,
        "target_digest": digest, "target_count": len(before),
        "pending_count": sum(row["status"] == "pending" for row in before),
        "approved_count": sum(row["status"] == "approved" for row in before),
        "target_proofs": proofs, "match_evidence": evidence,
        "reason": REASON,
        "changed_fields": ["media_not_ready_reason"],
        "preserved": ["status", "caption", "post_date", "account", "format",
                      "variant_status", "image_url", "source_media_url",
                      "source_media_asset_id", "approval", "publication", "claim"],
        "rollback": "Do not bulk-clear holds. Re-read each exact row and restore only with a row-level CAS matching this reason and the recorded row fingerprint.",
    }
    if not apply:
        return {"ok": True, "dry_run": True, "preflight": summary}
    if os.environ.get("ECHO_CROSS_DATE_MEDIA_REPEAT_HOLD_ENABLED", "").lower() != "true":
        return {"ok": False, "reason": "hold apply flag OFF", "observed_digest": digest}
    if not before or expected_digest != digest or not receipt_path:
        return {"ok": False, "reason": "exact dry-run digest and new private receipt required",
                "observed_digest": digest}
    progress = {**summary, "state": "before_write", "changed_ids": [],
                "inflight_id": None, "readback": [],
                "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        _receipt(receipt_path, progress, create=True)
    except Exception as exc:
        return {"ok": False, "reason": f"before-image receipt failed:{type(exc).__name__}"}

    for row in before:
        progress["state"] = "write_intent"
        progress["inflight_id"] = str(row["id"])
        try:
            _receipt(receipt_path, progress)
        except Exception as exc:
            return {"ok": False, "reason": f"write-intent receipt failed:{type(exc).__name__}",
                    "receipt": receipt_path}
        try:
            # Re-read the target and its earlier source rows immediately before
            # each PATCH. This catches a fresh hold/restage/status change after
            # the digest check and after any preceding per-row writes.
            current_row, _ = _target_from_live(
                store, media_store, next(spec for spec in TARGETS
                                         if spec["id"] == str(row["id"])),
                today=today, horizon_end=horizon_end, asset_cache={})
            if _snapshot(current_row) != _snapshot(row):
                raise ValueError("target snapshot changed after digest check")
        except Exception as exc:
            progress["state"] = "cas_conflict"
            progress["inflight_id"] = None
            progress.setdefault("conflict_ids", []).append(str(row["id"]))
            progress.setdefault("conflict_reasons", {})[str(row["id"])] = type(exc).__name__
            _receipt(receipt_path, progress)
            continue
        try:
            updated = _hold_exact(store, current_row)
        except Exception as exc:
            return {"ok": False, "reason": f"hold CAS uncertain:{type(exc).__name__}",
                    "receipt": receipt_path, "inflight_id": row["id"]}
        if updated is None:
            progress["state"] = "cas_conflict"
            progress["inflight_id"] = None
            progress.setdefault("conflict_ids", []).append(str(row["id"]))
            _receipt(receipt_path, progress)
            continue
        try:
            current = store.get_row(row["gym_id"], str(row["id"]))
        except Exception as exc:
            return {"ok": False, "reason": f"readback failed:{type(exc).__name__}",
                    "receipt": receipt_path, "inflight_id": row["id"]}
        expected_after = {**_snapshot(row), "media_not_ready_reason": REASON}
        if current is None or _snapshot(current) != expected_after:
            return {"ok": False, "reason": "readback mismatch; reconcile before retry",
                    "receipt": receipt_path, "inflight_id": row["id"]}
        progress["changed_ids"].append(str(row["id"]))
        progress["readback"].append(_safe_proof(current))
        progress["inflight_id"] = None
        progress["state"] = "partial_hold"
        _receipt(receipt_path, progress)
    progress["state"] = ("readback_verified" if not progress.get("conflict_ids")
                         else "partial_hold_conflicts")
    _receipt(receipt_path, progress)
    return {"ok": not progress.get("conflict_ids"), "receipt": receipt_path,
            "target_digest": digest, "changed_ids": progress["changed_ids"],
            "conflict_ids": progress.get("conflict_ids", [])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--receipt", help="new private JSON path required for apply")
    args = parser.parse_args(argv)
    result = run(apply=args.apply, expected_digest=args.expected_digest,
                 receipt_path=args.receipt)
    displayed = dict(result)
    if isinstance(displayed.get("preflight"), dict):
        displayed["preflight"] = {key: value for key, value in displayed["preflight"].items()
                                  if key not in {"target_proofs", "match_evidence"}}
    print(json.dumps(displayed, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
