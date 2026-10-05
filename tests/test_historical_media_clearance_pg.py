"""Disposable-PostgreSQL contract test for DRAFT_historical_media_clearance_20261003.sql.

Complements the offline selector/verifier tests (tests/test_historical_media_clearance.py)
with the database half of the contract: schema, RPC behavior, guard triggers and
the ACL surface. Runs against a throwaway initdb cluster (tests/pg_fixture.py) —
Unix socket inside pytest's tmp dir, never a live plane. Every negative case
asserts the migration FAILS CLOSED (errcode 23514 / permission denied / FK or
CHECK violation); absence of a receipt or of first_indexed_at must never make
an asset available.
"""

import hashlib
import json

import pytest

MD5_A = "0123456789abcdef0123456789abcdef"
MD5_B = "fedcba9876543210fedcba9876543210"

pytestmark = pytest.mark.usefixtures("hmc_db")


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def call_record(gym, asset, chash, decision, reviewer, evidence):
    return ("select public.record_historical_media_clearance("
            f"{q(gym)},{q(asset)},{q(chash)},{q(decision)},{q(reviewer)},"
            f"{q(json.dumps(evidence))}::jsonb)")


def call_revoke(gym, asset, chash, reviewer, reason):
    return ("select public.revoke_historical_media_clearance("
            f"{q(gym)},{q(asset)},{q(chash)},{q(reviewer)},{q(reason)})")


def cleared_evidence(gym="gymx", asset="asset1", chash=MD5_A, source="src1",
                     drop=(), blank=()):
    ev = {"gym_id": gym, "asset_id": asset, "source_id": source,
          "content_hash": chash, "method": "drive+calendar usage audit",
          "observed_at": "2026-09-30T00:00:00Z",
          "result": "no usage found in any channel",
          "proof_ref": "s3://echo-clearance/%s/%s.pdf" % (gym, asset)}
    for key in drop:
        ev.pop(key, None)
    for key in blank:
        ev[key] = "   "
    return ev


# Truthful caveat: a well-formed evidence object is required for a 'cleared'
# receipt, but the evidence TEXT is the reviewer's attestation — it is not
# cryptographic proof that the scene was actually unused; only method,
# observation time, stated result and a proof/​assertion reference are
# structurally enforced. These tests assert the structural requirement only.
CLEARED_EVIDENCE_FIELDS = ("method", "observed_at", "result", "proof_ref")


def refused(rc_err):
    rc, err = rc_err
    assert rc != 0, "expected the migration to refuse, but it succeeded"
    return err


def test_schema_contract(hmc_db):
    cols = hmc_db.sql(
        "select string_agg(column_name, ',' order by ordinal_position) "
        "from information_schema.columns where table_schema='public' "
        "and table_name='media_historical_clearance'")
    assert cols == ("gym_id,asset_id,version,source_id,content_hash,decision,"
                    "reviewer,evidence,recorded_at,revoked_at,revoked_by,"
                    "revocation_reason"), cols
    assert hmc_db.sql(
        "select count(*) from pg_constraint where conname="
        "'media_historical_clearance_source_tenant_fk'") == "1"
    assert hmc_db.sql(
        "select count(*) from pg_indexes where indexname='media_source_id_gym_key'") == "1"
    assert hmc_db.sql(
        "select count(*) from pg_trigger where tgname in "
        "('media_asset_first_indexed_at_guard','media_source_gym_id_guard',"
        "'media_historical_clearance_guard')") == "3"
    # RLS armed on the receipt table; no policy may ever grant anon writes.
    assert hmc_db.sql(
        "select relrowsecurity from pg_class where relname='media_historical_clearance'") == "t"
    assert hmc_db.sql(
        "select count(*) from pg_policies where tablename='media_historical_clearance'") == "0"
    # first_indexed_at declared (nullable on purpose: NULL = fail closed).
    assert hmc_db.sql(
        "select is_nullable from information_schema.columns where table_name='media_asset' "
        "and column_name='first_indexed_at'") == "YES"


