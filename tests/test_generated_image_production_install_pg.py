"""Blocked PG17 install diagnostic tests, NOT production-shaped rehearsal proof.

The owner-frozen full baseline and compatible whole-file overlay are missing.
These focused static/stub checks intentionally do not synthesize that baseline
or replace the accepted disposable full-manifest PG17 tests.
"""
from copy import deepcopy
import hashlib
import json
import re

import pytest

from tools import generated_image_release_preflight as preflight


def receipt():
    return {
        "project_id": preflight.PROJECT, "database": "postgres", "server_version": "17.6",
        "system_identifier": "7642734024280108049", "captured_at_utc": "2026-10-09T15:46:39Z",
        "public_0611": [{"filename": "0611_echo_source_brand_bundle.sql", "checksum": preflight.SQL0611_SHA}],
        "supabase_0611": [{"version": "20261009143836", "name": "0611_echo_source_brand_bundle", "sql_sha256": preflight.SQL0611_SHA}],
        "ledger_0625": [], "public_0625": [], "objects_0625": [],
        "claim": [{"name": preflight.CLAIM, "identity": preflight.IDENTITY, "owner": "postgres",
                   "body_md5": preflight.LIVE_BODY_MD5, "acl": preflight.ACL[:],
                   "config": ["search_path=public"], "definer": True}],
    }


def body(filename):
    source = (preflight.ROOT / "migrations" / filename).read_text()
    function = re.search(r"create or replace function public\.claim_calendar_publish_slot_owned\(", source, re.I)
    rest = source[function.end():]
    delimiter = re.search(r"\bas\s+(\$\w*\$)", rest, re.I)
    return rest[delimiter.end():rest.index(delimiter[1], delimiter.end())]


def test_exact_whole_sources_expose_current_body_and_compatible_overlay():
    assert preflight.exact_files() == preflight.PINS
    current = body("lasso_october7_catchup_capacity_20261008.sql")
    old = body("calendar_approval_provenance_20261005.sql")
    overlay = body("DRAFT_fixer_forward_lock_entry_calendar_20261008.sql")
    assert hashlib.md5(current.encode()).hexdigest() == preflight.LIVE_BODY_MD5
    assert hashlib.md5(old.encode()).hexdigest() != preflight.ENTRY_EXPECTED_MD5
    assert preflight.ENTRY_EXPECTED_MD5 == preflight.LIVE_BODY_MD5
    assert hashlib.md5(overlay.encode()).hexdigest() == preflight.ENTRY_OVERLAY_MD5
    assert "p_capacity = 6" in current and "p_capacity = 6" in overlay
    assert "perform public.fixer_forward_calendar_entry_lock_20261008();" in overlay
    assert "fixer_forward_calendar_entry_lock" not in current
    cutover = (preflight.ROOT / "migrations/DRAFT_fixer_forward_corpus_atomic_cutover_20261008.sql").read_text()
    assert preflight.ENTRY_OVERLAY_MD5 in cutover
    assert "'c624eedcee819496129639108be991f6'" not in cutover


def test_live_receipt_cannot_be_misclassified_as_install_acceptance():
    report = preflight.preflight(receipt=receipt())
    assert report["state"] == "blocked"
    assert report["production_rehearsal"] == "BLOCKED"
    assert report["mutation_supported"] is False
    assert report["installation_authorized"] is False
    assert report["catalog"]["0611"] == "already_applied_ledger_only"
    assert report["catalog"]["0625"] == "pending"
    assert report["catalog"]["claim_source"] == "current_whole_file_match"
    assert report["catalog"]["calendar_entry"] == "body_prerequisite_only_matched"


@pytest.mark.parametrize("field,value", [
    ("owner", "other_owner"), ("identity", "p_row_id uuid"),
    ("acl", preflight.ACL + ["=X/postgres"]),
    ("config", ["search_path=public, pg_temp"]), ("definer", False),
    ("body_md5", "0" * 32),
])
def test_catalog_acl_or_body_drift_refuses_without_mutation(field, value):
    frozen = receipt()
    frozen["claim"][0][field] = value
    before = deepcopy(frozen)
    result = preflight.preflight(receipt=frozen)
    assert result["installation_authorized"] is False
    assert result["catalog"]["claim_source"] == "drift"
    assert frozen == before


