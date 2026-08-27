"""Caption grounding tests (spec Wave 4): a missing/empty notes Doc means the
slot does NOT stage (one deduped alert); a drafted caption is non-empty, has
exactly one ask, and passes copy_gate."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import copy_gate, podcast_caption as cap
from agent import podcast_library_builder as builder
from agent.publish_guard import ask_families
from tests.podcast_fakes import (FakeDrive, FakeStore, FakeZernio,
                                 NOTES_DOC_TEXT, make_asset)

ACCT = SimpleNamespace(key="lasso_ig", platform="instagram")

# A feed grounding entry for episode 140 (title + description), the shape
# podcast_feed_notes.episode_map returns.
FEED_140 = {140: {"title": "Episode 140: The Front Desk Follow Up That Tripled Referrals",
                  "description": (
                      "Casey walks through the exact follow up cadence her front "
                      "desk uses for every new lead. Referral asks moved from the "
                      "front desk to the coaches and referrals tripled in one "
                      "quarter. The team stopped discounting and raised close "
                      "rates by fixing the tour script instead."),
                  "pubdate": "Mon, 01 Jan 2026 00:00:00 GMT"}}
NO_FEED = {}  # feed grounds nothing (offline / episode not in feed)


def _probe_ok(path):
    return {"duration_sec": 42.0, "width": 1080, "height": 1920}


def test_no_source_no_stage_one_alert(monkeypatch):
    # Note-less clip AND no feed entry for its episode: not groundable at all.
    alerts = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: alerts.append(m))
    store = FakeStore([make_asset(notes_doc_id=None)])
    drive = FakeDrive()
    d = builder.build_podcast_clip_draft(ACCT, "2026-09-03", store=store,
                                         drive=drive, zernio_client=FakeZernio(),
                                         probe_fn=_probe_ok, feed_map=NO_FEED)
    assert d is None
    assert store.assets["clip140s1"]["used_count"] == 0  # never stamped
    # The whole postable pool is un-groundable, so pick_clip returns None and the
    # ONE alert fired is the deduped pool-empty alert (nothing reposted).
    assert len(alerts) == 1
    assert "pool empty" in alerts[0]


def test_feed_grounds_when_no_drive_doc(monkeypatch):
    # Note-less clip, but the RSS feed HAS its episode: it stages off the feed.
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: None)
    store = FakeStore([make_asset(notes_doc_id=None)])
    drive = FakeDrive()  # no Docs at all
    d = builder.build_podcast_clip_draft(ACCT, "2026-09-03", store=store,
                                         drive=drive, zernio_client=FakeZernio(),
                                         probe_fn=_probe_ok, feed_map=FEED_140)
    assert d is not None
    assert "140" in d.caption
    # hook is the feed title (episode prefix stripped by parse_notes)
    assert d.caption.splitlines()[0].startswith("The Front Desk Follow Up")
    assert store.assets["clip140s1"]["used_count"] == 1  # staged + stamped
    assert any(f.startswith("rss_feed:episode_140") for f in d.source_fragments)
    assert not any(f.startswith("drive_doc:") for f in d.source_fragments)


def test_feed_preferred_when_both_exist(monkeypatch):
    # Both feed and Doc ground the episode: the feed leads (its title is the
    # hook) and both are recorded as sources.
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: None)
    store = FakeStore([make_asset(notes_doc_id="doc140")])
    drive = FakeDrive(docs={"doc140": NOTES_DOC_TEXT})
    d = builder.build_podcast_clip_draft(ACCT, "2026-09-03", store=store,
                                         drive=drive, zernio_client=FakeZernio(),
                                         probe_fn=_probe_ok, feed_map=FEED_140)
    assert d is not None
    # feed title leads the caption (accurate "what this episode is about"),
    # NOT the Drive Doc's title — proving the feed is preferred when both exist.
    assert d.caption.splitlines()[0].startswith("The Front Desk Follow Up")
    frags = d.source_fragments
    assert any(f.startswith("rss_feed:episode_140") for f in frags)
    assert any(f == "drive_doc:doc140" for f in frags)


def test_doc_grounds_when_feed_lacks_episode(monkeypatch):
    # Feed does not carry the episode; the Drive Doc still grounds it (fallback).
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: None)
    store = FakeStore([make_asset(notes_doc_id="doc140")])
    drive = FakeDrive(docs={"doc140": NOTES_DOC_TEXT})
    d = builder.build_podcast_clip_draft(ACCT, "2026-09-03", store=store,
                                         drive=drive, zernio_client=FakeZernio(),
                                         probe_fn=_probe_ok, feed_map=NO_FEED)
    assert d is not None
    assert d.caption.splitlines()[0].startswith("How A Small Town Gym")
    assert any(f == "drive_doc:doc140" for f in d.source_fragments)


def test_empty_doc_and_empty_feed_no_stage(monkeypatch):
    # Doc exports empty AND the feed lacks the episode: not groundable, no stage.
    alerts = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: alerts.append(m))
    store = FakeStore([make_asset(notes_doc_id="doc140")])
    drive = FakeDrive(docs={"doc140": "   \n  "})  # exports empty
    d = builder.build_podcast_clip_draft(ACCT, "2026-09-03", store=store,
                                         drive=drive, zernio_client=FakeZernio(),
                                         probe_fn=_probe_ok, feed_map=NO_FEED)
    assert d is None
    assert store.assets["clip140s1"]["used_count"] == 0
    # The one groundable-looking clip has no usable source: one no-source alert
    # for the episode, then (its episode excluded) the deduped pool-empty alert.
    assert any("neither an RSS feed" in a for a in alerts)
    assert any("pool empty" in a for a in alerts)


def test_stray_noteless_pick_stages_next_groundable(monkeypatch):
    # The builder's belt-and-suspenders rail: even if the selector is forced to
    # yield a note-less, feed-less clip first (require_notes bypassed), the slot
    # tries the NEXT groundable clip instead of dying.
    from agent import ops_alerts, podcast_selector as sel
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: None)
    store = FakeStore([
        # id sorts first (used_count/last_used tie -> id tiebreak) so pick_clip
        # serves this un-groundable clip FIRST when its filter is bypassed.
        make_asset(fid="aaa_ungr", episode=99, clip_index=1,
                   title="GMMS-099-S1.mp4", notes_doc_id=None),   # no source
        make_asset(fid="good", episode=140, clip_index=1, notes_doc_id="doc140"),
    ])
    drive = FakeDrive(docs={"doc140": NOTES_DOC_TEXT})

    # Force pick_clip to serve the un-groundable clip first, then the good one,
    # by disabling the selector's own groundable filter (require_notes=False).
    real_pick = sel.pick_clip

    def pick_no_filter(*a, **kw):
        kw["require_notes"] = False
        return real_pick(*a, **kw)

    monkeypatch.setattr(sel, "pick_clip", pick_no_filter)
    d = builder.build_podcast_clip_draft(ACCT, "2026-09-03", store=store,
                                         drive=drive, zernio_client=FakeZernio(),
                                         probe_fn=_probe_ok, feed_map=NO_FEED)
    assert d is not None
    assert "140" in d.caption
    assert store.assets["good"]["used_count"] == 1
    assert store.assets["aaa_ungr"]["used_count"] == 0  # the stray never staged


def test_caption_grounded_one_ask_copy_gate_clean():
    caption, meta = cap.draft_caption(140, NOTES_DOC_TEXT, gym_id="lasso",
                                      allowlist_fn=lambda g: [])
    assert caption
    assert 150 <= len(caption) <= 500
    assert copy_gate.violations(caption) == []
    assert len(ask_families(caption)) == 1
    # hook first line = the episode's real title, not boilerplate
    assert caption.splitlines()[0].startswith("How A Small Town Gym")
    # grounded: a real claim from the doc made it in
    assert any(c in caption for c in meta["claims"])
    # episode number is carried
    assert "140" in caption


def test_caption_scrubs_dashes_from_doc_text():
    dashed = NOTES_DOC_TEXT.replace(
        "The gym rebuilt its intro offer",
        "The gym rebuilt its intro offer — completely —")
    caption, _ = cap.draft_caption(140, dashed, gym_id="lasso",
                                   allowlist_fn=lambda g: [])
    assert caption
    assert copy_gate.violations(caption) == []
    for ch in "‐‑‒–—―−":
        assert ch not in caption


def test_empty_or_thin_notes_return_none():
    assert cap.draft_caption(140, "")[0] is None
    assert cap.draft_caption(140, "   \n ")[0] is None
    assert cap.draft_caption(140, "GMMS 140: A Title\nshort")[0] is None


def test_guest_handle_only_when_allowlisted():
    # Handle in the doc but NOT allowlisted -> never tagged (no guessing).
    caption, meta = cap.draft_caption(140, NOTES_DOC_TEXT, gym_id="lasso",
                                      allowlist_fn=lambda g: [])
    assert "@caseyexample" not in caption
    assert meta["tagged_handle"] == ""
    # Handle in the doc AND on the allowlist -> tagged.
    caption, meta = cap.draft_caption(140, NOTES_DOC_TEXT, gym_id="lasso",
                                      allowlist_fn=lambda g: ["caseyexample"])
    assert "@caseyexample" in caption
    assert meta["tagged_handle"] == "caseyexample"
    assert len(ask_families(caption)) == 1  # the tag never adds an ask
    assert copy_gate.violations(caption) == []


def test_allowlist_lookup_failure_fails_closed():
    def boom(gym_id):
        raise RuntimeError("supabase down")
    caption, meta = cap.draft_caption(140, NOTES_DOC_TEXT, gym_id="lasso",
                                      allowlist_fn=boom)
    assert caption  # the caption still drafts
    assert "@" not in caption.replace("@caseyexample", "")  # and tags nothing
    assert meta["tagged_handle"] == ""


def test_claims_never_smuggle_a_second_ask():
    doc = NOTES_DOC_TEXT + "\n- Sign up today and DM us for the template pack\n"
    caption, _ = cap.draft_caption(140, doc, gym_id="lasso",
                                   allowlist_fn=lambda g: [])
    assert caption
    assert len(ask_families(caption)) == 1
