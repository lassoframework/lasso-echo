"""Disposable PostgreSQL proof for the exact legacy Story exceptions."""

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PGBIN = Path("/opt/homebrew/opt/postgresql@17/bin")
FEED = "81276931-45c8-470b-b658-69a34e4b17a0"
HISTORICAL = "af677ffb-7834-455e-852c-b865a5155ac4"
NEW_STORY = "12121212-1212-4212-8212-121212121212"
CAPTION = "Exact approved platform caption"
FEED_URL = "https://example.com/feed.png"
STORY_URL = "https://example.com/reviewed-story.png"
OLD_URL = "https://example.com/legacy-story.png"
DIGEST = "a" * 64

# Third pair verified by receipt published-feed-story-gap-2c6e3489.json.
FEED3 = "2c6e3489-d965-5abc-b11b-7105047353cf"
HISTORICAL3 = "810b7a15-d187-4f73-bad9-3fab15954cc0"
NEW_STORY3 = "34343434-3434-4434-8434-343434343434"
CAPTION3 = ("Spend more time with the people in front of you.\n\n"
            "LASSO plans your content, creates the posts, and keeps the "
            "calendar moving. See the plan in one place.\n\n"
            "Save this for later.")
FEED3_URL = ("https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/"
             "echo/lasso/91d16553122dcf21/direct_2c6e3489.png")
STORY3_URL = "https://example.com/reviewed-story-oct4.png"
OLD3_URL = ("https://pub-c4a3291534e146e2977e8cd3d4e7343f.r2.dev/"
            "echo/lasso_ig/dcad077060d7579d/2026-10-04_810b7a15.png")
CAPTION3_SHA256 = ("68df52b993d8ee3af07ac939a7ad4775965fd412540c07d430f04e6c77aae7da")


def _run(*args):
    return subprocess.run(args, text=True, capture_output=True, check=True)


@pytest.fixture(scope="module")
def pg():
    if not (PGBIN / "initdb").exists():
        pytest.skip("PostgreSQL 17 unavailable")
    with tempfile.TemporaryDirectory(prefix="lasso-story-coexist-") as path:
        root = Path(path)
        _run(str(PGBIN / "initdb"), "-D", str(root / "data"), "-U", "postgres",
             "--auth=trust", "--no-instructions")
        _run(str(PGBIN / "pg_ctl"), "-D", str(root / "data"), "-l", str(root / "log"),
             "-o", f"-k {root} -c listen_addresses='' -c fsync=off", "-w", "start")
        def sql(statement):
            return _run(str(PGBIN / "psql"), "-U", "postgres", "-X", "-q", "-A", "-t",
                        "-v", "ON_ERROR_STOP=1", "-h", str(root), "-d", "postgres",
                        "-c", statement).stdout.strip()
        try:
            sql("create role service_role; create role anon; create role authenticated")
            sql("""create table public.content_calendar (
                id uuid primary key, gym_id text, account text, post_date date,
                pillar text, format text, caption text, image_url text,
                source_media_url text, source_media_asset_id text,
                status text, scheduled_at timestamptz, slot_index integer,
                variant_status text, logical_post_id uuid, media_not_ready_reason text,
                published_at timestamptz, late_post_id text, publish_claim_token uuid,
                publish_reservation_day date);
                create table public.echo_infographic_artifacts (
                tenant text, image_url text, image_sha256 text, evidence jsonb,
                source_identity jsonb);""")
            for name in ("lasso_paired_story_backfill_20261005.sql",
                         "lasso_paired_story_repair_20261005.sql",
                         "lasso_paired_story_published_legacy_coexistence_20261005.sql"):
                _run(str(PGBIN / "psql"), "-U", "postgres", "-X", "-q",
                     "-v", "ON_ERROR_STOP=1", "-h", str(root), "-d", "postgres",
                     "-f", str(ROOT / "migrations" / name))
            yield sql
        finally:
            _run(str(PGBIN / "pg_ctl"), "-D", str(root / "data"),
                 "-m", "immediate", "stop")


def _setup(sql):
    sql(f"""insert into public.content_calendar
      (id,gym_id,account,post_date,pillar,format,caption,image_url,
       source_media_url,status,scheduled_at,slot_index,variant_status,
       published_at,late_post_id)
      values
      ('{FEED}','lasso','instagram','2026-10-02','platform','feed',
       '{CAPTION}','{FEED_URL}','{FEED_URL}','pending',
       '2026-10-02T07:30:00-04:00',1,'active',null,null),
      ('{HISTORICAL}','lasso','instagram','2026-10-02','website','story',
       '','{OLD_URL}','{OLD_URL}','published',
       '2026-10-02T07:45:00-04:00',1,'active',now(),'legacy-receipt');""")
    source_hash = hashlib.sha256(CAPTION.encode()).hexdigest()
    evidence = {"grade_status": "PASS", "image_sha256": DIGEST,
                "policy_version": "review-v1", "aspect": "9:16",
                "pixels": "1080x1920", "verified_dimensions": {
                    "width": 1080, "height": 1920, "image_sha256": DIGEST}}
    source = {"source_id": f"content_calendar:{FEED}:caption",
              "source_hash": source_hash}
    sql("insert into public.echo_infographic_artifacts values "
        f"('lasso_ig','{STORY_URL}','{DIGEST}',"
        f"'{json.dumps(evidence)}'::jsonb,'{json.dumps(source)}'::jsonb)")
    return source_hash


