"""
client_media_sync: the missing link that starts Echo working on a CLIENT gym once it
UPLOADS its media. Fully OFFLINE: a fake R2 (list_keys/get_bytes), an injected store,
a tmp cwd so content_library/<base> and brand_voice/<base> resolve into tmp.

Asserts:
  sync_uploads
    * lists + downloads NEW media into content_library/<base>, writes a public_url
      sidecar, and carries the gym's caption into the sidecar
    * media that INGEST staged to pending_caption/ IS synced (with its per-file
      caption), never skipped
    * media spread across pending_caption/ + incoming/ + originals/ all sync;
      idempotent; the SAME basename in two prefixes downloads once
    * thumbs/, *.json sidecars, and manifest.json are NEVER synced as media
    * IDEMPOTENT: a file already present is skipped, never re-downloaded
    * non-media exts (and the _upload.json/_intake.json sidecars) are ignored
    * empty R2 -> 0 synced
  scan_and_generate
    * media + approved sources + no existing calendar -> build_client_month called,
      DRAFT rows produced (paused), gym_id == base
    * NO media -> awaiting, the generator is NOT called
    * a gym that ALREADY has a calendar -> not regenerated
    * flag OFF -> no-op, nothing touched
    * one gym failing never blocks the others
    * NEVER auto-publishes (no meta_publisher anywhere in the path)
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_media_sync as cms, client_sources as cs  # noqa: E402


# ---- fakes -----------------------------------------------------------------------

class FakeR2:
    """An in-memory R2: {key: bytes}. Records get_bytes calls so a re-sync can be
    proven to NOT re-download an already-present file."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.got = []

    def list_keys(self, prefix):
        return [k for k in self.objects if k.startswith(prefix)]

    def get_bytes(self, key):
        self.got.append(key)
        return self.objects.get(key)


class FakeStore:
    """Injectable calendar store. list_month drives the 'already has a calendar'
    check; delete_month/insert_rows are what build_client_month applies through."""

    def __init__(self, existing=None):
        self.existing = existing or {}     # (base, month) -> [rows]
        self.deleted = []
        self.inserted = []

    def list_month(self, base_key, month):
        return self.existing.get((base_key, month), [])

    def delete_month(self, base_key, month):
        self.deleted.append((base_key, month))
        return 0

    def insert_rows(self, base_key, rows):
        self.inserted.extend(rows)
        return rows


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    # Isolated db + all client flags on; run from a tmp cwd so content_library/<base>
    # and brand_voice/<base> land in tmp, not the repo.
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    # S3_PUBLIC_BASE_URL is read from a module constant captured at import time, so set
    # the attribute (not just the env var) the way the code actually reads it.
    from agent import config as _config
    monkeypatch.setattr(_config, "S3_PUBLIC_BASE_URL", "https://cdn.example.com")
    monkeypatch.chdir(tmp_path)
    yield


def _stock_sources(account_key):
    cs.add_source(account_key, "offer", "21 day kickstart for busy parents",
                  "client social intake")
    cs.add_source(account_key, "service", "Small group training", "client social intake")
    cs.add_source(account_key, "about", "Who we help: parents in their 40s",
                  "client social intake")


def _bible(base, never_line="(none provided in the intake)"):
    """Write a minimal drafted bible so voice loads + banned words parse."""
    d = os.path.join("brand_voice", base)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "lasso_voice.md"), "w", encoding="utf-8") as fh:
        fh.write("We help members win.\n#GetFit\nSave this post.\n"
                 f"Words to NEVER use: {never_line}\n")


def _r2_with_uploads(base, n=4, extra=None):
    """A fake R2 holding n uploaded photos under intake/<base>/incoming/ plus an
    _upload.json captions sidecar. `extra` adds arbitrary keys (non-media, etc.)."""
    objs = {}
    caps = {}
    for i in range(n):
        name = f"20260810T120000Z_photo_{i:02d}.jpg"
        objs[f"intake/{base}/incoming/{name}"] = b"\xff\xd8\xffFAKEJPEG"
        caps[name] = f"day {i} at the gym"
    objs[f"intake/{base}/incoming/20260810T120000Z_upload.json"] = json.dumps(
        {"note": "batch", "captions": caps}).encode("utf-8")
    if extra:
        objs.update(extra)
    return FakeR2(objs)


