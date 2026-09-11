"""
Astra (gpt-image-2.5) default image engine + Gemini fallback.

Every test here is OFFLINE: the OpenAI Responses call is mocked through the
AstraImageEngine transport seam, and the Gemini rung uses the same injected fake
client the rest of the suite uses. Nothing reaches a network.

Covered:
  - routing: default engine, the IMAGE_ENGINE switch, Sunburst vs Flare, sizes
  - the Responses payload shape (model, input, image_generation tool)
  - image extraction from the tool output, including revised_prompt
  - a rejected model surfaces the provider's REAL response body
  - the fallback chain: Astra -> retry once with backoff -> Gemini -> needs human
  - "needs human" fires an ops alert AND writes an audit row (never silent)
  - cost logging, the per-day total, and the one-per-day over-budget alert
  - creative_studio wiring, with every existing gate unchanged
"""

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import astra_prompt, creative_studio, db, image_engine  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nASTRA-FAKE-BYTES-PADDED-SO-THE-BASE64-IS-REALISTICALLY-LONG"
PNG_B64 = base64.b64encode(PNG).decode()
GEM = b"\x89PNG\r\n\x1a\nGEMINI-FAKE-BYTES"


def _real_png_b64_sized(w, h, color=(11, 22, 33)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _astra_ok_body(revised="revised by astra"):
    return json.dumps({"output": [{
        "type": "image_generation_call",
        "result": PNG_B64,
        "revised_prompt": revised,
    }]})


class _FakeGemini:
    """The Gemini rung's injected client (same shape the rest of the suite uses)."""

    def __init__(self, image=GEM, fail=False):
        self.image = image
        self.fail = fail
        self.calls = []

    def generate_image(self, prompt, model):
        self.calls.append((prompt, model))
        if self.fail:
            raise RuntimeError("gemini is down")
        return self.image


class _Transport:
    """Records every Responses POST and replays scripted (status, body) pairs."""

    def __init__(self, *responses):
        self.responses = list(responses) or [(200, _astra_ok_body())]
        self.payloads = []

    def __call__(self, url, headers, payload):
        self.payloads.append(payload)
        idx = min(len(self.payloads) - 1, len(self.responses) - 1)
        return self.responses[idx]


@pytest.fixture(autouse=True)
def _fast_and_armed(monkeypatch):
    """Creative studio armed, no real backoff sleeping, warning latch reset."""
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setattr(creative_studio, "_RENDER_RETRY_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(image_engine, "ASTRA_RETRY_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(image_engine, "_missing_key_warned", False)
    monkeypatch.setattr(image_engine, "_unknown_engine_warned", False)


@pytest.fixture
def astra_key(monkeypatch):
    monkeypatch.setenv(image_engine.OPENAI_API_KEY_ENV, "sk-test-not-a-real-key")


def _patch_astra(monkeypatch, transport):
    monkeypatch.setattr(image_engine.AstraImageEngine, "_post",
                        lambda self, payload: transport(
                            image_engine.ASTRA_RESPONSES_URL, {}, payload))


# ---------------------------------------------------------------------------
# 1. Routing
# ---------------------------------------------------------------------------


def test_default_engine_is_astra(monkeypatch):
    monkeypatch.delenv("IMAGE_ENGINE", raising=False)
    monkeypatch.delenv("AGENT_IMAGE_ENGINE", raising=False)
    assert image_engine.engine_name() == "astra"


def test_image_engine_env_switches_to_gemini_only(monkeypatch, astra_key):
    monkeypatch.setenv("IMAGE_ENGINE", "gemini")
    chain = image_engine.engine_chain()
    assert [e.name for e in chain] == ["gemini"]


def test_astra_chain_keeps_gemini_as_the_fallback_rung(monkeypatch, astra_key):
    chain = image_engine.engine_chain()
    assert [e.name for e in chain] == ["astra", "gemini"]


def test_missing_openai_key_boots_gemini_with_one_warning(monkeypatch, capsys):
    monkeypatch.delenv(image_engine.OPENAI_API_KEY_ENV, raising=False)
    assert [e.name for e in image_engine.engine_chain()] == ["gemini"]
    assert [e.name for e in image_engine.engine_chain()] == ["gemini"]
    warnings = [ln for ln in capsys.readouterr().out.splitlines()
                if "WARNING" in ln and image_engine.OPENAI_API_KEY_ENV in ln]
    assert len(warnings) == 1, warnings


def test_boot_announces_the_effective_engine(monkeypatch, capsys, astra_key):
    assert image_engine.announce_boot() == "astra"
    assert "IMAGE_ENGINE=astra effective=astra" in capsys.readouterr().out


def test_boot_without_a_key_announces_the_gemini_demotion(monkeypatch, capsys):
    monkeypatch.delenv(image_engine.OPENAI_API_KEY_ENV, raising=False)
    assert image_engine.announce_boot() == "gemini"
    out = capsys.readouterr().out
    assert "IMAGE_ENGINE=astra effective=gemini" in out
    assert "WARNING" in out


def test_a_typo_in_image_engine_announces_itself(monkeypatch, capsys, astra_key):
    """A typo'd engine name silently demoting to Gemini is the exact silent
    failure this module exists to prevent, and the startup line must not lie."""
    monkeypatch.setenv("IMAGE_ENGINE", "openai")
    assert [e.name for e in image_engine.engine_chain()] == ["gemini"]
    assert image_engine.announce_boot() == "gemini"
    out = capsys.readouterr().out
    assert "IMAGE_ENGINE=openai effective=gemini" in out
    assert out.count("is not a known engine") == 1, "warn once, not per call"


def test_default_astra_model_ids_are_the_spec_ids(monkeypatch):
    for var in ("ASTRA_IMAGE_MODEL", "AGENT_ASTRA_IMAGE_MODEL",
                "ASTRA_IMAGE_MODEL_FLARE", "AGENT_ASTRA_IMAGE_MODEL_FLARE",
                "ASTRA_BRIEF_MODEL", "AGENT_ASTRA_BRIEF_MODEL"):
        monkeypatch.delenv(var, raising=False)
    assert image_engine.astra_image_model() == "gpt-image-2.5-sunburst"
    assert image_engine.astra_image_model_flare() == "gpt-image-2.5-flare"
    assert image_engine.astra_brief_model() == "gpt-6-astra"


def test_story_quick_graphic_routes_to_flare():
    assert image_engine.select_astra_model(
        {"surface": "instagram story"}) == "gpt-image-2.5-flare"


def test_text_overlay_pins_sunburst_even_on_a_story():
    assert image_engine.select_astra_model(
        {"surface": "instagram story", "has_text_overlay": True}
    ) == "gpt-image-2.5-sunburst"


@pytest.mark.parametrize("kind", ["infographic", "carousel"])
def test_infographics_and_carousels_route_to_sunburst(kind):
    assert image_engine.select_astra_model(
        {"kind": kind, "surface": "instagram story"}) == "gpt-image-2.5-sunburst"


def test_size_defaults_are_feed_1080x1350_and_story_1080x1920():
    assert image_engine.size_for({}) == "1080x1350"
    assert image_engine.size_for({"surface": "instagram story"}) == "1080x1920"
    assert image_engine.size_for({"size": "2048x2048"}) == "2048x2048"


# ---- size snapping: the live API rejects any dimension not divisible by 16 ----
# Verified against the real endpoint 2026-09-10:
#   1080x1350 -> HTTP 400 "Width and height must both be divisible by 16."
#   1024x1280 / 1152x2048 / 1024x1024 -> HTTP 200
# Without the snap, EVERY Astra call 400s and the chain lives on Gemini forever.


def test_every_snapped_dimension_is_divisible_by_sixteen():
    for target in ("1080x1350", "1080x1920", "1024x1024", "1000x1500", "37x99"):
        w, h = image_engine.parse_size(image_engine.snap_size(target))
        assert w % 16 == 0 and h % 16 == 0, (target, w, h)
        assert w >= 16 and h >= 16


def test_the_two_echo_targets_snap_to_the_verified_sizes():
    assert image_engine.snap_size("1080x1350") == "1024x1280"   # 4:5, HTTP 200
    assert image_engine.snap_size("1080x1920") == "1152x2048"   # 9:16, HTTP 200


def test_the_snap_preserves_the_aspect_ratio():
    for target in ("1080x1350", "1080x1920"):
        tw, th = image_engine.parse_size(target)
        sw, sh = image_engine.parse_size(image_engine.snap_size(target))
        assert abs((sw / sh) - (tw / th)) < 0.001, (target, sw, sh)


def test_astra_sends_the_snapped_size_not_the_raw_target(astra_key):
    transport = _Transport((200, _astra_ok_body()))
    engine = image_engine.AstraImageEngine("sk-test", transport=transport)
    engine.generate("brief", {"kind": "infographic"})
    assert transport.payloads[0]["tools"][0]["size"] == "1024x1280"

    transport2 = _Transport((200, _astra_ok_body()))
    engine2 = image_engine.AstraImageEngine("sk-test", transport=transport2)
    engine2.generate("brief", {"surface": "instagram story"})
    assert transport2.payloads[0]["tools"][0]["size"] == "1152x2048"


def test_the_result_is_scaled_back_to_the_requested_target(astra_key):
    from PIL import Image
    import io
    body = json.dumps({"output": [{"type": "image_generation_call",
                                   "result": _real_png_b64_sized(1024, 1280)}]})
    engine = image_engine.AstraImageEngine(
        "sk-test", transport=_Transport((200, body)))
    res = engine.generate("brief", {"kind": "infographic"})
    with Image.open(io.BytesIO(res.image_bytes)) as im:
        assert im.size == (1080, 1350)


def test_a_failed_resize_keeps_the_generated_bytes():
    """Losing a card to a resize would be worse than a few pixels off target."""
    assert image_engine.fit_to_size(b"not-an-image", "1080x1350") == b"not-an-image"
    assert image_engine.fit_to_size(b"", "1080x1350") == b""


def test_sunburst_costs_more_than_flare():
    sun = image_engine.cost_estimate("astra", "gpt-image-2.5-sunburst")
    flare = image_engine.cost_estimate("astra", "gpt-image-2.5-flare")
    assert sun > flare > 0


# ---------------------------------------------------------------------------
# 2. The Astra engine itself
# ---------------------------------------------------------------------------


def test_astra_posts_the_spec_responses_payload(astra_key):
    transport = _Transport((200, _astra_ok_body()))
    engine = image_engine.AstraImageEngine("sk-test", transport=transport)
    engine.generate("the creative brief", {"kind": "infographic"})

    payload = transport.payloads[0]
    assert payload["model"] == "gpt-6-astra"
    assert payload["input"] == "the creative brief"
    assert len(payload["tools"]) == 1
    tool = payload["tools"][0]
    assert tool["type"] == "image_generation"
    assert tool["model"] == "gpt-image-2.5-sunburst"
    # the SNAPPED size: the live tool rejects anything not divisible by 16
    assert tool["size"] == "1024x1280"


def test_astra_extracts_image_and_revised_prompt_from_tool_output(astra_key):
    transport = _Transport((200, _astra_ok_body("astra rewrote this")))
    engine = image_engine.AstraImageEngine("sk-test", transport=transport)
    res = engine.generate("brief", {})
    assert res.image_bytes == PNG
    assert res.engine == "astra"
    assert res.model == "gpt-image-2.5-sunburst"
    assert res.revised_prompt == "astra rewrote this"
    assert res.cost_estimate > 0
    assert res.ok()


def test_astra_reads_a_nested_message_content_image_part(astra_key):
    body = json.dumps({"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "here you go"},
        {"type": "output_image", "b64_json": PNG_B64},
    ]}]})
    engine = image_engine.AstraImageEngine(
        "sk-test", transport=_Transport((200, body)))
    assert engine.generate("brief", {}).image_bytes == PNG


