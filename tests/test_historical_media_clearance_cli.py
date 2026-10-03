"""Operator CLI for the DRAFT historical clearance ledger — offline tests.

A FakeStore implements exactly the four SupabaseMediaStore methods the CLI
calls; no network, no secrets, no production path. Every write is driven with
--yes (the interactive confirmation is a TTY-only safeguard, tested separately
via a non-TTY refusal).
"""

import hashlib
import io
import json
import sys

import pytest

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from agent.media_source_store import MediaStoreError  # noqa: E402
from scripts import historical_media_clearance as cli  # noqa: E402

GYM = "gymx"
ASSET = "drive-file-1"
HASH_A = hashlib.sha256(b"bytes-a").hexdigest()
HASH_B = hashlib.sha256(b"bytes-b").hexdigest()


class FakeStore:
    """Exact-shape stand-in for SupabaseMediaStore's four ledger methods."""

    def __init__(self):
        self.sources = [{"id": "src1", "kind": "gym_drive", "name": "Drive A",
                         "folder_id": "fld1", "gym_id": GYM}]
        self.assets = {ASSET: {"id": ASSET, "gym_id": GYM, "content_hash": HASH_A,
                               "source_id": "src1", "first_indexed_at": "2026-06-01T00:00:00Z",
                               "indexed_at": "2026-09-01T00:00:00Z", "used_count": 0}}
        self.receipts = []          # rows as the table would return them
        self.recorded = []
        self.revoked = []
        self.fail_with = None       # simulate absent/unapplied DRAFT migration

    def _boom(self):
        if self.fail_with:
            raise self.fail_with

    def list_sources(self, gym_id, include_inactive=False):
        self._boom()
        assert gym_id == GYM
        return list(self.sources)

    def list_assets(self, gym_id, source_id=None):
        self._boom()
        assert gym_id == GYM
        return [dict(a) for a in self.assets.values()]

    def get_asset(self, asset_id):
        self._boom()
        asset = self.assets.get(asset_id)
        return dict(asset) if asset else None

    def list_historical_clearances(self, gym_id):
        self._boom()
        assert gym_id == GYM
        return [dict(r) for r in self.receipts]

    def record_historical_clearance(self, gym_id, asset_id, content_hash,
                                    decision, reviewer, evidence):
        self._boom()
        if not isinstance(evidence, dict) or not evidence:
            raise MediaStoreError(400, "invalid evidence")
        self.recorded.append({"gym_id": gym_id, "asset_id": asset_id,
                              "content_hash": content_hash, "decision": decision,
                              "reviewer": reviewer, "evidence": dict(evidence)})
        return {"gym_id": gym_id, "asset_id": asset_id, "version": 1,
                "decision": decision}

    def revoke_historical_clearance(self, gym_id, asset_id,
                                    expected_content_hash, reviewer, reason):
        self._boom()
        self.revoked.append({"gym_id": gym_id, "asset_id": asset_id,
                             "content_hash": expected_content_hash,
                             "reviewer": reviewer, "reason": reason})
        return True


def _evidence(content_hash=HASH_A, source_id="src1", asset_id=ASSET, gym_id=GYM,
              extra=None, concrete=True):
    ev = {"gym_id": gym_id, "asset_id": asset_id, "source_id": source_id,
          "content_hash": content_hash, "method": "drive_version_compare"}
    if concrete:      # concrete cleared-decision evidence, not identity-only
        ev.update({"observed_at": "2026-09-30T12:00:00Z",
                   "result": "drive version history shows no use since 2025-06",
                   "proof_ref": "drive-revisions-compare:20260930"})
    ev.update(extra or {})
    return ev


def _identity_only_evidence():
    return _evidence(concrete=False)


def _run(store, argv):
    out = io.StringIO()
    code = cli.main(argv, store=store, out=out)
    return code, out.getvalue()


# ---- list --------------------------------------------------------------------

