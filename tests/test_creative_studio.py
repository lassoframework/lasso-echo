"""
Creative studio tests: the flag gates the API call, empty facts block (no
fabrication), and the prompt carries the approved facts, the brand palette, and
the no-dash rule (with dashes scrubbed). No network — a fake client only.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio  # noqa: E402


# --- fakes modelling the genai.Client -> models.generate_content -> inline image path
class _FakeInlineData:
    def __init__(self, data):
        self.data = data


class _FakePart:
    def __init__(self, inline_data=None):
        self.inline_data = _FakeInlineData(inline_data) if inline_data is not None else None


class _FakeResp:
    def __init__(self, parts):
        content = type("Content", (), {"parts": parts})()
        self.candidates = [type("Candidate", (), {"content": content})()]


class _FakeModels:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []
        self.kwargs_calls = []

    def generate_content(self, model, contents, **kwargs):
        self.calls.append((model, contents))
        self.kwargs_calls.append(kwargs)
        return self._resp


class _FakeGenaiClient:
    def __init__(self, resp):
        self.models = _FakeModels(resp)


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
    # the full locked V3 palette must be enforced, not just navy
    for hexcode in ("#121E3C", "#FF0000", "#5EB9E6", "#FAF6F0"):
        assert hexcode in res["prompt"], hexcode
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


# ---- the Gemini client calls generate_content and extracts inline image bytes ----
def test_gemini_client_uses_generate_content_and_extracts_inline_bytes():
    # A text part first, then the image part -> the first inline_data wins.
    resp = _FakeResp([_FakePart(inline_data=None), _FakePart(inline_data=b"IMGBYTES")])
    genai_client = _FakeGenaiClient(resp)
    client = creative_studio._GeminiImageClient("fake-key", genai_client=genai_client)

    out = client.generate_image("a prompt", "gemini-3-pro-image")
    assert out == b"IMGBYTES"
    # called generate_content (not generate_images/predict) with model + prompt
    assert genai_client.models.calls == [("gemini-3-pro-image", "a prompt")]


def test_gemini_client_raises_when_no_inline_image_part():
    resp = _FakeResp([_FakePart(inline_data=None)])  # text only, no image
    client = creative_studio._GeminiImageClient("fake-key",
                                                genai_client=_FakeGenaiClient(resp))
    with pytest.raises(ValueError, match="no image returned from Gemini"):
        client.generate_image("a prompt", "gemini-3-pro-image")


# ---- consistent house style, subject varies by pillar, headline-only, palette kept --
def test_prompt_locks_house_style_and_varies_subject():
    headline = "Every lead, every post, every result. One screen."
    body = "Your leads, content, and reporting live in one place."
    p = creative_studio.build_prompt(headline, [body])
    low = p.lower()

    # a locked, consistent house look (illustrated-diagram concept)
    assert "house style" in low
    assert "minimal" in low
    assert "illustrated diagram" in low
    assert "consistent stroke weight" in low
    assert "negative space" in low
    assert "not a busy poster" in low
    # 4:5 PORTRAIT canvas for IG/FB feed
    assert "4:5" in p
    assert "1080x1350" in p
    assert "portrait" in low
    assert "taller than wide" in low
    assert "headline at the top" in low     # flow archetype default: headline up top
    # the SUBJECT varies by pillar; do NOT force a monitor/dashboard every time
    assert "subject varies by pillar" in low
    assert "do not default to a computer, monitor, or dashboard" in low
    # only the headline is rendered; body lines are context, not on-image text
    assert "only text to render on the image" in low
    assert "do not render this text on the image" in low
    # the single headline is the approved hook (scrubbed of dashes)
    assert headline in p
    # the palette is retained (all four locked V3 colors)
    for hexcode in ("#121E3C", "#FF0000", "#5EB9E6", "#FAF6F0"):
        assert hexcode in p


def test_image_aspect_is_config_tunable(monkeypatch):
    # default is 4:5 portrait; a config/env override retunes the prompt without a code edit
    assert "4:5" in creative_studio.build_prompt("H", ["a body line"])
    monkeypatch.setattr(config, "IMAGE_ASPECT", "1:1")
    monkeypatch.setattr(config, "IMAGE_PIXELS", "1080x1080")
    p = creative_studio.build_prompt("H", ["a body line"])
    assert "1:1" in p and "1080x1080" in p


# ---- validate_generation_models: startup guard for model-ID 404s ---------------

class _FakeModel:
    def __init__(self, name):
        self.name = name


class _FakeListClient:
    def __init__(self, names):
        self._models = [_FakeModel(n) for n in names]

    class _M:
        def __init__(self, models):
            self._models = models

        def list(self):
            return iter(self._models)

    def __init__(self, names):
        self._names = names
        self.models = _FakeListClient._M([_FakeModel(n) for n in names])


def _patch_genai(monkeypatch, live_model_names):
    """Monkeypatch google.genai so validate_generation_models uses our fake list."""
    import types as _types_mod
    fake_genai = _types_mod.SimpleNamespace(
        Client=lambda api_key: _FakeListClient(live_model_names)
    )
    import sys
    fake_google = _types_mod.SimpleNamespace(genai=fake_genai)
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)


def test_validate_models_silent_when_both_present(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_API_KEY", "fake-key")
    monkeypatch.setattr(config, "NANO_MODEL", "gemini-3-pro-image")
    monkeypatch.setattr(config, "NANO_MODEL_FLASH", "gemini-3.1-flash-image")
    _patch_genai(monkeypatch, [
        "models/gemini-3-pro-image",
        "models/gemini-3.1-flash-image",
        "models/gemini-3.1-flash-lite-image",
    ])
    creative_studio.validate_generation_models()
    out = capsys.readouterr().out
    assert "OK" in out
    assert "STARTUP" not in out


def test_validate_models_alerts_on_bad_model(monkeypatch):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_API_KEY", "fake-key")
    monkeypatch.setattr(config, "NANO_MODEL", "gemini-RETIRED-pro-image")
    monkeypatch.setattr(config, "NANO_MODEL_FLASH", "gemini-3.1-flash-image")
    _patch_genai(monkeypatch, [
        "models/gemini-3-pro-image",         # NOT the same as RETIRED
        "models/gemini-3.1-flash-image",
    ])
    alerts = []
    monkeypatch.setattr(
        "agent.ops_alerts.alert",
        lambda msg, **kw: alerts.append(msg),
    )
    creative_studio.validate_generation_models()
    assert len(alerts) == 1
    assert "AGENT_NANO_MODEL" in alerts[0]
    assert "gemini-RETIRED-pro-image" in alerts[0]
    # should list available image models to help Blake pick the right one
    assert "gemini-3-pro-image" in alerts[0]


def test_validate_models_skips_without_key(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.delenv("AGENT_NANO_API_KEY", raising=False)
    creative_studio.validate_generation_models()
    assert capsys.readouterr().out == ""


def test_validate_models_skips_when_flag_off(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_NANO_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_NANO_API_KEY", "fake-key")
    creative_studio.validate_generation_models()
    assert capsys.readouterr().out == ""
