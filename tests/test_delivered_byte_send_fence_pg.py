"""Targeted PostgreSQL regressions for the draft exact outbound byte fence.

Run with ``python tests/test_delivered_byte_send_fence_pg.py``. Uses only the
disposable local ``/tmp:5432/echo_exact_byte_test`` database; never production.
The database must be empty except for the minimal calendar fixture.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
DSN = "host=/tmp port=5432 dbname=echo_exact_byte_test user=blakeruff"
OWNER = "exact_byte_owner_20261010"
SERVICE = "service_role"
MIGRATION = ROOT / "migrations/delivered_byte_send_fence_20261010.sql"


def main():
    with psycopg.connect(DSN, autocommit=True) as admin:
        with admin.cursor() as cur:
            cur.execute("drop schema if exists public cascade; create schema public")
            cur.execute(f"drop role if exists {OWNER}")
            cur.execute("""
                create table if not exists public.content_calendar(
                  id uuid primary key, gym_id text, account text, format text,
                  image_url text, thumbnail_url text, post_date date,
                  publish_reservation_day date, publish_claim_token uuid,
                  status text, variant_status text, published_at timestamptz,
                  late_post_id text, logical_post_id uuid, reject_reason text,
                  gbp_location_id text, caption text, approval_digest text
                )
            """)
            # Migration syntax is checked before any scenario is seeded. On a
            # fresh disposable database any SQL error is reported verbatim.
            cur.execute(MIGRATION.read_text())

        def sql(query, args=None, conn=admin):
            with conn.cursor() as cur:
                cur.execute(query, args)
                return cur.fetchall() if cur.description else None

        def seed_authority(gym="gym-a", canonical="tenant-a"):
            sql(f"set role {OWNER}")
            try:
                sql("insert into exact_byte_tenant_20261010 values(%s,%s,'UTC','tenant evidence')",
                    (gym, canonical))
                sql("insert into exact_byte_target_20261010 values(%s,'instagram','feed',"
                    "%s,%s,'target evidence')",
                    (gym, Jsonb({"provider": "late", "platform": "instagram",
                                 "account_id": "acct-a"}), ["image_url"]))
            finally:
                sql("reset role")

        assert sql("select exact_byte_send_context_20261010(%s,%s)",
                   (uuid.uuid4(), uuid.uuid4()))[0][0] == {"enabled": False}

        def seed_calendar(row_id, token, image, post_date, *, gym="gym-a", status="publishing"):
            sql("insert into content_calendar(id,gym_id,account,format,image_url,post_date,"
                "publish_reservation_day,publish_claim_token,status,variant_status) "
                "values(%s,%s,'instagram','feed',%s,%s,(clock_timestamp() at time zone 'UTC')::date,%s,%s,'active')",
                (row_id, gym, image, post_date, token, status))

        def activate():
            corpus = sql("select exact_byte_corpus_digest_20261010()")[0][0]
            history = sql("select exact_byte_historical_snapshot_20261010()")[0][0]
            sql(f"set role {OWNER}")
            try:
                return sql("select exact_byte_activate_20261010(%s,%s,%s,%s,%s,%s,%s)",
                    (uuid.uuid4(), corpus, history, "a" * 40, "sha256:" + "b" * 64,
                     "deployment evidence", "history evidence"))[0][0]
            finally:
                sql("reset role")

        # Tenant and target authority is operator-only, immutable and idempotent
        # only as a whole database transaction; duplicate seed rows fail closed.
        seed_authority()
        assert sql("select count(*) from exact_byte_tenant_20261010")[0][0] == 1
        assert sql("select count(*) from exact_byte_target_20261010")[0][0] == 1
        try:
            sql(f"set role {SERVICE}")
            sql("insert into exact_byte_tenant_20261010 values('x','x','UTC','x')")
        except psycopg.Error:
            admin.rollback()
        else:
            raise AssertionError("service_role seeded exact-byte authority")
        finally:
            sql("reset role")
        try:
            sql(f"set role {SERVICE}")
            sql("select exact_byte_activate_20261010(%s,%s,%s,%s,%s,%s,%s)",
                (uuid.uuid4(), "sha256:" + "0" * 64, "sha256:" + "1" * 64,
                 "a" * 40, "sha256:" + "2" * 64, "service", "service"))
        except psycopg.Error:
            admin.rollback()
        else:
            raise AssertionError("service_role armed the exact-byte gate")
        finally:
            sql("reset role")
        assert sql("select enabled from exact_byte_gate_20261010 where singleton")[0][0] is False
        try:
            sql(f"set role {OWNER}")
            sql("insert into exact_byte_tenant_20261010 values('gym-a','tenant-a','UTC','retry')")
        except psycopg.Error:
            admin.rollback()
        else:
            raise AssertionError("duplicate tenant seed was accepted")
        finally:
            sql("reset role")

        # Every occurrence counts: one exact digest on a different historical
        # date still blocks reuse by an unproven posting identity.
        hist_row, hist_rev, obs_id = uuid.uuid4(), "sha256:" + "c" * 64, uuid.uuid4()
        sql(f"set role {OWNER}")
        try:
            sql("insert into exact_byte_history_coverage_20261010 values(%s,%s,true,false,'census')",
                (hist_row, hist_rev))
            sql("insert into exact_byte_history_observation_20261010(observation_id,calendar_row_id,"
                "row_revision,canonical_tenant,post_date,exact_url,sha256,byte_length,observation_kind,"
                "evidence_ref,observed_at) values(%s,%s,%s,'tenant-a','2026-01-02',%s,%s,10,"
                "'provider_receipt_bytes','receipt',now())",
                (obs_id, hist_row, hist_rev, "https://media.example.test/old.jpg", "sha256:" + "d" * 64))
        finally:
            sql("reset role")
        # Historical digest is intentionally not reused; the candidate is
        # blocked below by inserting a second occurrence matching its digest.
        row_id, token = uuid.uuid4(), uuid.uuid4()
        seed_calendar(row_id, token, "https://media.example.test/new.jpg", "2026-10-10")
        # Include the real GBP native location and failure bookkeeping columns.
        sql(f"set role {OWNER}")
        try:
            sql("insert into exact_byte_target_20261010 values('gym-a','googlebusiness','feed',"
                "%s,%s,'GBP target evidence')",
                (Jsonb({"provider": "zernio", "platform": "googlebusiness",
                        "account_id": "gbp-acct-a", "location_id": "locations/123"}), ["image_url"]))
        finally:
            sql("reset role")
        assert activate() is True
        context = sql("select exact_byte_send_context_20261010(%s,%s)", (row_id, token))[0][0]
        assert context["enabled"] is True
        candidate = Jsonb([{"ordinal": 0, "role": "image", "url": context["images"][0]["url"],
                            "sha256": "sha256:" + "d" * 64, "byte_length": 10}])
        assert sql("select exact_byte_authorize_send_20261010(%s,%s,%s,%s)",
                   (row_id, token, Jsonb(context), candidate))[0][0]["authorized"] is False

        # A current context and token are required. Any row revision or claim
        # token drift returns a hold; a successful grant is single-use.
        fresh_id, fresh_token = uuid.uuid4(), uuid.uuid4()
        seed_calendar(fresh_id, fresh_token, "https://media.example.test/fresh.jpg", "2026-10-10")
        ctx = sql("select exact_byte_send_context_20261010(%s,%s)", (fresh_id, fresh_token))[0][0]
        img = Jsonb([{"ordinal": 0, "role": "image", "url": ctx["images"][0]["url"],
                      "sha256": "sha256:" + "e" * 64, "byte_length": 11}])
        changed = dict(ctx)
        changed["row_revision"] = "sha256:" + "0" * 64
        assert sql("select exact_byte_authorize_send_20261010(%s,%s,%s,%s)",
                   (fresh_id, fresh_token, Jsonb(changed), img))[0][0]["authorized"] is False
        sql("update content_calendar set image_url='https://media.example.test/changed.jpg' where id=%s",
            (fresh_id,))
        assert sql("select exact_byte_authorize_send_20261010(%s,%s,%s,%s)",
                   (fresh_id, fresh_token, Jsonb(ctx), img))[0][0]["authorized"] is False
        ctx = sql("select exact_byte_send_context_20261010(%s,%s)",
                  (fresh_id, fresh_token))[0][0]
        img = Jsonb([{"ordinal": 0, "role": "image", "url": ctx["images"][0]["url"],
                      "sha256": "sha256:" + "e" * 64, "byte_length": 11}])
        try:
            sql("select exact_byte_send_context_20261010(%s,%s)",
                (fresh_id, uuid.uuid4()))
        except psycopg.Error:
            admin.rollback()
        else:
            raise AssertionError("stale claim token produced a send context")

        # Simultaneous grants for one row/token serialize to exactly one winner.
        def claim():
            with psycopg.connect(DSN) as conn:
                with conn.cursor() as cur:
                    cur.execute("select exact_byte_authorize_send_20261010(%s,%s,%s,%s)",
                                (fresh_id, fresh_token, Jsonb(ctx), img))
                    return cur.fetchone()[0]["authorized"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        assert sorted(results) == [False, True], results
        attempt_id = sql("select attempt_id from exact_byte_send_attempt_20261010 where calendar_row_id=%s",
                         (fresh_id,))[0][0]
        # GBP terminal bookkeeping must succeed without opening a location or
        # creative edit path. Every case uses a committed immutable grant.
        def must_reject(query, args, label):
            try:
                sql(query, args)
            except psycopg.Error:
                admin.rollback()
            else:
                raise AssertionError(label)

        def gbp_claim(digest, location=None):
            rid, claim_token = uuid.uuid4(), uuid.uuid4()
            seed_calendar(rid, claim_token, f"https://media.example.test/{rid}.jpg", "2026-10-10")
            sql("update content_calendar set account='googlebusiness',gbp_location_id=%s,"
                "caption='approved caption',approval_digest='approved proof' where id=%s", (location, rid))
            context = sql("select exact_byte_send_context_20261010(%s,%s)", (rid, claim_token))[0][0]
            return rid, claim_token, context, Jsonb([{
                "ordinal": 0, "role": "image", "url": context["images"][0]["url"],
                "sha256": "sha256:" + digest * 64, "byte_length": 17}])

        gbp_id, gbp_token, gbp_ctx, gbp_img = gbp_claim("1")
        assert "reject_reason" not in gbp_ctx["row_snapshot"]
        assert gbp_ctx["row_snapshot"]["gbp_location_id"] is None
        # Changed pre-send location cannot reuse the fetched context/grant.
        sql("update content_calendar set gbp_location_id='locations/evil' where id=%s", (gbp_id,))
        must_reject("select exact_byte_authorize_send_20261010(%s,%s,%s,%s)",
                    (gbp_id, gbp_token, Jsonb(gbp_ctx), gbp_img), "tampered GBP location authorized")
        sql("update content_calendar set gbp_location_id='locations/123' where id=%s", (gbp_id,))
        assert sql("select exact_byte_authorize_send_20261010(%s,%s,%s,%s)",
                   (gbp_id, gbp_token, Jsonb(gbp_ctx), gbp_img))[0][0]["authorized"] is False
        sql("update content_calendar set gbp_location_id=null where id=%s", (gbp_id,))
        assert sql("select exact_byte_authorize_send_20261010(%s,%s,%s,%s)",
                   (gbp_id, gbp_token, Jsonb(gbp_ctx), gbp_img))[0][0]["authorized"] is True
        must_reject("update content_calendar set gbp_location_id='locations/123' where id=%s",
                    (gbp_id,), "location changed before terminal publication")
        must_reject("update content_calendar set status='published',publish_claim_token=null,"
                    "gbp_location_id='locations/evil' where id=%s", (gbp_id,), "wrong terminal GBP location")
        must_reject("update content_calendar set status='published',gbp_location_id='locations/123' where id=%s",
                    (gbp_id,), "terminal GBP stamp retained live claim")
        for field, value in (("caption", "changed"), ("approval_digest", "changed"),
                             ("image_url", "https://media.example.test/evil.jpg")):
            must_reject(f"update content_calendar set status='published',publish_claim_token=null,"
                        f"gbp_location_id='locations/123',{field}=%s where id=%s", (value, gbp_id),
                        f"terminal stamp changed approved {field}")
        sql("update content_calendar set status='published',publish_claim_token=null,"
            "published_at=now(),late_post_id='gbp-provider-post',gbp_location_id='locations/123' "
            "where id=%s and status='publishing' and publish_claim_token=%s", (gbp_id, gbp_token))
        assert sql("select status,gbp_location_id from content_calendar where id=%s", (gbp_id,))[0] == (
            "published", "locations/123")
        assert sql("select exact_byte_history_complete_20261010()")[0][0] is True
        must_reject("update content_calendar set gbp_location_id='locations/evil' where id=%s",
                    (gbp_id,), "published native location was mutable")

        failed_id, failed_token, failed_ctx, failed_img = gbp_claim("2", "locations/123")
        assert sql("select exact_byte_authorize_send_20261010(%s,%s,%s,%s)",
                   (failed_id, failed_token, Jsonb(failed_ctx), failed_img))[0][0]["authorized"] is True
        must_reject("update content_calendar set status='failed',publish_claim_token=null,"
                    "reject_reason='definite no send',gbp_location_id='locations/evil' where id=%s",
                    (failed_id,), "failure bookkeeping changed native location")
        sql("update content_calendar set status='failed',publish_claim_token=null,"
            "reject_reason='definite no send' where id=%s and status='publishing' "
            "and publish_claim_token=%s", (failed_id, failed_token))
        assert sql("select status,reject_reason from content_calendar where id=%s", (failed_id,))[0] == (
            "failed", "definite no send")
        assert sql("select exact_byte_history_complete_20261010()")[0][0] is True
        # A real historical row still needs reviewed coverage even though
        # reject_reason is now excluded from the content revision.
        historical_id = uuid.uuid4()
        seed_calendar(historical_id, None, "https://media.example.test/uncovered.jpg", "2026-01-01", status="published")
        assert sql("select exact_byte_history_complete_20261010()")[0][0] is False
        sql("delete from content_calendar where id=%s", (historical_id,))
        assert sql("select exact_byte_history_complete_20261010()")[0][0] is True

        # The attempted calendar row cannot be deleted, even after OFF.
        assert sql("select exact_byte_disable_20261010()")[0][0] is True
        try:
            sql("delete from content_calendar where id=%s", (fresh_id,))
        except psycopg.Error:
            admin.rollback()
        else:
            raise AssertionError("calendar deletion erased an attempted row")
        assert sql("select count(*) from exact_byte_send_attempt_20261010 where attempt_id=%s",
                   (attempt_id,))[0][0] == 1
        assert sql("select exact_byte_send_context_20261010(%s,%s)",
                   (fresh_id, fresh_token))[0][0] == {"enabled": False}
        print("PASS: exact-byte PostgreSQL regression scenarios")


if __name__ == "__main__":
    main()
