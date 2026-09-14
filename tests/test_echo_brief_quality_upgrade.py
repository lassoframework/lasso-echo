"""
Tests for the echo-brief-quality-upgrade branch (spec sections 5, 6, 3, 7):

  - grade_gate.evaluate(): UNGRADED never counts as a pass; Q3/Q6 read the
    ACTUAL IMAGE (not the prompt text); Q7 critical-copy-accuracy is a hard
    block regardless of the other scores.
  - grade_gate.corrective_instruction(): a failed/ungraded result produces a
    specific instruction, and creative_studio.generate() (with
    AGENT_REAL_GRADE_POLICY=true) actually threads that instruction into the
    NEXT Astra brief rather than resending an identical one.
  - image_engine.AstraImageEngine.generate(): reference images passed via
    opts["reference_images"] actually appear as input_image content items in
    the real Responses API payload, and configured quality/reasoning_effort
    land on the request.
  - generation_log.record() persists a queryable row.

All offline: every network boundary is mocked through the existing transport /
vision-client injection seams the rest of the suite uses.
"""

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, db, generation_log, grade_gate, image_engine  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nASTRA-FAKE-BYTES-PADDED-SO-THE-BASE64-IS-REALISTICALLY-LONG"
PNG_B64 = base64.b64encode(PNG).decode()


def _astra_ok_body(revised="revised by astra"):
    return json.dumps({"output": [{
        "type": "image_generation_call",
        "result": PNG_B64,
        "revised_prompt": revised,
    }]})


class _Transport:
    def __init__(self, *responses):
        self.responses = list(responses) or [(200, _astra_ok_body())]
        self.payloads = []

    def __call__(self, url, headers, payload):
        self.payloads.append(payload)
        idx = min(len(self.payloads) - 1, len(self.responses) - 1)
        return self.responses[idx]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(image_engine, "ASTRA_RETRY_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(creative_studio, "_RENDER_RETRY_BACKOFF_SECS", 0.0,
                        raising=False)


def _patch_astra(monkeypatch, transport):
    monkeypatch.setattr(image_engine.AstraImageEngine, "_post",
                        lambda self, payload: transport(
                            image_engine.ASTRA_RESPONSES_URL, {}, payload))


# ---------------------------------------------------------------------------
# grade_gate.evaluate()
# ---------------------------------------------------------------------------


def test_evaluate_no_vision_client_is_ungraded_not_passed():
    result = grade_gate.evaluate(PNG, headline="Speed to lead wins",
                                 facts=["fact one"], vision_client=None)
    assert result.status == "UNGRADED"
    assert result.passed is False


class _AllYesClient:
    """YES to every question EXCEPT the critical-copy-accuracy check, where YES
    would mean "an error was found" — this client represents a clean card, so
    it answers NO there (accurate copy) and YES everywhere else."""
    def ask_image(self, image_bytes, question):
        if "APPROVED SOURCE COPY" in question:
            return "NO"
        return "YES"


class _AllYesButCopyIsWrong:
    """Every question returns YES *except* the critical-copy check, which must
    be distinguishable from the others (it asks about approved source copy)."""
    def ask_image(self, image_bytes, question):
        if "APPROVED SOURCE COPY" in question:
            return "YES, the image says '50% off' but the approved copy never mentions a discount."
        return "YES"


class _CopyCheckExplodes:
    def ask_image(self, image_bytes, question):
        if "APPROVED SOURCE COPY" in question:
            raise RuntimeError("vision API timeout")
        return "YES"


def test_evaluate_all_pass_with_accurate_copy():
    result = grade_gate.evaluate(
        PNG, headline="Speed to lead wins", facts=["fact one"],
        vision_client=_AllYesClient())
    assert result.status == "PASS"
    assert result.passed is True
    assert result.scores["Q7"] is True


def test_evaluate_critical_copy_error_hard_blocks_regardless_of_other_scores():
    result = grade_gate.evaluate(
        PNG, headline="Speed to lead wins", facts=["fact one"],
        vision_client=_AllYesButCopyIsWrong())
    # Every other question passed, but Q7 (fabricated discount) must still fail
    # the WHOLE card. This is the "hard block regardless of overall score" rule.
    assert result.status == "FAIL"
    assert result.passed is False
    assert result.scores["Q7"] is False
    assert "discount" in result.reason.lower() or "50%" in result.reason


def test_evaluate_unparseable_copy_check_is_ungraded_not_a_silent_pass():
    result = grade_gate.evaluate(
        PNG, headline="Speed to lead wins", facts=["fact one"],
        vision_client=_CopyCheckExplodes())
    assert result.status == "UNGRADED"
    assert result.passed is False
    assert result.scores["Q7"] is None


