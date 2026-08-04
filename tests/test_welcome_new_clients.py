"""Tests for agent/welcome_new_clients.py: the end-to-end auto welcome-post
pipeline (resolve -> logo -> generate -> ONE combined Slack card -> Approve
fans out to publish feed+story on BOTH lasso_ig and lasso_fb -> ledger), with
Stripe, Slack, and hosting all faked so nothing touches the network or a real
Meta account."""

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
    def list_customers(self, created_gte=None, limit=100, starting_after=None):
        return {"data": [], "has_more": False}


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
        self.chat_posts = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def _chat_post(self, text, blocks=None, channel=None, thread_ts=None):
        self.chat_posts.append({"text": text, "blocks": blocks})
        return {"ok": True, "ts": "123.456"}


class _FakeStore:
    """A real in-memory stand-in for PendingStore's get/put/remove contract."""

    def __init__(self):
        self.drafts = {}

    def put(self, draft):
        self.drafts[draft.draft_id] = draft

    def get(self, draft_id):
        return self.drafts.get(draft_id)

    def remove(self, draft_id):
        return self.drafts.pop(draft_id, None) is not None


def _write_tenant(base_dir, key, name, approver_name, consent=True):
    import json
    tdir = os.path.join(base_dir, key)
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "tenant.json"), "w", encoding="utf-8") as fh:
        json.dump({"key": key, "name": name, "approver_name": approver_name,
                  "welcome_post_consent": consent}, fh)


# ---------------------------------------------------------------------------
# build_roster (unchanged contract)
# ---------------------------------------------------------------------------

