#!/usr/bin/env python3
"""Stage a lead-reviewed LASSO row manifest through the atomic service RPC.

The manifest must already contain hosted, reviewed artifact provenance. This
client neither hosts files nor invents or repairs provenance. It writes a frozen
input backup and an append-only-style receipt after every attempted row.
"""

import argparse
import json
from pathlib import Path

RPC = "stage_lasso_campaign_row"
ALLOWED_RESULTS = {"inserted", "idempotent", "conflict"}


def _load(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("manifest must contain a rows list")
    return payload, rows


def _call(store, item):
    response = store._client().post(  # noqa: SLF001 - same narrow store transport
        store._rest("rpc/" + RPC),  # noqa: SLF001
        headers=store._headers({"Content-Type": "application/json"}),  # noqa: SLF001
        json={
            "p_row": item["row"],
            "p_artifact_tenant": item["artifact_tenant"],
            "p_source_hash": item["source_hash"],
            "p_policy_version": item["policy_version"],
            "p_image_sha256": item["image_sha256"],
        },
        timeout=30,
    )
    if response.status_code >= 300:
        raise RuntimeError("atomic campaign staging RPC failed")
    result = response.json()
    if not isinstance(result, dict) or result.get("result") not in ALLOWED_RESULTS:
        raise RuntimeError("atomic campaign staging RPC returned an invalid receipt")
    return result


def apply_manifest(payload, rows, store, receipt_dir, call_fn=None):
    folder = Path(receipt_dir)
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / "lasso-campaign-input.json"
    receipts_path = folder / "lasso-campaign-stage-receipts.json"
    if backup.exists() or receipts_path.exists():
        raise FileExistsError("receipt directory already contains campaign staging evidence")
    backup.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipts = []
    def persist():
        receipts_path.write_text(
            json.dumps(receipts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    for item in rows:
        if not isinstance(item, dict) or not all(item.get(key) for key in (
            "row", "artifact_tenant", "source_hash", "policy_version", "image_sha256"
        )):
            result = {"result": "conflict", "reason": "invalid_manifest_item"}
        else:
            try:
                result = (call_fn or _call)(store, item)
            except Exception as exc:
                receipts.append({"id": (item.get("row") or {}).get("id"),
                                 "result": "error", "reason": type(exc).__name__})
                persist()
                raise
        receipts.append({"id": (item.get("row") or {}).get("id"), **result})
        persist()
    return receipts


def main(argv=None):
    from agent.portal_calendar_store import SupabaseCalendarStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--receipt-dir", required=True)
    args = parser.parse_args(argv)
    payload, rows = _load(args.manifest)
    receipts = apply_manifest(payload, rows, SupabaseCalendarStore(), args.receipt_dir)
    print(json.dumps({key: sum(r["result"] == key for r in receipts) for key in sorted(ALLOWED_RESULTS)}))


if __name__ == "__main__":
    main()
