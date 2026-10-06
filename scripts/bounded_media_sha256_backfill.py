"""Explicit tenant-bound raw SHA256 backfill; default dry-run, at most 10 IDs.

No catalog scans, approval changes, moderation calls, or provider publishing.
A POSIX wall-clock timer interrupts reads/downloads/writes at the deadline.
Apply requires a separate successful 0600 dry-run ledger for the exact ID set.
Independent review is procedural and is not proven by this CLI.
Interrupted writes are uncertain: re-read current evidence before resuming.
"""
from __future__ import annotations

import argparse
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import stat
import tempfile
import time

from agent import gym_media_moderation as moderation
from agent import gym_media_selector
from agent.integrations.drive_client import DriveClient
from agent.media_source_store import default_store


class DeadlineReached(BaseException):
    pass


@contextmanager
def deadline(seconds):
    def expired(*_):
        raise DeadlineReached()
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise ValueError("existing process timer; run in a dedicated CLI process")
    prior = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior)


def load_ledger(path, gym_id, apply):
    if path.is_symlink():
        raise ValueError("ledger must not be a symlink")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("ledger must be an owned 0600 regular file")
        data = json.loads(path.read_text())
        if data.get("version") != 1 or data.get("gym_id") != gym_id or data.get("apply") is not apply:
            raise ValueError("ledger tenant/mode mismatch")
        if not isinstance(data.get("results"), dict):
            raise ValueError("invalid ledger results")
        return data
    return {"version": 1, "gym_id": gym_id, "apply": apply, "results": {}}


