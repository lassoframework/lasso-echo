"""Posts-on-hold grounding for the answer lane (Tough Temple ticket effb834f, 2026-10-08)."""
import json
import sys
import types
from datetime import date

from agent.slack_convo import answer_lane as al


class _Store:
    def __init__(self, months):
        self.months = months
        self.calls = []

    def list_month(self, account_key, month):
        self.calls.append((account_key, month))
        return list(self.months.get(month, []))


def _row(day, status="pending", reason=None):
    return {"post_date": day, "status": status, "media_not_ready_reason": reason}


TODAY = date(2026, 10, 8)


def test_counts_only_current_nonterminal_held_rows_with_client_safe_reasons():
    store = _Store({
        "2026-10": [
            _row("2026-10-05", reason="cross_gym_media_source_mismatch_review_required"),  # past
            _row("2026-10-08", reason="cross_gym_media_source_mismatch_review_required"),
            _row("2026-10-09", status="approved", reason="Cross-day exact image URL repeat review"),
            _row("2026-10-10", reason="cross_date_media_repeat_needs_new_visual"),
            _row("2026-10-11", status="published", reason="stale"),                     # terminal
            _row("2026-10-12", status="denied", reason="stale"),                        # terminal
            _row("2026-10-13", reason="   "),                                           # blank
            _row("2026-10-14"),                                                         # not held
        ],
        "2026-11": [_row("2026-11-02", reason="client_infographic_needs_verified_astra_brand_artifact")],
    })
    fact = al.posts_on_hold_fact(store, "gymkey", today=TODAY)
    assert store.calls == [("gymkey", "2026-10"), ("gymkey", "2026-11")]
    assert fact["count"] == 4
    assert fact["window"]["from"] == "2026-10-08"
    assert fact["window"]["through"] == "2026-11-30"
    by = {r["reason"]: r for r in fact["reasons"]}
    assert al._HOLD_REVIEW_TEXT not in by
    assert by[al._HOLD_REPEAT_TEXT]["count"] == 3
    assert by[al._HOLD_REPEAT_TEXT]["post_dates"] == ["2026-10-08", "2026-10-09", "2026-10-10"]
    assert by[al._HOLD_NOT_READY_TEXT]["post_dates"] == ["2026-11-02"]
    dumped = json.dumps(fact)
    for internal in ("cross_gym", "astra", "Cross-day", "cross_date_media_repeat"):
        assert internal not in dumped


def test_no_holds_is_an_explicit_zero_fact_with_a_bounded_window():
    fact = al.posts_on_hold_fact(_Store({"2026-10": [_row("2026-10-09")]}), "k", today=TODAY)
    assert fact["count"] == 0 and "reasons" not in fact
    assert fact["window"]["through"] == "2026-11-30" and fact["window"]["note"]


def test_unknown_reasons_get_cause_neutral_text_not_an_invented_review():
    store = _Store({"2026-10": [
        _row("2026-10-09", reason="paired_feed_not_ready"),
        _row("2026-10-10", reason="purpose_built_media_required"),
        _row("2026-10-11", reason="needs_verified_client_astra_infographic"),
        _row("2026-10-12", reason="caption_changed_needs_new_visual"),
        _row("2026-10-13", reason="global_cross_date_media_repeat"),
        _row("2026-10-14", reason="Cross-day image repeat review 2026-10-05"),
        _row("2026-10-15", reason="Incident approval provenance review 2026-10-05"),
        _row("2026-10-16", reason="caption_internal_clarification_needs_review"),
        _row("2026-10-17", reason="scene_review_hold"),
    ]})
    fact = al.posts_on_hold_fact(store, "k", today=TODAY)
    by = {r["reason"]: r["post_dates"] for r in fact["reasons"]}
    assert by[al._HOLD_PAIRED_STORY_TEXT] == ["2026-10-09"]
    assert by[al._HOLD_NOT_READY_TEXT] == ["2026-10-10", "2026-10-11", "2026-10-15",
                                           "2026-10-16"]
    assert by[al._HOLD_CAPTION_CHANGED_TEXT] == ["2026-10-12"]
    assert by[al._HOLD_REPEAT_TEXT] == ["2026-10-13", "2026-10-14"]
    # Only a real manual-review queue says LASSO is reviewing.
    assert by[al._HOLD_REVIEW_TEXT] == ["2026-10-17"]
    dumped = json.dumps(fact)
    for internal in ("paired_feed", "purpose_built", "astra", "global_cross", "Cross-day"):
        assert internal not in dumped


def test_coach_review_rows_are_not_client_facts():
    store = _Store({"2026-10": [
        _row("2026-10-09", status="coach_review", reason="cross_gym_media_source_mismatch_review_required"),
    ]})
    assert al.posts_on_hold_fact(store, "k", today=TODAY)["count"] == 0


