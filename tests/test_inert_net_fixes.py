"""Two safety nets that ran, logged nothing, and could not fire (AUD-106, AUD-107).

Both are the shape that has now shipped six times in this codebase: a component that
executes, raises nothing, and does nothing useful, while its own tests pass.

AUD-106. The stale-publishing sweep wrote the literal string "alerted" and cleared it
NOWHERE, so a row that stays stuck is muted permanently after one alert. Caught red-handed:
production held stuck_publishing_d4574f62... = 'alerted' while that exact row was STILL
status='publishing', published_at NULL, post_date 2026-08-28 -- eight days stranded, one
alert, then silence forever.

AUD-107. Both publish-claim release paths sat inside `except Exception: pass`. A claim that
fails to release leaves the row claimed, the publish lane skips it on every future pass, and
the client's approved post silently never goes out. The intended backstop watchdog had never
fired once, because a swallowed release leaves nothing to see. Production: an in_flight
claim on lasso_ig from 2026-09-01 12:29:55, four days stale, zero claim_hold_alerted_* keys.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_autopublish as cap  # noqa: E402


class _KV:
    def __init__(self, seed=None):
        self.d = dict(seed or {})

    def get(self, k, default=""):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class _Store:
    def __init__(self, rows):
        self._rows = rows

    def publishing_rows(self):
        return self._rows


def _row(rid="d4574f62", gym="lasso"):
    return {"id": rid, "gym_id": gym, "account": "instagram", "post_date": "2026-08-28"}


def _now(days=0):
    return datetime(2026, 9, 5, 12, 0) + timedelta(days=days)


# ---- AUD-106 ---------------------------------------------------------------------------

def test_a_still_stuck_row_re_alerts_instead_of_going_silent_forever():
    """The row is stuck, was alerted a day ago, and must speak again."""
    sent = []
    stamp = (_now(-2)).isoformat()
    kv = _KV({f"stuck_publishing_d4574f62": f"alerted:{stamp}"})
    out = cap.sweep_stuck_publishing(store=_Store([_row()]), kv=kv, now=_now(),
                                     alert=lambda m: sent.append(m))
    assert out == ["d4574f62"], "a row stuck for another day must re-alert"
    assert "STILL stuck" in sent[0]
    assert kv.get("stuck_publishing_d4574f62").startswith("alerted:")


def test_it_does_not_re_alert_every_single_sweep():
    """Re-alerting is daily, not per-tick. The sweep runs constantly."""
    sent = []
    kv = _KV({"stuck_publishing_d4574f62": f"alerted:{_now(0).isoformat()}"})
    out = cap.sweep_stuck_publishing(store=_Store([_row()]), kv=kv, now=_now(),
                                     alert=lambda m: sent.append(m))
    assert out == [] and sent == []


def test_the_legacy_bare_alerted_marker_is_adopted_not_trusted_forever():
    """Production holds the old value with no timestamp. It must neither be believed
    forever nor fire instantly on the next sweep."""
    sent = []
    kv = _KV({"stuck_publishing_d4574f62": "alerted"})
    out = cap.sweep_stuck_publishing(store=_Store([_row()]), kv=kv, now=_now(),
                                     alert=lambda m: sent.append(m))
    assert out == [] and sent == [], "adopting the legacy marker must not alert immediately"
    assert kv.get("stuck_publishing_d4574f62").startswith("alerted:"), "it gains a clock"
    out2 = cap.sweep_stuck_publishing(store=_Store([_row()]), kv=kv, now=_now(days=2),
                                      alert=lambda m: sent.append(m))
    assert out2 == ["d4574f62"], "and then it re-alerts one interval later"


def test_the_marker_is_always_a_timestamp_never_a_magic_word():
    """A sibling key stored a timestamp while this one stored 'alerted', so the two values
    were not comparable. Every write now has the same shape."""
    sent = []
    kv = _KV()
    cap.sweep_stuck_publishing(store=_Store([_row()]), kv=kv, now=_now(),
                               alert=lambda m: sent.append(m))
    first = kv.get("stuck_publishing_d4574f62")
    datetime.fromisoformat(first)          # first sighting: a bare timestamp
    cap.sweep_stuck_publishing(store=_Store([_row()]), kv=kv, now=_now(days=1),
                               alert=lambda m: sent.append(m))
    datetime.fromisoformat(kv.get("stuck_publishing_d4574f62").partition(":")[2])


# ---- AUD-107 ---------------------------------------------------------------------------

def test_a_failed_claim_release_alerts_instead_of_vanishing(monkeypatch):
    from agent import runner, ops_alerts

    def boom(*a, **k):
        raise RuntimeError("sqlite is locked")

    sent = []
    monkeypatch.setattr("agent.db.socialapi_claim_release", boom)
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: sent.append(m))
    ok = runner._release_claim("draft-1", "lasso_ig", "dry run, nothing sent")
    assert ok is False
    assert sent, "a swallowed release is how a post gets stranded with nobody told"
    assert "claim release FAILED" in sent[0] and "lasso_ig" in sent[0]
    assert "stranded" in sent[0].lower() or "skip it" in sent[0].lower()


def test_a_successful_release_is_silent(monkeypatch):
    from agent import runner, ops_alerts
    sent = []
    monkeypatch.setattr("agent.db.socialapi_claim_release", lambda *a, **k: True)
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: sent.append(m))
    assert runner._release_claim("draft-1", "lasso_ig", "why") is True
    assert sent == []


def test_an_alerting_failure_never_breaks_the_publish_pass(monkeypatch):
    """Alerting about a stranded post must not itself strand the pass."""
    from agent import runner, ops_alerts
    monkeypatch.setattr("agent.db.socialapi_claim_release",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(ops_alerts, "alert",
                        lambda m, **k: (_ for _ in ()).throw(RuntimeError("slack down")))
    assert runner._release_claim("draft-1", "lasso_ig", "why") is False
