"""
Echo channel repair (2026-09-21): the Engine package.

Corrective quality retries must carry the EXACT rejected candidate pixels into
the Astra request as a real image (base64 input_image), explicitly labeled as
the rejected candidate with a correction-only instruction. Benchmark references
stay separately labeled. Absent a repair image, the request payload is
byte-for-byte the legacy shape. All tests are OFFLINE: the Responses POST is
mocked through the AstraImageEngine transport seam. Nothing fetches a URL.

Covered:
  - a repair image rides in the payload as an input_image with the exact bytes
  - the rejected-candidate label and correction-only instruction are present
  - repair is independent of the benchmark reference count cap and flag
  - benchmark references remain separately labeled alongside a repair image
  - the request format is unchanged when no repair image is supplied
  - no public URL fetching for the repair image
"""

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import image_engine  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nREJECTED-CANDIDATE-FAKE-BYTES"
PNG_B64 = base64.b64encode(PNG).decode()
REPAIR = b"\x89PNG\r\n\x1a\nEXACT-REJECTED-CANDIDATE-PX"
REPAIR_B64 = base64.b64encode(REPAIR).decode()
REF1 = b"\x89PNG\r\n\x1a\nBENCHMARK-REFERENCE-ONE"
REF1_B64 = base64.b64encode(REF1).decode()
REF2 = b"\x89PNG\r\n\x1a\nBENCHMARK-REFERENCE-TWO"
REF2_B64 = base64.b64encode(REF2).decode()


def _astra_ok_body(revised="revised by astra"):
    return json.dumps({"output": [{
        "type": "image_generation_call",
        "result": PNG_B64,
        "revised_prompt": revised,
    }]})


class _Transport:
    """Records every Responses POST and replays scripted (status, body) pairs."""

    def __init__(self, *responses):
        self.responses = list(responses) or [(200, _astra_ok_body())]
        self.payloads = []

    def __call__(self, url, headers, payload):
        self.payloads.append(payload)
        idx = min(len(self.payloads) - 1, len(self.responses) - 1)
        return self.responses[idx]


@pytest.fixture
def transport():
    return _Transport()


@pytest.fixture
def engine(transport):
    return image_engine.AstraImageEngine("sk-test-not-a-real-key",
                                         transport=transport)


def _content_items(payload):
    """The content array of the single user message."""
    inp = payload["input"]
    assert isinstance(inp, list) and len(inp) == 1
    return inp[0]["content"]


def _text_items(content):
    return [c["text"] for c in content if c.get("type") == "input_text"]


def _image_items(content):
    return [c for c in content if c.get("type") == "input_image"]


def _image_bytes(item):
    url = item["image_url"]
    assert url.startswith("data:")
    header, _, b64 = url.partition(",")
    assert ";base64" in header
    return base64.b64decode(b64)


# ---------------------------------------------------------------------------
# Repair image payload
# ---------------------------------------------------------------------------


def test_repair_image_rides_as_an_actual_input_image(engine, transport):
    engine.generate("a brief", {"repair_image_bytes": REPAIR})
    images = _image_items(_content_items(transport.payloads[0]))
    assert len(images) == 1
    assert _image_bytes(images[0]) == REPAIR  # byte identity


def test_repair_image_gets_an_explicit_rejected_candidate_label(engine,
                                                               transport):
    engine.generate("a brief", {"repair_image_bytes": REPAIR})
    texts = _text_items(_content_items(transport.payloads[0]))
    assert any("REJECTED CANDIDATE" in t for t in texts)


def test_repair_instruction_is_correction_only(engine, transport):
    engine.generate("a brief", {"repair_image_bytes": REPAIR})
    instruction = image_engine.REPAIR_CANDIDATE_INSTRUCTION
    texts = _text_items(_content_items(transport.payloads[0]))
    assert instruction in texts
    lowered = instruction.lower()
    assert "only the corrections" in lowered
    assert "do not reproduce" in lowered


def test_repair_mime_defaults_to_png(engine, transport):
    engine.generate("a brief", {"repair_image_bytes": REPAIR})
    images = _image_items(_content_items(transport.payloads[0]))
    assert images[0]["image_url"].startswith("data:image/png;base64,")


def test_repair_mime_can_be_overridden(engine, transport):
    engine.generate("a brief", {"repair_image_bytes": REPAIR,
                                "repair_image_mime": "image/jpeg"})
    images = _image_items(_content_items(transport.payloads[0]))
    assert images[0]["image_url"].startswith("data:image/jpeg;base64,")


def test_empty_repair_bytes_keep_the_legacy_string_input(engine, transport):
    engine.generate("a brief", {"repair_image_bytes": b""})
    assert transport.payloads[0]["input"] == "a brief"


# ---------------------------------------------------------------------------
# Independence from benchmark references
# ---------------------------------------------------------------------------


def test_repair_rides_with_the_reference_flag_off(engine, transport,
                                                  monkeypatch):
    monkeypatch.setattr(image_engine.config,
                        "astra_reference_images_enabled", lambda: False)
    engine.generate("a brief", {"repair_image_bytes": REPAIR})
    images = _image_items(_content_items(transport.payloads[0]))
    assert [_image_bytes(i) for i in images] == [REPAIR]


