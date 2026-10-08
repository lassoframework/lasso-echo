"""One missing owned-LASSO feed per account/pass, before normal Story prep.

No provider calls. Normal planner source/ledger selection remains authoritative;
exact-caption reviewed media and the DB empty-slot insert arbitrate staging.
"""
import hashlib
import os
import uuid
from datetime import timedelta
from pathlib import Path

from agent import config, copy_gate, infographic_evidence, real_month_planner, real_month_run
from agent.jobs import lasso_held_media_repair as repair

ACCOUNTS = {"lasso_ig": "instagram", "lasso_fb": "facebook"}
HORIZON_DAYS = 7
NAMESPACE = uuid.UUID("ca09b07c-ff23-4157-82cb-246dc4ec73d4")


def enabled():
    return os.environ.get("AGENT_LASSO_FEED_RUNWAY", "false").strip().lower() in {
        "true", "1", "yes", "on"}


def _gates(account_key, store, artifacts):
    return (enabled() and config.real_month_plan_enabled()
            and config.lasso_three_feed_enabled()
            and config.lasso_infographic_quality_enabled(account_key)
            and config.POSTING_TIMEZONE == "America/New_York"
            and config.cadence_slot_times() == ("07:30", "18:30")
            and store.gym_autonomy("lasso") is True and artifacts.available)


def _source_snapshot():
    # Refuse insertion if the approved local sources changed while rendering.
    root = Path(__file__).resolve().parents[2] / "brand_voice"
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.glob("lasso*")) if p.is_file()}


def _coverage(rows, first, last):
    occupied = set()
    explicit = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("gym_id") != "lasso":
            raise ValueError("calendar read out of scope")
        day = str(row.get("post_date") or "")[:10]
        if not first <= day <= last:
            raise ValueError("calendar read out of date bounds")
        if row.get("status") in ("denied", "killed") or row.get("variant_status") == "inactive":
            continue
        if row.get("account") not in ACCOUNTS.values():
            raise ValueError("ambiguous account")
        if row.get("variant_status") != "active" or row.get("format") not in ("feed", "story"):
            raise ValueError("ambiguous active row")
        logical = row.get("logical_post_id")
        if logical not in (None, ""):
            try:
                uuid.UUID(str(logical))
            except (TypeError, ValueError, AttributeError):
                raise ValueError("malformed logical identity") from None
        slot = row.get("slot_index")
        if "slot_index" in row and slot is None and row["format"] == "story":
            if "logical_post_id" not in row or logical == "":
                raise ValueError("ambiguous logical identity")
            # A known active owned Story with an explicit NULL slot occupies all
            # three Story slots only on its exact account/date (wildcard). Legacy
            # rows may also carry an explicit NULL logical identity; occupancy
            # only, never pair inference. A missing slot_index key still refuses.
            for s in (0, 1, 2):
                occupied.add((row["account"], day, "story", s))
            continue
        if type(slot) is not int or slot not in (0, 1, 2):
            raise ValueError("ambiguous slot")
        key = (row["account"], day, row["format"], slot)
        if key in explicit:
            raise ValueError("duplicate active slot")
        explicit.add(key)
        occupied.add(key)
    return occupied


def _grade(existing, candidate):
    if not config.calendar_grade_enabled_for("lasso"):
        return True
    from agent.calendar_grade import grade_month, A_THRESHOLD
    # Existing rows are immutable. Never remediate a protected book or grade a
    # singleton as if it represented the whole month.
    rows = [r for r in existing if r.get("status") not in ("denied", "killed")
            and r.get("variant_status") == "active"] + [candidate]
    return grade_month(rows, profile="B2B").total >= A_THRESHOLD


