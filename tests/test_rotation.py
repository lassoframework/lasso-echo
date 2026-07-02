"""
Creative rotation + variety guard tests. Offline, adversarial. Asserts: fully
inert with the flag OFF; no repeat inside the window (and eligible again after
it); consecutive days never share a pillar; an unapproved stat card is NEVER
selected even as the only fresh option (oldest approved fallback + one ops
alert instead); the served log persists across a simulated restart; the library
and the generated Nano card both actually rotate (Nano is one source among
several).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, ops_alerts, rotation  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


SOURCE_DOC = """# LASSO Now
## Pillars
- Speed To Lead
## Pillar copy bank
### Pillar: Speed To Lead
Hook: Leads go cold in minutes.
Body: Answer fast and win.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
"""


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _acct():
    return Account(key="lasso_ig", display_name="LASSO IG", platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y")


def _voice():
    return VoiceDoc(raw="x", hashtags=["#LASSOFramework"])


def _lib(tmp_path, cards):
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    for name, note in cards:
        (lib / name).write_bytes(b"img-" + name.encode())
        if note:
            (lib / (os.path.splitext(name)[0] + ".txt")).write_text(note, encoding="utf-8")
    return str(lib)


def _arm(monkeypatch, tmp_path, source_doc=None):
    monkeypatch.setenv("AGENT_ROTATION_ENABLED", "true")
    monkeypatch.setenv("AGENT_ROTATION_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(exist_ok=True)
    src = tmp_path / "lasso_now.md"
    src.write_text(source_doc if source_doc is not None else "", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)  # conservative gate


CLEAN = [("lasso_p1_story.jpg", "A member story in plain words."),
         ("lasso_p2_habits.jpg", "Habits beat motivation."),
         ("lasso_p3_flow.jpg", "Simple systems win."),
         ("lasso_p4_mindset.jpg", "Do the boring work.")]


# ---- inert when OFF -------------------------------------------------------------
def test_inert_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_ROTATION_ENABLED", raising=False)
    lib = _lib(tmp_path, CLEAN)
    assert rotation.build_rotated_draft(_acct(), "2026-07-06", _voice(), lib) is None


# ---- no repeat inside the window, eligible again after it -----------------------
def test_no_repeat_within_window(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    lib = _lib(tmp_path, CLEAN)
    kind, first = rotation.choose("lasso_ig", "2026-07-06", lib)
    assert kind == "library"
    rotation.record_served("lasso_ig", os.path.basename(first.path),
                           rotation.pillar_of(first.path), "2026-07-06")
    kind2, second = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert os.path.basename(second.path) != os.path.basename(first.path)

    # after the window the first creative is eligible again: with ONLY it in the
    # library and its serve 35 days old, it must be chosen (a recent serve of the
    # same key would force the thin-pool fallback path instead)
    solo_lib = _lib(tmp_path / "solo", [(os.path.basename(first.path), "A plain story.")])
    served = rotation.load_served()
    served["lasso_ig"] = [{"key": os.path.basename(first.path),
                           "pillar": "p9", "date": "2026-06-01"}]  # 35 days before
    rotation.save_served(served)
    kind3, third = rotation.choose("lasso_ig", "2026-07-06", solo_lib)
    assert kind3 == "library"
    assert os.path.basename(third.path) == os.path.basename(first.path)


def test_pillars_never_repeat_on_consecutive_days(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    lib = _lib(tmp_path, CLEAN)
    pillars = []
    for day in ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"):
        kind, creative = rotation.choose("lasso_ig", day, lib)
        assert kind == "library"
        p = rotation.pillar_of(creative.path)
        rotation.record_served("lasso_ig", os.path.basename(creative.path), p, day)
        pillars.append(p)
    for a, b in zip(pillars, pillars[1:]):
        assert a != b, f"same pillar two days running: {pillars}"


# ---- fabrication gate supreme (adversarial) --------------------------------------
def test_unapproved_stat_card_never_selected_even_as_only_fresh(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    rec = RecordingPoster()
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    lib = _lib(tmp_path, [
        ("lasso_p1_approved.jpg", "A plain approved story."),
        ("lasso_p2_statcard.jpg",
         "Contact leads in 5 minutes and lift conversions up to 80 percent."),
    ])
    # the approved card was served yesterday; the stat card is the ONLY fresh option
    rotation.record_served("lasso_ig", "lasso_p1_approved.jpg", "p1", "2026-07-06")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    # fallback to the oldest APPROVED creative, never the unapproved stat card
    assert kind == "library"
    assert os.path.basename(creative.path) == "lasso_p1_approved.jpg"
    alerts = [n for n in rec.notices if "thin" in n]
    assert len(alerts) == 1                      # one loud line about the thin pool
    # and across a whole week it still never picks the stat card
    for day in ("2026-07-08", "2026-07-09", "2026-07-10"):
        k, c = rotation.choose("lasso_ig", day, lib)
        assert c is None or os.path.basename(c.path) != "lasso_p2_statcard.jpg"


def test_cleared_claim_passes_gate_unit():
    claim = "Clients report a 28-45% increase in annual revenue within their first year."
    assert rotation.is_gate_clean("A note. " + claim, approved_claims=[claim]) is True
    assert rotation.is_gate_clean("We grew 300% overnight.", approved_claims=[claim]) is False
    assert rotation.is_gate_clean("No numbers here, just a story.", approved_claims=[]) is True


# ---- persistence across a simulated restart --------------------------------------
def test_served_log_persists_across_restart(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    rotation.record_served("lasso_ig", "lasso_p1_story.jpg", "p1", "2026-07-06")
    # a fresh process = a fresh read of the same /data file
    raw = json.loads((tmp_path / "state" / "rotation_served.json").read_text())
    assert raw["lasso_ig"][0]["key"] == "lasso_p1_story.jpg"
    lib = _lib(tmp_path, CLEAN)
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert os.path.basename(creative.path) != "lasso_p1_story.jpg"  # window enforced


# ---- the library and the Nano card BOTH rotate ------------------------------------
def test_nano_is_one_source_among_several(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, source_doc=SOURCE_DOC)
    lib = _lib(tmp_path, [("lasso_p1_story.jpg", "A plain approved story.")])
    kinds = []
    for day in ("2026-07-06", "2026-07-07"):
        kind, payload = rotation.choose("lasso_ig", day, lib)
        kinds.append(kind)
        if kind == "library":
            rotation.record_served("lasso_ig", os.path.basename(payload.path),
                                   rotation.pillar_of(payload.path), day)
        else:
            rotation.record_served("lasso_ig", "nano:sig", f"brain:{payload}", day)
    assert "library" in kinds and "generate" in kinds  # both sources cycle
