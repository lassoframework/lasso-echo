"""The legacy registry operator may fill identities, never infer or rewrite more."""

import hashlib
import json
import time
from contextlib import contextmanager

import pytest

from agent import echo_clients, registry_identity_backfill as backfill

PIERCE = "11111111-aaaa-4000-8000-000000000001"
HILL = "22222222-bbbb-4000-8000-000000000002"
ADS = "33333333-cccc-4000-8000-000000000003"
COMMIT = "a" * 40


def _clients(*, tokens=None, settings=None, now=None):
    tokens = tokens if tokens is not None else [
        {"gym_id": PIERCE, "echo_account_key": "piercefitness"},
        {"gym_id": HILL, "echo_account_key": "hillcountry"},
        {"gym_id": ADS, "echo_account_key": "adsonly"},
    ]
    settings = settings if settings is not None else [
        {"gym_id": PIERCE}, {"gym_id": HILL}]
    gyms = [{"id": PIERCE, "name": "Pierce Fitness", "slug": "pierce-fitness"},
            {"id": HILL, "name": "Hill Country", "slug": "hill-country"},
            {"id": ADS, "name": "Ads Only", "slug": "ads-only"}]
    return echo_clients.build(settings, tokens, gyms, now=now or time.time())


def _run(path, clients, **options):
    return backfill.execute(path=path, snapshot_fn=lambda **kwargs: clients,
                            clock=lambda: clients.at + 1,
                            environ={"RAILWAY_GIT_COMMIT_SHA": COMMIT}, **options)


def _expectations(plan):
    return {"expected_original_sha256": plan.original_sha256,
            "expected_new_sha256": plan.new_sha256,
            "expected_mappings_sha256": plan.mappings_sha256,
            "expected_deployed_commit": COMMIT}


def test_default_dry_run_preserves_every_unrelated_byte_and_row_order(tmp_path):
    path = tmp_path / "gym_accounts.json"
    original = (
        '[\n  {"base":"piercefitness","name":"Piercé","extra":{"x":1}},\n'
        '  {"base":"hillcountry","gym_id":"","note":"keep"}\n]\n'
    ).encode("utf-8")
    path.write_bytes(original)
    result = _run(path, _clients())
    plan = result["plan"]
    assert result["status"] == "dry_run" and path.read_bytes() == original
    assert [m["base"] for m in plan.mappings] == ["piercefitness", "hillcountry"]
    expected = (
        '[\n  {"base":"piercefitness","name":"Piercé","extra":{"x":1},'
        '"gym_id":"' + PIERCE + '"},\n'
        '  {"base":"hillcountry","gym_id":"' + HILL + '","note":"keep"}\n]\n'
    ).encode("utf-8")
    assert plan.new_bytes == expected
    assert plan.original_sha256 == hashlib.sha256(original).hexdigest()
    assert plan.new_sha256 == hashlib.sha256(expected).hexdigest()
    assert list(tmp_path.iterdir()) == [path]


