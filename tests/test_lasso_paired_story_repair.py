from pathlib import Path
import pytest

from agent.jobs import lasso_paired_story_repair as repair
from test_lasso_paired_story_backfill import FEED_ID, LOGICAL_ID, DAY, STORY_URL, DIGEST

STORY_ID = "f4505c63-6352-44aa-bea5-cf78f0be587d"


def setup(monkeypatch):
    feed = {"id": FEED_ID, "gym_id": "lasso", "account": "instagram",
            "format": "feed", "post_date": DAY, "slot_index": 0,
            "variant_status": "active", "status": "pending", "pillar": "doctrine",
            "caption": "Current exact caption", "image_url": "https://cdn.example/feed.png",
            "scheduled_at": None, "logical_post_id": LOGICAL_ID,
            "media_not_ready_reason": None, "published_at": None, "late_post_id": None}
    story = {"id": STORY_ID, "gym_id": "lasso", "account": "instagram",
             "format": "story", "post_date": DAY, "slot_index": 0,
             "variant_status": "active", "status": "pending", "pillar": "platform",
             "caption": "", "image_url": "https://cdn.example/old-story.png",
             "source_media_url": "https://cdn.example/old-story.png",
             "scheduled_at": None, "logical_post_id": LOGICAL_ID,
             "media_not_ready_reason": "paired_feed_not_ready",
             "published_at": None, "late_post_id": None, "publish_claim_token": None}
    artifact = {"image_sha256": DIGEST, "evidence": {"grade_status": "PASS",
                "image_sha256": DIGEST, "policy_version": "v1", "aspect": "9:16",
                "pixels": "1080x1920", "verified_dimensions": {
                    "width": 1080, "height": 1920, "image_sha256": DIGEST}},
                "source_identity": {"source_id": f"content_calendar:{FEED_ID}:caption",
                "source_hash": repair.hashlib.sha256(feed["caption"].encode()).hexdigest()}}
    monkeypatch.setattr(repair.base, "_feed", lambda *_: feed)
    monkeypatch.setattr(repair.base, "_artifact", lambda *_: artifact)
    monkeypatch.setattr(repair.base, "_one", lambda *_: story)
    monkeypatch.setattr(repair.base, "_active_day", lambda *_: [feed, story])
    monkeypatch.setattr(repair.base, "_scheduled_after_feed", lambda *_: "2026-10-07T07:45:00-04:00")
    item = {"story_id": STORY_ID, "feed_id": FEED_ID,
            "story_image_url": STORY_URL, "story_sha256": DIGEST,
            "artifact_tenant": "lasso_ig", "policy_version": "v1"}
    return object(), item, story, feed, artifact


def test_repair_plan_exact_pending_pair_and_null_schedule(monkeypatch):
    store, item, story, feed, _ = setup(monkeypatch)
    action = repair.plan_one(store, item)
    assert action["expected_story"]["image_url"] == story["image_url"]
    assert action["expected_feed"]["scheduled_at"] is None
    assert action["story_scheduled_at"] == "2026-10-07T07:45:00-04:00"
    assert action["source_hash"] == repair.hashlib.sha256(feed["caption"].encode()).hexdigest()


def test_repair_refuses_claimed_published_and_logical_mismatch(monkeypatch):
    store, item, story, feed, _ = setup(monkeypatch)
    story["publish_claim_token"] = "claimed"
    with pytest.raises(ValueError, match="unclaimed"):
        repair.plan_one(store, item)
    story["publish_claim_token"] = None
    story["status"] = "published"
    with pytest.raises(ValueError, match="unclaimed"):
        repair.plan_one(store, item)
    story["status"] = "pending"
    story["logical_post_id"] = None
    with pytest.raises(ValueError, match="exact feed pair"):
        repair.plan_one(store, item)


def test_repair_refuses_ambiguous_feed_or_stale_artifact(monkeypatch):
    store, item, story, feed, artifact = setup(monkeypatch)
    monkeypatch.setattr(repair.base, "_active_day", lambda *_: [feed, dict(feed, id="other"), story])
    with pytest.raises(ValueError, match="ambiguous"):
        repair.plan_one(store, item)
    monkeypatch.setattr(repair.base, "_active_day", lambda *_: [feed, story])
    feed["caption"] = "changed"
    with pytest.raises(ValueError, match="does not prove"):
        repair.plan_one(store, item)