def test_list_shows_reviewable_assets_with_hash_source_receipt_evidence():
    store = FakeStore()
    store.receipts.append({"gym_id": GYM, "asset_id": ASSET, "version": 1,
                           "source_id": "src1", "content_hash": HASH_A,
                           "decision": "cleared", "reviewer": "blake",
                           "evidence": _evidence(), "revoked_at": None})
    code, out = _run(store, ["list", "--gym", GYM])
    assert code == 0
    payload = json.loads(out)
    assert payload["gym_id"] == GYM
    (row,) = payload["reviewable_assets"]
    assert row["asset_id"] == ASSET
    assert row["content_hash"] == HASH_A          # CURRENT hash, never inferred
    assert row["drive_source"]["folder_id"] == "fld1"
    assert row["first_indexed_at"] == "2026-06-01T00:00:00Z"
    assert row["used_count"] == 0
    assert row["active_receipt"]["decision"] == "cleared"
    # P1: default list output is REDACTED — bounded summary, never raw evidence
    assert "evidence" not in row["active_receipt"]
    summary = row["active_receipt"]["evidence_summary"]
    assert summary["field_count"] == len(store.receipts[0]["evidence"])
    assert set(summary["keys"]) == set(store.receipts[0]["evidence"].keys())
    assert _evidence()["proof_ref"] not in out   # raw evidence values never leak


def test_list_is_read_only():
    store = FakeStore()
    code, _ = _run(store, ["list", "--gym", GYM])
    assert code == 0
    assert store.recorded == [] and store.revoked == []


def test_list_requires_gym():
    code, _ = _run(FakeStore(), ["list", "--gym", " "])
    assert code == 2


def test_list_fails_closed_when_migration_absent():
    store = FakeStore()
    store.fail_with = MediaStoreError(404, "relation media_historical_clearance does not exist")
    code, _ = _run(store, ["list", "--gym", GYM])
    assert code == 3                       # never a silent empty list
    assert store.recorded == []


def test_list_show_evidence_requires_exact_asset_opt_in():
    store = FakeStore()
    store.receipts.append({"gym_id": GYM, "asset_id": ASSET, "version": 1,
                           "source_hash": None, "content_hash": HASH_A,
                           "decision": "cleared", "reviewer": "blake",
                           "evidence": _evidence(), "revoked_at": None})
    code, _ = _run(store, ["list", "--gym", GYM, "--show-evidence"])
    assert code == 2                        # full evidence is exact-asset opt-in only
    code, out = _run(store, ["list", "--gym", GYM, "--asset", ASSET,
                             "--show-evidence"])
    assert code == 0
    (row,) = json.loads(out)["reviewable_assets"]
    assert row["active_receipt"]["evidence"]["content_hash"] == HASH_A
    code, _ = _run(store, ["list", "--gym", GYM, "--asset", "nope",
                           "--show-evidence"])
    assert code == 2                        # exact scoping preserved


def test_history_shows_bounded_versions_per_exact_asset():
    store = FakeStore()
    store.receipts.append({"gym_id": GYM, "asset_id": ASSET, "version": 1,
                           "content_hash": HASH_A, "decision": "held",
                           "reviewer": "ops", "recorded_at": "2026-09-01T00:00:00Z",
                           "evidence": _evidence(), "revoked_at": "2026-09-02T00:00:00Z"})
    store.receipts.append({"gym_id": GYM, "asset_id": ASSET, "version": 2,
                           "content_hash": HASH_A, "decision": "cleared",
                           "reviewer": "blake", "recorded_at": "2026-09-30T00:00:00Z",
                           "evidence": _evidence(), "revoked_at": None})
    store.receipts.append({"gym_id": GYM, "asset_id": "other-asset", "version": 1,
                           "content_hash": HASH_B, "decision": "held",
                           "reviewer": "ops", "evidence": _evidence(asset_id="other-asset"),
                           "revoked_at": None})
    code, out = _run(store, ["history", "--gym", GYM, "--asset", ASSET])
    assert code == 0
    payload = json.loads(out)
    assert [v["version"] for v in payload["versions"]] == [1, 2]   # per exact asset
    assert payload["active_version"] == 2
    assert payload["current_content_hash"] == HASH_A
    for v in payload["versions"]:
        assert "evidence" not in v and v["evidence_summary"]["field_count"] > 0
    assert "other-asset" not in out and "ops-evidence" not in out
    code, out = _run(store, ["history", "--gym", GYM, "--asset", ASSET,
                             "--show-evidence"])
    assert code == 0
    payload = json.loads(out)
    assert payload["versions"][1]["evidence"]["proof_ref"].startswith("drive-")
    code, _ = _run(store, ["history", "--gym", GYM, "--asset", "ghost"])
    assert code == 2                        # exact scoping
    code, _ = _run(store, ["history", "--gym", "other-gym", "--asset", ASSET])
    assert code == 2


