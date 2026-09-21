"""Echo channel-repair tests for the Studio package (agent/creative_studio.py).

Covers the 2026-09-21 spec: LASSO content-quality corrective retries must send
the EXACT latest rejected candidate pixels via opts repair_image_bytes, a
terminal failure must record full scrubbed evidence in a durable sidecar (never
db.audit's 500-char truncation, never AGENT_GENERATION_RECORD), must NOT create
an approved PNG or review.json, must emit exactly ONE bounded contextual alert,
and must populate failure_info (reason/stage/reported) with no PASS bypass.
"""
import hashlib
import json

from agent import creative_studio, image_engine, infographic_review
from agent.grade_gate import GradeResult


LONG_REASON = (
    "Wordmark sits above y=0.10 and the CTA renders below y=0.85; " * 12
    + "the Story safe region is violated by both the header lockup and the "
    "destination footer, and the full scrubbed reason is far longer than any "
    "alert or 500-char audit column may carry."
)


def _env(monkeypatch):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_LASSO_INFOGRAPHIC_QUALITY", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _isolate(monkeypatch):
    monkeypatch.setattr(creative_studio, "_reference_images_for", lambda *a, **kw: ("", []))
    monkeypatch.setattr(creative_studio, "_generation_log_attempt", lambda **kw: None)
    monkeypatch.setattr(infographic_review, "AstraReviewer", lambda *a, **kw: object())
    alerts = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: alerts.append(msg))
    return alerts


class _SequenceEngine:
    """Returns a distinct byte payload per generation call."""

    def __init__(self):
        self.calls = []

    def __call__(self, prompt, opts, **kwargs):
        self.calls.append((prompt, dict(opts)))
        n = len(self.calls)
        return image_engine.ImageResult(
            image_bytes=f"candidate-bytes-{n}".encode(), engine="astra",
            model="test-model", prompt_used=prompt)


def _fail_then_pass(verdicts):
    """Review fake that pops a GradeResult per reviewed candidate."""
    reviewed = []

    def review(image_bytes, **kwargs):
        reviewed.append(image_bytes)
        status, reason = verdicts.pop(0)
        return GradeResult({}, status == "PASS", [], status=status, reason=reason)

    return review, reviewed


def test_retry_sends_exact_latest_rejected_pixels(monkeypatch, tmp_path):
    _env(monkeypatch)
    alerts = _isolate(monkeypatch)
    references = [{"id": "feed-benchmark", "bytes": b"benchmark pixels"}]
    monkeypatch.setattr(creative_studio, "_reference_images_for", lambda *a, **kw: ("benchmark", references))
    engine = _SequenceEngine()
    monkeypatch.setattr(image_engine, "generate_image", engine)
    verdicts = [("FAIL", "first rejection: wordmark too high"),
                ("FAIL", "second rejection: CTA clipped"),
                ("PASS", "")]
    review, reviewed = _fail_then_pass(verdicts)
    monkeypatch.setattr(infographic_review, "evaluate", review)

    out = tmp_path / "card.png"
    result = creative_studio.generate(
        "Hook", ["Approved fact"], client=object(), account_key="lasso_ig",
        surface="Story", aspect="9:16", pixels="1080x1920", draft_id="draft-1",
        out_path=str(out))

    assert result is not None
    assert result["grade_status"] == "PASS"
    # Three generation calls; the first must carry NO repair pixels.
    assert len(engine.calls) == 3
    assert "repair_image_bytes" not in engine.calls[0][1]
    assert "reference_images" not in engine.calls[0][1]
    assert all("reference_images" not in opts for _, opts in engine.calls[1:])
    # Attempt 2 repairs the exact bytes rejected at attempt 1, attempt 3 the
    # exact bytes rejected at attempt 2 (latest rejected pixels, not the first).
    assert engine.calls[1][1]["repair_image_bytes"] == b"candidate-bytes-1"
    assert engine.calls[2][1]["repair_image_bytes"] == b"candidate-bytes-2"
    # Reviewer saw every candidate; success produced no failure sidecar/alert.
    assert reviewed == [b"candidate-bytes-1", b"candidate-bytes-2", b"candidate-bytes-3"]
    assert not (tmp_path / "card.png.failure.json").exists()
    assert alerts == []


