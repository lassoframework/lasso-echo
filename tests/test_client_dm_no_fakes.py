"""FAKE-INJECTION BLINDNESS, addressed directly.

Two defects survived SIX independent audit rounds of the previous build, and both hid
for the same reason: every test in the suite injected its own working fake into the
exact seam production left empty.

  * `store` was a required keyword-only argument with NO default. runner called
    run_once() with no deps, so the flagship remedy died in production on
    `TypeError: diagnose_drive_photos() missing 1 required keyword-only argument` and
    escalated with that on a card -- byte-indistinguishable from a healthy refusal.
  * the poll's fake ignored the query params, so a wrong status filter that matched
    zero real rows read as {ok: True, handled: 0}: byte-identical to a healthy idle
    pass.

So this file does three things no fixture-driven test can do:

  1. a STATIC check with no fake at all -- no public entry point may have a required
     parameter beyond the gym key. That catches the TypeError class outright.
  2. a NO-FAKE call of every probe, the action and the whole decision, asserting the
     NAMED PRODUCER was actually invoked -- not merely that nothing raised, because a
     default of None satisfies that while leaving the seam unfilled.
  3. a FAITHFUL fake for the poll: one that applies the query params it is given, so
     a selector that asks for the wrong rows fails here instead of reading as idle.
     (D68.3's technique is right for a FENCE, which keeps rows out; it is exactly
     wrong for a SELECTOR, which decides which rows come in.)
"""
import inspect

import pytest

from agent import gym_media_index as _idx
from agent import gym_media_selector as _sel
from agent import voice as _voice
from agent.client_dm_support import conditions as C
from agent.client_dm_support import lane as L
from agent.client_dm_support import probes as P


# ---------------------------------------------------------------------------
# 1. STATIC. No fake, no live service, no call.
# ---------------------------------------------------------------------------
_ENTRY_POINTS = [
    (P.probe_drive, ("gym_key",)),
    (P.probe_cta, ("gym_key",)),
    (C._gym_drive_sync, ("gym_key",)),          # noqa: SLF001
    # text / gym_key / may_reply are supplied by the caller for every ticket; they are
    # arguments, not seams. Everything else must default.
    (L.decide, ("text", "gym_key", "may_reply")),
    (L.run_once, ()),
]


@pytest.mark.parametrize("fn,required", _ENTRY_POINTS,
                         ids=[f.__name__ for f, _ in _ENTRY_POINTS])
def test_no_entry_point_has_a_seam_production_cannot_fill(fn, required):
    """Every parameter beyond the named required ones must have a default. This is
    the exact shape of the round-6 CRITICAL, caught statically."""
    sig = inspect.signature(fn)
    missing = [name for name, p in sig.parameters.items()
               if p.default is inspect.Parameter.empty
               and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
               and name not in required]
    assert not missing, (
        f"{fn.__name__} has parameter(s) {missing} with no production default; the "
        f"real path supplies nothing, so this dies with a TypeError and cards a human "
        f"-- which looks exactly like a healthy refusal.")


def test_every_named_production_default_resolves_to_a_real_callable():
    """The producers by name, imported for real."""
    assert callable(_idx.default_store)
    assert callable(_sel.is_usable)
    assert callable(_voice._extract_ctas)          # noqa: SLF001
    from agent.jobs.sync_gym_media import sync_source
    assert callable(sync_source)


# ---------------------------------------------------------------------------
# 2. NO-FAKE CALLS. The seam is filled by the real named producer, and we prove the
#    producer was reached rather than that nothing blew up.
# ---------------------------------------------------------------------------
class _RecordingStore:
    """Stands in for the object default_store() RETURNS -- not for the seam itself.
    The point of these tests is that the real `default_store` was CALLED."""

    def __init__(self, sources=(), assets=()):
        self.sources, self.assets = list(sources), list(assets)
        self.source_calls, self.asset_calls = [], []

    def list_sources(self, gym_id=None, include_inactive=False):
        self.source_calls.append((gym_id, include_inactive))
        rows = [s for s in self.sources if gym_id in (None, s["gym_id"])]
        return rows if include_inactive else [s for s in rows if s["active"]]

    def list_assets(self, gym_id, source_id=None):
        self.asset_calls.append((gym_id, source_id))
        return [a for a in self.assets if a["gym_id"] == gym_id]


def test_probe_drive_with_no_deps_calls_the_real_default_store(monkeypatch):
    store = _RecordingStore()
    called = []

    def spy():
        called.append(True)
        return store

    monkeypatch.setattr(_idx, "default_store", spy)
    snap = P.probe_drive("crossfitlocal")           # NO store kwarg
    assert called, ("probe_drive did not reach gym_media_index.default_store; the "
                    "seam production leaves empty is still empty")
    assert store.source_calls and store.asset_calls
    assert snap.gym_key == "crossfitlocal"