def test_first_indexed_at_write_once(hmc_db):
    hmc_db.sql("insert into public.media_asset"
               "(id,source_id,gym_id,kind,title,content_hash,indexed_at,first_indexed_at) "
               "values('stamped','src1','gymx','photo','stamped',"
               "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',now(),now())")
    # Re-stamping or clearing the stamp is refused; other columns stay mutable.
    assert "23514" in refused(hmc_db.sql_rc(
        "update public.media_asset set first_indexed_at=now()+interval '1 day' where id='stamped'"))
    assert "23514" in refused(hmc_db.sql_rc(
        "update public.media_asset set first_indexed_at=null where id='stamped'"))
    hmc_db.sql("update public.media_asset set title='renamed' where id='asset1'")
    # Legacy rows keep NULL first_indexed_at (no backfill, runtime fails closed).
    assert hmc_db.sql(
        "select count(*) from public.media_asset where first_indexed_at is null") == "3"


def test_media_source_gym_id_immutable(hmc_db):
    # Re-pointing a bound source to another gym is refused (tenant binding).
    assert "23514" in refused(hmc_db.sql_rc(
        "update public.media_source set gym_id='gymz' where id='src1'"))
    # Every other legitimate source update keeps working (base schema binds
    # gym_id NOT NULL, so there is no NULL-bind path to exercise).
    hmc_db.sql("insert into public.media_source(id,gym_id,folder_id,connected_at) "
               "values('src3','gymx','folder3',now())")
    hmc_db.sql("update public.media_source set folder_name='rotated' where id='src1'")
    hmc_db.sql("update public.media_source set active=false, sync_mode='selected' where id='src1'")


def test_record_cleared_happy_path(hmc_db):
    out = hmc_db.sql(call_record("gymx", "asset1", MD5_A, "cleared", " blake ",
                                 cleared_evidence()))
    row = json.loads(out)
    assert row["version"] == 1
    assert row["availability_changed"] is False
    assert row["source_id"] == "src1"
    assert row["reviewer"] == "blake"
    assert hmc_db.sql(
        "select decision from public.media_historical_clearance "
        "where gym_id='gymx' and asset_id='asset1' and version=1") == "cleared"


def test_record_fails_closed_on_identity_and_evidence(hmc_db):
    cases = [
        # Asset of another gym.
        call_record("gymy", "asset1", MD5_A, "cleared", "blake", cleared_evidence()),
        call_record("gymy", "asset1", MD5_A, "cleared", "blake",
                    cleared_evidence(gym="gymy", asset="asset1", source="src1")),
        # Unknown asset.
        call_record("gymx", "assetZ", MD5_A, "cleared", "blake",
                    cleared_evidence(asset="assetZ")),
        # Stale hash since review.
        call_record("gymx", "asset1", MD5_B, "cleared", "blake",
                    cleared_evidence(chash=MD5_B)),
        # Evidence that does not bind the live row's source_id.
        call_record("gymx", "asset1", MD5_A, "cleared", "blake",
                    cleared_evidence(source="src2")),
        # Each required 'cleared' evidence field, OMITTED separately.
        *[call_record("gymx", "asset1", MD5_A, "cleared", "blake",
                      cleared_evidence(drop=(field,)))
          for field in CLEARED_EVIDENCE_FIELDS],
        # Each required field present but blank/whitespace, separately.
        *[call_record("gymx", "asset1", MD5_A, "cleared", "blake",
                      cleared_evidence(blank=(field,)))
          for field in CLEARED_EVIDENCE_FIELDS],
        # Evidence content_hash not bound to the requested hash.
        call_record("gymx", "asset1", MD5_A, "cleared", "blake",
                    cleared_evidence(chash="f" + MD5_A[1:])),
        # Blank reviewer / bad decision / non-hex hash.
        call_record("gymx", "asset1", MD5_A, "cleared", "  ", cleared_evidence()),
        call_record("gymx", "asset1", MD5_A, "approved", "blake", cleared_evidence()),
        call_record("gymx", "asset1", "zz" + MD5_A[2:], "held", "blake",
                    {"gym_id": "gymx", "asset_id": "asset1", "source_id": "src1",
                     "content_hash": "zz" + MD5_A[2:]}),
        # Asset whose source_id string points at another tenant's source row.
        call_record("gymx", "assetX", "a" * 32, "cleared", "blake",
                    cleared_evidence(asset="assetX", source="src2")),
    ]
    for index, stmt in enumerate(cases):
        rc, err = hmc_db.sql_rc(stmt)
        assert rc != 0, f"case {index} unexpectedly accepted"
        assert "23514" in err, f"case {index} returned {err}"