# ---- sync_uploads ----------------------------------------------------------------

def test_sync_downloads_new_media_with_public_url_and_caption():
    r2 = _r2_with_uploads("gritx", n=3)
    out = cms.sync_uploads("gritx", r2=r2)
    assert out == {"synced": 3, "skipped": 0}

    lib = os.path.join("content_library", "gritx")
    imgs = sorted(f for f in os.listdir(lib) if f.endswith(".jpg"))
    assert imgs == ["20260810T120000Z_photo_00.jpg",
                    "20260810T120000Z_photo_01.jpg",
                    "20260810T120000Z_photo_02.jpg"]
    # sidecar carries the R2 public url + the gym's own caption (never fabricated).
    # The caption lives under "note" (the EXACT key library._load_sidecar reads into
    # Creative.client_note); writing "client_note" here would be silently dropped.
    side = json.load(open(os.path.join(lib, "20260810T120000Z_photo_00.json")))
    assert side["public_url"] == (
        "https://cdn.example.com/intake/gritx/incoming/"
        "20260810T120000Z_photo_00.jpg")
    assert side["note"] == "day 0 at the gym"
    # and it round-trips through the library the drafter reads
    from agent import library as _lib
    creatives = {os.path.basename(c.path): c for c in _lib.list_creatives(lib)}
    assert creatives["20260810T120000Z_photo_00.jpg"].client_note == "day 0 at the gym"


def test_sync_is_idempotent_no_redownload():
    r2 = _r2_with_uploads("gritx", n=3)
    cms.sync_uploads("gritx", r2=r2)
    downloads_first = [k for k in r2.got if k.endswith(".jpg")]
    assert len(downloads_first) == 3

    r2.got.clear()
    out2 = cms.sync_uploads("gritx", r2=r2)
    assert out2 == {"synced": 0, "skipped": 3}
    # NOT re-downloaded: no media get_bytes on the second pass
    assert [k for k in r2.got if k.endswith(".jpg")] == []


def test_sync_ignores_non_media_and_sidecars():
    extra = {
        "intake/gritx/incoming/20260810T120000Z_notes.txt": b"hi",
        "intake/gritx/incoming/20260810T120000Z_intake.json": b"{}",
        "intake/gritx/incoming/20260810T120000Z_doc.pdf": b"%PDF",
    }
    r2 = _r2_with_uploads("gritx", n=2, extra=extra)
    out = cms.sync_uploads("gritx", r2=r2)
    assert out["synced"] == 2   # only the two .jpg, never txt/pdf/json
    lib = os.path.join("content_library", "gritx")
    got = sorted(os.listdir(lib))
    assert not any(f.endswith((".txt", ".pdf")) for f in got)


def test_sync_never_syncs_thumbs_json_or_manifest():
    """Thumbnails (a real image extension), every *.json sidecar, and manifest.json
    are NEVER downloaded as media, even though thumbs share a .jpg extension."""
    objs = {
        # one real staged photo the gym uploaded
        "intake/gritx/pending_caption/20260810T120000Z_photo_00.jpg": b"\xff\xd8\xffJ",
        "intake/gritx/pending_caption/20260810T120000Z_photo_00.json":
            json.dumps({"status": "needs_caption",
                        "original_key": "intake/gritx/incoming/x"}).encode("utf-8"),
        # a thumbnail (real .jpg extension) must be excluded
        "intake/gritx/thumbs/20260810T120000Z_photo_00_thumb.jpg": b"\xff\xd8\xffT",
        # the processed manifest (JSON) must be excluded
        "intake/gritx/manifest.json": json.dumps({"processed": []}).encode("utf-8"),
    }
    r2 = FakeR2(objs)
    out = cms.sync_uploads("gritx", r2=r2)
    assert out["synced"] == 1   # only the one real photo
    lib = os.path.join("content_library", "gritx")
    got = sorted(os.listdir(lib))
    # exactly the one photo + its written library sidecar; no thumb, no manifest
    assert "20260810T120000Z_photo_00.jpg" in got
    assert not any("_thumb" in f for f in got)
    assert "manifest.json" not in got
    # the thumbnail bytes were never even fetched
    assert not any(k.endswith("_thumb.jpg") for k in r2.got)


