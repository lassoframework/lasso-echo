"""
tests/test_db_constraint_contract.py -- postmortem-driven regression guard (2026-09-05).

Two live production bugs (both found running a real end-to-end regression test, neither
caught by any prior unit test) shared the same root cause: Python code wrote a string
literal to a Postgres column whose CHECK constraint did not allow that literal. Every
existing test used a FakeBus with no real constraint enforcement, so a value the DB would
reject at the boundary was accepted silently in every test run.

  1. echo_ticket_worker.py wrote support_tickets.status='escalated' -- never a legal
     value (the constraint only ever allowed new/triage/fixing/verification/hold/
     approved/merged/resolved/failed). Every unresolved-identity or unclassifiable
     portal ticket raised a BusError on the very first write and silently retried
     forever, with no card ever reaching a human.
  2. bus.py's claim_message() (the outbox CAS) wrote support_messages.delivery_status=
     'posting' -- a transient state the CODE has always needed (bus.py's own
     compare-and-swap: ready -> posting -> posted, so two concurrent consumers can
     never double-post one row) but the migration that defined the constraint (0309)
     never included it, having been written from the STEADY states its own comment
     described, not the actual code path. Every claim on every row, across the entire
     history of this table, raised the same 400 and failed -- meaning ZERO messages had
     EVER successfully posted through this system before the bug was found.

WHAT WOULD HAVE CAUGHT THIS SOONER (see DECISIONS.md's postmortem entry for the full
account): a static check, run in CI with no live database, that every string literal the
Python code ever writes to one of these two constrained columns is a member of the SAME
set the live CHECK constraint actually allows -- not a value some docstring or comment
implies is allowed, and not inferred from what a FakeBus happens to accept. THIS is that
check. If a future migration widens either constraint, update the allow-list constants
below in the SAME PR that changes the SQL (a description of the code coupling), or a
future removal of an allowed value here without also removing every writer will fail this
test, exactly as intended -- both directions of drift are the point.
"""
import ast
import os

# Sourced directly from the live CHECK constraints (verified via
# pg_get_constraintdef against project ooqcvmcjspeltuuhcvlh, 2026-09-05), not from any
# migration file's comment -- migration 0309's own comment for delivery_status
# ("only ever posts a row in 'ready'") is exactly the kind of claim that hid bug #2
# above; the constraint definition itself is the only source of truth.
SUPPORT_TICKETS_STATUS_VALUES = frozenset({
    "new", "triage", "fixing", "verification", "hold", "approved", "merged",
    "resolved", "failed",
})
SUPPORT_MESSAGES_DELIVERY_STATUS_VALUES = frozenset({
    "drafted", "held", "ready", "posting", "posted", "suppressed", "failed",
})

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_DIR = os.path.join(_REPO_ROOT, "agent")

# Files known to write these two columns -- the actual writer surface, not the whole
# repo (config.py, tests, and unrelated modules never touch these tables).
_STATUS_WRITER_FILES = (
    "echo_ticket_worker.py",
    os.path.join("slack_convo", "adapter.py"),
    os.path.join("slack_convo", "outbox.py"),
    os.path.join("slack_convo", "bus.py"),
)


def _iter_call_string_args(tree, func_names):
    """Yield every plain string literal passed (positionally or by keyword) to a call
    whose callee's final attribute/name matches one of func_names -- e.g. a call written
    as `bus.set_ticket(...)` or `self.mark_message(...)` both match "set_ticket" /
    "mark_message" regardless of the receiver expression."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if name not in func_names:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield arg.value
        for kw in node.keywords:
            if kw.arg in ("status", "delivery_status") and isinstance(
                    kw.value, ast.Constant) and isinstance(kw.value.value, str):
                yield kw.value.value


def _parsed_writer_files():
    for rel in _STATUS_WRITER_FILES:
        path = os.path.join(_AGENT_DIR, rel)
        with open(path, encoding="utf-8") as fh:
            yield rel, ast.parse(fh.read(), filename=path)


def test_every_ticket_status_literal_written_is_a_live_allowed_value():
    """set_ticket(...) is the only writer of support_tickets.status. Every literal ever
    passed as its status= keyword must be one pg_get_constraintdef actually allows."""
    seen = set()
    for rel, tree in _parsed_writer_files():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set_ticket"):
                continue
            for kw in node.keywords:
                if kw.arg == "status" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    seen.add((rel, kw.value.value))
    assert seen, "expected at least one status= literal in the known writer files"
    bad = [(rel, v) for rel, v in seen if v not in SUPPORT_TICKETS_STATUS_VALUES]
    assert not bad, (
        f"these support_tickets.status literals are not in the live CHECK constraint "
        f"and would raise a BusError on write, silently, in production: {bad}")


def test_every_delivery_status_literal_written_is_a_live_allowed_value():
    """mark_message(...) (positional 2nd arg) and record_outbound(...)/set_ticket(...)-
    adjacent delivery_status= keywords are the writers of support_messages.delivery_status.
    Every literal ever passed must be one the live CHECK constraint actually allows --
    this is the exact class of bug that meant 'posting' silently broke every claim in
    this table's history before it was found."""
    seen = set()
    for rel, tree in _parsed_writer_files():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None)
            if name == "mark_message" and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    seen.add((rel, arg.value))
            for kw in node.keywords:
                if kw.arg == "delivery_status" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    seen.add((rel, kw.value.value))
        # bus.py's claim_message() writes the CAS transition as a raw dict literal
        # passed to json.dumps(...) -- the actual PATCH BODY, not a mark_message call
        # and not the params/filter dict (which uses PostgREST's "eq.<value>" filter
        # syntax on the SAME column name and would false-positive if not excluded by
        # only matching dicts that are direct json.dumps(...) arguments).
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "dumps"):
                continue
            for arg in node.args:
                if not isinstance(arg, ast.Dict):
                    continue
                for k, v in zip(arg.keys, arg.values):
                    if (isinstance(k, ast.Constant) and k.value == "delivery_status"
                            and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                        seen.add((rel, v.value))
    assert seen, "expected at least one delivery_status literal in the known writer files"
    bad = [(rel, v) for rel, v in seen
           if v not in SUPPORT_MESSAGES_DELIVERY_STATUS_VALUES]
    assert not bad, (
        f"these support_messages.delivery_status literals are not in the live CHECK "
        f"constraint and would raise a BusError on write, silently, in production: {bad}")


def test_the_transient_posting_state_is_actually_present_in_the_allowlist():
    """Guards the allow-list constant itself, not just the writers -- if a future edit
    to this file's SUPPORT_MESSAGES_DELIVERY_STATUS_VALUES accidentally drops 'posting'
    again, this fails immediately rather than waiting for the next live incident."""
    assert "posting" in SUPPORT_MESSAGES_DELIVERY_STATUS_VALUES


def test_the_correct_hold_pattern_is_actually_present_in_the_allowlist():
    """Same guard for the other bug: 'escalated' must NEVER be added back to this
    set -- the correct representation is status='hold' + the separate escalated=True
    boolean column, which this constant intentionally excludes."""
    assert "hold" in SUPPORT_TICKETS_STATUS_VALUES
    assert "escalated" not in SUPPORT_TICKETS_STATUS_VALUES
