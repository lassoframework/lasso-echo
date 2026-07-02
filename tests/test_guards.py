"""
Quality and cost guard tests (OCR headline check + Gemini spend cap). Offline:
fake readers, fake nano clients, zero spend. Asserts: a mismatch flags a warning
on the card (never blocks), a match passes clean, the cap trips at N and falls
back to library-only with ONE alert, counters key by day, both guards fully
inert while OFF.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, db, ocr_check, ops_alerts  # noqa: E402


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


# ---- OCR headline check -----------------------------------------------------------
def _img(tmp_path):
    p = tmp_path / "card.png"
    p.write_bytes(b"\x89PNG fake")
    return str(p)


def test_ocr_inert_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_OCR_CHECK_ENABLED", raising=False)
    exploding = lambda b: (_ for _ in ()).throw(AssertionError("read during OFF"))
    assert ocr_check.headline_warning(_img(tmp_path), "H", reader=exploding) is None


def test_ocr_match_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OCR_CHECK_ENABLED", "true")
    reader = lambda b: "We run your ads. You run your gym."
    out = ocr_check.headline_warning(_img(tmp_path),
                                     "We run your ads. You run your gym.",
                                     reader=reader)
    assert out is None


def test_ocr_mismatch_flags_never_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OCR_CHECK_ENABLED", "true")
    reader = lambda b: "COMPLETELY DIFFERENT SLOGAN ON THE CARD"
    out = ocr_check.headline_warning(_img(tmp_path), "We train your sales team.",
                                     reader=reader)
    assert out is not None and "HEADLINE CHECK" in out
    # a reader crash never blocks either
    exploding = lambda b: (_ for _ in ()).throw(RuntimeError("vision down"))
    assert ocr_check.headline_warning(_img(tmp_path), "H", reader=exploding) is None


def test_warning_renders_on_card():
    from agent.drafter import Draft, DraftStatus
    from agent.slack_surface import build_card_blocks
    d = Draft(draft_id="d", account_key="lasso_ig", platform="instagram",
              caption="c", hashtags=[], creative_path="/x.png",
              creative_public_url="", scheduled_for="t",
              status=DraftStatus.PENDING, warnings=["HEADLINE CHECK: mismatch"])
    blocks = str(build_card_blocks(d))
    assert "HEADLINE CHECK: mismatch" in blocks
    assert "Approve" in blocks                      # buttons intact, nothing blocked


# ---- Gemini spend cap ----------------------------------------------------------------
class FakeNano:
    def __init__(self):
        self.calls = 0

    def generate_image(self, prompt, model):
        self.calls += 1
        return b"\x89PNG\r\n\x1a\nFAKE"


def _arm_studio(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))


def test_cap_trips_at_n_and_falls_back_with_one_alert(monkeypatch, tmp_path):
    _arm_studio(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_SPEND_CAP_ENABLED", "true")
    monkeypatch.setenv("AGENT_GEMINI_DAILY_CAP", "2")
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    nano = FakeNano()
    outs = [creative_studio.generate("H", ["fact"], client=nano,
                                     out_path=str(tmp_path / f"c{i}.png"))
            for i in range(4)]
    assert outs[0] is not None and outs[1] is not None
    assert outs[2] is None and outs[3] is None       # capped: library-only fallback
    assert nano.calls == 2                            # zero spend past the cap
    cap_alerts = [n for n in rec.notices if "daily cap" in n]
    assert len(cap_alerts) == 1                       # exactly one alert


def test_counters_key_by_day():
    assert db.counter_bump("gemini_calls", "2026-07-06") == 1
    assert db.counter_bump("gemini_calls", "2026-07-06") == 2
    assert db.counter_get("gemini_calls", "2026-07-07") == 0   # fresh day, fresh count


def test_cap_inert_when_off(monkeypatch, tmp_path):
    _arm_studio(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_SPEND_CAP_ENABLED", raising=False)
    nano = FakeNano()
    for i in range(3):
        assert creative_studio.generate("H", ["fact"], client=nano,
                                        out_path=str(tmp_path / f"o{i}.png")) is not None
    assert nano.calls == 3                            # no counting, no capping
    from datetime import date
    assert db.counter_get("gemini_calls", date.today().isoformat()) == 0