def test_sync_pulls_pending_caption_with_per_file_caption():
    """The real bug: a gym's uploaded photo that ingest STAGED to pending_caption/
    (nothing left in incoming/) IS synced, and a per-file <stem>.json caption on it
    is carried into the library sidecar. Previously sync listed only incoming/ and
    found 0."""
    objs = {
        "intake/eng/pending_caption/20260810T120000Z_squat.jpg": b"\xff\xd8\xffENG",
        "intake/eng/pending_caption/20260810T120000Z_squat.json":
            json.dumps({"caption": "6am small group",
                        "status": "needs_caption"}).encode("utf-8"),
    }
    r2 = FakeR2(objs)
    out = cms.sync_uploads("eng", r2=r2)
    assert out == {"synced": 1, "skipped": 0}
    lib = os.path.join("content_library", "eng")
    assert os.path.exists(os.path.join(lib, "20260810T120000Z_squat.jpg"))
    side = json.load(open(os.path.join(lib, "20260810T120000Z_squat.json")))
    assert side["note"] == "6am small group"
    assert side["public_url"] == (
        "https://cdn.example.com/intake/eng/pending_caption/"
        "20260810T120000Z_squat.jpg")


def test_sync_across_prefixes_skips_originals_idempotent_and_deduped():
    """Media in pending_caption/ + incoming/ sync; originals/ (the RAW archive of an
    already-converted upload) is deliberately NOT synced — pulling it double-counted
    the same video (raw .mov + converted .mp4, different basenames), inflating the
    media count and double-placing the video on two feed days. The batch _upload.json
    caption applies to the incoming/ photo; a re-sync re-downloads nothing."""
    objs = {
        # staged (uncaptioned) in pending_caption/
        "intake/eng/pending_caption/20260810T120000Z_a.jpg": b"\xff\xd8\xffA",
        # fresh + captioned via the batch sidecar in incoming/
        "intake/eng/incoming/20260810T120000Z_b.jpg": b"\xff\xd8\xffB",
        "intake/eng/incoming/20260810T120000Z_upload.json":
            json.dumps({"note": "batch",
                        "captions": {"20260810T120000Z_b.jpg": "coach Dave"}}
                       ).encode("utf-8"),
        # a raw MOV archived to originals/: MUST NOT sync (its converted .mp4 is the
        # library copy; the raw would double-count the same video)
        "intake/eng/originals/20260810T120000Z_c.mov": b"\x00MOV",
        # the same basename in originals/ never shadows the processed copy
        "intake/eng/pending_caption/20260810T120000Z_d.jpg": b"\xff\xd8\xffDP",
        "intake/eng/originals/20260810T120000Z_d.jpg": b"\xff\xd8\xffDO",
    }
    r2 = FakeR2(objs)
    out = cms.sync_uploads("eng", r2=r2)
    assert out == {"synced": 3, "skipped": 0}   # a, b, d — never the raw archive

    lib = os.path.join("content_library", "eng")
    imgs = sorted(f for f in os.listdir(lib) if f.endswith((".jpg", ".mov")))
    assert imgs == ["20260810T120000Z_a.jpg", "20260810T120000Z_b.jpg",
                    "20260810T120000Z_d.jpg"]
    # dedup: the processed pending_caption bytes won for the shared basename 'd'
    with open(os.path.join(lib, "20260810T120000Z_d.jpg"), "rb") as fh:
        assert fh.read() == b"\xff\xd8\xffDP"
    # batch caption reached the incoming photo's sidecar
    side_b = json.load(open(os.path.join(lib, "20260810T120000Z_b.json")))
    assert side_b["note"] == "coach Dave"

    # IDEMPOTENT: nothing re-downloads on a second pass.
    r2.got.clear()
    out2 = cms.sync_uploads("eng", r2=r2)
    assert out2 == {"synced": 0, "skipped": 3}
    assert [k for k in r2.got if _is_media_get(k)] == []


def test_sync_non_media_exts_ignored_across_prefixes():
    """Non-media extensions anywhere are ignored; an empty gym -> 0 synced."""
    objs = {
        "intake/eng/pending_caption/20260810T120000Z_readme.txt": b"hi",
        "intake/eng/originals/20260810T120000Z_scan.pdf": b"%PDF",
    }
    r2 = FakeR2(objs)
    out = cms.sync_uploads("eng", r2=r2)
    assert out == {"synced": 0, "skipped": 0}
    assert not os.path.isdir(os.path.join("content_library", "eng"))


