"""
tests/test_intake_pagination.py -- 2026-09-23 intake starvation fix.

The old poll (bus.find_new_tickets oldest-20 + intake_pass re-reading that same window
every pass) let 20 permanently-failing or probe/test rows starve every fresh customer
ticket behind them. These tests pin the fix:

  * find_new_tickets filters is_test SERVER-SIDE (with a one-time 400 fallback for an
    older database) and pages by a (created_at, id) KEYSET cursor, never OFFSET;
  * intake_pass advances a persisted per-(product, source, identity) cursor past EVERY
    raw row of the page -- failed rows included -- so >20 poison rows no longer
    monopolise the window, a fresh ticket behind them is processed within bounded
    polls, the cursor survives a restart (file under config.data_dir()), and the sweep
    wraps to retry still-'new' failures;
  * a ticket that SUCCEEDED left the status=new/classification=null set, so no sweep,
    wrap or restart ever processes it twice (the inbound-row guard is the backstop).
"""
import json

import pytest

from agent import echo_ticket_worker as W
from agent.slack_convo.bus import Bus, TicketPage
from tests.test_echo_ticket_worker import _calls, _client_deps, _notices, _ticket


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_ECHO_TICKETS_ENABLED", "true")
    for ident in ("ECHO", "SCOUT"):
        monkeypatch.setenv(f"SLACK_CONVO_{ident}_ENABLED", "true")
        monkeypatch.setenv(f"SLACK_CONVO_{ident}_CLIENT_REPLY", "true")
        monkeypatch.setenv(f"SLACK_CONVO_{ident}_AUTO_ANSWER", "true")
    monkeypatch.setenv("SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE", "true")
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    yield


class KeysetBus:
    """A bus fake that honours the real pagination contract: server-side is_test
    exclusion, (created_at, id) ascending keyset pages of `limit`, and a TicketPage
    carrying raw page facts. Rows in self.fail_ids raise on record_inbound, so they
    stay status='new' forever -- the poison-ticket shape."""

    def __init__(self, tickets):
        self.tickets = {t["id"]: dict(t) for t in tickets}
        self.inbound = []
        self.outbound = []
        self.patches = []
        self.fail_ids = set()
        self.attempts = {}

    def find_new_tickets(self, *, product, source, limit=20, after=None):
        rows = [t for t in self.tickets.values()
                if t.get("product") == product and t.get("source") == source
                and t.get("status") == "new" and t.get("classification") is None
                and not t.get("is_test")]
        rows.sort(key=lambda t: (t["created_at"], t["id"]))
        if after:
            cur = (str(after["created_at"]), str(after["id"]))
            rows = [t for t in rows if (t["created_at"], t["id"]) > cur]
        page = rows[:int(limit)]
        return TicketPage(page, raw_count=len(page),
                          raw_last=(page[-1] if page else None))

    def record_inbound(self, **kwargs):
        tid = kwargs.get("ticket_id")
        self.attempts[tid] = self.attempts.get(tid, 0) + 1
        if tid in self.fail_ids:
            raise RuntimeError("permanent failure for this row")
        self.inbound.append(kwargs)
        return ({"id": f"in-{len(self.inbound)}"}, False)

    def inbound_count(self, ticket_id):
        return len([m for m in self.inbound if m.get("ticket_id") == ticket_id])

    def record_outbound(self, **kwargs):
        row = {"id": f"out-{len(self.outbound)}", **kwargs}
        self.outbound.append(row)
        return row

    def set_ticket(self, ticket_id, **fields):
        self.patches.append((ticket_id, fields))
        self.tickets[ticket_id].update(fields)

    def ticket(self, ticket_id):
        return dict(self.tickets[ticket_id])


def _poison(i):
    return _ticket(id=f"poison-{i:02d}", reporter=f"owner{i}@gym.com",
                   created_at=f"2026-09-20T00:00:{i:02d}+00:00")