# ---- record ------------------------------------------------------------------

def test_record_happy_path_binds_exact_identity():
    store = FakeStore()
    code, out = _run(store, ["record", "--gym", GYM, "--asset", ASSET,
                             "--content-hash", HASH_A, "--decision", "cleared",
                             "--reviewer", "blake", "--yes",
                             "--evidence", json.dumps(_evidence())])
    assert code == 0, out
    (call,) = store.recorded
    assert call["gym_id"] == GYM and call["asset_id"] == ASSET
    assert call["content_hash"] == HASH_A and call["decision"] == "cleared"
    assert call["evidence"]["source_id"] == "src1"


@pytest.mark.parametrize("missing", ["method", "observed_at", "result"])
def test_record_cleared_requires_concrete_evidence_fields(missing):
    store = FakeStore()
    code, _ = _run(store, ["record", "--gym", GYM, "--asset", ASSET,
                           "--content-hash", HASH_A, "--decision", "cleared",
                           "--reviewer", "blake", "--yes",
                           "--evidence", json.dumps(_evidence())])
    assert code == 0 and store.recorded        # sanity: full evidence records
    ev = _evidence()
    ev.pop(missing)
    store = FakeStore()
    code, _ = _run(store, ["record", "--gym", GYM, "--asset", ASSET,
                           "--content-hash", HASH_A, "--decision", "cleared",
                           "--reviewer", "blake", "--yes",
                           "--evidence", json.dumps(ev)])
    assert code == 2 and store.recorded == []


def test_record_cleared_refuses_identity_only_evidence_and_requires_proof_ref():
    store = FakeStore()
    code, _ = _run(store, ["record", "--gym", GYM, "--asset", ASSET,
                           "--content-hash", HASH_A, "--decision", "cleared",
                           "--reviewer", "blake", "--yes",
                           "--evidence", json.dumps(_identity_only_evidence())])
    assert code == 2 and store.recorded == []   # four IDs alone never clear
    ev = _evidence()
    for k in ("proof_ref", "reviewer_assertion", "assertion_ref"):
        ev.pop(k, None)
    code, _ = _run(FakeStore(), ["record", "--gym", GYM, "--asset", ASSET,
                                 "--content-hash", HASH_A, "--decision", "cleared",
                                 "--reviewer", "blake", "--yes",
                                 "--evidence", json.dumps(ev)])
    assert code == 2                            # assertion/proof reference required
    code, _ = _run(FakeStore(), ["record", "--gym", GYM, "--asset", ASSET,
                                 "--content-hash", HASH_A, "--decision", "held",
                                 "--reviewer", "blake", "--yes",
                                 "--evidence", json.dumps(_identity_only_evidence())])
    assert code == 0                            # non-cleared decisions unaffected


def test_record_refuses_stale_hash_and_never_calls_store():
    store = FakeStore()
    code, _ = _run(store, ["record", "--gym", GYM, "--asset", ASSET,
                           "--content-hash", HASH_B, "--decision", "cleared",
                           "--reviewer", "blake", "--yes",
                           "--evidence", json.dumps(_evidence(content_hash=HASH_B))])
    assert code == 2
    assert store.recorded == []            # never inferred, never auto-cleared


def test_record_requires_exact_gym_asset_scope():
    store = FakeStore()
    code, _ = _run(store, ["record", "--gym", "other-gym", "--asset", ASSET,
                           "--content-hash", HASH_A, "--decision", "cleared",
                           "--reviewer", "blake", "--yes",
                           "--evidence", json.dumps(_evidence())])
    assert code == 2                       # asset does not belong to that gym
    assert store.recorded == []


