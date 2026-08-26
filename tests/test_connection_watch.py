"""Partial-connection watch (Hill Country, 2026-08-26).

A gym that connected SOME platforms but not all three past the grace window fires
ONE deduped ops alert; full connection clears the stamps; zero connections is not
partial; pacing keeps the sweep to one per window; a Zernio error on one gym never
blocks the rest. Flag off = no-op.
"""

from datetime import datetime, timedelta, timezone

from agent import connection_watch as cw


NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self):
        self.kv = {}

    def kv_get(self, k):
        return self.kv.get(k) or None

    def kv_set(self, k, v):
        self.kv[k] = v

    def gym_get(self, key):
        return {"account_key": key, "zernio_profile_id": f"pid-{key}"}


class FakeZernio:
    def __init__(self, platforms_by_pid, raises_for=()):
        self._by_pid = platforms_by_pid
        self._raises = set(raises_for)

    def find_profile_id(self, name):
        return f"pid-{name}"

    def list_accounts(self, pid):
        if pid in self._raises:
            raise RuntimeError("zernio down")
        return {"accounts": [{"platform": p, "_id": f"a-{p}"}
                             for p in self._by_pid.get(pid, [])]}


def _run(db, zernio, clients, now=NOW, monkeypatch=None, alerts=None, force=True):
    alerts = alerts if alerts is not None else []
    return cw.watch_connections(
        zernio=zernio, db_mod=db, clients=clients, now=now,
        alert=alerts.append, logger=lambda m: None, force=force), alerts


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.delenv("AGENT_CONNECTION_WATCH", raising=False)
    out = cw.watch_connections(zernio=FakeZernio({}), db_mod=FakeDb(),
                               clients=["hillcountry"], alert=lambda m: None)
    assert out["ok"] is False and out["alerted"] == 0


def test_partial_fresh_stamps_but_never_alerts(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECTION_WATCH", "true")
    db = FakeDb()
    z = FakeZernio({"pid-hillcountry": ["instagram"]})
    out, alerts = _run(db, z, ["hillcountry"])
    assert out["partial"] == 1 and out["alerted"] == 0 and alerts == []
    # first-seen stamped for the exact missing set
    assert db.kv_get("conn_watch_seen_hillcountry_facebook-googlebusiness")


def test_partial_past_grace_alerts_once(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECTION_WATCH", "true")
    db = FakeDb()
    z = FakeZernio({"pid-hillcountry": ["instagram"]})
    _run(db, z, ["hillcountry"], now=NOW - timedelta(hours=25))
    out, alerts = _run(db, z, ["hillcountry"], now=NOW)
    assert out["alerted"] == 1 and len(alerts) == 1
    msg = alerts[0]
    assert "hillcountry" in msg and "facebook" in msg and "googlebusiness" in msg
    assert "intake-link" in msg
    # dedupe: a third sweep alerts nothing
    out2, alerts2 = _run(db, z, ["hillcountry"], now=NOW + timedelta(hours=1))
    assert out2["alerted"] == 0 and alerts2 == []


def test_missing_set_change_starts_new_grace_cycle(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECTION_WATCH", "true")
    db = FakeDb()
    _run(db, FakeZernio({"pid-g": ["instagram"]}), ["g"],
         now=NOW - timedelta(hours=30))
    # facebook connects; googlebusiness still missing -> NEW missing set, fresh grace
    out, alerts = _run(db, FakeZernio({"pid-g": ["instagram", "facebook"]}),
                       ["g"], now=NOW)
    assert out["alerted"] == 0 and alerts == []
    out2, alerts2 = _run(db, FakeZernio({"pid-g": ["instagram", "facebook"]}),
                         ["g"], now=NOW + timedelta(hours=25))
    assert out2["alerted"] == 1 and "googlebusiness" in alerts2[0]


def test_fully_connected_clears_stamps_and_never_alerts(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECTION_WATCH", "true")
    db = FakeDb()
    _run(db, FakeZernio({"pid-g": ["instagram"]}), ["g"],
         now=NOW - timedelta(hours=30))
    all_three = ["instagram", "facebook", "googlebusiness"]
    out, alerts = _run(db, FakeZernio({"pid-g": all_three}), ["g"], now=NOW)
    assert out["alerted"] == 0 and alerts == []
    assert not db.kv_get("conn_watch_seen_g_facebook-googlebusiness")
    # later partial re-alerts after its own fresh grace
    _run(db, FakeZernio({"pid-g": ["instagram"]}), ["g"], now=NOW)
    out2, alerts2 = _run(db, FakeZernio({"pid-g": ["instagram"]}), ["g"],
                         now=NOW + timedelta(hours=25))
    assert out2["alerted"] == 1


def test_zero_connected_is_not_partial(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECTION_WATCH", "true")
    db = FakeDb()
    out, alerts = _run(db, FakeZernio({"pid-g": []}), ["g"],
                       now=NOW + timedelta(hours=100))
    assert out["partial"] == 0 and out["alerted"] == 0 and alerts == []


def test_one_gym_error_never_blocks_the_rest(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECTION_WATCH", "true")
    db = FakeDb()
    z = FakeZernio({"pid-good": ["instagram"]}, raises_for={"pid-bad"})
    out, _ = _run(db, z, ["bad", "good"])
    assert out["skipped"] == 1 and out["partial"] == 1


def test_pacing_skips_within_window_and_force_overrides(monkeypatch):
    monkeypatch.setenv("AGENT_CONNECTION_WATCH", "true")
    db = FakeDb()
    z = FakeZernio({"pid-g": ["instagram"]})
    out1, _ = _run(db, z, ["g"], force=False)
    assert out1.get("reason") != "paced"
    out2, _ = _run(db, z, ["g"], now=NOW + timedelta(hours=1), force=False)
    assert out2.get("reason") == "paced"
    out3, _ = _run(db, z, ["g"], now=NOW + timedelta(hours=1), force=True)
    assert out3.get("reason") != "paced"


def test_connect_page_says_each_platform_needs_its_own_approval():
    from agent.intake_web import CONNECT_PAGE
    assert "All three need their own approval" in CONNECT_PAGE
    assert "Not yet" in CONNECT_PAGE
    assert 'id="prog"' in CONNECT_PAGE


def test_connect_page_opens_oauth_in_new_tab_and_polls_on_focus():
    """OAuth must open in _blank so the original page stays alive to detect return.
    Hill Country 2026-08-26: window.location.href navigated the same tab away,
    leaving no mechanism to update the connected badges afterward."""
    from agent.intake_web import CONNECT_PAGE
    # New tab — NOT same-tab navigation
    assert 'window.open(url, "_blank"' in CONNECT_PAGE
    assert "window.location.href = url" not in CONNECT_PAGE
    # Focus listener re-polls when user returns from the OAuth window
    assert 'addEventListener("focus"' in CONNECT_PAGE
    assert "refreshStatus" in CONNECT_PAGE
    # Backup interval poll so mobile/embedded contexts work too
    assert "setInterval" in CONNECT_PAGE


def test_connect_link_kind(monkeypatch):
    """kind='connect' mints the /portal/<token>/connect URL."""
    import agent.intake_web as iw
    from agent import intake_tokens
    monkeypatch.setenv("AGENT_INTAKE_SIGNING_SECRET", "test-secret-cw")
    link = iw.link_for("hillcountry", kind="connect")
    assert link.endswith("/portal/" + intake_tokens.mint("hillcountry") + "/connect")