def test_repair_refuses_slot_with_historical_published_story(monkeypatch):
    store, item, story, feed, _ = setup(monkeypatch)
    historical = {"id": "older", "account": "instagram", "format": "story",
                  "slot_index": 0, "status": "published", "variant_status": "active",
                  "published_at": "2026-10-07T12:00:00Z", "late_post_id": "late"}
    monkeypatch.setattr(repair.base, "_active_day", lambda *_: [feed, story, historical])
    with pytest.raises(ValueError, match="historical Story already published"):
        repair.plan_one(store, item)


def test_repair_refuses_unmeasured_aspect_declaration(monkeypatch):
    store, item, _, _, artifact = setup(monkeypatch)
    artifact["evidence"].pop("verified_dimensions")
    with pytest.raises(ValueError, match="does not prove"):
        repair.plan_one(store, item)


def test_repair_dry_run_and_rpc_cas(monkeypatch):
    store, item, _, _, _ = setup(monkeypatch)
    original_apply = repair.apply_one
    monkeypatch.setattr(repair, "apply_one", lambda *_: pytest.fail("dry-run wrote"))
    assert repair.run({"repairs": [item]}, store)[0]["receipt"]["result"] == "dry_run"
    monkeypatch.setattr(repair, "apply_one", original_apply)
    action = repair.plan_one(store, item)
    class Client:
        def post(self, path, *, headers, json, timeout):
            assert path == repair.RPC
            assert json["p_expected_story"]["pillar"] == "platform"
            assert json["p_expected_feed"]["pillar"] == "doctrine"
            assert json["p_expected_feed"]["scheduled_at"] is None
            return type("R", (), {"status_code": 200, "json": lambda self: {
                "result": "repaired", "id": STORY_ID, "hold_reason": "paired_feed_not_ready"}})()
    class Store:
        def _client(self): return Client()
        def _rest(self, path): return path
        def _headers(self, extra=None): return extra or {}
    assert repair.apply_one(Store(), action)["result"] == "repaired"


def test_repair_accepts_generic_media_holds(monkeypatch):
    for hold in ("caption_changed_needs_new_visual",
                 "cross_date_media_repeat_needs_new_visual"):
        store, item, story, feed, _ = setup(monkeypatch)
        story["media_not_ready_reason"] = hold
        action = repair.plan_one(store, item)
        assert action["expected_story"]["media_not_ready_reason"] == hold


def test_repair_refuses_unrelated_hold_reason(monkeypatch):
    store, item, story, _, _ = setup(monkeypatch)
    story["media_not_ready_reason"] = "unrelated_hold"
    with pytest.raises(ValueError, match="unclaimed"):
        repair.plan_one(store, item)


def test_repair_sql_preserves_rows_and_enforces_claim_source():
    sql = (Path(__file__).resolve().parents[1] / "migrations" /
           "lasso_paired_story_repair_20261005.sql").read_text()
    assert "for update" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "lasso_story_current_source" in sql
    assert "lasso_story_publish_source_guard" in sql
    assert "lasso_managed_paired_stories" in sql
    assert "where m.story_id = old.id" in sql
    assert "publish_claim_token is not null" in sql
    assert "grant execute on function public.repair_lasso_paired_story" in sql
    assert "delete from" not in sql.lower()
    # Both generic media holds are repairable only through this exact-CAS RPC;
    # every other hold reason still conflicts.
    assert "'caption_changed_needs_new_visual'" in sql
    assert "'cross_date_media_repeat_needs_new_visual'" in sql
    gate = sql.split("from public.content_calendar", 1)[0]
    assert "media_not_ready_reason not in" in sql


# ---------------------------------------------------------------------------
# Live guard proof against a disposable PostgreSQL 17 instance (Unix socket
# only, no network). Skips when local PG17 binaries are missing.
# ---------------------------------------------------------------------------

import hashlib as _hashlib
import json as _json
import subprocess as _subprocess
import tempfile as _tempfile

ROOT = Path(__file__).resolve().parents[1]
PGBIN_CANDIDATES = [Path("/opt/homebrew/opt/postgresql@17/bin"),
                    Path("/usr/local/opt/postgresql@17/bin")]
PG_FEED = "81276931-45c8-470b-b658-69a34e4b17a0"
PG_STORY = "f4505c63-6352-44aa-bea5-cf78f0be587d"
PG_LOGICAL = "11111111-1111-4111-8111-111111111111"
PG_CAPTION = "Exact approved source caption"
PG_FEED_URL = "https://example.com/feed.png"
PG_STORY_URL = "https://example.com/reviewed-story.png"
PG_OLD_URL = "https://example.com/old-story.png"
PG_DIGEST = "b" * 64