def test_repair_image_is_not_counted_against_the_reference_cap(
        engine, transport, monkeypatch):
    monkeypatch.setattr(image_engine.config, "astra_reference_max",
                        lambda: 1)
    references = [{"id": "r1", "bytes": REF1},
                  {"id": "r2", "bytes": REF2}]
    engine.generate("a brief", {"reference_images": references,
                                "repair_image_bytes": REPAIR})
    images = _image_items(_content_items(transport.payloads[0]))
    # Cap trims references to one; the repair image rides on top regardless.
    assert len(images) == 2
    payloads = [_image_bytes(i) for i in images]
    assert REPAIR in payloads
    assert REF1 in payloads
    assert REF2 not in payloads


def test_repair_rides_even_with_zero_reference_cap(engine, transport,
                                                   monkeypatch):
    monkeypatch.setattr(image_engine.config, "astra_reference_max", lambda: 0)
    engine.generate("a brief", {"reference_images": [{"id": "r1",
                                                      "bytes": REF1}],
                                "repair_image_bytes": REPAIR})
    images = _image_items(_content_items(transport.payloads[0]))
    assert [_image_bytes(i) for i in images] == [REPAIR]


def test_benchmark_references_stay_separately_labeled(engine, transport):
    engine.generate("a brief", {
        "reference_images": [{"id": "bench-1", "bytes": REF1}],
        "repair_image_bytes": REPAIR,
    })
    content = _content_items(transport.payloads[0])
    texts = _text_items(content)
    assert any("REJECTED CANDIDATE" in t for t in texts)
    assert any("BENCHMARK REFERENCE" in t and "bench-1" in t for t in texts)
    images = _image_items(content)
    assert [_image_bytes(i) for i in images] == [REPAIR, REF1]
    # The rejected-candidate label precedes the repair image; the benchmark
    # label precedes its reference: labels attach to the right pixels.
    types = [c.get("type") for c in content]
    repair_idx = next(i for i, c in enumerate(content)
                      if c.get("type") == "input_image"
                      and _image_bytes(c) == REPAIR)
    ref_idx = next(i for i, c in enumerate(content)
                   if c.get("type") == "input_image"
                   and _image_bytes(c) == REF1)
    assert "REJECTED CANDIDATE" in content[repair_idx - 1]["text"]
    assert "BENCHMARK REFERENCE" in content[ref_idx - 1]["text"]


def test_references_without_repair_keep_the_legacy_unlabeled_shape(
        engine, transport):
    engine.generate("a brief",
                    {"reference_images": [{"id": "r1", "bytes": REF1}]})
    content = _content_items(transport.payloads[0])
    types = [c.get("type") for c in content]
    assert types == ["input_text", "input_image"]  # no injected label texts


# ---------------------------------------------------------------------------
# Legacy request format unchanged
# ---------------------------------------------------------------------------


def test_plain_request_is_still_a_bare_string(engine, transport):
    engine.generate("a brief", None)
    assert transport.payloads[0]["input"] == "a brief"


def test_no_repair_option_does_not_mention_rejected_candidates(engine,
                                                              transport):
    engine.generate("a brief", {"size": "1080x1350"})
    payload = transport.payloads[0]
    assert payload["input"] == "a brief"
    assert "REJECTED" not in json.dumps(payload)


def test_none_and_empty_opts_are_identical_to_legacy(engine, transport):
    engine.generate("a brief", {})
    engine.generate("a brief", None)
    p_empty, p_none = transport.payloads
    assert p_empty["input"] == p_none["input"] == "a brief"


# ---------------------------------------------------------------------------
# No URL fetching for repair
# ---------------------------------------------------------------------------


def test_repair_never_fetches_a_public_url(engine, transport, monkeypatch):
    def _boom(url, timeout=60):  # pragma: no cover - must never be called
        raise AssertionError(f"unexpected network fetch: {url}")

    monkeypatch.setattr(image_engine, "fetch_image_bytes", _boom)
    result = engine.generate("a brief", {"repair_image_bytes": REPAIR})
    assert result.ok()


def test_a_repair_url_string_is_treated_as_base64_data_not_a_link(
        engine, transport):
    # Even a caller passing a pre-encoded string rides as inline base64 data,
    # never as an http URL the engine would fetch.
    engine.generate("a brief", {"repair_image_bytes": REPAIR_B64})
    images = _image_items(_content_items(transport.payloads[0]))
    assert images[0]["image_url"] == f"data:image/png;base64,{REPAIR_B64}"
    assert not images[0]["image_url"].startswith("http")


def test_repair_only_honors_explicit_input_fidelity(engine, transport, monkeypatch):
    monkeypatch.setenv("ASTRA_INPUT_FIDELITY", "high")
    engine.generate("Repair", {"repair_image_bytes": REPAIR})
    assert transport.payloads[0]["tools"][0]["input_fidelity"] == "high"
    assert transport.payloads[0]["tools"][0]["action"] == "edit"
