"""A missing Echo registry row cannot strand approved client calendar posts."""

import json

from agent import accounts, echo_clients, registry_reconcile

ZANSHIN = "630e22ab-0000-4000-8000-000000000001"
ADS_ONLY = "99999999-0000-4000-8000-000000000002"
OTHER_CLIENT = "22222222-0000-4000-8000-000000000003"
BASE = "zanshinfitness630e22"


def _calendar(base=BASE, **changes):
    row = {"gym_id": base, "account": "instagram", "post_date": "2026-10-05",
           "status": "approved", "variant_status": "active", "published_at": None}
    row.update(changes)
    return row


def _clients(*, token=BASE, extra_settings=(), extra_tokens=()):
    settings = [{"gym_id": ZANSHIN}] + list(extra_settings)
    tokens = [{"gym_id": ZANSHIN, "echo_account_key": token}] + list(extra_tokens)
    gyms = [{"id": ZANSHIN, "name": "Zanshin Fitness", "slug": "zanshin-fitness"},
            {"id": ADS_ONLY, "name": "Ads Only", "slug": "ads-only"},
            {"id": OTHER_CLIENT, "name": "Other Client", "slug": "other-client"}]
    return echo_clients.build(settings, tokens, gyms)


def _registry(monkeypatch, tmp_path, clients):
    path = tmp_path / "gym_accounts.json"
    monkeypatch.setenv("AGENT_DYNAMIC_ACCOUNTS", "true")
    monkeypatch.setattr("agent.config.gym_registry_path", lambda: str(path))
    monkeypatch.setattr("agent.ops_alerts.alert", lambda _message: None)
    monkeypatch.setattr("agent.echo_clients.snapshot", lambda **_kw: clients)
    echo_clients.set_test_override(clients.is_client)
    accounts._dynamic_cache = None
    return path


def test_zanshin_approved_row_repairs_only_the_missing_registry_entry(monkeypatch, tmp_path):
    clients = _clients(extra_tokens=[{"gym_id": ADS_ONLY,
                                      "echo_account_key": "adsonly999999"}])
    path = _registry(monkeypatch, tmp_path, clients)
    # Any accidental call into the publisher is a test failure. Registry repair
    # merely makes the client visible to the next ordinary scheduler tick.
    monkeypatch.setattr("agent.calendar_autopublish.publish_client_gyms",
                        lambda *_a, **_kw: (_ for _ in ()).throw(
                            AssertionError("repair published a post")))
    rows = [_calendar(), _calendar("adsonly999999")]
    result = registry_reconcile.reconcile(clients=clients, calendar_rows=rows)

    assert result["ok"] and result["registered"] == [BASE]
    assert json.loads(path.read_text()) == [
        {"base": BASE, "name": "Zanshin Fitness", "ig_handle": "",
         "fb_page": "", "gym_id": ZANSHIN}]
    assert accounts.find_base_for_gym_id(ADS_ONLY) is None
    assert registry_reconcile.reconcile(clients=clients, calendar_rows=rows)["registered"] == []


def test_calendar_base_wins_over_a_different_portal_token(monkeypatch, tmp_path):
    clients = _clients(token="zanshinportalminted630e22")
    path = _registry(monkeypatch, tmp_path, clients)
    # The calendar base is an Echo-derived alias of this exact UUID; registering
    # the portal token instead would leave the approved row invisible.
    assert clients.key_to_gym[BASE] == ZANSHIN
    result = registry_reconcile.reconcile(clients=clients,
                                          calendar_rows=[_calendar()])
    assert result["registered"] == [BASE]
    assert json.loads(path.read_text())[0]["base"] == BASE


def test_positive_marker_and_one_issued_key_can_register_without_calendar(monkeypatch, tmp_path):
    clients = _clients()
    path = _registry(monkeypatch, tmp_path, clients)
    result = registry_reconcile.reconcile(clients=clients, calendar_rows=[])
    assert result["registered"] == [BASE]
    assert json.loads(path.read_text())[0]["gym_id"] == ZANSHIN


def test_multiple_calendar_bases_for_one_client_are_held(monkeypatch, tmp_path):
    clients = _clients()
    path = _registry(monkeypatch, tmp_path, clients)
    result = registry_reconcile.reconcile(
        clients=clients, calendar_rows=[_calendar(), _calendar("zanshinfitness")])
    assert result["registered"] == []
    assert (ZANSHIN, "competing calendar bases") in result["held"]
    assert not path.exists()


def test_bare_name_alias_in_calendar_cannot_bind_a_tenant(monkeypatch, tmp_path):
    clients = _clients()
    path = _registry(monkeypatch, tmp_path, clients)
    assert clients.key_to_gym["zanshinfitness"] == ZANSHIN
    result = registry_reconcile.reconcile(
        clients=clients, calendar_rows=[_calendar("zanshinfitness")])
    assert result["registered"] == []
    assert (ZANSHIN, "unsafe or ambiguous base") in result["held"]
    assert not path.exists()


