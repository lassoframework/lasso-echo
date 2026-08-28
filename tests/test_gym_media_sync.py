"""gym_media_drive §4: sync indexes photos+videos, removes vanished + flips
pending, un-share -> revoked_externally + coach notified (no crash), budgeted
probe writes eligibility back."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.jobs import sync_gym_media as sync  # noqa: E402
from tests.gym_media_fakes import (FakeMediaStore, FakeDrive, photo, video,  # noqa: E402
                                   make_asset, make_source, _Resp)


def _src():
    return make_source("src1", gym_id="pierce", folder_id="fold1")


def test_sync_indexes_photos_and_videos(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    drive = FakeDrive(files=[photo("p1"), video("v1")])
    store = FakeMediaStore()
    res = sync.sync_source(_src(), drive=drive, store=store)
    assert res["ok"] is True
    assert res["inserted"] == 2
    assert store.assets["p1"]["eligible"] is True       # photo has dims -> gated
    assert store.assets["v1"]["eligible"] is None        # video unprobed


def test_sync_removed_file_flips_pending(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    flipped = []
    monkeypatch.setattr("agent.jobs.sync_gym_media._flip_pending_for_missing",
                        lambda g, ids, log: flipped.extend(ids) or len(ids))
    # p_gone existed before but is not in this walk.
    store = FakeMediaStore(assets=[make_asset("p_gone", gym_id="pierce",
                                             source_id="src1")])
    drive = FakeDrive(files=[photo("p1")])
    res = sync.sync_source(_src(), drive=drive, store=store)
    assert store.assets["p_gone"]["eligible"] is False
    assert store.assets["p_gone"]["reject_reason"] == "removed_from_drive"
    assert "p_gone" in flipped


def test_unshare_marks_revoked_and_notifies(monkeypatch):
    notes = []
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda text, channel=None, poster=None: notes.append(text))
    # walk raises a 403 (the SA lost access).
    drive = FakeDrive(walk_raises=_Resp(403))
    store = FakeMediaStore(sources=[_src()])
    res = sync.sync_source(_src(), drive=drive, store=store)
    assert res.get("revoked") is True                    # no crash
    assert store.sources["src1"]["revoked_externally"] is True
    assert notes and "revoked" in notes[0].lower()


def test_sync_probes_videos_and_writes_eligibility(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)

    def fake_probe(path):
        return {"duration_sec": 30.0, "width": 1080, "height": 1920, "codec": "h264"}

    drive = FakeDrive(files=[video("v1")])
    store = FakeMediaStore()
    res = sync.sync_source(_src(), drive=drive, store=store, probe_fn=fake_probe)
    assert store.assets["v1"]["eligible"] is True
    assert store.assets["v1"]["duration_sec"] == 30.0


def test_sync_unprobed_video_stays_ineligible(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    drive = FakeDrive(files=[video("v1")])
    store = FakeMediaStore()
    res = sync.sync_source(_src(), drive=drive, store=store,
                           probe_fn=lambda p: None)   # probe always fails
    assert store.assets["v1"]["eligible"] is None      # fail closed


def test_sync_queues_ambiguous_video_for_a_human(monkeypatch):
    # STORY_CLASSIFIER (default ON): an unprobed, neutral-named 9:16-unknown video
    # has no confident signal -> AMBIGUOUS -> enqueued to the "Sort these" queue,
    # never auto-decided. The sync summary reports it.
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest", lambda *a, **k: None)
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    enq = []
    monkeypatch.setattr("agent.story_sort_queue.enqueue",
                        lambda gym, aid, **k: (enq.append(aid) or True))
    drive = FakeDrive(files=[video("amb1", title="movie.mp4")])
    store = FakeMediaStore()
    res = sync.sync_source(_src(), drive=drive, store=store, probe_fn=lambda p: None)
    assert res["queued_ambiguous"] == 1
    assert "amb1" in enq


def test_sync_classifier_off_queues_nothing(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest", lambda *a, **k: None)
    monkeypatch.setenv("STORY_CLASSIFIER", "false")
    calls = []
    monkeypatch.setattr("agent.story_sort_queue.enqueue",
                        lambda *a, **k: calls.append(1) or True)
    drive = FakeDrive(files=[video("amb2", title="movie.mp4")])
    store = FakeMediaStore()
    res = sync.sync_source(_src(), drive=drive, store=store, probe_fn=lambda p: None)
    assert res["queued_ambiguous"] == 0
    assert calls == []


def test_sync_camera_native_video_is_not_queued(monkeypatch):
    # A camera-native filename classifies RAW (a confident non-ambiguous verdict), so
    # it is NOT queued for sorting — it just enters the raw pool.
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest", lambda *a, **k: None)
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    enq = []
    monkeypatch.setattr("agent.story_sort_queue.enqueue",
                        lambda gym, aid, **k: (enq.append(aid) or True))

    def probe_landscape(path):
        return {"duration_sec": 180.0, "width": 1920, "height": 1080, "codec": "h264"}

    drive = FakeDrive(files=[video("raw1", title="IMG_4021.MOV")])
    store = FakeMediaStore()
    res = sync.sync_source(_src(), drive=drive, store=store, probe_fn=probe_landscape)
    assert res["queued_ambiguous"] == 0
    assert enq == []


def test_run_stagger_and_deny_sweep(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    swept = {}
    monkeypatch.setattr("agent.gym_media_selector.observe_denials",
                        lambda store=None: {"rolled_back": 2})
    sleeps = []
    drive = FakeDrive(files=[photo("p1")])
    store = FakeMediaStore(sources=[
        make_source("s1", gym_id="pierce", folder_id="f1"),
        make_source("s2", gym_id="acme", folder_id="f2")])
    res = sync.run(drive=drive, store=store, sleep=lambda s: sleeps.append(s))
    assert res["sources"] == 2
    assert res["rolled_back"] == 2
    assert sleeps == [30.0]        # staggered once between the two sources