def test_q3_and_q6_read_the_actual_image_not_the_prompt_text():
    """A prompt that says nothing useful (no 'exactly one', no 'visual anchor')
    still PASSES Q3/Q6 when the vision client says the rendered image has them —
    proving the check is now image-based, not a keyword match on prompt text."""
    class _ImageLooksGreatClient:
        def ask_image(self, image_bytes, question):
            return "YES"

    result = grade_gate.evaluate(
        PNG, prompt_text="a prompt with none of the old magic keywords in it",
        headline="Speed to lead wins", facts=["fact one"],
        vision_client=_ImageLooksGreatClient())
    assert result.scores["Q3"] is True
    assert result.scores["Q6"] is True


def test_evaluate_footer_url_is_approved_not_a_fabrication():
    """Regression: the first live sample run hard-blocked every card because
    the Q7 approved-copy list never included the URL footer every card
    renders, so the vision model (correctly) flagged the approved footer as
    an unapproved URL. The footer, once passed, must be treated as approved."""
    class _FlagsAnyURLNotInCopy:
        def ask_image(self, image_bytes, question):
            if "APPROVED SOURCE COPY" in question:
                if "lassoframework.com".upper() in question.upper():
                    return "NO"  # the footer IS in the approved copy now
                return "YES, unapproved URL rendered."
            return "YES"

    result = grade_gate.evaluate(
        PNG, headline="Speed to lead wins", facts=["fact one"],
        footer="LASSOFRAMEWORK.COM", vision_client=_FlagsAnyURLNotInCopy())
    assert result.status == "PASS"
    assert result.scores["Q7"] is True


def test_corrective_instruction_names_the_specific_failure():
    gr = grade_gate.GradeResult(
        scores={"Q1": True, "Q2": True, "Q3": False, "Q4": True, "Q5": False,
               "Q6": True, "Q7": True},
        passed=False, failed_questions=["Q3", "Q5"], status="FAIL")
    note = grade_gate.corrective_instruction(gr)
    assert "accent color" in note.lower()
    assert "enlarge the headline" in note.lower() or "low-contrast" in note.lower()


def test_corrective_instruction_empty_on_a_clean_pass():
    gr = grade_gate.GradeResult(scores={}, passed=True, failed_questions=[],
                                status="PASS")
    assert grade_gate.corrective_instruction(gr) == ""


# ---------------------------------------------------------------------------
# creative_studio.generate(): corrective feedback actually propagates
# ---------------------------------------------------------------------------


class _FailThenPassVision:
    """First image graded: Q6 (visual anchor) AND Q2 (scale contrast) both
    fail -> 2 hard fails -> the card fails (1 fail alone is still allowed, so
    this fixture fails two to force a real FAIL/retry). Copy is always
    accurate. Every later grade: everything passes."""
    def __init__(self):
        self.grades = 0

    def ask_image(self, image_bytes, question):
        if "APPROVED SOURCE COPY" in question:
            self.grades += 1
            return "NO"  # copy is always accurate in this fixture
        if self.grades == 0 and ("visual anchor" in question.lower()
                                 or "scale contrast" in question.lower()):
            return "NO"
        return "YES"