def _is_media_get(key):
    return key.endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"))


def test_sync_empty_r2_zero_synced():
    r2 = FakeR2({})
    out = cms.sync_uploads("gritx", r2=r2)
    assert out == {"synced": 0, "skipped": 0}


# ---- scan_and_generate -----------------------------------------------------------

def _feed_ig_rows(rows):
    """The instagram FEED rows (one per photo placed): skips the FB mirror + stories."""
    return [r for r in rows if r.get("format") == "feed"
            and str(r.get("account", "")).lower() in ("instagram", "ig", "")]


def _story_rows(rows):
    return [r for r in rows if r.get("format") == "story"]


def test_generate_when_media_and_sources_and_no_calendar():
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=5)
    store = FakeStore()

    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2)
    assert out["ok"] is True
    assert out["synced"] == 5
    assert out["generated"] == 1
    # DRAFT rows landed, gym_id == base, all PAUSED (pending), no publish anywhere
    assert store.inserted, "expected draft calendar rows"
    for row in store.inserted:
        assert row["gym_id"] == "gritx"
        assert row["status"] == "pending"
        assert "id" not in row

    # MEDIA-CAPPED, ONE PHOTO PER FEED, NO REUSE: 5 photos -> EXACTLY 5 ig feeds,
    # each a DISTINCT image_url, plus 5 paired stories (never padded to 30).
    ig_feeds = _feed_ig_rows(store.inserted)
    assert len(ig_feeds) == 5
    feed_imgs = [r["image_url"] for r in ig_feeds]
    assert len(set(feed_imgs)) == 5, "every feed must use a distinct photo (no reuse)"
    assert len(_story_rows(store.inserted)) == 5, "one paired story per feed"


def test_no_media_awaits_generator_not_called():
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = FakeR2({})        # nothing uploaded
    store = FakeStore()

    called = {"n": 0}
    import agent.client_month_run as cmr

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("build_client_month must not run without media")

    # patch the symbol the module imports lazily
    cmr.build_client_month = _boom
    try:
        out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2)
    finally:
        import importlib
        importlib.reload(cmr)

    assert out["ok"] is True
    assert out["awaiting"] == 1
    assert out["generated"] == 0
    assert called["n"] == 0
    assert store.inserted == [] and store.deleted == []


def _existing_feed_calendar(base, month, n):
    """n instagram FEED rows (one per already-placed photo) in the given month, the
    shape real_calendar_mirror writes and _existing_feed_count reads."""
    return [{"gym_id": base, "account": "instagram", "format": "feed",
             "post_date": f"{month}-{(i % 28) + 1:02d}", "status": "pending",
             "image_url": f"u{i}"} for i in range(n)]


def test_only_two_photos_builds_two_feeds_not_thirty():
    """Blake's cap: 2 photos -> 2 feed posts (+ 2 stories), never padded to 30."""
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=2)
    store = FakeStore()

    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2, days=30)
    assert out["ok"] is True and out["generated"] == 1
    ig_feeds = _feed_ig_rows(store.inserted)
    assert len(ig_feeds) == 2, "2 photos -> exactly 2 feeds, never 30"
    assert len({r["image_url"] for r in ig_feeds}) == 2  # distinct photos
    assert len(_story_rows(store.inserted)) == 2          # 2 paired stories


def test_equal_media_and_feeds_is_idempotent_skip():
    """media_count == existing feed rows -> SKIP, no rebuild (idempotent)."""
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=4)
    # already 4 feed rows placed for 4 photos -> nothing to grow.
    store = FakeStore(existing={("gritx", "2026-08"): _existing_feed_calendar(
        "gritx", "2026-08", 4)})

    from datetime import date
    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2,
                                now=date(2026, 8, 10))
    assert out["ok"] is True
    assert out["skipped_existing"] == 1
    assert out["generated"] == 0
    # media still synced, but NO calendar write (no delete, no insert)
    assert store.inserted == [] and store.deleted == []


