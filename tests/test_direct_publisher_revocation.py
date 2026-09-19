"""Direct publisher entry points hold revoked clients before any vendor work."""

from types import SimpleNamespace

import pytest

from agent import config, gbp_publisher, meta_publisher, publish_billing_gate as gate
from agent import socialapi_publisher
from agent import intake_web


@pytest.mark.parametrize(
    "publisher,error,key",
    [
        (meta_publisher, meta_publisher.PublishError, "gymx_ig"),
        (socialapi_publisher, socialapi_publisher.SocialApiPublishError, "gymx_fb"),
        (gbp_publisher, gbp_publisher.GbpError, "gymx_gbp"),
    ],
)
def test_direct_publish_holds_revoked_client_before_network(monkeypatch, publisher, error, key):
    monkeypatch.setattr(config, "publish_enabled", lambda: True)
    monkeypatch.setattr(config, "stories_enabled", lambda: True)
    monkeypatch.setattr(config, "gbp_enabled", lambda: True)
    bases = []
    monkeypatch.setattr(gate, "publishing_blocked", lambda base: bases.append(base) or True)

    class NoNetwork:
        def post(self, *args, **kwargs):
            raise AssertionError("vendor POST after revocation")

        def get(self, *args, **kwargs):
            raise AssertionError("vendor GET after revocation")

    account = SimpleNamespace(key=key)
    draft = SimpleNamespace(draft_id="revoked-1", is_story=False)
    with pytest.raises(error, match="publishing held"):
        publisher.publish(draft, account, http=NoNetwork())
    assert bases == ["gymx"]


def test_revocation_store_outage_holds_unknown_client_but_not_lasso(monkeypatch):
    monkeypatch.setattr(intake_web, "_default_r2", lambda: None)
    assert gate.account_revoked("gymx") is True
    assert gate.publishing_blocked("gymx") is True
    assert gate.account_revoked("lasso") is False
    assert gate.account_revoked("lasso-client") is True


def test_fresh_denylist_can_clear_outage_hold(monkeypatch):
    monkeypatch.setattr(intake_web, "_default_r2", lambda: None)
    assert gate.account_revoked("gymx") is True
    monkeypatch.setattr(
        intake_web, "_default_r2",
        lambda: SimpleNamespace(get_bytes=lambda key: b'{"revoked": []}'),
    )
    assert gate.account_revoked("gymx") is False


@pytest.mark.parametrize(
    "publisher,error,key",
    [
        (meta_publisher, meta_publisher.PublishError, "gymx_ig"),
        (socialapi_publisher, socialapi_publisher.SocialApiPublishError, "gymx_fb"),
        (gbp_publisher, gbp_publisher.GbpError, "gymx_gbp"),
    ],
)
def test_direct_publish_holds_on_unreadable_revocation_store(
    monkeypatch, publisher, error, key
):
    monkeypatch.setattr(config, "publish_enabled", lambda: True)
    monkeypatch.setattr(config, "gbp_enabled", lambda: True)
    monkeypatch.setattr(intake_web, "_default_r2", lambda: None)

    class NoNetwork:
        def post(self, *args, **kwargs):
            raise AssertionError("vendor POST during revocation-store outage")

        def get(self, *args, **kwargs):
            raise AssertionError("vendor GET during revocation-store outage")

    with pytest.raises(error, match="publishing held"):
        publisher.publish(
            SimpleNamespace(draft_id="outage-1", is_story=False),
            SimpleNamespace(key=key),
            http=NoNetwork(),
        )