def test_probe_drive_with_no_deps_uses_the_selectors_own_predicate(monkeypatch):
    """The library count must come from gym_media_selector.is_usable, not from a copy
    of its rule living here."""
    seen = []
    real = _sel.is_usable

    def spy(asset):
        seen.append(asset.get("id"))
        return real(asset)

    store = _RecordingStore(
        sources=[{"id": "s1", "gym_id": "g", "kind": "gym_drive", "active": True,
                  "revoked_externally": False}],
        assets=[{"id": "a1", "gym_id": "g", "source_id": "s1", "eligible": True},
                {"id": "a2", "gym_id": "g", "source_id": "s1", "eligible": None}])
    monkeypatch.setattr(_idx, "default_store", lambda: store)
    monkeypatch.setattr(_sel, "is_usable", spy)
    snap = P.probe_drive("g")
    assert seen == ["a1", "a2"], "the selector's predicate was not consulted"
    assert snap.get("drive_library_usable") == 1   # the unprobed row fails closed


def test_probe_cta_with_no_deps_calls_the_real_extractor_and_the_real_dir(monkeypatch,
                                                                         tmp_path):
    from agent import config as _config
    seen = []
    real = _voice._extract_ctas                     # noqa: SLF001

    def spy(raw):
        seen.append(len(raw))
        return real(raw)

    gym = tmp_path / "toughtemple52040e"
    gym.mkdir()
    (gym / "lasso_voice.md").write_text(
        "### CTA rotation (cycle in order, one per post)\n"
        "> TODO: this section was missing or empty in the intake. Fill it by hand "
        "before activation.\n### Next\n", encoding="utf-8")
    monkeypatch.setattr(_config, "client_voice_dir", lambda: str(tmp_path))
    monkeypatch.setattr(_voice, "_extract_ctas", spy)

    snap = P.probe_cta("toughtemple52040e")         # NO voice_dir, NO read_text
    assert seen, "probe_cta did not reach agent.voice._extract_ctas"
    assert snap.get("voice_doc_present") is True
    assert snap.get("cta_pool_count") == 0


def test_probe_cta_with_no_deps_reads_a_real_missing_file(monkeypatch, tmp_path):
    """default_read_text must return None for an absent doc rather than raising --
    the production path has no read_text injected."""
    from agent import config as _config
    monkeypatch.setattr(_config, "client_voice_dir", lambda: str(tmp_path))
    snap = P.probe_cta("nosuchgym")
    assert snap.get("voice_doc_present") is False


def test_the_action_with_no_deps_calls_the_real_sync_source(monkeypatch):
    """The flagship remedy, on the real path, with nothing injected."""
    from agent.jobs import sync_gym_media as _sync
    calls = []

    def spy(source, **kw):
        calls.append(source["id"])
        return {"ok": True, "inserted": 4}

    store = _RecordingStore(sources=[{"id": "s1", "gym_id": "crossfitlocal",
                                      "kind": "gym_drive", "active": True,
                                      "revoked_externally": False}])
    monkeypatch.setattr(_idx, "default_store", lambda: store)
    monkeypatch.setattr(_sync, "sync_source", spy)
    values = C._gym_drive_sync("crossfitlocal")      # noqa: SLF001 - NO deps at all
    assert calls == ["s1"], "the action did not reach jobs.sync_gym_media.sync_source"
    assert values == {"drive_sync_ran": True, "drive_files_added": 4}


def test_the_whole_decision_runs_with_no_deps_at_all(monkeypatch):
    """End to end through decide() with deps={} -- exactly what the real caller does.
    The previous build's flagship path raised TypeError here for six rounds."""
    from agent.jobs import sync_gym_media as _sync
    store = _RecordingStore(sources=[{"id": "s1", "gym_id": "crossfitlocal",
                                      "kind": "gym_drive", "active": True,
                                      "revoked_externally": False}])

    def spy(source, **kw):
        store.assets.extend([{"id": f"a{i}", "gym_id": "crossfitlocal",
                              "source_id": "s1", "eligible": True} for i in range(6)])
        return {"ok": True, "inserted": 6}

    monkeypatch.setattr(_idx, "default_store", lambda: store)
    monkeypatch.setattr(_sync, "sync_source", spy)
    d = L.decide(text="my posts have no photos", gym_key="crossfitlocal",
                 may_reply=True, deps={})
    assert d.outcome == L.Outcome.REPLY, d.reason
    assert d.reply_text == ("I ran your photo sync just now. It added 6 new file(s). "
                            "Your media library has 6 file(s) Echo can post.")


def test_a_decision_never_raises_even_when_the_store_explodes(monkeypatch):
    """One ticket's exception must never sink the pass, and must never be silence."""
    class Boom:
        def list_sources(self, **kw):
            raise RuntimeError("supabase 503")

    monkeypatch.setattr(_idx, "default_store", Boom)
    d = L.decide(text="no photos on my posts", gym_key="crossfitlocal",
                 may_reply=True, deps={})
    assert d.outcome == L.Outcome.ESCALATE
    assert "503" in d.reason