def test_denied_post_triggers_replacement_generation():
    """Dale / ENG: a denied post must NOT block its own replacement.

    Old bug: _existing_feed_count included denied rows, so existing_feeds stayed at
    build_target and the scanner never rebuilt (no fresh post appeared). Fix: denied,
    killed, and deleted rows are excluded from the count so the scanner sees a gap and
    generates a replacement draft.
    """
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=4)   # 4 photos in R2
    # Existing calendar: 3 pending + 1 denied = 4 rows, but only 3 ACTIVE.
    existing_rows = _existing_feed_calendar("gritx", "2026-08", 3) + [
        {"gym_id": "gritx", "account": "instagram", "format": "feed",
         "post_date": "2026-08-28", "status": "denied", "image_url": "u_denied"},
    ]
    from datetime import date
    store = FakeStore(existing={("gritx", "2026-08"): existing_rows})
    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2,
                                now=date(2026, 8, 1), days=30)
    assert out["ok"] is True
    # Scanner sees 3 active feeds vs build_target of 4 -> rebuilds (generates replacement).
    assert out["generated"] == 1, \
        "denied post must not block its own replacement (old bug: no rebuild fired)"
    assert store.inserted, "replacement draft must be written"


def test_uploading_more_extends_calendar_up_to_new_count():
    """media_count > existing feed rows -> (re)build up to the new count; never past it."""
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=5)         # 5 photos now on hand
    # only 2 feeds were built before (2 photos at the time) -> extend to 5.
    store = FakeStore(existing={("gritx", "2026-08"): _existing_feed_calendar(
        "gritx", "2026-08", 2)})

    from datetime import date
    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2,
                                now=date(2026, 8, 10), days=30)
    assert out["ok"] is True
    assert out["generated"] == 1
    # extended (delete-then-insert) to exactly 5 distinct-photo feeds, never past 5.
    ig_feeds = _feed_ig_rows(store.inserted)
    assert len(ig_feeds) == 5
    assert len({r["image_url"] for r in ig_feeds}) == 5
    assert store.deleted, "extend does a gym-scoped delete-then-insert"


def test_never_builds_past_media_count_even_with_large_days():
    """days is only an UPPER bound: 3 photos + days=30 -> exactly 3 feeds."""
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=3)
    store = FakeStore()
    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2, days=30)
    assert out["ok"] is True and out["generated"] == 1
    assert len(_feed_ig_rows(store.inserted)) == 3


def _feeds_over_span(base, start, days):
    """`days` instagram FEED rows on `days` CONSECUTIVE, genuinely-distinct post_dates
    from `start` (may cross a month boundary), keyed into the store by each row's own
    month so _existing_feed_count reads them the way the live store returns rows. Unlike
    _existing_feed_calendar (which wraps at 28 and collides), this yields exactly `days`
    distinct feed dates — needed to model a gym built out to a full month cap."""
    from collections import defaultdict
    from datetime import timedelta
    by_month = defaultdict(list)
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        by_month[(base, d[:7])].append(
            {"gym_id": base, "account": "instagram", "format": "feed",
             "post_date": d, "status": "pending", "image_url": f"u{i}"})
    return dict(by_month)


# ---- CHURN FIX: a library LARGER than `days` must not rebuild every scan ----------
# (Ryan Parr / GritX, 2026-08-18: "it's been recreating the post since yesterday".)
# GritX had 179 usable media but a 30-day cap, so it placed ~30 feeds; the OLD compare
# `existing_feeds >= media_count` (30 >= 179 -> False) rebuilt on EVERY scan forever.
# The fix compares against build_target = min(days, media_count), so a full month is
# idempotent. These tests FAIL on the old code and pass on the fix.

