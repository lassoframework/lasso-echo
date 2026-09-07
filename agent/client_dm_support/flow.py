"""
flow.py — the end-to-end decision for ONE client DM ticket.

    ad hard-stop -> route -> diagnose -> plan -> scope gate -> execute ->
    re-run the SAME diagnostic -> ground the reply -> auto-reply, or escalate.

Every step can only ever move the outcome towards ESCALATE. There is no branch that
turns a refusal back into a send, and the only producer of `auto_reply` is the final
reply.compose() on a path that passed all of the above.

ON THE ROUTER, WHICH IS NOT A SAFETY GATE.
route() looks at the client's words to pick WHICH diagnostic to run. That is a keyword
map, i.e. an enumeration over an open set -- the shape D68 says must never be a safety
control. It is not one here, and the asymmetry is what makes that true:

    route() returning None            -> ESCALATE (safe)
    route() returning the wrong lane  -> that lane's FACTS will not match any remedy,
                                         or will not verify, or will not ground a
                                         reply -> ESCALATE (safe)
    route() returning the right lane  -> safety still comes entirely from the facts

So a phrasing the router misses costs a human a ticket; it never costs a client a
wrong autonomous action. Safety is downstream of the router, in what the ANSWER is
derived from, exactly as D68 requires.

THE CODE-FIX LANE IS DELIBERATELY EMPTY TODAY.
scope_gate knows how to scope-check a KIND_CODE_FIX action (allowed roots, one named
gym, all the foundation triggers), because getting that gate right is the hard part
and it should exist before it is needed. But remedies.plan() never returns one and
remedies.EXECUTORS has no entry for it, so no code fix can execute. That is recorded
here rather than left to be discovered, and tests/test_client_dm_flow.py holds a
TWO-WAY guard on it (D68: "assert the allow-list still CONTAINS what it must, not only
that writers stay inside it") so the lane cannot be filled without the test failing
and forcing the wiring question to be answered.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ad_block as _ad
from . import diagnostics as _diag
from . import remedies as _rem
from . import reply as _reply
from . import scope_gate as _gate
from . import verify as _verify

DECISION_AUTO_REPLY = "auto_reply"
DECISION_ESCALATE = "escalate"


@dataclass(frozen=True)
class DmDecision:
    decision: str
    reason: str
    gym_key: str = ""
    diagnostic_id: str = ""
    remedy_id: str = ""
    reply_text: str = ""
    template_id: str = ""
    foundation_trigger: str = ""
    client_text: str = ""          # the client's own words, for the escalation card
    audit: dict = field(default_factory=dict)

    @property
    def will_post(self):
        return self.decision == DECISION_AUTO_REPLY and bool(self.reply_text)


def _escalate(reason, *, trigger="", **kw):
    return DmDecision(DECISION_ESCALATE, reason, foundation_trigger=trigger, **kw)


def _with_text(decision, text):
    """Attach the client's own words so the escalation card can show them. Never used
    to decide anything -- only to tell the human what they are looking at."""
    return DmDecision(
        decision.decision, decision.reason, gym_key=decision.gym_key,
        diagnostic_id=decision.diagnostic_id, remedy_id=decision.remedy_id,
        reply_text=decision.reply_text, template_id=decision.template_id,
        foundation_trigger=decision.foundation_trigger,
        client_text=str(text or ""), audit=decision.audit)


# ---------------------------------------------------------------------------
# The router. See the module docstring: a miss costs a human a ticket, never a
# client a wrong action.
# ---------------------------------------------------------------------------
ROUTES = (
    (_diag.DIAG_DRIVE_PHOTOS, (
        "photo", "photos", "picture", "pictures", "image", "images",
        "google drive", "drive folder", "my folder", "no media", "blank post",
    )),
    (_diag.DIAG_CTA_POOL, (
        "call to action", "cta", "no cta", "link in bio", "booking link",
    )),
)


def route(text):
    """Which diagnostic this message is about, or None."""
    low = (text or "").lower()
    for diagnostic_id, terms in ROUTES:
        if any(t in low for t in terms):
            return diagnostic_id
    return None


def handle_ticket(*, text, gym_key, deps=None, diagnostic_id=None):
    """Decide what to do about one client DM. Posts nothing; the caller posts.

    `deps` is a dict of injectables handed to diagnostics and executors (store,
    now, daily_hour_utc, lane_active_for, voice_dir, read_text, sync_source, log).
    Every one of them has a real production default, so a test double is never the
    only implementation of a seam (D68's "built but not wired" class).
    """
    deps = dict(deps or {})
    return _with_text(_decide(text=text, gym_key=gym_key, deps=deps,
                              diagnostic_id=diagnostic_id), text)


def _decide(*, text, gym_key, deps, diagnostic_id=None):
    # 1. THE AD HARD STOP. Unconditional, first, before anything reads a fact.
    #    Note this is the BELT, not the control: the control is that this package
    #    has no ad-write call path at all (ad_block.assert_no_ad_call_path).
    if _ad.is_ad_money_topic(text):
        return _escalate(_ad.AD_ESCALATION_REASON,
                         trigger=_gate.TRIGGER_AD_MONEY, gym_key=str(gym_key or ""))

    # 2. Resolve the join key. A portal uuid raises rather than querying with it.
    try:
        key = _diag.require_account_key(gym_key)
    except _diag.DiagnosticError as e:
        return _escalate(f"could not resolve the gym's account key: {e}")

    # 3. Route.
    diagnostic_id = diagnostic_id or route(text)
    if diagnostic_id not in _diag.ALL_DIAGNOSTICS:
        return _escalate(
            "no enumerated diagnostic covers this message, so there is no fact base "
            "to ground a reply in",
            gym_key=key,
        )

    # 4. Diagnose (read-only).
    diag_kw = _diagnostic_kwargs(diagnostic_id, deps)
    try:
        before = _diag.run(diagnostic_id, key, stage="diagnosis", **diag_kw)
    except Exception as e:  # noqa: BLE001
        return _escalate(f"diagnostic {diagnostic_id!r} failed: {type(e).__name__}: {e}",
                         gym_key=key, diagnostic_id=diagnostic_id)

    # 5. Plan a remedy from the FACTS. None == escalate.
    remedy = _rem.plan(before)
    if remedy is None:
        return _escalate(
            "the facts do not match any enumerated remedy for this lane",
            gym_key=key, diagnostic_id=diagnostic_id,
            audit={"facts": dict(before.facts)},
        )

    # 6. The foundation-trigger check, on the PROPOSED ACTION.
    verdict = _gate.check(remedy.action)
    if not verdict.allowed:
        return _escalate(verdict.reason, trigger=verdict.trigger, gym_key=key,
                         diagnostic_id=diagnostic_id, remedy_id=remedy.id)

    # 7. Execute (execute() re-checks the gate itself; belt and braces).
    result = _rem.execute(remedy, gym_key=key, **_executor_kwargs(deps))
    if not result.ok:
        return _escalate(f"the scoped fix did not complete: {result.reason}",
                         gym_key=key, diagnostic_id=diagnostic_id, remedy_id=remedy.id)

    # 8. Verify by re-running the SAME diagnostic — when the remedy committed to a
    #    measurable change. A remedy that writes nothing (Case 2) has nothing to
    #    verify and says so, rather than being handed a free pass.
    if remedy.expectation is not None:
        try:
            after = _diag.run(diagnostic_id, key, stage="verification", **diag_kw)
        except Exception as e:  # noqa: BLE001
            return _escalate(
                f"could not re-run {diagnostic_id!r} to verify: {type(e).__name__}: {e}",
                gym_key=key, diagnostic_id=diagnostic_id, remedy_id=remedy.id)
        vr = _verify.check(remedy.expectation, before, after, run_facts=result.run_facts)
        if not vr.verified:
            return _escalate(
                f"the fix ran but did not verify, so nothing may claim it worked: "
                f"{vr.reason}",
                gym_key=key, diagnostic_id=diagnostic_id, remedy_id=remedy.id,
                audit={"verification": vr.reason})
        snapshot = _verify.verified_snapshot(after, result.run_facts)
        verification_note = vr.reason
    else:
        if remedy.action.kind not in _gate.NON_WRITING_KINDS:
            # A writing remedy with no expectation could never be verified. Refuse
            # rather than reply — this is the "verification step skipped" hole.
            return _escalate(
                "a writing remedy declared no expectation, so its effect cannot be "
                "verified and no reply may be composed",
                gym_key=key, diagnostic_id=diagnostic_id, remedy_id=remedy.id)
        snapshot = before
        verification_note = "no write was performed; nothing to verify"

    # 9. THE GROUNDED-REPLY GATE. Byte-identical template render over these facts,
    #    or the whole thing is refused.
    try:
        text_out, audit = _reply.compose(remedy.reply_template_id, snapshot)
    except _reply.ReplyRefused as e:
        return _escalate(f"the reply could not be grounded: {e}",
                         gym_key=key, diagnostic_id=diagnostic_id, remedy_id=remedy.id)

    audit = dict(audit)
    audit["verification"] = verification_note
    audit["scope"] = verdict.reason
    return DmDecision(
        DECISION_AUTO_REPLY,
        f"grounded in {len(audit['fact_keys'])} verified fact key(s)",
        gym_key=key, diagnostic_id=diagnostic_id, remedy_id=remedy.id,
        reply_text=text_out, template_id=remedy.reply_template_id, audit=audit,
    )


_DIAG_KW = {
    _diag.DIAG_DRIVE_PHOTOS: ("store", "now", "daily_hour_utc", "lane_active_for"),
    _diag.DIAG_CTA_POOL: ("voice_dir", "read_text"),
}
_EXECUTOR_KW = ("store", "sync_source", "log")


def _diagnostic_kwargs(diagnostic_id, deps):
    return {k: deps[k] for k in _DIAG_KW.get(diagnostic_id, ()) if k in deps}


def _executor_kwargs(deps):
    return {k: deps[k] for k in _EXECUTOR_KW if k in deps}