def test_build_roster_errors_without_stripe_key(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.delenv(config.STRIPE_API_KEY_ENV, raising=False)
    from agent import stripe_client as sc
    monkeypatch.setattr(sc, "default_client", lambda: None)
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    assert "error" in out


def test_build_roster_dedupes_two_contacts_same_gym(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    customers = [_FakeCustomer("cus_1", name="Acme Gym"),
                _FakeCustomer("cus_2", name="Acme Gym")]
    _patch_pipeline(monkeypatch, customers, {})
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    assert out["gyms_deduped"] == 1
    assert out["roster"][0]["collapsed_contacts"] == 2


def test_build_roster_excludes_delinquent(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    customers = [_FakeCustomer("cus_1", name="Acme Gym")]
    _patch_pipeline(monkeypatch, customers, {"cus_1": "past_due"})
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    assert out["roster"][0]["include"] is False
    assert "delinquent" in out["roster"][0]["exclude_reason"]


def test_build_roster_excludes_already_posted(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    customers = [_FakeCustomer("cus_1", name="Acme Gym")]
    _patch_pipeline(monkeypatch, customers, {})
    welcome_ledger.record_posted(welcome_ledger.gym_key("Acme Gym"), "Acme Gym",
                                 "", "", "CONFIRMED", "stripe_business_name", "T1")
    out = wnc.build_roster(0, base_dir=str(tmp_path))
    assert out["roster"][0]["include"] is False
    assert "already welcomed" in out["roster"][0]["exclude_reason"]


# ---------------------------------------------------------------------------
# generate_and_surface_gym: builds the bundle + posts ONE combined card
# ---------------------------------------------------------------------------

def _confirmed_entry(**overrides):
    entry = {
        "gym_key": "acct:acme_gym", "gym_name": "Acme Gym", "owner_name": "Jordan Blake",
        "confidence": gym_resolve.CONFIRMED, "source": "portal",
        "account_key": "acme_gym", "website": "", "note": "", "collapsed_contacts": 1,
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
    assert len(poster.notices) == 1
    assert poster.chat_posts == []


def test_hosting_disabled_blocks_generation(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    entry = _confirmed_entry()
    poster = _FakePoster()
    result = wnc.generate_and_surface_gym(entry, poster=poster, cache_dir=str(tmp_path))
    assert result["posted"] is False
    assert "hosting disabled" in result["reason"]


def test_confirmed_entry_generates_bundle_and_posts_one_card(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    entry = _confirmed_entry()
    poster = _FakePoster()
    store = _FakeStore()
    host_calls = []

    def fake_host(path):
        host_calls.append(path)
        return f"https://cdn.example.com/{os.path.basename(path)}"

    result = wnc.generate_and_surface_gym(
        entry, poster=poster, host_fn=fake_host, cache_dir=str(tmp_path), store=store)

    assert result["posted"] is True
    assert len(host_calls) == 2  # feed + story, hosted once each
    # ONE combined Slack message with both images, not two separate cards
    assert len(poster.chat_posts) == 1
    image_blocks = [b for b in poster.chat_posts[0]["blocks"] if b["type"] == "image"]
    assert len(image_blocks) == 2
    # the four real per-target drafts exist, targeting both accounts, both formats
    ledger = welcome_ledger.get_entry(entry["gym_key"])
    assert store.get(ledger["ig_feed_draft_id"]).account_key == "lasso_ig"
    assert store.get(ledger["fb_feed_draft_id"]).account_key == "lasso_fb"
    assert store.get(ledger["ig_story_draft_id"]).is_story is True
    assert store.get(ledger["fb_story_draft_id"]).is_story is True
    # the card itself is a fifth, display-only draft
    primary = store.get(ledger["primary_draft_id"])
    assert primary.draft_type == "welcome_multi"


def test_ledger_records_stripe_customer_id_and_draft_ids(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    entry = _confirmed_entry()
    wnc.generate_and_surface_gym(entry, poster=_FakePoster(),
                                 host_fn=lambda p: "https://x/y.png",
                                 cache_dir=str(tmp_path), store=_FakeStore())
    ledger = welcome_ledger.get_entry(entry["gym_key"])
    assert ledger["stripe_customer_id"] == "cus_1"
    assert ledger["status"] == "posted_for_review"
    assert all(ledger[f] for f in welcome_ledger.BUNDLE_FIELDS)


# ---------------------------------------------------------------------------
# handle_welcome_approval: the one-card-both-go-out fan-out + re-checked guards
# ---------------------------------------------------------------------------

def _build_bundle(monkeypatch, tmp_path, consent=True, subscription_status="active"):
    """Generate a real bundle (4 target drafts + 1 primary) into a _FakeStore
    and return (store, primary_draft, entry_dict)."""
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    _write_tenant(str(tmp_path), "acme_gym", "Acme Gym", "Jordan Blake", consent=consent)
    entry = _confirmed_entry()
    store = _FakeStore()
    wnc.generate_and_surface_gym(entry, poster=_FakePoster(),
                                 host_fn=lambda p: f"https://cdn.example.com/{os.path.basename(p)}",
                                 cache_dir=str(tmp_path), store=store, out_dir=str(tmp_path))
    ledger = welcome_ledger.get_entry(entry["gym_key"])
    primary = store.get(ledger["primary_draft_id"])

    from agent import stripe_client as sc
    monkeypatch.setattr(sc, "default_client", lambda: object())
    monkeypatch.setattr(sc, "subscription_status",
                        lambda cust_id, client=None: subscription_status)
    return store, primary, entry


class _RecordingHandleAction:
    """Patches approvals.handle_action to record calls and return a canned
    ActionResult per target, instead of really calling meta_publisher."""

    def __init__(self):
        self.calls = []

    def __call__(self, action, draft, actor_slack_id=None, account=None, **kw):
        from agent.approvals import ActionResult
        self.calls.append((action, draft.draft_id, getattr(account, "key", None)))
        return ActionResult(ok=True, action=action, draft_id=draft.draft_id,
                            detail="would_publish (draft-only)")


def test_approve_denies_non_approver(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    store, primary, entry = _build_bundle(monkeypatch, tmp_path)
    res = wnc.handle_welcome_approval("approve", primary, "U_RANDOM", store=store)
    assert res.ok is False
    assert "not the approver" in res.detail


def test_approve_blocks_without_consent(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    store, primary, entry = _build_bundle(monkeypatch, tmp_path, consent=False)
    res = wnc.handle_welcome_approval("approve", primary, config.APPROVER_SLACK_ID, store=store, base_dir=str(tmp_path))
    assert res.ok is False
    assert "consent" in res.detail.lower()
    # nothing removed from the store; Blake can fix consent and retry
    ledger = welcome_ledger.get_entry(entry["gym_key"])
    assert store.get(ledger["ig_feed_draft_id"]) is not None


def test_approve_blocks_when_no_portal_record_at_all(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    entry_override = {"account_key": ""}  # Stripe/domain-only resolution
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    store = _FakeStore()
    entry = _confirmed_entry(**entry_override)
    wnc.generate_and_surface_gym(entry, poster=_FakePoster(),
                                 host_fn=lambda p: "https://x/y.png",
                                 cache_dir=str(tmp_path), store=store)
    ledger = welcome_ledger.get_entry(entry["gym_key"])
    primary = store.get(ledger["primary_draft_id"])
    res = wnc.handle_welcome_approval("approve", primary, config.APPROVER_SLACK_ID, store=store, base_dir=str(tmp_path))
    assert res.ok is False
    assert "no portal record" in res.detail.lower()


def test_approve_blocks_on_delinquent_subscription_rechecked(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    store, primary, entry = _build_bundle(monkeypatch, tmp_path,
                                          subscription_status="past_due")
    res = wnc.handle_welcome_approval("approve", primary, config.APPROVER_SLACK_ID, store=store, base_dir=str(tmp_path))
    assert res.ok is False
    assert "delinquent" in res.detail.lower()


def test_approve_blocks_when_already_published(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    store, primary, entry = _build_bundle(monkeypatch, tmp_path)
    welcome_ledger.mark_status(entry["gym_key"], "published")
    res = wnc.handle_welcome_approval("approve", primary, config.APPROVER_SLACK_ID, store=store, base_dir=str(tmp_path))
    assert res.ok is False
    assert "already published" in res.detail.lower()


def test_approve_fans_out_to_all_four_targets(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    store, primary, entry = _build_bundle(monkeypatch, tmp_path)
    recorder = _RecordingHandleAction()
    monkeypatch.setattr("agent.approvals.handle_action", recorder)
    res = wnc.handle_welcome_approval("approve", primary, config.APPROVER_SLACK_ID, store=store, base_dir=str(tmp_path))
    assert res.ok is True
    account_keys = {c[2] for c in recorder.calls}
    assert account_keys == {"lasso_ig", "lasso_fb"}
    assert len(recorder.calls) == 4
    ledger = welcome_ledger.get_entry(entry["gym_key"])
    assert ledger["status"] == "published"
    # all four target drafts (and the primary) removed from the store
    for field in welcome_ledger.BUNDLE_FIELDS:
        assert store.get(ledger[field]) is None


def test_approve_partial_failure_marks_ledger_partial_and_keeps_failed_draft(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    store, primary, entry = _build_bundle(monkeypatch, tmp_path)

    from agent.approvals import ActionResult

    def flaky_handle_action(action, draft, actor_slack_id=None, account=None, **kw):
        if getattr(account, "key", None) == "lasso_fb" and not draft.is_story:
            return ActionResult(ok=False, action=action, draft_id=draft.draft_id,
                                detail="Held: media not ready.")
        return ActionResult(ok=True, action=action, draft_id=draft.draft_id,
                            detail="would_publish (draft-only)")

    monkeypatch.setattr("agent.approvals.handle_action", flaky_handle_action)
    res = wnc.handle_welcome_approval("approve", primary, config.APPROVER_SLACK_ID, store=store, base_dir=str(tmp_path))
    assert res.ok is False
    ledger = welcome_ledger.get_entry(entry["gym_key"])
    assert ledger["status"] == "partial"
    # the failed fb feed draft stays in the store for a retry; others cleared
    assert store.get(ledger["fb_feed_draft_id"]) is not None
    assert store.get(ledger["ig_feed_draft_id"]) is None


def test_skip_removes_bundle_and_marks_ledger(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    store, primary, entry = _build_bundle(monkeypatch, tmp_path)
    res = wnc.handle_welcome_approval("skip", primary, config.APPROVER_SLACK_ID, store=store, base_dir=str(tmp_path))
    assert res.ok is True
    ledger = welcome_ledger.get_entry(entry["gym_key"])
    assert ledger["status"] == "skipped"
    for field in welcome_ledger.BUNDLE_FIELDS:
        assert store.get(ledger[field]) is None


def test_approve_with_no_ledger_entry_is_refused(monkeypatch, tmp_path):
    _arm_db(monkeypatch, tmp_path)
    from agent.drafter import Draft, DraftStatus
    orphan = Draft(draft_id="wel_orphan", account_key="lasso_ig", platform="instagram",
                   caption="", hashtags=[], creative_path="", creative_public_url="",
                   scheduled_for="2026-08-04", status=DraftStatus.PENDING,
                   draft_type="welcome_multi")
    res = wnc.handle_welcome_approval("approve", orphan, config.APPROVER_SLACK_ID,
                                      store=_FakeStore())
    assert res.ok is False
    assert "no welcome bundle" in res.detail.lower()


# ---------------------------------------------------------------------------
# run_pipeline / run_backfill: flag gating (unchanged)
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
