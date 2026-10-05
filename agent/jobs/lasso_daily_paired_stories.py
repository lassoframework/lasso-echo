"""Prepare and stage missing LASSO feed-paired Stories for today and tomorrow.

Each account/day/slot is inspected before any paid generation. One persisted
source-bound 9:16 PASS artifact is reused when available. A distributed
artifact lease and five-minute retry cooldown bound duplicate paid attempts.
The guarded Story RPC only stages; it never publishes or edits existing rows.
"""

import hashlib
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent import config, infographic_evidence, variant_regen, visual_writer_prepare
from agent.infographic_artifacts import ArtifactStore
from agent.portal_calendar_store import SupabaseCalendarStore
from agent.jobs import lasso_paired_story_backfill as stage
from agent.jobs import lasso_paired_story_repair as repair

ACCOUNTS = {"instagram": "lasso_ig", "facebook": "lasso_fb"}
MAX_PER_RUN = 9  # Six current/next-day pairs plus three catchup repairs.
MAX_BACKLOG_PER_RUN = 3


def _system_ready(store):
    response = store._client().post(
        store._rest("rpc/lasso_paired_story_system_ready"),
        headers=store._headers({"Content-Type": "application/json"}),
        json={}, timeout=10)
    return response.status_code == 200 and response.json() is True


def release_ready_holds(store, day, *, catchup_days=0):
    """Release DB-proven holds over the publisher's exact catchup window."""
    run_day = stage.date.fromisoformat(day)
    if type(catchup_days) is not int or catchup_days < 0:
        raise ValueError("invalid paired Story catchup window")
    end = min(stage.LAST, run_day)
    first = max(stage.FIRST, run_day - timedelta(days=catchup_days))
    if first > end:
        return {"released": 0, "blocked": 0}
    if not _system_ready(store):
        return {"released": 0, "blocked": 0}
    complete_reader = getattr(store, "rows_in_range_complete", None)
    if callable(complete_reader):
        rows = complete_reader("lasso", first.isoformat(), end.isoformat())
    else:
        # Narrow injected stores use the same complete, paginated day reader.
        rows = []
        for offset in range((end - first).days + 1):
            rows.extend(stage._active_day(store, (first + timedelta(days=offset)).isoformat()))
    if not isinstance(rows, list) or any(
        not isinstance(r, dict) or r.get("gym_id") != "lasso"
        or not first.isoformat() <= str(r.get("post_date") or "")[:10] <= end.isoformat()
        for r in rows):
        raise RuntimeError("paired Story catchup read incomplete")
    held = [r for r in rows if r.get("gym_id") == "lasso"
            and r.get("account") in ACCOUNTS
            and r.get("format") == "story"
            and r.get("slot_index") in (0, 1, 2)
            and r.get("status") == "pending"
            and r.get("variant_status") == "active"
            and r.get("media_not_ready_reason") == "paired_feed_not_ready"]
    # Today's Stories first, then newest backlog. The same call still attempts
    # every eligible held row across the catchup window; no fixed seven-day gap.
    held.sort(key=lambda r: (-stage.date.fromisoformat(str(r["post_date"])[:10]).toordinal(),
                             r["account"], r["slot_index"]))
    result = {"released": 0, "blocked": 0}
    for story in held:
        feed = [r for r in rows if r.get("account") == story["account"]
                and r.get("format") == "feed"
                and str(r.get("post_date") or "")[:10] == str(story["post_date"])[:10]
                and r.get("slot_index") == story["slot_index"]
                and r.get("variant_status") == "active"]
        if len(feed) != 1 or feed[0].get("status") != "published" or not feed[0].get("published_at") \
                or feed[0].get("late_post_id") is None:
            continue
        response = store._client().post(store._rest("rpc/release_lasso_paired_story_hold"),
            headers=store._headers({"Content-Type": "application/json"}),
            json={"p_story_id": story["id"]}, timeout=30)
        if response.status_code >= 400:
            result["blocked"] += 1
            continue
        receipt = response.json()
        if isinstance(receipt, dict) and receipt.get("result") == "released" \
                and receipt.get("id") == story["id"]:
            result["released"] += 1
        else:
            result["blocked"] += 1
    return result


def _candidate_artifact(store, tenant, source_id, source_hash):
    rows = stage._request_rows(store, "echo_infographic_artifacts", {
        "tenant": "eq." + tenant,
        "source_identity->>source_id": "eq." + source_id,
        "source_identity->>source_hash": "eq." + source_hash,
        "select": "image_url,image_sha256,evidence,source_identity",
        "order": "created_at.desc", "limit": "10"})
    if len(rows) > 10:
        raise RuntimeError("artifact lookup exceeded bound")
    for artifact in rows:
        evidence = artifact.get("evidence") or {}
        source = artifact.get("source_identity") or {}
        if (source == {"source_id": source_id, "source_hash": source_hash}
                and evidence.get("grade_status") == "PASS"
                and stage.measured_story_evidence(evidence, artifact.get("image_sha256"))
                and evidence.get("policy_version") == infographic_evidence.POLICY_VERSION
                and evidence.get("brain_snapshot") == infographic_evidence.brain_snapshot()
                and artifact.get("image_sha256") == evidence.get("image_sha256")
                and isinstance(artifact.get("image_url"), str)
                and artifact["image_url"].startswith("https://")):
            return artifact
    return None