def test_today_defaults_to_the_gym_posting_timezone(monkeypatch):
    from agent import config
    monkeypatch.setattr(config, "posting_timezone_for", lambda key: "Pacific/Pago_Pago")
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    expected = datetime.now(ZoneInfo("Pacific/Pago_Pago")).date()
    assert al._gym_today("k") == expected
    monkeypatch.setattr(config, "posting_timezone_for",
                        lambda key: (_ for _ in ()).throw(RuntimeError("db down")))
    assert al._gym_today("k") == date.today()


def test_december_rolls_into_january():
    store = _Store({})
    al.posts_on_hold_fact(store, "k", today=date(2026, 12, 30))
    assert store.calls == [("k", "2026-12"), ("k", "2027-01")]


def test_default_fetch_state_includes_hold_fact_and_survives_failure(monkeypatch):
    import agent.portal_calendar_store as pcs
    monkeypatch.setattr(al, "_gym_today", lambda key: date.today())

    class FakeStore:
        def list_month(self, key, month):
            if month == date.today().strftime("%Y-%m"):
                return [_row(date.today().isoformat(),
                             reason="cross_gym_media_source_mismatch_review_required")]
            return []

    monkeypatch.setattr(pcs, "SupabaseCalendarStore", FakeStore)
    fake_zr = types.ModuleType("agent.zernio_routes")
    fake_zr.handle_social_status = lambda key: (200, {"platforms": {}})
    monkeypatch.setitem(sys.modules, "agent.zernio_routes", fake_zr)
    import agent as _agent
    monkeypatch.setattr(_agent, "zernio_routes", fake_zr, raising=False)
    who = types.SimpleNamespace(kind="client", account_key="gymkey")
    facts = al.default_fetch_state({}, who)
    assert facts["posts_on_hold"]["count"] >= 1
    assert facts["calendar_this_month"] == {"pending": 1}

    class Broken:
        def list_month(self, key, month):
            raise RuntimeError("boom")

    monkeypatch.setattr(pcs, "SupabaseCalendarStore", Broken)
    facts = al.default_fetch_state({}, who)
    assert facts["posts_on_hold"] == {"unavailable": "RuntimeError"}


def test_answer_with_only_hold_fact_is_grounded():
    store_fact = {"identity_kind": "client", "account_key": "k",
                  "posts_on_hold": {"count": 2, "window": {"from": "2026-10-08",
                                                                "through": "2026-11-30"},
                                    "scope": al._HOLD_SCOPE_TEXT,
                                    "reasons": [{"reason": al._HOLD_REPEAT_TEXT, "count": 2,
                                                 "post_dates": ["2026-10-08", "2026-10-09"]}]}}
    ident = types.SimpleNamespace(name="echo", product="echo")
    who = types.SimpleNamespace(kind="client", account_key="k")
    seen = {}

    def llm(system, user):
        seen["user"] = user
        return "Two of your upcoming posts are on hold until they get a different photo."

    out = al.answer({"raw_text": "Why is account on hold?"}, who, [],
                    "Why is account on hold?", identity=ident,
                    fetch_state=lambda t, w: store_fact, llm=llm)
    assert out and out["grounding"]["facts"]["posts_on_hold"]["count"] == 2
    assert "posts_on_hold" in seen["user"]


def test_tough_temple_live_reasons_say_different_photo_not_review():
    """Verifier finding on #353: both live Tough Temple reasons contain 'review' but no one
    is reviewing them; they clear when the visual is swapped."""
    for reason in ("cross_gym_media_source_mismatch_review_required",
                   "Cross-day exact image URL repeat review 2026-10-05"):
        assert al._hold_text(reason) == al._HOLD_REPEAT_TEXT, reason
    store = _Store({"2026-10": [
        _row("2026-10-11", reason="cross_gym_media_source_mismatch_review_required"),
        _row("2026-10-14", status="approved",
             reason="Cross-day exact image URL repeat review 2026-10-05"),
    ]})
    fact = al.posts_on_hold_fact(store, "toughtemple52040e", today=TODAY)
    assert fact["count"] == 2
    assert [r["reason"] for r in fact["reasons"]] == [al._HOLD_REPEAT_TEXT]
    assert "review" not in json.dumps(fact["reasons"]).lower()


def test_different_photo_wording_states_no_cause():
    """The different-photo sentence covers repeats AND cross-gym source holds (Tough Temple
    fa939a9a is legacy igfill media, not a repeat), so it must not claim a repeat as cause."""
    assert al._HOLD_REPEAT_TEXT == "needs a different photo or video"
    for reason in ("cross_gym_media_source_mismatch_review_required",
                   "Cross-day exact image URL repeat review 2026-10-05",
                   "cross_date_media_repeat_needs_new_visual"):
        text = al._hold_text(reason)
        assert text == al._HOLD_REPEAT_TEXT
        assert "two different days" not in text and "because" not in text

