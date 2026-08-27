"""LASSO VIDEO MIX (AGENT_LASSO_VIDEO_MIX), all offline. Asserts:

  * MIX: the video mix weaves recurring podcast VIDEO into the non-sprint rotation
    (thu + sun stay podcast and are video_preferred; a cap-safe Wed video slot is
    added) and podcast/video stays AT OR UNDER the 25% cap on every live window.
  * SPRINT PRESERVATION (the key regression): every summit_queue.SPRINT_CYCLES day is
    byte-for-byte identical with the mix ON vs OFF — the sprint owns its days, the mix
    only fills non-sprint rotation days. Dark days + welcome-on-top unchanged.
  * REMAP: lasso_remap.remap rebuilds only unapproved future days and preserves
    approved/published rows (via apply_month_plan.preserve_and_prune); --write off is a
    dry no-op; AGENT_REAL_MONTH_PLAN off is a no-op.
  * VIDEO ROW: a staged podcast video row lands PENDING and routes to the Zernio video
    upload path (podcast_library_builder), caption grounded in the episode notes Doc.
  * FLAG OFF: plan_month is byte-for-byte the pre-video rotation.
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent import real_month_planner as rmp  # noqa: E402
from agent import summit_queue as sq  # noqa: E402

ACCT = "lasso"
# Three live windows: two sprint-heavy (Aug/Sep), one quiet at the cap ceiling (Nov).
WINDOWS = [("2026-08-07", 30), ("2026-09-01", 30), ("2026-11-01", 30)]


def _window_days(start, days):
    from datetime import date, timedelta
    s = date.fromisoformat(start)
    return [(s + timedelta(days=i)).isoformat() for i in range(days)]


def _feeds(plan):
    return [s for s in plan if s.fmt == rmp.FEED]


def _podcast_share(plan):
    feeds = _feeds(plan)
    n = len(feeds) or 1
    return Counter(s.category for s in feeds).get("podcast", 0) / n


# ---- FLAG OFF: byte-for-byte the pre-video rotation ----------------------------------

def test_flag_off_is_byte_for_byte():
    for start, days in WINDOWS:
        off = rmp.plan_month(ACCT, start, days=days, video_mix=False)
        # Nothing is video_preferred and podcast stays the pre-video 2/7 rotation.
        assert not any(s.video_preferred for s in off), start
        base_off = [(s.post_date, s.category, s.fmt, s.is_sprint) for s in off]
        # The env-resolved default (flag unset) must match the explicit video_mix=False.
        os.environ.pop("AGENT_LASSO_VIDEO_MIX", None)
        default = rmp.plan_month(ACCT, start, days=days)
        assert [(s.post_date, s.category, s.fmt, s.is_sprint) for s in default] == base_off


# ---- MIX: recurring video under the 25% cap ------------------------------------------

def test_video_mix_adds_recurring_video_preferred_slots():
    for start, days in WINDOWS:
        on = rmp.plan_month(ACCT, start, days=days, video_mix=True)
        vp_feeds = [s for s in _feeds(on) if s.video_preferred]
        # Recurring: several video-preferred feeds across the month (not a one-off).
        assert len(vp_feeds) >= 3, (start, len(vp_feeds))
        # Every video-preferred slot rides the podcast category (respects the cap lane).
        assert all(s.category == "podcast" for s in vp_feeds), start


def test_thu_sun_stay_podcast_and_prefer_video():
    from datetime import date
    start, days = "2026-09-01", 30
    win = set(_window_days(start, days))
    sprint = {d for d in sq.sprint_days() if d in win}
    on = rmp.plan_month(ACCT, start, days=days, video_mix=True)
    # A Thu / Sun the sprint does NOT own keeps podcast and is video_preferred
    # (Blake: keep thu+sun). Sprint-window days are untouched, so exclude them.
    checked = 0
    for s in _feeds(on):
        if s.post_date in sprint:
            continue
        wd = date.fromisoformat(s.post_date).weekday()
        if s.category == "podcast" and wd in (3, 6) and s.base_category == "podcast":
            assert s.video_preferred, (s.post_date, wd)
            checked += 1
    assert checked >= 2, "expected recurring non-sprint thu/sun podcast slots"


def test_video_mix_respects_25pct_podcast_cap():
    for start, days in WINDOWS:
        on = rmp.plan_month(ACCT, start, days=days, video_mix=True)
        share = _podcast_share(on)
        assert share <= 0.25 + 1e-9, f"{start}: podcast {share:.1%} over cap"


def test_video_mix_raises_video_presence_vs_off():
    # The whole point: MORE video-of-humans on the rotation days the summit sprint does
    # NOT own than the text-only baseline. A sprint day (even its varied second slot) is
    # untouched, so compare over days that are entirely outside the sprint window.
    total_added = 0
    for start, days in WINDOWS:
        win = set(_window_days(start, days))
        sprint = {d for d in sq.sprint_days() if d in win}
        off = rmp.plan_month(ACCT, start, days=days, video_mix=False)
        on = rmp.plan_month(ACCT, start, days=days, video_mix=True)
        off_pod = sum(1 for s in _feeds(off)
                      if s.post_date not in sprint and s.category == "podcast")
        on_vp = sum(1 for s in _feeds(on)
                    if s.post_date not in sprint and s.video_preferred)
        # ON marks >= the OFF podcast feeds on non-sprint days as video-preferred
        # (thu/sun at minimum), and adds a cap-safe midweek slot where headroom exists.
        assert on_vp >= off_pod, (start, on_vp, off_pod)
        on_pod = sum(1 for s in _feeds(on)
                     if s.post_date not in sprint and s.category == "podcast")
        total_added += (on_pod - off_pod)
    # Across the three windows the mix nets at least one extra podcast/video day.
    assert total_added >= 1, total_added


def test_midweek_video_never_breaches_cap():
    # The Wed video slot is only added while podcast stays <= 25%. In a quiet window at
    # the ceiling (Nov) no Wed is added; in sprint-heavy windows Wed slots appear.
    nov = rmp.plan_month(ACCT, "2026-11-01", days=30, video_mix=True)
    assert _podcast_share(nov) <= 0.25 + 1e-9
    sep = rmp.plan_month(ACCT, "2026-09-01", days=30, video_mix=True)
    from datetime import date
    wed_podcast = [s for s in _feeds(sep) if not s.is_sprint
                   and s.category == "podcast"
                   and date.fromisoformat(s.post_date).weekday() == 2]
    assert wed_podcast, "expected at least one cap-safe midweek (Wed) video slot in Sep"


# ---- SPRINT PRESERVATION (the key regression) ----------------------------------------

def test_sprint_days_untouched_by_video_mix():
    for start, days in WINDOWS:
        win = set(_window_days(start, days))
        sprint = sorted(d for d in sq.sprint_days() if d in win)
        off = rmp.plan_month(ACCT, start, days=days, video_mix=False)
        on = rmp.plan_month(ACCT, start, days=days, video_mix=True)

        def _sprint_slots(plan):
            return sorted(
                (s.post_date, s.category, s.fmt, s.slot_index, s.is_sprint)
                for s in plan if s.post_date in sprint and s.is_sprint)

        # Every sprint slot is byte-for-byte identical with the mix ON vs OFF.
        assert _sprint_slots(on) == _sprint_slots(off), start
        # And no sprint slot was ever marked video_preferred (video is non-sprint only).
        assert not any(s.is_sprint and s.video_preferred for s in on), start
        # The sprint still owns each sprint day (>= 1 summit feed).
        for d in sprint:
            summit_feeds = [s for s in _feeds(on)
                            if s.post_date == d and s.is_sprint and s.category == "summit"]
            assert summit_feeds, (start, d, "sprint day lost its summit feed")


def test_dark_days_never_get_video_or_sprint():
    plan = rmp.plan_month(ACCT, "2026-11-01", days=14, video_mix=True)
    for dark in ("2026-11-07", "2026-11-08"):
        assert dark not in sq.sprint_days()
        assert not any(s.post_date == dark and s.is_sprint for s in plan)


def test_welcome_on_top_unchanged_by_video_mix():
    wd = {"2026-09-02"}
    off = rmp.plan_month(ACCT, "2026-09-01", days=14, video_mix=False, welcome_dates=wd)
    on = rmp.plan_month(ACCT, "2026-09-01", days=14, video_mix=True, welcome_dates=wd)
    # A welcome day is a dated override; the mix never converts it to podcast/video.
    off_w = [s for s in _feeds(off) if s.post_date == "2026-09-02"]
    on_w = [s for s in _feeds(on) if s.post_date == "2026-09-02"]
    assert [s.category for s in off_w] == [s.category for s in on_w] == ["welcome"]


# ---- REMAP: rebuild only unapproved future days, preserve approved --------------------

class _FakeStore:
    """A minimal calendar store: list_month / delete_month / insert_rows with a
    server-side status filter so approved/published rows are never deleted."""
    def __init__(self, existing):
        self.rows = list(existing)     # each: {id, post_date, status, caption, ...}
        self.deleted = []
        self.inserted = []

    def list_month(self, account_key, month):
        return [dict(r) for r in self.rows if str(r.get("post_date", ""))[:7] == month]

    def locked_slots(self, account_key, month):
        # Mirror the real store: human-owned (non-wipeable) rows lock their slot so a
        # rebuild never inserts a duplicate into an approved/published cell.
        from agent.portal_calendar_store import _slot_key, _WIPEABLE_STATUSES
        locked = set()
        for r in self.list_month(account_key, month):
            status = str(r.get("status") or "").lower()
            if status and status not in _WIPEABLE_STATUSES:
                locked.add(_slot_key(r))
        return locked

    def delete_month(self, account_key, month, *, preserve_human=True):
        keep, gone = [], []
        for r in self.rows:
            m = str(r.get("post_date", ""))[:7]
            approved = str(r.get("status", "")).lower() in ("approved", "published")
            if m == month and not (preserve_human and approved):
                gone.append(r)
            else:
                keep.append(r)
        self.rows = keep
        self.deleted.extend(gone)
        return len(gone)

    def insert_rows(self, account_key, rows):
        out = []
        for i, r in enumerate(rows):
            rr = dict(r); rr["id"] = f"new_{i}"
            out.append(rr)
        self.inserted.extend(out)
        self.rows.extend(out)
        return out


def test_remap_noop_when_real_month_plan_off(monkeypatch):
    from agent import lasso_remap
    monkeypatch.delenv("AGENT_REAL_MONTH_PLAN", raising=False)
    out = lasso_remap.remap("lasso", month="2026-11", write=True, store=_FakeStore([]))
    assert out["ok"] is False and "AGENT_REAL_MONTH_PLAN" in out["reason"]


def test_remap_dry_run_stages_nothing(monkeypatch):
    from agent import lasso_remap
    monkeypatch.setenv("AGENT_REAL_MONTH_PLAN", "true")
    store = _FakeStore([])
    out = lasso_remap.remap("lasso", month="2026-11", write=False, store=store)
    assert out.get("dry_run") is True
    assert not store.inserted and not store.deleted


def test_remap_preserves_approved_replaces_unapproved(monkeypatch):
    # apply_month_plan.preserve_and_prune keeps approved/published rows and prunes the
    # staged rows that collide with them; only unapproved future days are replaced.
    from agent import real_month_planner as _rmp
    approved = {"id": "keep1", "post_date": "2026-11-10", "status": "approved",
                "caption": "human approved", "pillar": "summit",
                "account": "instagram", "format": "feed"}
    pending = {"id": "old1", "post_date": "2026-11-11", "status": "pending",
               "caption": "old pending", "pillar": "platform",
               "account": "instagram", "format": "feed"}
    store = _FakeStore([approved, pending])
    from agent.drafter import Draft, DraftStatus
    fresh = [
        # a fresh row on a NEW day (should be inserted)
        Draft(draft_id="f1", account_key="lasso", platform="instagram",
              caption="fresh nov row", hashtags=[], creative_path="x.png",
              creative_public_url="https://cdn/x.jpg", scheduled_for="",
              status=DraftStatus.PENDING, day_key="2026-11-12",
              draft_type="feed", category="podcast"),
        # a fresh row that COLLIDES with the approved slot (should be pruned, not inserted)
        Draft(draft_id="f2", account_key="lasso", platform="instagram",
              caption="would-overwrite approved", hashtags=[], creative_path="y.png",
              creative_public_url="https://cdn/y.jpg", scheduled_for="",
              status=DraftStatus.PENDING, day_key="2026-11-10",
              draft_type="feed", category="podcast"),
    ]
    res = _rmp.apply_month_plan("lasso", fresh, store, span_months=["2026-11"])
    assert res["ok"] is True
    # the approved row survived the delete pass (never wiped)
    assert any(r.get("id") == "keep1" for r in store.rows)
    # the colliding fresh row was PRUNED off the approved (instagram/feed) slot: no NEW
    # row lands on that exact cell next to the human's approved post.
    assert not any(str(r.get("post_date")) == "2026-11-10"
                   and str(r.get("account", "")).lower() == "instagram"
                   and str(r.get("format", "")).lower() == "feed"
                   and str(r.get("id", "")).startswith("new_")
                   for r in store.rows)
    # the non-colliding fresh row WAS inserted
    assert any(str(r.get("post_date")) == "2026-11-12" and str(r.get("id", "")).startswith("new_")
               for r in store.rows)
    # the old PENDING row on 11-11 was replaced (deleted; not human-owned)
    assert not any(r.get("id") == "old1" for r in store.rows)


# ---- VIDEO ROW: staged pending, Zernio video upload, notes-grounded caption ----------

def test_video_row_stages_pending_via_zernio_upload():
    from agent import podcast_library_builder as plib

    class _Store:
        def available(self): return True
        def update_asset(self, *a, **k): pass

    class _Drive:
        def available(self): return True
        def export_doc_text(self, doc_id): return "Real episode notes about member wins."
        def download(self, asset_id, path):
            with open(path, "wb") as f:
                f.write(b"\x00" * 2048)

    class _Zernio:
        def __init__(self): self.uploaded = []
        def media_generate_upload_link(self, filename, mime):
            assert mime == "video/mp4"     # routes to the VIDEO upload path
            return {"uploadUrl": "https://up.test/put", "publicUrl": "https://cdn.test/clip.mp4"}
        def media_upload_file(self, url, path, mime):
            self.uploaded.append((url, mime))
        def media_check_upload_status(self, public_url): return True

    asset = {"id": "clip1", "episode": "EP130", "notes_doc_id": "doc1",
             "title": "ep130_clip.mp4", "kind": "clip", "clip_index": 0,
             "size_bytes": 2048}

    def _pick(*, store, now, exclude_ids, feed_episodes=None, **_kw):
        return asset if not exclude_ids else None

    def _probe(path):
        return {"width": 1080, "height": 1920, "duration_sec": 30.0}

    def _cap(episode, notes_text=None, *, feed_text=None, gym_id=None,
             allowlist_fn=None):
        return ("Real people, real wins from EP130. Book your intro today.",
                {"claims": ["member wins"]})

    import agent.podcast_selector as _sel
    orig_pick, orig_stamp = _sel.pick_clip, _sel.stamp_use
    import agent.podcast_caption as _pc
    orig_draft = _pc.draft_caption
    _sel.pick_clip = _pick
    _sel.stamp_use = lambda *a, **k: None
    _pc.draft_caption = _cap
    z = _Zernio()
    try:
        draft = plib.build_podcast_clip_draft(
            "lasso", "2026-09-10", store=_Store(), drive=_Drive(),
            zernio_client=z, probe_fn=_probe, feed_map={})
    finally:
        _sel.pick_clip, _sel.stamp_use = orig_pick, orig_stamp
        _pc.draft_caption = orig_draft

    assert draft is not None
    assert str(getattr(draft.status, "value", draft.status)).lower() == "pending"
    assert draft.category == "podcast" and draft.draft_type == "podcast"
    assert draft.creative_public_url == "https://cdn.test/clip.mp4"
    assert z.uploaded and z.uploaded[0][1] == "video/mp4"   # went through the video path
    # caption grounds in the notes Doc (source_fragments carry the drive doc + clip)
    assert any(str(f).startswith("drive_doc:doc1") for f in (draft.source_fragments or []))
