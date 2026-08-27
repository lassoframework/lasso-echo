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


def _probe_ok(path):
    return {"duration_sec": 42.0, "width": 1080, "height": 1920}


def test_missing_notes_doc_no_stage_one_alert(monkeypatch):
    alerts = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: alerts.append(m))
    store = FakeStore([make_asset(notes_doc_id=None)])
    drive = FakeDrive()
    d = builder.build_podcast_clip_draft(ACCT, "2026-09-03", store=store,
                                         drive=drive, zernio_client=FakeZernio(),
                                         probe_fn=_probe_ok)
    assert d is None
    assert store.assets["clip140s1"]["used_count"] == 0  # never stamped
    # deduped: a second run does not alert again
    d = builder.build_podcast_clip_draft(ACCT, "2026-09-04", store=store,
                                         drive=drive, zernio_client=FakeZernio(),
                                         probe_fn=_probe_ok)
    assert d is None
    assert len(alerts) == 1
    assert "140" in alerts[0]


def test_empty_notes_export_no_stage_one_alert(monkeypatch):
    alerts = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: alerts.append(m))
    store = FakeStore([make_asset()])
    drive = FakeDrive(docs={"doc140": "   \n  "})  # exports empty
    d = builder.build_podcast_clip_draft(ACCT, "2026-09-03", store=store,
                                         drive=drive, zernio_client=FakeZernio(),
                                         probe_fn=_probe_ok)
    assert d is None
    assert store.assets["clip140s1"]["used_count"] == 0
    assert len(alerts) == 1


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
