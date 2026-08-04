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
