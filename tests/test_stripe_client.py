"""Tests for agent/stripe_client.py: read-only customer + subscription lookup.
The real HTTP client is never exercised here; a fake client stands in so tests
never touch the network or need a key."""

import pytest

from agent import config, stripe_client as sc


class _FakeClient:
    def __init__(self, pages=None, subs_by_customer=None, raise_on_list=False):
        self._pages = pages or []
        self._subs = subs_by_customer or {}
        self._raise_on_list = raise_on_list

    def list_customers(self, created_gte=None, limit=100, starting_after=None):
        if self._raise_on_list:
            raise ValueError("boom")
        idx = 0 if starting_after is None else \
            next(i for i, p in enumerate(self._pages) if p.get("_after") == starting_after)
        return self._pages[idx]

    def list_subscriptions(self, customer_id, limit=10):
        return {"data": self._subs.get(customer_id, [])}


def test_default_client_none_without_key(monkeypatch):
    monkeypatch.delenv(config.STRIPE_API_KEY_ENV, raising=False)
    assert sc.default_client() is None


def test_default_client_built_when_key_present(monkeypatch):
    monkeypatch.setenv(config.STRIPE_API_KEY_ENV, "sk_test_123")
    client = sc.default_client()
    assert isinstance(client, sc._UrllibStripeClient)


def test_list_new_customers_none_without_client():
    assert sc.list_new_customers(0, client=None) is None


def test_list_new_customers_single_page():
    client = _FakeClient(pages=[{"data": [
        {"id": "cus_1", "email": "a@acme.com", "name": "Acme Gym", "created": 100},
        {"id": "cus_2", "email": "b@acme.com", "name": "", "created": 101},
    ], "has_more": False}])
    out = sc.list_new_customers(0, client=client)
    assert [c.id for c in out] == ["cus_1", "cus_2"]
    assert out[0].name == "Acme Gym"
    assert out[1].name == ""


def test_list_new_customers_paginates():
    client = _FakeClient(pages=[
        {"data": [{"id": "cus_1", "created": 1}], "has_more": True, "_after": None},
        {"data": [{"id": "cus_2", "created": 2}], "has_more": False, "_after": "cus_1"},
    ])
    out = sc.list_new_customers(0, client=client)
    assert [c.id for c in out] == ["cus_1", "cus_2"]


def test_list_new_customers_returns_none_on_error():
    client = _FakeClient(raise_on_list=True)
    assert sc.list_new_customers(0, client=client) is None


def test_subscription_status_empty_without_client():
    assert sc.subscription_status("cus_1", client=None) == ""


def test_subscription_status_picks_most_recent():
    client = _FakeClient(subs_by_customer={"cus_1": [
        {"status": "canceled", "created": 10},
        {"status": "active", "created": 20},
    ]})
    assert sc.subscription_status("cus_1", client=client) == "active"


def test_subscription_status_no_subscription_found():
    client = _FakeClient(subs_by_customer={})
    assert sc.subscription_status("cus_1", client=client) == ""


@pytest.mark.parametrize("status,expected", [
    ("past_due", True), ("unpaid", True), ("incomplete_expired", True),
    ("active", False), ("trialing", False), ("canceled", False), ("", False),
])
def test_is_delinquent(status, expected):
    assert sc.is_delinquent(status) is expected


@pytest.mark.parametrize("status,expected", [
    ("active", True), ("trialing", True),
    ("past_due", False), ("canceled", False), ("", False),
])
def test_is_active_paying(status, expected):
    assert sc.is_active_paying(status) is expected