@pytest.mark.parametrize("field", ["project_id", "database", "server_version", "system_identifier"])
def test_target_identity_drift(field):
    frozen = receipt()
    frozen[field] = "wrong"
    result = preflight.preflight(receipt=frozen)["catalog"]
    assert result["target"] == "drift"
    assert result["0611"] == "drift"
    assert result["0625"] != "pending"
    assert result["calendar_entry"] == "drift"


@pytest.mark.parametrize("field,value", [
    ("public_0611", []), ("supabase_0611", []),
    ("supabase_0611", [{"version": "0611", "name": "0611_echo_source_brand_bundle", "sql_sha256": preflight.SQL0611_SHA}]),
])
def test_partial_0611_is_drift(field, value):
    frozen = receipt()
    frozen[field] = value
    assert preflight.preflight(receipt=frozen)["catalog"]["0611"] == "drift"


@pytest.mark.parametrize("field", ["ledger_0625", "public_0625", "objects_0625"])
def test_partial_or_unverified_applied_0625_is_never_pending(field):
    frozen = receipt()
    frozen[field] = [{"version": "0625", "name": "present"}] if field == "ledger_0625" else ["present"]
    assert preflight.preflight(receipt=frozen)["catalog"]["0625"] == "drift_or_unverified_applied"


def test_missing_or_overloaded_claim_refuses():
    for claims in ([], receipt()["claim"] * 2):
        frozen = receipt()
        frozen["claim"] = claims
        assert preflight.preflight(receipt=frozen)["catalog"]["claim_scope"] == "drift"


def test_hash_drift_opens_no_connection(monkeypatch):
    changed = dict(preflight.PINS)
    changed[next(iter(changed))] = "0" * 64
    monkeypatch.setattr(preflight, "exact_files", lambda root: changed)
    def refuse_connection():
        pytest.fail("Hash drift must refuse before opening a DB connection")
    assert preflight.preflight(open_connection=refuse_connection)["state"] == "hash_drift"


class Connection:
    def __init__(self, failure=False):
        self.commands = []
        self.closed = False
        self.rolled_back = False
        self.failure = failure

    def execute(self, command):
        self.commands.append(command)
        if command == preflight.CATALOG_SQL and self.failure:
            raise RuntimeError("lost read ACK")
        return self

    def fetchone(self):
        return (receipt(),)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_fresh_readback_uses_new_connection_read_only_and_always_rolls_back():
    old = Connection(failure=True)
    with pytest.raises(RuntimeError, match="lost read ACK"):
        preflight.preflight(open_connection=lambda: old)
    fresh = Connection()
    result = preflight.preflight(open_connection=lambda: fresh)
    for connection in (old, fresh):
        assert connection.commands == ["BEGIN READ ONLY",
            "SET LOCAL statement_timeout='30s'; SET LOCAL lock_timeout='5s'", preflight.CATALOG_SQL]
        assert connection.rolled_back and connection.closed
    assert result["catalog"]["calendar_entry"] == "body_prerequisite_only_matched"
    assert "COMMIT" not in fresh.commands


def test_default_cli_stays_no_go_and_has_no_apply_flag(capsys):
    assert preflight.main([]) == 2
    assert '"state": "catalog_receipt_required"' in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        preflight.main(["--apply"])
    assert error.value.code == 2


def test_different_valid_0611_ledger_version_is_drift():
    frozen = receipt()
    frozen["supabase_0611"][0]["version"] = "20261009143837"
    result = preflight.preflight(receipt=frozen)
    assert result["catalog"]["0611"] == "drift"
    assert result["installation_authorized"] is False
    assert preflight.SQL0611_VERSION == "20261009143836"