def test_active_receipt_uniqueness_and_revocation_flow(hmc_db):
    hmc_db.sql(call_record("gymx", "asset1", MD5_A, "held", "blake",
                           cleared_evidence()))
    # A second ACTIVE receipt is refused until explicit revocation.
    assert "23514" in refused(hmc_db.sql_rc(
        call_record("gymx", "asset1", MD5_A, "cleared", "sam", cleared_evidence())))
    # Revocation compare-and-sets the CURRENT hash.
    assert "23514" in refused(hmc_db.sql_rc(
        call_revoke("gymx", "asset1", MD5_B, "blake", "wrong hash")))
    # Revocation needs a named reviewer and a reason.
    assert "23514" in refused(hmc_db.sql_rc(
        call_revoke("gymx", "asset1", MD5_A, "  ", "reason")))
    assert "23514" in refused(hmc_db.sql_rc(
        call_revoke("gymx", "asset1", MD5_A, "blake", " ")))
    assert hmc_db.sql(call_revoke("gymx", "asset1", MD5_A, "blake",
                                  "corrected review pending")) == "t"
    # Only THIS version is superseded; history stays visible.
    assert hmc_db.sql(
        "select count(*) from public.media_historical_clearance "
        "where revoked_at is not null") == "1"
    # After revocation a NEW version at the CURRENT hash is recordable.
    out = json.loads(hmc_db.sql(call_record("gymx", "asset1", MD5_A, "cleared",
                                            "sam", cleared_evidence())))
    assert out["version"] == 2
    # Revoking with no active receipt fails closed.
    hmc_db.sql(call_revoke("gymx", "asset1", MD5_A, "blake", "redo"))
    assert "23514" in refused(hmc_db.sql_rc(
        call_revoke("gymx", "asset1", MD5_A, "blake", "nothing active")))


def test_partial_unique_index_enforced_at_database_level(hmc_db):
    # Regression for the P2 gap: the single-active-receipt rule must hold even
    # for direct DML that bypasses the RPC pre-check, and a concurrent
    # check-then-insert race cannot produce two active receipts.
    assert hmc_db.sql(
        "select count(*) from pg_indexes where indexname="
        "'media_historical_clearance_one_active'") == "1"

    def ins(version, chash):
        ev = json.dumps({"gym_id": "gymx", "asset_id": "asset1",
                         "source_id": "src1", "content_hash": chash})
        return ("insert into public.media_historical_clearance "
                "(gym_id,asset_id,version,source_id,content_hash,decision,"
                "reviewer,evidence) values (" + q("gymx") + "," + q("asset1")
                + "," + str(version) + "," + q("src1") + "," + q(chash)
                + ",'held','blake'," + q(ev) + "::jsonb)")

    hmc_db.sql(ins(1, MD5_A))
    # A second ACTIVE receipt (different version) is rejected by the partial
    # unique index itself — not by RPC logic.
    err = refused(hmc_db.sql_rc(ins(2, MD5_B)))
    assert "duplicate key" in err.lower(), err
    assert "media_historical_clearance_one_active" in err, err
    # Legitimate one-way revocation frees the slot...
    hmc_db.sql("update public.media_historical_clearance set revoked_at=now(),"
               "revoked_by='blake',revocation_reason='corrected review pending' "
               "where gym_id='gymx' and asset_id='asset1' and version=1")
    # ...and recording the next version for the same asset still works.
    hmc_db.sql(ins(2, MD5_A))
    assert hmc_db.sql(
        "select count(*) from public.media_historical_clearance "
        "where gym_id='gymx' and asset_id='asset1'") == "2"


def test_known_used_is_permanent(hmc_db):
    hmc_db.sql(call_record("gymx", "asset1", MD5_A, "known_used", "blake",
                           cleared_evidence()))
    # No further receipt of any kind, ever.
    assert "23514" in refused(hmc_db.sql_rc(
        call_record("gymx", "asset1", MD5_A, "cleared", "sam", cleared_evidence())))
    assert "23514" in refused(hmc_db.sql_rc(
        call_record("gymx", "asset1", MD5_A, "held", "sam", cleared_evidence())))
    # known_used cannot be revoked, mutated or deleted, even directly.
    assert "23514" in refused(hmc_db.sql_rc(
        call_revoke("gymx", "asset1", MD5_A, "blake", "mistake")))
    assert "23514" in refused(hmc_db.sql_rc(
        "update public.media_historical_clearance set revoked_at=now(),"
        "revoked_by='x',revocation_reason='y' "
        "where gym_id='gymx' and asset_id='asset1'"))
    assert "23514" in refused(hmc_db.sql_rc(
        "delete from public.media_historical_clearance where asset_id='asset1'"))