def _pgbin():
    for cand in PGBIN_CANDIDATES:
        if (cand / "postgres").exists() and (cand / "initdb").exists():
            return cand
    return None


@pytest.fixture(scope="module")
def pg():
    pgbin = _pgbin()
    if pgbin is None:
        pytest.skip("PostgreSQL 17 unavailable")
    def run(*args):
        return _subprocess.run(args, text=True, capture_output=True, check=True)
    with _tempfile.TemporaryDirectory(prefix="lasso-story-repair-") as path:
        root = Path(path)
        run(str(pgbin / "initdb"), "-D", str(root / "data"), "-U", "postgres",
            "--auth=trust", "--no-instructions")
        run(str(pgbin / "pg_ctl"), "-D", str(root / "data"), "-l", str(root / "log"),
            "-o", f"-k {root} -c listen_addresses='' -c fsync=off", "-w", "start")
        def sql(statement):
            return run(str(pgbin / "psql"), "-U", "postgres", "-X", "-q", "-A", "-t",
                       "-v", "ON_ERROR_STOP=1", "-h", str(root), "-d", "postgres",
                       "-c", statement).stdout.strip()
        try:
            sql("create role service_role; create role anon; create role authenticated")
            sql("""create table public.content_calendar (
                id uuid primary key, gym_id text, account text, post_date date,
                pillar text, format text, caption text, image_url text,
                source_media_url text, source_media_asset_id text,
                thumbnail_url text, created_at timestamptz,
                publish_reservation_day date,
                status text, scheduled_at timestamptz, slot_index integer,
                variant_status text, logical_post_id uuid, media_not_ready_reason text,
                published_at timestamptz, late_post_id text, publish_claim_token uuid);
                create table public.echo_infographic_artifacts (
                tenant text, image_url text, image_sha256 text, evidence jsonb,
                source_identity jsonb, created_at timestamptz default now());
                -- Minimal stand-in for the coexistence migration's exception
                -- probe: no historical published Story is excused in this
                -- scratch database.
                create or replace function public.lasso_unrelated_published_story(
                  uuid, uuid) returns boolean language sql stable as
                $$ select false $$;""")
            for name in ("lasso_paired_story_backfill_20261005.sql",
                         "lasso_paired_story_repair_20261005.sql"):
                run(str(pgbin / "psql"), "-U", "postgres", "-X", "-q",
                    "-v", "ON_ERROR_STOP=1", "-h", str(root), "-d", "postgres",
                    "-f", str(ROOT / "migrations" / name))
            yield sql
        finally:
            run(str(pgbin / "pg_ctl"), "-D", str(root / "data"),
                "-m", "immediate", "stop")


def _pg_seed(sql, hold, *, feed_status="pending"):
    sql("delete from public.lasso_managed_paired_stories; "
        "delete from public.content_calendar; "
        "delete from public.echo_infographic_artifacts;")
    published = "null, null" if feed_status == "pending" else \
        "now(), 'feed-receipt'"
    sql(f"""insert into public.content_calendar
      (id,gym_id,account,post_date,pillar,format,caption,image_url,
       source_media_url,status,scheduled_at,slot_index,variant_status,
       logical_post_id,media_not_ready_reason,published_at,late_post_id)
      values
      ('{PG_FEED}','lasso','instagram','2026-10-03','doctrine','feed',
       '{PG_CAPTION}','{PG_FEED_URL}','{PG_FEED_URL}','{feed_status}',
       '2026-10-03T07:30:00-04:00',0,'active','{PG_LOGICAL}',null,{published}),
      ('{PG_STORY}','lasso','instagram','2026-10-03','old','story',
       '','{PG_OLD_URL}','{PG_OLD_URL}','pending',
       '2026-10-03T07:45:00-04:00',0,'active','{PG_LOGICAL}','{hold}',null,null);""")
    source_hash = _hashlib.sha256(PG_CAPTION.encode()).hexdigest()
    evidence = {"grade_status": "PASS", "image_sha256": PG_DIGEST,
                "policy_version": "review-v1", "aspect": "9:16",
                "pixels": "1080x1920", "verified_dimensions": {
                    "width": 1080, "height": 1920, "image_sha256": PG_DIGEST}}
    source = {"source_id": f"content_calendar:{PG_FEED}:caption",
              "source_hash": source_hash}
    sql("insert into public.echo_infographic_artifacts"
        "(tenant,image_url,image_sha256,evidence,source_identity) values "
        f"('lasso_ig','{PG_STORY_URL}','{PG_DIGEST}',"
        f"'{_json.dumps(evidence)}'::jsonb,'{_json.dumps(source)}'::jsonb)")
    expected_story = {"account": "instagram", "post_date": "2026-10-03",
                      "slot_index": 0, "status": "pending",
                      "variant_status": "active", "pillar": "old", "caption": "",
                      "image_url": PG_OLD_URL, "source_media_url": PG_OLD_URL,
                      "media_not_ready_reason": hold,
                      "scheduled_at": "2026-10-03T07:45:00-04:00",
                      "logical_post_id": PG_LOGICAL}
    expected_feed = {"caption": PG_CAPTION, "image_url": PG_FEED_URL,
                     "pillar": "doctrine", "status": feed_status,
                     "scheduled_at": "2026-10-03T07:30:00-04:00",
                     "logical_post_id": PG_LOGICAL}
    return sql(f"""select public.repair_lasso_paired_story(
      '{PG_STORY}','{PG_FEED}',
      '{_json.dumps(expected_story)}'::jsonb,'{_json.dumps(expected_feed)}'::jsonb,
      '{PG_STORY_URL}','{PG_DIGEST}','lasso_ig','{source_hash}','review-v1',
      '2026-10-03T07:45:00-04:00');""")