@pytest.mark.parametrize("mutate", [
    lambda ev: ev.update(gym_id="other"),
    lambda ev: ev.update(asset_id="other"),
    lambda ev: ev.update(source_id="other"),
    lambda ev: ev.update(content_hash=hashlib.sha256(b"x").hexdigest()),
    lambda ev: ev.clear(),
])
def test_record_refuses_unbound_or_empty_evidence(mutate):
    ev = _evidence()
    mutate(ev)
    code, _ = _run(FakeStore(), ["record", "--gym", GYM, "--asset", ASSET,
                                 "--content-hash", HASH_A, "--decision", "held",
                                 "--reviewer", "blake", "--yes",
                                 "--evidence", json.dumps(ev)])
    assert code == 2


def test_record_requires_named_reviewer_and_known_decision():
    base = ["record", "--gym", GYM, "--asset", ASSET, "--content-hash", HASH_A,
            "--yes", "--evidence", json.dumps(_evidence())]
    with pytest.raises(SystemExit):      # argparse: required --reviewer missing
        _run(FakeStore(), base + ["--decision", "cleared"])
    with pytest.raises(SystemExit):      # argparse: --decision choices enforce the tri-state
        _run(FakeStore(), base + ["--decision", "bogus", "--reviewer", "blake"])
    store = FakeStore()
    code, _ = _run(store, ["record", "--gym", GYM, "--asset", ASSET,
                           "--content-hash", HASH_A, "--decision", "known_used",
                           "--reviewer", "ops", "--yes",
                           "--evidence-file", _evidence_file()])
    assert code == 0 and store.recorded[0]["decision"] == "known_used"


def _evidence_file(tmp_path=None):
    import tempfile
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(_evidence(), fh)
    fh.close()
    return fh.name


def test_record_surfaces_store_refusal_as_fail_closed():
    store = FakeStore()
    store.fail_with = MediaStoreError(409, "an active receipt already exists")
    code, _ = _run(store, ["record", "--gym", GYM, "--asset", ASSET,
                           "--content-hash", HASH_A, "--decision", "cleared",
                           "--reviewer", "blake", "--yes",
                           "--evidence", json.dumps(_evidence())])
    assert code == 3


def test_record_without_yes_on_non_tty_refuses():
    code, _ = _run(FakeStore(), ["record", "--gym", GYM, "--asset", ASSET,
                                 "--content-hash", HASH_A, "--decision", "cleared",
                                 "--reviewer", "blake",
                                 "--evidence", json.dumps(_evidence())])
    assert code == 2                       # no TTY: must pass --yes explicitly


# ---- revoke ------------------------------------------------------------------

def test_revoke_happy_path_exact_hash_reviewer_reason():
    store = FakeStore()
    code, out = _run(store, ["revoke", "--gym", GYM, "--asset", ASSET,
                             "--content-hash", HASH_A, "--reviewer", "blake",
                             "--reason", "corrected review after drive byte change",
                             "--yes"])
    assert code == 0, out
    (call,) = store.revoked
    assert call == {"gym_id": GYM, "asset_id": ASSET, "content_hash": HASH_A,
                    "reviewer": "blake",
                    "reason": "corrected review after drive byte change"}
    payload = json.loads(out)
    assert payload["revoked"]["confirmed"] is True
    assert payload["revoked"]["reason_submitted"] == \
        "corrected review after drive byte change"
    assert "version" not in payload["revoked"]   # RPC returns bool; none fabricated


def test_revoke_refuses_stale_hash_and_requires_reason():
    store = FakeStore()
    code, _ = _run(store, ["revoke", "--gym", GYM, "--asset", ASSET,
                           "--content-hash", HASH_B, "--reviewer", "blake",
                           "--reason", "x", "--yes"])
    assert code == 2 and store.revoked == []
    code, _ = _run(FakeStore(), ["revoke", "--gym", GYM, "--asset", ASSET,
                                 "--content-hash", HASH_A, "--reviewer", "blake",
                                 "--reason", " ", "--yes"])
    assert code == 2


def test_revoke_surfaces_store_refusal():
    store = FakeStore()
    store.fail_with = MediaStoreError(409, "known_used receipts can never be revoked")
    code, _ = _run(store, ["revoke", "--gym", GYM, "--asset", ASSET,
                           "--content-hash", HASH_A, "--reviewer", "blake",
                           "--reason", "undo", "--yes"])