def run(*, account_key, now=None, store=None, artifact_store=None,
        horizon_days=HORIZON_DAYS, planner=None, render=None):
    from agent.jobs.lasso_cadence_recovery import _local_now
    from agent import build_lock, variant_regen
    from agent.portal_calendar_store import SupabaseCalendarStore
    from agent.infographic_artifacts import ArtifactStore
    out = {"ok": False, "attempted": 0, "generated": 0, "reused": 0,
           "inserted": 0, "occupied": 0, "blocked": 0, "missing": []}
    if not enabled():
        return dict(out, ok=True, reason="runway lane disabled")
    if (account_key not in ACCOUNTS or type(horizon_days) is not int
            or not 2 <= horizon_days <= 30):
        return dict(out, reason="invalid account or horizon")
    if not (config.real_month_plan_enabled() and config.lasso_three_feed_enabled()
            and config.lasso_infographic_quality_enabled(account_key) and variant_regen.enabled()):
        return dict(out, reason="runway source or review gates off")
    store = store or SupabaseCalendarStore()
    artifacts = artifact_store or ArtifactStore()
    try:
        if store.gym_autonomy("lasso") is not True or not artifacts.available:
            return dict(out, reason="autonomy or artifact store unavailable")
    except Exception:
        return dict(out, reason="autonomy or artifact store unreadable")
    if config.POSTING_TIMEZONE != "America/New_York" or config.cadence_slot_times() != ("07:30", "18:30"):
        return dict(out, reason="unsupported canonical schedule override")
    instant = _local_now(now)
    first = instant.date().isoformat()
    last = (instant.date() + timedelta(days=horizon_days - 1)).isoformat()
    grade_last = (instant.date() + timedelta(days=29)).isoformat()
    holder = "lasso-runway:" + str(uuid.uuid4())
    if not build_lock.acquire("lasso", holder=holder):
        return dict(out, reason="LASSO build lock occupied")
    heartbeat = build_lock.start_heartbeat("lasso", holder=holder)
    claimed = False
    cache_key = None
    try:
        existing = store.rows_in_range_complete("lasso", first, grade_last, all_statuses=True)
        if not isinstance(existing, list):
            raise ValueError("incomplete calendar read")
        occupied = _coverage(existing, first, grade_last)
        candidates = []
        for offset in range(horizon_days):
            day = (instant.date() + timedelta(days=offset)).isoformat()
            for slot in range(3):
                if (ACCOUNTS[account_key], day, "feed", slot) in occupied:
                    out["occupied"] += 1
                    continue
                out["missing"].append([ACCOUNTS[account_key], day, slot])
                if (ACCOUNTS[account_key], day, "story", slot) in occupied:
                    out["blocked"] += 1  # never invent a feed under an orphan Story
                else:
                    candidates.append((day, slot))
        if not candidates:
            return dict(out, ok=not out["blocked"], reason="orphan Story holds" if out["blocked"] else "runway complete")
        day, slot = candidates[0]
        out["target"] = [ACCOUNTS[account_key], day, slot]
        out["attempted"] = 1
        source_before = _source_snapshot()
        drafts = (planner or real_month_run.plan_and_build)(
            account_key, day, 1, source_only=True,
            slot_selector=lambda s: s.fmt == "feed" and s.post_date == day
            and s.cadence_slot == slot)
        rows = [r for r in real_month_planner.to_calendar_rows(drafts, "lasso")
                if r.get("account") == ACCOUNTS[account_key] and r.get("format") == "feed"
                and r.get("post_date") == day and r.get("slot_index") == slot]
        if len(rows) != 1 or len(drafts) != 1 or not getattr(drafts[0], "source_fragments", None):
            return dict(out, blocked=out["blocked"] + 1, reason="approved source unavailable")
        row = rows[0]
        caption = str(row.get("caption") or "")
        if not caption.strip() or copy_gate.lasso_violations(caption):
            return dict(out, blocked=out["blocked"] + 1, reason="source caption gate")
        from agent import caption_ledger
        if caption_ledger.is_blocked_strict("lasso", caption, day):
            return dict(out, blocked=out["blocked"] + 1, reason="source caption cooldown")
        if not _grade(existing, row):
            return dict(out, blocked=out["blocked"] + 1, reason="existing plus candidate calendar grade")
        from agent.calendar_autopublish import scheduled_iso_for_row
        row["scheduled_at"] = scheduled_iso_for_row(row, tz_name="America/New_York")
        allowed = {"gym_id", "account", "post_date", "slot_index", "format", "caption",
                   "image_url", "status", "logical_post_id", "scheduled_at", "pillar"}
        row = {k: v for k, v in row.items() if k in allowed}
        if not _gates(account_key, store, artifacts) or not variant_regen.enabled():
            return dict(out, blocked=out["blocked"] + 1, reason="fresh source gate closed")
        caption_ledger.record_staged_strict("lasso", caption, day)
        row["id"] = str(uuid.uuid5(NAMESPACE, f"{account_key}|{day}|{slot}"))
        row["logical_post_id"] = (
            str(uuid.uuid5(NAMESPACE, f"logical|{account_key}|{day}|{slot}"))
            if config.logical_post_id_enabled() else None)
        row["variant_status"] = "active"
        row["status"] = "pending"
        source_id = f"content_calendar:{row['id']}:caption"
        source_hash = hashlib.sha256(caption.encode()).hexdigest()
        cache_key = "held-feed:" + hashlib.sha256(f"{source_id}:{source_hash}".encode()).hexdigest()
        claimed = artifacts.claim(account_key, cache_key, holder)
        if not claimed:
            return dict(out, blocked=out["blocked"] + 1, reason="source artifact lease or cooldown")
        reviewed = repair._reviewed_artifact_record(store, source_id, source_hash, account_key)
        if reviewed:
            out["reused"] = 1
        else:
            if not _gates(account_key, store, artifacts) or not variant_regen.enabled():
                return dict(out, blocked=out["blocked"] + 1, reason="fresh render gate closed")
            result = (render or variant_regen.generate_variant_image)(row, account_key)
            if not result.get("ok"):
                return dict(out, blocked=out["blocked"] + 1, reason="reviewed render: " + str(result.get("reason")))
            out["generated"] = 1
            reviewed = repair._reviewed_artifact_record(store, source_id, source_hash, account_key)
        if not reviewed:
            return dict(out, blocked=out["blocked"] + 1, reason="exact-caption PASS artifact missing")
        if _source_snapshot() != source_before:
            return dict(out, blocked=out["blocked"] + 1, reason="approved source changed during render")
        fresh = store.rows_in_range_complete("lasso", first, grade_last, all_statuses=True)
        if fresh != existing:
            return dict(out, blocked=out["blocked"] + 1, reason="calendar changed during render")
        row["image_url"] = reviewed["image_url"]
        row["media_not_ready_reason"] = None
        # Narrow RPC validates exact source, actual canonical future schedule,
        # current artifact policy/Brain PASS and an empty target feed+Story slot.
        if not _gates(account_key, store, artifacts) or not variant_regen.enabled():
            return dict(out, blocked=out["blocked"] + 1, reason="fresh insert gate closed")
        if not _grade(fresh, row):
            return dict(out, blocked=out["blocked"] + 1, reason="fresh existing plus candidate calendar grade")
        result = store.stage_lasso_runway_feed(row, policy_version=infographic_evidence.POLICY_VERSION,
                                               brain_snapshot=infographic_evidence.brain_snapshot())
        if result.get("result") not in ("inserted", "idempotent"):
            return dict(out, blocked=out["blocked"] + 1, reason="stage: " + str(result.get("reason", result.get("result"))))
        saved = store.get_row("lasso", row["id"])
        if not isinstance(saved, dict) or any(saved.get(k) != row[k] for k in
                ("id", "gym_id", "account", "post_date", "slot_index", "format", "caption",
                 "image_url", "status", "variant_status", "logical_post_id", "scheduled_at")):
            return dict(out, blocked=out["blocked"] + 1, reason="insert readback mismatch")
        after = store.rows_in_range_complete("lasso", first, grade_last, all_statuses=True)
        if [r for r in after if str(r.get("id")) != row["id"]] != existing:
            return dict(out, blocked=out["blocked"] + 1, reason="concurrent non-target calendar change after insert")
        return dict(out, ok=True, inserted=int(result["result"] == "inserted"), reason="reviewed feed staged")
    except Exception as exc:
        return dict(out, blocked=out["blocked"] + 1, reason=f"runway failed: {type(exc).__name__}: {exc}")
    finally:
        if claimed:
            artifacts.release(account_key, cache_key, holder)
        heartbeat.stop()
        build_lock.release("lasso", holder=holder)
