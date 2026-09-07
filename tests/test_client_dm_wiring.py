"""
THE NOT-WIRED GUARD for agent/client_dm_support/.

D68 names this class and gives the cheap check for it:

    "For every capability, name the PRODUCER of the value it gates on, and assert that
     producer exists -- statically, in a test, with no test double and no live service."

This capability shipped an instance of it anyway, on its flagship case. `store` was a
required keyword-only argument with no default in BOTH diagnose_drive_photos and
_exec_per_gym_drive_sync, and nothing on the real path supplies one: runner.py calls
run_once() with no deps, consumer passes deps={}, and _diagnostic_kwargs therefore
passes nothing. So every Case-1 ticket -- the only executable remedy the capability has
-- died on

    TypeError: diagnose_drive_photos() missing 1 required keyword-only argument: 'store'

and escalated with that written on a card, which is byte-indistinguishable from a
healthy refusal. Six independent audits missed it because every test in the suite
injects `deps=drive_deps(store, sync)`: the fake filled the exact seam production left
empty, which is D68's sentence about this class, verbatim.

THE RULE THESE TESTS ASSERT: every injectable seam in this package has a real
production default, so calling it with NO deps at all fails for a REAL reason (no
credentials, no network) and never because an argument had nowhere to come from.

They are deliberately written against the rule rather than the code: they call the
production entry points with nothing, and inspect signatures for the shape that caused
it, so a new seam added the same way fails here rather than in a client's DM thread.
"""
import inspect

import pytest

from agent.client_dm_support import consumer, diagnostics, flow, remedies


# ---------------------------------------------------------------------------
# The shape that caused it: a required keyword-only arg on a production entry point.
# ---------------------------------------------------------------------------
INJECTABLE_ENTRY_POINTS = (
    diagnostics.diagnose_drive_photos,
    diagnostics.diagnose_cta_pool,
    remedies._exec_per_gym_drive_sync,
)


@pytest.mark.parametrize("fn", INJECTABLE_ENTRY_POINTS,
                         ids=lambda f: f.__name__)
def test_every_injectable_seam_has_a_production_default(fn):
    """A keyword-only parameter with no default is a seam only a test can fill."""
    sig = inspect.signature(fn)
    required = [
        name for name, p in sig.parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
        and p.default is inspect.Parameter.empty
    ]
    # gym_key is positional, and the executor legitimately requires it.
    required = [r for r in required if r != "gym_key"]
    assert required == [], (
        f"{fn.__name__} requires keyword arg(s) {required} with no default. Nothing on "
        f"the production path supplies them (runner -> run_once() -> deps={{}}), so "
        f"this seam can only ever be filled by a test."
    )


# ---------------------------------------------------------------------------
# The behaviour, which is what actually matters: call it with NOTHING.
# ---------------------------------------------------------------------------
def _failure_of(fn, *args, **kw):
    try:
        fn(*args, **kw)
        return None
    except Exception as e:  # noqa: BLE001
        return e


@pytest.mark.parametrize("diagnostic_id", sorted(diagnostics.ALL_DIAGNOSTICS))
def test_a_diagnostic_called_with_no_deps_never_fails_on_a_missing_seam(diagnostic_id):
    """It may fail for a REAL reason -- no Supabase credentials, no network, no file.
    It may not fail because an argument had nowhere to come from."""
    err = _failure_of(diagnostics.run, diagnostic_id, "crossfitlocal")
    if isinstance(err, TypeError) and "required keyword-only argument" in str(err):
        pytest.fail(f"{diagnostic_id} has an unwired seam: {err}")
    if isinstance(err, AttributeError) and "NoneType" in str(err):
        pytest.fail(f"{diagnostic_id} left a seam as None: {err}")


