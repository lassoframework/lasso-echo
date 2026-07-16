"""
Pixel fabrication gate + stat-slab retirement.

The gate must BLOCK a card whose RENDERED text carries a stat with no approved
receipt (the "80% more conversions" slab class) and NAME the number, while a card
built from an approved receipt renders clean. The stat-slab layout must be retired
brand wide. Fully offline: tmp sidecars, injected readers, explicit approved lists.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import pixel_gate, creative_studio, ocr_check  # noqa: E402


# The approved receipts used across the pure-function tests (mirrors the real
# verified_stats USE lines: the 5-minute stat is approved, "80% more" is not).
APPROVED = [
    "Contact a new lead within 5 minutes and you can lift conversions up to 80 percent.",
    "71.9% booked vs an 18.5% industry average.",
    "The benchmark is 60 percent show rate on cold traffic.",
]


# ---- the claim scan -----------------------------------------------------------
def test_unapproved_slab_stat_is_flagged():
    bad = "80% more conversions"
    assert pixel_gate.offending_numbers(bad, APPROVED) == ["80%"]
    assert not pixel_gate.is_clean(bad, APPROVED)


def test_approved_stat_wording_is_clean():
    good = "Contact a new lead within 5 minutes and you can lift conversions up to 80 percent."
    assert pixel_gate.offending_numbers(good, APPROVED) == []
    assert pixel_gate.is_clean(good, APPROVED)


def test_non_stat_headline_is_clean():
    assert pixel_gate.is_clean("Built by gym owners, for gym owners.", APPROVED)
    assert pixel_gate.offending_numbers("Every lead gets a follow up.", APPROVED) == []


def test_approved_719_passes_unapproved_number_fails():
    assert pixel_gate.is_clean("71.9% booked vs an 18.5% industry average.", APPROVED)
    assert pixel_gate.offending_numbers("We tripled revenue 300%.", APPROVED) == ["300%"]


# ---- recorded rendered text + gate_creative -----------------------------------
def _card(tmp_path, name, rendered_text=None, note=""):
    png = tmp_path / (name + ".png")
    png.write_bytes(b"\x89PNG" + b"0" * 4096)
    if rendered_text is not None:
        (tmp_path / (name + ".json")).write_text(
            json.dumps({"rendered_text": rendered_text}))

    class _C:
        path = str(png)
        client_note = note
    return _C()


def test_gate_creative_blocks_recorded_bad_stat(tmp_path, monkeypatch):
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    c = _card(tmp_path, "slab", rendered_text="80% more conversions")
    ok, reason = pixel_gate.gate_creative(c)
    assert ok is False
    assert "80%" in reason


def test_gate_creative_passes_recorded_approved_stat(tmp_path, monkeypatch):
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    c = _card(tmp_path, "clean", rendered_text="71.9% booked vs an 18.5% industry average.")
    ok, reason = pixel_gate.gate_creative(c)
    assert ok is True
    assert reason == ""


def test_gate_creative_unverifiable_passes_when_studio_off(tmp_path, monkeypatch):
    # studio disarmed: no vision path to fail closed on, so an un-scanned image
    # falls back to the deterministic note check (dev / non-OCR deployments work).
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    monkeypatch.setattr(pixel_gate, "_default_reader", lambda: None)
    from agent import config
    monkeypatch.setattr(config, "creative_studio_enabled", lambda: False)
    c = _card(tmp_path, "norecord", rendered_text=None)  # no sidecar, no reader
    ok, reason = pixel_gate.gate_creative(c)
    assert ok is True


def test_gate_creative_fails_closed_on_unreadable_image(tmp_path, monkeypatch):
    # studio armed but the read cannot run: a card WITH rendered pixels blocks,
    # never passes as 'unverifiable'.
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    monkeypatch.setattr(pixel_gate, "_default_reader", lambda: None)
    from agent import config
    monkeypatch.setattr(config, "creative_studio_enabled", lambda: True)
    c = _card(tmp_path, "unreadable", rendered_text=None)  # image present, no reader
    ok, reason = pixel_gate.gate_creative(c)
    assert ok is False
    assert "could not verify" in reason


def test_gate_creative_forced_verification_blocks_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    monkeypatch.setattr(pixel_gate, "_default_reader", lambda: None)
    c = _card(tmp_path, "forced", rendered_text=None)
    ok, reason = pixel_gate.gate_creative(c, require_verification=True)
    assert ok is False and "could not verify" in reason


def test_read_finding_no_text_records_exempt(tmp_path, monkeypatch):
    # a successful read that finds NO text (pure photo) records the exempt sentinel
    # and passes; a later look needs no reader.
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    monkeypatch.setattr(pixel_gate, "_default_reader", lambda: (lambda _b: ""))
    c = _card(tmp_path, "photo", rendered_text=None)
    ok, reason = pixel_gate.gate_creative(c, require_verification=True)
    assert ok is True  # scanned, no text: nothing to fabricate
    # recorded as scanned (empty), so it never re-reads and never blocks
    monkeypatch.setattr(pixel_gate, "_default_reader", lambda: None)
    assert pixel_gate.recorded_rendered_text(c.path) == ""
    ok2, _ = pixel_gate.gate_creative(c, require_verification=True)
    assert ok2 is True


def test_video_creative_is_exempt(tmp_path, monkeypatch):
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    monkeypatch.setattr(pixel_gate, "_default_reader", lambda: None)
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"0" * 100)

    class _V:
        path = str(vid)
        client_note = ""
    ok, reason = pixel_gate.gate_creative(_V(), require_verification=True)
    assert ok is True  # no still pixels to OCR; documented gap, not a silent pass-as-clean


def test_ocr_model_is_not_the_generation_model():
    from agent import config
    assert config.OCR_MODEL != config.NANO_MODEL
    assert "image" not in config.OCR_MODEL  # a vision TEXT model, not a *-image gen model


def test_ocr_belt_records_read_then_gates(tmp_path, monkeypatch):
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    monkeypatch.setattr(pixel_gate, "_default_reader",
                        lambda: (lambda _b: "80% more conversions"))
    c = _card(tmp_path, "drift", rendered_text=None)
    ok, reason = pixel_gate.gate_creative(c)
    assert ok is False and "80%" in reason
    # the read was recorded, so the next look is free (no reader needed)
    monkeypatch.setattr(pixel_gate, "_default_reader", lambda: None)
    assert pixel_gate.recorded_rendered_text(c.path) == "80% more conversions"


# ---- OCR block belt -----------------------------------------------------------
def _png(tmp_path, name="img"):
    p = tmp_path / (name + ".png")
    p.write_bytes(b"\x89PNG" + b"0" * 4096)
    return str(p)


def test_headline_block_flags_drifted_number(tmp_path, monkeypatch):
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    reader = lambda _b: "80% more conversions"
    reason = ocr_check.headline_block(
        _png(tmp_path), intended_headline="The gym that answers first wins the member.",
        reader=reader)
    assert reason and "80%" in reason


def test_headline_block_none_when_number_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(pixel_gate, "_approved_claims", lambda: APPROVED)
    reader = lambda _b: "71.9% booked"
    reason = ocr_check.headline_block(
        _png(tmp_path), intended_headline="71.9% booked", reader=reader)
    assert reason is None


def test_headline_block_none_without_reader(tmp_path):
    assert ocr_check.headline_block(_png(tmp_path), "anything", reader=None) is None


# ---- stat-slab retirement -----------------------------------------------------
def test_stat_hero_removed_from_layouts():
    assert "stat_hero" not in creative_studio.LAYOUTS


def test_variant_block_remaps_retired_stat_hero(capsys):
    block = creative_studio.variant_block("cream", "stat_hero")
    # remapped to chart, never raises, and carries the no-slab law
    assert "CHART" in block or "chart" in block.lower()
    assert "stat slab" in block.lower()


def test_variant_block_still_raises_on_unknown_layout():
    with pytest.raises(ValueError):
        creative_studio.variant_block("cream", "not_a_real_layout")


def test_no_stat_slab_law_in_default_prompt():
    prompt = creative_studio.build_prompt("A clear headline", ["one approved fact"])
    assert "stat slab" in prompt.lower()


def test_number_card_style_is_not_a_slab():
    style = creative_studio.NUMBER_CARD_STYLE.lower()
    assert "colossal" not in style
    assert "retired" in style


# ---- retro scan ---------------------------------------------------------------
class _FakeStore:
    def __init__(self, drafts):
        self._drafts = drafts
        self.put_calls = []

    def list_pending(self):
        return list(self._drafts)

    def put(self, d):
        self.put_calls.append(d)


class _FakePoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)


def test_fabrication_scan_auto_blocks_bad_card(tmp_path, monkeypatch):
    from agent import fabrication_scan, rotation, ops_alerts
    from agent.drafter import Draft, DraftStatus

    monkeypatch.setattr(rotation, "_approved_claims", lambda: APPROVED)
    monkeypatch.setattr(ops_alerts, "alert", lambda *a, **k: None)

    bad = _card(tmp_path, "queued_slab", rendered_text="80% more conversions")
    draft = Draft(draft_id="plan_lasso_fb_x", account_key="lasso_fb",
                  platform="facebook_page", caption="c", hashtags=[],
                  creative_path=bad.path, creative_public_url="",
                  scheduled_for="2026-07-20", status=DraftStatus.PENDING,
                  day_key="2026-07-20")
    store = _FakeStore([draft])
    report = fabrication_scan.scan(store=store, poster=_FakePoster(), auto_block=True)
    assert len(report["blocked"]) == 1
    assert report["blocked"][0]["kind"] == "stat"
    assert "80%" in report["blocked"][0]["reason"]
    assert store.put_calls and store.put_calls[0].status == DraftStatus.BLOCKED


def test_fabrication_scan_dry_run_does_not_block(tmp_path, monkeypatch):
    from agent import fabrication_scan, rotation, ops_alerts
    from agent.drafter import Draft, DraftStatus

    monkeypatch.setattr(rotation, "_approved_claims", lambda: APPROVED)
    monkeypatch.setattr(ops_alerts, "alert", lambda *a, **k: None)

    bad = _card(tmp_path, "queued_slab2", rendered_text="80% more conversions")
    draft = Draft(draft_id="plan_lasso_fb_y", account_key="lasso_fb",
                  platform="facebook_page", caption="c", hashtags=[],
                  creative_path=bad.path, creative_public_url="",
                  scheduled_for="2026-07-20", status=DraftStatus.PENDING,
                  day_key="2026-07-20")
    store = _FakeStore([draft])
    report = fabrication_scan.scan(store=store, poster=_FakePoster(), auto_block=False)
    assert len(report["blocked"]) == 1
    assert store.put_calls == []  # dry run: nothing flipped