def _fresh():
    return _ticket(id="fresh-1", created_at="2026-09-21T00:00:00+00:00")


def _run(bus, cursor_path, *, log=None, identity_name="echo", product="echo", source="website_tab"):
    _default_log, open_dm, post = _calls()
    _cards, notice = _notices()
    return W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                         write_hold_notice=notice, cursor_path=str(cursor_path),
                         identity_name=identity_name, product=product, source=source,
                         log=log or (lambda *a, **k: None), **_client_deps())


def test_twenty_plus_poison_rows_no_longer_starve_a_fresh_ticket(tmp_path):
    """25 permanently-failing rows sit ahead of one fresh customer ticket. The old
    window returned poison 1-20 on every poll forever; the fresh ticket must now be
    reached within two bounded passes (20 walked past on pass 1, 5 + fresh on pass 2)."""
    bus = KeysetBus([_poison(i) for i in range(25)] + [_fresh()])
    bus.fail_ids = {f"poison-{i:02d}" for i in range(25)}
    cursor = tmp_path / "cursor.json"

    first = _run(bus, cursor)
    assert first["processed"] == 0
    assert bus.tickets["fresh-1"]["classification"] is None  # not reached YET, not stuck
    saved = json.loads(cursor.read_text())
    leg = W._intake_leg_key(W.PRODUCT, W.SOURCE, "echo")
    assert saved[leg]["id"] == "poison-19"  # the window moved past the first page

    second = _run(bus, cursor)
    assert second["processed"] == 1
    worked = bus.tickets["fresh-1"]
    assert worked["classification"] is not None and worked["status"] != "new"
    assert bus.inbound_count("fresh-1") == 1
    # The poison rows failed, stayed 'new', and were never classified by accident.
    assert all(bus.tickets[f"poison-{i:02d}"]["classification"] is None
               for i in range(25))


def test_twenty_plus_server_side_test_rows_never_fill_the_window(tmp_path):
    """25 is_test rows (the durable probe flag) are excluded before paging, so a page
    of them cannot crowd out the fresh ticket even once."""
    tests = [_ticket(id=f"probe-{i:02d}", reporter=f"probe{i}@gym.com",
                     created_at=f"2026-09-19T00:00:{i:02d}+00:00", is_test=True)
             for i in range(25)]
    bus = KeysetBus(tests + [_fresh()])
    result = _run(bus, tmp_path / "cursor.json")
    assert result["processed"] == 1
    assert bus.tickets["fresh-1"]["classification"] is not None


def test_failed_rows_are_retried_after_the_sweep_wraps(tmp_path):
    """One poison row: pass 1 fails it and moves the cursor past it; pass 2 finds
    nothing past the cursor, wraps to the top and retries it. Fair, bounded, and the
    failure never wedges the queue at one end."""
    bus = KeysetBus([_poison(0)])
    bus.fail_ids = {"poison-00"}
    cursor = tmp_path / "cursor.json"
    assert _run(bus, cursor)["processed"] == 0
    assert bus.attempts["poison-00"] == 1
    assert _run(bus, cursor)["processed"] == 0
    assert bus.attempts["poison-00"] == 2  # retried via wrap, not every-poll starvation


def test_short_page_wrap_retries_old_failure_despite_new_arrivals(tmp_path):
    bus = KeysetBus([_poison(0)])
    bus.fail_ids = {"poison-00"}  # the old ticket fails once, then recovers
    cursor = tmp_path / "cursor.json"
    assert _run(bus, cursor)["processed"] == 0
    assert bus.attempts["poison-00"] == 1

    bus.fail_ids.clear()
    bus.tickets["fresh-1"] = _fresh()  # a new ticket arrived between polls
    assert _run(bus, cursor)["processed"] == 2
    assert bus.attempts["poison-00"] == 2
    assert bus.tickets["poison-00"]["status"] != "new"
    assert bus.tickets["fresh-1"]["classification"] is not None


