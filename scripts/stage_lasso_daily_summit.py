#!/usr/bin/env python3
"""Stage the approved daily LASSO Summit third feed slot, with explicit review.

This command is deliberately narrow.  It consumes the frozen before snapshot and
three-slot plan, and can alter only a pending, active existing Summit row's
ordinal or insert the exact reviewed daily catalog creative.  It never approves,
publishes, schedules immediately, regenerates creative, or changes regular rows.
"""

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EVIDENCE = Path("/Users/blakeruff/Documents/Codex/echo-calendar-refresh-evidence-20260923")
DEFAULT_SNAPSHOT = EVIDENCE / "calendar-before-full.json"
DEFAULT_THREE_SLOT_PLAN = EVIDENCE / "calendar-three-slot-plan.json"
DEFAULT_CATALOG = ROOT / "brand_voice" / "lasso_summit_daily.json"
TARGET_ACCOUNTS = ("instagram", "facebook")
TARGET_GYM = "lasso"
CAMPAIGN = "lasso_summit_daily_20260923"
START, END = "2026-09-23", "2026-11-08"
PENDING = {"pending"}
OCCUPYING = {"pending", "approved", "publishing", "published", "draft"}
PROTECTED = {"approved", "published", "publishing", "denied", "killed", "failed"}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rows(payload):
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("calendar snapshot must contain a rows list")
    return rows


def _source_identity(entry):
    """Keep the cache identity byte-compatible with agent.lasso_daily_summit."""
    from agent.lasso_daily_summit import _catalog_entry, _source_identity as source
    resolved = _catalog_entry(entry["date"], catalog_path=entry["_catalog_path"])
    if resolved is None:
        raise ValueError("invalid catalog entry")
    return source(resolved)


def _catalog(path):
    payload = _json(path)
    entries = payload.get("posts", [])
    found = {}
    for raw in entries:
        day = str(raw.get("date") or "")
        if day in found:
            raise ValueError("catalog contains duplicate date: " + day)
        if not raw.get("caption") or not isinstance(raw.get("on_image"), dict):
            raise ValueError("catalog entry lacks exact approved content: " + day)
        entry = dict(raw)
        entry["_catalog_path"] = str(path)
        found[day] = entry
    expected = [(date.fromisoformat(START)).fromordinal(date.fromisoformat(START).toordinal() + i).isoformat()
                for i in range((date.fromisoformat(END) - date.fromisoformat(START)).days + 1)]
    if sorted(found) != expected:
        raise ValueError("catalog must contain exactly Sep 23 through Nov 8")
    return found


def deterministic_id(account, day):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TARGET_GYM}|{account}|{day}|{CAMPAIGN}"))


def _active(row):
    return str(row.get("variant_status") or "active").lower() == "active"


def _in_scope(day, account):
    try:
        parsed = date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return False
    return (date.fromisoformat(START) <= parsed <= date.fromisoformat(END)
            and str(account).lower() in TARGET_ACCOUNTS)


def _is_summit(row):
    text = " ".join(str(row.get(k) or "") for k in ("caption", "pillar", "category", "draft_type")).lower()
    return "summit" in text or "nashville" in text


def _cached_artifact(artifacts, cache_key, expected_source):
    """Return the exact service-stored reviewed record behind a valid cache hit."""
    try:
        url = artifacts.cached(TARGET_GYM, cache_key) if artifacts else None
        if not url:
            return None
        response = artifacts.http.get(artifacts.url, headers=artifacts.headers, params={
            "tenant": "eq." + TARGET_GYM, "cache_key": "eq." + cache_key,
            "image_url": "eq." + url,
            "select": "tenant,image_url,image_sha256,evidence,source_identity",
            "order": "created_at.desc", "limit": "1"}, timeout=10)
        if response.status_code >= 300:
            return None
        records = response.json() or []
        if len(records) != 1:
            return None
        record = records[0]
        evidence = record.get("evidence") or {}
        source = record.get("source_identity") or {}
        digest = str(record.get("image_sha256") or "")
        if (record.get("tenant") != TARGET_GYM or record.get("image_url") != url
                or len(digest) != 64 or evidence.get("grade_status") != "PASS"
                or evidence.get("image_sha256") != digest or not evidence.get("policy_version")
                or source.get("source_id") != expected_source.get("source_id")
                or source.get("source_hash") != expected_source.get("source_hash")):
            return None
        return {"artifact_tenant": TARGET_GYM, "image_url": url,
                "image_sha256": digest, "policy_version": evidence["policy_version"],
                "source_hash": source["source_hash"]}
    except Exception:
        return None