def test_big_library_built_out_to_days_cap_is_idempotent_no_churn():
    """179-media gym already built out to its 30-feed `days` cap -> SKIP, no rebuild.

    Reproduces the GritX churn exactly: media_count (30 here, standing in for 179 >
    days) FAR exceeds the feeds a 30-day month can hold. Once built out, a re-scan must
    be idempotent (no delete-then-insert), or the calendar 'recreates the post' daily."""
    _stock_sources("gritx_ig")
    _bible("gritx")
    # 40 photos on hand but a 30-day month: the build can only ever place 30 feeds.
    r2 = _r2_with_uploads("gritx", n=40)
    # the gym is ALREADY built out to the 30-feed cap across the planned span.
    from datetime import date
    start = date(2026, 8, 1)
    store = FakeStore(existing=_feeds_over_span("gritx", start, 30))

    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2,
                                now=start, days=30)
    assert out["ok"] is True
    # THE FIX: idempotent skip, NOT a rebuild. Old code: existing_feeds(30) >=
    # media_count(40) is False -> it would rebuild (churn). New: >= build_target(30).
    assert out["generated"] == 0, "a full month must not rebuild (this was the churn)"
    assert out["skipped_existing"] == 1
    assert store.inserted == [] and store.deleted == [], \
        "no delete-then-insert: the calendar must not be recreated"
    res = {r["base"]: r for r in out["results"]}["gritx"]
    assert res["status"] == "has_calendar"
    assert res["build_target"] == 30


def test_big_library_extends_only_up_to_days_cap_then_stops():
    """A gym with more media than `days` but a SHORT existing calendar still grows —
    but only up to the `days` cap, and then never churns again on the next scan."""
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=40)      # plenty of media
    from datetime import date
    # only 5 feeds built so far -> should extend up to the 10-day cap (build_target=10).
    store = FakeStore(existing={("gritx", "2026-08"): _existing_feed_calendar(
        "gritx", "2026-08", 5)})
    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2,
                                now=date(2026, 8, 1), days=10)
    assert out["ok"] is True and out["generated"] == 1
    assert len(_feed_ig_rows(store.inserted)) == 10, "extends up to the days cap"

    # Now it is built out to the cap: a SECOND scan must be idempotent (no churn).
    store2 = FakeStore(existing={("gritx", "2026-08"): _existing_feed_calendar(
        "gritx", "2026-08", 10)})
    out2 = cms.scan_and_generate(clients=["gritx"], store=store2, r2=r2,
                                 now=date(2026, 8, 1), days=10)
    assert out2["generated"] == 0 and out2["skipped_existing"] == 1
    assert store2.inserted == [] and store2.deleted == []


def test_thin_library_smaller_than_days_alerts_the_coach_once():
    """Ryan's own hypothesis, surfaced: a gym with FEWER media than a full month gets a
    clear 'out of fresh creative' alert (deduped per count), never a silent short
    calendar. The build still proceeds; the alert is a signal, not a block."""
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=3)       # only 3 photos for a 30-day month
    store = FakeStore()

    alerts = []
    import agent.ops_alerts as _oa
    orig = _oa.alert
    _oa.alert = lambda msg, *a, **k: alerts.append(msg)
    try:
        out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2, days=30)
        # re-scan with the SAME count must NOT re-alert (deduped)
        store2 = FakeStore(existing={("gritx", "2026-08"): _existing_feed_calendar(
            "gritx", "2026-08", 3)})
        from datetime import date
        cms.scan_and_generate(clients=["gritx"], store=store2, r2=r2,
                              now=date(2026, 8, 1), days=30)
    finally:
        _oa.alert = orig

    assert out["generated"] == 1, "a thin library still builds what it can"
    thin = [m for m in alerts if "out of fresh creative" in m]
    assert len(thin) == 1, "exactly one thin-creative alert, deduped across scans"
    assert "3 usable" in thin[0] and "gritx" in thin[0]


def test_full_month_library_does_not_alert_thin():
    """A library that fills the whole month (media >= days) fires NO thin alert."""
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=6)
    store = FakeStore()
    alerts = []
    import agent.ops_alerts as _oa
    orig = _oa.alert
    _oa.alert = lambda msg, *a, **k: alerts.append(msg)
    try:
        # days=5, media=6 -> media >= days, no thin signal
        cms.scan_and_generate(clients=["gritx"], store=store, r2=r2, days=5)
    finally:
        _oa.alert = orig
    assert not [m for m in alerts if "out of fresh creative" in m]


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "false")
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=3)
    store = FakeStore()
    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2)
    assert out["ok"] is False
    assert store.inserted == [] and store.deleted == []
    # nothing downloaded either
    assert not os.path.isdir(os.path.join("content_library", "gritx"))


