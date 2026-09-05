"""
AUD-008 / AUD-105 — the comment reply engine fails SILENTLY, fleet wide.

VERIFIED LIVE 2026-09-05 against the shared plane:
  echo_reply_accounts   6 rows, covering eng and topfuel ONLY
  echo_reply_settings   2 rows (eng, topfuel), both last_sync_at NULL
  echo_reply_queue     10 rows, EVERY ONE created 2026-08-31T14:27:20
                       -> five days of zero ingest
  gym_social_accounts  42 rows all carrying a late_account_id; 30 instagram or
                       facebook, of which 26 across 13 gyms have NO mapping

The portal's reply webhook answers HTTP 200 with ignored='account not mapped to a
gym' (or 'gym disabled'), and a 200 is an error to nobody, so 13 of 15 gyms drop
every inbound comment in silence.

AUD-105 is why nobody noticed: the REPLY NEEDED cards come from inbox_alerts.py,
which reads the Zernio inbox directly. No code in agent/ had ever read
echo_reply_queue, so an empty queue produced no signal at all.

Everything offline: the four tables are injected as plain dicts.
"""

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import reply_engine_watch as rew   # noqa: E402

NOW = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)


def _prod_shape():
    """The real production shape, reduced: eng + topfuel mapped, zanshin and
    pierce connected but unmapped, queue frozen on 2026-08-31."""
    return {
        "reply_accounts": [
            {"gym_id": "eng", "platform": "instagram", "late_account_id": "A1"},
            {"gym_id": "topfuel", "platform": "instagram", "late_account_id": "A2"},
        ],
        "reply_settings": [
            {"gym_id": "eng", "enabled": True},
            {"gym_id": "topfuel", "enabled": True},
        ],
        "social_accounts": [
            {"gym_id": "uuid-eng", "platform": "instagram", "late_account_id": "A1"},
            {"gym_id": "uuid-tf", "platform": "instagram", "late_account_id": "A2"},
            {"gym_id": "uuid-zan", "platform": "instagram", "late_account_id": "B1"},
            {"gym_id": "uuid-zan", "platform": "facebook", "late_account_id": "B2"},
            {"gym_id": "uuid-pie", "platform": "instagram", "late_account_id": "B3"},
            # googlebusiness is a different lane and must NOT be called a gap
            {"gym_id": "uuid-zan", "platform": "googlebusiness",
             "late_account_id": "B4"},
        ],
        "queue_rows": [{"gym_id": "eng", "created_at": "2026-08-31T14:27:20"}],
        "gym_names": {"uuid-eng": "eng", "uuid-tf": "top-fuel",
                      "uuid-zan": "zanshin-fitness", "uuid-pie": "pierce-fitness"},
    }


def _audit(d=None, now=NOW):
    d = d or _prod_shape()
    return rew.audit(reply_accounts=d["reply_accounts"],
                     reply_settings=d["reply_settings"],
                     social_accounts=d["social_accounts"],
                     queue_rows=d["queue_rows"], gym_names=d["gym_names"], now=now)


# ---- AUD-008: the mapping gap ----------------------------------------------

def test_unmapped_accounts_are_counted_and_their_gyms_named():
    f = _audit()
    assert f["unmapped"] == 3                      # B1, B2, B3
    assert f["unmapped_gyms"] == ["pierce-fitness", "zanshin-fitness"]
    assert f["mapped"] == 2


def test_a_googlebusiness_account_is_not_called_a_reply_gap():
    # Reviews are a different lane. Naming a gap that is not one is how a
    # watchdog earns its way into the noise filter.
    d = _prod_shape()
    d["social_accounts"] = [r for r in d["social_accounts"]
                            if r["late_account_id"] != "B4"]
    assert _audit(d)["unmapped"] == _audit()["unmapped"]


def test_the_report_says_the_webhook_answers_200_and_throws_comments_away():
    msg = rew.report(_audit())
    assert "200" in msg and "not mapped to a gym" in msg
    assert "zanshin-fitness" in msg and "pierce-fitness" in msg


def test_a_fully_mapped_healthy_fleet_says_nothing():
    d = _prod_shape()
    d["social_accounts"] = [r for r in d["social_accounts"]
                            if r["late_account_id"] in ("A1", "A2")]
    d["queue_rows"] = [{"gym_id": "eng", "created_at": "2026-09-05T09:00:00"}]
    assert rew.report(_audit(d)) == ""


# ---- AUD-105: nobody was watching the queue --------------------------------

