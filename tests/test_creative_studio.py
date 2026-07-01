"""
Creative studio tests: the flag gates the API call, empty facts block (no
fabrication), and the prompt carries the approved facts, the brand palette, and
the no-dash rule (with dashes scrubbed). No network — a fake client only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.calls = 0
        self.last_prompt = None
        self.last_model = None

    def generate_image(self, prompt, model):
        self.calls += 1
        self.last_prompt = prompt
        self.last_model = model
        return b"\x89PNG\r\n\x1a\nFAKEBYTES"


class _ExplodingClient:
    def generate_image(self, prompt, model):
        raise AssertionError("API call made while it should not have been!")


# ---- 1. flag OFF -> no API call ---------------------------------------------
def test_flag_off_makes_no_api_call(monkeypatch):
    monkeypatch.delenv("AGENT_NANO_ENABLED", raising=False)  # OFF (default)
    res = creative_studio.generate(
        "Speed to lead", ["Leads answered in 5 min close 3x more"],
        client=_ExplodingClient())
    assert res is None


# ---- 2. empty facts blocks (no fabrication) ---------------------------------
def test_empty_facts_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    out = tmp_path / "x.png"
    assert creative_studio.generate("Headline", [], client=_ExplodingClient(),
                                    out_path=str(out)) is None
    assert creative_studio.generate("Headline", None, client=_ExplodingClient(),
                                    out_path=str(out)) is None
    assert not out.exists()


# ---- 3. approved facts reach the prompt; the client is called with the model --
def test_approved_facts_reach_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    fake = _FakeClient()
    out = tmp_path / "info.png"
    fact = "Gyms using LASSO cut cost per lead by 40 percent"
    res = creative_studio.generate("Speed to lead wins", [fact],
                                   client=fake, out_path=str(out))
    assert res is not None
    assert fake.calls == 1
    assert fact in res["prompt"]
    assert fake.last_prompt == res["prompt"]
    assert fake.last_model == config.NANO_MODEL
    assert res["path"] == str(out) and out.exists()


# ---- 4. prompt carries the brand palette and the no-dash rule ---------------
def test_prompt_carries_palette_and_no_dash_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    fake = _FakeClient()
    out = tmp_path / "info.png"
    res = creative_studio.generate("H", ["A real approved fact"],
                                   client=fake, out_path=str(out))
    assert "#121E3C" in res["prompt"]                 # brand palette anchor
    assert "no em dashes" in res["prompt"].lower()    # no-dash rule
    assert "no en dashes" in res["prompt"].lower()


# ---- 5. dashes in approved text are scrubbed --------------------------------
def test_dashes_are_scrubbed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    fake = _FakeClient()
    out = tmp_path / "info.png"
    res = creative_studio.generate(
        "Signups up 3 — 5 times",
        ["Members grew 10 – 20 percent in a quarter"],
        client=fake, out_path=str(out))
    assert "—" not in res["prompt"]   # em dash gone
    assert "–" not in res["prompt"]   # en dash gone
