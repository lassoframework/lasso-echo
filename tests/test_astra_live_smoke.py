"""
LIVE Astra smoke test — the one test in this suite that touches a real network.

It is SKIPPED unless BOTH are true:

    RUN_LIVE_SMOKE=1  and  OPENAI_API_KEY is set

Run it by hand:

    RUN_LIVE_SMOKE=1 OPENAI_API_KEY=... python -m pytest -q \
        tests/test_astra_live_smoke.py -s

It generates ONE 1080x1350 infographic through the real Responses API and writes
it to /tmp/astra_smoke_<timestamp>.png for eyeball review, printing the engine,
model, latency and estimated cost. On a rejection it FAILS with the provider's
exact response body (secret-scrubbed) rather than silently falling back, because
the whole point of this test is to prove whether the configured model ids are
real.

The key is captured at IMPORT time: tests/conftest.py strips every OPENAI_ env
var per test (the offline-by-default quarantine), so it has to be re-set inside
the test for the live call to work.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import astra_prompt, image_engine  # noqa: E402

# Captured before conftest's per-test env sweep removes them.
_LIVE_KEY = os.environ.get("OPENAI_API_KEY", "")
_LIVE_ON = os.environ.get("RUN_LIVE_SMOKE") == "1"

pytestmark = pytest.mark.skipif(
    not (_LIVE_ON and _LIVE_KEY),
    reason="live smoke is opt-in: set RUN_LIVE_SMOKE=1 and OPENAI_API_KEY")

SMOKE_HEADLINE = "Speed to lead wins the sale"
SMOKE_FACTS = [
    "Most gyms answer a new lead the next day",
    "The gyms that answer in five minutes book the most calls",
    "Speed is a process, not a personality",
]
SMOKE_CTA = "Book a growth call"


def test_astra_renders_a_real_1080x1350_infographic(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", _LIVE_KEY)

    brief = astra_prompt.build_infographic_brief(
        SMOKE_HEADLINE, SMOKE_FACTS, cta=SMOKE_CTA, surface="feed post")

    engine = image_engine.AstraImageEngine(_LIVE_KEY)
    opts = {"kind": "infographic", "has_text_overlay": True,
            "surface": "feed post", "size": "1080x1350"}

    print(f"\n[live-smoke] brief_model={image_engine.astra_brief_model()!r} "
          f"image_model={image_engine.select_astra_model(opts)!r} "
          f"size={image_engine.size_for(opts)!r}")

    try:
        result = engine.generate(brief, opts)
    except image_engine.ImageEngineError as exc:
        pytest.fail(
            "LIVE Astra call was REJECTED. This is the provider's exact "
            f"response:\n{exc.detail(limit=4000)}")

    out = f"/tmp/astra_smoke_{int(time.time())}.png"
    with open(out, "wb") as fh:
        fh.write(result.image_bytes)

    print(f"[live-smoke] wrote {out} ({len(result.image_bytes)} bytes) "
          f"engine={result.engine} model={result.model} "
          f"latency_ms={result.latency_ms} cost_est=${result.cost_estimate:.3f}")
    if result.revised_prompt:
        print(f"[live-smoke] revised_prompt: {result.revised_prompt[:400]}")

    assert len(result.image_bytes) > 1000, "the live render returned a stub"
    assert os.path.getsize(out) == len(result.image_bytes)
