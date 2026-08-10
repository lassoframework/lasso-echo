"""
client_media_sync: the missing link that starts Echo working on a CLIENT gym once it
UPLOADS its media. Fully OFFLINE: a fake R2 (list_keys/get_bytes), an injected store,
a tmp cwd so content_library/<base> and brand_voice/<base> resolve into tmp.

Asserts:
  sync_uploads
    * lists + downloads NEW media into content_library/<base>, writes a public_url
      sidecar, and carries the gym's caption into the sidecar
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
    # sidecar carries the R2 public url + the gym's own caption (never fabricated)
    side = json.load(open(os.path.join(lib, "20260810T120000Z_photo_00.json")))
    assert side["public_url"] == (
        "https://cdn.example.com/intake/gritx/incoming/"
        "20260810T120000Z_photo_00.jpg")
    assert side["client_note"] == "day 0 at the gym"


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


def test_sync_empty_r2_zero_synced():
    r2 = FakeR2({})
    out = cms.sync_uploads("gritx", r2=r2)
    assert out == {"synced": 0, "skipped": 0}


# ---- scan_and_generate -----------------------------------------------------------

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


def test_existing_calendar_not_regenerated():
    _stock_sources("gritx_ig")
    _bible("gritx")
    r2 = _r2_with_uploads("gritx", n=4)
    # gym already has rows in the start month -> never regenerate
    store = FakeStore(existing={("gritx", "2026-08"): [{"id": "x"}]})

    from datetime import date
    out = cms.scan_and_generate(clients=["gritx"], store=store, r2=r2,
                                now=date(2026, 8, 10))
    assert out["ok"] is True
    assert out["skipped_existing"] == 1
    assert out["generated"] == 0
    # media still synced, but NO calendar write
    assert store.inserted == [] and store.deleted == []


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
