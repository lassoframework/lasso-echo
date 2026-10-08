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
    assert fact["window"] == "2026-10-08 onward"
    by = {r["reason"]: r for r in fact["reasons"]}
    assert by[al._HOLD_REVIEW_TEXT]["count"] == 3
    assert by[al._HOLD_REVIEW_TEXT]["post_dates"] == ["2026-10-08", "2026-10-09", "2026-11-02"]
    assert by[al._HOLD_REPEAT_TEXT]["post_dates"] == ["2026-10-10"]
    dumped = json.dumps(fact)
    for internal in ("cross_gym", "astra", "Cross-day", "cross_date_media_repeat"):
        assert internal not in dumped


def test_no_holds_is_an_explicit_zero_fact():
    fact = al.posts_on_hold_fact(_Store({"2026-10": [_row("2026-10-09")]}), "k", today=TODAY)
    assert fact["count"] == 0 and "reasons" not in fact


def test_december_rolls_into_january():
    store = _Store({})
    al.posts_on_hold_fact(store, "k", today=date(2026, 12, 30))
    assert store.calls == [("k", "2026-12"), ("k", "2027-01")]


def test_default_fetch_state_includes_hold_fact_and_survives_failure(monkeypatch):
    import agent.portal_calendar_store as pcs

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
                  "posts_on_hold": {"count": 2, "window": "2026-10-08 onward",
                                    "scope": al._HOLD_SCOPE_TEXT,
                                    "reasons": [{"reason": al._HOLD_REVIEW_TEXT, "count": 2,
                                                 "post_dates": ["2026-10-08", "2026-10-09"]}]}}
    ident = types.SimpleNamespace(name="echo", product="echo")
    who = types.SimpleNamespace(kind="client", account_key="k")
    seen = {}

    def llm(system, user):
        seen["user"] = user
        return "Two of your upcoming posts are on hold while we review their photos."

    out = al.answer({"raw_text": "Why is account on hold?"}, who, [],
                    "Why is account on hold?", identity=ident,
                    fetch_state=lambda t, w: store_fact, llm=llm)
    assert out and out["grounding"]["facts"]["posts_on_hold"]["count"] == 2
    assert "posts_on_hold" in seen["user"]