def _eligible(feed, account, day):
    return (feed.get("gym_id") == "lasso"
            and str(feed.get("account") or "").strip().lower() == account
            and str(feed.get("format") or "").strip().lower() == "feed"
            and str(feed.get("post_date") or "")[:10] == day
            and feed.get("slot_index") in (0, 1, 2)
            and feed.get("variant_status") == "active"
            and feed.get("status") in ("pending", "approved", "published")
            and stage.allowed_feed_hold(feed, day)
            and str(feed.get("caption") or "").strip()
            and str(feed.get("image_url") or "").startswith("https://"))


def _verify_staged_pair(store, action, *, expected_hold=None):
    """Read the committed Story and durable feed link after the stage RPC."""
    story = stage._one(store, "content_calendar", {
        "gym_id": "eq.lasso", "id": "eq." + action["story_id"], "select": "*"})
    link = stage._one(store, "lasso_managed_paired_stories", {
        "story_id": "eq." + action["story_id"], "select": "story_id,feed_id"})
    try:
        schedule_matches = (stage.datetime.fromisoformat(
            str(story.get("scheduled_at")).replace("Z", "+00:00")) ==
            stage.datetime.fromisoformat(
                str(action["story_scheduled_at"]).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        schedule_matches = False
    feed_pillar = (action["feed_pillar"] if "feed_pillar" in action else
                   action["expected_feed"]["pillar"])
    logical_id = (action["feed_logical_post_id"] if "feed_logical_post_id" in action else
                  action["expected_feed"]["logical_post_id"])
    if (link.get("story_id") != action["story_id"]
            or link.get("feed_id") != action["feed_id"]
            or story.get("id") != action["story_id"]
            or story.get("gym_id") != "lasso"
            or str(story.get("account") or "").lower() != action["account"]
            or str(story.get("post_date") or "")[:10] != action["date"]
            or story.get("slot_index") != action["slot_index"]
            or story.get("format") != "story"
            or story.get("variant_status") != "active"
            or story.get("status") not in ("pending", "approved", "publishing", "published")
            or story.get("caption") != ""
            or story.get("pillar") != feed_pillar
            or story.get("image_url") != action["story_image_url"]
            or story.get("source_media_url") != action["story_image_url"]
            or story.get("logical_post_id") != logical_id
            or story.get("media_not_ready_reason") != expected_hold
            or not schedule_matches):
        raise RuntimeError("staged Story or source-link readback mismatch")


def run(*, now=None, account="instagram", store=None, artifact_store=None,
        generate_fn=None, host_fn=None, max_per_run=MAX_PER_RUN,
        catchup_days=0):
    summary = {"ok": False, "account": account, "eligible": 0, "generated": 0,
               "reused": 0, "staged": 0, "repaired": 0,
               "occupied": 0, "blocked": 0}
    if (account not in ACCOUNTS or type(max_per_run) is not int
            or not 1 <= max_per_run <= MAX_PER_RUN
            or type(catchup_days) is not int or not 0 <= catchup_days <= 37):
        summary["reason"] = "invalid account or cap"
        return summary
    tenant = ACCOUNTS[account]
    if not (config.calendar_autopublish_enabled()
            and config.lasso_three_feed_enabled()
            and config.lasso_infographic_quality_enabled(tenant)
            and variant_regen.enabled()
            and not visual_writer_prepare.enabled()):
        summary["reason"] = "reviewed LASSO Story cadence not armed"
        return summary
    instant = now or datetime.now(stage.ZONE)
    if isinstance(instant, str):
        instant = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    if isinstance(instant, date) and not isinstance(instant, datetime):
        instant = datetime.combine(instant, time.min, stage.ZONE)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=stage.ZONE)
    today = instant.astimezone(stage.ZONE).date()
    store = store or SupabaseCalendarStore()
    artifact_store = artifact_store or ArtifactStore()
    if not artifact_store.available:
        summary["reason"] = "artifact lease store unavailable"
        return summary
    try:
        if not _system_ready(store):
            summary["reason"] = "managed Story migrations not active"
            return summary
    except Exception:
        summary["reason"] = "managed Story migration preflight failed"
        return summary
    summary["ok"] = True
    offsets = [0, 1] + [-n for n in range(1, catchup_days + 1)]
    backlog_attempts = 0
    for offset in offsets:
        day = (today + timedelta(days=offset)).isoformat()
        if not stage.FIRST <= today + timedelta(days=offset) <= stage.LAST:
            continue
        try:
            rows = stage._active_day(store, day)
        except Exception:
            summary["blocked"] += 3
            continue
        for slot in (0, 1, 2):
            feeds = [r for r in rows if _eligible(r, account, day)
                     and r.get("slot_index") == slot]
            if len(feeds) != 1:
                summary["blocked"] += 1
                continue
            feed = feeds[0]
            occupied = [r for r in rows if str(r.get("account") or "").lower() == account
                        and str(r.get("format") or "").lower() == "story"
                        and (r.get("variant_status") or "active") == "active"
                        and (r.get("status") or "pending") not in ("denied", "killed", "failed")
                        and r.get("slot_index") in (None, slot)]
            repair_target = None
            if len(occupied) == 1 and occupied[0].get("slot_index") == slot:
                candidate = occupied[0]
                if (candidate.get("status") == "pending"
                        and candidate.get("variant_status") == "active"
                        and candidate.get("published_at") is None
                        and candidate.get("late_post_id") is None
                        and candidate.get("publish_claim_token") is None
                        and candidate.get("logical_post_id") == feed.get("logical_post_id")
                        and candidate.get("media_not_ready_reason") in
                        (None, "paired_feed_not_ready")):
                    try:
                        if store.lasso_paired_story_ready_for_feed(feed["id"]) is True:
                            summary["occupied"] += 1
                            continue
                    except Exception:
                        # Missing/failed source proof never certifies a safe
                        # existing Story; the exact repair RPC still validates.
                        pass
                    repair_target = candidate
            if occupied and repair_target is None:
                summary["occupied"] += 1
                continue
            if summary["eligible"] >= max_per_run:
                continue
            if offset < 0 and backlog_attempts >= MAX_BACKLOG_PER_RUN:
                continue
            summary["eligible"] += 1
            if offset < 0:
                backlog_attempts += 1
            source_id = f"content_calendar:{feed['id']}:caption"
            source_hash = hashlib.sha256(feed["caption"].encode("utf-8")).hexdigest()
            cache_key = "paired-story:" + hashlib.sha256(
                f"{source_id}:{source_hash}".encode("utf-8")).hexdigest()
            owner = str(uuid.uuid4())
            claimed = False
            try:
                artifact = _candidate_artifact(store, tenant, source_id, source_hash)
                if artifact is None:
                    claimed = artifact_store.claim(tenant, cache_key, owner)
                    if not claimed:
                        summary["blocked"] += 1
                        continue
                    # Recheck calendar and persisted evidence after acquiring the
                    # lease. Another daily tick may have staged or generated it.
                    fresh = stage._feed(store, str(feed["id"]))
                    fresh_day = stage._active_day(store, day)
                    fresh_occupied = [r for r in fresh_day if (
                        str(r.get("account") or "").lower() == account
                        and str(r.get("format") or "").lower() == "story"
                        and (r.get("variant_status") or "active") == "active"
                        and (r.get("status") or "pending") not in ("denied", "killed", "failed")
                        and r.get("slot_index") in (None, slot))]
                    if (fresh != feed or (repair_target is None and fresh_occupied)
                            or (repair_target is not None and
                                (len(fresh_occupied) != 1 or
                                 fresh_occupied[0] != repair_target))):
                        summary["occupied"] += 1
                        continue
                    artifact = _candidate_artifact(store, tenant, source_id, source_hash)
                    if artifact is None:
                        result = variant_regen.generate_variant_image(
                            dict(feed, format="story"), tenant,
                            generate_fn=generate_fn, host_fn=host_fn)
                        if not result.get("ok"):
                            summary["blocked"] += 1
                            continue
                        summary["generated"] += 1
                        artifact = stage._artifact(store, result["image_url"], tenant)
                else:
                    summary["reused"] += 1
                evidence = artifact.get("evidence") or {}
                recorded = artifact.get("source_identity") or {}
                if (recorded != {"source_id": source_id, "source_hash": source_hash}
                        or evidence.get("grade_status") != "PASS"
                        or not stage.measured_story_evidence(
                            evidence, artifact.get("image_sha256"))
                        or evidence.get("image_sha256") != artifact.get("image_sha256")
                        or evidence.get("policy_version") != infographic_evidence.POLICY_VERSION):
                    summary["blocked"] += 1
                    continue
                item = {"account": account, "date": day, "slot_index": slot,
                        "feed_id": str(feed["id"]),
                        "story_image_url": artifact.get("image_url"),
                        "story_sha256": artifact.get("image_sha256"),
                        "artifact_tenant": tenant,
                        "policy_version": evidence.get("policy_version")}
                if repair_target is not None:
                    repair_item = dict(item, story_id=str(repair_target["id"]))
                    action = repair.plan_one(store, repair_item)
                    receipt = repair.apply_one(store, action)
                    if receipt.get("result") == "repaired":
                        _verify_staged_pair(store, action,
                            expected_hold=receipt.get("hold_reason"))
                        summary["repaired"] += 1
                    else:
                        summary["blocked"] += 1
                else:
                    action = stage.plan_one(store, item)
                    if action["state"] != "ready":
                        summary["occupied"] += 1
                        continue
                    receipt = stage.apply_one(store, action)
                    if receipt.get("result") in ("inserted", "idempotent"):
                        _verify_staged_pair(store, action)
                        summary["staged"] += 1
                    else:
                        summary["blocked"] += 1
            except Exception:
                summary["blocked"] += 1
            finally:
                if claimed:
                    try:
                        artifact_store.release(tenant, cache_key, owner)
                    except Exception:
                        summary["blocked"] += 1
    return summary
