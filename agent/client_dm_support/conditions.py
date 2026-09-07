"""
conditions.py — ONE registry: what is wrong, what may be done about it, what may be
said, and how the doing is proved. All four in a single object per condition.

WHY THIS REPLACES FOUR MODULES.

The previous build split this across facts.py (the key list), diagnostics.py (the
measurement), remedies.py (the plan), scope_gate.py (the permission) and reply.py (the
sentence) -- five registries joined by string name. Six of the seven audit rounds found
at least one defect that was a DISAGREEMENT BETWEEN TWO OF THEM rather than a bug in
any one: a template claiming what no fact measured, a fact whose name promised more
than its query, a detector wider than the extractor it fed, a `requires` presence test
standing in for a value test. A registry you can edit one half of is a registry that
will drift, and every round paid for that.

Here, a Condition is not editable by halves. It names its probe, its `applies`
predicate over that probe's readings, the single enumerated action (or None), the
comparator that proves the action worked, and the ordered readings it reports. If a
reading is not in `report`, it is not in the reply; if it is, the reply is exactly that
reading's registered sentence.

THE THREE HARD INVARIANTS, asserted by assert_registry_wellformed() -- which compose()
calls on EVERY real invocation, not only in tests:

  1. NO ACTION WITHOUT A VERIFICATION. `action is not None` requires `expect is not
     None`. A remedy that writes and cannot be checked may not produce a reply. This is
     the "verification step skipped" hole, closed at the registry rather than at the
     call site, so no branch can route around it.
  2. NO SENTENCE WITHOUT A MEASUREMENT. Every entry in `report` must be a Reading with
     a non-empty `say`. There is no field on a Condition where free prose can be typed.
  3. NO ACTION OUTSIDE THE CLOSED SET, and the closed set touches exactly one domain.

BLAKE'S HARD LIMITS ARE STRUCTURAL HERE, NOT A GATE THAT COULD MISFIRE.
ACTIONS holds exactly ONE entry and its `domain` is "gym_media" -- a per-gym Drive
media sync. There is no generic action machinery, no action-kind DSL, no code-fix lane,
no path from any Condition to anything else. Ad budget, targeting, campaign
launch/pause, billing, pixel/CAPI, secrets, schema/RLS, feature flags, another
client's data: none of them is a case this code declines, they are all things this
code has no way to express. A closed set of one is a stronger argument than any list
of what must not be imported -- which is the conclusion the previous build reached in
its own round 3, after finding fourteen bypasses of its AST scanner across two rounds
and withdrawing the claim that the scanner was a proof.

The remaining tripwire (no_ad_rail.py) is labelled as exactly that and nothing rests
on it.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import probes as _p
from . import readings as _r


class Refused(Exception):
    """This capability will not produce a client reply for this ticket. Always
    escalates to a human; never softens into a partial or hedged message."""


# ---------------------------------------------------------------------------
# THE CLOSED SET OF ACTIONS. One entry. See the module docstring.
#
# `domain` is checked against SAFE_DOMAINS and FOUNDATION_DOMAINS by the foundation
# gate below, and both sets are pinned literally by
# tests/test_client_dm_conditions.py (a two-way guard: a writer straying outside the
# allow-list fails, AND the allow-list quietly widening fails).
# ---------------------------------------------------------------------------
FOUNDATION_DOMAINS = frozenset({
    "ad_budget",        # Blake: ALWAYS escalate
    "ad_targeting",     # Blake: ALWAYS escalate
    "ad_campaign",      # launch / pause / duplicate: ALWAYS escalate
    "billing",          # Stripe, invoicing, plan/tier
    "pixel_capi",       # pixel and CAPI setup
    "secrets",          # tokens, credentials, auth config
    "schema",           # migrations, RLS, DDL
    "feature_flags",    # anything affecting more than one gym
    "cross_gym",        # another client's data
})

SAFE_DOMAINS = frozenset({"gym_media"})


@dataclass(frozen=True)
class Action:
    id: str
    domain: str
    describe: str
    run: object          # callable(gym_key, *, store=None, log=None) -> dict of readings


def _gym_drive_sync(gym_key, *, store=None, log=None, sync_source=None):
    """Run THIS gym's own Drive media sync, now, instead of waiting for the nightly
    pass. Scoped to one gym by construction: it resolves that gym's own single active
    source and calls jobs.sync_gym_media.sync_source on it. The fleet-wide
    sync_gym_media.run() is deliberately NOT reachable from here.

    Returns the two run readings, or raises. Never returns a partial success.
    """
    key = _p.require_account_key(gym_key)
    store = store if store is not None else _p.default_media_store()
    if sync_source is None:
        from ..jobs.sync_gym_media import sync_source as _ss
        sync_source = _ss
    log = log or (lambda m: print(f"[client-dm] {m}"))

    sources = [s for s in (store.list_sources(gym_id=key, include_inactive=False) or [])
               if str(s.get("kind") or "") == _p.DRIVE_SOURCE_KIND]
    if len(sources) != 1:
        raise Refused(
            f"expected exactly one active gym_drive source for {key!r}, found "
            f"{len(sources)}; refusing to guess which folder the client meant"
        )
    src = sources[0]
    # TENANT re-assertion at the write path, not only at the read path.
    if str(src.get("gym_id") or "") != key:
        raise Refused(
            f"source {src.get('id')!r} is owned by {src.get('gym_id')!r}, not {key!r}"
        )
    summary = sync_source(src, store=store, log=log) or {}
    if not summary.get("ok"):
        raise Refused(
            f"the Drive sync for {key!r} did not complete: "
            f"{'share revoked' if summary.get('revoked') else summary.get('error') or 'unknown'}"
        )
    return {
        _r.DRIVE_SYNC_RAN.key: True,
        _r.DRIVE_FILES_ADDED.key: int(summary.get("inserted") or 0),
    }


ACTION_GYM_DRIVE_SYNC = "gym_drive_sync"

ACTIONS = {
    ACTION_GYM_DRIVE_SYNC: Action(
        id=ACTION_GYM_DRIVE_SYNC,
        domain="gym_media",
        describe=("run this one gym's own Google Drive media sync now, rather than "
                  "waiting for the nightly pass"),
        run=_gym_drive_sync,
    ),
}


TRIGGER_UNREGISTERED = "action_not_registered"
TRIGGER_FOUNDATION = "foundation_domain"


def foundation_gate(action_id):
    """Blake's hard limits, at the one place an action can be chosen.

    Refuses an unregistered action, and refuses any registered action whose domain is
    not in SAFE_DOMAINS or is in FOUNDATION_DOMAINS. Both halves matter: the first
    stops a new action being reachable before anyone reviews its domain, the second
    stops a domain being renamed into the safe set.
    """
    action = ACTIONS.get(action_id)
    if action is None:
        raise Refused(f"{TRIGGER_UNREGISTERED}: {action_id!r} is not a registered action")
    if action.domain in FOUNDATION_DOMAINS:
        raise Refused(
            f"{TRIGGER_FOUNDATION}: {action.id!r} touches {action.domain!r}, which "
            f"always escalates to a human"
        )
    if action.domain not in SAFE_DOMAINS:
        raise Refused(
            f"{TRIGGER_FOUNDATION}: {action.id!r} touches {action.domain!r}, which is "
            f"not in SAFE_DOMAINS; anything ambiguous escalates"
        )
    return action


# ---------------------------------------------------------------------------
# Verification comparators. A closed set of two, because a comparator that cannot
# fail is not a verification: the previous build shipped INCREASED and BECAME_TRUE
# unpinned, and either could have been weakened to accept "no change" with the whole
# suite still green.
# ---------------------------------------------------------------------------
INCREASED = "increased"


@dataclass(frozen=True)
class Expect:
    comparator: str
    key: str

    def check(self, before, after):
        b = before.require(self.key)
        a = after.require(self.key)
        if self.comparator == INCREASED:
            if not a > b:
                raise Refused(
                    f"{self.key} did not increase ({b} -> {a}), so nothing may claim "
                    f"the fix worked"
                )
            return f"{self.key} rose from {b} to {a}"
        raise Refused(f"unknown comparator {self.comparator!r}")


# ---------------------------------------------------------------------------
# THE CONDITION REGISTRY.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Condition:
    id: str
    probe_id: str
    applies: object              # callable(Snapshot) -> bool
    report: tuple                # ordered reading keys; each MUST have a sentence
    describe: str                # for the human card. Never sent to a client.
    action: str = ""             # "" == no action exists; only a measurement + a question
    expect: object = None        # Expect, required whenever action is set
    ask: str = ""                # key in readings.ASKS, appended last


CONDITIONS = {}


def _cond(c):
    if c.id in CONDITIONS:
        raise ValueError(f"duplicate condition {c.id!r}")
    CONDITIONS[c.id] = c
    return c


def _drive_singleton(s):
    """The only shape any Drive sentence in this capability is true for: exactly one
    active source, and no disagreement about who owns the rows.

    Both halves are live-motivated, not defensive padding (measured 2026-09-07):
    train7164ae502 has SIX active sources today, and two gyms' media_source.gym_id
    disagrees with their media_asset.gym_id. Every Drive sentence here says "your
    folder", singular, so anything else has no true sentence and escalates.
    """
    return (s.get(_r.DRIVE_ACTIVE_SOURCES.key) == 1
            and not s.get(_r.DRIVE_IDENTITY_SPLIT.key))


COND_DRIVE_EMPTY = _cond(Condition(
    id="drive_library_empty",
    probe_id=_p.PROBE_DRIVE,
    describe=("one active Drive folder, share intact, and nothing in the library the "
              "post selector can use -- the sync has not run since the folder was "
              "connected"),
    applies=lambda s: (_drive_singleton(s)
                       and not s.get(_r.DRIVE_SOURCE_REVOKED.key)
                       and s.get(_r.DRIVE_LIBRARY_USABLE.key) == 0),
    action=ACTION_GYM_DRIVE_SYNC,
    expect=Expect(INCREASED, _r.DRIVE_LIBRARY_USABLE.key),
    report=(_r.DRIVE_SYNC_RAN.key, _r.DRIVE_FILES_ADDED.key,
            _r.DRIVE_LIBRARY_USABLE.key),
))

COND_DRIVE_REVOKED = _cond(Condition(
    id="drive_share_revoked",
    probe_id=_p.PROBE_DRIVE,
    describe=("the gym's single Drive folder is no longer shared with Echo; only the "
              "gym owner can re-share it, so there is nothing for Echo to fix"),
    applies=lambda s: (_drive_singleton(s)
                       and bool(s.get(_r.DRIVE_SOURCE_REVOKED.key))),
    action="",
    expect=None,
    report=(_r.DRIVE_SOURCE_REVOKED.key, _r.DRIVE_LIBRARY_USABLE.key),
    ask="drive_reshare",
))

COND_CTA_EMPTY = _cond(Condition(
    id="cta_pool_empty",
    probe_id=_p.PROBE_CTA,
    describe=("the gym's brand voice doc yields zero usable CTAs from the extractor "
              "the caption pipeline consumes. THERE IS NO FIX ECHO MAY PERFORM: a CTA "
              "is the client's own booking link, phone number or offer, and inventing "
              "one violates CLAUDE.md's client-content-only rule. The correct "
              "behaviour is to ask."),
    applies=lambda s: (bool(s.get(_r.VOICE_DOC_PRESENT.key))
                       and s.get(_r.CTA_POOL_COUNT.key) == 0),
    action="",
    expect=None,
    report=(_r.CTA_POOL_COUNT.key,),
    ask="cta_needed",
))


def assert_registry_wellformed():
    """The three hard invariants. Called on every compose(), not only in tests."""
    problems = []
    for c in CONDITIONS.values():
        if c.probe_id not in _p.ALL_PROBES:
            problems.append(f"{c.id}: unknown probe {c.probe_id!r}")
        if c.action:
            if c.action not in ACTIONS:
                problems.append(f"{c.id}: unregistered action {c.action!r}")
            if c.expect is None:
                problems.append(
                    f"{c.id}: declares action {c.action!r} but no expectation, so its "
                    f"effect could never be verified and it must not reply"
                )
        if c.expect is not None and not c.action:
            problems.append(f"{c.id}: expectation with no action")
        if not c.report:
            problems.append(f"{c.id}: reports nothing, so it has no reply to compose")
        for key in c.report:
            r = _r.READINGS.get(key)
            if r is None:
                problems.append(f"{c.id}: reports unknown reading {key!r}")
            elif not r.speakable():
                problems.append(
                    f"{c.id}: reports {key!r}, which has no client-facing sentence. A "
                    f"reading with no `say` may inform a decision and may go on the "
                    f"human card, but Echo may not speak it."
                )
        if c.ask and c.ask not in _r.ASKS:
            problems.append(f"{c.id}: unknown ask {c.ask!r}")
    for a in ACTIONS.values():
        if a.domain in FOUNDATION_DOMAINS or a.domain not in SAFE_DOMAINS:
            problems.append(
                f"action {a.id!r} is registered with domain {a.domain!r}, which the "
                f"foundation gate always refuses; it must not be reachable at all"
            )
    if problems:
        raise Refused("condition registry is malformed: " + "; ".join(problems))
    return True


def match(snapshot):
    """The condition this gym's measured state is in, or None.

    Exactly one, or none. Two conditions matching the same readings is an ambiguity,
    and an ambiguity escalates -- it never picks the first.
    """
    hits = [c for c in CONDITIONS.values()
            if c.probe_id == snapshot.probe_id and bool(c.applies(snapshot))]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise Refused(
            "more than one condition matches these readings "
            f"({', '.join(sorted(h.id for h in hits))}); ambiguity escalates"
        )
    return None


# ---------------------------------------------------------------------------
# THE GROUNDED REPLY.
# ---------------------------------------------------------------------------
MAX_REPLY_CHARS = 700


def compose(condition, snapshot):
    """The ONLY producer of client-facing reply text in this capability.

    The reply is the concatenation, in `report` order, of each reading's registered
    sentence rendered from the value THIS snapshot carries, plus at most one registered
    ask. There is no other path: no template string, no model output, no interpolation
    of a client-supplied value, nothing this function was handed as text.

    Returns (text, audit). Raises Refused rather than returning anything partial.
    """
    assert_registry_wellformed()
    if not isinstance(snapshot, _r.Snapshot):
        raise Refused(f"compose() needs a Snapshot, got {type(snapshot).__name__}")
    if snapshot.probe_id != condition.probe_id:
        raise Refused(
            f"condition {condition.id!r} is grounded in {condition.probe_id!r} but the "
            f"snapshot came from {snapshot.probe_id!r}"
        )
    if condition.action and snapshot.stage != "verification":
        raise Refused(
            f"condition {condition.id!r} performs an action, so its reply may only be "
            f"composed from a VERIFICATION snapshot; got stage {snapshot.stage!r}"
        )
    parts, used = [], []
    for key in condition.report:
        reading = _r.READINGS[key]
        try:
            value = snapshot.require(key)
            parts.append(reading.sentence(value))
        except _r.Ungrounded as e:
            raise Refused(f"{condition.id}: {e}") from e
        used.append((key, value))
    if condition.ask:
        parts.append(_r.ASKS[condition.ask])
    text = " ".join(parts)
    if len(text) > MAX_REPLY_CHARS:
        raise Refused(
            f"composed reply is {len(text)} chars, over the {MAX_REPLY_CHARS} cap"
        )
    assert_is_grounded(text, condition, snapshot)
    return text, {
        "condition_id": condition.id,
        "probe_id": condition.probe_id,
        "gym_key": snapshot.gym_key,
        "stage": snapshot.stage,
        "readings": dict(used),
        "ask": condition.ask,
        "action": condition.action,
    }


def assert_is_grounded(candidate, condition, snapshot):
    """THE GATE. Byte-identical to the reconstruction, or refuse.

    Rather than inspecting a candidate reply and deciding whether each sentence is
    traceable -- which would be enumerating an open set, since the space of sentences
    is open -- this rebuilds what the registry WOULD say from these readings and
    demands exact equality. One extra clause, one softened hedge, one appended "and
    we'll keep an eye on it" changes a byte and the whole reply is refused.

    Note honestly what this proves and what it does not: it proves every sentence in
    the reply is a registered sentence rendered from a value this snapshot actually
    carries. It cannot prove the READING measures what its name says -- that is
    readings.py's job, which is why measurement and sentence live in one object there.
    """
    parts = []
    for key in condition.report:
        parts.append(_r.READINGS[key].sentence(snapshot.require(key)))
    if condition.ask:
        parts.append(_r.ASKS[condition.ask])
    expected = " ".join(parts)
    if candidate != expected:
        raise Refused(
            "the candidate reply is not a byte-identical reconstruction from the "
            f"registered sentences of {condition.id!r} over these readings, so it "
            f"contains at least one claim not traceable to a measurement "
            f"(expected {len(expected)} chars, got {len(candidate or '')})"
        )
    return expected