def test_terminal_failure_sidecar_single_alert_and_failure_info(monkeypatch, tmp_path):
    _env(monkeypatch)
    alerts = _isolate(monkeypatch)
    engine = _SequenceEngine()
    monkeypatch.setattr(image_engine, "generate_image", engine)
    verdicts = [("FAIL", LONG_REASON)] * 3
    review, _ = _fail_then_pass(verdicts)
    monkeypatch.setattr(infographic_review, "evaluate", review)

    out = tmp_path / "story.png"
    failure_info = {"stale": True}
    result = creative_studio.generate(
        "Hook", ["Approved fact"], client=object(), account_key="lasso_ig",
        surface="Story", aspect="9:16", pixels="1080x1920", draft_id="draft-9",
        cta="Save your spot", out_path=str(out), failure_info=failure_info)

    # None contract on terminal failure; no approved PNG, no review.json.
    assert result is None
    assert not out.exists()
    assert not (tmp_path / "story.png.review.json").exists()

    # failure_info: cleared at entry, populated for the terminal failure.
    assert "stale" not in failure_info
    assert failure_info["stage"] == "quality"
    assert failure_info["reported"] is True
    assert failure_info["reason"] == LONG_REASON

    # Exactly ONE bounded contextual alert with account/surface/draft/attempts.
    assert len(alerts) == 1
    alert = alerts[0]
    assert "lasso_ig" in alert and "Story" in alert and "draft-9" in alert
    assert "attempts=3" in alert
    assert len(alert) < 600
    # The long reason is NOT truncated into the alert at full length.
    assert LONG_REASON not in alert
    assert "y=0.10" in alert  # short leading fragment of the reason

    # Durable sidecar with FULL reason, copy, review history, context, hash.
    sidecar_path = tmp_path / "story.png.failure.json"
    assert sidecar_path.exists()
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["reason"] == LONG_REASON          # full, not 500-char capped
    assert sidecar["status"] == "FAIL"
    assert sidecar["attempts"] == 3
    assert sidecar["account_key"] == "lasso_ig"
    assert sidecar["surface"] == "Story"
    assert sidecar["draft_id"] == "draft-9"
    assert sidecar["headline"] == "Hook"
    assert sidecar["facts"] == ["Approved fact"]
    assert sidecar["cta"] == "Save your spot"
    assert len(sidecar["review_history"]) == 3
    assert sidecar["candidate_sha256"] == hashlib.sha256(b"candidate-bytes-3").hexdigest()
    assert failure_info["sidecar"] == str(sidecar_path)


def test_sidecar_error_does_not_conceal_failure(monkeypatch, tmp_path):
    _env(monkeypatch)
    alerts = _isolate(monkeypatch)
    engine = _SequenceEngine()
    monkeypatch.setattr(image_engine, "generate_image", engine)
    review, _ = _fail_then_pass([("FAIL", LONG_REASON)] * 3)
    monkeypatch.setattr(infographic_review, "evaluate", review)

    # The output directory is replaced by a FILE so the sidecar write fails.
    blocker = tmp_path / "blocked.png"
    blocker.write_bytes(b"i am a file, not a directory")
    out = blocker / "card.png"          # parent is not a directory -> write fails
    failure_info = {}
    result = creative_studio.generate(
        "Hook", ["Approved fact"], client=object(), account_key="lasso_ig",
        out_path=str(out), failure_info=failure_info)

    # FAIL stays FAIL: None return, failure surfaced, alert still fired.
    assert result is None
    assert failure_info["stage"] == "quality"
    assert failure_info["reported"] is True
    assert failure_info["reason"] == LONG_REASON
    assert len(alerts) == 1


def test_evidence_retained_without_generation_record(monkeypatch, tmp_path):
    _env(monkeypatch)
    alerts = _isolate(monkeypatch)
    # Prove the sidecar is independent of the generation-record path: the log
    # hook records nothing here (mirrors AGENT_GENERATION_RECORD unset).
    recorded = []
    monkeypatch.setattr(creative_studio, "_generation_log_attempt",
                        lambda **kw: recorded.append(kw))
    engine = _SequenceEngine()
    monkeypatch.setattr(image_engine, "generate_image", engine)
    review, _ = _fail_then_pass([("UNGRADED", "reviewer unavailable")] * 3)
    monkeypatch.setattr(infographic_review, "evaluate", review)

    out = tmp_path / "card.png"
    result = creative_studio.generate(
        "Hook", ["Approved fact"], client=object(), account_key="lasso_ig",
        out_path=str(out))
    assert result is None
    sidecar = json.loads((tmp_path / "card.png.failure.json").read_text())
    assert sidecar["reason"] == "reviewer unavailable"
    assert sidecar["status"] == "UNGRADED"
    assert len(sidecar["review_history"]) == 3
    assert len(alerts) == 1
    _ = recorded  # the record hook ran or not; evidence must exist regardless