@pytest.mark.parametrize("path,value", [
    ((), None), ((), "arbitrary-private-content"), ((), 7), ((), []),
    (("public_0611",), None), (("public_0611",), "arbitrary-private-content"),
    (("public_0611", 0), None), (("public_0611", 0), "arbitrary-private-content"),
    (("supabase_0611", 0), None), (("supabase_0611", 0), 1),
    (("supabase_0611", 0, "version"), None),
    (("ledger_0625",), [None]), (("ledger_0625",), ["arbitrary-private-content"]),
    (("claim", 0), None), (("claim", 0), "arbitrary-private-content"),
    (("claim", 0, "acl"), None), (("claim", 0, "acl"), "arbitrary-private-content"),
    (("claim", 0, "acl"), [None]), (("claim", 0, "acl"), [1, "arbitrary-private-content"]),
    (("claim", 0, "acl"), {"arbitrary-private-content": True}),
    (("claim", 0, "body_md5"), "arbitrary-private-content"),
    (("claim", 0, "body_md5"), "a" * 31), (("claim", 0, "body_md5"), None),
    (("claim", 0, "config"), [None]), (("claim", 0, "definer"), "true"),
    (("captured_at_utc",), None), (("captured_at_utc",), "arbitrary-private-content"),
    (("captured_at_utc",), "2026-02-30T15:46:39Z"),
    (("captured_at_utc",), "2026-10-09T25:46:39Z"),
    (("captured_at_utc",), "2026-10-09T15:46:39"),
])
def test_invalid_nested_receipt_is_sanitized_cli_no_go(path, value, tmp_path, capsys):
    frozen = receipt()
    if path:
        target = frozen
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    else:
        frozen = value
    before = deepcopy(frozen)
    result = preflight.preflight(receipt=frozen)
    assert result["state"] == "invalid_catalog_receipt"
    assert result["installation_authorized"] is False
    assert "catalog" not in result
    assert preflight.reconcile(frozen) == {"state": "invalid_catalog_receipt"}
    assert frozen == before
    source = tmp_path / "receipt.json"
    source.write_text(json.dumps(frozen))
    assert preflight.main(["--catalog-receipt", str(source)]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["state"] == "invalid_catalog_receipt"
    assert "arbitrary-private-content" not in output.out + output.err
    assert "Traceback" not in output.out + output.err
    assert output.err == ""


@pytest.mark.parametrize("data", [b'{"arbitrary-private-content":', b'\xff', b'{' * 2000])
def test_bad_receipt_file_is_structured_cli_no_go(data, tmp_path, capsys):
    source = tmp_path / "receipt.json"
    source.write_bytes(data)
    assert preflight.main(["--catalog-receipt", str(source)]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["state"] == "invalid_catalog_receipt"
    assert output.err == ""
    assert "arbitrary-private-content" not in output.out


def test_extra_offline_fields_are_not_reflected():
    frozen = receipt()
    frozen["untrusted"] = "arbitrary-private-content"
    frozen["claim"][0]["untrusted"] = "arbitrary-private-content"
    result = preflight.preflight(receipt=frozen)
    assert result["catalog"]["0611"] == "already_applied_ledger_only"
    assert "arbitrary-private-content" not in json.dumps(result)


def test_deleted_nullable_config_is_sanitized_api_and_cli_no_go(tmp_path, capsys):
    frozen = receipt()
    del frozen["claim"][0]["config"]
    frozen["claim"][0]["untrusted"] = "arbitrary-private-content"
    result = preflight.preflight(receipt=frozen)
    assert result["state"] == "invalid_catalog_receipt"
    assert result["installation_authorized"] is False
    assert "catalog" not in result
    assert preflight.reconcile(frozen) == {"state": "invalid_catalog_receipt"}
    source = tmp_path / "receipt.json"
    source.write_text(json.dumps(frozen))
    assert preflight.main(["--catalog-receipt", str(source)]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["state"] == "invalid_catalog_receipt"
    assert output.err == ""
    assert "arbitrary-private-content" not in output.out
    assert "Traceback" not in output.out