def test_one_gym_failing_never_blocks_others():
    # gritx is healthy; topfuel's R2 raises on list -> sync no-ops for it, and it has
    # no sources so it never builds, but gritx must still generate.
    _stock_sources("gritx_ig")
    _bible("gritx")

    class HalfBrokenR2(FakeR2):
        def list_keys(self, prefix):
            if prefix.startswith("intake/topfuel/"):
                raise RuntimeError("R2 boom for topfuel")
            return super().list_keys(prefix)

    r2 = _r2_with_uploads("gritx", n=4)
    broken = HalfBrokenR2(r2.objects)
    store = FakeStore()

    out = cms.scan_and_generate(clients=["topfuel", "gritx"], store=store, r2=broken)
    assert out["ok"] is True
    assert out["generated"] == 1      # gritx built despite topfuel's list failure
    statuses = {r["base"]: r["status"] for r in out["results"]}
    assert statuses["gritx"] == "generated"
    # topfuel: sync failed soft (0 synced) and it has no sources -> not built, no crash
    assert statuses["topfuel"] in ("no_sources", "awaiting_media", "error")


# ---- FIX 1: durable client bibles (survive a wiped /app on restart) --------------

def _durable_bible(base, durable_root, never_line="(none provided in the intake)"):
    """Write a client bible into the DURABLE voice dir (the persistent data volume),
    NOT the repo-relative brand_voice/<base>/. Mirrors _bible's content."""
    d = os.path.join(durable_root, base)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "lasso_voice.md"), "w", encoding="utf-8") as fh:
        fh.write("We help members win.\n#GetFit\nSave this post.\n"
                 f"Words to NEVER use: {never_line}\n")


def test_onboard_writes_bible_to_durable_path_not_repo(monkeypatch, tmp_path):
    """social_intake_reader.onboard_from_social lands the drafted bible on the DURABLE
    data volume (config.client_voice_dir()), never the ephemeral repo-relative
    brand_voice/<base>/ that a deploy wipes."""
    durable = str(tmp_path / "data" / "brand_voice")
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", durable)
    from agent import social_intake_reader as sir
    answers = {"base_key": "gritx", "gym": {"name": "GritX"},
               "voice": {"vibe": "warm", "words_to_never_use": "shredded, beast"}}
    out = sir.onboard_from_social("gritx_ig", answers, approve=True)
    assert out["base"] == "gritx"
    # the bible + proof are on the durable volume...
    assert os.path.exists(os.path.join(durable, "gritx", "lasso_voice.md"))
    assert os.path.exists(os.path.join(durable, "gritx", "social_proof.md"))
    # ...and NOT in the ephemeral repo-relative tree
    assert not os.path.exists(os.path.join("brand_voice", "gritx", "lasso_voice.md"))
    # the banned words rode into the bible verbatim (reviewer + guard both see them)
    assert set(out["banned_words"]) == {"shredded", "beast"}


def test_onboard_never_clobbers_a_reviewed_durable_bible(monkeypatch, tmp_path):
    """A re-run of onboard leaves an existing (human-reviewed) durable bible intact."""
    durable = str(tmp_path / "data" / "brand_voice")
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", durable)
    d = os.path.join(durable, "gritx")
    os.makedirs(d, exist_ok=True)
    reviewed = "REVIEWED BY A HUMAN\nWords to NEVER use: keep\n"
    with open(os.path.join(d, "lasso_voice.md"), "w", encoding="utf-8") as fh:
        fh.write(reviewed)
    from agent import social_intake_reader as sir
    sir.onboard_from_social("gritx_ig", {"base_key": "gritx", "gym": {"name": "GritX"},
                            "voice": {"words_to_never_use": "beast"}}, approve=True)
    assert open(os.path.join(d, "lasso_voice.md")).read() == reviewed


def test_restart_survival_durable_bible_loads_and_gym_builds(monkeypatch, tmp_path):
    """The BUG-1 fix, end to end: a WIPED /app (repo brand_voice/<base> ABSENT) but the
    durable bible PRESENT on the data volume -> the client voice still loads and the
    gym's DRAFT calendar is built from its real media (no 'voice doc missing')."""
    durable = str(tmp_path / "data" / "brand_voice")
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", durable)
    _stock_sources("gritx_ig")
    _durable_bible("gritx", durable, never_line="beast")
    # prove the repo-relative bible does NOT exist (simulating a wiped /app image)
    assert not os.path.exists(os.path.join("brand_voice", "gritx", "lasso_voice.md"))

    r2 = _r2_with_uploads("gritx", n=3)
    store = FakeStore()
    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2)
    assert out["ok"] is True
    assert out["generated"] == 1, "durable bible must let the build proceed post-restart"
    assert store.inserted, "expected DRAFT calendar rows built from the durable voice"
    # the durable-parsed banned word reached the build's guard (banned words resolved
    # from the durable bible, not the missing repo path)
    assert cms._banned_words_for("gritx") == ("beast",)


