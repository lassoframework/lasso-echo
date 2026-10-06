"""Offline contracts for Echo's OpenAI text runtime and provider boundary."""

import ast
import json
from pathlib import Path

import pytest

from agent import clipper, config, drafter, openai_text, podcast_auto, video_editor, website_intake


@pytest.fixture
def no_text_lanes(monkeypatch):
    for name in ("SLACK_CONVO_ENABLED", "SLACK_CONVO_ECHO_ENABLED", "AGENT_SB7_ENABLED",
                 "AGENT_WEBSITE_AUTO_INTAKE", "AGENT_CLIPPER_ENABLED",
                 "AGENT_AUTO_REELS_ENABLED", "AGENT_GBP_MIRROR", "AGENT_GBP_MONTH_SWEEP",
                 "AGENT_VIDEO_EDITOR_ENABLED", "AGENT_PODCAST_AUTO_ENABLED",
                 "AGENT_EPISODE_INBOX_ENABLED"):
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("AGENT_GBP_MIRROR_GYMS", "")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", "false")
    for model_env in ("AGENT_SLACK_CONVO_MODEL", "AGENT_SB7_MODEL", "AGENT_CLIPPER_MODEL"):
        monkeypatch.delenv(model_env, raising=False)


def _response(text="Grounded answer.", status="completed"):
    return json.dumps({"status": status, "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": text}]}]})


def test_shared_responses_contract_and_no_secret_in_test_transport(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    seen = []

    def transport(url, headers, payload):
        seen.append((url, headers, payload))
        return 200, _response()

    assert openai_text.complete("system", "user", model="gpt-6-astra",
                                max_output_tokens=1600, transport=transport) \
        == "Grounded answer."
    assert seen == [(openai_text.RESPONSES_URL, {"Content-Type": "application/json"},
                     {"model": "gpt-6-astra", "instructions": "system", "input": "user",
                      "max_output_tokens": 1600})]


def test_provider_failure_and_incomplete_response_fail_closed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="HTTP 401") as error:
        openai_text.complete("system", "user", model="gpt-6-astra",
                             max_output_tokens=1000,
                             transport=lambda *_: (401, "private provider body"))
    assert "private provider body" not in str(error.value)
    with pytest.raises(RuntimeError, match="no complete text"):
        openai_text.complete("system", "user", model="gpt-6-astra",
                             max_output_tokens=1000,
                             transport=lambda *_: (200, _response(status="incomplete")))