def test_full_pages_periodically_wrap_without_blocking_later_rows(tmp_path, monkeypatch):
    bus = KeysetBus([_poison(i) for i in range(25)])
    bus.fail_ids = {f"poison-{i:02d}" for i in range(25)}
    cursor = tmp_path / "cursor.json"
    now = [1_000_000.0]
    monkeypatch.setattr(W, "_intake_now", lambda: now[0])

    assert _run(bus, cursor)["processed"] == 0
    assert json.loads(cursor.read_text())[W._intake_leg_key(W.PRODUCT, W.SOURCE, "echo")]["id"] == "poison-19"

    # Even if full pages keep arriving and the tail is never observed, an elapsed sweep
    # bound restarts a pass. The following full-page walk still reaches rows after it.
    now[0] += W._INTAKE_MAX_SWEEP_SECONDS + 1
    assert _run(bus, cursor)["processed"] == 0
    assert _run(bus, cursor)["processed"] == 0
    assert bus.attempts["poison-20"] == 1
    assert bus.attempts["poison-24"] == 1


def test_successful_rows_are_never_processed_twice_across_polls_and_restarts(tmp_path):
    """A worked ticket leaves the status=new/classification=null set, so neither the
    next poll nor a fresh 'process' (new bus over the same cursor file, the restart
    shape) can re-run it; the inbound-row guard is the backstop."""
    cursor = tmp_path / "cursor.json"
    bus = KeysetBus([_fresh()])
    assert _run(bus, cursor)["processed"] == 1
    assert bus.inbound_count("fresh-1") == 1

    # Same process, next poll: nothing left to do.
    assert _run(bus, cursor)["processed"] == 0
    assert bus.inbound_count("fresh-1") == 1

    # Restart shape: a brand-new bus over the same persisted cursor and the same row.
    bus2 = KeysetBus([_fresh()])
    bus2.tickets["fresh-1"].update(bus.tickets["fresh-1"])  # worked state survived
    assert _run(bus2, cursor)["processed"] == 0
    assert bus2.inbound == [] and bus2.outbound == []