def test_lasso_committed_repo_bible_still_loads(monkeypatch, tmp_path):
    """LASSO's OWN committed bible (repo-relative, never onboarded) is untouched: with
    NO durable file present, the resolver falls back to the account's repo voice_doc."""
    durable = str(tmp_path / "data" / "brand_voice")
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", durable)
    _stock_sources("gritx_ig")
    _bible("gritx")   # repo-relative brand_voice/gritx/lasso_voice.md, no durable file
    assert not os.path.exists(os.path.join(durable, "gritx", "lasso_voice.md"))

    r2 = _r2_with_uploads("gritx", n=2)
    store = FakeStore()
    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2)
    assert out["ok"] is True and out["generated"] == 1
    # the resolver returned the repo path (durable absent), and banned words parsed
    resolved = cms._resolve_client_voice_path(
        "gritx", os.path.join("brand_voice", "gritx", "lasso_voice.md"))
    assert resolved == os.path.join("brand_voice", "gritx", "lasso_voice.md")


def test_no_meta_publisher_in_path():
    """Guard: the sync/generate path never imports or calls the live publisher. Check
    the compiled code (constants + names), so a module docstring mentioning the
    publisher by name in prose never trips the guard."""
    code = cms.scan_and_generate.__code__
    names = set()

    def _collect(c):
        names.update(c.co_names)
        for const in c.co_consts:
            if hasattr(const, "co_names"):
                _collect(const)

    _collect(code)
    # also fold in every function/method the module defines
    for obj in vars(cms).values():
        if hasattr(obj, "__code__"):
            _collect(obj.__code__)
    for banned in ("meta_publisher", "publish_due", "publish", "autopublish",
                   "host_media"):
        assert not any(banned in n for n in names), f"unexpected reference: {banned}"


# ---- audit B4: _write_sidecar MERGES into a pre-existing sidecar --------------------

def test_write_sidecar_merges_context_and_consent_without_clobbering_note(tmp_path, monkeypatch):
    """A pre-existing sidecar (a reviewed note) must NOT swallow this upload's context +
    consent (audit B4 hardening). The note is preserved; context merges in; consent records."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    from agent import dam
    lib = str(tmp_path)
    media = "20260810T120000Z_p.jpg"
    stem = os.path.splitext(media)[0]
    open(os.path.join(lib, media), "wb").close()
    # a sidecar already carries a reviewed note + public_url (no context/consent yet)
    with open(os.path.join(lib, stem + ".json"), "w") as fh:
        json.dump({"note": "reviewed note", "public_url": "https://r2/p.jpg"}, fh)

    cms._write_sidecar(lib, media, "clients/x/p.jpg", "a different caption",
                       lambda *_: None, client_context="busy professionals, 6am crew",
                       consent=True)

    with open(os.path.join(lib, stem + ".json")) as fh:
        side = json.load(fh)
    assert side["note"] == "reviewed note"                     # never clobbered
    assert side["client_context"] == "busy professionals, 6am crew"  # merged in
    # consent recorded in the DAM audit trail + sidecar
    assert str(side.get("consent", "")).lower() == "granted"
    assert dam.consent_log_entries(os.path.join(lib, media))    # an audit row exists


def test_write_sidecar_records_consent_at_most_once(tmp_path, monkeypatch):
    """The consent_recorded marker dedups: a second call with consent=True adds no new
    audit row (a re-sync cannot duplicate the consent log)."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    from agent import dam
    lib = str(tmp_path)
    media = "20260810T120000Z_q.jpg"
    open(os.path.join(lib, media), "wb").close()
    for _ in range(3):
        cms._write_sidecar(lib, media, "clients/x/q.jpg", "cap", lambda *_: None,
                           client_context="ctx", consent=True)
    assert len(dam.consent_log_entries(os.path.join(lib, media))) == 1