def test_token_alias_shared_by_two_clients_is_held(monkeypatch, tmp_path):
    clients = _clients(extra_settings=[{"gym_id": OTHER_CLIENT}],
                       extra_tokens=[{"gym_id": OTHER_CLIENT,
                                      "echo_account_key": BASE}])
    path = _registry(monkeypatch, tmp_path, clients)
    assert BASE in clients.ambiguous_keys
    result = registry_reconcile.reconcile(clients=clients, calendar_rows=[_calendar()])
    assert result["registered"] == []
    assert not path.exists()


def test_ads_only_token_collision_cannot_claim_a_client_base(monkeypatch, tmp_path):
    clients = _clients(extra_tokens=[{"gym_id": ADS_ONLY,
                                      "echo_account_key": BASE}])
    path = _registry(monkeypatch, tmp_path, clients)
    assert BASE in clients.ambiguous_keys
    result = registry_reconcile.reconcile(clients=clients, calendar_rows=[_calendar()])
    assert result["registered"] == []
    assert not path.exists()


def test_two_issued_keys_without_calendar_do_not_guess(monkeypatch, tmp_path):
    clients = _clients(extra_tokens=[{"gym_id": ZANSHIN,
                                      "echo_account_key": "zanshinsecondkey"}])
    path = _registry(monkeypatch, tmp_path, clients)
    result = registry_reconcile.reconcile(clients=clients, calendar_rows=[])
    assert result["registered"] == []
    assert (ZANSHIN, "no unique issued key") in result["held"]
    assert not path.exists()


def test_existing_gym_id_or_base_is_never_duplicated_or_reassigned(monkeypatch, tmp_path):
    clients = _clients(extra_settings=[{"gym_id": OTHER_CLIENT}],
                       extra_tokens=[{"gym_id": OTHER_CLIENT,
                                      "echo_account_key": "otherclient222222"}])
    path = _registry(monkeypatch, tmp_path, clients)
    assert accounts.register_gym("otherclient222222", name="Other Client",
                                 gym_id=OTHER_CLIENT)
    assert accounts.register_gym(BASE, name="Zanshin Fitness", gym_id=ZANSHIN)
    before = path.read_text()
    # Identity guard returns the already registered base for this UUID; it must
    # not create a second row under Zanshin's base.
    assert accounts.register_gym(BASE, name="Other Client", gym_id=OTHER_CLIENT) == [
        "otherclient222222_ig", "otherclient222222_fb"]
    assert path.read_text() == before
    # A separate missing client cannot take over Zanshin's existing base.
    third = "33333333-0000-4000-8000-000000000004"
    echo_clients.set_test_override(lambda ident: True)
    assert accounts.register_gym(BASE, name="Third Gym", gym_id=third) == []
    assert path.read_text() == before
    result = registry_reconcile.reconcile(clients=clients, calendar_rows=[_calendar()])
    assert result["registered"] == []
    assert len(json.loads(path.read_text())) == 2


def test_repair_cannot_take_over_an_unstamped_row_created_after_its_read(monkeypatch,
                                                                         tmp_path):
    clients = _clients()
    path = _registry(monkeypatch, tmp_path, clients)
    path.write_text(json.dumps([{"base": BASE, "name": "Existing owner", "gym_id": ""}]))
    before = path.read_text()
    assert accounts.register_gym(BASE, name="Zanshin Fitness", gym_id=ZANSHIN,
                                 require_absent=True) == []
    assert path.read_text() == before
    result = registry_reconcile.reconcile(clients=clients,
                                          calendar_rows=[_calendar()])
    assert result["registered"] == []
    assert path.read_text() == before


def test_unreadable_client_universe_or_calendar_fails_closed(monkeypatch, tmp_path):
    path = _registry(monkeypatch, tmp_path, _clients())
    unknown = echo_clients.ClientSet(ok=False, error="marker table unreadable")
    result = registry_reconcile.reconcile(clients=unknown, calendar_rows=[_calendar()])
    assert not result["ok"] and "unreadable" in result["error"]
    assert not path.exists()

    monkeypatch.setattr(registry_reconcile, "_calendar_rows", lambda **_kw: None)
    result = registry_reconcile.reconcile(clients=_clients())
    assert not result["ok"] and "calendar" in result["error"]
    assert not path.exists()
    result = registry_reconcile.reconcile(clients=_clients(),
                                          calendar_rows=[{"gym_id": BASE}, "bad row"])
    assert not result["ok"] and result["error"] == "approved calendar returned invalid rows"
    assert not path.exists()


def test_corrupt_registry_is_preserved(monkeypatch, tmp_path):
    clients = _clients()
    path = _registry(monkeypatch, tmp_path, clients)
    path.write_text("{invalid json")
    result = registry_reconcile.reconcile(clients=clients, calendar_rows=[_calendar()])
    assert not result["ok"] and result["error"] == "publisher registry unreadable"
    assert path.read_text() == "{invalid json"
