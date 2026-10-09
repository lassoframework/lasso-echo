"""Cross-date media repeat HOLD lane (AGENT_MEDIA_REPEAT_SWEEP_HOLD, default OFF).

The ordinary nightly sweep never swaps an APPROVED duplicate and gives up when
no fresh media exists. This lane (agent/jobs/media_repeat_sweep.py
_hold_cross_date_repeats + SupabaseCalendarStore.hold_repeat_media) marks a
later-date EXACT duplicate with only
media_not_ready_reason='cross_date_media_repeat_needs_new_visual', preserving
status, caption, image, approval and variant.

Identity is exact and PROVABLE: ONLY the canonical full URL of the current
DELIVERED image_url groups rows -- never a basename, never source_media_url,
never source_media_asset_id (2026-10-04 safety repair: the row schema has no
immutable source-to-delivered receipt, so source/asset matching could false-
hold fresh media from stale Story/feed metadata). Source-null or transformed
same-photo repeats delivered under DIFFERENT derivative URLs are out of scope
(PR268 release gap -- not a global guarantee). Perceptual near-dupe detection
(PR268 pHash/atomic DB guard) is a separate, still-OFF lane.
"""

import copy
import os
import sys
import types
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent.jobs import media_repeat_sweep as mrs  # noqa: E402
from agent.portal_calendar_store import (PortalStoreError,  # noqa: E402
                                       SupabaseCalendarStore)

GYM = "gritx"
TODAY = date(2026, 10, 4)
URL_A = "https://cdn.test/shared-photo.jpg"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", raising=False)
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_WINDOW_DAYS", "30")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(mrs, "_lib_dir", lambda base: str(lib))
    yield


def _row(rid, pd, account="instagram", status="pending", fmt="feed", **over):
    row = {
        "id": rid, "gym_id": GYM, "post_date": pd, "status": status,
        "variant_status": "active", "account": account, "format": fmt,
        "caption": f"Caption for {rid} keep as written.",
        "image_url": URL_A, "source_media_url": None,
        "source_media_asset_id": None, "media_not_ready_reason": None,
        "created_at": "2026-09-30T12:00:00Z", "published_at": None,
        "late_post_id": None, "publish_claim_token": None,
        "publish_reservation_day": None, "scheduled_at": None,
        "thumbnail_url": None, "byte_hash": None, "r2_key": None,
        "drive_file_id": None, "slot_index": None, "time_slot": None,
        "visual_group_key": None,
    }
    row.update(over)
    return row


class _Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class _Http:
    """Predicate-matching PATCH, exactly like PostgREST: zero matches = no write."""

    def __init__(self, store):
        self.store = store
        self.patches = []

    def patch(self, url, *, params, json, **kwargs):
        self.patches.append((copy.deepcopy(params), copy.deepcopy(json)))
        assert set(json) == {"media_not_ready_reason"}
        def _match(row, field, pred):
            if pred == "is.null":
                return row.get(field) is None
            # Mirrors production PostgREST bare-eq semantics per the
            # 2026-10-04 probes: EVERYTHING after eq. is the literal value,
            # even when the value itself starts with a double quote.
            assert pred.startswith("eq."), pred
            return str(row.get(field)) == pred[3:]

        matches = [row for row in self.store.rows.values()
                   if all(_match(row, field, pred)
                          for field, pred in params.items())]
        if len(matches) == 1:
            matches[0]["media_not_ready_reason"] = json["media_not_ready_reason"]
        return _Response(copy.deepcopy(matches))


class MemoryStore:
    """Fresh rereads off live state + the REAL hold_repeat_media CAS."""

    def __init__(self, rows):
        self.rows = {r["id"]: copy.deepcopy(r) for r in rows}
        self.http = _Http(self)
        self.fail_page = None
        self.hold_repeat_media = types.MethodType(
            SupabaseCalendarStore.hold_repeat_media, self)

    PAGE = 500


    def rows_in_range(self, base, start, end):
        return [copy.deepcopy(r) for r in self.rows.values()
                if r["gym_id"] == base and start <= r["post_date"] <= end]

    def rows_in_range_repeat_hold(self, base, start, end):
        rows = sorted((r for r in self.rows.values()
                       if r["gym_id"] == base
                       and start <= r["post_date"] <= end),
                      key=lambda r: str(r["id"]))
        out = []
        for i in range(0, len(rows), self.PAGE):
            if self.fail_page is not None and i // self.PAGE == self.fail_page:
                raise PortalStoreError(500, "injected incomplete page")
            out.extend(copy.deepcopy(r) for r in rows[i:i + self.PAGE])
        return out

    def _client(self):
        return self.http

    def _rest(self, table):
        return f"https://supabase.test/rest/v1/{table}"

    def _headers(self, extra=None):
        return dict(extra or {})


def _sweep(store, apply=True, monkeypatch=None, hold="true"):
    if monkeypatch is not None:
        monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", hold)
    return mrs.sweep_gym(GYM, store, apply=apply, today=TODAY)