def build_plan(snapshot, three_slot_plan, catalog_path=DEFAULT_CATALOG, *, artifact_store=None):
    """Make a no-write manifest from the supplied frozen evidence files."""
    catalog = _catalog(catalog_path)
    before = _rows(snapshot)
    known_columns = set().union(*(row.keys() for row in before)) if before else set()
    by_id = {str(row.get("id")): row for row in before if row.get("id")}
    actions, blocked = [], []
    # The upstream plan owns regular creative.  This lane consumes only its explicit
    # Summit actions, preventing a staging pass from replacing a regular slot.
    for mutation in three_slot_plan.get("mutations", []):
        if mutation.get("reason") != "move_existing_summit_original_to_additive_slot":
            continue
        row = by_id.get(str(mutation.get("row_id")))
        if not _in_scope(mutation.get("date"), mutation.get("account")):
            blocked.append({"operation": "move", "row_id": mutation.get("row_id"), "reason": "out_of_scope_input"})
            continue
        if not row:
            blocked.append({"operation": "move", "row_id": mutation.get("row_id"), "reason": "snapshot_row_missing"})
            continue
        expected = {"id": row["id"], "gym_id": TARGET_GYM, "post_date": mutation["date"],
                    "account": mutation["account"], "format": "feed", "status": str(row.get("status") or "pending"),
                    "variant_status": row.get("variant_status") or "active", "slot_index": row.get("slot_index"),
                    "image_url": row.get("image_url"), "caption": row.get("caption"),
                    "late_post_id": row.get("late_post_id"), "published_at": row.get("published_at")}
        if (str(row.get("gym_id")) != TARGET_GYM or str(row.get("account")).lower() != mutation["account"]
                or str(row.get("format")).lower() != "feed" or str(row.get("post_date"))[:10] != mutation["date"]
                or not _active(row) or expected["status"] not in PENDING or not _is_summit(row)
                or expected["late_post_id"] is not None or expected["published_at"] is not None):
            blocked.append({"operation": "move", "row_id": row["id"], "reason": "not_mutable_pending_active_summit"})
            continue
        actions.append({"operation": "guarded_move", "date": mutation["date"], "account": mutation["account"],
                        "expected": expected, "set": {"slot_index": 2, "scheduled_at": mutation["set"].get("scheduled_at")},
                        "preserves": ["caption", "image_url", "status", "pillar", "published_at", "late_post_id"]})
    for insert in three_slot_plan.get("inserts", []):
        row = insert.get("row") or {}
        if (row.get("pillar") != "summit" or not _in_scope(row.get("post_date"), row.get("account"))
                or str(row.get("gym_id")) != TARGET_GYM or str(row.get("format")).lower() != "feed"
                or int(row.get("slot_index", 2)) != 2):
            continue
        entry = catalog.get(str(row.get("post_date")))
        if not entry:
            blocked.append({"operation": "insert", "date": row.get("post_date"), "reason": "catalog_entry_missing"})
            continue
        source, cache_key = _source_identity(entry)
        artifact = _cached_artifact(artifact_store, cache_key, source)
        catalog_url = str(entry.get("hosted_image_url") or "").strip()
        if not artifact or not catalog_url or catalog_url != artifact["image_url"]:
            blocked.append({"operation": "insert", "date": row["post_date"], "account": row["account"],
                            "reason": "reviewed_artifact_missing", "source_identity": source})
            continue
        new_row = {"id": deterministic_id(row["account"], row["post_date"]), "gym_id": TARGET_GYM,
                   "account": row["account"], "format": "feed", "post_date": row["post_date"],
                   "scheduled_at": row.get("scheduled_at"), "slot_index": 2, "status": "pending",
                   "variant_status": "active", "pillar": "summit", "caption": entry["caption"], "image_url": artifact["image_url"]}
        if not set(new_row).issubset(known_columns):
            raise ValueError("insert row contains columns absent from frozen calendar schema")
        actions.append({"operation": "insert_if_primary_key_missing", "date": row["post_date"],
                        "account": row["account"], "row": new_row,
                        "uniqueness": {"id": new_row["id"]}, "source_identity": source,
                        "artifact_tenant": artifact["artifact_tenant"],
                        "image_sha256": artifact["image_sha256"],
                        "policy_version": artifact["policy_version"]})
    return {"version": 1, "mode": "dry_plan_no_writes", "scope": {"gym_id": TARGET_GYM, "accounts": list(TARGET_ACCOUNTS), "start": START, "end": END, "format": "feed"},
            "inputs": {"snapshot": str(DEFAULT_SNAPSHOT), "three_slot_plan": str(DEFAULT_THREE_SLOT_PLAN), "catalog": str(catalog_path)},
            "guards": {"only_pending_active_summit_moves": True, "preserve_supplied_original_caption_and_art": True,
                       "protected_statuses_untouched": sorted(PROTECTED), "catalog_requires_exact_artifact_cache_identity": True,
                       "cas_includes_id_gym_date_original_image_caption_status_variant_and_publish_markers": True,
                       "insert_via_atomic_logical_slot_rpc": True, "read_current_day_after_every_write": True,
                       "never_publish_or_approve": True}, "actions": actions, "blocked": blocked}