# ---------------------------------------------------------------------------
# 3. A FAITHFUL POLL FAKE. It applies the params it is given.
# ---------------------------------------------------------------------------
class FaithfulBus:
    """Applies product / source / status / limit / offset exactly as PostgREST does.

    The previous build's fake ignored them, so a poll asking for statuses no client
    ticket ever lands in passed every test for four rounds while matching zero real
    rows in production.
    """

    def __init__(self, tickets, messages=None):
        self.tickets = list(tickets)
        self._messages = dict(messages or {})
        self.written = []

    def available(self):
        return True

    @staticmethod
    def _match(row, key, expr):
        if expr.startswith("eq."):
            return str(row.get(key) or "") == expr[3:]
        if expr.startswith("in."):
            return str(row.get(key) or "") in expr[3:].strip("()").split(",")
        raise AssertionError(f"unsupported filter {expr!r}")

    def _get(self, table, params):
        assert table == "support_tickets"
        rows = [r for r in self.tickets
                if all(self._match(r, k, v) for k, v in params.items()
                       if k not in ("select", "order", "limit", "offset"))]
        rows.sort(key=lambda r: r.get("created_at") or "")
        off = int(params.get("offset", 0))
        lim = int(params.get("limit", 50))
        return rows[off:off + lim]

    def recent_messages(self, ticket_id, limit=200):
        rows = list(self._messages.get(ticket_id, []))
        rows.sort(key=lambda m: m.get("created_at") or "", reverse=True)
        return rows[:limit]

    def record_outbound(self, **kw):
        self.written.append(kw)
        self._messages.setdefault(kw["ticket_id"], []).append(
            {"direction": "outbound", "attachments": kw.get("meta") or {},
             "created_at": "2099", "body": kw.get("body")})
        return {"id": f"m{len(self.written)}"}


def _ticket(tid, **kw):
    row = {"id": tid, "product": "echo", "source": "website_tab", "status": "hold",
           "identity_kind": "client", "slack_channel_id": "C1",
           "client_id": "uuid", "raw_text": "hi", "created_at": f"2026-09-0{tid}",
           "is_test": False}
    row.update(kw)
    return row


def test_the_poll_selects_the_statuses_real_client_tickets_actually_carry():
    """Live 2026-09-07: every real client ticket is status='hold', classification
    NULL. The previous build asked for ('new','triage') and matched zero of them."""
    bus = FaithfulBus([
        _ticket("1", status="hold"),
        _ticket("2", status="new"),
        _ticket("3", status="triage"),
        _ticket("4", status="resolved"),
        _ticket("5", status="verification"),
    ])
    got, capped = L.poll(bus, limit=10)
    assert sorted(t["id"] for t in got) == ["1", "2", "3"]
    assert capped is False


def test_the_poll_selects_the_sources_real_client_tickets_actually_carry():
    """Live 2026-09-07: all three real client tickets are source='website_tab'; every
    'slack_conversation' row in the table is a phase-4 arming probe."""
    bus = FaithfulBus([
        _ticket("1", source="website_tab"),
        _ticket("2", source="slack_conversation"),
        _ticket("3", source="ops_fix"),
        _ticket("4", source="coach_portal"),
    ])
    got, _ = L.poll(bus, limit=10)
    assert sorted(t["id"] for t in got) == ["1", "2"]


def test_the_poll_ignores_staff_tickets_and_tickets_with_no_slack_surface():
    bus = FaithfulBus([
        _ticket("1"),
        _ticket("2", identity_kind="staff"),
        _ticket("3", slack_channel_id=None),
    ])
    got, _ = L.poll(bus, limit=10)
    assert [t["id"] for t in got] == ["1"]


def test_the_poll_pages_past_tickets_this_lane_already_answered():
    """This lane never changes a ticket's status, so answered tickets stay in an open
    status forever and created_at.asc parks them at the front. Without paging, a
    handful of them starve every new ticket behind them at a clean-looking zero."""
    handled = {str(i): [{"direction": "outbound", "created_at": "2026",
                         "attachments": {L.LANE_META: L.LANE_NAME}}]
               for i in range(1, 60)}
    bus = FaithfulBus([_ticket(str(i), created_at=f"2026-09-{i:03d}")
                       for i in range(1, 62)], messages=handled)
    got, capped = L.poll(bus, limit=2)
    assert [t["id"] for t in got] == ["60", "61"]
    assert capped is False


def test_the_poll_ceiling_is_reported_as_a_backlog_not_as_an_empty_queue():
    handled = {str(i): [{"direction": "outbound", "created_at": "2026",
                         "attachments": {L.LANE_META: L.LANE_NAME}}]
               for i in range(1, 900)}
    bus = FaithfulBus([_ticket(str(i), created_at=f"2026-09-{i:04d}")
                       for i in range(1, 900)], messages=handled)
    got, capped = L.poll(bus, limit=1)
    assert got == [] and capped is True


def test_the_stopping_rule_and_the_loop_use_the_same_predicate():
    """Round 5's finding: a stopping rule that counted one thing and a loop that
    discarded rows for two OTHER reasons meant fifty undiscardable rows ended paging
    on page one at {ok: True, handled: 0}. Everything past _actionable is acted on."""
    bus = FaithfulBus([_ticket("1"), _ticket("2", identity_kind="staff")])
    got, _ = L.poll(bus, limit=10)
    for t in got:
        assert L._actionable(bus, t) is True         # noqa: SLF001
