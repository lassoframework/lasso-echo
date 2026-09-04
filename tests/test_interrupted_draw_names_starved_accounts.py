"""The interrupted-draw alert has to say what was actually lost.

2026-09-04. The 09:21 alert read: "the daily draw for 2026-09-04 was INTERRUPTED mid-run
... 1 gym(s) have NO rows for 2026-09-04: mflhaa5139." A human reads that and reasonably
decides it can wait.

What it did not say is that lasso_ig -- LASSO's own Instagram, client zero -- had not
published in eight days and had not recorded a heartbeat in three, starved by the very
draws this alert was firing about. _gyms_short_on reads CLIENT gym calendars and nothing
else, and run_daily walks roughly thirty fleet-wide, network-bound maintenance sweeps
before it ever reaches the static account loop. So an interrupted draw loses client zero
FIRST and the alert was structurally blind to it.

Understating the damage is worse than silence here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import listener, ops_alerts  # noqa: E402


class _Acct:
    def __init__(self, key):
        self.key = key


def _arm(monkeypatch, *, short, starved, day="2026-09-04"):
    sent = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: sent.append(m))
    monkeypatch.setattr(listener, "_gyms_short_on", lambda d: short)
    monkeypatch.setattr(listener, "_accounts_starved_on", lambda d: starved)
    state = {"draw_started": day}
    monkeypatch.setattr(listener, "_read_state", lambda: dict(state))
    monkeypatch.setattr(listener, "_write_state", lambda d: state.update(d))
    from agent import db
    monkeypatch.setattr(db, "kv_get", lambda k, d="": "")
    monkeypatch.setattr(db, "kv_set", lambda k, v: None)
    return sent


def test_a_starved_account_is_named(monkeypatch):
    sent = _arm(monkeypatch, short=["mflhaa5139"], starved=["lasso_ig", "lasso_fb"])
    assert listener.alert_interrupted_draw() is True
    assert "lasso_ig" in sent[0] and "lasso_fb" in sent[0]
    assert "never drafted at all" in sent[0]


def test_client_gyms_covered_but_client_zero_starved_is_not_no_action_needed(monkeypatch):
    """The exact state that went unreported: every client gym has its rows, so the old
    code said "No action needed" -- while LASSO drafted nothing at all."""
    sent = _arm(monkeypatch, short=[], starved=["lasso_ig"])
    assert listener.alert_interrupted_draw() is True
    assert "No action needed" not in sent[0]
    assert "--force" in sent[0], "it must name the recovery command"
    assert "lasso_ig" in sent[0]


def test_a_fully_covered_day_is_still_quiet(monkeypatch):
    """The harmless-restart branch is unchanged: nothing short, nothing starved, and the
    phrasing stays the one the triage classifier already treats as noise."""
    from agent import ops_triage
    sent = _arm(monkeypatch, short=[], starved=[])
    assert listener.alert_interrupted_draw() is True
    assert "No action needed" in sent[0]
    assert ops_triage.classify(sent[0]) == ops_triage.NOISE, \
        "a restart that cost nothing must not wake anyone"


def test_a_real_loss_is_never_classified_as_noise(monkeypatch):
    for short, starved in (([], ["lasso_ig"]), (["mflhaa5139"], []),
                           (["mflhaa5139"], ["lasso_ig"])):
        from agent import ops_triage
        sent = _arm(monkeypatch, short=short, starved=starved)
        assert listener.alert_interrupted_draw() is True
        assert ops_triage.classify(sent[0]) == ops_triage.NEEDS_TRIAGE, (short, starved)


def test_unreadable_heartbeats_say_unknown_not_fine(monkeypatch):
    """Same contract as _gyms_short_on: unknown is unknown, never silently 'fine'."""
    sent = _arm(monkeypatch, short=[], starved=None)
    assert listener.alert_interrupted_draw() is True
    assert "unknown" in sent[0].lower()


def test_starved_accounts_reads_the_heartbeat_store(monkeypatch):
    """The helper itself: an account with a heartbeat is fine, one without is starved."""
    from agent import accounts, heartbeat
    monkeypatch.setattr(accounts, "active_accounts",
                        lambda: [_Acct("lasso_ig"), _Acct("lasso_fb")])
    monkeypatch.setattr(heartbeat, "heartbeat_at",
                        lambda key, day: "2026-09-04T12:00:00" if key == "lasso_fb" else "")
    assert listener._accounts_starved_on("2026-09-04") == ["lasso_ig"]


def test_zero_accounts_is_unknown_not_all_healthy(monkeypatch):
    """Checking nothing makes 'nothing is starved' vacuously true -- the worst direction
    for this to fail, same as _gyms_short_on's own guard."""
    from agent import accounts
    monkeypatch.setattr(accounts, "active_accounts", lambda: [])
    assert listener._accounts_starved_on("2026-09-04") is None


def test_a_broken_account_read_is_unknown(monkeypatch):
    from agent import accounts
    monkeypatch.setattr(accounts, "active_accounts",
                        lambda: (_ for _ in ()).throw(RuntimeError("registry down")))
    assert listener._accounts_starved_on("2026-09-04") is None