def _current_day(store, day):
    response = store._client().get(store._rest("content_calendar"), params={"gym_id": "eq.lasso", "post_date": "eq." + day, "format": "eq.feed", "variant_status": "eq.active"}, headers=store._headers(), timeout=30)
    if response.status_code >= 400:
        raise RuntimeError("current-day read failed")
    return response.json() or []


def _assert_one_summit(rows, account):
    active = [row for row in rows if str(row.get("account") or "").lower() == account
              and str(row.get("status") or "pending").lower() in OCCUPYING and _is_summit(row)]
    if len(active) != 1 or any(int(row.get("slot_index", -1)) != 2 for row in active):
        raise RuntimeError("current day does not contain exactly one Summit at ordinal 2")


def campaign_shape_report(store, *, current_day_fn=None):
    """Read back every campaign day; only 0/1 regular plus 2 Summit is complete."""
    reader = current_day_fn or _current_day
    first, last = date.fromisoformat(START), date.fromisoformat(END)
    days = []
    for ordinal in range(first.toordinal(), last.toordinal() + 1):
        day = date.fromordinal(ordinal).isoformat()
        try:
            rows = reader(store, day)
        except Exception as exc:
            for account in TARGET_ACCOUNTS:
                days.append({"date": day, "account": account, "status": "read_error",
                             "error": type(exc).__name__})
            continue
        for account in TARGET_ACCOUNTS:
            active = [row for row in rows
                      if str(row.get("account") or "").lower() == account
                      and str(row.get("variant_status") or "active").lower() == "active"
                      and str(row.get("status") or "pending").lower() in OCCUPYING]
            regular = [row for row in active if str(row.get("pillar") or "").lower() != "summit"]
            summit = [row for row in active if str(row.get("pillar") or "").lower() == "summit"]
            regular_slots = sorted((row.get("slot_index") for row in regular),
                                   key=lambda value: (value is None, str(value)))
            summit_slots = [row.get("slot_index") for row in summit]
            complete = (len(active) == 3 and regular_slots == [0, 1]
                        and summit_slots == [2])
            days.append({"date": day, "account": account,
                         "status": "complete" if complete else "partial",
                         "regular_slots": regular_slots, "summit_slots": summit_slots,
                         "active_row_ids": [row.get("id") for row in active]})
    return {"scope": {"gym_id": TARGET_GYM, "start": START, "end": END,
                      "accounts": list(TARGET_ACCOUNTS)},
            "complete": sum(day["status"] == "complete" for day in days),
            "total": len(days), "days": days}


