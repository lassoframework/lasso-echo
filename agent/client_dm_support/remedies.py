"""
remedies.py — plan a remedy from FACTS, then execute it if the scope gate allows.

Two halves, deliberately separate:

  plan()     reads the grounding snapshot and returns a Remedy: the ProposedAction,
             the reply template to use if it succeeds, and the Expectation the remedy
             commits to. plan() writes nothing and never sees the client's sentence --
             it decides from the enumerated facts only.

  execute()  runs the remedy's executor, but ONLY after scope_gate.check() allows the
             ProposedAction. The gate is called inside execute(), not merely by the
             caller, so there is no path to an executor that skips it.

THE CASE-2 OUTCOME IS FIRST-CLASS. Some diagnoses have a real, executable, data-only
fix (Case 1: run this gym's Drive sync). Others correctly conclude "there is no fix I
can perform, only a question I can ask" -- and that is a REMEDY here, not a failure:
KIND_ASK_CLIENT, with a reply template that states the real diagnosis and asks for the
information only the client has. It never writes a voice doc; scope_gate blocks every
brand_voice/ path, and no executor in this file opens one for writing.

NOTHING HERE IMPORTS OR CALLS AN AD SURFACE. See ad_block.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import diagnostics as _diag
from . import scope_gate as _gate
from . import verify as _verify


class RemedyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Remedy:
    id: str
    action: _gate.ProposedAction
    reply_template_id: str
    expectation: object = None          # verify.Expectation, or None for non-writing kinds
    executor: str = ""                  # key into EXECUTORS; empty for non-writing kinds
    note: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    reason: str
    run_facts: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# PLANNING — facts in, remedy out. No client text anywhere in this function.
# ---------------------------------------------------------------------------
def plan(snapshot):
    """Return a Remedy, or None when the facts do not match any enumerated remedy.

    None means ESCALATE. There is no fallback remedy and no 'general' branch: a fact
    pattern this function does not recognise has nowhere to match, which is the
    refuse-by-default shape D68 requires.
    """
    d = snapshot.diagnostic_id
    key = snapshot.gym_key
    f = snapshot.facts

    if d == _diag.DIAG_DRIVE_PHOTOS:
        return _plan_drive(key, f)
    if d == _diag.DIAG_CTA_POOL:
        return _plan_cta(key, f)
    return None


def _plan_drive(key, f):
    # The lane is not even armed for this gym -> shared config decision, not ours.
    if not f.get("drive_lane_active_for_gym"):
        return None
    # No source at all, or an inactive one -> the client has not finished connecting;
    # nothing for Echo to run, and the honest reply is not in the registry, so escalate.
    if not f.get("media_source_present") or not f.get("media_source_active"):
        return None

    # The share was revoked externally. Echo cannot fix a permission on the client's
    # own Google account: state the fact, ask them to re-share.
    if f.get("media_source_revoked"):
        return Remedy(
            id="drive_revoked_tell",
            action=_gate.ProposedAction(
                kind=_gate.KIND_ASK_CLIENT,
                summary="tell the gym their Drive share was revoked; ask them to re-share",
            ),
            reply_template_id="drive_revoked",
            note="Echo has no way to restore a share on the client's own Drive.",
        )

    # Assets already exist -> nothing was broken; no enumerated remedy claims this,
    # so it escalates rather than getting a reassuring reply nobody verified.
    if int(f.get("media_asset_count") or 0) > 0:
        return None

    # THE CASE-1 SHAPE. Active, un-revoked source, zero assets. Run THIS gym's sync
    # now rather than telling them to wait for the nightly slot.
    return Remedy(
        id="drive_sync_now",
        action=_gate.ProposedAction(
            kind=_gate.KIND_PER_GYM_SYNC,
            summary="run the existing gym-media Drive sync for this one gym's own source",
            tables=("media_source", "media_asset"),
            scope_column="gym_id",
            scope_values=(key,),
        ),
        reply_template_id="drive_synced",
        expectation=_verify.Expectation("media_asset_count", _verify.ROSE_ABOVE_ZERO),
        executor="per_gym_drive_sync",
        note="Touches only this gym's media_source/media_asset rows.",
    )


def _plan_cta(key, f):
    # THE CASE-2 SHAPE. The section exists and is the unfilled onboarding TODO. There
    # is NO executable fix: a CTA is the client's own booking link / phone / offer, and
    # inventing one would break CLAUDE.md's hardest rule. So the remedy is a question.
    if f.get("cta_section_present") and f.get("cta_section_is_todo"):
        return Remedy(
            id="cta_ask_client",
            action=_gate.ProposedAction(
                kind=_gate.KIND_ASK_CLIENT,
                summary="ask the gym for their real CTAs; write nothing",
            ),
            reply_template_id="cta_ask",
            note="No data-only fix exists. Echo may not invent a CTA.",
        )
    if f.get("voice_doc_present") and not f.get("cta_section_present"):
        return Remedy(
            id="cta_ask_client_no_section",
            action=_gate.ProposedAction(
                kind=_gate.KIND_ASK_CLIENT,
                summary="ask the gym for their real CTAs; write nothing",
            ),
            reply_template_id="cta_missing_section",
            note="No data-only fix exists. Echo may not invent a CTA.",
        )
    # A pool that already has entries, or a missing voice doc, is not this remedy's
    # shape. Escalate.
    return None


# ---------------------------------------------------------------------------
# EXECUTION
# ---------------------------------------------------------------------------
def _exec_per_gym_drive_sync(remedy, *, gym_key, store=None, sync_source=None,
                             log=None, **_kw):
    """Run the EXISTING nightly sync -- agent/jobs/sync_gym_media.sync_source -- over
    exactly this gym's own active gym_drive sources, and report what it inserted.

    Scoped three ways: list_sources is filtered by gym_id, each row's gym_id is
    re-checked here (a store fake or a bad filter cannot widen it), and the store's
    own list_assets requires a gym_id. Nothing global runs; the nightly `run()` that
    walks every gym is deliberately NOT called.
    """
    log = log or (lambda m: None)
    if store is None:                       # the production default; see diagnostics.py
        from .. import gym_media_index as _idx
        store = _idx.default_store()
    if sync_source is None:
        from ..jobs import sync_gym_media as _job
        sync_source = _job.sync_source

    sources = [
        s for s in store.list_sources(gym_id=gym_key)
        if str(s.get("kind") or "gym_drive") == "gym_drive"
        and str(s.get("gym_id") or "") == gym_key      # second, independent scope check
    ]
    if not sources:
        return ExecutionResult(False, f"no active gym_drive source for {gym_key!r}")

    inserted = 0
    ran = 0
    for s in sources:
        try:
            summary = sync_source(s, store=store, log=log) or {}
        except Exception as e:  # noqa: BLE001 - one source never sinks the remedy
            log(f"sync_source failed for {gym_key}: {type(e).__name__}: {e}")
            return ExecutionResult(False, f"sync failed: {type(e).__name__}")
        ran += 1
        if summary.get("revoked"):
            return ExecutionResult(False, "the Drive share is revoked; sync cannot read it")
        inserted += int(summary.get("inserted") or 0)

    return ExecutionResult(
        True,
        f"ran the Drive sync over {ran} source(s) for {gym_key}",
        run_facts={"sync_ran": True, "assets_inserted_this_run": int(inserted)},
    )


EXECUTORS = {
    "per_gym_drive_sync": _exec_per_gym_drive_sync,
}


def execute(remedy, **deps):
    """Run a remedy's executor, but only after the scope gate allows its action.

    The gate call lives HERE rather than only in flow.py so that there is no second
    caller that could reach an executor without it.
    """
    if remedy is None:
        return ExecutionResult(False, "no remedy to execute")

    verdict = _gate.check(remedy.action)
    if not verdict.allowed:
        return ExecutionResult(
            False,
            f"scope gate refused ({verdict.trigger}): {verdict.reason}",
        )

    if remedy.action.kind in _gate.NON_WRITING_KINDS:
        # Nothing to run. This is a real, successful outcome (Case 2), not a failure.
        return ExecutionResult(True, f"{remedy.action.kind}: nothing to execute",
                               run_facts={})

    fn = EXECUTORS.get(remedy.executor)
    if fn is None:
        return ExecutionResult(False, f"no executor registered for {remedy.executor!r}")
    return fn(remedy, **deps)
