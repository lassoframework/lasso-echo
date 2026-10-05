"""Cross-gym source guard (2026-10-05): an asset is pickable/hostable/stampable
ONLY when asset.gym_id == the requested gym AND its linked media_source exists,
is active and carries the same gym. Missing or incomplete evidence fails closed.
Production carried 95 media_asset rows whose gym_id disagreed with the linked
media_source.gym_id; these tests pin the prevention."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db as agent_db  # noqa: E402
from agent import gym_media_selector as sel  # noqa: E402
from agent import gym_media_builder as bld  # noqa: E402
from agent import event_calendar as ec  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset, make_source  # noqa: E402

NOW = sel._now_utc()
GYM = "pierce"


def _patched(monkeypatch):
    """Isolate claims + alerts so picks run offline."""
    monkeypatch.setattr(agent_db, "drive_asset_claimed_ids", lambda base: set())


# ---- selector: the picker never returns a wrongly-sourced asset ------------------
def test_pick_excludes_asset_whose_source_belongs_to_another_gym(monkeypatch):
    _patched(monkeypatch)
    bad = make_asset("bad", gym_id=GYM, source_id="foreign-src")
    good = make_asset("good", gym_id=GYM, source_id="own-src")
    store = FakeMediaStore(
        sources=[make_source("foreign-src", gym_id="othergym"),
                 make_source("own-src", gym_id=GYM)],
        assets=[bad, good])
    picked = sel.pick_media(GYM, store=store, now=NOW)
    assert picked is not None and picked["id"] == "good"
    assert [a["id"] for a in sel.pickable(GYM, store=store, now=NOW)] == ["good"]


def test_pick_excludes_asset_with_missing_source_row(monkeypatch):
    _patched(monkeypatch)
    orphan = make_asset("orphan", gym_id=GYM, source_id="ghost-src")
    # explicit sources=[]: no auto-seeded stand-in, the source truly does not exist
    store = FakeMediaStore(sources=[], assets=[orphan])
    assert sel.pick_media(GYM, store=store, now=NOW) is None


def test_pick_excludes_asset_with_inactive_source(monkeypatch):
    _patched(monkeypatch)
    dead = make_asset("dead", gym_id=GYM, source_id="off-src")
    store = FakeMediaStore(
        sources=[make_source("off-src", gym_id=GYM, active=False)],
        assets=[dead])
    assert sel.pick_media(GYM, store=store, now=NOW) is None


def test_pick_excludes_asset_with_no_source_id(monkeypatch):
    _patched(monkeypatch)
    bare = make_asset("bare", gym_id=GYM, source_id=None)
    store = FakeMediaStore(sources=[], assets=[bare])
    assert sel.pick_media(GYM, store=store, now=NOW) is None


def test_pick_fails_closed_when_source_evidence_is_unreadable(monkeypatch):
    _patched(monkeypatch)
    good = make_asset("good", gym_id=GYM, source_id="own-src")

    class BlindStore(FakeMediaStore):
        def list_sources(self, gym_id=None, include_inactive=False):
            raise RuntimeError("postgrest down")

    store = BlindStore(sources=[make_source("own-src", gym_id=GYM)], assets=[good])
    assert sel.pick_media(GYM, store=store, now=NOW) is None
    assert sel.pickable(GYM, store=store, now=NOW) == []


def test_cooldown_fallback_also_enforces_the_source_guard(monkeypatch):
    _patched(monkeypatch)
    bad = make_asset("bad", gym_id=GYM, source_id="foreign-src")
    store = FakeMediaStore(sources=[make_source("foreign-src", gym_id="othergym")],
                           assets=[bad])
    assert sel.cooldown_fallback(GYM, store=store) == []


# ---- builder: assert_source gates hosting/stamping -------------------------------
def test_builder_assert_source_blocks_cross_gym_source():
    asset = make_asset("bad", gym_id=GYM, source_id="foreign-src")
    store = FakeMediaStore(sources=[make_source("foreign-src", gym_id="othergym")],
                           assets=[asset])
    assert bld.assert_source(asset, GYM, store) is False


def test_builder_assert_source_blocks_inactive_and_missing_sources():
    asset = make_asset("a", gym_id=GYM, source_id="off-src")
    off = FakeMediaStore(sources=[make_source("off-src", gym_id=GYM, active=False)],
                         assets=[asset])
    assert bld.assert_source(asset, GYM, off) is False
    ghost = FakeMediaStore(sources=[], assets=[asset])
    assert bld.assert_source(asset, GYM, ghost) is False


def test_builder_assert_source_fails_closed_on_unproven_evidence():
    asset = make_asset("a", gym_id=GYM, source_id="s1")

    class BlindStore(FakeMediaStore):
        def list_sources(self, gym_id=None, include_inactive=False):
            raise RuntimeError("postgrest down")

    assert bld.assert_source(asset, GYM, BlindStore(assets=[asset])) is False


def test_builder_assert_source_accepts_active_same_gym_source():
    asset = make_asset("ok", gym_id=GYM, source_id="own-src")
    store = FakeMediaStore(sources=[make_source("own-src", gym_id=GYM)],
                           assets=[asset])
    assert bld.assert_source(asset, GYM, store) is True


# ---- event path: an INJECTED picker cannot bypass the guard ----------------------
def test_event_attach_media_holds_cross_gym_sourced_asset(monkeypatch):
    from agent import gym_media_selector as _sel
    store = FakeMediaStore(sources=[make_source("foreign-src", gym_id="othergym")])
    monkeypatch.setattr(_sel._idx, "default_store", lambda: store)
    rows = [{"post_date": "2026-10-03", "account": "instagram", "format": "feed"}]
    hosted = []
    kept, held = ec._attach_media(
        GYM, rows, lambda m: None,
        picker=lambda exclude: {"id": "a1", "gym_id": GYM, "source_id": "foreign-src"},
        host=lambda asset, gym, drive: hosted.append(asset["id"]) or "https://cdn.test/a1.jpg")
    assert kept == [] and len(held) == 1
    assert hosted == []  # never hosted


def test_event_attach_media_holds_wrong_gym_asset_from_injected_picker(monkeypatch):
    from agent import gym_media_selector as _sel
    store = FakeMediaStore(sources=[make_source("own-src", gym_id="othergym")])
    monkeypatch.setattr(_sel._idx, "default_store", lambda: store)
    rows = [{"post_date": "2026-10-03", "account": "instagram", "format": "feed"}]
    kept, held = ec._attach_media(
        GYM, rows, lambda m: None,
        picker=lambda exclude: {"id": "a1", "gym_id": "othergym", "source_id": "own-src"},
        host=lambda asset, gym, drive: "https://cdn.test/a1.jpg")
    assert kept == [] and len(held) == 1


def test_event_attach_media_holds_when_source_evidence_unproven(monkeypatch):
    from agent import gym_media_selector as _sel

    class BlindStore(FakeMediaStore):
        def list_sources(self, gym_id=None, include_inactive=False):
            raise RuntimeError("postgrest down")

    monkeypatch.setattr(_sel._idx, "default_store",
                        lambda: BlindStore(sources=[make_source("s1", gym_id=GYM)]))
    rows = [{"post_date": "2026-10-03", "account": "instagram", "format": "feed"}]
    kept, held = ec._attach_media(
        GYM, rows, lambda m: None,
        picker=lambda exclude: {"id": "a1", "gym_id": GYM, "source_id": "s1"},
        host=lambda asset, gym, drive: "https://cdn.test/a1.jpg")
    assert kept == [] and len(held) == 1


# ---- P1 regressions: the real Snapshot wrappers carry validated source evidence --
def test_runner_drive_kind_available_picks_with_real_wrapper(monkeypatch):
    """The runner's assets-only Snapshot used to fail-close every Drive pick."""
    from types import SimpleNamespace
    from agent import config as _config, gym_media_index as _idx, runner
    _patched(monkeypatch)
    store = FakeMediaStore(
        sources=[make_source("own-src", gym_id=GYM)],
        assets=[make_asset("ok", gym_id=GYM, source_id="own-src")])
    monkeypatch.setattr(_idx, "default_store", lambda: store)
    monkeypatch.setattr(_config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(_config, "gym_drive_connect_active_for", lambda key: True)
    account = SimpleNamespace(key=f"{GYM}_ig")
    assert runner._client_drive_kind_available(account, "photo") is True
    assert runner._unused_client_photo_available(account, "/tmp/x/team.jpg", "2026-10-05") is True


def test_runner_drive_kind_available_uncertain_when_source_read_fails(monkeypatch):
    """A sources read failure must propagate as uncertainty (None), not empty."""
    from types import SimpleNamespace
    from agent import config as _config, gym_media_index as _idx, runner
    _patched(monkeypatch)

    class BlindStore(FakeMediaStore):
        def list_sources(self, gym_id=None, include_inactive=False):
            raise RuntimeError("postgrest down")

    monkeypatch.setattr(_idx, "default_store",
                        lambda: BlindStore(sources=[make_source("s1", gym_id=GYM)],
                                           assets=[make_asset("a", gym_id=GYM, source_id="s1")]))
    monkeypatch.setattr(_config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(_config, "gym_drive_connect_active_for", lambda key: True)
    account = SimpleNamespace(key=f"{GYM}_ig")
    assert runner._client_drive_kind_available(account, "photo") is None


def test_unused_client_photo_holds_video_when_source_evidence_unreadable(monkeypatch):
    """Unknown Drive inventory (unreadable sources) holds the video tier open."""
    from types import SimpleNamespace
    from agent import client_content, config as _config, gym_media_index as _idx, runner
    _patched(monkeypatch)
    monkeypatch.setattr(client_content, "pick_image", lambda *a, **k: None)

    class BlindStore(FakeMediaStore):
        def list_sources(self, gym_id=None, include_inactive=False):
            raise RuntimeError("postgrest down")

    monkeypatch.setattr(_idx, "default_store",
                        lambda: BlindStore(sources=[make_source("s1", gym_id=GYM)],
                                           assets=[make_asset("a", gym_id=GYM, source_id="s1")]))
    monkeypatch.setattr(_config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(_config, "gym_drive_connect_active_for", lambda key: True)
    account = SimpleNamespace(key=f"{GYM}_ig")
    # uncertainty -> True: an unused photo may exist, so video must not start
    assert runner._unused_client_photo_available(account, "/tmp/x/team.jpg", "2026-10-05") is True


def test_gbp_drive_photo_candidate_picks_with_validated_source_snapshot(monkeypatch):
    """The GBP wrapper now exposes the same-gym source rows it already read."""
    from agent import config as _config, gbp_planner, gym_media_index as _idx
    from agent.integrations import drive_client as _dc
    _patched(monkeypatch)
    src = make_source("own-src", gym_id=GYM)
    src.update(sync_status="ready", sync_finished_at="2026-10-02T00:00:00Z")
    store = FakeMediaStore(sources=[src],
                           assets=[make_asset("ok", gym_id=GYM, source_id="own-src")])
    monkeypatch.setattr(_idx, "default_store", lambda: store)
    monkeypatch.setattr(_config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(_config, "gym_drive_connect_active_for", lambda key: True)
    monkeypatch.setattr(gbp_planner, "_global_writer_enabled", lambda: False)
    monkeypatch.setattr(gbp_planner, "_cropped_image_url",
                        lambda *a, **k: "https://cdn.test/crop.jpg")

    class FakeDrive:
        def available(self):
            return True

        def download(self, file_id, dest):
            with open(dest, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xe0jpeg-bytes")
            return dest

    monkeypatch.setattr(_dc, "DriveClient", FakeDrive)
    pick = gbp_planner._drive_photo_candidate(f"{GYM}_ig", "2026-10-05", set())
    assert pick is not None and pick.get("asset", {}).get("id") == "ok"
    assert pick["url"] == "https://cdn.test/crop.jpg"


def test_gbp_drive_photo_candidate_fails_closed_when_source_read_fails(monkeypatch):
    from agent import config as _config, gbp_planner, gym_media_index as _idx
    _patched(monkeypatch)

    class BlindStore(FakeMediaStore):
        def list_sources(self, gym_id=None, include_inactive=False):
            raise RuntimeError("postgrest down")

    monkeypatch.setattr(_idx, "default_store",
                        lambda: BlindStore(sources=[make_source("s1", gym_id=GYM)],
                                           assets=[make_asset("a", gym_id=GYM, source_id="s1")]))
    monkeypatch.setattr(_config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(_config, "gym_drive_connect_active_for", lambda key: True)
    pick = gbp_planner._drive_photo_candidate(f"{GYM}_ig", "2026-10-05", set())
    assert pick == {"hold": True}


# ---- P1 regression: selector boundary re-validates active_source_ids output ------
def test_selector_revalidates_active_source_ids_via_full_rows(monkeypatch):
    """An adapter whose active_source_ids returns foreign ids must not bypass the
    guard when it also exposes full rows; boundary validation uses the rows."""
    _patched(monkeypatch)
    bad = make_asset("bad", gym_id=GYM, source_id="foreign-src")

    class LiarStore(FakeMediaStore):
        def active_source_ids(self, gym_id):
            return {"foreign-src"}  # claims a source the gym does not own

    store = LiarStore(sources=[make_source("own-src", gym_id=GYM)],
                      assets=[bad])
    assert sel.pick_media(GYM, store=store, now=NOW) is None


def test_selector_rejects_ids_only_source_evidence():
    class IdsOnlyStore:
        def active_source_ids(self, gym_id):
            return {"foreign-src"}

    try:
        sel.verified_source_ids(IdsOnlyStore(), GYM)
    except sel.SourceEvidenceUnavailable:
        pass
    else:
        raise AssertionError("source IDs without gym ownership proof must fail closed")
