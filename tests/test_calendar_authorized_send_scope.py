"""Offline wiring checks for the actual claimed calendar provider invocation."""
from contextlib import contextmanager

import pytest

from agent import calendar_autopublish as cap
from agent import forward_media_publish as bridge
from agent import zernio_publisher
from agent.forward_media_send_context import ProviderSendHold
from agent.meta_publisher import PublishResult
from tests.test_calendar_autopublish import _FakeStore, _row, RUN_DATE, LATE_NOW


class Store(_FakeStore):
    def mark_published(self, row_id, media_id, published_at, **kwargs):
        assert kwargs["expected_claim_token"] == "owned-token"
        return super().mark_published(row_id, media_id, published_at)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setattr(cap, "_alert_publish_blocked", lambda *args, **kwargs: None)
    monkeypatch.setattr(cap, "_note_repeat_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(cap, "_alert_ambiguous_publish", lambda *args, **kwargs: None)
    monkeypatch.setattr(cap, "_lasso_zernio_missing", lambda: [])
    monkeypatch.setattr(bridge, "authorize", lambda *args: True)


def _trusted_test_publishers(monkeypatch, publisher):
    # Guard ON permits only the configured lower-boundary send functions.
    monkeypatch.setattr(cap.meta_publisher, "publish", publisher)
    monkeypatch.setattr(zernio_publisher, "publish", publisher)


@pytest.mark.parametrize("zernio", [False, True])
def test_each_provider_call_runs_inside_exact_claim_scope(setup, monkeypatch, zernio):
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "true")
    monkeypatch.setenv("AGENT_LASSO_VIA_ZERNIO", str(zernio).lower())
    row = _row("scoped")
    store = Store([row], claim_returns={"scoped": "owned-token"})
    events = []
    active = []
    @contextmanager
    def scope(given_store, given_row, token):
        assert given_store is store
        assert given_row["id"] == "scoped"
        assert given_row["image_url"] == row["image_url"]
        assert token == "owned-token"
        events.append("enter")
        active.append(True)
        try:
            yield
        finally:
            active.pop()
            events.append("exit")
    monkeypatch.setattr(bridge, "authorized_send", scope)
    def publisher(draft, account, **kwargs):
        assert active == [True]
        if zernio:
            assert kwargs == {"scheduled_for": None}
        else:
            assert kwargs == {}
        events.append("provider")
        return PublishResult(ok=True, mode="published", media_id="media")
    _trusted_test_publishers(monkeypatch, publisher)
    out = cap.publish_due(RUN_DATE, store=store, publisher=publisher,
                          zernio_publish=publisher, now=LATE_NOW)
    assert events == ["enter", "provider", "exit"]
    assert out["published"] == ["scoped"]
    assert active == []


def test_scope_refusal_never_calls_provider_and_releases_owned_token(setup, monkeypatch):
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "true")
    store = Store([_row("held")], claim_returns={"held": "owned-token"})
    released = []
    monkeypatch.setattr(cap, "_revert_to_pending", lambda *a, **kw: released.append(kw) or True)
    @contextmanager
    def refused(*args):
        raise ProviderSendHold("committed receipt unavailable")
        yield
    monkeypatch.setattr(bridge, "authorized_send", refused)
    publisher = lambda *a: pytest.fail("provider was called")
    _trusted_test_publishers(monkeypatch, publisher)
    out = cap.publish_due(RUN_DATE, store=store, now=LATE_NOW,
                          publisher=publisher)
    assert out["published"] == []
    assert out["forward_media_holds"] == {"held": "forward_media_verification"}
    assert released[0]["expected_claim_token"] == "owned-token"
    assert out["recovery_required"] == []


def test_provider_failure_closes_scope_and_retains_ambiguous_claim(setup, monkeypatch):
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "true")
    store = Store([_row("ambiguous")], claim_returns={"ambiguous": "owned-token"})
    events = []
    @contextmanager
    def scope(*args):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")
    monkeypatch.setattr(bridge, "authorized_send", scope)
    monkeypatch.setattr(cap, "_revert_to_pending", lambda *a, **kw: pytest.fail("ambiguous claim released"))
    def publisher(*args):
        raise TimeoutError("response unavailable")
    _trusted_test_publishers(monkeypatch, publisher)
    out = cap.publish_due(RUN_DATE, store=store, publisher=publisher, now=LATE_NOW)
    assert events == ["enter", "exit"]
    assert out["recovery_required"] == ["ambiguous"]


def test_flag_off_keeps_plain_provider_call(setup, monkeypatch):
    monkeypatch.setattr(bridge, "authorized_send", lambda *a: pytest.fail("OFF entered authority scope"))
    store = _FakeStore([_row("legacy")])
    calls = []
    def publisher(draft, account):
        calls.append(draft.draft_id)
        return PublishResult(ok=True, mode="published", media_id="media")
    out = cap.publish_due(RUN_DATE, store=store, publisher=publisher, now=LATE_NOW)
    assert calls == ["legacy"]
    assert out["published"] == ["legacy"]


def test_real_scope_needs_committed_receipts_after_precheck(setup, monkeypatch):
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "true")
    row_id = "11111111-1111-4111-8111-111111111111"
    token = "22222222-2222-4222-8222-222222222222"
    store = _FakeStore([_row(row_id)], claim_returns={row_id: token})
    released = []
    monkeypatch.setattr(cap, "_revert_to_pending", lambda *a, **kw: released.append(kw) or True)
    # setup permits the early bridge precheck, but this fake store cannot read
    # the committed claim and exact-byte receipts demanded by the real scope.
    publisher = lambda *a: pytest.fail("unproven scope sent")
    _trusted_test_publishers(monkeypatch, publisher)
    out = cap.publish_due(RUN_DATE, store=store, now=LATE_NOW,
                          publisher=publisher)
    assert out["published"] == []
    assert out["forward_media_holds"] == {row_id: "forward_media_verification"}
    assert released[0]["expected_claim_token"] == token
    assert out["recovery_required"] == []


def test_guard_on_rejects_unverified_callbacks_before_read_or_claim(setup, monkeypatch):
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "true")
    class NoReadStore:
        def due_rows(self, *args, **kwargs):
            pytest.fail("unverified callback read due rows")
    fake = lambda *args, **kwargs: pytest.fail("unverified callback sent")
    for kwargs in ({"publisher": fake}, {"zernio_publish": fake}):
        out = cap.publish_due(RUN_DATE, store=NoReadStore(), now=LATE_NOW, **kwargs)
        assert out["held"] is True
        assert out["reason"] == "unverified provider callback"
