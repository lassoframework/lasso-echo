"""
verify.py — re-run the SAME diagnostic and prove the fact actually moved.

D68's fourth "built but not wired" instance was `verification_after`: a fix-verified
notify path where no producer ever wrote the column, so the lane read "no fix has been
verified yet" forever and looked healthy doing it. The lesson recorded there is to
name the PRODUCER of the value you gate on. Here the producer is explicit and single:
diagnostics.run(<the same diagnostic id>, stage='verification'). Nothing else can
produce a verification, and check() refuses one that came from anywhere else.

The expectation is declared BEFORE the remedy executes, by remedies.plan(). That
ordering is deliberate: an expectation chosen after seeing the result is not a test,
it is a description. So the remedy commits to "media_asset_count must rise above 0"
first, and then either that happened or the flow escalates.

There is no partial success. verified is a bool. A remedy that ran without error but
did not move the fact is NOT verified, and a reply claiming a fix cannot be composed,
because reply.py's action-claiming templates all require a run fact that only a real
verified execution supplies.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import facts as _facts

# Comparators. A comparator not listed here is a programming error, not a
# permissive default.
ROSE_ABOVE_ZERO = "rose_above_zero"
INCREASED = "increased"
BECAME_TRUE = "became_true"
BECAME_FALSE = "became_false"

ALL_COMPARATORS = frozenset({ROSE_ABOVE_ZERO, INCREASED, BECAME_TRUE, BECAME_FALSE})


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Expectation:
    """What the remedy commits to changing, declared before it runs."""

    fact_key: str
    comparator: str

    def __post_init__(self):
        if self.fact_key not in _facts.ALL_FACT_KEYS:
            raise VerificationError(
                f"expectation names {self.fact_key!r}, not a key in ALL_FACT_KEYS"
            )
        if self.comparator not in ALL_COMPARATORS:
            raise VerificationError(
                f"unknown comparator {self.comparator!r}; the set is "
                f"{sorted(ALL_COMPARATORS)}"
            )


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    reason: str
    fact_key: str = ""
    before: object = None
    after: object = None
    run_facts: dict = field(default_factory=dict)


def _fail(reason, **kw):
    return VerificationResult(False, reason, **kw)


def check(expectation, before, after, *, run_facts=None):
    """True only when the SAME diagnostic, re-run after the remedy, shows the fact
    moved in the committed direction.

    Every guard below fails closed. In particular a caller who forgets to re-run the
    diagnostic and passes the diagnosis snapshot twice gets a refusal, not a pass:
    `after.stage` must be 'verification'.
    """
    if expectation is None:
        return _fail("no expectation was declared, so nothing can be verified")
    if not isinstance(before, _facts.GroundingSnapshot) or \
       not isinstance(after, _facts.GroundingSnapshot):
        return _fail("verification needs two GroundingSnapshots")
    if after.stage != "verification":
        return _fail(
            f"the post-fix snapshot has stage {after.stage!r}; a verification must be "
            f"a fresh re-run of the diagnostic, not a re-read of the diagnosis"
        )
    if before.stage != "diagnosis":
        return _fail(f"the pre-fix snapshot has stage {before.stage!r}, expected 'diagnosis'")
    if before.diagnostic_id != after.diagnostic_id:
        return _fail(
            f"verified with a DIFFERENT diagnostic ({before.diagnostic_id!r} then "
            f"{after.diagnostic_id!r}); 'verified' may never mean a friendlier query agreed"
        )
    if before.gym_key != after.gym_key:
        return _fail(
            f"snapshots are for different gyms ({before.gym_key!r} vs {after.gym_key!r})"
        )

    k = expectation.fact_key
    if k not in before or k not in after:
        return _fail(f"fact {k!r} is absent from one of the snapshots", fact_key=k)

    b, a = before.get(k), after.get(k)
    c = expectation.comparator

    if c == ROSE_ABOVE_ZERO:
        ok = isinstance(b, int) and isinstance(a, int) and b == 0 and a > 0
        why = (f"{k}: {b} -> {a}" if ok else
               f"{k} was expected to rise from 0 to >0 but went {b} -> {a}")
    elif c == INCREASED:
        ok = isinstance(b, (int, float)) and isinstance(a, (int, float)) and a > b
        why = f"{k}: {b} -> {a}" if ok else f"{k} did not increase ({b} -> {a})"
    elif c == BECAME_TRUE:
        ok = (b is False) and (a is True)
        why = f"{k}: {b} -> {a}" if ok else f"{k} did not become true ({b} -> {a})"
    elif c == BECAME_FALSE:
        ok = (b is True) and (a is False)
        why = f"{k}: {b} -> {a}" if ok else f"{k} did not become false ({b} -> {a})"
    else:  # unreachable: Expectation validates the comparator on construction
        return _fail(f"unknown comparator {c!r}", fact_key=k, before=b, after=a)

    return VerificationResult(
        verified=bool(ok), reason=why, fact_key=k, before=b, after=a,
        run_facts=dict(run_facts or {}),
    )


def verified_snapshot(after, run_facts):
    """The snapshot a reply is actually composed from: the post-fix facts PLUS the
    run facts that prove the fix executed. Only produced on a verified path, which is
    why an action-claiming template (which requires a run fact) is structurally unable
    to render off an unverified one."""
    merged = dict(after.facts)
    for k, v in dict(run_facts or {}).items():
        merged[k] = v
    return _facts.GroundingSnapshot.build(
        after.diagnostic_id, "verification", after.gym_key, merged
    )
