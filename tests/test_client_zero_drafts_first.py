"""Client zero must be drafted before the fleet sweeps, and stay that way.

2026-09-04. lasso_ig had gone eight days without publishing and three days without so
much as a heartbeat, while the client-gym lane ran fine on every one of those days. The
cause was ordering, not a bug in any lane: the static account loop sat at the very END of
run_daily, after roughly thirty fleet-wide, network-bound maintenance sweeps. A deploy or
restart landing mid-draw therefore cut the run off before it ever reached LASSO's own
accounts. The echo service took six deploys before noon that day.

That is a structural property of the function, so it gets a structural test. This file
parses runner.py and asserts the ordering directly -- a behavioural test would pass just
as happily with the loop back at the bottom.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

RUNNER = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent", "runner.py")

# Sweeps that are client-gym or fleet maintenance: the account loop reads nothing they
# write, and every one of them makes network calls that can be cut off mid-run.
FLEET_SWEEP_MARKERS = (
    "client_media_sync_enabled",
    "gbp_conn_sync_enabled",
    "gbp_month_sweep_enabled",
    "account_key_doctor",
    "plan_horizon",
    "media_repeat",
    "learning_loop_enabled",
)

# The only three sweeps a LASSO draft actually depends on. These are allowed above it --
# the welcome trigger fills the queue the loop's WELCOME DRIP pops, and the other two
# refresh inputs the caption path reads.
LASSO_FEEDERS = ("welcome_queue_enabled", "mentions_enabled",
                 "podcast_library_index_enabled")


def _run_daily_body():
    tree = ast.parse(open(RUNNER).read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_daily")
    return fn.body


def _account_loop_index(body):
    for i, st in enumerate(body):
        if isinstance(st, ast.For) and getattr(st.target, "id", "") == "account":
            return i
    raise AssertionError("the account loop vanished from run_daily")


def _index_of_marker(body, marker):
    for i, st in enumerate(body):
        if marker in ast.dump(st):
            return i
    return None


def test_the_account_loop_runs_before_every_fleet_sweep():
    body = _run_daily_body()
    loop = _account_loop_index(body)
    for marker in FLEET_SWEEP_MARKERS:
        at = _index_of_marker(body, marker)
        if at is None:
            continue  # a sweep may be renamed or removed; that is not this test's business
        assert loop < at, (
            f"the account loop (statement {loop}) must run BEFORE the {marker} sweep "
            f"(statement {at}). Client zero is drafted last again, which is exactly how "
            f"lasso_ig went eight days without publishing.")


def test_the_three_sweeps_a_lasso_draft_depends_on_still_run_above_it():
    """The other half of the invariant: moving the loop up must not have moved it above
    the welcome queue it pops from."""
    body = _run_daily_body()
    loop = _account_loop_index(body)
    for marker in LASSO_FEEDERS:
        at = _index_of_marker(body, marker)
        assert at is not None, f"{marker} disappeared from run_daily"
        assert at < loop, (
            f"{marker} feeds the LASSO draft and must run BEFORE the account loop")


def test_the_loop_reads_nothing_the_fleet_sweeps_write():
    """The data-flow argument the reorder rests on, pinned so it cannot rot: every free
    name the loop reads is assigned in the prologue, above every sweep."""
    body = _run_daily_body()
    loop_idx = _account_loop_index(body)
    loop = body[loop_idx]
    used = {n.id for n in ast.walk(loop)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    feeders = {_index_of_marker(body, m) for m in LASSO_FEEDERS}
    for i, st in enumerate(body[:loop_idx]):
        if i in feeders:
            continue
        assigned = {n.id for n in ast.walk(st)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        # Prologue statements are fine; what must never appear is a SWEEP that assigns a
        # name the loop then reads. The prologue is everything before the first feeder.
        if i < min(f for f in feeders if f is not None):
            continue
        assert not (assigned & used), (
            f"statement {i} assigns {assigned & used}, which the account loop reads -- "
            f"the reorder's data-flow argument no longer holds")
