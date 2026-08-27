"""Welcome drip pacing + once-ever dedup (the 2026-08-27 burst: 5 welcomes in
15 minutes, Pierce twice), all offline.

The rails are DURABLE (kv) and checked BEFORE the publish call:
  * a (gym, account, kind) welcome publishes at most once, EVER;
  * at most AGENT_WELCOME_PER_DAY distinct gyms are welcomed per day fleet-wide;
  * a cap-blocked gym is REQUEUED (never lost); a duplicate is dropped;
  * publish_extra_welcomes stops serving once the day's cap is met, restarts
    included.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import welcome_queue as wq
from agent.drafter import DraftStatus


def _draft(gym_key="pierce", account_key="lasso_ig", story=False,
           day_key="2026-08-27"):
    from agent.drafter import Draft
    d = Draft(
        draft_id=f"welc{'s' if story else 'f'}_{gym_key}",
        account_key=account_key, platform="instagram",
        caption=f"Welcome to the LASSO family, {gym_key}.", hashtags=[],
        creative_path=f"welcome_{gym_key}{'_story' if story else ''}.png",
        creative_public_url="https://cdn/w.png", scheduled_for=day_key,
        status=DraftStatus.PENDING, day_key=day_key,
        draft_type="story" if story else "feed", is_story=story,
        topic_type="WELCOME")
    d.welcome_gym_key = gym_key
    return d


def test_once_ever_a_welcome_never_repeats():
    d = _draft("pierce")
    ok, _ = wq.welcome_publish_gate(d)
    assert ok
    wq.record_welcome_published(d)
    ok2, why = wq.welcome_publish_gate(d)
    assert not ok2 and "once-ever" in why
    # a different day changes nothing: Pierce NEVER gets a second feed welcome
    ok3, why3 = wq.welcome_publish_gate(_draft("pierce", day_key="2026-08-28"))
    assert not ok3 and "once-ever" in why3


def test_cross_post_and_story_are_not_duplicates():
    """The SAME gym's feed on lasso_fb and story on lasso_ig are part of ONE
    welcome, not repeats."""
    wq.record_welcome_published(_draft("eng", "lasso_ig"))
    assert wq.welcome_publish_gate(_draft("eng", "lasso_fb"))[0]
    assert wq.welcome_publish_gate(_draft("eng", "lasso_ig", story=True))[0]


def test_daily_fleet_cap_blocks_new_gyms(monkeypatch):
    monkeypatch.setenv("AGENT_WELCOME_PER_DAY", "1")
    wq.record_welcome_published(_draft("sycamore"))
    ok, why = wq.welcome_publish_gate(_draft("westwood"))
    assert not ok and "cap" in why
    # the day's already-counted gym still finishes its fan-out
    assert wq.welcome_publish_gate(_draft("sycamore", "lasso_fb"))[0]
    # tomorrow the next gym drips out
    assert wq.welcome_publish_gate(_draft("westwood", day_key="2026-08-28"))[0]


def test_unidentifiable_gym_fails_closed():
    d = _draft("x")
    d.welcome_gym_key = ""
    d.creative_path = "not_a_welcome.png"
    ok, why = wq.welcome_publish_gate(d)
    assert not ok and "closed" in why


def test_gym_key_parsed_from_creative_path():
    d = _draft("portal:abc_def", story=True)
    d.welcome_gym_key = ""                       # older drafts lack the attribute
    assert wq.welcome_gym_key_for(d) == "portal:abc_def"


def test_requeue_puts_served_gym_back_in_the_drip():
    with wq._conn() as conn:
        conn.execute("INSERT INTO welcome_queue (gym_key, name, caption, feed_url, "
                     "status) VALUES ('boltonclub', 'Bolton Club', 'cap', 'u', "
                     "'queued')")
        conn.commit()
    item = wq.serve_one_more("2026-08-27")
    assert item["gym_key"] == "boltonclub"
    assert wq.serve_one_more("2026-08-27") is None       # queue drained
    wq.requeue("boltonclub")
    assert wq.serve_one_more("2026-08-28")["gym_key"] == "boltonclub"


def test_extras_lane_stops_at_the_durable_cap(monkeypatch):
    """A refired draw calling publish_extra_welcomes again must NOT pop more
    gyms once the day's published cap is met — the kv record survives the
    restart, the old in-memory loop bound did not."""
    monkeypatch.setenv("AGENT_WELCOME_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AGENT_WELCOME_PER_DAY", "2")
    with wq._conn() as conn:
        for g in ("g1", "g2", "g3"):
            conn.execute("INSERT INTO welcome_queue (gym_key, name, caption, "
                         "feed_url, status) VALUES (?, ?, 'cap', 'u', 'queued')",
                         (g, g))
        conn.commit()
    # the day already welcomed 2 gyms (the drip + one extra, first pass)
    wq.record_welcome_published(_draft("g1", day_key="2026-08-27"))
    wq.record_welcome_published(_draft("g2", day_key="2026-08-27"))
    posted = wq.publish_extra_welcomes("2026-08-27", post_fn=lambda d: None)
    assert posted == []                                   # cap met: serves nothing
    # every queued gym is still queued for tomorrow (nothing burned)
    assert {r["gym_key"] for r in wq.queue_status()
            if r["status"] == "queued"} == {"g1", "g2", "g3"}