def test_missing_openai_key_and_old_model_do_not_call_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        openai_text.complete("system", "user", model="gpt-6-astra",
                             max_output_tokens=1000,
                             transport=lambda *_: pytest.fail("provider called"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(ValueError, match="supported OpenAI text model"):
        openai_text.complete("system", "user", model="claude-sonnet-5",
                             max_output_tokens=1000,
                             transport=lambda *_: pytest.fail("provider called"))


def test_caption_website_and_clipper_use_shared_openai_client(monkeypatch):
    monkeypatch.delenv("AGENT_SB7_MODEL", raising=False)
    monkeypatch.delenv("AGENT_CLIPPER_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []

    def complete(system, user, **kwargs):
        calls.append((system, user, kwargs))
        return "model text"

    monkeypatch.setattr(openai_text, "complete", complete)
    assert drafter._call_llm_caption("caption rules", "caption facts") == "model text"
    assert website_intake._call_llm("site rules", "site facts") == "model text"
    assert clipper._default_llm("clip rules", "transcript") == "model text"
    assert calls == [
        ("caption rules", "caption facts", {"model": "gpt-6-astra", "max_output_tokens": 1600}),
        ("site rules", "site facts", {"model": "gpt-6-astra", "max_output_tokens": 4000}),
        ("clip rules", "transcript", {"model": "gpt-6-astra", "max_output_tokens": 4000}),
    ]


@pytest.mark.parametrize("env_name,model_reader", [
    ("AGENT_SLACK_CONVO_MODEL", config.slack_convo_model),
    ("AGENT_SB7_MODEL", config.sb7_model),
    ("AGENT_CLIPPER_MODEL", config.clipper_model),
])
def test_stale_model_overrides_are_rejected(monkeypatch, env_name, model_reader):
    monkeypatch.setenv(env_name, "claude-sonnet-5")
    with pytest.raises(ValueError, match="supported OpenAI text model"):
        model_reader()


@pytest.mark.parametrize("model", ["gpt-image-2.5-sunburst", "gpt-bogus", "gpt-6-astra-bogus"])
def test_non_text_or_unknown_models_never_reach_provider(monkeypatch, model):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(ValueError, match="supported OpenAI text model"):
        openai_text.complete("system", "user", model=model, max_output_tokens=1000,
                             transport=lambda *_: pytest.fail("provider called"))


@pytest.mark.parametrize("model", sorted(openai_text.ALLOWED_TEXT_MODELS))
def test_supported_text_models_pass_local_validation(model):
    assert openai_text.validate_model(model) == model


_ARMED_LANES = [
    ("slack", "SLACK_CONVO_ENABLED", "SLACK_CONVO_ECHO_ENABLED", "AGENT_SLACK_CONVO_MODEL"),
    ("sb7", "AGENT_SB7_ENABLED", None, "AGENT_SB7_MODEL"),
    ("website", "AGENT_WEBSITE_AUTO_INTAKE", None, "AGENT_SB7_MODEL"),
    ("clipper", "AGENT_CLIPPER_ENABLED", None, "AGENT_CLIPPER_MODEL"),
    ("auto_reels", "AGENT_AUTO_REELS_ENABLED", None, "AGENT_SB7_MODEL"),
    ("gbp_mirror", "AGENT_GBP_MIRROR", None, "AGENT_SB7_MODEL"),
    ("gbp_month_sweep", "AGENT_GBP_MONTH_SWEEP", None, "AGENT_SB7_MODEL"),
    ("video_editor", "AGENT_VIDEO_EDITOR_ENABLED", None, "AGENT_CLIPPER_MODEL"),
    ("podcast_auto", "AGENT_PODCAST_AUTO_ENABLED", None, "AGENT_CLIPPER_MODEL"),
    ("episode_inbox", "AGENT_EPISODE_INBOX_ENABLED", None, "AGENT_CLIPPER_MODEL"),
]


@pytest.mark.parametrize("lane,flag,extra_flag,model_env", _ARMED_LANES)
@pytest.mark.parametrize("problem", ["missing_key", "stale_model", "image_model", "bogus_model"])
def test_every_armed_lane_fails_preflight_with_classifier_off(
        monkeypatch, no_text_lanes, lane, flag, extra_flag, model_env, problem):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv(flag, "true")
    if extra_flag:
        monkeypatch.setenv(extra_flag, "true")
    if problem == "missing_key":
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        expected = "OPENAI_API_KEY"
    else:
        monkeypatch.setenv(model_env, {
            "stale_model": "claude-sonnet-5",
            "image_model": "gpt-image-2.5-sunburst",
            "bogus_model": "gpt-bogus",
        }[problem])
        expected = model_env
    with pytest.raises(RuntimeError, match=expected):
        openai_text.startup_preflight()


def test_listener_and_daily_worker_fail_before_side_effects(monkeypatch, no_text_lanes):
    from agent import listener, opus_ingest, runner
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", "false")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(opus_ingest, "validated_project_ids",
                        lambda: pytest.fail("listener started after failed preflight"))
    monkeypatch.setattr(runner, "_trust_startup_warning",
                        lambda: pytest.fail("daily worker started after failed preflight"))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        listener.run_listener()
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "false")
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        runner.run_daily()


def test_manual_text_entrypoints_fail_before_staging_or_fetch(monkeypatch, no_text_lanes):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_CLIPPER_ENABLED", "true")
    monkeypatch.setattr(clipper, "stage_episode",
                        lambda *_args, **_kwargs: pytest.fail("episode staged"))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        clipper.clip_episode("episode.mp4")
    monkeypatch.setenv("AGENT_CLIPPER_ENABLED", "false")
    monkeypatch.setenv("AGENT_WEBSITE_AUTO_INTAKE", "true")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        website_intake.run(bases=["test"], fetch=lambda *_: pytest.fail("website fetched"))
    monkeypatch.setenv("AGENT_WEBSITE_AUTO_INTAKE", "false")
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        video_editor.edit_episode("episode.mp4", llm=lambda *_: "offline")
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "false")
    monkeypatch.setenv("AGENT_PODCAST_AUTO_ENABLED", "true")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        podcast_auto.run(source="episode.mp4", llm=lambda *_: "offline")


