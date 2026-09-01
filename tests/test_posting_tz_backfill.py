"""tests/test_posting_tz_backfill.py — audit item 4, 2026-08-31.

Nine of sixteen gyms in the worker registry had gyms.posting_timezone NULL, so
config.posting_timezone_for() handed them the GLOBAL default and their posts landed at
Echo's hour rather than their own. Nothing but a human `set-timezone` had ever written
that column, and nothing ever alerted that it was empty.

These tests pin the two halves and — the part that matters most — the refusal to guess:
a gym with no evidence stays NULL and gets named in an alert, because writing a
plausible default would destroy the exact signal the watchdog reads.

Fully offline: injected registry rows, an injected GBP map, and an injected document
reader. No volume, no Supabase, no network.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent.jobs import posting_tz_backfill as tzjob  # noqa: E402


def _row(key, tz=None, name=""):
    return {"account_key": key, "posting_timezone": tz, "display_name": name}


def _reader(mapping):
    """Serve document text by BASENAME-bearing path, so tests do not depend on /data."""
    def read(path):
        for needle, text in mapping.items():
            if needle in path:
                return text
        return ""
    return read


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.delenv("AGENT_POSTING_TZ_WATCH", raising=False)


# ---- 1. evidence priority ------------------------------------------------------

def test_connected_gbp_location_is_the_strongest_evidence():
    gbp = {"zanshinfitness630e22": ("America/New_York", "CrossFit Zanshin")}
    writes, _already, unresolved = tzjob.plan(
        [_row("zanshinfitness630e22")], gbp, read=_reader({}))
    assert unresolved == []
    assert writes[0]["tz"] == "America/New_York"
    assert "CrossFit Zanshin" in writes[0]["evidence"]


def test_falls_back_to_the_gyms_own_brand_bible():
    """CrossFit Newtown has no GBP connection; its own bible says where it is."""
    read = _reader({"crossfitnewtown": (
        "CrossFit Newtown is located in the heart of Newtown, PA and is dedicated "
        "to providing an environment where members are challenged.")})
    writes, _a, unresolved = tzjob.plan([_row("crossfitnewtown")], {}, read=read)
    assert unresolved == []
    assert writes[0]["tz"] == "America/New_York"
    assert "Newtown, PA" in writes[0]["evidence"]


def test_gbp_wins_over_a_document_mention():
    read = _reader({"eng": "We started in Denver, CO before moving."})
    gbp = {"eng": ("America/New_York", "CrossFit ENG")}
    writes, _a, _u = tzjob.plan([_row("eng")], gbp, read=read)
    assert writes[0]["tz"] == "America/New_York"
    assert "Google Business" in writes[0]["evidence"]


@pytest.mark.parametrize("place,expected", [
    ("Chapel Hill, NC", "America/New_York"),
    ("Bethesda, MD", "America/New_York"),
    ("Conway, NH", "America/New_York"),
    ("Boulder, CO", "America/Denver"),
])
def test_real_document_places_resolve(place, expected):
    read = _reader({"g": f"Our gym has served {place} since 2014."})
    writes, _a, _u = tzjob.plan([_row("g")], {}, read=read)
    assert writes and writes[0]["tz"] == expected


# ---- 2. the refusal to guess ---------------------------------------------------

def test_a_gym_with_no_evidence_is_left_null_and_named():
    """The Bolton Club: no connected GBP row, and no city/state anywhere in its own
    copy. Writing a default here would look like success and destroy the signal."""
    seen = []
    out = tzjob.run(apply=True, rows=[_row("theboltonclub", name="The Bolton Club")],
                    gbp_map={}, read=_reader({}),
                    writer=lambda g, t: pytest.fail("must not write a guess"),
                    alert=seen.append)
    assert out["writes"] == []
    assert [u["gym"] for u in out["unresolved"]] == ["theboltonclub"]
    assert len(seen) == 1
    assert "theboltonclub" in seen[0]


def test_no_alert_when_every_gym_has_a_timezone():
    seen = []
    out = tzjob.run(apply=True, rows=[_row("eng", "America/New_York")],
                    gbp_map={}, read=_reader({}), writer=lambda g, t: None,
                    alert=seen.append)
    assert seen == []
    assert out["unresolved"] == []


def test_an_invalid_timezone_is_never_written():
    """A bad map entry must not reach the publish lane; ZoneInfo is the gate."""
    writes, _a, unresolved = tzjob.plan(
        [_row("g")], {"g": ("Mars/Olympus_Mons", "Bad Row")}, read=_reader({}))
    assert writes == []
    assert [u["gym"] for u in unresolved] == ["g"]


def test_a_bare_two_letter_word_is_not_read_as_a_state():
    """'IN', 'OR' and 'OK' are ordinary words. Only a real 'City, ST' counts."""
    read = _reader({"g": "Come IN and train. It is OK to be new here, OR just watch."})
    writes, _a, unresolved = tzjob.plan([_row("g")], {}, read=read)
    assert writes == []
    assert [u["gym"] for u in unresolved] == ["g"]


def test_an_unconnected_gbp_row_is_not_evidence():
    gbp_rows = [{"portal_gym_key": "g", "timezone": "America/Denver",
                 "status": "pending", "location_name": "Somewhere"}]

    class Store:
        def available(self):
            return True

        def all_connections(self):
            return gbp_rows

    assert tzjob.gbp_timezones(store=Store()) == {}


# ---- 3. never overwrite a human ------------------------------------------------

def test_an_existing_timezone_is_never_re_decided():
    """`python -m agent set-timezone` always wins; this job only fills a NULL."""
    gbp = {"topfuel": ("America/Indianapolis", "Top Fuel CrossFit")}
    writes, already, _u = tzjob.plan([_row("topfuel", "America/Chicago")], gbp,
                                     read=_reader({}))
    assert writes == []
    assert already == [{"gym": "topfuel", "tz": "America/Chicago"}]


def test_apply_false_writes_nothing():
    calls = []
    out = tzjob.run(apply=False, rows=[_row("g")],
                    gbp_map={"g": ("America/Denver", "G")},
                    read=_reader({}), writer=lambda *a: calls.append(a),
                    alert=lambda m: None)
    assert out["writes"] and out["written"] == 0
    assert calls == []


def test_apply_writes_only_the_timezone_column():
    calls = []
    out = tzjob.run(apply=True, rows=[_row("g")],
                    gbp_map={"g": ("America/Denver", "G")},
                    read=_reader({}),
                    writer=lambda gym, tz: calls.append((gym, tz)),
                    alert=lambda m: None)
    assert calls == [("g", "America/Denver")]
    assert out["written"] == 1


def test_one_write_failure_never_blocks_the_others():
    def writer(gym, tz):
        if gym == "bad":
            raise RuntimeError("db locked")

    out = tzjob.run(apply=True, rows=[_row("bad"), _row("good")],
                    gbp_map={"bad": ("America/Denver", "B"),
                             "good": ("America/New_York", "G")},
                    read=_reader({}), writer=writer, alert=lambda m: None)
    assert out["written"] == 1


# ---- 4. the flag ---------------------------------------------------------------

def test_flag_defaults_on():
    assert config.posting_tz_watch_enabled() is True


def test_flag_off_is_a_true_noop(monkeypatch):
    monkeypatch.setenv("AGENT_POSTING_TZ_WATCH", "false")
    seen = []
    out = tzjob.run(apply=True, rows=[_row("theboltonclub")], gbp_map={},
                    read=_reader({}),
                    writer=lambda *a: pytest.fail("must not write"),
                    alert=seen.append)
    assert out["ok"] is False
    assert seen == []
