"""
generation_log.py — persist the full record of one image generation (spec
section 7). OFF by default (config.generation_record_enabled(),
AGENT_GENERATION_RECORD): a new capability, ships off, same house rule as
every other flag in this repo.

Persists, going forward only (never backfilled for past generations):
  - the approved source copy (headline, cta, facts)
  - the full assembled brief actually sent
  - which reference images (by content-hash id) were attached, if any
  - the actual model id(s) + request settings (quality, reasoning_effort,
    input_fidelity) actually used
  - the returned revised_prompt, when the provider gave one
  - the original render's sha256 and the final resized image's path + sha256
  - the grade_gate.evaluate() result (status, scores, reason) and whatever
    corrective feedback was fed into the NEXT attempt
  - the attempt count and the final status ("approved", "withheld", "needs_human")

SECRETS: nothing this module touches ever holds an API key (briefs and
prompts carry no credentials by construction elsewhere in this repo), but any
string is still worth defending: it goes through ops_alerts.scrub before it is
written, so a stray credential accidentally embedded upstream is still caught.
"""

import hashlib

from . import config


def _sha256(data: bytes) -> str:
    try:
        return hashlib.sha256(data or b"").hexdigest()
    except Exception:
        return ""


def _scrub(text):
    try:
        from . import ops_alerts
        return ops_alerts.scrub(str(text or ""))
    except Exception:
        return str(text or "")


def record(*, draft_id="", account_key="", kind="infographic", headline="",
          cta="", facts=None, brief="", reference_ids=None, engine="",
          model="", route="", quality_used="", reasoning_effort_used="",
          input_fidelity_used="", revised_prompt="", original_bytes=b"",
          final_path="", final_bytes=b"", grade_status="", grade_scores=None,
          grade_reason="", corrective_feedback="", attempt=1,
          final_status=""):
    """Write one generation_records row. Never raises (a logging failure must
    never lose or block a real generation); returns the row id or None."""
    if not config.generation_record_enabled():
        return None
    try:
        from . import db as _db
        return _db.record_generation(
            draft_id=draft_id, account_key=account_key, kind=kind,
            headline=_scrub(headline), cta=_scrub(cta),
            facts=[_scrub(f) for f in (facts or [])],
            brief=_scrub(brief), reference_ids=list(reference_ids or []),
            engine=engine, model=model, route=route,
            quality_used=quality_used,
            reasoning_effort_used=reasoning_effort_used,
            input_fidelity_used=input_fidelity_used,
            revised_prompt=_scrub(revised_prompt),
            original_sha256=_sha256(original_bytes), final_path=final_path,
            final_sha256=_sha256(final_bytes), grade_status=grade_status,
            grade_scores=grade_scores or {}, grade_reason=_scrub(grade_reason),
            corrective_feedback=_scrub(corrective_feedback), attempt=attempt,
            final_status=final_status)
    except Exception as exc:  # noqa: BLE001
        print(f"[generation-log] record failed: {type(exc).__name__}: {exc}")
        return None
