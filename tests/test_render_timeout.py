"""
Render timeout + bounded retry tests for creative_studio.generate().

A single Gemini render call must never stall a long calendar render. These tests
inject fake model clients (no network) that block, raise, or succeed on a later
attempt, and assert generate() returns within the timeout, retries within the
bound, and never hangs. A short AGENT_RENDER_TIMEOUT_SECS override keeps the
tests fast; the env override itself is asserted too.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio  # noqa: E402


IMG = b"\x89PNG\r\n\x1a\nFAKEBYTES"


class _BlockingClient:
    """generate_image blocks far longer than the test timeout, modelling a hang."""

    def __init__(self, block_secs=30.0):
        self.block_secs = block_secs
        self.calls = 0

    def generate_image(self, prompt, model):
        self.calls += 1
        time.sleep(self.block_secs)
        return IMG


class _TransientErrorClient:
    """generate_image always raises a transient error."""

    def __init__(self):
        self.calls = 0

    def generate_image(self, prompt, model):
        self.calls += 1
        raise RuntimeError("transient network blip")


class _SucceedsOnNthClient:
    """Raises a transient error until the Nth attempt, then returns bytes."""

    def __init__(self, succeed_on=2):
        self.succeed_on = succeed_on
        self.calls = 0

    def generate_image(self, prompt, model):
        self.calls += 1
        if self.calls < self.succeed_on:
            raise RuntimeError("transient")
        return IMG


class _CountingClient:
    """Succeeds on the first attempt; counts calls to prove no extra retries."""

    def __init__(self):
        self.calls = 0

    def generate_image(self, prompt, model):
        self.calls += 1
        return IMG


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    # Flag on so the API path runs; short timeout + no backoff to keep tests fast.
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_RENDER_TIMEOUT_SECS", "0.3")
    monkeypatch.setattr(creative_studio, "_RENDER_RETRY_BACKOFF_SECS", 0.0)


# ---- 1. a hung call returns None within ~the timeout, does not hang ----------
def test_blocking_call_times_out_and_returns_none(monkeypatch, tmp_path):
    client = _BlockingClient(block_secs=30.0)
    out = tmp_path / "x.png"
    start = time.monotonic()
    res = creative_studio.generate(
        "Headline", ["A real approved fact"], client=client, out_path=str(out))
    elapsed = time.monotonic() - start
    assert res is None
    assert not out.exists()
    # 3 attempts x 0.3s timeout, plus a little slack; must be nowhere near a hang.
    assert elapsed < 5.0, elapsed


# ---- 2. a transient error is retried up to the bound, then None -------------
def test_transient_error_retries_then_none(monkeypatch, tmp_path):
    client = _TransientErrorClient()
    out = tmp_path / "x.png"
    res = creative_studio.generate(
        "Headline", ["A real approved fact"], client=client, out_path=str(out))
    assert res is None
    # first attempt + _RENDER_MAX_RETRIES retries
    assert client.calls == creative_studio._RENDER_MAX_RETRIES + 1
    assert not out.exists()


# ---- 3. success on the 2nd attempt -> the image is returned -----------------
def test_success_on_second_attempt(monkeypatch, tmp_path):
    client = _SucceedsOnNthClient(succeed_on=2)
    out = tmp_path / "x.png"
    res = creative_studio.generate(
        "Headline", ["A real approved fact"], client=client, out_path=str(out))
    assert res is not None
    assert client.calls == 2
    assert res["path"] == str(out) and out.exists()


# ---- 4. success on the first attempt -> unchanged behavior, no extra calls --
def test_success_first_attempt_no_extra_calls(monkeypatch, tmp_path):
    client = _CountingClient()
    out = tmp_path / "x.png"
    res = creative_studio.generate(
        "Headline", ["A real approved fact"], client=client, out_path=str(out))
    assert res is not None
    assert client.calls == 1
    assert res["path"] == str(out) and out.exists()


# ---- 5. the timeout is configurable via the env override --------------------
def test_timeout_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_RENDER_TIMEOUT_SECS", "4.5")
    assert creative_studio._render_timeout_secs() == 4.5
    # a bad or non-positive value falls back to the safe default
    monkeypatch.setenv("AGENT_RENDER_TIMEOUT_SECS", "not-a-number")
    assert creative_studio._render_timeout_secs() == creative_studio._RENDER_TIMEOUT_DEFAULT_SECS
    monkeypatch.setenv("AGENT_RENDER_TIMEOUT_SECS", "0")
    assert creative_studio._render_timeout_secs() == creative_studio._RENDER_TIMEOUT_DEFAULT_SECS
    monkeypatch.delenv("AGENT_RENDER_TIMEOUT_SECS", raising=False)
    assert creative_studio._render_timeout_secs() == creative_studio._RENDER_TIMEOUT_DEFAULT_SECS


# ---- 6. a hung render does not block the caller's thread indefinitely -------
def test_does_not_hang_the_caller(monkeypatch, tmp_path):
    client = _BlockingClient(block_secs=60.0)
    out = tmp_path / "x.png"
    result = {}

    def _run():
        result["res"] = creative_studio.generate(
            "Headline", ["A real approved fact"], client=client, out_path=str(out))

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=6.0)
    assert not t.is_alive(), "generate() hung past the render timeout budget"
    assert result.get("res") is None