def test_explicit_empty_model_override_fails_closed(monkeypatch, no_text_lanes):
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_SB7_MODEL", " ")
    with pytest.raises(RuntimeError, match="AGENT_SB7_MODEL"):
        openai_text.startup_preflight()


@pytest.mark.parametrize("problem", ["missing_key", "stale_model", "image_model", "bogus_model"])
def test_episode_inbox_only_flag_blocks_boot_and_poll_before_side_effects(
        monkeypatch, no_text_lanes, problem):
    from agent import db, episode_inbox, listener
    monkeypatch.setenv("AGENT_EPISODE_INBOX_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    if problem == "missing_key":
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        expected = "OPENAI_API_KEY"
    else:
        monkeypatch.setenv("AGENT_CLIPPER_MODEL", {
            "stale_model": "claude-sonnet-5",
            "image_model": "gpt-image-2.5-sunburst",
            "bogus_model": "gpt-bogus",
        }[problem])
        expected = "AGENT_CLIPPER_MODEL"
    monkeypatch.setattr(db, "kv_set", lambda *_: pytest.fail("last-run or claim written"))

    class NoListClient:
        def list_prefix(self, *_):
            pytest.fail("episode inbox listed")

    with pytest.raises(RuntimeError, match=expected):
        listener.run_listener()
    with pytest.raises(RuntimeError, match=expected):
        episode_inbox.poll(client=NoListClient())


@pytest.mark.parametrize("problem", ["missing_key", "stale_model", "image_model", "bogus_model"])
def test_manual_website_intake_forces_preflight_with_auto_sweep_off(
        monkeypatch, no_text_lanes, problem):
    from agent import __main__ as cli
    monkeypatch.setenv("AGENT_WEBSITE_AUTO_INTAKE", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    if problem == "missing_key":
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        expected = "OPENAI_API_KEY"
    else:
        monkeypatch.setenv("AGENT_SB7_MODEL", {
            "stale_model": "claude-sonnet-5",
            "image_model": "gpt-image-2.5-sunburst",
            "bogus_model": "gpt-bogus",
        }[problem])
        expected = "AGENT_SB7_MODEL"
    monkeypatch.setattr(website_intake, "fetch_site_text",
                        lambda *_args, **_kwargs: pytest.fail("website fetched"))
    with pytest.raises(RuntimeError, match=expected):
        website_intake.intake_from_website("gymx", domain="gymx.com")
    with pytest.raises(RuntimeError, match=expected):
        cli.main(["website-intake", "--account", "gymx", "--domain", "gymx.com"])


def test_clipper_preserves_missing_key_error_type(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(clipper.ClipperError, match="OPENAI_API_KEY"):
        clipper._default_llm("rules", "transcript")


def test_runtime_source_has_no_anthropic_import_key_or_claude_model_default():
    agent_dir = Path(__file__).resolve().parents[1] / "agent"
    offenders = []
    for path in agent_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] == "anthropic" for alias in node.names):
                    offenders.append(f"{path.name}:{node.lineno}:import")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] == "anthropic":
                    offenders.append(f"{path.name}:{node.lineno}:from")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value == "ANTHROPIC_API_KEY" or node.value.startswith("claude-"):
                    offenders.append(f"{path.name}:{node.lineno}:constant")
    assert offenders == []