def test_corrective_feedback_propagates_into_the_next_astra_brief(monkeypatch):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_REAL_GRADE_POLICY", "true")
    monkeypatch.setenv("AGENT_GRADE_ENABLED", "true")
    monkeypatch.setenv(image_engine.OPENAI_API_KEY_ENV, "sk-test")
    monkeypatch.setenv(config.NANO_API_KEY_ENV, "")

    vision = _FailThenPassVision()
    monkeypatch.setattr(creative_studio, "_default_vision_client", lambda: vision)

    transport = _Transport((200, _astra_ok_body()), (200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)

    out = creative_studio.generate("Speed to lead wins", ["fact one"],
                                   out_path="/tmp/_test_corrective_upgrade.png")
    assert out is not None
    # Two Astra calls: first attempt, then one retry after the Q6 failure.
    assert len(transport.payloads) == 2
    second_input = transport.payloads[1]["input"]
    assert "CORRECTIVE FEEDBACK" in second_input
    assert "visual anchor" in second_input.lower()
    first_input = transport.payloads[0]["input"]
    assert "CORRECTIVE FEEDBACK" not in first_input


# ---------------------------------------------------------------------------
# image_engine: reference images actually reach the real request payload
# ---------------------------------------------------------------------------


def test_reference_images_become_input_image_items_on_the_request(monkeypatch):
    monkeypatch.setenv(image_engine.OPENAI_API_KEY_ENV, "sk-test")
    transport = _Transport((200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)

    engine = image_engine.AstraImageEngine("sk-test")
    ref_bytes = b"\x89PNG\r\n\x1a\nA-REFERENCE-IMAGE"
    opts = {"kind": "infographic",
           "reference_images": [
               {"id": "abc123", "bytes": ref_bytes, "mime": "image/png"},
           ]}
    result = engine.generate("the brief text", opts)
    assert result.ok()
    payload = transport.payloads[0]
    assert isinstance(payload["input"], list)
    content = payload["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "the brief text"}
    image_items = [c for c in content if c.get("type") == "input_image"]
    assert len(image_items) == 1
    expected_b64 = base64.b64encode(ref_bytes).decode("ascii")
    assert image_items[0]["image_url"] == f"data:image/png;base64,{expected_b64}"
    assert result.reference_ids_used == ["abc123"]


def test_no_reference_images_keeps_input_a_plain_string(monkeypatch):
    """Byte-for-byte unchanged shape for every existing (no-reference) call."""
    monkeypatch.setenv(image_engine.OPENAI_API_KEY_ENV, "sk-test")
    transport = _Transport((200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)
    engine = image_engine.AstraImageEngine("sk-test")
    engine.generate("the brief text", {"kind": "infographic"})
    assert transport.payloads[0]["input"] == "the brief text"


def test_configured_quality_lands_on_the_image_tool(monkeypatch):
    monkeypatch.setenv(image_engine.OPENAI_API_KEY_ENV, "sk-test")
    monkeypatch.setenv("ASTRA_IMAGE_QUALITY", "high")
    transport = _Transport((200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)
    engine = image_engine.AstraImageEngine("sk-test")
    result = engine.generate("the brief text", {"kind": "infographic"})
    assert transport.payloads[0]["tools"][0]["quality"] == "high"
    assert result.quality_used == "high"


def test_unconfigured_quality_omits_the_field(monkeypatch):
    monkeypatch.delenv("ASTRA_IMAGE_QUALITY", raising=False)
    monkeypatch.setenv(image_engine.OPENAI_API_KEY_ENV, "sk-test")
    transport = _Transport((200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)
    engine = image_engine.AstraImageEngine("sk-test")
    engine.generate("the brief text", {"kind": "infographic"})
    assert "quality" not in transport.payloads[0]["tools"][0]


def test_invalid_quality_value_is_ignored():
    os.environ["ASTRA_IMAGE_QUALITY"] = "ultra-mega"
    try:
        assert config.astra_image_quality() == ""
    finally:
        del os.environ["ASTRA_IMAGE_QUALITY"]


def test_reasoning_effort_lands_on_the_top_level_payload_not_the_tool(monkeypatch):
    monkeypatch.setenv(image_engine.OPENAI_API_KEY_ENV, "sk-test")
    monkeypatch.setenv("ASTRA_REASONING_EFFORT", "medium")
    transport = _Transport((200, _astra_ok_body()))
    _patch_astra(monkeypatch, transport)
    engine = image_engine.AstraImageEngine("sk-test")
    engine.generate("the brief text", {"kind": "infographic"})
    payload = transport.payloads[0]
    assert payload["reasoning"] == {"effort": "medium"}
    assert "reasoning" not in payload["tools"][0]


# ---------------------------------------------------------------------------
# generation_log.record()
# ---------------------------------------------------------------------------


def test_generation_record_persists_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_GENERATION_RECORD", "true")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "gen.db"))
    rid = generation_log.record(
        draft_id="d1", account_key="lasso", headline="Speed to lead wins",
        facts=["fact one"], brief="the full brief", reference_ids=["abc123"],
        engine="astra", model="gpt-image-2.5-sunburst", route="astra:gpt-image-2.5-sunburst",
        quality_used="high", revised_prompt="revised text",
        original_bytes=b"orig-bytes", final_path="/tmp/x.png",
        final_bytes=b"final-bytes", grade_status="PASS",
        grade_scores={"Q1": True}, grade_reason="", corrective_feedback="",
        attempt=1, final_status="approved")
    assert rid is not None
    row = db.get_generation(rid)
    assert row["headline"] == "Speed to lead wins"
    assert row["facts"] == ["fact one"]
    assert row["reference_ids"] == ["abc123"]
    assert row["grade_status"] == "PASS"
    assert row["final_status"] == "approved"
    assert len(row["original_sha256"]) == 64


def test_generation_record_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_GENERATION_RECORD", raising=False)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "gen2.db"))
    assert generation_log.record(headline="x", facts=["y"]) is None