def test_corrupt_cursor_file_is_a_safe_cold_start(tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{not json")
    bus = KeysetBus([_fresh()])
    assert _run(bus, cursor)["processed"] == 1


def test_replace_failure_keeps_process_cursor_and_logs_without_starving_fresh_ticket(tmp_path, monkeypatch):
    bus = KeysetBus([_poison(i) for i in range(25)] + [_fresh()])
    bus.fail_ids = {f"poison-{i:02d}" for i in range(25)}
    cursor = tmp_path / "unwritable-cursor.json"
    logs = []

    def fail_replace(_src, _dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(W.os, "replace", fail_replace)
    assert _run(bus, cursor, log=logs.append)["processed"] == 0
    assert any("cursor persistence failed" in line and "Permission denied" in line for line in logs)

    # The durable write is still failing, but the same process uses its last known
    # cursor so the remaining poison rows no longer pin the oldest page.
    assert _run(bus, cursor, log=logs.append)["processed"] == 1
    assert bus.tickets["fresh-1"]["classification"] is not None
    assert bus.inbound_count("fresh-1") == 1


def test_malformed_leg_is_dropped_without_resetting_other_leg(tmp_path):
    cursor = tmp_path / "cursor.json"
    scout_key = W._intake_leg_key("portal", "website_tab", "scout")
    cursor.write_text(json.dumps({
        W._intake_leg_key("echo", "website_tab", "echo"): ["bad", "shape"],
        scout_key: {"created_at": "2026-09-20T00:00:00+00:00", "id": "scout-20"},
        W._intake_leg_key("portal", "website_tab", "other"): {
            "created_at": ["truthy", "but", "invalid"], "id": "other-20"},
    }))

    loaded = W._load_intake_cursors(str(cursor))
    assert W._intake_leg_key("echo", "website_tab", "echo") not in loaded
    assert W._intake_leg_key("portal", "website_tab", "other") not in loaded
    assert loaded[scout_key]["id"] == "scout-20"


# ---- bus-level: the query itself -----------------------------------------------------

class _Response:
    def __init__(self, rows, status=200):
        self._rows, self.status_code, self.text = rows, status, json.dumps(rows)

    def json(self):
        return self._rows


class _RecordingHttp:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs.get("params") or {})
        rows, status = self.pages.pop(0)
        return _Response(rows, status)


def _row(i, **over):
    r = {"id": f"00000000-0000-0000-0000-0000000000{i:02d}",
         "created_at": f"2026-09-20T00:00:{i:02d}+00:00", "status": "new",
         "classification": None, "raw_text": f"client message {i}", "reporter": "o@g.com"}
    r.update(over)
    return r


def test_find_new_tickets_filters_is_test_server_side_and_pages_by_keyset():
    http = _RecordingHttp([([_row(1)], 200)])
    bus = Bus(url="https://example.supabase.co", service_key="service", http=http)
    page = bus.find_new_tickets(product="echo", source="website_tab",
                                after={"created_at": "2026-09-20T00:00:00+00:00",
                                       "id": "00000000-0000-0000-0000-000000000000"})
    params = http.calls[0]
    assert params["is_test"] == "eq.false"
    assert params["status"] == "eq.new" and params["classification"] == "is.null"
    assert params["order"] == "created_at.asc,id.asc"
    # Keyset, not OFFSET: strictly past the cursor, with the id tiebreak, and no offset
    # parameter that a concurrent insert could shift into a silent skip.
    assert "offset" not in params
    assert params["or"] == ('(created_at.gt."2026-09-20T00:00:00+00:00",'
                            'and(created_at.eq."2026-09-20T00:00:00+00:00",'
                            'id.gt."00000000-0000-0000-0000-000000000000"))')
    assert isinstance(page, TicketPage) and page.raw_count == 1


def test_find_new_tickets_falls_back_once_when_the_is_test_column_is_missing():
    rows = [_row(1), _row(2)]
    http = _RecordingHttp([([], 400), (rows, 200)])
    bus = Bus(url="https://example.supabase.co", service_key="service", http=http)
    page = bus.find_new_tickets(product="echo", source="website_tab")
    assert len(http.calls) == 2
    assert "is_test" not in http.calls[1]
    # The status/classification predicates -- what makes a row safe to drop from work --
    # are never part of the fallback.
    assert http.calls[1]["status"] == "eq.new"
    assert http.calls[1]["classification"] == "is.null"
    assert [t["id"] for t in page] == [r["id"] for r in rows]


def test_find_new_tickets_non_400_errors_still_raise():
    from agent.slack_convo.bus import BusError
    http = _RecordingHttp([([], 401)])
    bus = Bus(url="https://example.supabase.co", service_key="service", http=http)
    with pytest.raises(BusError):
        bus.find_new_tickets(product="echo", source="website_tab")
    assert len(http.calls) == 1  # no silent retry masking an auth failure


def test_find_new_tickets_still_drops_strict_probe_rows_and_reports_raw_page():
    """The local strict predicate still applies on top of the server filter (probe rows
    only recognizable by heuristic), and the page facts let the caller's cursor advance
    PAST the dropped rows instead of re-fetching them forever."""
    probe = _row(1, raw_text="[arming-probe] check", reporter="blake+zztest@lasso.com")
    http = _RecordingHttp([([probe, _row(2)], 200)])
    bus = Bus(url="https://example.supabase.co", service_key="service", http=http)
    page = bus.find_new_tickets(product="echo", source="website_tab")
    assert [t["id"] for t in page] == [_row(2)["id"]]
    assert page.raw_count == 2 and page.raw_last["id"] == _row(2)["id"]
