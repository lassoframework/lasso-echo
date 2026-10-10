"""Draft one-ticket repair for ENG's missed September 4 denied post.

There is intentionally no scheduled job or CLI entry point. The one-ticket SQL
admission is OFF, and this coordinator requires the separate forward-stage,
trusted visual attestation, and reservation stack. Stage creates INACTIVE
candidates only; a separately reviewed finalize call makes pending approval
cards. Neither step publishes or changes the historical denied rows/ticket.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import config, forward_media_observation_bridge as observation_bridge
from . import media_guard, portal_calendar_store as pcs

TICKET_ID = "35e066d0-d9bc-40e6-aef8-86719a010590"
TENANT = "eng"
LOGICAL_POST_ID = "6d21c00f-349c-5a06-9328-5a570a81babb"
ORIGINAL_IDS = (
    "2ad9f097-e7cc-49a2-b348-30c8e1cda80d",  # Facebook feed
    "b1bb4d63-fda7-4b1e-9303-dff3483f4387",  # Instagram feed
    "ff792b3e-be08-4c4e-b0f5-0347e075a9ba",  # Instagram story
)
ORIGINAL_ASSET = "1BNl4pJqTFF21JYk-RIDS9-YHz9YYXLKt"
GROUP = {("facebook", "feed"), ("instagram", "feed"), ("instagram", "story")}


class EngRecreationRefused(RuntimeError):
    """A safe candidate or exact admission could not be established."""


def _date(value):
    try:
        if isinstance(value, datetime):
            raise ValueError("date, not timestamp, required")
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise EngRecreationRefused("valid future target date required") from exc


def _required_stack():
    if (pcs.forward_reservation_flag() is not True
            or not config.logical_post_id_enabled()
            or not media_guard.enabled()
            or not observation_bridge.enabled()):
        raise EngRecreationRefused("forward reservation and visual proof stack must be armed")


def _future_target(value):
    target = _date(value)
    today = date.today()
    if not today < target <= today + timedelta(days=31):
        raise EngRecreationRefused("target must be a future date inside the planning horizon")
    return target


def _checked_originals(store):
    rows = [store.get_row(TENANT, row_id) for row_id in ORIGINAL_IDS]
    if (any(not isinstance(row, dict) for row in rows)
            or {row.get("id") for row in rows} != set(ORIGINAL_IDS)
            or {(row.get("account"), row.get("format")) for row in rows} != GROUP
            or any(row.get("gym_id") != TENANT or row.get("post_date") != "2026-09-04"
                   or row.get("status") != "denied" or row.get("variant_status") != "active"
                   or row.get("source_media_asset_id") != ORIGINAL_ASSET
                   or any(row.get(k) is not None for k in
                          ("published_at", "late_post_id", "publish_claim_token"))
                   for row in rows)):
        raise EngRecreationRefused("exact original denied rows changed")
    return rows


def _checked_candidates(rows, target):
    if (not isinstance(rows, list) or len(rows) != 3
            or any(not isinstance(r, dict) for r in rows)
            or {(r.get("account"), r.get("format")) for r in rows} != GROUP):
        raise EngRecreationRefused("one Instagram feed, Facebook feed and Story required")
    source_proofs = set()
    source_urls = set()
    feed_captions = set()
    for row in rows:
        if (row.get("gym_id") != TENANT or row.get("post_date") != target.isoformat()
                or row.get("logical_post_id") != LOGICAL_POST_ID
                or row.get("status") != "pending"
                or row.get("media_not_ready_reason") is not None
                or row.get("source_media_asset_id") in (None, ORIGINAL_ASSET)
                or any(row.get(k) is not None for k in
                       ("approved_at", "approved_by", "published_at", "late_post_id",
                        "publish_claim_token"))
                or not all(urlsplit(str(row.get(k) or "")).scheme == "https"
                           for k in ("image_url", "source_media_url"))):
            raise EngRecreationRefused("candidate is not an unapproved future ENG replacement")
        proof = row.get(pcs.RESERVATION_PROOF)
        if (not pcs.valid_reservation_proof(proof)
                or proof["source_media_asset_id"] != row["source_media_asset_id"]):
            raise EngRecreationRefused("trusted forward source screening proof missing")
        source_proofs.add(proof["source_sha256"])
        source_urls.add(row["source_media_url"])
        if row["format"] == "feed":
            feed_captions.add(row.get("caption"))
        if observation_bridge.METADATA not in row:
            raise EngRecreationRefused("byte observation packet missing")
    if len(source_proofs) != 1 or len(source_urls) != 1 or len(feed_captions) != 1:
        raise EngRecreationRefused("replacement siblings do not share source and feed copy")
    return rows


def build_candidates(account, voice, library_path: Path | str, store, target_date,
                     *, banned_words=(), logger=None):
    """Use Echo's existing A+ draft/story/mirror path; write nothing to calendar.

    This can call the normal caption and media builders. A failure leaves the
    ticket reservation intact for explicit reconciliation. The caller must not
    silently repick after an uncertain stage attempt.
    """
    _required_stack()
    target = _future_target(target_date)
    if getattr(account, "key", None) != TENANT or voice is None:
        raise EngRecreationRefused("exact ENG account and approved voice required")
    originals = _checked_originals(store)
    from . import client_month_run as month
    log = logger or (lambda _message: None)
    excluded = {Path(urlsplit(row.get("image_url") or "").path).name
                for row in originals}
    feed, reason = month._clean_draft_for_day(
        account, target.isoformat(), voice, library_path, banned_words, log,
        exclude_keys=excluded, allow_reuse=False, prefer_photos=True)
    if feed is None or not month._has_real_creative(feed):
        raise EngRecreationRefused(f"safe new creative unavailable: {reason or 'no media'}")
    feed.logical_post_id = LOGICAL_POST_ID
    drafts = month._finish_feed_with_story(
        account, feed, library_path, log, day_key=target.isoformat())
    if not isinstance(drafts, list) or len(drafts) != 2:
        raise EngRecreationRefused("feed and paired Story could not be prepared")
    rows = month._to_rows(TENANT, drafts)
    _checked_candidates(rows, target)
    try:
        original = media_guard.swap_original_identity(TENANT, originals[1], store)
    except Exception as exc:
        raise EngRecreationRefused("denied original byte identity unavailable") from exc
    if rows[0][pcs.RESERVATION_PROOF]["source_sha256"] == original["sha256"]:
        raise EngRecreationRefused("candidate repeats the denied original bytes")
    return drafts, rows


class EngSept5Recreator:
    """Explicit maintenance caller; each state change is a separate operator step."""

    def __init__(self, store):
        self.store = store

    def begin(self, target_date):
        _required_stack()
        target = _future_target(target_date)
        _checked_originals(self.store)
        result = self.store._reservation_rpc(
            "eng_sept5_recreation_begin", {"p_target_date": target.isoformat()})
        if not isinstance(result, dict) or result.get("ticket_id") != TICKET_ID \
                or result.get("tenant_id") != TENANT or result.get("state") != "reserved" \
                or result.get("target_date") != target.isoformat():
            raise EngRecreationRefused("one-ticket reservation response uncertain")
        return result

    def stage(self, account, target_date, drafts, rows):
        """Stage only; an unknown response is never retried or treated as active."""
        _required_stack()
        target = _future_target(target_date)
        if getattr(account, "key", None) != TENANT:
            raise EngRecreationRefused("exact ENG account required")
        self.begin(target)
        originals = _checked_originals(self.store)
        _checked_candidates(rows, target)
        try:
            original = media_guard.swap_original_identity(TENANT, originals[1], self.store)
        except Exception as exc:
            raise EngRecreationRefused("denied original byte identity unavailable") from exc
        if rows[0][pcs.RESERVATION_PROOF]["source_sha256"] == original["sha256"]:
            raise EngRecreationRefused("candidate repeats the denied original bytes")
        from . import client_month_run as month
        from . import post_quality
        if not isinstance(drafts, list) or len(drafts) != 2:
            raise EngRecreationRefused("exact prepared feed and Story required")
        if not post_quality.is_a_plus(drafts[0], require_media=True):
            raise EngRecreationRefused("replacement feed does not pass the quality gate")
        if month._to_rows(TENANT, drafts) != rows:
            raise EngRecreationRefused("candidate rows differ from the prepared drafts")
        if not month._record_feed_served(account, drafts[0], target.isoformat()):
            raise EngRecreationRefused("source media reservation unavailable")
        try:
            staged = month._insert_rows_with_poster_evidence(
                self.store.insert_rows, TENANT, rows,
                month._poster_render_evidence_by_url(drafts), expected_old_rows=[])
        except Exception:
            # A request may already have staged. Retain the served reservation
            # and use the store's exact last_forward_stage_attempt to reconcile.
            raise
        attempt = getattr(self.store, "last_forward_stage_attempt", None)
        if (not isinstance(attempt, dict) or attempt.get("tenant_id") != TENANT
                or attempt.get("old_row_ids") != [] or not isinstance(staged, list)
                or len(staged) != 3 or any(r.get("variant_status") != "candidate"
                                           for r in staged)):
            raise EngRecreationRefused("stage receipt uncertain; reconcile exact batch status")
        return {"batch_id": attempt["batch_id"], "staged": staged}

    def bind(self, batch_id):
        _required_stack()
        result = self.store._reservation_rpc(
            "eng_sept5_recreation_bind", {"p_batch_id": str(batch_id)})
        if not isinstance(result, dict) or result.get("ticket_id") != TICKET_ID \
                or result.get("batch_id") != str(batch_id) or result.get("state") != "staged":
            raise EngRecreationRefused("ticket-to-batch binding uncertain")
        return result

    def finalize(self, batch_id, attested_candidates):
        """Separate admission after trusted owner/attester proof, never on stage."""
        _required_stack()
        if (not isinstance(attested_candidates, list)
                or len(attested_candidates) != 3):
            raise EngRecreationRefused("three attested candidates required")
        result = self.store._reservation_rpc("eng_sept5_recreation_finalize", {
            "p_batch_id": str(batch_id), "p_candidates": attested_candidates})
        if (not isinstance(result, dict) or result.get("ticket_id") != TICKET_ID
                or result.get("batch_id") != str(batch_id)
                or result.get("state") != "finalized"
                or not isinstance(result.get("finalize_receipt"), dict)):
            raise EngRecreationRefused("finalize outcome uncertain; read exact receipt")
        return result
