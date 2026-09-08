"""
Slack "cancel/skip a scheduled post" feature (AGENT_SLACK_CANCEL_POST_ENABLED).

Joe Floria (demo_app--info538--mail) reported real confusion: he could not find any way
to cancel or skip a scheduled post from Slack. The only existing mechanism was the
portal's own Deny button (agent/portal_social.py handle_deny, token isolated, budgeted
30/month, publish gate respected). This feature makes THAT SAME mechanism reachable from
a plain Slack message, through the existing conversational adapter
(agent/slack_convo/*), rather than inventing a second, parallel, inconsistent
mechanism or a second state machine.

Three layers under test:
  1. classifier.classify -- "cancel my post" / "skip today's post" text is recognized
     as CANCEL_POST, gated end to end on cancel_post_enabled (byte identical off).
  2. cancel_lane.cancel_post -- resolves WHICH content_calendar row a bare cancel
     request means (today / tomorrow / the next upcoming eligible one), then denies
     it through portal_social.handle_deny -- never a second write path.
  3. adapter.handle_event -- end to end through Slack's own event/Deps shape (the
     same driving pattern test_slack_convo.py uses for every other lane), proving:
       - only a resolved CLIENT's own account_key can ever be touched (never another
         gym's, and never when identity resolution failed to pin one account down)
       - the client gets a clear, correct confirmation back
       - the flag being off is byte identical to today (no regression risk)
"""
import os
import sys
import uuid
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_social as ps  # noqa: E402
from agent.slack_convo import adapter as A  # noqa: E402
from agent.slack_convo import cancel_lane as CL  # noqa: E402
from agent.slack_convo import classifier as C  # noqa: E402
from agent.slack_convo import identities as IDS  # noqa: E402
from agent.slack_convo import identity_gate as IG  # noqa: E402


# ==========================================================================
# 1. classifier.classify: detection + the flag as the ONLY gate
# ==========================================================================

@pytest.mark.parametrize("text", [
    "cancel my post today",
    "can you skip tomorrow's post",
    "please skip tomorrow's scheduled post",
    "stop the post for today",
    "pull the post today please",
    "don't post today",
    "do not post tomorrow",
    "hold off on posting today",
])
def test_classify_recognizes_cancel_post_when_enabled(text):
    assert C.classify(text, has_open_ticket=False, identity_product="echo",
                      cancel_post_enabled=True) == C.CANCEL_POST


@pytest.mark.parametrize("text", [
    "cancel my post today",
    "can you skip tomorrow's post",
])
def test_classify_never_returns_cancel_post_when_flag_off(text):
    """Byte identical to before this label existed when the flag is off."""
    label = C.classify(text, has_open_ticket=False, identity_product="echo",
                       cancel_post_enabled=False)
    assert label != C.CANCEL_POST


def test_flag_off_a_question_shaped_cancel_falls_to_question_not_escalate_change():
    """'can you cancel my post today' starts with 'can you' (_QUESTION_RE) -- with the
    flag OFF this must classify exactly as it did before CANCEL_POST existed: QUESTION.
    This is the literal regression check: adding the label must not change ANY existing
    classification when the flag is off."""
    assert C.classify("can you cancel my post today", has_open_ticket=False,
                      identity_product="echo", cancel_post_enabled=False) == C.QUESTION


def test_flag_on_the_same_question_shaped_cancel_now_wins_over_question():
    """With the flag ON, the SAME message no longer falls into the QUESTION/billing trap
    (answer_lane._BILLING_RE matches 'cancel my', which used to escalate this with no
    help -- exactly Joe Floria's story)."""
    assert C.classify("can you cancel my post today", has_open_ticket=False,
                      identity_product="echo", cancel_post_enabled=True) == C.CANCEL_POST


def test_cancel_word_alone_with_unrelated_noun_never_matches():
    """Narrow on purpose: 'cancel' about something else (a membership, a subscription)
    must never be read as a content_calendar action."""
    assert C.classify("please cancel my membership", has_open_ticket=False,
                      identity_product="echo", cancel_post_enabled=True) != C.CANCEL_POST


def test_llm_or_brain_hint_cannot_smuggle_cancel_post_past_the_flag():
    """The flag is the ONE switch: even an injected llm/brain_hint returning CANCEL_POST
    must not escape a flag-off classify() call."""
    label = C.classify("something ambiguous entirely", has_open_ticket=False,
                       identity_product="echo", cancel_post_enabled=False,
                       llm=lambda t: C.CANCEL_POST)
    assert label != C.CANCEL_POST


