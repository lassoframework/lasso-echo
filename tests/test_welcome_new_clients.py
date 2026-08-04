"""Tests for agent/welcome_new_clients.py: the end-to-end auto welcome-post
pipeline (resolve -> logo -> generate -> surface -> ledger), with Stripe,
Slack, and hosting all faked so nothing touches the network."""

import os

import pytest

from agent import config, gym_resolve, welcome_ledger, welcome_new_clients as wnc


def _arm_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))


class _FakeCustomer:
    def __init__(self, id, email="", name="", created=1000, metadata=None):
        self.id = id
        self.email = email
        self.name = name
        self.created = created
        self.metadata = metadata or {}


class _FakeStripeClient:
    """Stands in for stripe_client's real client: list_new_customers/
    subscription_status are called with this object via client=..."""

    def __init__(self, customers, statuses=None):
        self._customers = customers
        self._statuses = statuses or {}

    # matches the interface list_new_customers expects on its `client` param
    def list_customers(self, created_gte=None, limit=100, starting_after=None):
        return {"data": [], "has_more": False}


@pytest.fixture(autouse=True)
def _patch_stripe(monkeypatch):
    """Route stripe_client.list_new_customers / subscription_status to fixture
    data instead of any real HTTP, per test via monkeypatching the module
    functions directly (simpler than faking urllib for every test)."""
    yield


def _patch_pipeline(monkeypatch, customers, statuses):
    from agent import stripe_client as sc
    monkeypatch.setattr(sc, "list_new_customers",
                        lambda since_ts, client=None, max_pages=20: customers)
    monkeypatch.setattr(sc, "subscription_status",
                        lambda cust_id, client=None: statuses.get(cust_id, "active"))
    monkeypatch.setattr(sc, "default_client", lambda: object())


class _FakePoster:
    def __init__(self):
        self.notices = []
        self.cards = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"ok": True}


class _FakeStore:
    def __init__(self):
        self.drafts = []

    def put(self, draft):
        self.drafts.append(draft)


# ---------------------------------------------------------------------------
# build_roster
# ---------------------------------------------------------------------------