def test_the_diagnostic_actually_CALLS_the_named_store_producer(monkeypatch):
    """Signature-shape and 'it did not TypeError' are both too weak: a default of None
    satisfies both while leaving the seam unfilled, and the real failure then surfaces
    as AttributeError on NoneType. So assert the PRODUCER was invoked -- D68's question
    is 'what process, by name, supplies this value', and this is that question executed."""
    called = []

    class Probe:
        def list_sources(self, gym_id=None, include_inactive=False):
            called.append(("sources", gym_id))
            return []

        def list_assets(self, gym_id, source_id=None):
            called.append(("assets", gym_id))
            return []

    from agent import gym_media_index
    monkeypatch.setattr(gym_media_index, "default_store", lambda: Probe())
    diagnostics.diagnose_drive_photos("crossfitlocal", now="2026-09-06T21:45:00+00:00",
                                      daily_hour_utc=12, lane_active_for=lambda k: True)
    assert called, "the diagnostic never reached gym_media_index.default_store()"


def test_the_executor_called_with_no_deps_never_fails_on_a_missing_seam():
    err = _failure_of(remedies._exec_per_gym_drive_sync, None, gym_key="crossfitlocal")
    if isinstance(err, TypeError) and "required keyword-only argument" in str(err):
        pytest.fail(f"the per-gym sync executor has an unwired seam: {err}")
    if isinstance(err, AttributeError) and "NoneType" in str(err):
        pytest.fail(f"the per-gym sync executor left a seam as None: {err}")


def test_the_executor_actually_CALLS_the_named_store_producer(monkeypatch):
    called = []

    class Probe:
        def list_sources(self, gym_id=None, include_inactive=False):
            called.append(gym_id)
            return []

    from agent import gym_media_index
    monkeypatch.setattr(gym_media_index, "default_store", lambda: Probe())
    result = remedies._exec_per_gym_drive_sync(None, gym_key="crossfitlocal")
    assert called == ["crossfitlocal"], (
        "the executor never reached gym_media_index.default_store()")
    assert not result.ok        # no sources for this gym in the probe


def test_the_whole_flow_with_no_deps_never_escalates_on_a_missing_seam():
    """The end-to-end form, and the exact reproduction from the audit. An escalation
    whose reason is a TypeError about a missing argument is the inert state wearing a
    healthy refusal's clothes."""
    for text, key in (("the posts waiting for my approval have no photos",
                       "crossfitlocal"),
                      ("my posts have no call to action", "toughtemple52040e")):
        d = flow.handle_ticket(text=text, gym_key=key, deps=None)
        assert "required keyword-only argument" not in d.reason, (
            f"unwired seam reached the decision for {text!r}: {d.reason}")
        assert "TypeError" not in d.reason, d.reason


def test_the_named_producers_exist_with_no_double_and_no_live_service():
    """D68's question, asked directly: what process, BY NAME, supplies this value?"""
    from agent import gym_media_index, account_key_resolve
    from agent.jobs import sync_gym_media
    from agent.slack_convo import bus as slack_bus

    assert callable(gym_media_index.default_store)      # the store seam
    assert callable(sync_gym_media.sync_source)         # the executor's real fix
    assert callable(account_key_resolve.portal_key_for_gym)   # the gym-key seam
    assert callable(consumer.confirm_gym_binding)             # its second control
    assert callable(slack_bus.Bus)                            # the ticket + delivery bus


def test_the_flow_docstring_no_longer_claims_something_it_did_not_do():
    """It asserted "Every one of them has a real production default" while two seams
    had none. The claim is only allowed to stand because the tests above now hold it."""
    doc = inspect.getdoc(flow.handle_ticket) or ""
    if "real production default" in doc:
        for fn in INJECTABLE_ENTRY_POINTS:
            sig = inspect.signature(fn)
            bad = [n for n, p in sig.parameters.items()
                   if p.kind is inspect.Parameter.KEYWORD_ONLY
                   and p.default is inspect.Parameter.empty and n != "gym_key"]
            assert not bad, (fn.__name__, bad)