def test_unrelated_published_story_allows_exact_managed_pair(pg):
    sql = pg
    source_hash = _setup(sql)
    assert sql(f"select public.lasso_unrelated_published_story('{HISTORICAL}','{FEED}')") == "t"
    result = sql(f"""select public.stage_lasso_paired_story(
      '{FEED}','instagram','2026-10-02',1,'pending','{CAPTION}','{FEED_URL}',
      '2026-10-02T07:30:00-04:00',null,'{NEW_STORY}','{STORY_URL}',
      '{DIGEST}','lasso_ig','{source_hash}','review-v1',
      '2026-10-02T07:45:00-04:00');""")
    assert json.loads(result)["result"] == "inserted"
    assert sql(f"select public.lasso_paired_story_ready_for_feed('{FEED}')") == "t"
    assert sql("select count(*) from public.content_calendar where format='story'") == "2"
    # If the historical Story acquires an exact source link, the exception
    # closes even though its pillar remains different.
    pg("insert into public.echo_infographic_artifacts values "
       f"('lasso','{OLD_URL}','{DIGEST}','{{}}'::jsonb,"
       f"'{{\"source_feed_id\":\"{FEED}\"}}'::jsonb)")
    assert sql(f"select public.lasso_unrelated_published_story('{HISTORICAL}','{FEED}')") == "f"
    assert sql(f"select public.lasso_paired_story_ready_for_feed('{FEED}')") == "f"
    pg(f"delete from public.echo_infographic_artifacts where image_url='{OLD_URL}'")
    pg(f"update public.content_calendar set status='published', published_at=now(), "
       f"late_post_id='feed-receipt' where id='{FEED}'")
    try:
        pg(f"update public.content_calendar set status='publishing' where id='{NEW_STORY}'")
    except subprocess.CalledProcessError as exc:
        pytest.fail(exc.stderr)
    assert sql(f"select status from public.content_calendar where id='{NEW_STORY}'") == "publishing"


def test_unlisted_or_ambiguous_historical_pair_is_refused(pg):
    sql = pg
    sql(f"update public.content_calendar set status='pending', published_at=null, "
        f"late_post_id=null where id='{FEED}'")
    assert sql(f"select public.lasso_unrelated_published_story('{HISTORICAL}',"
               "'00000000-0000-4000-8000-000000000001')") == "f"
    sql(f"delete from public.echo_infographic_artifacts where image_url='{OLD_URL}'")
    sql(f"update public.content_calendar set pillar='platform' where id='{HISTORICAL}'")
    assert sql(f"select public.lasso_unrelated_published_story('{HISTORICAL}','{FEED}')") == "f"


def _setup_third_pair(sql):
    assert hashlib.sha256(CAPTION3.encode()).hexdigest() == CAPTION3_SHA256
    sql(f"""insert into public.content_calendar
      (id,gym_id,account,post_date,pillar,format,caption,image_url,
       source_media_url,status,scheduled_at,slot_index,variant_status,
       published_at,late_post_id)
      values
      ('{FEED3}','lasso','instagram','2026-10-04','doctrine','feed',
       '{CAPTION3}','{FEED3_URL}','{FEED3_URL}','published',
       '2026-10-04T07:30:00-04:00',0,'active',now(),'feed-receipt-3'),
      ('{HISTORICAL3}','lasso','instagram','2026-10-04','doctrine','story',
       '','{OLD3_URL}','{OLD3_URL}','published',
       '2026-10-04T07:45:00-04:00',0,'active',now(),'legacy-receipt-3');""")
    evidence = {"grade_status": "PASS", "image_sha256": DIGEST,
                "policy_version": "review-v1", "aspect": "9:16",
                "pixels": "1080x1920", "verified_dimensions": {
                    "width": 1080, "height": 1920, "image_sha256": DIGEST}}
    source = {"source_id": f"content_calendar:{FEED3}:caption",
              "source_hash": CAPTION3_SHA256}
    sql("insert into public.echo_infographic_artifacts values "
        f"('lasso_ig','{STORY3_URL}','{DIGEST}',"
        f"'{json.dumps(evidence)}'::jsonb,'{json.dumps(source)}'::jsonb)")