def test_build_roster_errors_without_stripe_key(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.delenv(config.STRIPE_API_KEY_ENV, raising=False)
    from agent import stripe_client as sc
    monkeypatch.setattr(sc, "default_client", lambda: None)
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    assert "error" in out
    assert config.STRIPE_API_KEY_ENV in out["error"]


def test_build_roster_dedupes_two_contacts_same_gym(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    customers = [
        _FakeCustomer("cus_1", name="Acme Gym"),
        _FakeCustomer("cus_2", name="Acme Gym"),  # same business name, second contact
    ]
    _patch_pipeline(monkeypatch, customers, {})
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    assert out["customers_seen"] == 2
    assert out["gyms_deduped"] == 1
    assert out["roster"][0]["collapsed_contacts"] == 2
    assert out["roster"][0]["include"] is True


def test_build_roster_excludes_delinquent(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    customers = [_FakeCustomer("cus_1", name="Acme Gym")]
    _patch_pipeline(monkeypatch, customers, {"cus_1": "past_due"})
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    entry = out["roster"][0]
    assert entry["include"] is False
    assert "delinquent" in entry["exclude_reason"]


def test_build_roster_excludes_non_active_status(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    customers = [_FakeCustomer("cus_1", name="Acme Gym")]
    _patch_pipeline(monkeypatch, customers, {"cus_1": "incomplete"})
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    entry = out["roster"][0]
    assert entry["include"] is False
    assert "not an active paying subscription" in entry["exclude_reason"]


def test_build_roster_excludes_already_posted(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    customers = [_FakeCustomer("cus_1", name="Acme Gym")]
    _patch_pipeline(monkeypatch, customers, {})
    key = welcome_ledger.gym_key("Acme Gym")
    welcome_ledger.record_posted(key, "Acme Gym", "", "", "CONFIRMED",
                                 "stripe_business_name", "T1")
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    entry = out["roster"][0]
    assert entry["include"] is False
    assert "already welcomed" in entry["exclude_reason"]


def test_build_roster_excludes_unresolved(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    customers = [_FakeCustomer("cus_1", email="", name="")]
    _patch_pipeline(monkeypatch, customers, {})
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    entry = out["roster"][0]
    assert entry["include"] is False
    assert "could not resolve" in entry["exclude_reason"]


# ---------------------------------------------------------------------------
# generate_and_surface_gym
# ---------------------------------------------------------------------------

def _confirmed_entry(**overrides):
    entry = {
        "gym_key": "name:acme-gym", "gym_name": "Acme Gym", "owner_name": "Jordan Blake",
        "confidence": gym_resolve.CONFIRMED, "source": "stripe_business_name",
        "account_key": "", "website": "", "note": "", "collapsed_contacts": 1,
        "stripe_customer_id": "cus_1", "include": True, "exclude_reason": "",
    }
    entry.update(overrides)
    return entry


def test_inferred_entry_only_posts_confirmation_request(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    entry = _confirmed_entry(confidence=gym_resolve.INFERRED, source="email_domain")
    poster = _FakePoster()
    result = wnc.generate_and_surface_gym(entry, poster=poster)
    assert result["posted"] is False
    assert "confirmation" in result["reason"]
    assert len(poster.notices) == 1
    assert "Acme Gym" in poster.notices[0]
    assert poster.cards == []


def test_hosting_disabled_blocks_generation(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    entry = _confirmed_entry()
    poster = _FakePoster()
    result = wnc.generate_and_surface_gym(entry, poster=poster, cache_dir=str(tmp_path))
    assert result["posted"] is False
    assert "hosting disabled" in result["reason"]
    assert len(poster.notices) == 1


def test_confirmed_entry_generates_and_posts_feed_and_story(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    entry = _confirmed_entry()  # website="" -> no logo scrape attempt, ghost plate
    poster = _FakePoster()
    store = _FakeStore()
    host_calls = []

    def fake_host(path):
        host_calls.append(path)
        return f"https://cdn.example.com/{os.path.basename(path)}"

    result = wnc.generate_and_surface_gym(
        entry, poster=poster, host_fn=fake_host, cache_dir=str(tmp_path), store=store)

    assert result["posted"] is True
    assert len(host_calls) == 2  # feed + story
    assert len(store.drafts) == 2
    assert len(poster.cards) == 2
    assert store.drafts[1].is_story is True
    assert store.drafts[0].is_story is False
    assert welcome_ledger.already_posted(entry["gym_key"]) is True


def test_confirmed_entry_records_ledger_with_confidence_and_source(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    entry = _confirmed_entry()
    poster = _FakePoster()
    wnc.generate_and_surface_gym(entry, poster=poster,
                                 host_fn=lambda p: "https://x/y.png",
                                 cache_dir=str(tmp_path), store=_FakeStore())
    entries = welcome_ledger.all_entries()
    assert len(entries) == 1
    assert entries[0]["confidence"] == "CONFIRMED"
    assert entries[0]["source"] == "stripe_business_name"


# ---------------------------------------------------------------------------
# run_pipeline / run_backfill: flag gating
# ---------------------------------------------------------------------------

def test_run_pipeline_off_by_default(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_WELCOME_POSTS_ENABLED", raising=False)
    out = wnc.run_pipeline(0)
    assert "error" in out
    assert "OFF" in out["error"]


def test_run_backfill_computes_since_ts_from_days(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_WELCOME_POSTS_ENABLED", "true")
    from agent import stripe_client as sc
    seen = {}

    def fake_list(since_ts, client=None, max_pages=20):
        seen["since_ts"] = since_ts
        return []

    monkeypatch.setattr(sc, "list_new_customers", fake_list)
    monkeypatch.setattr(sc, "default_client", lambda: object())
    wnc.run_backfill(days=45, now_ts=1_700_000_000, base_dir=str(tmp_path))
    assert seen["since_ts"] == 1_700_000_000 - 45 * 86400
