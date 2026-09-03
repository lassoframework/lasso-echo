"""Regressions for the four defects the 2026-09-02 verification audit found AFTER the
Supabase REST outage was cleared. Each one is a case where a safety net had shipped but
could not actually fire, so the system LOOKED protected while being wide open.

  A  the listener watchdog swept on a different service than the one recording heartbeats
  B  the calendar_unreadable stall latch never cleared, muting 14 gyms for the next outage
  C  is_systemic keyed on the exception class, so one gym's Meta timeout could claim the
     systemic slot and suppress escalation for a real database outage
  D  a failed revert out of the 'publishing' claim was swallowed, stranding the row where
     nothing retries it and nothing alerts
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import ops_triage as ot  # noqa: E402


# ---- C: systemic must key on the SHARED host, not the exception class ------------------

def test_supabase_timeout_is_systemic():
    """The real GBP alert from the outage: same exception, shared host -> one incident."""
    assert ot.is_systemic(
        "ECHO ALERT: GBP lane failed: ReadTimeout: HTTPSConnectionPool("
        "host='ooqcvmcjspeltuuhcvlh.supabase.co', port=443): Read timed out. "
        "(read timeout=30). The draft run is unaffected.") is True


def test_a_single_gyms_meta_timeout_is_NOT_systemic():
    """THE BUG: identical exception, per-gym host. Treating this as systemic let one gym's
    Meta timeout claim the 30-minute slot and mute the escalation for a genuine fleet
    outage (audit scenario G)."""
    assert ot.is_systemic(
        "ECHO ALERT: publish failed for gritx_ig: ReadTimeout: HTTPSConnectionPool("
        "host='graph.facebook.com', port=443): Read timed out. (read timeout=30)"
    ) is False


def test_zernio_timeout_for_one_gym_is_NOT_systemic():
    assert ot.is_systemic(
        "ECHO ALERT: theboltonclub: Max retries exceeded with url: "
        "https://api.zernio.com/v1/posts (Caused by ConnectTimeoutError)") is False


def test_transport_error_with_no_host_fans_out_rather_than_collapsing():
    """Fail toward MORE eyes: an unrecognised shape must not be silently collapsed."""
    assert ot.is_systemic("ECHO ALERT: something failed: ReadTimeout") is False


def test_the_stall_alerts_stay_systemic_without_naming_a_host():
    """calendar_unreadable NAMES the shared dependency, so it never needed a host."""
    for gym in ("district_h", "eng", "gritx", "train7164ae502"):
        msg = (f"ECHO ALERT: gym {gym} is STALLED at 'calendar_unreadable': the shared "
               "calendar could not be read (Supabase creds/network); no rebuild will run "
               "until it reads again")
        assert ot.is_systemic(msg) is True, gym
        assert ot.classify(msg) == ot.NEEDS_TRIAGE, gym


# ---- B: the stall latch must re-arm when the calendar reads again ----------------------

class _FakeKv:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def kv_get(self, key):
        return self.store.get(key, "")

    def kv_set(self, key, value):
        self.store[key] = value


def test_clear_stall_rearms_calendar_unreadable(monkeypatch):
    """After the outage, 14 gyms held a permanent gym_stall_alerted_*_calendar_unreadable
    key. Nothing cleared it on recovery, so the NEXT outage would have been silent for
    every one of them."""
    from agent import client_media_sync as cms
    fake = _FakeKv({"gym_stall_alerted_district_h_calendar_unreadable": "1"})
    monkeypatch.setattr(cms, "db", fake, raising=False)
    import agent.db as real_db
    monkeypatch.setattr(real_db, "kv_get", fake.kv_get)
    monkeypatch.setattr(real_db, "kv_set", fake.kv_set)

    cms._clear_stall("district_h", "calendar_unreadable")
    assert fake.store["gym_stall_alerted_district_h_calendar_unreadable"] == ""


def test_clearing_an_unset_stall_is_a_noop_and_never_raises(monkeypatch):
    from agent import client_media_sync as cms
    fake = _FakeKv()
    import agent.db as real_db
    monkeypatch.setattr(real_db, "kv_get", fake.kv_get)
    monkeypatch.setattr(real_db, "kv_set", fake.kv_set)
    cms._clear_stall("neverstalled", "calendar_unreadable")
    assert fake.store == {}


def test_a_stalled_gym_realerts_after_recovery(monkeypatch):
    """The whole point: alert -> recover -> stall again MUST alert a second time."""
    from agent import client_media_sync as cms
    fake = _FakeKv()
    import agent.db as real_db
    monkeypatch.setattr(real_db, "kv_get", fake.kv_get)
    monkeypatch.setattr(real_db, "kv_set", fake.kv_set)
    sent = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda msg, **kw: sent.append(msg))

    cms._alert_stall("eng", "calendar_unreadable", "outage one", lambda m: None)
    cms._alert_stall("eng", "calendar_unreadable", "outage one", lambda m: None)
    assert len(sent) == 1, "deduped within an episode"

    cms._clear_stall("eng", "calendar_unreadable")          # reads recover
    cms._alert_stall("eng", "calendar_unreadable", "outage two", lambda m: None)
    assert len(sent) == 2, "a NEW outage must alert again"


# ---- D: a failed revert out of 'publishing' must shout, never strand silently ----------

class _StoreThatFailsRevert:
    def mark_publish_failed(self, *a, **kw):
        raise RuntimeError("ReadTimeout: supabase unreachable")


class _StoreThatReverts:
    def __init__(self):
        self.calls = []

    def mark_publish_failed(self, row_id, revert_status=None, reject_reason=None):
        self.calls.append((row_id, revert_status, reject_reason))


def test_failed_revert_alerts_and_reports_false(monkeypatch):
    """THE BUG: `except Exception: pass` meant a revert that died during the outage left
    the row claimed in 'publishing' forever -- mark_publishing only re-claims
    pending/approved rows, so nothing retried it and nothing said so."""
    from agent import calendar_autopublish as ca
    sent = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda msg, **kw: sent.append(msg))

    ok = ca._revert_to_pending(store=_StoreThatFailsRevert(), row_id="f75c19e9",
                               reject_reason="publish_guard: multi_ask")
    assert ok is False
    assert len(sent) == 1
    body = sent[0]
    assert "STRANDED" in body and "f75c19e9" in body
    # the operator must be told the revert is SAFE: the guard blocks before any network
    # call, so unlike the generic stuck-publishing sweep there is no double-post risk.
    assert "did NOT go out" in body or "did not go out" in body.lower()


def test_successful_revert_is_silent_and_reports_true(monkeypatch):
    from agent import calendar_autopublish as ca
    sent = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda msg, **kw: sent.append(msg))
    store = _StoreThatReverts()

    ok = ca._revert_to_pending(store=store, row_id="r1", reject_reason="publish_guard: x")
    assert ok is True
    assert sent == []
    assert store.calls == [("r1", "pending", "publish_guard: x")]


def test_revert_falls_back_for_stores_without_reject_reason(monkeypatch):
    """Older store/test fakes take no reject_reason kwarg; the revert still has to land."""
    from agent import calendar_autopublish as ca

    class _Old:
        def __init__(self):
            self.calls = []

        def mark_publish_failed(self, row_id, revert_status=None):
            self.calls.append((row_id, revert_status))

    store = _Old()
    assert ca._revert_to_pending(store=store, row_id="r2", reject_reason="x") is True
    assert store.calls == [("r2", "pending")]


def test_publish_blocked_alert_tells_the_truth_about_the_revert(monkeypatch):
    """The alert used to claim 'reverted to pending' unconditionally -- including on the
    nights the revert threw and the row was left stranded."""
    from agent import calendar_autopublish as ca
    fake = _FakeKv()
    import agent.db as real_db
    monkeypatch.setattr(real_db, "kv_get", fake.kv_get)
    monkeypatch.setattr(real_db, "kv_set", fake.kv_set)
    sent = []
    import agent.ops_alerts as oa
    monkeypatch.setattr(oa, "alert", lambda msg, **kw: sent.append(msg))

    ca._alert_publish_blocked("lasso", "row1", "multi_ask", reverted=False)
    assert "REVERT FAILED" in sent[0]
    assert "stranded" in sent[0]

    fake.store.clear()
    sent.clear()
    ca._alert_publish_blocked("lasso", "row2", "multi_ask", reverted=True)
    assert "reverted to pending" in sent[0]
    assert "REVERT FAILED" not in sent[0]


# ---- A: the watchdog must sweep on the service that records the heartbeat --------------

def test_listener_sweep_is_not_wired_into_the_worker_lane():
    """It swept on `echo` while heartbeats landed on `echo-intake-web`'s volume, so it read
    an empty kv and reported never_seen forever."""
    import pathlib
    runner = pathlib.Path(__file__).parent.parent / "agent" / "runner.py"
    src = runner.read_text()
    assert "listener_watch" in src, "the explanatory comment must stay"
    assert "_lw.sweep(" not in src, "the worker must not sweep a kv it never writes"


def test_intake_web_sweeps_where_the_heartbeat_lands(monkeypatch):
    from agent import intake_web as iw
    import agent.listener_watch as lw
    seen = {}

    def _fake_sweep(*, alert=None, **kw):
        seen["alert"] = alert
        return {"checked": 1, "down": 1, "recovered": 0, "healthy": 0, "never_seen": 0}

    monkeypatch.setattr(lw, "sweep", _fake_sweep)
    summary = iw.start_listener_watch_thread(once=True)
    assert summary["down"] == 1
    assert callable(seen["alert"]), "sweep must be given this service's own alert path"


def test_intake_web_alert_uses_the_support_channel_and_forces(monkeypatch):
    """This service has no ops channel and no AGENT_OPS_ALERTS_ENABLED, so an unforced
    default-poster alert would be a silent no-op here."""
    from agent import intake_web as iw
    import agent.listener_watch as lw
    from agent import config
    posted = {}

    monkeypatch.setattr(config, "support_channel_id", lambda: "C0BTDAE1GLW")

    import agent.ops_alerts as oa

    def _capture(message, poster=None, force=False):
        posted["message"] = message
        posted["force"] = force
        posted["channel"] = getattr(poster, "_channel", None)
        return {"ok": True}

    monkeypatch.setattr(oa, "alert", _capture)
    monkeypatch.setattr(lw, "sweep",
                        lambda *, alert=None, **kw: (alert("scout-listener is down"),
                                                     {"checked": 1, "down": 1,
                                                      "recovered": 0, "healthy": 0,
                                                      "never_seen": 0})[1])

    iw.start_listener_watch_thread(once=True)
    assert posted["force"] is True
    assert posted["channel"] == "C0BTDAE1GLW"
    assert "scout-listener is down" in posted["message"]


def test_intake_web_alert_drops_cleanly_with_no_channel(monkeypatch):
    from agent import intake_web as iw
    import agent.listener_watch as lw
    from agent import config
    monkeypatch.setattr(config, "support_channel_id", lambda: "")
    monkeypatch.setattr(lw, "sweep",
                        lambda *, alert=None, **kw: (alert("down"),
                                                     {"checked": 1, "down": 1,
                                                      "recovered": 0, "healthy": 0,
                                                      "never_seen": 0})[1])
    # must not raise
    assert iw.start_listener_watch_thread(once=True)["down"] == 1