def _cas_move(store, expected, values):
    params = {"id": "eq." + str(expected["id"]), "gym_id": "eq.lasso", "post_date": "eq." + expected["post_date"],
              "account": "eq." + expected["account"], "format": "eq.feed", "status": "eq." + expected["status"],
              "variant_status": "eq." + expected["variant_status"],
              "slot_index": "is.null" if expected["slot_index"] is None else "eq." + str(expected["slot_index"]),
              "image_url": "eq." + str(expected["image_url"]), "caption": "eq." + str(expected["caption"]),
              "late_post_id": "is.null", "published_at": "is.null"}
    response = store._client().patch(store._rest("content_calendar"), params=params,
        headers=store._headers({"Content-Type": "application/json", "Prefer": "return=representation"}), json=values, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError("conditional Summit move failed")
    rows = response.json() or []
    return rows[0] if len(rows) == 1 else None


def _atomic_insert(store, action):
    response = store._client().post(store._rest("rpc/stage_lasso_campaign_row"),
        headers=store._headers({"Content-Type": "application/json"}), json={
            "p_row": action["row"], "p_artifact_tenant": action["artifact_tenant"],
            "p_source_hash": action["source_identity"]["source_hash"],
            "p_policy_version": action["policy_version"],
            "p_image_sha256": action["image_sha256"]}, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError("atomic Summit insert failed")
    result = response.json()
    if not isinstance(result, dict) or result.get("result") not in {"inserted", "idempotent", "conflict"}:
        raise RuntimeError("atomic Summit insert returned an invalid receipt")
    return result


def apply(plan, store, receipt_dir, *, current_day_fn=None, move_fn=None, insert_fn=None):
    """Apply the reviewed manifest once.  A zero-row CAS is a conflict and is never retried."""
    folder = Path(receipt_dir); folder.mkdir(parents=True, exist_ok=True)
    before_path, receipt_path = folder / "calendar-before-apply.json", folder / "summit-stage-receipts.json"
    before, receipts = [], []
    def persist():
        before_path.write_text(json.dumps(before, indent=2, sort_keys=True, default=str) + "\n")
        receipt_path.write_text(json.dumps(receipts, indent=2, sort_keys=True, default=str) + "\n")
    reader = current_day_fn or _current_day
    for action in plan.get("actions", []):
        day, account = action["date"], action["account"]
        if not _in_scope(day, account):
            receipts.append({"date": day, "account": account, "operation": action.get("operation"), "status": "out_of_scope_input"}); persist(); continue
        proposed = action.get("expected") if action.get("operation") == "guarded_move" else action.get("row")
        if not isinstance(proposed, dict) or proposed.get("gym_id") != TARGET_GYM or proposed.get("account") != account or proposed.get("post_date") != day or proposed.get("format") != "feed":
            receipts.append({"date": day, "account": account, "operation": action.get("operation"), "status": "out_of_scope_input"}); persist(); continue
        rows = reader(store, day); before.append({"date": day, "account": account, "rows": rows}); persist()
        if action["operation"] == "guarded_move":
            expected = action["expected"]
            if (expected.get("gym_id") != TARGET_GYM or expected.get("account") != account
                    or expected.get("post_date") != day or expected.get("format") != "feed"
                    or expected.get("status") != "pending" or expected.get("variant_status") != "active"):
                receipts.append({"date": day, "account": account, "operation": "move", "status": "out_of_scope_input"}); persist(); continue
            if any(str(r.get("id")) != str(expected.get("id")) and str(r.get("account") or "").lower() == account
                   and str(r.get("status") or "pending").lower() in OCCUPYING
                   and (_is_summit(r) or r.get("slot_index") == 2) for r in rows):
                receipts.append({"date": day, "account": account, "operation": "move", "status": "conflict_existing_summit_or_slot"}); persist(); continue
            changed = (move_fn or _cas_move)(store, action["expected"], action["set"])
            if not changed:
                receipts.append({"date": day, "account": account, "operation": "move", "status": "conflict"}); persist(); continue
        else:
            new_row = action.get("row") or {}
            if (new_row.get("gym_id") != TARGET_GYM or new_row.get("account") != account
                    or new_row.get("post_date") != day or new_row.get("format") != "feed"
                    or new_row.get("slot_index") != 2 or new_row.get("status") != "pending"
                    or new_row.get("variant_status") != "active" or not new_row.get("id")):
                receipts.append({"date": day, "account": account, "operation": "insert", "status": "out_of_scope_input"}); persist(); continue
            if not all(action.get(key) for key in ("artifact_tenant", "source_identity", "policy_version", "image_sha256")):
                receipts.append({"date": day, "account": account, "operation": "insert", "status": "reviewed_artifact_missing"}); persist(); continue
            result = (insert_fn or _atomic_insert)(store, action)
            if result["result"] == "conflict":
                receipts.append({"date": day, "account": account, "operation": "insert", "status": "conflict",
                                 "reason": result.get("reason", "atomic_rpc_conflict")}); persist(); continue
            if result["result"] == "idempotent":
                try:
                    _assert_one_summit(reader(store, day), account)
                    status = "already_present"
                except Exception:
                    status = "readback_conflict"
                receipts.append({"date": day, "account": account, "operation": "insert", "status": status}); persist(); continue
        try:
            _assert_one_summit(reader(store, day), account)
            receipts.append({"date": day, "account": account, "operation": action["operation"], "status": "applied"})
        except Exception as exc:
            receipts.append({"date": day, "account": account, "operation": action["operation"], "status": "readback_conflict", "error": type(exc).__name__})
        persist()
    return receipts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT)); parser.add_argument("--three-slot-plan", default=str(DEFAULT_THREE_SLOT_PLAN))
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG)); parser.add_argument("--output", required=True)
    parser.add_argument("--apply", action="store_true"); parser.add_argument("--receipt-dir")
    parser.add_argument("--audit-only", action="store_true", help="read current campaign shape without writes")
    args = parser.parse_args(argv)
    if args.audit_only:
        from agent.portal_calendar_store import SupabaseCalendarStore
        report = campaign_shape_report(SupabaseCalendarStore())
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"complete": report["complete"], "total": report["total"]}))
        return 0 if report["complete"] == report["total"] else 1
    artifacts = None
    try:
        from agent.infographic_artifacts import ArtifactStore
        artifacts = ArtifactStore()
    except Exception:
        pass
    plan = build_plan(_json(args.snapshot), _json(args.three_slot_plan), args.catalog, artifact_store=artifacts)
    Path(args.output).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    if args.apply:
        if not args.receipt_dir: raise SystemExit("--apply requires --receipt-dir")
        from agent.portal_calendar_store import SupabaseCalendarStore
        receipts = apply(plan, SupabaseCalendarStore(), args.receipt_dir)
        report = campaign_shape_report(SupabaseCalendarStore())
        (Path(args.receipt_dir) / "campaign-day-shape.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"applied": sum(r["status"] == "applied" for r in receipts),
                          "receipts": len(receipts), "complete_days": report["complete"],
                          "total_days": report["total"]}))
    print(json.dumps({"mode": plan["mode"], "actions": len(plan["actions"]), "blocked": len(plan["blocked"])}))


if __name__ == "__main__":
    sys.exit(main())
