"""Tests for agent/welcome_ledger.py: dedupe-by-gym + never-welcome-twice
(Part D). Each test gets its own isolated echo.db via AGENT_DB_PATH."""

from agent import welcome_ledger as wl


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))


def test_gym_key_prefers_account_key():
    assert wl.gym_key("Acme Gym", account_key="acme_gym") == "acct:acme_gym"


def test_gym_key_slugifies_name_when_no_account():
    assert wl.gym_key("Iron Forge Fitness!!") == "name:iron-forge-fitness"


def test_gym_key_same_gym_same_key_regardless_of_contact():
    # two different contacts, same gym (no portal account_key yet)
    k1 = wl.gym_key("Acme Gym")
    k2 = wl.gym_key("ACME GYM")
    assert k1 == k2


def test_gym_key_empty_for_blank_name_and_no_account():
    assert wl.gym_key("") == ""


def test_already_posted_false_initially(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    assert wl.already_posted("name:acme-gym") is False


def test_record_then_already_posted_true(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    key = wl.gym_key("Acme Gym")
    wl.record_posted(key, "Acme Gym", "Jordan Blake", "", "CONFIRMED",
                     "stripe_business_name", "T1")
    assert wl.already_posted(key) is True


def test_record_is_idempotent_insert_or_replace(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    key = wl.gym_key("Acme Gym")
    wl.record_posted(key, "Acme Gym", "Jordan Blake", "", "CONFIRMED", "portal", "T1")
    wl.record_posted(key, "Acme Gym", "Jordan Blake Jr", "", "CONFIRMED", "portal", "T2")
    entries = wl.all_entries()
    assert len(entries) == 1
    assert entries[0]["template_id"] == "T2"


def test_mark_status_updates_row(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    key = wl.gym_key("Acme Gym")
    wl.record_posted(key, "Acme Gym", "", "", "CONFIRMED", "portal", "T1")
    wl.mark_status(key, "approved")
    entries = wl.all_entries()
    assert entries[0]["status"] == "approved"


def test_all_entries_empty_on_fresh_db(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    assert wl.all_entries() == []


def test_record_stores_bundle_draft_ids(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    key = wl.gym_key("Acme Gym")
    wl.record_posted(key, "Acme Gym", "", "", "CONFIRMED", "portal", "T1",
                     primary_draft_id="wel_1", ig_feed_draft_id="wel_1_ig_feed",
                     fb_feed_draft_id="wel_1_fb_feed", ig_story_draft_id="wel_1_ig_story",
                     fb_story_draft_id="wel_1_fb_story", stripe_customer_id="cus_1")
    entry = wl.get_entry(key)
    assert entry["stripe_customer_id"] == "cus_1"
    assert entry["primary_draft_id"] == "wel_1"
    assert all(entry[f] for f in wl.BUNDLE_FIELDS)


def test_find_by_primary_draft_id(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    key = wl.gym_key("Acme Gym")
    wl.record_posted(key, "Acme Gym", "", "", "CONFIRMED", "portal", "T1",
                     primary_draft_id="wel_1")
    found = wl.find_by_primary_draft_id("wel_1")
    assert found["gym_key"] == key
    assert wl.find_by_primary_draft_id("nope") is None
    assert wl.find_by_primary_draft_id("") is None


def test_set_primary_draft_id_repoints_lookup(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    key = wl.gym_key("Acme Gym")
    wl.record_posted(key, "Acme Gym", "", "", "CONFIRMED", "portal", "T1",
                     primary_draft_id="wel_1")
    wl.set_primary_draft_id(key, "wel_1e")
    assert wl.find_by_primary_draft_id("wel_1") is None
    assert wl.find_by_primary_draft_id("wel_1e")["gym_key"] == key


def test_get_entry_none_when_missing(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    assert wl.get_entry("name:nope") is None