def test_direct_dml_guard_one_way_revocation_only(hmc_db):
    hmc_db.sql(call_record("gymx", "asset1", MD5_A, "cleared", "blake",
                           cleared_evidence()))
    # Identity columns can never change; un-revoke is impossible.
    assert "23514" in refused(hmc_db.sql_rc(
        "update public.media_historical_clearance set decision='held' "
        "where asset_id='asset1'"))
    hmc_db.sql("update public.media_historical_clearance set revoked_at=now(),"
               "revoked_by='blake',revocation_reason='direct ok' where asset_id='asset1'")
    assert "23514" in refused(hmc_db.sql_rc(
        "update public.media_historical_clearance set revoked_at=null,"
        "revoked_by=null,revocation_reason=null where asset_id='asset1'"))
    assert "23514" in refused(hmc_db.sql_rc(
        "delete from public.media_historical_clearance where asset_id='asset1'"))
    # Durable tenant FK: a receipt cannot bind a source id that exists only
    # under a DIFFERENT gym.
    err = refused(hmc_db.sql_rc(
        "insert into public.media_historical_clearance "
        "(gym_id,asset_id,version,source_id,content_hash,decision,reviewer,evidence) "
        "values('gymx','asset9',1,'src2','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','held','eve',"
        "'{\"gym_id\":\"gymx\",\"asset_id\":\"asset9\",\"source_id\":\"src2\","
        "\"content_hash\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\"}'::jsonb)"))
    assert "foreign key" in err.lower()
    # Evidence CHECK binding is enforced at the table level too.
    assert "23514" in refused(hmc_db.sql_rc(
        "insert into public.media_historical_clearance "
        "(gym_id,asset_id,version,source_id,content_hash,decision,reviewer,evidence) "
        "values('gymx','asset1',9,'src1','0123456789abcdef0123456789abcdef','held','eve',"
        "'{\"gym_id\":\"gymx\",\"asset_id\":\"WRONG\",\"source_id\":\"src1\","
        "\"content_hash\":\"0123456789abcdef0123456789abcdef\"}'::jsonb)"))


def test_acl_contract(hmc_db):
    rec = call_record("gymx", "asset1", MD5_A, "cleared", "blake",
                      cleared_evidence())
    rev = call_revoke("gymx", "asset1", MD5_A, "blake", "r")
    # anon/authenticated: no RPC execution, no table reads.
    for role in ("anon", "authenticated"):
        err = refused(hmc_db.sql_rc(f"set local role {role}; {rec}"))
        assert "permission denied" in err.lower()
        err = refused(hmc_db.sql_rc(
            f"set local role {role}; select * from public.media_historical_clearance"))
        assert "permission denied" in err.lower()
    # service_role: RPCs executable, receipts readable; direct DML stays owner-only.
    hmc_db.sql(f"set local role service_role; {rec}")
    assert hmc_db.sql(
        "set local role service_role; select count(*) from public.media_historical_clearance") == "1"
    err = refused(hmc_db.sql_rc(
        "set local role service_role; "
        "insert into public.media_historical_clearance "
        "(gym_id,asset_id,version,source_id,content_hash,decision,reviewer,evidence) "
        "values('gymx','asset1',5,'src1','0123456789abcdef0123456789abcdef','held','eve',"
        "'{\"gym_id\":\"gymx\",\"asset_id\":\"asset1\",\"source_id\":\"src1\","
        "\"content_hash\":\"0123456789abcdef0123456789abcdef\"}'::jsonb)"))
    assert "permission denied" in err.lower()
    assert hmc_db.sql(f"set local role service_role; {rev}") == "t"
    # Guard trigger functions are not executable by any runtime role.
    for fn in ("media_historical_clearance_guard", "media_asset_first_indexed_at_guard",
               "media_source_gym_id_guard"):
        for role in ("anon", "authenticated", "service_role"):
            err = refused(hmc_db.sql_rc(
                f"set local role {role}; select public.{fn}()"))
            assert "permission denied" in err.lower() or "does not exist" in err.lower() \
                or "undefined" in err.lower(), (fn, role, err)