def test_astra_http_error_surfaces_the_real_provider_response(astra_key):
    body = json.dumps({"error": {
        "message": "The model `gpt-image-2.5-sunburst` does not exist",
        "type": "invalid_request_error", "code": "model_not_found"}})
    engine = image_engine.AstraImageEngine(
        "sk-test", transport=_Transport((404, body)))
    with pytest.raises(image_engine.ImageEngineError) as exc:
        engine.generate("brief", {})
    assert exc.value.status == 404
    assert "model_not_found" in exc.value.response_text
    assert "model_not_found" in exc.value.detail()


def test_astra_two_hundred_with_no_image_is_a_loud_error(astra_key):
    body = json.dumps({"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "I cannot draw that"}]}]})
    engine = image_engine.AstraImageEngine(
        "sk-test", transport=_Transport((200, body)))
    with pytest.raises(image_engine.ImageEngineError) as exc:
        engine.generate("brief", {})
    assert "no image" in str(exc.value).lower()


def test_astra_never_puts_the_key_on_the_result(astra_key):
    engine = image_engine.AstraImageEngine(
        "sk-test-secret", transport=_Transport((200, _astra_ok_body())))
    res = engine.generate("brief", {})
    assert "sk-test-secret" not in repr(res)


# ---------------------------------------------------------------------------
# 3. The fallback chain
# ---------------------------------------------------------------------------


def test_astra_wins_and_gemini_is_never_called(monkeypatch, astra_key):
    transport = _Transport((200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)
    gem = _FakeGemini()
    res = image_engine.generate_image("brief", {}, gemini_client=gem)
    assert res.engine == "astra" and res.image_bytes == PNG
    assert gem.calls == []


def test_astra_retries_exactly_once_with_backoff_then_falls_to_gemini(
        monkeypatch, astra_key):
    transport = _Transport((500, "upstream boom"))
    _patch_astra(monkeypatch, transport)
    monkeypatch.setattr(image_engine, "ASTRA_RETRY_BACKOFF_SECS", 1.5)
    gem = _FakeGemini()
    slept = []
    res = image_engine.generate_image(
        "brief", {}, gemini_client=gem, sleep=slept.append)

    assert image_engine.ASTRA_MAX_RETRIES == 1
    assert len(transport.payloads) == 2, "Astra must be tried twice, not more"
    assert slept == [1.5], "the retry must back off before the second attempt"
    assert res.engine == "gemini" and res.image_bytes == GEM


def test_astra_succeeding_on_the_retry_skips_gemini(monkeypatch, astra_key):
    transport = _Transport((500, "boom"), (200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)
    gem = _FakeGemini()
    res = image_engine.generate_image(
        "brief", {}, gemini_client=gem, sleep=lambda _s: None)
    assert res.engine == "astra"
    assert gem.calls == []


def test_each_engine_gets_its_own_prompt(monkeypatch, astra_key):
    transport = _Transport((500, "boom"))
    _patch_astra(monkeypatch, transport)
    gem = _FakeGemini()
    image_engine.generate_image(
        "shared", {"engine_prompts": {"astra": "ASTRA BRIEF",
                                      "gemini": "GEMINI PROMPT"}},
        gemini_client=gem, sleep=lambda _s: None)
    assert transport.payloads[0]["input"] == "ASTRA BRIEF"
    assert gem.calls[0][0] == "GEMINI PROMPT"


def test_gemini_uses_the_routed_gemini_model(monkeypatch, astra_key):
    """The routed model must reach the Gemini rung. Deliberately NOT the config
    default, so a rung that ignores the route and hardcodes NANO_MODEL fails."""
    _patch_astra(monkeypatch, _Transport((500, "boom")))
    gem = _FakeGemini()
    image_engine.generate_image("brief", {"gemini_model": "gemini-3.1-flash-image"},
                                gemini_client=gem, sleep=lambda _s: None)
    assert gem.calls[0][1] == "gemini-3.1-flash-image"


# ---------------------------------------------------------------------------
# 4. Needs human: a slot NEVER fails silently
# ---------------------------------------------------------------------------


def test_every_engine_failing_marks_needs_human_loudly(monkeypatch, astra_key):
    _patch_astra(monkeypatch, _Transport((500, "astra down")))
    gem = _FakeGemini(fail=True)
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))

    res = image_engine.generate_image(
        "brief", {}, gemini_client=gem, account_key="gritx",
        subject="Speed to lead wins", sleep=lambda _s: None)

    assert res is None
    assert len(alerts) == 1
    assert "NEEDS HUMAN" in alerts[0]
    assert "Speed to lead wins" in alerts[0]
    assert "gritx" in alerts[0]

    rows = [r for r in db.audit_rows() if r["kind"] == "image_needs_human"]
    assert len(rows) == 1
    assert rows[0]["subject"] == "Speed to lead wins"
    assert rows[0]["account_key"] == "gritx"


def test_needs_human_detail_names_every_engine_that_failed(monkeypatch, astra_key):
    _patch_astra(monkeypatch, _Transport((404, "no such model")))
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    image_engine.generate_image("brief", {}, gemini_client=_FakeGemini(fail=True),
                                subject="hook", sleep=lambda _s: None)
    assert "astra" in alerts[0] and "gemini" in alerts[0]
    assert "no such model" in alerts[0]


def test_needs_human_never_raises_when_the_store_is_broken(monkeypatch):
    monkeypatch.setattr("agent.db.audit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert image_engine.mark_needs_human(subject="x")["needs_human"] is True


# ---------------------------------------------------------------------------
# 5. Cost observability
# ---------------------------------------------------------------------------


def test_cost_accumulates_per_day_and_prints_the_daily_line(
        monkeypatch, astra_key, capsys):
    _patch_astra(monkeypatch, _Transport((200, _astra_ok_body())))
    image_engine.generate_image("brief", {}, gemini_client=_FakeGemini())
    image_engine.generate_image("brief", {}, gemini_client=_FakeGemini())

    out = capsys.readouterr().out
    assert "daily image spend" in out
    assert image_engine.daily_image_count() == 2
    expected = 2 * image_engine.cost_estimate("astra", "gpt-image-2.5-sunburst")
    assert abs(image_engine.daily_cost_usd() - expected) < 0.02
    assert "engine=astra" in out and "latency_ms=" in out and "cost_est=$" in out


def test_daily_cost_alert_fires_once_over_the_ten_dollar_threshold(monkeypatch):
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    assert image_engine.daily_cost_alert_usd() == 10.0

    image_engine.record_cost(9.99)
    assert alerts == []
    image_engine.record_cost(0.50)
    assert len(alerts) == 1 and "over budget" in alerts[0]
    image_engine.record_cost(5.00)
    assert len(alerts) == 1, "the over-budget alert fires once per day, not per image"


def test_cost_alert_threshold_is_env_tunable(monkeypatch):
    monkeypatch.setenv("AGENT_IMAGE_DAILY_COST_ALERT_USD", "2.5")
    assert image_engine.daily_cost_alert_usd() == 2.5
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    image_engine.record_cost(3.00)
    assert len(alerts) == 1


# ---------------------------------------------------------------------------
# 6. creative_studio wiring — no gate weakened
# ---------------------------------------------------------------------------


def test_generate_draws_with_astra_and_writes_the_file(monkeypatch, astra_key,
                                                       tmp_path):
    _patch_astra(monkeypatch, _Transport((200, _astra_ok_body())))
    out = tmp_path / "card.png"
    res = creative_studio.generate("Speed to lead wins", ["Answer in five minutes"],
                                   client=_FakeGemini(), out_path=str(out))
    assert res is not None
    assert out.read_bytes() == PNG
    assert res["model"] == "gpt-image-2.5-sunburst"
    assert res["route"].startswith("astra:")


def test_generate_sends_astra_the_brief_not_the_gemini_prompt(monkeypatch,
                                                              astra_key, tmp_path):
    transport = _Transport((200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)
    creative_studio.generate("Speed to lead wins", ["Answer in five minutes"],
                             client=_FakeGemini(),
                             out_path=str(tmp_path / "c.png"))
    brief = transport.payloads[0]["input"]
    assert "FLAT EDITORIAL INFOGRAPHIC" in brief
    assert "URL FOOTER" in brief


def test_generate_falls_back_to_the_unchanged_gemini_path(monkeypatch, astra_key,
                                                          tmp_path):
    _patch_astra(monkeypatch, _Transport((503, "astra unavailable")))
    gem = _FakeGemini()
    out = tmp_path / "card.png"
    res = creative_studio.generate("Speed to lead wins", ["Answer in five minutes"],
                                   client=gem, out_path=str(out))
    assert res is not None
    assert out.read_bytes() == GEM
    assert res["route"].startswith("gemini:")
    # the Gemini rung still receives the classic single-prompt text
    assert "Design a clean, minimal, premium LASSO-branded infographic." in gem.calls[0][0]


def test_route_label_is_not_double_prefixed_on_a_regrade(monkeypatch, tmp_path):
    """The grade gate re-renders in a loop; the route label must stay
    'gemini:<pro/flash route>', never 'gemini:gemini:...'."""
    from agent import grade_gate
    monkeypatch.setenv("AGENT_STYLE_GATE_ENABLED", "true")
    real = grade_gate.grade_card
    calls = []

    def _fail_once(prompt, headline=""):
        calls.append(1)
        if len(calls) == 1:
            return grade_gate.GradeResult(scores={}, passed=False,
                                          failed_questions=["Q6"])
        return real(prompt, headline=headline)

    monkeypatch.setattr(grade_gate, "grade_card", _fail_once)
    res = creative_studio.generate("Speed to lead wins", ["A fact"],
                                   client=_FakeGemini(),
                                   out_path=str(tmp_path / "c.png"))
    assert len(calls) == 2, "the gate must have forced one re-render"
    assert res["route"].count("gemini:") == 1, res["route"]
    assert "pro:all" in res["route"]


def test_generate_needs_no_gemini_key_when_astra_is_available(monkeypatch,
                                                              astra_key, tmp_path):
    """An Astra key ALONE renders. There is no Gemini client and no Gemini key,
    which used to be an unconditional early None."""
    monkeypatch.delenv("AGENT_NANO_API_KEY", raising=False)
    assert creative_studio._default_client() is None
    _patch_astra(monkeypatch, _Transport((200, _astra_ok_body())))
    out = tmp_path / "card.png"
    res = creative_studio.generate("Speed to lead wins", ["Answer in five minutes"],
                                   out_path=str(out))
    assert res is not None and out.read_bytes() == PNG
    assert res["route"].startswith("astra:")


def test_generate_with_no_engine_at_all_stays_silent(monkeypatch):
    """UNCHANGED contract: no Gemini key and no Astra key -> None, no alert."""
    monkeypatch.delenv(image_engine.OPENAI_API_KEY_ENV, raising=False)
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    assert creative_studio.generate("Headline", ["A fact"]) is None
    assert alerts == []


def test_generate_gates_are_untouched(monkeypatch, astra_key):
    """Flag off and the no-fabrication gate still block BEFORE any engine runs."""
    _patch_astra(monkeypatch, _Transport((200, _astra_ok_body())))
    monkeypatch.setenv("AGENT_NANO_ENABLED", "false")
    assert creative_studio.generate("Headline", ["A fact"]) is None
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    assert creative_studio.generate("Headline", []) is None


def test_story_surface_with_a_headline_still_asks_for_sunburst(monkeypatch,
                                                               astra_key, tmp_path):
    transport = _Transport((200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)
    creative_studio.generate("Speed to lead wins", ["A fact"],
                             client=_FakeGemini(), surface="instagram story",
                             out_path=str(tmp_path / "s.png"))
    tool = transport.payloads[0]["tools"][0]
    assert tool["model"] == "gpt-image-2.5-sunburst"
    assert tool["size"] == "1152x2048"          # snapped 9:16, same ratio


# ---------------------------------------------------------------------------
# 7. The other image callers route through the same chain
# ---------------------------------------------------------------------------


def _real_png_b64(color=(11, 22, 33)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class _GeminiPng:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def generate_image(self, prompt, model):
        self.calls.append((prompt, model))
        if self.fail:
            raise RuntimeError("gemini down")
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (99, 99, 99)).save(buf, format="PNG")
        return buf.getvalue()


def test_welcome_background_renders_through_astra(monkeypatch, astra_key, tmp_path):
    from agent import welcome_templates as wt
    body = json.dumps({"output": [{"type": "image_generation_call",
                                   "result": _real_png_b64()}]})
    transport = _Transport((200, body))
    _patch_astra(monkeypatch, transport)
    gem = _GeminiPng()
    path, mode = wt.ensure_background(wt.get_template("T2"), bg_client=gem,
                                      cache_dir=str(tmp_path))
    assert mode == "pro" and os.path.isfile(path)
    assert gem.calls == [], "Astra must draw it, not the fallback rung"
    assert transport.payloads[0]["tools"][0]["model"] == "gpt-image-2.5-sunburst"


def test_welcome_story_background_takes_the_flare_quick_graphic_route(
        monkeypatch, astra_key, tmp_path):
    from agent import welcome_templates as wt
    body = json.dumps({"output": [{"type": "image_generation_call",
                                   "result": _real_png_b64()}]})
    transport = _Transport((200, body))
    _patch_astra(monkeypatch, transport)
    wt.ensure_background(wt.get_template("T2"), bg_client=_GeminiPng(),
                         cache_dir=str(tmp_path), fmt="story")
    assert transport.payloads[0]["tools"][0]["model"] == "gpt-image-2.5-flare"


def test_welcome_background_degrades_instead_of_raising(monkeypatch, astra_key,
                                                        tmp_path):
    """Every engine failing used to let the provider exception escape."""
    from agent import welcome_templates as wt
    _patch_astra(monkeypatch, _Transport((500, "astra down")))
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    path, mode = wt.ensure_background(
        wt.get_template("T2"), bg_client=_GeminiPng(fail=True),
        cache_dir=str(tmp_path))
    assert mode == "placeholder" and os.path.isfile(path)
    assert path.endswith("_placeholder.png")
    assert any("NEEDS HUMAN" in a for a in alerts)


# ---------------------------------------------------------------------------
# 8. The infographic brief
# ---------------------------------------------------------------------------


def test_brief_carries_voice_palette_fonts_hook_and_cta():
    brief = astra_prompt.build_infographic_brief(
        "Speed to lead wins", ["Answer in five minutes"], cta="Book a call")
    assert "BRAND VOICE" in brief
    assert "#FAF6F0" in brief and "#121E3C" in brief      # brand colors
    assert "Anton" in brief and "Oswald" in brief          # brand fonts
    assert "Speed to lead wins" in brief                   # the hook
    assert "Book a call" in brief                          # the CTA


def test_brief_states_the_four_full_gym_blocks():
    brief = astra_prompt.build_infographic_brief("Hook", ["A fact"], cta="Book")
    assert "BOLD HEADLINE" in brief
    assert "THREE ELEMENT VISUAL METAPHOR" in brief
    assert "CTA BUTTON BLOCK" in brief
    assert "URL FOOTER" in brief
    assert "LASSOFRAMEWORK.COM" in brief


def test_brief_passes_the_house_style_grade_gate():
    from agent import grade_gate
    brief = astra_prompt.build_infographic_brief(
        "Speed to lead wins", ["Answer in five minutes"], cta="Book a call")
    assert grade_gate.grade_card(brief, headline="Speed to lead wins").passed


def test_brief_obeys_the_hard_copy_rules():
    brief = astra_prompt.build_infographic_brief("Hook", ["A fact"])
    assert "—" not in brief and "–" not in brief
    with pytest.raises(ValueError):
        astra_prompt.build_infographic_brief("Pick the right vendor", ["A fact"])


def test_brief_invents_nothing_when_there_is_nothing_to_say():
    brief = astra_prompt.build_infographic_brief("Hook", [])
    assert "NO FABRICATION" in brief
    assert "APPROVED CONTEXT" not in brief


def test_brief_uses_the_story_frame_on_a_story_surface():
    brief = astra_prompt.build_infographic_brief(
        "Hook", ["A fact"], surface="instagram story")
    assert "1080x1920" in brief and "9:16" in brief