def test_third_pair_stages_and_claims_managed_story(pg):
    sql = pg
    _setup_third_pair(sql)
    assert sql(f"select public.lasso_unrelated_published_story("
               f"'{HISTORICAL3}','{FEED3}')") == "t"
    result = sql(f"""select public.stage_lasso_paired_story(
      '{FEED3}','instagram','2026-10-04',0,'published','{CAPTION3}','{FEED3_URL}',
      '2026-10-04T07:30:00-04:00',null,'{NEW_STORY3}','{STORY3_URL}',
      '{DIGEST}','lasso_ig','{CAPTION3_SHA256}','review-v1',
      '2026-10-04T07:45:00-04:00');""")
    assert json.loads(result)["result"] == "inserted"
    # ready_for_feed governs only pending/approved feeds; for an already
    # published feed the meaningful proof is staging plus the claim below.
    assert sql("select count(*) from public.content_calendar where format='story' "
               f"and post_date='2026-10-04'") == "2"
    # The historical row is preserved byte-for-byte.
    assert sql(f"select image_url from public.content_calendar "
               f"where id='{HISTORICAL3}'") == OLD3_URL
    assert sql(f"select status || '|' || late_post_id from public.content_calendar "
               f"where id='{HISTORICAL3}'") == "published|legacy-receipt-3"
    # Claim guard still governs the new managed Story.
    try:
        pg(f"update public.content_calendar set status='publishing' where id='{NEW_STORY3}'")
    except subprocess.CalledProcessError as exc:
        pytest.fail(exc.stderr)


def test_third_pair_pending_feed_refuses(pg):
    sql = pg
    # The third feed was independently observed published; a pending or
    # approved third feed must refuse the exception.
    sql(f"update public.content_calendar set status='pending', published_at=null, "
        f"late_post_id=null where id='{FEED3}'")
    assert sql(f"select public.lasso_unrelated_published_story("
               f"'{HISTORICAL3}','{FEED3}')") == "f"
    sql(f"update public.content_calendar set status='approved' where id='{FEED3}'")
    assert sql(f"select public.lasso_unrelated_published_story("
               f"'{HISTORICAL3}','{FEED3}')") == "f"
    # Restore the observed published state; the exception reopens.
    sql(f"update public.content_calendar set status='published', published_at=now(), "
        f"late_post_id='feed-receipt-3' where id='{FEED3}'")
    assert sql(f"select public.lasso_unrelated_published_story("
               f"'{HISTORICAL3}','{FEED3}')") == "t"


def test_third_pair_changed_or_unlisted_evidence_refuses(pg):
    sql = pg
    # Unlisted feed for the same historical Story refuses.
    assert sql(f"select public.lasso_unrelated_published_story('{HISTORICAL3}',"
               "'00000000-0000-4000-8000-000000000003')") == "f"
    # Changed legacy image URL refuses.
    sql(f"update public.content_calendar set image_url='{OLD3_URL}.moved', "
        f"source_media_url='{OLD3_URL}.moved' where id='{HISTORICAL3}'")
    assert sql(f"select public.lasso_unrelated_published_story("
               f"'{HISTORICAL3}','{FEED3}')") == "f"
    sql(f"update public.content_calendar set image_url='{OLD3_URL}', "
        f"source_media_url='{OLD3_URL}' where id='{HISTORICAL3}'")
    # Changed feed image URL refuses.
    sql(f"update public.content_calendar set image_url='{FEED3_URL}.moved', "
        f"source_media_url='{FEED3_URL}.moved' where id='{FEED3}'")
    assert sql(f"select public.lasso_unrelated_published_story("
               f"'{HISTORICAL3}','{FEED3}')") == "f"
    sql(f"update public.content_calendar set image_url='{FEED3_URL}', "
        f"source_media_url='{FEED3_URL}' where id='{FEED3}'")
    # Changed feed caption refuses (sha256 binding).
    sql("update public.content_calendar set caption='edited caption' "
        f"where id='{FEED3}'")
    assert sql(f"select public.lasso_unrelated_published_story("
               f"'{HISTORICAL3}','{FEED3}')") == "f"
    sql(f"update public.content_calendar set caption='{CAPTION3}' "
        f"where id='{FEED3}'")
    # Pillar mismatch refuses.
    sql(f"update public.content_calendar set pillar='platform' "
        f"where id='{HISTORICAL3}'")
    assert sql(f"select public.lasso_unrelated_published_story("
               f"'{HISTORICAL3}','{FEED3}')") == "f"
    sql(f"update public.content_calendar set pillar='doctrine' "
        f"where id='{HISTORICAL3}'")


def test_third_pair_null_metadata_refuses(pg):
    sql = pg
    # Erased feed metadata must refuse, never fall through a NULL predicate.
    restores = {"slot_index": "0", "status": "'published'",
                "variant_status": "'active'", "post_date": "'2026-10-04'",
                "gym_id": "'lasso'"}
    for column, original in restores.items():
        sql(f"update public.content_calendar set {column}=null "
            f"where id='{FEED3}'")
        assert sql(f"select public.lasso_unrelated_published_story("
                   f"'{HISTORICAL3}','{FEED3}')") == "f", column
        sql(f"update public.content_calendar set {column}={original} "
            f"where id='{FEED3}'")
    # Restoring correct values reopens the exception.
    assert sql(f"select public.lasso_unrelated_published_story("
               f"'{HISTORICAL3}','{FEED3}')") == "t"
