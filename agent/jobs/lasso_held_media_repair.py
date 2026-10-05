"""Bounded autonomous visual repair for LASSO's held Instagram feed runway.

The caption is the generation source. A reviewed artifact is required before an
exact-row compare-and-swap can replace the repeated image and clear its hold.
This job never claims or publishes a calendar row.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent import config, infographic_evidence, variant_regen, visual_writer_prepare
from agent.infographic_artifacts import ArtifactStore
from agent.portal_calendar_store import SupabaseCalendarStore

HOLD_REASON = "cross_date_media_repeat_needs_new_visual"
GYM = "lasso"
ACCOUNT = "lasso_ig"
MAX_PER_DAY = 3
HORIZON_DAYS = 1
_CAS_COLUMNS = (
    "id", "gym_id", "status", "variant_status", "account", "format",
    "post_date", "caption", "image_url", "source_media_url",
    "source_media_asset_id", "drive_file_id", "thumbnail_url", "created_at",
    "media_not_ready_reason", "published_at", "late_post_id",
    "publish_claim_token", "publish_reservation_day", "slot_index",
    "scheduled_at",
)


def _local_day(now):
    instant = now or datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    if isinstance(instant, str):
        instant = datetime.fromisoformat(instant)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=ZoneInfo(config.POSTING_TIMEZONE))
    return instant.astimezone(ZoneInfo(config.POSTING_TIMEZONE)).date()


def _eligible(row, first, last):
    return (isinstance(row, dict)
            and all(key in row for key in _CAS_COLUMNS)
            and row["gym_id"] == GYM
            and row["status"] == "pending"
            and row["variant_status"] == "active"
            and str(row["account"] or "").lower() in ("instagram", "ig")
            and str(row["format"] or "").lower() == "feed"
            and first <= str(row["post_date"] or "")[:10] <= last
            and row["media_not_ready_reason"] == HOLD_REASON
            and isinstance(row["caption"], str) and bool(row["caption"].strip())
            and isinstance(row["image_url"], str) and bool(row["image_url"].strip())
            and row["published_at"] is None and row["late_post_id"] is None
            and row["publish_claim_token"] is None
            and row["publish_reservation_day"] is None)


def _same_row(left, right):
    return isinstance(right, dict) and all(
        right.get(key) == left[key] for key in _CAS_COLUMNS)


def _eq(value):
    if value is None:
        return "is.null"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'eq."{escaped}"'


def _reviewed_existing_artifact(store, source_id, source_hash):
    """Reuse a persisted reviewed image before any new paid generation.

    A failed or incomplete lookup is an error, not a cache miss: generating in
    that state could bill repeatedly for the same row after a process restart.
    """
    response = store._client().get(
        store._rest("echo_infographic_artifacts"),
        params={"tenant": f"eq.{ACCOUNT}",
                "source_identity->>source_id": _eq(source_id),
                "source_identity->>source_hash": _eq(source_hash),
                "select": "image_url,evidence,source_identity",
                "order": "created_at.desc", "limit": "2"},
        headers=store._headers(), timeout=30)
    if response.status_code >= 400:
        raise RuntimeError("reviewed artifact lookup failed")
    rows = response.json()
    if not isinstance(rows, list) or len(rows) > 2:
        raise RuntimeError("reviewed artifact lookup incomplete")
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("reviewed artifact lookup malformed")
        evidence = row.get("evidence") or {}
        if (row.get("source_identity") == {"source_id": source_id,
                                            "source_hash": source_hash}
                and isinstance(row.get("image_url"), str)
                and row["image_url"].startswith("https://")
                and evidence.get("policy_version") == infographic_evidence.POLICY_VERSION
                and evidence.get("brain_snapshot") == infographic_evidence.brain_snapshot()
                and evidence.get("brief_model") == "gpt-6-astra"
                and evidence.get("grade_status") == "PASS"
                and evidence.get("image_sha256")
                and evidence.get("review_response_id")):
            return row["image_url"]
    return None


def _replace_exact(store, current, new_url):
    """Clear one hold only if every generation-relevant row field still matches."""
    if not _eligible(current, str(current["post_date"])[:10],
                     str(current["post_date"])[:10]):
        return None
    if not isinstance(new_url, str) or not new_url.startswith("https://"):
        return None
    params = {key: _eq(current[key]) for key in _CAS_COLUMNS}
    payload = {"image_url": new_url, "source_media_url": new_url,
               "source_media_asset_id": None, "drive_file_id": None,
               "thumbnail_url": None, "media_not_ready_reason": None}
    if visual_writer_prepare.enabled():
        payload = store._prepare_visual_replacement(GYM, current, payload)
        params = store._visual_media_cas(current, params)
    response = store._client().patch(
        store._rest("content_calendar"), params=params,
        headers=store._headers({"Content-Type": "application/json",
                                "Prefer": "return=representation"}),
        json=payload, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError("held media exact-row swap failed")
    rows = response.json()
    if visual_writer_prepare.enabled() and store._visual_media_result(
            rows, GYM, current, payload) is None:
        return None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    after = rows[0]
    if any(after.get(key) != payload.get(key, current[key]) for key in _CAS_COLUMNS):
        return None
    if any(after.get(key) != value for key, value in payload.items()):
        return None
    return after


def run(*, now=None, store=None, artifact_store=None, generate_fn=None,
        host_fn=None, max_per_day=MAX_PER_DAY):
    """Repair at most three held feed visuals per local date, today and tomorrow."""
    summary = {"ok": False, "attempted": 0, "generated": 0, "reused": 0,
               "repaired": 0, "skipped": 0, "errors": 0}
    if not (config.lasso_three_feed_enabled()
            and config.lasso_infographic_quality_enabled(ACCOUNT)
            and variant_regen.enabled()):
        summary["reason"] = "lasso reviewed regeneration not armed"
        return summary
    if not isinstance(max_per_day, int) or not 1 <= max_per_day <= MAX_PER_DAY:
        summary["reason"] = "invalid repair cap"
        return summary
    day = _local_day(now)
    first, last = day.isoformat(), (day + timedelta(days=HORIZON_DAYS)).isoformat()
    store = store or SupabaseCalendarStore()
    artifact_store = artifact_store or ArtifactStore()
    if not artifact_store.available:
        summary["reason"] = "artifact store unavailable"
        return summary
    try:
        rows = store.list_pending_media_between(GYM, first, last)
    except Exception:
        summary["reason"] = "held calendar read incomplete"
        return summary
    if not isinstance(rows, list):
        summary["reason"] = "held calendar read malformed"
        return summary
    summary["ok"] = True
    rows = sorted((row for row in rows if _eligible(row, first, last)),
                  key=lambda row: (row["post_date"], row.get("slot_index") or 0,
                                   str(row["id"])))
    per_day = {first: 0, last: 0}
    for row in rows:
        row_day = str(row["post_date"])[:10]
        if per_day[row_day] >= max_per_day:
            summary["skipped"] += 1
            continue
        source_id = f"content_calendar:{row['id']}:caption"
        source_hash = hashlib.sha256(row["caption"].encode("utf-8")).hexdigest()
        # One distributed lease per row/caption serializes paid attempts.
        cache_key = "held-feed:" + hashlib.sha256(
            f"{source_id}:{source_hash}".encode("utf-8")).hexdigest()
        owner = str(uuid.uuid4())
        try:
            if not artifact_store.claim(ACCOUNT, cache_key, owner):
                summary["skipped"] += 1
                continue
            summary["attempted"] += 1
            per_day[row_day] += 1
            fresh = store.get_row(GYM, row["id"])
            if not _same_row(row, fresh):
                summary["skipped"] += 1
                continue
            url = _reviewed_existing_artifact(store, source_id, source_hash)
            if url:
                summary["reused"] += 1
            else:
                result = variant_regen.generate_variant_image(
                    row, ACCOUNT, generate_fn=generate_fn, host_fn=host_fn)
                if not result.get("ok"):
                    summary["errors"] += 1
                    continue
                url = result["image_url"]
                summary["generated"] += 1
            fresh = store.get_row(GYM, row["id"])
            if not _same_row(row, fresh):
                summary["skipped"] += 1
                continue
            if _replace_exact(store, fresh, url):
                summary["repaired"] += 1
            else:
                summary["skipped"] += 1
        except Exception:
            summary["errors"] += 1
        finally:
            try:
                artifact_store.release(ACCOUNT, cache_key, owner)
            except Exception:
                summary["errors"] += 1
    return summary