def test_no_pass_bypass_and_success_leaves_failure_info_empty(monkeypatch, tmp_path):
    _env(monkeypatch)
    alerts = _isolate(monkeypatch)
    engine = _SequenceEngine()
    monkeypatch.setattr(image_engine, "generate_image", engine)
    review, _ = _fail_then_pass([("PASS", "clean")])
    monkeypatch.setattr(infographic_review, "evaluate", review)

    out = tmp_path / "card.png"
    failure_info = {"precall": "must be cleared"}
    result = creative_studio.generate(
        "Hook", ["Approved fact"], client=object(), account_key="lasso_ig",
        out_path=str(out), failure_info=failure_info)

    assert result is not None
    assert result["grade_status"] == "PASS"
    assert out.exists()                       # approved output only on real PASS
    assert failure_info == {}                 # cleared at entry, nothing set
    assert not (tmp_path / "card.png.failure.json").exists()
    assert alerts == []


def test_render_unavailable_reports_via_failure_info(monkeypatch, tmp_path):
    _env(monkeypatch)
    _isolate(monkeypatch)
    monkeypatch.setattr(image_engine, "generate_image", lambda *a, **kw: None)
    failure_info = {}
    result = creative_studio.generate(
        "Hook", ["Approved fact"], client=object(), account_key="lasso_ig",
        out_path=str(tmp_path / "card.png"), failure_info=failure_info)
    assert result is None
    assert failure_info["stage"] == "render_unavailable"
    # The real chain has already emitted the ops alert + audit via
    # mark_needs_human for this call, so the caller must not duplicate it.
    assert failure_info["reported"] is True
    assert "every engine" in failure_info["reason"]


def test_failure_sidecar_scrubs_secrets_in_full_nested_history(monkeypatch, tmp_path):
    """Lead regression: the sidecar must redact secrets from EVERY string,
    including deep inside the review history, while keeping the full
    >500-character reason intact."""
    from agent import ops_alerts
    from agent.ops_alerts import REDACTED

    fake_secret = "xK9f2QpL7mZt4WvN8sB3cR6yH"
    monkeypatch.setenv("AGENT_TEST_FAKE_SECRET", fake_secret)
    path = str(tmp_path / "story.png.failure.json")
    payload = {
        "reason": LONG_REASON,                # >500 chars, must survive in full
        "review_history": [
            {"attempt": 1, "note": f"provider echoed key {fake_secret} back"},
            {"attempt": 2, "note": ("nested " * 100) + fake_secret},
        ],
        "headline": "Hook",
    }
    written = creative_studio._write_failure_sidecar(path, payload)
    assert written == path
    sidecar_text = open(path).read()
    assert fake_secret not in sidecar_text
    assert REDACTED in sidecar_text
    sidecar = json.loads(sidecar_text)
    assert sidecar["reason"] == LONG_REASON          # full reason, not truncated
    for entry in sidecar["review_history"]:
        assert fake_secret not in json.dumps(entry)


def test_regeneration_from_old_saved_hook_uses_blakes_approved_correction(monkeypatch, tmp_path):
    _env(monkeypatch)
    _isolate(monkeypatch)
    engine = _SequenceEngine()
    monkeypatch.setattr(image_engine, "generate_image", engine)
    seen = []
    def review(raw, **kw):
        seen.append(kw["headline"])
        return GradeResult({}, True, [], status="PASS", reason="")
    monkeypatch.setattr(infographic_review, "evaluate", review)
    result = creative_studio.generate("You did not open a gym to run ads at 11pm.",
        ["Approved fact"], client=object(), account_key="lasso_ig", surface="Story",
        out_path=str(tmp_path / "story.png"))
    assert seen == ["You did not open a gym to run your own ads."]
    assert result["infographic_copy"]["headline"] == seen[0]
    assert "11pm" not in engine.calls[0][1]["engine_prompts"]["astra"]
