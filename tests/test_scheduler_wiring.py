"""
Scheduler WIRING pins — proof that the guards are actually CALLED.

THE GAP THIS CLOSES (audit 2026-08-31, measured, not guessed). Echo's guards are good
and most are individually well tested. But nothing anywhere asserted that they are
REACHED. The auditor deleted call sites one at a time from a throwaway copy and ran the
suite:

    deleted `publish_client_gyms(...)` from listener  -> 936 tests passed
      (that is THE ENTIRE CLIENT PUBLISH LANE, silently removable)
    deleted `sweep_stuck_publishing()` from listener  -> 936 tests passed
    deleted the billing-gate call site                -> 780 tests passed
    deleted `daily_cap=config.client_daily_publish_cap()` -> 780 tests passed

Every one of those is a whole class of bug re-armed by a single deleted line, with a
fully green suite. Unit tests pin what a function DOES; these pin that it is WIRED.

They are deliberately structural (an AST walk of the scheduler modules) rather than
behavioural. A behavioural test would need to boot the listener loop, and the thing most
worth protecting here is exactly the line that a refactor drops by accident.

Adding a lane? Add it here too. A lane nothing pins is a lane that can vanish.
"""

import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_AGENT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent")


def _calls_in(module_filename):
    """Every called name in the module, as {"attr_or_func_name", ...}, plus the set of
    keyword argument names used at each call, as {(call_name, kwarg), ...}."""
    with open(os.path.join(_AGENT, module_filename), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names, kwargs = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if not name:
            continue
        names.add(name)
        for kw in node.keywords:
            if kw.arg:
                kwargs.add((name, kw.arg))
    return names, kwargs


@pytest.fixture(scope="module")
def listener_calls():
    return _calls_in("listener.py")


@pytest.fixture(scope="module")
def runner_calls():
    return _calls_in("runner.py")


@pytest.fixture(scope="module")
def autopublish_calls():
    return _calls_in("calendar_autopublish.py")


# ---- the publish lanes themselves --------------------------------------------------

@pytest.mark.parametrize("lane", [
    "publish_client_gyms",      # THE client publish lane. Deleting it published nothing
                                # for any gym, and 936 tests still passed.
    "run_slot_ticks",           # the LASSO slot lane
    "sweep_stuck_publishing",   # the only thing that ever notices a stranded claim
    "sweep_expired_rows",       # the only thing that notices a row that can never post
])
def test_the_listener_still_calls_its_publish_lanes(listener_calls, lane):
    names, _ = listener_calls
    assert lane in names, (
        f"agent/listener.py no longer calls {lane}(). Every guard downstream of it is "
        "now unreachable, and the rest of the suite cannot tell.")


@pytest.mark.parametrize("lane", [
    "run",                      # the watchdog sweeps below all present as .run()
])
def test_the_runner_daily_still_runs_sweeps(runner_calls, lane):
    names, _ = runner_calls
    assert lane in names


# ---- the guards that ride INSIDE the publish lane ----------------------------------

def test_the_billing_gate_is_still_consulted_before_publishing(autopublish_calls):
    """Deleting this call site left 780 tests green. It is the ONLY consultation of the
    billing gate anywhere in the publish path."""
    names, _ = autopublish_calls
    assert "publishing_blocked" in names, (
        "calendar_autopublish no longer calls publishing_blocked(): a canceled gym's "
        "posts publish again, and no unit test notices.")


def test_the_daily_flood_cap_is_still_passed_to_publish_due(autopublish_calls):
    """The cap is a PARAMETER one caller happens to pass, not a rule at the choke point.
    Deleting the kwarg silently uncaps the fleet; 780 tests stayed green."""
    _, kwargs = autopublish_calls
    assert ("publish_due", "daily_cap") in kwargs, (
        "publish_client_gyms no longer passes daily_cap= to publish_due(): the "
        "per-gym daily flood cap is off for every gym, silently.")


def test_the_per_gym_timezone_is_still_resolved_in_the_publish_lane(autopublish_calls):
    """Slot/timezone drift is a recurring class. The per-gym resolver must stay wired;
    falling back to the global tz posts gyms on the wrong wall clock."""
    names, _ = autopublish_calls
    assert "posting_timezone_for" in names, (
        "calendar_autopublish no longer resolves the per-gym posting timezone: every "
        "gym would publish on the global default clock.")


def test_the_media_and_meta_rails_are_still_wired_into_the_publish_lane(autopublish_calls):
    """The [why]/edit-rationale strip and the cross-day media guard both live in this
    lane. Neither has a test that notices if the CALL disappears."""
    names, _ = autopublish_calls
    assert "split_meta_suffix" in names, (
        "the publish lane no longer strips the edit-rationale meta suffix: a [why] "
        "block can ship in a live caption again.")


# ---- the nightly integrity watchdogs ----------------------------------------------
# Each of these exists precisely because its failure mode is SILENT. A watchdog nothing
# calls is worse than no watchdog: it shows up in the flag report as armed.

@pytest.mark.parametrize("module_name", [
    "account_key_doctor",        # base -> one live gym (the stranding class)
    "account_key_split_watch",   # portal key vs content key (the split-brain class)
    "zernio_failed_watch",       # Zernio says FAILED, Echo says published
    "publish_billing_gate",      # the gate's own inertness self-report
    "onboarding_watch",          # a gym set up wrong on day one
    "zernio_profile_link",       # a gym with media but no publishable profile
])
def test_the_daily_run_still_imports_every_integrity_watchdog(module_name):
    """Structural pin on the IMPORT, because each watchdog is reached through a local
    `from . import x as y` inside run_daily's isolated try/except blocks."""
    with open(os.path.join(_AGENT, "runner.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # Both shapes are used in run_daily: `from . import x as y` (the module is
            # the alias) and `from .x import fn` (the module is node.module).
            if node.module:
                imported.add(node.module.lstrip(".").split(".")[-1])
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[-1])
    assert module_name in imported, (
        f"agent/runner.py no longer imports {module_name}: its nightly sweep never "
        "runs, and it still reports as armed in the flag list.")
