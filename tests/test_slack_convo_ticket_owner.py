"""Shared Slack subscriptions must not let another bot consume an owner's reply."""
import pytest
from test_slack_convo import A, C, FakeBus, _deps, _ev


@pytest.mark.parametrize("status", ["new", "fixing", "resolved"])
@pytest.mark.parametrize("surface", ["channel", "app_mention", "im", "mpim"])
def test_wrong_bot_leaves_event_for_original_owner(monkeypatch, status, surface):
    bus = FakeBus()
    first = A.handle_event(_ev("my facebook posts are broken"), "first", _deps(bus))
    bus.set_ticket(first.ticket_id, status=status)
    event = _ev("the facebook post is still broken", ts="1.002",
                channel_type="channel" if surface == "app_mention" else surface,
                etype="app_mention" if surface == "app_mention" else "message",
                thread_ts="1.001")
    before = list(bus.msgs)
    original = C.classify
    def forbidden(*args, **kwargs):
        raise AssertionError("wrong bot reached classification")
    monkeypatch.setattr(C, "classify", forbidden)
    wrong = A.handle_event(event, "shared-event", _deps(bus, identity="ranger"))
    assert wrong.reason == "other_ticket_identity"
    assert bus.msgs == before
    monkeypatch.setattr(C, "classify", original)
    owner = A.handle_event(event, "shared-event", _deps(bus))
    assert owner.ticket_id == first.ticket_id
    assert not owner.duplicate
    assert any(m.get("slack_event_id") == "shared-event" for m in bus.msgs)
    assert bus.ticket(first.ticket_id)["bot_identity"] == "echo"


@pytest.mark.parametrize("surface", ["im", "mpim"])
def test_unthreaded_dm_lookup_rejects_wrong_owner(surface):
    bus = FakeBus()
    A.handle_event(_ev("my facebook posts are broken"), "first", _deps(bus))
    before = list(bus.msgs)
    result = A.handle_event(_ev("please fix my post", ts="1.002", channel_type=surface),
                            "second", _deps(bus, identity="ranger"))
    assert result.reason == "other_ticket_identity"
    assert bus.msgs == before


def test_unique_thread_race_checks_winner_before_inbound():
    bus = FakeBus()
    original = bus.get_or_create_ticket
    def other_wins(**kwargs):
        kwargs["bot_identity"] = "ranger"
        return original(**kwargs)
    bus.get_or_create_ticket = other_wins
    result = A.handle_event(_ev("my facebook posts are broken"), "race", _deps(bus))
    assert result.reason == "other_ticket_identity"
    assert bus.msgs == []


def test_legacy_missing_owner_keeps_existing_fallback_without_reassignment():
    bus = FakeBus()
    first = A.handle_event(_ev("my facebook posts are broken"), "first", _deps(bus))
    bus.set_ticket(first.ticket_id, bot_identity=None)
    result = A.handle_event(_ev("please fix my post", ts="1.002"), "second", _deps(bus))
    assert result.ticket_id == first.ticket_id
    assert result.reason != "other_ticket_identity"
    assert bus.ticket(first.ticket_id)["bot_identity"] is None
