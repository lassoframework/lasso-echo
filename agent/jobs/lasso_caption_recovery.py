"""Bounded approved-source caption repair for occupied owned LASSO feeds.

One pending unclaimed feed/account/pass, today+tomorrow. Existing strict history,
Story-before-feed holds, exact CAS and explicit partial-state guards do the work.
No renderer, provider, wholesale sweep or source-file write occurs here.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import os
import uuid

from agent import caption_ledger, config, copy_gate, real_month_planner, real_month_run, variant_regen
from agent.jobs import grade_fix, lasso_feed_runway as runway
from agent.jobs import lasso_paired_story_backfill as pairs

FIELDS = set(runway.repair._CAS_COLUMNS) | {"pillar"}
NULL_RECEIPTS = ("published_at", "late_post_id", "publish_claim_token", "publish_reservation_day")
FEED_HOLDS = (None, "caption_changed_needs_new_visual", "cross_date_media_repeat_needs_new_visual")
STORY_HOLDS = FEED_HOLDS + ("paired_feed_not_ready",)


class _Refused(ValueError):
    pass


def enabled():
    return os.environ.get("AGENT_LASSO_CAPTION_RECOVERY", "false").strip().lower() in {"true", "1", "yes", "on"}


def _gates(store, account_key):
    return (enabled() and config.lasso_three_feed_enabled() and config.caption_cooldown_enabled()
            and config.real_month_plan_enabled() and config.content_brain_enabled()
            and config.lasso_infographic_quality_enabled(account_key) and variant_regen.enabled()
            and config.POSTING_TIMEZONE == "America/New_York"
            and store.gym_autonomy("lasso") is True)


def _eligible(row, platform, first, last, fmt="feed"):
    if (not isinstance(row, dict) or not FIELDS.issubset(row)
            or row["gym_id"] != "lasso" or row["account"] != platform
            or row["format"] != fmt or row["status"] != "pending"
            or row["variant_status"] != "active" or type(row["slot_index"]) is not int
            or row["slot_index"] not in (0, 1, 2)
            or not first <= str(row["post_date"] or "")[:10] <= last
            or any(row[k] is not None for k in NULL_RECEIPTS)
            or row["media_not_ready_reason"] not in (FEED_HOLDS if fmt == "feed" else STORY_HOLDS)
            or not isinstance(row["image_url"], str) or not row["image_url"].startswith("https://")
            or not row["pillar"]):
        return False
    try:
        date.fromisoformat(str(row["post_date"]))
        uuid.UUID(str(row["id"]))
        # Known legacy NULL identities stay NULL. Exact account/day/slot plus
        # the registered Story feed UUID provide the linkage when present.
        if row["logical_post_id"] is not None:
            uuid.UUID(str(row["logical_post_id"]))
        created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        if created.tzinfo is None:
            return False
        if row["scheduled_at"] is not None:
            scheduled = datetime.fromisoformat(str(row["scheduled_at"]).replace("Z", "+00:00"))
            if scheduled.tzinfo is None:
                return False
    except (TypeError, ValueError, AttributeError):
        return False
    return bool(str(row["caption"] or "").strip()) if fmt == "feed" else row["caption"] == ""


def _siblings(store, target, day_rows):
    day, account, slot = target["post_date"], target["account"], target["slot_index"]
    if not isinstance(day_rows, list) or any(not isinstance(r, dict) or r.get("gym_id") != "lasso"
        or r.get("post_date") != day or r.get("variant_status") != "active" for r in day_rows):
        raise _Refused("incomplete active day context")
    same = [r for r in day_rows if r.get("account") == account
            and r.get("status") not in ("denied", "killed")
            and (r.get("slot_index") == slot or r.get("slot_index") is None)]
    feeds = [r for r in same if r.get("format") == "feed"]
    if len(feeds) != 1 or feeds[0] != target:
        raise _Refused("ambiguous or changed source feed")
    stories = [r for r in same if r.get("format") == "story"]
    if len(stories) > 1 or any(r.get("format") not in ("feed", "story") for r in same):
        raise _Refused("ambiguous slot occupants")
    if not stories:
        return []
    story = stories[0]
    if (not _eligible(story, account, day, day, "story")
            or story["slot_index"] != slot or story["logical_post_id"] != target["logical_post_id"]):
        raise _Refused("protected or orphan Story source")
    link = pairs._one(store, "lasso_managed_paired_stories", {
        "story_id": "eq." + story["id"], "select": "story_id,feed_id"})
    if link.get("story_id") != story["id"] or link.get("feed_id") != target["id"]:
        raise _Refused("unknown Story registry source")
    return stories


def _replacement_grade(existing, target, caption):
    from agent.calendar_grade import grade_month, A_THRESHOLD
    if sum(r.get("id") == target["id"] for r in existing) != 1:
        return False
    view = [dict(r, caption=caption) if r.get("id") == target["id"] else dict(r)
            for r in existing if r.get("status") not in ("denied", "killed")
            and r.get("variant_status") == "active"]
    return grade_month(view, profile="B2B").total >= A_THRESHOLD


def _candidate(target, existing, account_key, regen, *, store=None, author_summary=None):
    avoid = [str(r.get("caption") or "") for r in existing]
    def valid(result):
        if result is None:
            return None
        caption, pillar = result
        if (pillar != target["pillar"] or not isinstance(caption, str)
                or copy_gate.lasso_violations(caption)
                or not grade_fix._clears_craft(caption, allow_no_ask=True)
                or caption_ledger.caption_hash(caption) in {caption_ledger.caption_hash(c) for c in avoid if c}
                or caption_ledger.is_blocked_strict("lasso", caption, target["post_date"])):
            return None
        return caption
    result = valid(regen(target, avoid) if regen else None)
    if result is not None:
        return result
    drafts = real_month_run.plan_and_build(account_key, target["post_date"], 1,
        source_only=True, slot_selector=lambda s: s.fmt == "feed"
        and s.post_date == target["post_date"] and s.cadence_slot == target["slot_index"])
    rows = [r for r in real_month_planner.to_calendar_rows(drafts, "lasso")
            if r.get("account") == target["account"] and r.get("format") == "feed"
            and r.get("post_date") == target["post_date"] and r.get("slot_index") == target["slot_index"]]
    if (len(drafts) == 1 and len(rows) == 1 and getattr(drafts[0], "source_fragments", None)
            and rows[0].get("pillar") == target["pillar"]):
        fragments = getattr(drafts[0], "source_fragments", [])
        if any(str(f).strip() and str(f) in target["caption"] for f in fragments):
            result = valid((rows[0]["caption"], rows[0]["pillar"]))
            if result is not None:
                return result
    # Author only after both finite approved-source routes are exhausted.
    # Source review does not replace the later full book grade or exact repair.
    from agent import lasso_caption_authoring
    authored = lasso_caption_authoring.author(target, avoid,
        gate_fn=(lambda: _gates(store, account_key)) if store is not None else None)
    if author_summary is not None:
        author_summary.update({k: authored[k] for k in
            ("ok", "writer_calls", "reviewer_calls", "reused", "receipt_key", "receipt_sha256", "reason")
            if k in authored})
    return valid((authored["caption"], target["pillar"])) if authored.get("ok") is True else None


class _ExactContextStore:
    """Revalidate the patch helper's own read and every write, without bypasses."""
    def __init__(self, store, target, day_rows, source_snapshot, account_key, book, caption, author_receipt=None):
        self.store, self.target, self.day_rows = store, target, day_rows
        self.source_snapshot, self.account_key = source_snapshot, account_key
        self.book, self.caption = book, caption
        self.context_validated = False
        self.author_receipt = author_receipt

    def __getattr__(self, name):
        return getattr(self.store, name)

    def _fresh(self):
        if not _gates(self.store, self.account_key) or runway._source_snapshot() != self.source_snapshot:
            raise _Refused("fresh caption source gate closed")
        if self.author_receipt:
            from agent import lasso_caption_authoring
            if not lasso_caption_authoring.current_receipt_valid(self.target,
                    [r.get("caption") or "" for r in self.book], self.author_receipt,
                    expected_caption=self.caption, gate_fn=lambda: _gates(self.store, self.account_key)):
                raise _Refused("fresh authored source receipt unavailable")
        if not _replacement_grade(self.book, self.target, self.caption):
            raise _Refused("fresh replacement book below A")

    def active_rows_on_day_complete(self, gym, day):
        self._fresh()
        rows = self.store.active_rows_on_day_complete(gym, day)
        if rows != self.day_rows:
            raise _Refused("caption sibling context changed")
        _siblings(self.store, self.target, rows)
        self.context_validated = True
        return rows

    def patch_pending_plan(self, *args, **kwargs):
        if not self.context_validated:
            raise _Refused("guarded caption context not validated")
        self._fresh()
        return self.store.patch_pending_plan(*args, **kwargs)


