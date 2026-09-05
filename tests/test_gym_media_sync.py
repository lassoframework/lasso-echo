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


def test_run_remaps_stale_fingerprint_source(monkeypatch):
    """The CrossFit Reverb class: a media_source row landed under a stale account-key
    fingerprint. The nightly sync resolves it to the currently-registered gym IN
    MEMORY for this pass (never rewriting the row itself) so the assets it indexes
    land under the key everything else reads."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    # Identity resolution (2026-09-04 audit): the resolver maps a gym's COMPUTED Echo key
    # onto that gym's live portal key by gym_id, so the sync is pinned by stubbing that
    # mapping rather than a name-slug "bases" list.
    monkeypatch.setattr(
        "agent.account_key_resolve.resolve",
        lambda key, now_fn=None, get=None: (
            "crossfitreverb30b5b2" if key == "crossfitreverb6cdf33" else key))
    drive = FakeDrive(files=[photo("p1")])
    store = FakeMediaStore(sources=[
        make_source("srev", gym_id="crossfitreverb6cdf33", folder_id="frev")])
    res = sync.run(drive=drive, store=store, sleep=lambda s: None)
    assert res["sources"] == 1
    assert res["results"][0]["gym_id"] == "crossfitreverb30b5b2"
    assert store.assets["p1"]["gym_id"] == "crossfitreverb30b5b2"


def test_run_leaves_registered_sources_untouched(monkeypatch):
    """A normal, already-registered gym key is never remapped (client_gym_bases
    contains it, so _resolve_stale_fingerprint short-circuits)."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    monkeypatch.setattr(
        "agent.calendar_autopublish.client_gym_bases", lambda: ["pierce"])
    drive = FakeDrive(files=[photo("p1")])
    store = FakeMediaStore(sources=[_src()])
    res = sync.run(drive=drive, store=store, sleep=lambda s: None)
    assert res["results"][0]["gym_id"] == "pierce"


# ---- SELF-RUNNING ambiguous sort (Blake 2026-08-31: no human sorting) ---------------

def _ambiguous_asset(aid="amb1", title="1KEKvwEuXYpEwCkohfN7"):
    """A Drive-ID-titled photo with no dims/signals: metadata-classifies AMBIGUOUS."""
    return {"id": aid, "title": title, "kind": "photo", "content_hash": ""}


def test_ambiguous_defaults_raw_via_auto_resolve(monkeypatch):
    monkeypatch.setenv("AGENT_SORT_AMBIGUOUS_DEFAULT", "true")
    calls = {"enq": [], "res": []}
    from agent import story_sort_queue as q
    monkeypatch.setattr(q, "enqueue", lambda g, a, **k: calls["enq"].append((g, a)) or True)
    monkeypatch.setattr(q, "resolve",
                        lambda g, a, lane, resolved_by="": (calls["res"].append(
                            (g, a, lane, resolved_by)) or (lane, None)))
    n = sync._sort_ambiguous([_ambiguous_asset()], "pierce", lambda m: None)
    assert n == 0, "nothing queues for a human when the default is armed"
    assert calls["res"] == [("pierce", "amb1", "raw", "echo-auto-sort")]


def test_ambiguous_with_edit_stamp_defaults_finished(monkeypatch):
    monkeypatch.setenv("AGENT_SORT_AMBIGUOUS_DEFAULT", "true")
    from agent import story_sort_queue as q
    res = []
    monkeypatch.setattr(q, "enqueue", lambda g, a, **k: True)
    monkeypatch.setattr(q, "resolve",
                        lambda g, a, lane, resolved_by="": (res.append(lane) or (lane, None)))
    # an edit-suite export name is a finished signal, but paired with a conflicting
    # camera-ish nothing it can still land ambiguous at the metadata stage for photos
    # with zero dims — the default must then lean FINISHED on the name.
    asset = {"id": "amb2", "title": "final_export_v2", "kind": "photo", "content_hash": ""}
    from agent import story_classifier as sc
    sig = sc.gather_signals(asset)
    verdict = sc.classify(sig)
    if verdict.verdict == sc.AMBIGUOUS:       # only assert the default when ambiguous
        sync._sort_ambiguous([asset], "pierce", lambda m: None)
        assert res == ["finished"]


