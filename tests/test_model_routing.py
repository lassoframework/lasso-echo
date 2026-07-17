"""
Model routing tests. Asserts that _route_model() returns Pro for all cards when
Flash is OFF (the default), routes text-heavy cards to Pro when Flash is ON, routes
text-light cards to Flash when Flash is ON, and that generate() includes model and
route in its return dict. Fully OFFLINE.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio  # noqa: E402


class CaptureNano:
    def __init__(self):
        self.calls = []

    def generate_image(self, prompt, model):
        self.calls.append({"prompt": prompt, "model": model})
        return b"\x89PNG\r\n\x1a\nFAKE"


# ---- _route_model direct tests ---------------------------------------------------

def test_flash_off_always_routes_pro(monkeypatch):
    monkeypatch.delenv("AGENT_NANO_FLASH_ENABLED", raising=False)
    model, route = creative_studio._route_model("Leads go cold in minutes.", ["Answer fast."])
    assert model == config.NANO_MODEL
    assert "pro:all" in route
    assert config.NANO_MODEL in route


def test_flash_off_text_light_still_routes_pro(monkeypatch):
    monkeypatch.delenv("AGENT_NANO_FLASH_ENABLED", raising=False)
    model, route = creative_studio._route_model("", [])
    assert model == config.NANO_MODEL
    assert "pro:all" in route


def test_flash_on_headline_present_routes_pro(monkeypatch):
    monkeypatch.setenv("AGENT_NANO_FLASH_ENABLED", "true")
    model, route = creative_studio._route_model("Speed to lead wins.", [])
    assert model == config.NANO_MODEL
    assert "pro:rendered-text" in route


def test_flash_on_facts_with_digits_routes_pro(monkeypatch):
    monkeypatch.setenv("AGENT_NANO_FLASH_ENABLED", "true")
    model, route = creative_studio._route_model("", ["Response within 5 minutes triples bookings."])
    assert model == config.NANO_MODEL
    assert "pro:rendered-text" in route


def test_flash_on_no_text_routes_flash(monkeypatch):
    monkeypatch.setenv("AGENT_NANO_FLASH_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_MODEL_FLASH", "gemini-3.1-flash-image")
    model, route = creative_studio._route_model("", ["a gym scene with no numbers"])
    assert model == config.NANO_MODEL_FLASH
    assert "flash:photographic" in route


# ---- generate() return dict includes model and route -----------------------------

def test_generate_return_dict_has_model_and_route(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.delenv("AGENT_NANO_FLASH_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_STYLE_GATE_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_SPEND_CAP_ENABLED", raising=False)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))

    cap = CaptureNano()
    result = creative_studio.generate(
        "Speed to lead wins.", ["Answer fast and book more."],
        client=cap,
    )
    assert result is not None
    assert "model" in result
    assert "route" in result
    assert result["model"] == config.NANO_MODEL
    assert "pro:all" in result["route"]
    # the actual API call used the same model
    assert len(cap.calls) == 1
    assert cap.calls[0]["model"] == config.NANO_MODEL