# ---------------------------------------------------------------------------
# HARD-LIMIT CONTROLS THAT NO TEST ASSERTED (both mutated GREEN on the full suite).
# ---------------------------------------------------------------------------
def test_the_schema_ops_blocklist_is_load_bearing_and_still_populated():
    """scope_gate.SCHEMA_OPS could be emptied and all 6528 tests stayed green."""
    from agent.client_dm_support import scope_gate as sg
    for op in ("create table", "alter table", "drop table", "create policy",
               "alter policy", "drop policy", "grant", "revoke", "migration", "rls"):
        assert op in sg.SCHEMA_OPS, op
    for sql in ("DROP TABLE media_asset", "alter table media_source add column x",
                "create policy p on media_asset", "apply migration 0312",
                "GRANT select ON media_asset TO anon", "enable rls on media_asset"):
        v = sg.check(sg.ProposedAction(
            kind=sg.KIND_DATA_PATCH, tables=("media_asset",),
            scope_column="gym_id", scope_values=("crossfitlocal",), sql_ops=(sql,)))
        assert v.escalate, sql
        assert v.trigger == sg.TRIGGER_SCHEMA, sql


def test_the_fixer_bus_path_fence_is_load_bearing_and_still_present():
    """Replacing "agent/slack_convo/" in BLOCKED_PATH_FRAGMENTS stayed green too. It is
    NOT redundant with ALLOWED_CODE_FIX_ROOTS: paths are only re-checked against those
    roots for KIND_CODE_FIX, so a non-code-fix action naming that path is caught by the
    blocklist alone."""
    from agent.client_dm_support import scope_gate as sg
    assert "agent/slack_convo/" in sg.BLOCKED_PATH_FRAGMENTS
    for kind in (sg.KIND_CODE_FIX, sg.KIND_PER_GYM_SYNC, sg.KIND_DATA_PATCH):
        v = sg.check(sg.ProposedAction(
            kind=kind, paths=("agent/slack_convo/adapter.py",),
            tables=("media_asset",) if kind != sg.KIND_CODE_FIX else (),
            scope_column="gym_id", scope_values=("crossfitlocal",)))
        assert v.escalate, kind


def test_every_blocked_path_fragment_is_still_present():
    """The two-way half: a future edit must not quietly narrow the list."""
    from agent.client_dm_support import scope_gate as sg
    for frag in ("migrations/", ".env", ".github/", "agent/config.py",
                 "agent/slack_convo/", "railway.json", "secrets", "credentials",
                 "brand_voice/"):
        assert frag in sg.BLOCKED_PATH_FRAGMENTS, frag


# ---------------------------------------------------------------------------
# The remaining round-6 fixes.
# ---------------------------------------------------------------------------
def test_no_template_claims_an_outcome_no_fact_key_measures():
    """media_asset_count counts every row the store returns, including eligible=False
    and eligible=None (unprobed video) rows that the selector excludes. "New posts will
    draw from those" was therefore false whenever the sync pulled in unprobed video --
    and the byte-identity gate cannot see it, because the false half was template prose,
    not an interpolated fact."""
    from agent.client_dm_support import reply
    for t in reply.TEMPLATES.values():
        low = t.text.lower()
        for claim in ("will draw from", "will be used", "will go out",
                      "from now on", "going forward"):
            assert claim not in low, (t.id, claim)


