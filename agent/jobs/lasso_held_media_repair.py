"""Bounded autonomous visual repair for LASSO's held feed runway.

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
CAPTION_HOLD_REASON = "caption_changed_needs_new_visual"
GYM = "lasso"
ACCOUNTS = {"instagram": "lasso_ig", "facebook": "lasso_fb"}
ACCOUNT = "lasso_ig"
MAX_PER_DAY = 3
HORIZON_DAYS = 1
INCIDENT_BACKLOG_FIRST = "2026-10-02"
INCIDENT_BACKLOG_LAST = "2026-10-05"
INCIDENT_RECOVERY_FIRST = "2026-10-06"
INCIDENT_RECOVERY_LAST = "2026-10-11"
MAX_INCIDENT_BACKLOG_PER_RUN = 2
_CAS_COLUMNS = (
    "id", "gym_id", "status", "variant_status", "account", "format",
    "post_date", "caption", "image_url", "source_media_url",
    "source_media_asset_id", "thumbnail_url", "created_at",
    "media_not_ready_reason", "published_at", "late_post_id",
    "publish_claim_token", "publish_reservation_day", "slot_index",
    "scheduled_at", "logical_post_id",
)


def _local_day(now):
    instant = now or datetime.now(ZoneInfo(config.POSTING_TIMEZONE))
    if isinstance(instant, str):
        instant = datetime.fromisoformat(instant)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=ZoneInfo(config.POSTING_TIMEZONE))
    return instant.astimezone(ZoneInfo(config.POSTING_TIMEZONE)).date()


def _row_account_key(row):
    platform = str(row.get("account") or "").lower()
    return ACCOUNTS.get("instagram" if platform == "ig" else platform)


def _eligible(row, first, last, account_key=ACCOUNT):
    fmt = str((row or {}).get("format") or "").lower()
    reason = (row or {}).get("media_not_ready_reason")
    return (isinstance(row, dict)
            and all(key in row for key in _CAS_COLUMNS)
            and row["gym_id"] == GYM
            and row["status"] == "pending"
            and row["variant_status"] == "active"
            and _row_account_key(row) == account_key
            and (fmt == "feed" or (fmt == "story" and reason == CAPTION_HOLD_REASON))
            and first <= str(row["post_date"] or "")[:10] <= last
            and reason in (HOLD_REASON, CAPTION_HOLD_REASON)
            and isinstance(row["caption"], str)
            and (fmt == "story" or bool(row["caption"].strip()))
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
    return f"eq.{value}"


def _reviewed_artifact_record(store, source_id, source_hash, account_key=ACCOUNT):
    """Reuse a persisted reviewed image before any new paid generation.

    A failed or incomplete lookup is an error, not a cache miss: generating in
    that state could bill repeatedly for the same row after a process restart.
    """
    response = store._client().get(
        store._rest("echo_infographic_artifacts"),
        params={"tenant": f"eq.{account_key}",
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
            return row
    return None


def _reviewed_existing_artifact(store, source_id, source_hash, account_key=ACCOUNT):
    record = _reviewed_artifact_record(store, source_id, source_hash, account_key)
    return record["image_url"] if record else None


def _mirror_ig_sibling(store, fb_row):
    """Find one exact active IG sibling, or refuse an ambiguous mirror.

    A present logical_post_id is authoritative. Legacy rows use their date and
    slot; exact caption equality keeps platform-specific FB copy independent.
    Returns (sibling, ambiguous_or_missing_logical_group).
    """
    logical_id = fb_row.get("logical_post_id")
    day = str(fb_row["post_date"])[:10]
    if logical_id:
        rows = store.list_active_logical_post_rows(GYM, logical_id)
    else:
        rows = store.rows_in_range_repeat_hold(GYM, day, day)
    if not isinstance(rows, list):
        raise RuntimeError("mirror sibling read incomplete")
    same_slot = [row for row in rows if isinstance(row, dict)
                 and row.get("gym_id") == GYM
                 and str(row.get("account") or "").lower() in ("instagram", "ig")
                 and str(row.get("format") or "").lower() == "feed"
                 and row.get("variant_status") == "active"
                 and str(row.get("post_date") or "")[:10] == day
                 and row.get("slot_index") == fb_row.get("slot_index")
                 and (not logical_id or row.get("logical_post_id") == logical_id)]
    exact = [row for row in same_slot if row.get("caption") == fb_row["caption"]]
    if len(exact) > 1 or (logical_id and len(same_slot) != 1):
        return None, True
    if not exact:
        return None, False
    return exact[0], False


def _reuse_ig_for_fb(store, fb_row):
    """Return (reviewed URL, mirror_found) after FB provenance is durable.

    A matching mirror whose IG visual is not yet repaired waits. It never pays
    for a different FB visual merely because the IG artifact is unavailable.
    """
    ig_row, ambiguous = _mirror_ig_sibling(store, fb_row)
    if ambiguous:
        return None, True
    if ig_row is None:
        return None, False
    if ig_row.get("media_not_ready_reason") is not None:
        return None, True
    if not _same_row(ig_row, store.get_row(GYM, ig_row["id"])):
        return None, True
    ig_source_id = f"content_calendar:{ig_row['id']}:caption"
    source_hash = hashlib.sha256(fb_row["caption"].encode("utf-8")).hexdigest()
    ig_artifact = _reviewed_artifact_record(store, ig_source_id, source_hash, ACCOUNT)
    if not ig_artifact or ig_row.get("image_url") != ig_artifact["image_url"]:
        return None, True

    fb_source = {"source_id": f"content_calendar:{fb_row['id']}:caption",
                 "source_hash": source_hash}
    fb_evidence = dict(ig_artifact["evidence"])
    fb_evidence["mirror_reuse_from"] = {
        "tenant": ACCOUNT, "source_id": ig_source_id,
        "source_hash": source_hash}
    url = ig_artifact["image_url"]
    # ArtifactStore's source-aware cache query is by row/caption. Its table's
    # unique tenant+URL key also needs checking before insert so another FB
    # source's evidence is never overwritten by a mirror reuse.
    def fb_record_for_url():
        response = store._client().get(
            store._rest("echo_infographic_artifacts"),
            params={"tenant": f"eq.{ACCOUNTS['facebook']}", "image_url": _eq(url),
                    "select": "image_url,evidence,source_identity", "limit": "2"},
            headers=store._headers(), timeout=30)
        if response.status_code >= 400:
            raise RuntimeError("FB reviewed artifact lookup failed")
        rows = response.json()
        if not isinstance(rows, list) or len(rows) > 1:
            raise RuntimeError("FB reviewed artifact lookup incomplete")
        return rows[0] if rows else None

    existing = fb_record_for_url()
    if existing is None:
        response = store._client().post(
            store._rest("echo_infographic_artifacts"),
            params={"on_conflict": "tenant,image_url"},
            headers=store._headers({"Content-Type": "application/json",
                                    "Prefer": "resolution=ignore-duplicates"}),
            json={"tenant": ACCOUNTS["facebook"], "image_url": url,
                  "image_sha256": fb_evidence["image_sha256"],
                  "evidence": fb_evidence, "source_identity": fb_source},
            timeout=30)
        if response.status_code >= 400:
            raise RuntimeError("FB mirror artifact persistence failed")
        existing = fb_record_for_url()
    if (not isinstance(existing, dict)
            or existing.get("source_identity") != fb_source
            or existing.get("image_url") != url
            or existing.get("evidence", {}).get("mirror_reuse_from") !=
                fb_evidence["mirror_reuse_from"]
            or existing.get("evidence", {}).get("image_sha256") !=
                ig_artifact["evidence"]["image_sha256"]):
        raise RuntimeError("FB mirror artifact provenance conflict")
    if not _same_row(ig_row, store.get_row(GYM, ig_row["id"])):
        return None, True
    return url, True


def _paired_feed_for_story(store, story):
    """Return the exact reviewed feed source; hold on ambiguity or stale media."""
    day = str(story["post_date"])[:10]
    logical_id = story.get("logical_post_id")
    rows = (store.list_active_logical_post_rows(GYM, logical_id) if logical_id
            else store.rows_in_range_repeat_hold(GYM, day, day))
    if not isinstance(rows, list):
        raise RuntimeError("paired Story feed read incomplete")
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("gym_id") == GYM
               and str(row.get("account") or "").lower() ==
                   str(story.get("account") or "").lower()
               and str(row.get("format") or "").lower() == "feed"
               and row.get("variant_status") == "active"
               and str(row.get("post_date") or "")[:10] == day
               and row.get("slot_index") == story.get("slot_index")
               and (not logical_id or row.get("logical_post_id") == logical_id)]
    if len(matches) != 1:
        return None
    feed = matches[0]
    if (feed.get("media_not_ready_reason") is not None
            or feed.get("status") not in ("pending", "approved", "published")
            or not isinstance(feed.get("caption"), str)
            or not feed["caption"].strip()
            or story.get("caption") not in ("", feed["caption"])):
        return None
    if not _same_row(feed, store.get_row(GYM, feed["id"])):
        return None
    source_id = f"content_calendar:{feed['id']}:caption"
    source_hash = hashlib.sha256(feed["caption"].encode("utf-8")).hexdigest()
    artifact = _reviewed_artifact_record(
        store, source_id, source_hash, _row_account_key(feed))
    if not artifact or artifact["image_url"] != feed.get("image_url"):
        return None
    return feed


def _replace_exact(store, current, new_url, account_key=ACCOUNT):
    """Clear one hold only if every generation-relevant row field still matches."""
    if not _eligible(current, str(current["post_date"])[:10],
                     str(current["post_date"])[:10], account_key):
        return None
    if not isinstance(new_url, str) or not new_url.startswith("https://"):
        return None
    if (current["media_not_ready_reason"] == CAPTION_HOLD_REASON
            and new_url == current["image_url"]):
        return None  # a caption change needs a genuinely new reviewed visual
    params = {key: _eq(current[key]) for key in _CAS_COLUMNS}
    payload = {"image_url": new_url, "source_media_url": new_url,
               "source_media_asset_id": None,
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
        host_fn=None, max_per_day=MAX_PER_DAY, account_key=ACCOUNT,
        include_incident_backlog=False):
    """Repair current runway; optional dated outage replay is capped per run.

    The normal daily runner does not request the historical window. A recovery
    invocation can opt in only during October 6–11, after caption CAS has put
    changed rows on hold. No row is published by this job.
    """
    summary = {"ok": False, "attempted": 0, "generated": 0, "reused": 0,
               "repaired": 0, "skipped": 0, "errors": 0}
    if not (account_key in ACCOUNTS.values()
            and config.lasso_three_feed_enabled()
            and config.lasso_infographic_quality_enabled(account_key)
            and variant_regen.enabled()):
        summary["reason"] = "lasso reviewed regeneration not armed"
        return summary
    if not isinstance(max_per_day, int) or not 1 <= max_per_day <= MAX_PER_DAY:
        summary["reason"] = "invalid repair cap"
        return summary
    day = _local_day(now)
    today = day.isoformat()
    if include_incident_backlog and not (
            INCIDENT_RECOVERY_FIRST <= today <= INCIDENT_RECOVERY_LAST):
        summary["reason"] = "incident recovery window closed"
        return summary
    first = INCIDENT_BACKLOG_FIRST if include_incident_backlog else today
    last = (day + timedelta(days=HORIZON_DAYS)).isoformat()
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
    rows = sorted((row for row in rows if _eligible(row, first, last, account_key)
                   and (str(row["post_date"])[:10] >= today
                        or INCIDENT_BACKLOG_FIRST <= str(row["post_date"])[:10]
                                                 <= INCIDENT_BACKLOG_LAST)),
                  key=lambda row: (str(row["post_date"])[:10] < today,
                                   row["post_date"],
                                   0 if row["format"] == "feed" else 1,
                                   row.get("slot_index") or 0, str(row["id"])))
    per_day = {}
    backlog_attempted = 0
    for row in rows:
        row_day = str(row["post_date"])[:10]
        incident_backlog = row_day < today
        if incident_backlog and backlog_attempted >= MAX_INCIDENT_BACKLOG_PER_RUN:
            summary["skipped"] += 1
            continue
        kind = str(row["format"]).lower()
        count_key = (row_day, kind)
        if per_day.get(count_key, 0) >= max_per_day:
            summary["skipped"] += 1
            continue
        source_row = row
        if kind == "story":
            try:
                feed = _paired_feed_for_story(store, row)
            except Exception:
                summary["errors"] += 1
                continue
            if feed is None:
                summary["skipped"] += 1
                continue
            source_row = dict(row, caption=feed["caption"])
        source_id = f"content_calendar:{row['id']}:caption"
        source_hash = hashlib.sha256(source_row["caption"].encode("utf-8")).hexdigest()
        # One distributed lease per row/caption serializes paid attempts.
        cache_key = "held-feed:" + hashlib.sha256(
            f"{source_id}:{source_hash}".encode("utf-8")).hexdigest()
        owner = str(uuid.uuid4())
        claimed = False
        try:
            claimed = artifact_store.claim(account_key, cache_key, owner)
            if not claimed:
                summary["skipped"] += 1
                continue
            summary["attempted"] += 1
            if incident_backlog:
                backlog_attempted += 1
            per_day[count_key] = per_day.get(count_key, 0) + 1
            fresh = store.get_row(GYM, row["id"])
            if not _same_row(row, fresh):
                summary["skipped"] += 1
                continue
            url = None
            if kind == "feed" and account_key == ACCOUNTS["facebook"]:
                url, mirror_found = _reuse_ig_for_fb(store, row)
                if mirror_found and not url:
                    summary["skipped"] += 1
                    continue
            if not url:
                url = _reviewed_existing_artifact(store, source_id, source_hash,
                                                  account_key)
            if url:
                summary["reused"] += 1
            else:
                result = variant_regen.generate_variant_image(
                    source_row, account_key, generate_fn=generate_fn, host_fn=host_fn)
                if not result.get("ok"):
                    summary["errors"] += 1
                    continue
                url = result["image_url"]
                summary["generated"] += 1
            if row["media_not_ready_reason"] == CAPTION_HOLD_REASON:
                reviewed = _reviewed_artifact_record(
                    store, source_id, source_hash, account_key)
                if not reviewed or reviewed["image_url"] != url:
                    summary["errors"] += 1
                    continue
            if kind == "story" and _paired_feed_for_story(store, row) is None:
                summary["skipped"] += 1
                continue
            fresh = store.get_row(GYM, row["id"])
            if not _same_row(row, fresh):
                summary["skipped"] += 1
                continue
            if _replace_exact(store, fresh, url, account_key):
                summary["repaired"] += 1
            else:
                summary["skipped"] += 1
        except Exception:
            summary["errors"] += 1
        finally:
            if claimed:
                try:
                    artifact_store.release(account_key, cache_key, owner)
                except Exception:
                    summary["errors"] += 1
    return summary