def test_ambiguous_flag_off_still_queues_for_human(monkeypatch):
    monkeypatch.delenv("AGENT_SORT_AMBIGUOUS_DEFAULT", raising=False)
    from agent import story_sort_queue as q
    enq, res = [], []
    monkeypatch.setattr(q, "enqueue", lambda g, a, **k: enq.append(a) or True)
    monkeypatch.setattr(q, "resolve",
                        lambda g, a, lane, resolved_by="": (res.append(lane) or (lane, None)))
    n = sync._sort_ambiguous([_ambiguous_asset()], "pierce", lambda m: None)
    assert n == 1 and enq == ["amb1"] and res == []


def test_auto_resolve_failure_falls_back_to_human_queue(monkeypatch):
    monkeypatch.setenv("AGENT_SORT_AMBIGUOUS_DEFAULT", "true")
    from agent import story_sort_queue as q
    monkeypatch.setattr(q, "enqueue", lambda g, a, **k: True)
    monkeypatch.setattr(q, "resolve",
                        lambda g, a, lane, resolved_by="": (None, "store down"))
    n = sync._sort_ambiguous([_ambiguous_asset()], "pierce", lambda m: None)
    assert n == 1, "a failed auto-decision must fall back to the human queue, never vanish"


# ---- 2026-09-01: a confident FINISHED verdict is QUARANTINED, not discarded ----
# The proof-run gap: a FINISHED verdict (direct, or an echo-auto-sort resolution)
# used to be computed and thrown away, with zero effect on media_asset.eligible.
# story_candidates._eligible_raw / gym_media_selector.pick_media both fail closed
# on eligible is not True, so a real write here is what actually keeps a finished
# clip out of the raw pool.

def _finished_asset(aid="fin1", title="movie.mp4"):
    # 9:16 in-band duration + OCR text found -> a confident, direct FINISHED verdict.
    return {"id": aid, "title": title, "kind": "video", "content_hash": "",
            "width": 1080, "height": 1920, "duration_sec": 12.0, "eligible": True}


def test_direct_finished_verdict_quarantines_out_of_pool(monkeypatch):
    from agent import gym_media_index as _idx
    from tests.gym_media_fakes import FakeMediaStore
    store = FakeMediaStore(assets=[_finished_asset()])
    ocr_signals = {"fin1": (True, None)}   # the real, live OCR probe found text
    n = sync._sort_ambiguous([_finished_asset()], "pierce", lambda m: None,
                             store=store, ocr_signals=ocr_signals)
    assert n == 0, "a direct FINISHED verdict never queues for a human"
    assert store.assets["fin1"]["eligible"] is False
    assert store.assets["fin1"]["reject_reason"] == _idx.REJECT_FINISHED_CONTENT


def test_auto_sort_finished_resolution_also_quarantines(monkeypatch):
    monkeypatch.setenv("AGENT_SORT_AMBIGUOUS_DEFAULT", "true")
    from agent import gym_media_index as _idx
    from agent import story_sort_queue as q
    from tests.gym_media_fakes import FakeMediaStore
    monkeypatch.setattr(q, "enqueue", lambda g, a, **k: True)
    monkeypatch.setattr(
        q, "resolve", lambda g, a, lane, resolved_by="": (lane, None))
    # an edit-suite export name is a real finished signal; with no dims/duration
    # this classifies AMBIGUOUS at the metadata stage and auto-sorts to "finished".
    asset = {"id": "amb3", "title": "final_export_v2", "kind": "photo",
             "content_hash": "", "eligible": True}
    from agent import story_classifier as sc
    sig = sc.gather_signals(asset)
    verdict = sc.classify(sig)
    assert verdict.verdict == sc.AMBIGUOUS  # sanity: this is the auto-sort path
    store = FakeMediaStore(assets=[asset])
    sync._sort_ambiguous([asset], "pierce", lambda m: None, store=store)
    assert store.assets["amb3"]["eligible"] is False
    assert store.assets["amb3"]["reject_reason"] == _idx.REJECT_FINISHED_CONTENT


def test_raw_verdict_never_touches_eligibility(monkeypatch):
    from tests.gym_media_fakes import FakeMediaStore
    # camera-native landscape long clip -> confident RAW, no store write at all.
    asset = {"id": "raw9", "title": "IMG_4021.MOV", "kind": "video",
             "content_hash": "", "width": 1920, "height": 1080,
             "duration_sec": 180, "eligible": True}
    store = FakeMediaStore(assets=[asset])
    sync._sort_ambiguous([asset], "pierce", lambda m: None, store=store)
    assert store.updates == []
    assert store.assets["raw9"]["eligible"] is True