def test_one_tickets_exception_does_not_sink_the_whole_pass():
    """A Supabase 5xx on ONE gym's source read propagated out of run_once, dropping
    every other client's ticket in the pass with nothing written."""
    class Bus:
        def __init__(self):
            self.out = []

        def available(self):
            return True

        def _get(self, table, params):
            return [{"id": "BAD", "product": "echo", "source": "slack_conversation",
                     "status": "hold", "bot_identity": "echo", "client_id": "u1"},
                    {"id": "OK", "product": "echo", "source": "slack_conversation",
                     "status": "hold", "bot_identity": "echo", "client_id": "u2"}]

        def recent_messages(self, tid, limit=200):
            return [{"direction": "inbound", "author_type": "client",
                     "body": "my posts have no photos",
                     "attachments": {"surface": "mpim"}}]

        def record_outbound(self, **kw):
            self.out.append(kw)
            return {"id": len(self.out)}

    class ExplodingStore:
        """Succeeds for the DIAGNOSTIC and raises inside the EXECUTOR.

        That distinction is the whole point: flow._decide already guards step 4 (the
        diagnostic), so a store that raises on the first read is caught there and this
        test would pass with the consumer-level guard removed. The ungated path is step
        7 -- _rem.execute -> _exec_per_gym_drive_sync -> store.list_sources -- which
        neither flow nor the executor wraps. A Supabase 5xx there propagated out of
        run_once and dropped every other client's ticket in the pass."""

        def __init__(self):
            self.calls = 0

        def available(self):
            return True

        def list_sources(self, gym_id=None, include_inactive=False):
            self.calls += 1
            if self.calls > 1:                 # the executor's read, not the diagnostic's
                raise RuntimeError("supabase 500")
            return [{"id": 1, "gym_id": gym_id, "kind": "gym_drive",
                     "folder_name": "Ad Photos", "active": True,
                     "revoked_externally": False,
                     "connected_at": "2026-09-06T21:18:00+00:00"}]

        def list_assets(self, gym_id, source_id=None):
            return []

    bus = Bus()
    out = consumer.run_once(
        bus=bus, flag_on=True,
        deps={"store": ExplodingStore(), "sync_source": lambda s, **kw: {"ok": True},
              "now": "2026-09-06T21:45:00+00:00",
              "daily_hour_utc": 12, "lane_active_for": lambda k: True,
              "log": lambda m: None},
        portal_key_for_gym=lambda u: "gymone" if u == "u1" else "gymtwo",
        confirm_binding=lambda _g, _k: True,
        notice_sink=lambda _t, _d: None)
    assert out["handled"] == 2, out
    assert out["escalated"] == 2, out
    assert len(bus.out) == 2, "a human was told about neither ticket"


def test_the_poll_ceiling_is_reported_in_the_summary_not_only_in_an_alert():
    """{ok:True, handled:0} was byte-identical to a healthy idle pass, and runner.py
    only prints when ok is falsy — so the caller saw nothing at all."""
    tickets, msgs = [], {}
    for i in range(consumer.POLL_PAGE * consumer.POLL_MAX_PAGES + 20):
        tid = f"C{i}"
        tickets.append({"id": tid, "product": "echo", "source": "slack_conversation",
                        "status": "hold", "bot_identity": "echo", "client_id": "u1"})
        msgs[tid] = [{"direction": "inbound", "author_type": "client", "body": "hi",
                      "attachments": {"surface": "channel"}}]

    class Bus:
        def available(self):
            return True

        def _get(self, table, params):
            off = int(params.get("offset", 0) or 0)
            lim = int(params.get("limit", 50) or 50)
            return tickets[off:off + lim]

        def recent_messages(self, tid, limit=200):
            return list(msgs.get(tid, []))

        def record_outbound(self, **kw):
            return {"id": 1}

    out = consumer.run_once(bus=Bus(), flag_on=True, deps={},
                            portal_key_for_gym=lambda u: "gymone",
                            confirm_binding=lambda _g, _k: True,
                            notice_sink=lambda _t, _d: None,
                            log=lambda m: None)
    assert out["poll_ceiling_hit"] is True, out
    assert out["ok"] is False, "a truncated scan reported as a healthy pass"
    assert "ceiling" in out["reason"]


def test_confirm_gym_binding_requires_the_unique_owner_to_be_THIS_gym():
    """`gid` was computed and never used again in the uniqueness check."""
    from agent import account_key_resolve as akr
    real = akr.resolve
    akr.resolve = lambda k, **kw: k
    try:
        state = lambda: ({}, {}, {"other-gym": "sharedkey"}, True)   # noqa: E731
        assert consumer.confirm_gym_binding(
            "this-gym", "sharedkey", state=state,
            fresh_key=lambda _g: "sharedkey") is False
        assert consumer.confirm_gym_binding(
            "other-gym", "sharedkey", state=state,
            fresh_key=lambda _g: "sharedkey") is True
    finally:
        akr.resolve = real