def test_a_frozen_queue_is_reported_as_ingest_that_stopped():
    f = _audit()
    assert f["ingest_stale"] is True
    assert f["newest_row"] == "2026-08-31T14:27:20"
    assert f["stale_days_actual"] == 4
    assert "ingests nothing" in rew.report(f)


def test_an_entirely_empty_queue_is_stale_not_healthy():
    d = _prod_shape()
    d["queue_rows"] = []
    f = _audit(d)
    assert f["ingest_stale"] is True, "an empty queue is the silence, not the calm"


def test_a_fresh_queue_row_is_not_stale():
    d = _prod_shape()
    d["queue_rows"] = [{"gym_id": "eng", "created_at": "2026-09-04T09:00:00"}]
    assert _audit(d)["ingest_stale"] is False


# ---- the key-space split this watchdog turned up ---------------------------

def test_a_mapped_enabled_gym_is_never_called_disabled():
    # The reply tables key gym_id by the Echo ACCOUNT KEY; gym_social_accounts
    # keys it by the gyms UUID. Comparing them directly made eng and top-fuel,
    # both healthy, look disabled. The join must go through late_account_id.
    f = _audit()
    assert f["disabled_gyms"] == [], "a false alarm is how a watchdog gets muted"
    assert f["key_space_split"] is True
    assert "key spaces disagree" in rew.key_space_note(f)


def test_a_genuinely_disabled_gym_is_still_caught():
    d = _prod_shape()
    d["reply_settings"] = [{"gym_id": "eng", "enabled": True}]   # topfuel dropped
    f = _audit(d)
    assert f["disabled_gyms"] == ["top-fuel"]
    assert "gym disabled" in rew.report(f)


# ---- the watchdog's posture -------------------------------------------------

class _KV:
    def __init__(self, durable=True):
        self.data = {}
        self._durable = durable

    def kv_get(self, key, default=""):
        return self.data.get(key, default)

    def kv_set(self, key, value):
        self.data[key] = str(value)

    def kv_is_durable(self):
        return self._durable


def _reader():
    return _prod_shape()


def test_flag_off_is_a_noop_that_reads_nothing():
    reads = []

    def _boom():
        reads.append(1)
        raise AssertionError("must not read while the flag is off")

    out = rew.run(reader=_boom, now=NOW)
    assert out["ok"] is False and out["alerted"] is False and reads == []


def test_flag_on_alerts_once_per_day_for_an_unchanged_outage(monkeypatch):
    monkeypatch.setenv("AGENT_REPLY_ENGINE_WATCH", "true")
    kv, said = _KV(), []
    for _ in range(3):
        rew.run(reader=_reader, alert_fn=said.append, db=kv, now=NOW)
    assert len(said) == 1


def test_it_speaks_again_when_the_gap_changes(monkeypatch):
    monkeypatch.setenv("AGENT_REPLY_ENGINE_WATCH", "true")
    kv, said = _KV(), []
    rew.run(reader=_reader, alert_fn=said.append, db=kv, now=NOW)

    def _worse():
        d = _prod_shape()
        d["social_accounts"].append({"gym_id": "uuid-new", "platform": "instagram",
                                     "late_account_id": "C1"})
        return d

    rew.run(reader=_worse, alert_fn=said.append, db=kv, now=NOW)
    assert len(said) == 2, "a newly connected unmapped gym is new information"


def test_an_unreadable_plane_never_reports_a_false_all_clear(monkeypatch):
    monkeypatch.setenv("AGENT_REPLY_ENGINE_WATCH", "true")

    def _fails():
        raise RuntimeError("postgrest down")

    out = rew.run(reader=_fails, alert_fn=lambda t: None, db=_KV(), now=NOW)
    assert out["ok"] is False and out["alerted"] is False
    assert "read failed" in out["reason"]


def test_an_ephemeral_kv_stays_silent_rather_than_alerting_every_run(monkeypatch):
    monkeypatch.setenv("AGENT_REPLY_ENGINE_WATCH", "true")
    said = []
    rew.run(reader=_reader, alert_fn=said.append, db=_KV(durable=False), now=NOW)
    assert said == []


def test_the_watchdog_never_offers_to_repair_the_mapping_itself(monkeypatch):
    # Mapping a gym ARMS replying on that client's behalf. That is a person's
    # decision and this module must say so rather than quietly doing it.
    monkeypatch.setenv("AGENT_REPLY_ENGINE_WATCH", "true")
    said = []
    rew.run(reader=_reader, alert_fn=said.append, db=_KV(), now=NOW)
    assert "Nothing here is auto repaired" in said[0]
    assert "a person's decision" in said[0]