def test_ranger_action_words_still_win_for_ranger_identity():
    """Ranger's own 'kill'/'stop the ad' verbs must still route to ACTION_REQUEST, not
    be stolen by the new cancel-post regex, for a ranger identity."""
    assert C.classify("please pause the ad", has_open_ticket=False,
                      identity_product="ranger", cancel_post_enabled=True) \
        == C.ACTION_REQUEST


# ==========================================================================
# 2. cancel_lane.cancel_post: row resolution + the actual write
# ==========================================================================

def _row(row_id, post_date, status="pending", gym_id="crossfitlocal"):
    return {"id": row_id, "gym_id": gym_id, "post_date": post_date, "status": status,
           "account": "instagram", "caption": "hello", "image_url": "https://cdn/x.jpg",
           "pillar": "education", "format": "feed", "scheduled_at": None}


class _FakeRangeStore:
    """Stands in for SupabaseCalendarStore for cancel_lane + portal_social.handle_deny,
    both of which this feature calls. Mirrors gym isolation (a cross-gym id never loads
    or writes) the same way test_portal_social_supabase.py's _FakeStore does."""

    def __init__(self, rows=None):
        self._rows = {r["id"]: dict(r) for r in (rows or [])}
        self.patches = []

    def rows_in_range(self, account_key, start_iso, end_iso):
        out = [dict(r) for r in self._rows.values()
              if r.get("gym_id") == account_key
              and start_iso <= (r.get("post_date") or "") <= end_iso]
        out.sort(key=lambda r: r.get("post_date") or "")
        return out

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        return dict(r)

    def set_status(self, account_key, row_id, new_status):
        self.patches.append((row_id, new_status))
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        r["status"] = new_status
        return dict(r)