def test_rpc_rebinds_caption_held_story_and_holds_until_feed_receipt(pg):
    sql = pg
    receipt = _json.loads(_pg_seed(sql, "caption_changed_needs_new_visual"))
    assert receipt["result"] == "repaired" and receipt["id"] == PG_STORY
    assert receipt["hold_reason"] == "paired_feed_not_ready"
    assert sql(f"select image_url || '|' || coalesce(media_not_ready_reason,'') "
               f"from public.content_calendar where id='{PG_STORY}'") == \
        f"{PG_STORY_URL}|paired_feed_not_ready"
    # The registry binding lands in the same transaction.
    assert sql("select feed_id from public.lasso_managed_paired_stories "
               f"where story_id='{PG_STORY}'") == PG_FEED
    # Feed claim preflight accepts the repaired, source-bound held Story.
    assert sql(f"select public.lasso_paired_story_ready_for_feed('{PG_FEED}')") == "t"
    # The publish guard refuses while the repaired Story is still held waiting
    # for the real feed receipt.
    with pytest.raises(_subprocess.CalledProcessError) as blocked:
        sql(f"update public.content_calendar set status='publishing' "
            f"where id='{PG_STORY}'")
    assert "source proof missing or held" in blocked.value.stderr
    # After the real feed receipt, only the release RPC clears the hold.
    sql(f"update public.content_calendar set status='published', "
        f"published_at=now(), late_post_id='feed-receipt' where id='{PG_FEED}'")
    released = _json.loads(sql(
        f"select public.release_lasso_paired_story_hold('{PG_STORY}')"))
    assert released["result"] == "released" and released["id"] == PG_STORY
    sql(f"update public.content_calendar set status='publishing' "
        f"where id='{PG_STORY}'")
    assert sql(f"select status from public.content_calendar "
               f"where id='{PG_STORY}'") == "publishing"


def test_rpc_clears_repeat_hold_when_feed_already_published(pg):
    sql = pg
    sql(f"update public.content_calendar set status='published', "
        f"published_at=now(), late_post_id='receipt' where id='{PG_FEED}'")
    receipt = _json.loads(_pg_seed(
        sql, "cross_date_media_repeat_needs_new_visual", feed_status="published"))
    assert receipt["result"] == "repaired"
    assert receipt["hold_reason"] is None
    assert sql(f"select media_not_ready_reason is null from "
               f"public.content_calendar where id='{PG_STORY}'") == "t"


def test_rpc_refuses_unrelated_hold_and_story_drift(pg):
    sql = pg
    receipt = _json.loads(_pg_seed(sql, "unrelated_hold"))
    assert receipt["result"] == "conflict"
    assert receipt["reason"] == "story_changed_or_claimed"
    assert sql(f"select image_url from public.content_calendar "
               f"where id='{PG_STORY}'") == PG_OLD_URL
    assert sql("select count(*) from public.lasso_managed_paired_stories") == "0"