def save_ledger(path, data):
    # Recheck on every replace; no URLs, credentials, media bytes or titles stored.
    load_ledger(path, data["gym_id"], data["apply"])
    fd, temp = tempfile.mkstemp(prefix=".sha-ledger-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class TenantStore:
    def __init__(self, store, gym_id):
        self.store, self.gym_id = store, gym_id
        self.expected_receipts = None

    def get_asset(self, asset_id):
        row = self.store.get_asset(asset_id)
        if not row or row.get("id") != asset_id or str(row.get("gym_id")) != self.gym_id:
            raise ValueError("missing asset or tenant mismatch")
        if row.get("kind") != "photo":
            raise ValueError("only photos are allowed")
        source = self.store.get_source(row.get("source_id"))
        if not source or source.get("id") != row.get("source_id") or str(source.get("gym_id")) != self.gym_id or source.get("active") is not True:
            raise ValueError("active source tenant mismatch")
        if (row.get("review_status") != "approved" or row.get("review_content_hash") != row.get("content_hash")
                or not re.fullmatch(r"[0-9a-f]{32}", str(row.get("content_hash") or ""))
                or not gym_media_selector._clean_moderation_evidence(row)):
            raise ValueError("clean current hash-bound approval required")
        if self.expected_receipts is not None:
            receipt = self.expected_receipts[asset_id]
            if row["content_hash"] != receipt["content_hash"] or evidence_identity(row) != receipt["evidence_identity"]:
                raise ValueError("dry-run receipt drift; manual_reconciliation_required")
        return row

    def update_moderation_sha256(self, gym_id, *args, **kwargs):
        if gym_id != self.gym_id:
            raise ValueError("write tenant mismatch")
        if self.expected_receipts is not None:
            asset_id, evidence = args[:2]
            if evidence.get("sha256") != self.expected_receipts[asset_id]["sha256"]:
                raise ValueError("derived SHA256 differs from exact dry-run receipt")
        return self.store.update_moderation_sha256(gym_id, *args, **kwargs)


class MeteredDrive:
    def __init__(self, drive):
        self.drive, self.bytes_downloaded = drive, 0

    def download(self, asset_id, path):
        try:
            return self.drive.download(asset_id, path)
        finally:
            if Path(path).exists():
                self.bytes_downloaded += Path(path).stat().st_size


def evidence_identity(row):
    """Fingerprint existing review and moderation evidence without the derived SHA."""
    bound = {key: row.get(key) for key in ("id", "gym_id", "source_id", "kind", "content_hash",
             "review_status", "review_content_hash", "reviewed_by", "reviewed_at",
             "moderation_status", "people_detected")}
    bound["moderation_json"] = {k: v for k, v in row["moderation_json"].items() if k != "sha256"}
    return hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def apply_receipts(path, gym_id, asset_ids):
    if path is None or not Path(path).exists():
        raise ValueError("apply requires an exact successful dry-run ledger")
    ledger = load_ledger(Path(path), gym_id, False)
    entries = ledger["results"]
    if set(entries) != set(asset_ids):
        raise ValueError("dry-run ledger explicit IDs must match exactly")
    for asset_id, item in entries.items():
        if (item.get("ok") is not True or item.get("dry_run") is not True
                or item.get("changed") is not False or item.get("asset_id") != asset_id
                or not re.fullmatch(r"[0-9a-f]{32}", str(item.get("content_hash") or ""))
                or any(not re.fullmatch(r"[0-9a-f]{64}", str(item.get(k) or ""))
                       for k in ("sha256", "evidence_identity"))):
            raise ValueError("dry-run ledger lacks exact successful identity evidence")
    return entries


def run(gym_id, asset_ids, *, ledger_path, store, drive, apply=False,
        deadline_seconds=60, dry_run_ledger=None):
    if not gym_id.strip() or not 1 <= len(asset_ids) <= 10 or len(set(asset_ids)) != len(asset_ids) or any(not a.strip() for a in asset_ids):
        raise ValueError("require one tenant and 1 to 10 distinct explicit asset IDs")
    if not 0 < deadline_seconds <= 300:
        raise ValueError("deadline must be greater than zero and at most 300 seconds")
    path = Path(ledger_path)
    data = load_ledger(path, gym_id, apply)
    receipts = apply_receipts(dry_run_ledger, gym_id, asset_ids) if apply else None
    tenant_store, metered = TenantStore(store, gym_id), MeteredDrive(drive)
    tenant_store.expected_receipts = receipts
    started = time.monotonic()
    results, stopped, interrupted_asset = [], False, None
    try:
        with deadline(deadline_seconds):
            # Validate the complete requested apply batch before its first write.
            if apply:
                for asset_id in asset_ids:
                    try:
                        tenant_store.get_asset(asset_id)
                    except Exception as exc:
                        return {"ok": False, "gym_id": gym_id, "apply": True,
                                "manual_reconciliation_required": True,
                                "asset_id": asset_id, "error_type": type(exc).__name__}
            for asset_id in asset_ids:
                item_start, prior_bytes = time.monotonic(), metered.bytes_downloaded
                try:
                    row = tenant_store.get_asset(asset_id)
                    prior = data["results"].get(asset_id, {})
                    current_sha = row["moderation_json"].get("sha256")
                    identity = evidence_identity(row)
                    if apply and current_sha == receipts[asset_id]["sha256"]:
                        result = dict(receipts[asset_id], dry_run=False, changed=False, already_applied=True)
                    elif apply and (current_sha is not None or prior):
                        # Any persisted attempt can have reached the DB. Never repeat it
                        # unless exact expected hash/evidence readback proves completion.
                        result = {"ok": False, "manual_reconciliation_required": True}
                    elif not apply and prior.get("ok") and prior.get("content_hash") == row["content_hash"] and prior.get("evidence_identity") == identity:
                        result = dict(prior, resumed=True)
                    elif current_sha is not None:
                        result = {"ok": False, "reason": "raw SHA256 already present; missing-only job"}
                    else:
                        if apply:
                            data["results"][asset_id] = {"asset_id": asset_id, "ok": False,
                                "attempting": True, "expected_sha256": receipts[asset_id]["sha256"]}
                            save_ledger(path, data)
                        outcome = moderation.backfill_asset_sha256(gym_id, asset_id, store=tenant_store, drive=metered, apply=apply)
                        result = {k: outcome[k] for k in ("ok", "sha256", "changed", "dry_run") if k in outcome}
                        if apply and (not result.get("ok") or result.get("sha256") != receipts[asset_id]["sha256"]):
                            result.update(ok=False, manual_reconciliation_required=True)
                        result.update(content_hash=row["content_hash"], evidence_identity=identity)
                    result.update(asset_id=asset_id, bytes_downloaded=metered.bytes_downloaded-prior_bytes,
                                  elapsed_seconds=round(time.monotonic()-item_start, 3))
                except Exception as exc:
                    result = {"ok": False, "asset_id": asset_id, "error_type": type(exc).__name__,
                              "bytes_downloaded": metered.bytes_downloaded-prior_bytes}
                    if apply:
                        result["manual_reconciliation_required"] = True
                data["results"][asset_id] = result
                save_ledger(path, data)
                results.append(result)
    except DeadlineReached:
        stopped = True
        interrupted_asset = asset_id if "asset_id" in locals() else None
    return {"gym_id": gym_id, "apply": apply, "deadline_reached": stopped,
            "interrupted_asset_id": interrupted_asset, "write_outcome_uncertain": stopped and apply,
            "manual_reconciliation_required": bool(stopped and apply) or any(r.get("manual_reconciliation_required") for r in results),
            "results": results, "bytes_downloaded": metered.bytes_downloaded,
            "elapsed_seconds": round(time.monotonic()-started, 3),
            "provider_publish_calls": 0, "moderation_provider_calls": 0,
            "paid_review_or_model_calls": 0, "infra_cost_usd": None,
            "ok": not stopped and len(results) == len(asset_ids) and all(r.get("ok") for r in results)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gym_id")
    parser.add_argument("--asset-id", action="append", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--deadline-seconds", type=float, default=60)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run-ledger", type=Path, help="exact prior successful 0600 dry-run ledger required for apply")
    args = parser.parse_args(argv)
    if args.apply and not os.isatty(0):
        parser.error("--apply requires an interactive operator shell")
    if args.apply and args.dry_run_ledger is None:
        parser.error("--apply requires --dry-run-ledger")
    try:
        result = run(args.gym_id, args.asset_id, ledger_path=args.ledger,
                     store=default_store(), drive=DriveClient(), apply=args.apply,
                     deadline_seconds=args.deadline_seconds, dry_run_ledger=args.dry_run_ledger)
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "manual_reconciliation_required": bool(args.apply)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