@pytest.fixture(autouse=True)
def _social_env(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    yield


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    yield


def test_bare_cancel_targets_the_next_upcoming_eligible_row(db_tmp):
    today = date(2026, 9, 8)
    store = _FakeRangeStore([
        _row("past-1", "2026-09-01", status="published"),
        _row("next-1", "2026-09-09", status="pending"),
        _row("later-1", "2026-09-20", status="approved"),
    ])
    result = CL.cancel_post("crossfitlocal", "U_client", "cancel my post",
                            sb_store=store, today=today)
    assert result["ok"] is True
    assert result["row"]["id"] == "next-1"
    assert store.patches == [("next-1", "denied")]
    assert "2026-09-09" in result["body"]


def test_today_hint_targets_only_todays_row(db_tmp):
    today = date(2026, 9, 8)
    store = _FakeRangeStore([
        _row("today-1", "2026-09-08", status="pending"),
        _row("tmrw-1", "2026-09-09", status="pending"),
    ])
    result = CL.cancel_post("crossfitlocal", "U_client", "skip today's post",
                            sb_store=store, today=today)
    assert result["ok"] is True
    assert result["row"]["id"] == "today-1"
    assert store.patches == [("today-1", "denied")]


def test_tomorrow_hint_targets_only_tomorrows_row(db_tmp):
    today = date(2026, 9, 8)
    store = _FakeRangeStore([
        _row("today-1", "2026-09-08", status="pending"),
        _row("tmrw-1", "2026-09-09", status="pending"),
    ])
    result = CL.cancel_post("crossfitlocal", "U_client", "cancel tomorrow's post",
                            sb_store=store, today=today)
    assert result["ok"] is True
    assert result["row"]["id"] == "tmrw-1"
    assert store.patches == [("tmrw-1", "denied")]


def test_find_target_row_itself_never_offers_a_published_row():
    """Direct unit test of cancel_lane's OWN filter, independent of the second
    guard portal_social.handle_deny applies at write time -- this must refuse a
    published row on its own, not merely rely on being masked by that second gate."""
    store = _FakeRangeStore([_row("live-1", "2026-09-08", status="published")])
    row = CL.find_target_row(store, "crossfitlocal", today=date(2026, 9, 8))
    assert row is None


def test_already_published_row_is_never_offered_as_a_target(db_tmp):
    """A published post is never cancellable; find_target_row must skip it entirely
    (and the write path's own _published_is_final would refuse it as a second,
    independent guard even if this filter were ever wrong)."""
    today = date(2026, 9, 8)
    store = _FakeRangeStore([
        _row("live-1", "2026-09-08", status="published"),
    ])
    result = CL.cancel_post("crossfitlocal", "U_client", "cancel today's post",
                            sb_store=store, today=today)
    assert result["ok"] is False
    assert store.patches == [], "a published row must never be written to"


def test_nothing_scheduled_returns_a_clean_explanation_not_an_error():
    store = _FakeRangeStore([])
    result = CL.cancel_post("crossfitlocal", "U_client", "cancel my post",
                            sb_store=store, today=date(2026, 9, 8))
    assert result["ok"] is False
    assert result["row"] is None
    assert "could not find" in result["body"].lower()
    assert store.patches == []


def test_cross_gym_row_never_reachable_even_if_id_guessed(db_tmp):
    """TOKEN ISOLATION: cancel_lane only ever asks the store for ROWS BELONGING TO
    account_key (rows_in_range is gym-scoped), and the underlying write
    (portal_social.handle_deny -> set_status) is gym-scoped a second time. A gym B row
    is invisible to gym A's cancel request end to end."""
    today = date(2026, 9, 8)
    store = _FakeRangeStore([
        _row("gym-b-row", "2026-09-09", status="pending", gym_id="other_gym"),
    ])
    result = CL.cancel_post("crossfitlocal", "U_client", "cancel my post",
                            sb_store=store, today=today)
    assert result["ok"] is False, "gym A must never see gym B's row as a target"
    assert store.patches == []
    assert store._rows["gym-b-row"]["status"] == "pending", "gym B's row is untouched"


def test_already_denied_row_is_idempotent_not_double_charged(db_tmp):
    today = date(2026, 9, 8)
    store = _FakeRangeStore([_row("d-1", "2026-09-08", status="pending")])
    r1 = CL.cancel_post("crossfitlocal", "U_client", "cancel today's post",
                        sb_store=store, today=today)
    assert r1["ok"] is True
    spent_after_first = ps.recreate_spent("crossfitlocal")
    # a second identical message, same day: the row is now 'denied' so
    # find_target_row (pending/approved only) no longer offers it at all --
    # there is nothing left to double-cancel, which is itself the safe outcome.
    r2 = CL.cancel_post("crossfitlocal", "U_client", "cancel today's post",
                        sb_store=store, today=today)
    assert r2["ok"] is False
    assert ps.recreate_spent("crossfitlocal") == spent_after_first, \
        "a repeat cancel with nothing left to cancel must never burn a second unit"


def test_monthly_budget_exhaustion_is_a_clean_decline_not_a_crash(db_tmp):
    today = date(2026, 9, 8)
    for _ in range(ps.MONTHLY_RECREATE_BUDGET):
        ps.spend_recreate("crossfitlocal")
    store = _FakeRangeStore([_row("d-1", "2026-09-08", status="pending")])
    result = CL.cancel_post("crossfitlocal", "U_client", "cancel today's post",
                            sb_store=store, today=today)
    assert result["ok"] is False
    assert store.patches == [], "a budget-exhausted decline must never write"


# ==========================================================================
# 3. adapter.handle_event: end to end through the real Slack-shaped surface
# ==========================================================================

class _FakeBus:
    """Minimal bus double covering exactly what CANCEL_POST's path in handle_event
    touches (get_or_create_ticket / record_inbound / record_outbound / set_ticket /
    find_ticket_by_thread / find_open_ticket_in_conversation / count/find for rate
    limiting), same shape as test_slack_convo.py's FakeBus."""

    def __init__(self):
        self.tickets = {}
        self.msgs = []

    def find_ticket_by_thread(self, channel_id, thread_ts):
        for t in self.tickets.values():
            if t["slack_channel_id"] == channel_id and t["slack_thread_ts"] == thread_ts:
                return dict(t)
        return None

    def find_open_ticket_in_conversation(self, channel_id, within_days):
        return None

    def get_or_create_ticket(self, **kw):
        ex = self.find_ticket_by_thread(kw["channel_id"], kw["thread_ts"])
        if ex:
            return ex, False
        t = {"id": str(uuid.uuid4()), "product": kw["product"],
            "client_id": kw.get("client_id"), "reporter": kw.get("reporter"),
            "raw_text": kw["raw_text"], "status": "new",
            "slack_channel_id": kw["channel_id"], "slack_thread_ts": kw["thread_ts"],
            "slack_user_id": kw["slack_user_id"], "identity_kind": kw["identity_kind"],
            "bot_identity": kw["bot_identity"], "classification": kw.get("classification"),
            "request_type": kw.get("request_type"), "escalated": False, "lane": None,
            "hold_tier": None, "verification_before": None, "verification_after": None}
        self.tickets[t["id"]] = t
        return dict(t), True

    def set_ticket(self, tid, **fields):
        self.tickets[tid].update(fields)
        return dict(self.tickets[tid])

    def count_tickets_for_user_today(self, slack_user_id, bot_identity=None):
        return 0

    def find_recent_ticket_for_user_today(self, slack_user_id, bot_identity=None):
        return None

    def record_inbound(self, **kw):
        if kw.get("slack_event_id") and any(
                m.get("slack_event_id") == kw["slack_event_id"] for m in self.msgs):
            return None, True
        m = {"id": str(uuid.uuid4()), "direction": "inbound", **kw}
        self.msgs.append(m)
        return dict(m), False

    def record_outbound(self, **kw):
        att = {"kind": kw["kind"]}
        att.update(kw.get("meta") or {})
        m = {"id": str(uuid.uuid4()), "direction": "outbound", "ticket_id": kw["ticket_id"],
            "body": kw["body"], "delivery_status": kw["delivery_status"], "attachments": att}
        self.msgs.append(m)
        return dict(m)

    def messages(self, tid, limit=40):
        return [dict(m) for m in self.msgs if m.get("ticket_id") == tid]

    def outbound_kinds(self, tid):
        return [m["attachments"]["kind"] for m in self.msgs
               if m.get("ticket_id") == tid and m["direction"] == "outbound"]


def _client_who(account_key="crossfitlocal", gym_id="g-1"):
    return IG.Identity(IG.CLIENT, "U_CLIENT", email="chad@x.com", display="Chad",
                      account_key=account_key, gym_id=gym_id, reason="test")


def _deps(bus, *, who=None, client_armed=True, cancel_post_enabled=True, cancel_post=None):
    ident = IDS.get("echo")
    who_obj = who if who is not None else _client_who()
    return A.Deps(bus=bus, identity=ident,
                 resolve_identity=lambda uid: who_obj,
                 identity_enabled=lambda: True,
                 client_reply_armed=lambda: client_armed,
                 staff_reply_armed=lambda: True,
                 daily_cap=lambda: 10, open_window_days=lambda: 7,
                 cancel_post_enabled=lambda: cancel_post_enabled,
                 cancel_post=cancel_post,
                 log=lambda *a, **k: None)


def _ev(text, ts="1.001", channel="G0MPIM", channel_type="mpim", user="U_CLIENT"):
    return {"type": "message", "channel": channel, "channel_type": channel_type,
           "user": user, "text": text, "ts": ts}


def test_adapter_cancels_and_confirms_the_clients_own_post():
    bus = _FakeBus()
    calls = []

    def fake_cancel(account_key, actor_id, text):
        calls.append((account_key, actor_id, text))
        return {"ok": True, "body": "Done. The post scheduled for 2026-09-09 is "
                                    "cancelled and will not go out.",
               "row": {"id": "row-1"}, "result": {"ok": True}}

    d = A.handle_event(_ev("cancel my post today"), "G0MPIM:1.001",
                      _deps(bus, cancel_post=fake_cancel))
    assert not d.ignored
    assert d.classification == C.CANCEL_POST
    assert calls == [("crossfitlocal", "U_CLIENT", "cancel my post today")], \
        "scoped to the CALLER's own account_key, and passes the actual actor id"
    kinds = bus.outbound_kinds(d.ticket_id)
    assert A.KIND_ACK in kinds
    assert A.KIND_STATUS in kinds
    status_row = [m for m in bus.messages(d.ticket_id)
                 if m["direction"] == "outbound"
                 and m["attachments"]["kind"] == A.KIND_STATUS][0]
    assert "2026-09-09" in status_row["body"]
    assert status_row["attachments"]["resolve_notice"] is True, \
        "the outbox must resolve this ticket once the confirmation actually posts"
    assert A.KIND_FIXER_REQUEST not in kinds, "no code-fix worker for a cancel request"
    assert A.KIND_ESCALATION not in kinds, "a clean cancel never pages a human"


def test_adapter_never_scopes_to_another_gym():
    """The account_key the cancel lane receives is ALWAYS who.account_key -- the
    identity gate's own resolution for the Slack user who sent the message -- never
    anything parsed out of the message text itself. There is no field in the event a
    client could set to point this at a different gym."""
    bus = _FakeBus()
    calls = []

    def fake_cancel(account_key, actor_id, text):
        calls.append(account_key)
        return {"ok": True, "body": "Done.", "row": {"id": "r1"}, "result": {}}

    who = _client_who(account_key="the_real_gym")
    A.handle_event(_ev("cancel my post today, gym_id=some_other_gym"), "G0MPIM:1.001",
                  _deps(bus, who=who, cancel_post=fake_cancel))
    assert calls == ["the_real_gym"]


def test_adapter_escalates_when_identity_has_no_account_key():
    """Staff/coach (or any identity with no account_key) can never reach the write
    path: there is nothing to scope it to, so this escalates to a human instead of
    guessing whose calendar to touch."""
    bus = _FakeBus()
    staff_who = IG.Identity(IG.STAFF, "U_STAFF", email="blake@x.com", reason="test")
    calls = []

    def fake_cancel(*a, **k):
        calls.append(a)
        return {"ok": True, "body": "should never run"}

    d = A.handle_event(_ev("cancel my post today", channel="D_STAFF", channel_type="im"),
                      "D_STAFF:1.001",
                      _deps(bus, who=staff_who, cancel_post=fake_cancel))
    assert calls == [], "the write path must never run with no account to scope it to"
    assert bus.tickets[d.ticket_id]["status"] == "hold"
    assert bus.tickets[d.ticket_id]["escalated"] is True
    assert A.KIND_ESCALATION in bus.outbound_kinds(d.ticket_id)


def test_adapter_flag_off_never_classifies_as_cancel_post():
    """AGENT_SLACK_CANCEL_POST_ENABLED off: byte identical to today. The message is
    classified as it always was (QUESTION, here, from 'can you'), the cancel lane is
    never called, and the client sees the pre-existing behavior only."""
    bus = _FakeBus()
    calls = []

    def fake_cancel(*a, **k):
        calls.append(a)
        return {"ok": True, "body": "should never run"}

    d = A.handle_event(_ev("can you cancel my post today"), "G0MPIM:1.001",
                      _deps(bus, cancel_post_enabled=False, cancel_post=fake_cancel))
    assert d.classification != C.CANCEL_POST
    assert calls == [], "the cancel lane must never run while the flag is off"


def test_adapter_lane_exception_escalates_never_claims_success():
    bus = _FakeBus()

    def boom(*a, **k):
        raise RuntimeError("store unreachable")

    d = A.handle_event(_ev("cancel my post today"), "G0MPIM:1.001",
                      _deps(bus, cancel_post=boom))
    assert bus.tickets[d.ticket_id]["status"] == "hold"
    assert bus.tickets[d.ticket_id]["escalated"] is True
    kinds = bus.outbound_kinds(d.ticket_id)
    assert A.KIND_ESCALATION in kinds
    # never a fabricated success message when the lane itself blew up
    posted_bodies = [m["body"] for m in bus.messages(d.ticket_id)
                    if m["direction"] == "outbound"
                    and m["attachments"]["kind"] in (A.KIND_STATUS, A.KIND_TEMPLATE)]
    assert not any("cancelled and will not go out" in b for b in posted_bodies)


def test_adapter_declined_cancel_still_confirms_no_escalation():
    """A clean, explained decline (nothing eligible to cancel, or the budget is used
    up) is a complete, honest answer -- not a system failure. It must not page a
    human; the client already has the reason in the reply."""
    bus = _FakeBus()

    def declined(*a, **k):
        return {"ok": False, "body": "I could not find a post scheduled for you to "
                                     "cancel right now.", "row": None, "result": None}

    d = A.handle_event(_ev("cancel my post today"), "G0MPIM:1.001",
                      _deps(bus, cancel_post=declined))
    assert d.reason == "cancel_post_declined"
    kinds = bus.outbound_kinds(d.ticket_id)
    assert A.KIND_STATUS in kinds
    assert A.KIND_ESCALATION not in kinds