# --- flag OFF ---------------------------------------------------------------

def test_flag_off_no_holds_no_extra_write(monkeypatch):
    store = MemoryStore([
        _row("r1", "2026-10-02", status="approved"),
        _row("r2", "2026-10-06", status="approved"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch, hold="false")
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert all("repeat-hold" not in d for d in result["detail"])
    assert not config.media_repeat_sweep_hold_enabled()


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", raising=False)
    assert config.media_repeat_sweep_hold_enabled() is False


# --- dry run ----------------------------------------------------------------

def test_dry_run_counts_without_writing(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("r1", "2026-10-02", status="approved"),
        _row("r2", "2026-10-06"),
    ])
    result = _sweep(store, apply=False, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    assert store.http.patches == []
    assert store.rows["r2"]["media_not_ready_reason"] is None
    assert any("[dry-run]" in d for d in result["detail"])


# --- cross-platform identity ------------------------------------------------

def test_holds_exact_repeat_across_ig_fb_gbp(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", account="instagram", status="approved"),
        _row("ig", "2026-10-06", account="instagram"),
        _row("fb", "2026-10-07", account="facebook"),
        _row("gbp", "2026-10-08", account="googlebusiness"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 3
    for rid in ("ig", "fb", "gbp"):
        row = store.rows[rid]
        assert row["media_not_ready_reason"] == "cross_date_media_repeat_needs_new_visual"
        assert row["status"] == "pending"           # preserved
        assert row["image_url"] == URL_A            # preserved
        assert row["caption"].endswith("keep as written.")
    assert store.rows["own"]["media_not_ready_reason"] is None


def test_stale_asset_after_feed_repoint_does_not_hold(monkeypatch):
    """Feed re-point left a STALE source_media_asset_id (and stale source
    URL) on the replacement row while the delivered image_url is a brand new
    visual. The mere presence of an asset id is not evidence of a current
    binding, so no identity groups these rows: no false hold."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("a1", "2026-10-02", status="approved",
             image_url="https://cdn.test/one.jpg",
             source_media_url="https://cdn.test/raw-one.jpg",
             source_media_asset_id="asset-9"),
        _row("a2", "2026-10-06", fmt="feed",
             image_url="https://cdn.test/two.jpg",
             source_media_url="https://cdn.test/raw-one.jpg",
             source_media_asset_id="asset-9"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert store.rows["a2"]["media_not_ready_reason"] is None


def test_identical_delivered_url_holds_despite_differing_source_asset(monkeypatch):
    """IDENTICAL DELIVERED URL must hold even when the asset/source fields
    differ across the rows: the delivered URL is the only provable identity,
    and these rows deliver the same URL (asset/source corroboration is gone;
    the URL alone groups them)."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("a1", "2026-10-02", status="approved",
             image_url="https://cdn.test/raw.jpg",
             source_media_url="https://cdn.test/raw-a.jpg",
             source_media_asset_id="asset-1"),
        _row("a2", "2026-10-06", fmt="feed",
             image_url="https://cdn.test/raw.jpg",
             source_media_url="https://cdn.test/raw-b.jpg",
             source_media_asset_id="asset-2"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    assert store.rows["a2"]["media_not_ready_reason"] == (
        "cross_date_media_repeat_needs_new_visual")


def test_story_source_asset_do_not_hold_when_delivered_urls_differ(monkeypatch):
    """STALE STORY METADATA (the P1 Luna rereview finding): a story row whose
    source_media_url/source_media_asset_id point at an older raw creative
    while its delivered card URL differs. There is no immutable receipt tying
    the story asset id to the delivered card, so source/asset fields are NOT
    identity: no hold. This is the deliberate conservative rule; catching the
    transformed same-photo repeat needs an immutable render receipt (PR268)."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("a1", "2026-10-02", status="approved",
             image_url="https://cdn.test/raw.jpg",
             source_media_url="https://cdn.test/raw.jpg",
             source_media_asset_id="asset-9"),
        _row("a2", "2026-10-06", fmt="story",
             image_url="https://cdn.test/raw__story_card.jpg",
             source_media_url="https://cdn.test/raw.jpg",
             source_media_asset_id="asset-9"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert store.rows["a2"]["media_not_ready_reason"] is None


def test_url_normalization_trailing_slash_and_case(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("n1", "2026-10-02", status="approved",
             image_url="https://CDN.test/Shared-Photo.jpg/"),
        _row("n2", "2026-10-06", image_url="https://cdn.test/Shared-Photo.jpg"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1


def test_basename_alone_never_groups(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("b1", "2026-10-02", status="approved",
             image_url="https://cdn-a.test/x/photo.jpg"),
        _row("b2", "2026-10-06", image_url="https://cdn-b.test/y/photo.jpg"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []


def test_source_null_transformed_lookalike_out_of_scope(monkeypatch):
    """A row carrying neither a source URL/asset id nor the owner row's full
    URL is never grouped, even if a human would call it the same visual."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("s1", "2026-10-02", status="approved",
             image_url="https://cdn.test/raw.jpg"),
        # burned derivative: different delivered URL, no source, no asset id
        _row("s2", "2026-10-06", image_url="https://cdn.test/raw__story.jpg"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0


# --- same-date siblings -----------------------------------------------------

def test_same_date_siblings_are_one_post(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("f", "2026-10-06", account="instagram", fmt="feed"),
        _row("m", "2026-10-06", account="facebook", fmt="feed"),
        _row("st", "2026-10-06", account="instagram", fmt="story"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []


# --- approval preserved / owner rules ---------------------------------------

def test_approved_later_date_held_approval_preserved(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("pub", "2026-10-01", status="published",
             published_at="2026-10-01T15:00:00Z"),
        _row("appr", "2026-10-06", status="approved"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    row = store.rows["appr"]
    assert row["status"] == "approved"
    assert row["media_not_ready_reason"] == "cross_date_media_repeat_needs_new_visual"
    # the published owner is untouched (and would fail the CAS predicate anyway)
    assert store.rows["pub"]["media_not_ready_reason"] is None


def test_non_owner_future_pending_held_when_owner_is_later_approved(monkeypatch):
    """Owner semantics are NON-OWNER FUTURE DATE, not later-date: pending
    Oct 6 with an approved Oct 8 owner -- the approved client card is
    preserved, and the earlier pending duplicate is still held because it
    is future to today, preventing a visible repeat."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-08", status="approved"),
        _row("pend", "2026-10-06"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    assert store.rows["pend"]["media_not_ready_reason"] == (
        "cross_date_media_repeat_needs_new_visual")
    assert store.rows["own"]["media_not_ready_reason"] is None
    assert store.rows["own"]["status"] == "approved"


def test_earliest_date_owns_when_no_published_or_approved(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("early", "2026-10-03"),
        _row("late", "2026-10-09"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    assert store.rows["early"]["media_not_ready_reason"] is None
    assert store.rows["late"]["media_not_ready_reason"] is not None


# --- replacement interplay --------------------------------------------------

def test_no_hold_after_successful_replacement(monkeypatch):
    """The reread is fresh: once the swap re-pointed the later row, the URLs
    differ and there is nothing to hold."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("fixed", "2026-10-06", image_url="https://cdn.test/fresh.jpg"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []


def test_hold_when_replacement_unavailable(monkeypatch):
    """Small library: the swap leaves the repeat; the hold lane still marks it
    instead of letting it publish as a visible repeat."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("stuck", "2026-10-06"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    assert store.rows["stuck"]["media_not_ready_reason"] == (
        "cross_date_media_repeat_needs_new_visual")


# --- CAS rails ---------------------------------------------------------------

def test_stale_claim_race_loses_and_is_reported(monkeypatch):
    """A publish claim landing between the reread and the CAS makes the PATCH
    match zero rows: no hold, no silent success."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("racy", "2026-10-06"),
    ])
    # The claim lands between the sweep's reread and the CAS itself: the stale
    # before-image passes the local pre-check, and only the server-side
    # predicate (publish_claim_token is.null) stops the write.
    real_patch = store.http.patch

    def patch_with_race(url, *, params, json, **kwargs):
        store.rows["racy"]["publish_claim_token"] = "claim-123"
        return real_patch(url, params=params, json=json, **kwargs)

    store.http.patch = patch_with_race
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.rows["racy"]["media_not_ready_reason"] is None
    assert len(store.http.patches) == 1          # the CAS was attempted...
    assert any("matched no row" in d for d in result["detail"])


def test_published_and_publishing_never_held(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("live", "2026-10-06", status="published",
             published_at="2026-10-06T15:00:00Z"),
        _row("ing", "2026-10-07", status="publishing"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []


def test_past_dated_and_already_held_left_alone(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("past", "2026-10-01"),
        _row("held", "2026-10-06",
             media_not_ready_reason="some_other_hold"),
        _row("sched", "2026-10-07", scheduled_at="2026-10-07T14:00:00Z"),
        _row("rsvd", "2026-10-08", publish_reservation_day="2026-10-08"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert store.rows["held"]["media_not_ready_reason"] == "some_other_hold"


def test_hold_cas_pins_claim_reservation_and_schedule(monkeypatch):
    """The PATCH predicate itself carries the publish-safety fields."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("t", "2026-10-06"),
    ])
    _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert len(store.http.patches) == 1
    params, payload = store.http.patches[0]
    for field in ("publish_claim_token", "publish_reservation_day",
                  "scheduled_at", "published_at", "late_post_id",
                  "media_not_ready_reason"):
        assert params[field] == "is.null"
    for field in ("id", "gym_id", "post_date", "status", "image_url",
                  "caption", "account", "format"):
        assert params[field].startswith("eq.")
    assert params["source_media_asset_id"] == "is.null"  # row carries none
    assert payload == {
        "media_not_ready_reason": "cross_date_media_repeat_needs_new_visual"}


# --- fail closed -------------------------------------------------------------

def test_reread_error_fails_closed_no_write(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("t", "2026-10-06"),
    ])

    def boom(base, start, end):
        raise RuntimeError("window read failed")

    store.rows_in_range_repeat_hold = boom
    result = {"detail": []}
    mrs._hold_cross_date_repeats(
        GYM, store, apply=True, start="2026-09-04", end="2026-12-03",
        today_iso=TODAY.isoformat(), result=result)
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert all(r["media_not_ready_reason"] is None for r in store.rows.values())
    assert any("fail closed" in d for d in result["detail"])


def test_reread_error_propagates_to_sweep_result_and_receipt(monkeypatch):
    """P2: a complete tenant reread exception must NOT let the nightly runner
    mark the receipt successful. Driving the real sweep_gym: the result
    carries hold_errors and the "error" key (the existing result contract
    that makes main()/runner treat the gym as failed), and still no write
    reached the store."""
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("t", "2026-10-06"),
    ])

    def boom(base, start, end):
        raise RuntimeError("window read failed")

    store.rows_in_range_repeat_hold = boom
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["hold_errors"] == 1
    assert result["error"] == "RuntimeError"
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert all(r["media_not_ready_reason"] is None for r in store.rows.values())
    assert any("fail closed" in d for d in result["detail"])


# --- multi-identity grouping (P1) -------------------------------------------

def test_same_asset_different_delivered_urls_feed_no_hold(monkeypatch):
    """A shared source_media_asset_id with DIFFERENT delivered URLs is no
    longer any identity at all (2026-10-04): asset-only and asset-corroborated
    matching was removed because the schema has no immutable binding. Only an
    identical delivered URL can group -- see
    test_identical_delivered_url_holds_despite_differing_source_asset."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved", fmt="story",
             image_url="https://cdn.test/raw.jpg",
             source_media_url="https://cdn.test/raw.jpg",
             source_media_asset_id="asset-7"),
        _row("deriv", "2026-10-06", fmt="story",
             image_url="https://cdn.test/raw__story_card.jpg",
             source_media_asset_id="asset-7"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert store.rows["deriv"]["media_not_ready_reason"] is None


def test_delivered_url_same_groups_despite_differing_source(monkeypatch):
    """Two rows DELIVERED the same URL are repeats even if their source URLs
    differ -- the delivered URL is always indexed."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved",
             image_url=URL_A, source_media_url="https://cdn.test/orig-a.jpg"),
        _row("later", "2026-10-06",
             image_url=URL_A, source_media_url="https://cdn.test/orig-b.jpg"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    assert store.rows["later"]["media_not_ready_reason"] == (
        "cross_date_media_repeat_needs_new_visual")


def test_stale_feed_source_url_does_not_cause_hold(monkeypatch):
    """Feed re-point left a stale source_media_url on the replacement row.
    Without an immutable asset id the source URL is NOT trusted as identity,
    so the differing delivered URL wins: no manufactured repeat."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved",
             image_url="https://cdn.test/photo-a.jpg",
             source_media_url="https://cdn.test/raw-lib.jpg"),
        _row("rep", "2026-10-06", fmt="feed",
             image_url="https://cdn.test/photo-b.jpg",
             source_media_url="https://cdn.test/raw-lib.jpg"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert store.rows["rep"]["media_not_ready_reason"] is None


def test_story_source_url_alone_never_groups(monkeypatch):
    """A story's delivered image_url is a burned caption card; its
    source_media_url names the raw library photo -- but source-only matching
    is removed (no immutable receipt), so differing delivered URLs never
    group. PR268 release gap: the transformed same-photo repeat here is NOT
    caught by this lane."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved",
             image_url="https://cdn.test/raw-lib.jpg"),
        _row("story", "2026-10-06", fmt="story",
             image_url="https://cdn.test/raw-lib__story.jpg",
             source_media_url="https://cdn.test/raw-lib.jpg"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert store.rows["story"]["media_not_ready_reason"] is None


def test_path_case_is_significant_host_case_is_not(monkeypatch):
    """CDN paths are case-sensitive (Shared-Photo.jpg != shared-photo.jpg:
    no grouping); host capitalization is not (CDN.test == cdn.test: groups)."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("h1", "2026-10-02", status="approved",
             image_url="https://CDN.test/Shared-Photo.jpg"),
        _row("h2", "2026-10-06",
             image_url="https://cdn.test/Shared-Photo.jpg"),
        _row("p1", "2026-10-03", status="approved",
             image_url="https://cdn.test/MixedCase.jpg"),
        _row("p2", "2026-10-07",
             image_url="https://cdn.test/mixedcase.jpg"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1          # only the host-case pair
    assert store.rows["h2"]["media_not_ready_reason"] is not None
    assert store.rows["p2"]["media_not_ready_reason"] is None


def test_url_canonicalization_ports_and_fragments(monkeypatch):
    """Default ports and fragments normalize away without touching path case
    or query semantics."""
    assert mrs._canonical_media_url("HTTPS://CDN.test:443/a/B.jpg?X=1#sig") == \
        "https://cdn.test/a/B.jpg?X=1"
    assert mrs._canonical_media_url("http://cdn.test:8080/a.jpg") == \
        "http://cdn.test:8080/a.jpg"
    assert mrs._canonical_media_url("not a url") is None


def test_rows_sharing_multiple_identities_held_once(monkeypatch):
    """Distinct target rows sharing a delivered URL (with redundant source /
    asset fields present) are still held exactly once (distinct targets
    deduped across groups)."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved",
             image_url="https://cdn.test/raw.jpg", source_media_asset_id="asset-3"),
        _row("st", "2026-10-06", fmt="story",
             image_url="https://cdn.test/raw.jpg", source_media_asset_id="asset-3",
             source_media_url="https://cdn.test/raw.jpg"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    assert len(store.http.patches) == 1


# --- complete paginated read (P2) -------------------------------------------

def test_hold_reads_beyond_1000_rows(monkeypatch):
    """The repeat group sits entirely past the old 1000-row cap: a complete
    tenant-scoped paginated read must still see it and hold it."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    rows = [
        _row(f"fill{i:04d}", "2026-10-02",
             image_url=f"https://cdn.test/filler-{i:04d}.jpg")
        for i in range(1100)
    ]
    rows.append(_row("own", "2026-10-03", status="approved",
                     image_url="https://cdn.test/shared.jpg"))
    rows.append(_row("later", "2026-10-09",
                     image_url="https://cdn.test/shared.jpg"))
    store = MemoryStore(rows)
    result = {"detail": []}
    mrs._hold_cross_date_repeats(
        GYM, store, apply=True, start="2026-09-04", end="2026-12-03",
        today_iso=TODAY.isoformat(), result=result)
    assert result["rows_held"] == 1
    assert store.rows["later"]["media_not_ready_reason"] == (
        "cross_date_media_repeat_needs_new_visual")
    assert all(r["media_not_ready_reason"] is None
               for k, r in store.rows.items() if k != "later")


def test_incomplete_page_fails_closed_zero_holds(monkeypatch):
    """A page failing mid-pagination invalidates the ENTIRE read: zero hold
    writes, nothing decided from a partial book."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    rows = [
        _row("own", "2026-10-02", status="approved",
             image_url="https://cdn.test/shared.jpg"),
    ]
    rows += [
        _row(f"dup{i:04d}", "2026-10-09",
             image_url="https://cdn.test/shared.jpg")
        for i in range(600)
    ]
    store = MemoryStore(rows)
    store.fail_page = 1                      # second page dies mid-read
    result = {"detail": []}
    mrs._hold_cross_date_repeats(
        GYM, store, apply=True, start="2026-09-04", end="2026-12-03",
        today_iso=TODAY.isoformat(), result=result)
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert all(r["media_not_ready_reason"] is None
               for r in store.rows.values())
    assert any("fail closed" in d for d in result["detail"])


# --- CAS pins visual media columns (P4) -------------------------------------

def test_concurrent_thumbnail_and_slot_mutation_loses(monkeypatch):
    """A thumbnail_url/slot_index change landing between the grouping read and
    the CAS makes the predicate match zero rows: no hold, row left intact."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("t", "2026-10-06", thumbnail_url="https://cdn.test/thumb-a.jpg",
             slot_index=1, time_slot="09:00"),
    ])
    real_patch = store.http.patch

    def patch_with_mutation(url, *, params, json, **kwargs):
        store.rows["t"]["thumbnail_url"] = "https://cdn.test/thumb-b.jpg"
        store.rows["t"]["slot_index"] = 2
        store.rows["t"]["time_slot"] = "12:00"
        return real_patch(url, params=params, json=json, **kwargs)

    store.http.patch = patch_with_mutation
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.rows["t"]["media_not_ready_reason"] is None
    assert any("matched no row" in d for d in result["detail"])


def test_hold_cas_pins_visual_media_columns(monkeypatch):
    """thumbnail_url, byte_hash, r2_key, drive_file_id, slot_index and
    time_slot are part of the exact before-image for the hold CAS."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("t", "2026-10-06", thumbnail_url="https://cdn.test/th.jpg",
             byte_hash="bh-1", r2_key="r2/1.jpg", drive_file_id="drv-1",
             slot_index=1, time_slot="09:00"),
    ])
    _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert len(store.http.patches) == 1
    params, _payload = store.http.patches[0]
    for field in ("thumbnail_url", "byte_hash", "r2_key", "drive_file_id",
                  "slot_index", "time_slot"):
        # Bare eq.<value> is the ONLY form with live-match evidence: the
        # 2026-10-04 production probe showed eq."pending" matched ZERO rows
        # against real PostgREST while every bare eq.<value> matched the row.
        assert params[field].startswith("eq."), field
        assert not params[field].startswith('eq."'), field


# --- reporting (P5) ----------------------------------------------------------

def test_reporting_counts_held_skipped_errors_and_approval(monkeypatch):
    """Held targets are distinct, skips and errors are explicit, and an
    approved later-date row is held WITH its approval preserved and reported
    as needing a new visual -- not silently skipped."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("pend", "2026-10-06"),
        _row("appr", "2026-10-07", status="approved"),
        _row("live", "2026-10-08", status="published",
             published_at="2026-10-08T15:00:00Z"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 2
    assert result["hold_errors"] == 0
    assert result["rows_skipped"] >= 1       # the published twin
    assert store.rows["appr"]["status"] == "approved"
    assert store.rows["appr"]["media_not_ready_reason"] == (
        "cross_date_media_repeat_needs_new_visual")
    assert any("approved" in d and "needs new visual" in d
               for d in result["detail"])


def test_detail_never_leaks_raw_urls(monkeypatch):
    """Privacy: detail lines carry fingerprints, not raw media URLs or signed
    URL fragments, and never captions."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    secret = "https://cdn.test/photo.jpg?sig=SECRETTOKEN&exp=999"
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved", image_url=secret),
        _row("t", "2026-10-06", image_url=secret),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    for line in result["detail"]:
        assert "SECRETTOKEN" not in line
        assert secret not in line
        assert "Caption for" not in line
    assert any("fp:" in d for d in result["detail"])


# --- REAL SupabaseCalendarStore pagination via fake PostgREST HTTP ----------

class _PagedStore:
    """Drives the REAL SupabaseCalendarStore.rows_in_range_repeat_hold (and
    the real hold_repeat_media CAS) against fake PostgREST GET/PATCH traffic:
    id=gt cursor pages, within- and between-page ordering, malformed /
    out-of-scope / error-page rejection, and the max-page tripwire."""
    PAGE = SupabaseCalendarStore._REPEAT_HOLD_PAGE_SIZE
    MAX_PAGES = SupabaseCalendarStore._REPEAT_HOLD_MAX_PAGES

    def __init__(self, rows, *, mode="ok"):
        self.rows = {r["id"]: copy.deepcopy(r) for r in rows}
        # Class constants the real methods read off the instance:
        self._REPEAT_HOLD_PAGE_SIZE = SupabaseCalendarStore._REPEAT_HOLD_PAGE_SIZE
        self._REPEAT_HOLD_MAX_PAGES = SupabaseCalendarStore._REPEAT_HOLD_MAX_PAGES
        self.mode = mode          # ok | http_error | nonmonotonic | out_of_scope
                                  # | malformed | endless
        self.gets = 0
        self.http = _Http(self)
        self.hold_repeat_media = types.MethodType(
            SupabaseCalendarStore.hold_repeat_media, self)
        self.rows_in_range_repeat_hold = types.MethodType(
            SupabaseCalendarStore.rows_in_range_repeat_hold, self)

    def rows_in_range(self, base, start, end):
        return [copy.deepcopy(r) for r in self.rows.values()
                if r["gym_id"] == base and start <= r["post_date"] <= end]

    def _client(self):
        return self

    def _rest(self, table):
        return f"https://supabase.test/rest/v1/{table}"

    def _headers(self, extra=None):
        return dict(extra or {})

    def patch(self, url, *, params, json, **kwargs):
        return self.http.patch(url, params=params, json=json, **kwargs)

    def get(self, url, *, params, headers, timeout):
        self.gets += 1
        if self.mode == "http_error":
            return _Response({"code": 500}) if False else _ErrorResponse(500)
        last = params.get("id")
        lo = last.split(".", 1)[1] if last else None
        pool = sorted((r for r in self.rows.values()
                       if lo is None or str(r["id"]) > lo),
                      key=lambda r: str(r["id"]))
        page = [copy.deepcopy(r) for r in pool[:self.PAGE]]
        if self.mode == "endless":
            # Fabricate valid, ascending, never-before-seen rows forever so
            # the page cap -- not the data -- must stop the read.
            base = f"endless-{self.gets:05d}-"
            page = [dict(_row(f"{base}{i:03d}", "2026-10-09",
                              image_url=f"https://cdn.test/e-{self.gets}-{i}.jpg"))
                    for i in range(self.PAGE)]
        elif self.mode == "nonmonotonic" and len(page) > 1:
            page[0], page[1] = page[1], page[0]   # descending pair in-page
        elif self.mode == "out_of_scope" and page:
            bad = dict(page[-1])
            bad["gym_id"] = "somebody-else"
            page[-1] = bad
        elif self.mode == "malformed" and page:
            page[-1] = {"no_id": True}
        return _Response(page)


class _ErrorResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "server error"

    def json(self):
        return {}


def _paged_rows(n_filler, shared_url="https://cdn.test/shared.jpg"):
    rows = [
        _row(f"fill{i:05d}", "2026-10-02",
             image_url=f"https://cdn.test/filler-{i:05d}.jpg")
        for i in range(n_filler)
    ]
    rows.append(_row("own", "2026-10-03", status="approved",
                     image_url=shared_url))
    rows.append(_row("later", "2026-10-09", image_url=shared_url))
    return rows


def test_real_paginated_read_holds_repeat_past_1000_rows(monkeypatch):
    """The REAL store reader walks id=gt cursor pages (>1000 rows => at least
    3 pages), the repeat past the old 1000-row cap is found and held, and the
    real CAS predicate pins the row."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = _PagedStore(_paged_rows(1100))
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert store.gets >= 3
    assert result["rows_held"] == 1
    assert store.rows["later"]["media_not_ready_reason"] == (
        "cross_date_media_repeat_needs_new_visual")
    assert len(store.http.patches) == 1


@pytest.mark.parametrize("mode", ["http_error", "nonmonotonic",
                                  "out_of_scope", "malformed"])
def test_real_paginated_read_fails_closed_no_hold_writes(monkeypatch, mode):
    """Any bad page -- HTTP error, non-ascending ids WITHIN a page, an
    out-of-scope row, a malformed row -- invalidates the ENTIRE read through
    the real store code: the sweep writes zero holds."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = _PagedStore(_paged_rows(1100), mode=mode)
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert store.http.patches == []
    assert all(r["media_not_ready_reason"] is None
               for r in store.rows.values())
    assert any("fail closed" in d for d in result["detail"])


def test_real_paginated_read_max_page_tripwire(monkeypatch):
    """A server that never returns a short page cannot loop forever: the
    real reader raises after _REPEAT_HOLD_MAX_PAGES, the sweep fails closed
    and nothing is held."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = _PagedStore([], mode="endless")
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert store.gets == _PagedStore.MAX_PAGES
    assert result["rows_held"] == 0
    assert store.http.patches == []


def test_real_hold_cas_refuses_incomplete_before_image():
    """A before image missing any APPLIED core _VISUAL_MEDIA_CAS_COLUMNS key
    is refused locally -- never a silent partial predicate. The four draft
    scene columns (not yet migrated to live content_calendar, 2026-10-04
    Zanshin probe) are optional: missing ones are skipped, present ones are
    pinned."""
    from agent.portal_calendar_store import (_CORE_VISUAL_MEDIA_CAS_COLUMNS,
                                             _DRAFT_SCENE_CAS_COLUMNS)
    core = _row("core", "2026-10-06")
    store = _PagedStore([core])
    for key in _CORE_VISUAL_MEDIA_CAS_COLUMNS:
        incomplete = dict(core)
        del incomplete[key]
        assert store.hold_repeat_media(GYM, incomplete, "some reason") is None
    assert store.http.patches == []

    # The live Zanshin failure, reproduced: a row whose select=* lacks the
    # four draft columns still holds, and the predicate excludes them.
    bare = {k: v for k, v in _row("bare", "2026-10-07").items()
            if k not in _DRAFT_SCENE_CAS_COLUMNS}
    store = _PagedStore([_row("bare", "2026-10-07")])
    assert store.hold_repeat_media(GYM, bare, "some reason") is not None
    assert len(store.http.patches) == 1
    params, _ = store.http.patches[0]
    for key in _DRAFT_SCENE_CAS_COLUMNS:
        assert key not in params

    # Draft columns present on the row ARE pinned in the predicate, bare form.
    rich = _row("rich", "2026-10-08", byte_hash="bh-9", r2_key="r2/9.jpg",
                drive_file_id="drv-9", visual_group_key="vg-9")
    store = _PagedStore([rich])
    assert store.hold_repeat_media(GYM, rich, "some reason") is not None
    assert len(store.http.patches) == 1
    params, _ = store.http.patches[0]
    assert params["byte_hash"] == "eq.bh-9"
    assert params["r2_key"] == "eq.r2/9.jpg"
    assert params["drive_file_id"] == "eq.drv-9"
    assert params["visual_group_key"] == "eq.vg-9"


def test_hold_cas_bare_eq_reproduces_live_quoted_filter_failure(monkeypatch):
    """Live probe 2026-10-04: status=eq.\"pending\" matched ZERO rows against
    real PostgREST while status=eq.pending matched the row (HTTP 200). The
    hold predicate must use the bare form -- no eq."..." anywhere -- or the
    hold can never match and every repeat slips through."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("t", "2026-10-06"),
    ])
    _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert len(store.http.patches) == 1
    params, _payload = store.http.patches[0]
    assert params["status"] == "eq.pending"
    for field, pred in params.items():
        assert not pred.startswith('eq."'), (field, pred)
        assert "\\" not in pred, (field, pred)


def test_comma_quote_paren_captions_hold_with_exact_bare_predicates(monkeypatch):
    """2026-10-04 follow-up production probes (read-only): live pending/approved
    rows whose captions contain a comma, a double quote, or parentheses matched
    HTTP 200 with exactly ONE row for the bare caption=eq.<exact caption> form
    and ZERO rows for the quoted form. Captions with these characters are
    common; they must hold with exact bare predicates -- the full CAS intact,
    status/approval preserved -- never fail closed."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("comma", "2026-10-06",
             caption="Sale this weekend, don't miss it"),
        _row("quote", "2026-10-07", caption='She said "PRs over pounds" today'),
        _row("leadquote", "2026-10-08", caption='"PRs over pounds", she said'),
        _row("paren", "2026-10-09", caption="Bring a friend (spots limited!)"),
        _row("combo", "2026-10-10",
             caption='Coach tip: "breathe, brace, lift" (see highlights), ok?'),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 5
    assert result["hold_errors"] == 0
    assert len(store.http.patches) == 5
    for params, payload in store.http.patches:
        # Exact bare predicates: no quoting, no escaping, full CAS field set.
        for field, pred in params.items():
            if pred == "is.null":
                continue
            # Bare eq with the EXACT stored value, never a re-encoded form:
            # a leading-quote caption's predicate legitimately begins
            # eq." -- that quote is part of the value, not wrapping.
            row = next(r for r in store.rows.values()
                       if r["id"] == params["id"][len("eq."):])
            expected = "eq." + str(row[field]) if row[field] is not None \
                else "is.null"
            assert pred == expected, (field, pred, expected)
        assert payload == {"media_not_ready_reason":
                           "cross_date_media_repeat_needs_new_visual"}
    held = {rid: r for rid, r in store.rows.items()
            if r["media_not_ready_reason"]
            == "cross_date_media_repeat_needs_new_visual"}
    assert set(held) == {"comma", "quote", "leadquote", "paren", "combo"}
    for rid, row in held.items():
        assert row["status"] == "pending"           # approval never invented
        assert row["image_url"] == URL_A            # media preserved


def test_reserved_char_caption_mismatch_noops_without_write():
    """Exactness is two-sided: a before image whose reserved-character caption
    no longer matches the stored row must match ZERO rows server-side and
    leave the row untouched (the stale no-op), never PATCH."""
    row = _row("t", "2026-10-06",
               caption="Sale this weekend, don't miss it (bring a friend)")
    store = _PagedStore([row])
    stale = dict(row, caption="Sale this weekend, don't miss it "
                              "(bring two friends)")
    assert store.hold_repeat_media(GYM, stale, "some reason") is None
    assert store.rows["t"]["media_not_ready_reason"] is None   # untouched


def test_backslash_caption_fails_closed_with_precise_blocker(monkeypatch):
    """Backslash is the one character still without live-match evidence: it
    can flip the PostgREST parser into escape/quoted-literal mode, so a
    caption containing it fails closed with a precise blocker -- never
    PATCH, never a weakened predicate. The row is reported for a manual
    hold instead."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("t", "2026-10-06", caption="Line one\\nline two"),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert result["hold_errors"] == 1
    assert store.http.patches == []          # no weakened predicate was sent
    assert store.rows["t"]["media_not_ready_reason"] is None
    assert any("hold CAS error" in d for d in result["detail"])


def test_malformed_cas_value_fails_closed(monkeypatch):
    """Non-scalar CAS values are malformed before-images: refused before any
    write."""
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved"),
        _row("bad", "2026-10-06", caption={"ops": "not a string"}),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 0
    assert result["hold_errors"] == 1
    assert store.http.patches == []
    assert all(r["media_not_ready_reason"] is None for r in store.rows.values())


def test_empty_caption_matches_exactly_and_holds(monkeypatch):
    monkeypatch.setenv("AGENT_MEDIA_REPEAT_SWEEP_HOLD", "true")
    store = MemoryStore([
        _row("own", "2026-10-02", status="approved", caption=""),
        _row("empty", "2026-10-06", caption=""),
    ])
    result = _sweep(store, apply=True, monkeypatch=monkeypatch)
    assert result["rows_held"] == 1
    assert result["hold_errors"] == 0
    params, _payload = store.http.patches[0]
    assert params["caption"] == "eq."
    assert store.rows["empty"]["media_not_ready_reason"] == (
        "cross_date_media_repeat_needs_new_visual")


def test_stale_empty_vs_nonempty_caption_noops_without_write():
    row = _row("t", "2026-10-06", caption="Caption restored after the read")
    store = _PagedStore([row])
    stale = dict(row, caption="")
    assert store.hold_repeat_media(GYM, stale, "some reason") is None
    assert len(store.http.patches) == 1
    assert store.http.patches[0][0]["caption"] == "eq."
    assert store.rows["t"]["media_not_ready_reason"] is None


def test_stale_before_image_noops_without_write():
    """A before image whose fields no longer match the stored row matches
    zero rows server-side: hold_repeat_media returns None and the row is
    untouched (the stale-field no-op)."""
    row = _row("t", "2026-10-06")
    store = _PagedStore([row])
    stale = dict(row, caption="An older caption that was since edited")
    assert store.hold_repeat_media(GYM, stale, "some reason") is None
    assert store.rows["t"]["media_not_ready_reason"] is None   # untouched
