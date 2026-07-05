"""
Calendar assembler tests (calendar Part A). Offline. Asserts: the assembled
month matches the draft queue and posts state exactly (the same store the
Slack cards read); rest days honor the posting schedule (Saturday skip);
specials tag from real draft evidence plus the Monday podcast expectation;
an empty posting day emits an open draft slot and NEVER an invented concept;
the run is read only (store byte identical).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_artifact, db  # noqa: E402

MONTH = "2026-07"


def _seed(account_key="lasso_ig"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, "
            "media_id, mode, published_at, creative_key) VALUES (?,?,?,?,?,?,?,?)",
            ("d1", account_key, "instagram", "published caption", "m1",
             "published", "2026-07-01T14:00:00", "lasso_v2_one_screen.png"))
        for draft_id, day, status, dtype, path, caption in (
                ("d2", "2026-07-02", "approved", "feed",
                 "lib/lasso_v2_b2b_16_cpl.png", "approved caption"),
                ("d3", "2026-07-03", "pending", "feed",
                 "lib/lasso_v2_platform_719_booking.png", "pending caption"),
                ("d6", "2026-07-06", "pending", "podcast",
                 "lib/podcast_release.png", "EPISODE 140: fresh episode"),
                ("d7", "2026-07-07", "pending", "book",
                 "lib/book_card.png", "book caption")):
            conn.execute(
                "INSERT INTO drafts (draft_id, account_key, status, day_key, "
                "draft_type, data) VALUES (?,?,?,?,?,?)",
                (draft_id, account_key, status, day, dtype,
                 json.dumps({"creative_path": path, "caption": caption})))
        conn.commit()


def _by_date(plan):
    return {d["date"]: d for d in plan["days"]}


def test_assembler_matches_store_state(monkeypatch):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    _seed()
    plan = calendar_artifact.assemble_month("lasso_ig", MONTH)
    days = _by_date(plan)
    assert len(plan["days"]) == 31
    d1 = days["2026-07-01"]
    assert d1["status"] == "published"
    assert d1["concept"] == "lasso_v2_one_screen.png"
    assert d1["caption"] == "published caption"
    assert (d1["canvas"], d1["layout"]) == ("cream", "poster")  # house default
    d2 = days["2026-07-02"]
    assert d2["status"] == "approved"
    assert d2["concept"] == "lasso_v2_b2b_16_cpl.png"
    assert (d2["canvas"], d2["layout"]) == ("cream", "stat_hero")  # b2b brief
    d3 = days["2026-07-03"]
    assert d3["status"] == "pending"
    assert (d3["canvas"], d3["layout"]) == ("navy", "stat_hero")  # platform brief
    assert plan["rollup"]["published"] == 1
    assert plan["rollup"]["approved"] == 1
    assert plan["rollup"]["pending"] == 3


def test_rest_days_honor_schedule(monkeypatch):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    plan = calendar_artifact.assemble_month("lasso_ig", MONTH)
    for d in plan["days"]:
        from datetime import date
        is_saturday = date.fromisoformat(d["date"]).weekday() == 5
        if is_saturday:
            assert d["status"] == "rest", d["date"]    # the Saturday skip
        else:
            assert d["status"] != "rest", d["date"]


def test_specials_tagged_from_evidence_and_schedule(monkeypatch):
    _seed()
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    days = _by_date(calendar_artifact.assemble_month("lasso_ig", MONTH))
    assert days["2026-07-06"]["special"] == "podcast"          # draft evidence
    assert days["2026-07-07"]["special"] == "book campaign"
    assert days["2026-07-13"]["special"] == ""                 # flag off: no tag
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    days = _by_date(calendar_artifact.assemble_month("lasso_ig", MONTH))
    assert days["2026-07-13"]["special"] == "podcast release day"  # a Monday


def test_empty_days_never_fabricate(monkeypatch):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    plan = calendar_artifact.assemble_month("lasso_ig", MONTH)  # nothing seeded
    for d in plan["days"]:
        assert d["concept"] == "" and d["caption"] == "", d    # never invented
        assert d["status"] in ("draft", "rest")
    assert plan["rollup"]["draft"] + plan["rollup"]["rest"] == 31


def test_assembler_is_read_only(monkeypatch):
    _seed()
    db.kv_set("approved_calendar_lasso_ig_2026-07",
              json.dumps(["lasso_v2_one_screen.png"]))
    with db.connect() as conn:
        before = "\n".join(conn.iterdump())
    plan = calendar_artifact.assemble_month("lasso_ig", MONTH)
    assert plan["seeded_keys"] == ["lasso_v2_one_screen.png"]
    with db.connect() as conn:
        assert "\n".join(conn.iterdump()) == before