def run(*, account_key, now=None, store=None):
    from agent.jobs.lasso_cadence_recovery import _local_now
    from agent import build_lock
    from agent.portal_calendar_store import SupabaseCalendarStore
    out = {"ok": False, "attempted": 0, "repaired": 0, "skipped": 0, "blocked": 0, "partial": 0}
    if not enabled():
        return dict(out, ok=True, reason="caption lane disabled")
    if account_key not in runway.ACCOUNTS:
        return dict(out, reason="unknown account")
    store = store or SupabaseCalendarStore()
    try:
        if not _gates(store, account_key):
            return dict(out, reason="caption source or autonomy gates closed")
    except Exception:
        return dict(out, reason="caption gates unreadable")
    day = _local_now(now).date()
    first, last = day.isoformat(), (day + timedelta(days=1)).isoformat()
    grade_last = (day + timedelta(days=29)).isoformat()
    holder = "lasso-caption:" + str(uuid.uuid4())
    if not build_lock.acquire("lasso", holder=holder):
        return dict(out, skipped=1, reason="LASSO build lock occupied")
    heartbeat = build_lock.start_heartbeat("lasso", holder=holder)
    patch_applied = False
    target = None
    try:
        existing = store.rows_in_range_complete("lasso", first, grade_last, all_statuses=True)
        runway._coverage(existing, first, grade_last)
        targets = sorted((r for r in existing if _eligible(r, runway.ACCOUNTS[account_key], first, last)),
                         key=lambda r: (r["post_date"], r["slot_index"], r["id"]))
        target = None
        for row in targets:
            if caption_ledger.is_blocked_strict("lasso", row["caption"], row["post_date"]):
                target = row
                break
        if target is None:
            return dict(out, ok=True, reason="no blocked pending captions")
        out["attempted"] = 1
        out["target_id"] = target["id"]
        source = runway._source_snapshot()
        context = store.active_rows_on_day_complete("lasso", target["post_date"])
        _siblings(store, target, context)
        author_summary = {}
        caption = _candidate(target, existing, account_key, grade_fix._lasso_caption_regen(lambda m: None),
                             store=store, author_summary=author_summary)
        if author_summary:
            out["authoring"] = author_summary
        if caption is None:
            return dict(out, blocked=1, reason="approved same-topic source exhausted")
        fresh = store.rows_in_range_complete("lasso", first, grade_last, all_statuses=True)
        if fresh != existing or runway._source_snapshot() != source:
            return dict(out, blocked=1, reason="caption source or protected book changed")
        if not _gates(store, account_key) or caption_ledger.is_blocked_strict("lasso", caption, target["post_date"]):
            return dict(out, blocked=1, reason="fresh caption gate closed")
        if not _replacement_grade(fresh, target, caption):
            return dict(out, blocked=1, reason="replacement book below A")
        proxy = _ExactContextStore(store, target, context, source, account_key, fresh, caption,
                                  author_receipt=author_summary.get("receipt_key") if author_summary.get("ok") else None)
        repair_target = deepcopy(target)
        if not grade_fix._patch_date_rows("lasso", [repair_target], proxy, caption, target["pillar"], lambda m: None):
            return dict(out, blocked=1, reason="exact caption repair refused")
        patch_applied = True
        saved = store.get_row("lasso", target["id"])
        if (not isinstance(saved, dict) or saved.get("caption") != caption
                or saved.get("media_not_ready_reason") != "caption_changed_needs_new_visual"
                or any(saved.get(k) != target[k] for k in FIELDS - {"caption", "media_not_ready_reason"})):
            return dict(out, partial=1, blocked=1, reason="caption repair readback mismatch")
        return dict(out, ok=True, repaired=1, reason="exact caption repaired; visual held")
    except grade_fix.PartialLassoCaptionRepair as exc:
        # Re-read the exact partial state; do not undo history or attempt another
        # caption. Existing visual/managed Story repair can resume valid holds.
        try:
            states = store.active_rows_on_day_complete("lasso", target["post_date"])
            readback = [{k: r.get(k) for k in ("id", "format", "status", "media_not_ready_reason")}
                        for r in states if r.get("id") in {target["id"], *exc.applied_ids}]
        except Exception:
            readback = "unavailable"
        return dict(out, partial=1, blocked=1, applied_ids=list(exc.applied_ids),
                    partial_readback=readback, reason="partial caption repair retained")
    except Exception as exc:
        return dict(out, blocked=1, partial=int(patch_applied), reason=str(exc) if isinstance(exc, _Refused)
                    else "caption recovery refused: " + type(exc).__name__)
    finally:
        heartbeat.stop()
        build_lock.release("lasso", holder=holder)
