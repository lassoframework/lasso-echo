"""Offline contracts for Echo's OpenAI text runtime and provider boundary."""

import ast
import json
from pathlib import Path

import pytest

from agent import clipper, config, drafter, openai_text, website_intake


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
    with pytest.raises(ValueError, match="OpenAI gpt model"):
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
    with pytest.raises(ValueError, match="OpenAI gpt model"):
        model_reader()


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