def test_nine_known_legacy_rows_in_thirteen_row_registry_plan_together(tmp_path):
    path = tmp_path / "gym_accounts.json"
    bases = sorted(backfill._LEGACY_BASES)
    ids = [f"{index + 1:08x}-1111-4000-8000-{index + 1:012x}"
           for index in range(len(bases))]
    clients = echo_clients.build(
        [{"gym_id": gid} for gid in ids],
        [{"gym_id": gid, "echo_account_key": base}
         for base, gid in zip(bases, ids)],
        [{"id": gid, "name": f"Gym {index}", "slug": f"gym-{index}"}
         for index, gid in enumerate(ids)], now=time.time())
    rows = [{"base": base, "name": f"Legacy {index}", "tag": index}
            for index, base in enumerate(bases)]
    rows += [{"base": f"current{index}",
              "gym_id": f"{index + 20:08x}-1111-4000-8000-{index + 20:012x}",
              "tag": "unchanged"} for index in range(4)]
    raw = (json.dumps(rows, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(raw)
    plan = _run(path, clients)["plan"]
    assert plan.row_count == 13 and len(plan.mappings) == 9
    assert [m["base"] for m in plan.mappings] == bases
    new_rows = json.loads(plan.new_bytes)
    assert len(new_rows) == 13
    for index, gid in enumerate(ids):
        assert new_rows[index] == {**rows[index], "gym_id": gid}
    assert new_rows[9:] == rows[9:]
    assert path.read_bytes() == raw


def test_bare_name_alias_cannot_backfill_when_ads_only_gym_shares_name(tmp_path):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"}]'
    path.write_bytes(raw)
    clients = echo_clients.build(
        [{"gym_id": PIERCE}],
        [{"gym_id": PIERCE, "echo_account_key": "pierceissued"},
         {"gym_id": ADS, "echo_account_key": "adsonlyissued"}],
        [{"id": PIERCE, "name": "Pierce Fitness", "slug": "piercefitness"},
         {"id": ADS, "name": "Pierce Fitness", "slug": "piercefitness"}],
        now=time.time())
    assert clients.key_to_gym["piercefitness"] == PIERCE
    assert clients.token_keys_by_gym[PIERCE] == frozenset({"pierceissued"})
    with pytest.raises(backfill.BackfillBlocked, match="unique Echo owner"):
        _run(path, clients)
    assert path.read_bytes() == raw


def test_exact_raw_uuid_derived_key_can_backfill_without_issued_key(tmp_path):
    path = tmp_path / "gym_accounts.json"
    gid = "30b5b200-aaaa-4000-8000-000000000004"
    base = "crossfitreverb30b5b2"
    raw = ('[{"base":"' + base + '"}]').encode()
    path.write_bytes(raw)
    clients = echo_clients.build(
        [{"gym_id": gid}], [],
        [{"id": gid, "name": "CrossFit Reverb", "slug": "crossfit-reverb"}],
        now=time.time())
    assert base == echo_clients._portal_key(gid, "CrossFit Reverb")
    assert _run(path, clients)["plan"].mappings[0]["gym_id"] == gid
    assert path.read_bytes() == raw


@pytest.mark.parametrize("bad_source", ["marker", "issued_owner", "gym_row"])
def test_suffix_on_raw_authoritative_uuid_never_becomes_identity(tmp_path, bad_source):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"}]'
    path.write_bytes(raw)
    clients = echo_clients.build(
        [{"gym_id": PIERCE + "_ig" if bad_source == "marker" else PIERCE}],
        [{"gym_id": PIERCE + "_ig" if bad_source == "issued_owner" else PIERCE,
          "echo_account_key": "piercefitness"}],
        [{"id": PIERCE + "_ig" if bad_source == "gym_row" else PIERCE,
          "name": "Pierce Fitness", "slug": "piercefitness"}], now=time.time())
    with pytest.raises(backfill.BackfillBlocked, match="unique Echo owner"):
        _run(path, clients)
    assert path.read_bytes() == raw


def test_symlink_registry_target_is_rejected_before_read_or_write(tmp_path):
    actual = tmp_path / "actual.json"
    raw = b'[{"base":"piercefitness"}]'
    actual.write_bytes(raw)
    link = tmp_path / "gym_accounts.json"
    link.symlink_to(actual)
    with pytest.raises(backfill.BackfillBlocked, match="not a regular file"):
        _run(link, _clients())
    plan = backfill.build_plan(raw, _clients())
    with pytest.raises(backfill.BackfillBlocked, match="not a regular file"):
        _run(link, _clients(), apply=True, **_expectations(plan))
    assert actual.read_bytes() == raw
    assert not list(tmp_path.glob("*.backup"))


def test_apply_is_guarded_backed_up_receipted_and_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "gym_accounts.json"
    original = b'[{"base":"piercefitness","name":"Pierce"}]\n'
    path.write_bytes(original)
    clients = _clients()
    monkeypatch.setattr("agent.calendar_autopublish.publish_client_gyms",
                        lambda *_a, **_kw: pytest.fail("operator published"))
    plan = _run(path, clients)["plan"]
    result = _run(path, clients, apply=True, **_expectations(plan))
    assert result["status"] == "applied" and path.read_bytes() == plan.new_bytes
    assert backfill._sha(path.read_bytes()) == plan.new_sha256
    backup = tmp_path / result["backup"].split("/")[-1]
    receipt_path = tmp_path / result["receipt"].split("/")[-1]
    assert backup.read_bytes() == original
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "applied"
    assert receipt["mappings"] == list(plan.mappings)
    assert receipt["original_sha256"] == plan.original_sha256
    assert receipt["backup_sha256"] == plan.original_sha256
    assert receipt["new_sha256"] == receipt["readback_sha256"] == plan.new_sha256
    assert receipt["expected_deployed_commit"] == receipt["observed_deployed_commit"] == COMMIT
    assert plan.original_sha256 in receipt["rollback"]
    assert result["receipt_sha256"] == backfill._sha(receipt_path.read_bytes())
    files_before = set(tmp_path.iterdir())
    again = _run(path, clients, apply=True, **_expectations(plan))
    assert again["status"] == "already_applied"
    assert set(tmp_path.iterdir()) == files_before


def test_failed_post_write_readback_keeps_backup_and_prepared_receipt(tmp_path,
                                                                      monkeypatch):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"}]'
    path.write_bytes(raw)
    clients = _clients()
    plan = _run(path, clients)["plan"]
    real_write = backfill._write_new

    def corrupt_registry(target, data, mode):
        if target == path:
            return real_write(target, b"corrupt", mode)
        return real_write(target, data, mode)

    monkeypatch.setattr(backfill, "_write_new", corrupt_registry)
    with pytest.raises(backfill.BackfillBlocked, match="post-write registry checksum"):
        _run(path, clients, apply=True, **_expectations(plan))
    backups = list(tmp_path.glob("*.backup"))
    receipts = list(tmp_path.glob("*.receipt.json"))
    assert len(backups) == len(receipts) == 1
    assert backups[0].read_bytes() == raw
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "prepared"
    assert receipt["original_sha256"] == backfill._sha(raw)


@pytest.mark.parametrize("fail_after_replace", [False, True])
def test_failed_applied_receipt_write_is_safely_finalized_on_retry(
        tmp_path, monkeypatch, fail_after_replace):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"}]'
    path.write_bytes(raw)
    clients = _clients()
    plan = _run(path, clients)["plan"]
    real_write = backfill._write_new
    failed = {"once": False}

    def fail_receipt(target, data, mode):
        if str(target).endswith(".receipt.json") and not failed["once"]:
            failed["once"] = True
            if fail_after_replace:
                real_write(target, data, mode)
            raise OSError("simulated receipt write failure")
        return real_write(target, data, mode)

    monkeypatch.setattr(backfill, "_write_new", fail_receipt)
    with pytest.raises(backfill.BackfillBlocked, match="registry I/O failed"):
        _run(path, clients, apply=True, **_expectations(plan))
    assert path.read_bytes() == plan.new_bytes
    assert failed["once"]
    monkeypatch.setattr(backfill, "_write_new", real_write)
    recovered = _run(path, clients, apply=True, **_expectations(plan))
    receipt = json.loads((tmp_path / recovered["receipt"].split("/")[-1]).read_text())
    assert receipt["status"] == "applied"
    assert receipt["readback_sha256"] == plan.new_sha256
    assert recovered["status"] == ("already_applied" if fail_after_replace else "recovered")
    assert len(list(tmp_path.glob("*.backup"))) == 1
    assert _run(path, clients, apply=True, **_expectations(plan))["status"] == "already_applied"


def test_recovery_refuses_tampered_prepared_receipt(tmp_path, monkeypatch):
    path = tmp_path / "gym_accounts.json"
    path.write_bytes(b'[{"base":"piercefitness"}]')
    clients = _clients()
    plan = _run(path, clients)["plan"]
    real_write = backfill._write_new

    def fail_receipt(target, data, mode):
        if str(target).endswith(".receipt.json"):
            raise OSError("simulated receipt failure")
        return real_write(target, data, mode)

    monkeypatch.setattr(backfill, "_write_new", fail_receipt)
    with pytest.raises(backfill.BackfillBlocked):
        _run(path, clients, apply=True, **_expectations(plan))
    monkeypatch.setattr(backfill, "_write_new", real_write)
    receipt_path = next(tmp_path.glob("*.receipt.json"))
    receipt = json.loads(receipt_path.read_text())
    receipt["mappings"][0]["gym_id"] = ADS
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(backfill.BackfillBlocked, match="receipt disagrees"):
        _run(path, clients, apply=True, **_expectations(plan))
    assert path.read_bytes() == plan.new_bytes
    assert json.loads(receipt_path.read_text())["status"] == "prepared"


@pytest.mark.parametrize("raw", [
    b'[{"base":"piercefitness","base":"hillcountry"}]',
    b'[{"base":"piercefitness"},{"base":"piercefitness"}]',
    b'[{"base":"piercefitness","gym_id":"not-a-uuid"}]',
    b'[{"base":"piercefitness","gym_id":"11111111-aaaa-4000-8000-000000000001"},'
    b'{"base":"hillcountry","gym_id":"11111111-AAAA-4000-8000-000000000001"}]',
    b'[{"base":"piercefitness"},42]',
    b'{"base":"piercefitness"}',
])
def test_malformed_or_duplicate_registry_blocks_the_whole_plan(tmp_path, raw):
    path = tmp_path / "gym_accounts.json"
    path.write_bytes(raw)
    with pytest.raises(backfill.BackfillBlocked):
        _run(path, _clients())
    assert path.read_bytes() == raw


def test_unknown_ads_only_and_ambiguous_mappings_refuse_partial_backfill(tmp_path):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"},{"base":"crossfitlocal"}]'
    path.write_bytes(raw)
    ads_claim = _clients(tokens=[
        {"gym_id": PIERCE, "echo_account_key": "piercefitness"},
        {"gym_id": HILL, "echo_account_key": "hillcountry"},
        {"gym_id": ADS, "echo_account_key": "crossfitlocal"},
    ])
    with pytest.raises(backfill.BackfillBlocked, match="unique Echo owner"):
        _run(path, ads_claim)
    assert path.read_bytes() == raw

    raw = b'[{"base":"piercefitness"},{"base":"crossfitlocal"}]'
    path.write_bytes(raw)
    with pytest.raises(backfill.BackfillBlocked, match="unique Echo owner"):
        _run(path, _clients())
    assert path.read_bytes() == raw

    raw = b'[{"base":"piercefitness"},{"base":"unknownclient"}]'
    path.write_bytes(raw)
    with pytest.raises(backfill.BackfillBlocked, match="outside legacy scope"):
        _run(path, _clients())
    assert path.read_bytes() == raw

    ambiguous = _clients(tokens=[
        {"gym_id": PIERCE, "echo_account_key": "piercefitness"},
        {"gym_id": HILL, "echo_account_key": "piercefitness"},
    ])
    path.write_bytes(b'[{"base":"piercefitness"}]')
    with pytest.raises(backfill.BackfillBlocked, match="unique Echo owner"):
        _run(path, ambiguous)


def test_two_missing_bases_cannot_resolve_to_one_uuid(tmp_path):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"},{"base":"crossfitnewtown"}]'
    path.write_bytes(raw)
    clients = _clients(tokens=[
        {"gym_id": PIERCE, "echo_account_key": "piercefitness"},
        {"gym_id": PIERCE, "echo_account_key": "crossfitnewtown"},
    ])
    with pytest.raises(backfill.BackfillBlocked, match="one gym UUID"):
        _run(path, clients)
    assert path.read_bytes() == raw


def test_existing_uuid_cannot_be_assigned_to_a_second_base(tmp_path):
    path = tmp_path / "gym_accounts.json"
    raw = ('[{"base":"legacyexisting","gym_id":"' + PIERCE
           + '"},{"base":"piercefitness"}]').encode()
    path.write_bytes(raw)
    with pytest.raises(backfill.BackfillBlocked, match="one gym UUID"):
        _run(path, _clients())
    assert path.read_bytes() == raw


def test_stale_or_unreadable_client_snapshot_fails_closed(tmp_path):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"}]'
    path.write_bytes(raw)
    stale = _clients(now=time.time() - 1000)
    with pytest.raises(backfill.BackfillBlocked, match="fresh authoritative"):
        backfill.execute(path=path, snapshot_fn=lambda **_kw: stale,
                         clock=time.time)
    unknown = echo_clients.ClientSet(ok=False, error="marker read failed", at=time.time())
    with pytest.raises(backfill.BackfillBlocked, match="fresh authoritative"):
        backfill.execute(path=path, snapshot_fn=lambda **_kw: unknown,
                         clock=time.time)
    with pytest.raises(backfill.BackfillBlocked, match="fresh authoritative"):
        backfill.execute(path=path,
                         snapshot_fn=lambda **_kw: (_ for _ in ()).throw(OSError()),
                         clock=time.time)
    assert path.read_bytes() == raw


def test_changed_hash_plan_or_deployment_refuses_apply_without_backup(tmp_path):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"}]'
    path.write_bytes(raw)
    clients = _clients()
    plan = _run(path, clients)["plan"]
    bad = _expectations(plan)
    bad["expected_deployed_commit"] = "b" * 40
    with pytest.raises(backfill.BackfillBlocked, match="deployed commit"):
        _run(path, clients, apply=True, **bad)
    bad = _expectations(plan)
    bad["expected_mappings_sha256"] = "0" * 64
    with pytest.raises(backfill.BackfillBlocked, match="plan changed"):
        _run(path, clients, apply=True, **bad)
    path.write_bytes(raw + b" \n")
    with pytest.raises(backfill.BackfillBlocked, match="plan changed"):
        _run(path, clients, apply=True, **_expectations(plan))
    assert not list(tmp_path.glob("*.backup"))


def test_lock_failure_refuses_apply_before_any_backup_or_registry_write(tmp_path,
                                                                          monkeypatch):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"}]'
    path.write_bytes(raw)
    clients = _clients()
    plan = _run(path, clients)["plan"]
    monkeypatch.setattr(backfill.fcntl, "flock",
                        lambda *_a: (_ for _ in ()).throw(OSError("lock failed")))
    with pytest.raises(backfill.BackfillBlocked, match="advisory lock"):
        _run(path, clients, apply=True, **_expectations(plan))
    assert path.read_bytes() == raw
    assert not list(tmp_path.glob("*.backup"))


def test_apply_refreshes_ownership_inside_registry_lock(tmp_path, monkeypatch):
    path = tmp_path / "gym_accounts.json"
    raw = b'[{"base":"piercefitness"}]'
    path.write_bytes(raw)
    clients = _clients()
    plan = _run(path, clients)["plan"]
    state = {"locked": False, "snapshot_calls": 0}

    @contextmanager
    def locked(_path):
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    def snapshot(**_kwargs):
        state["snapshot_calls"] += 1
        assert state["locked"]
        return clients

    monkeypatch.setattr(backfill, "_registry_lock", locked)
    result = backfill.execute(
        path=path, snapshot_fn=snapshot, clock=lambda: clients.at + 1,
        environ={"RAILWAY_GIT_COMMIT_SHA": COMMIT}, apply=True,
        **_expectations(plan))
    assert result["status"] == "applied"
    assert state["snapshot_calls"] == 1
    assert not state["locked"]
