"""gym_media_drive §4: sync indexes photos+videos, removes vanished + flips
pending, un-share -> revoked_externally + coach notified (no crash), budgeted
probe writes eligibility back."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent.jobs import sync_gym_media as sync  # noqa: E402

_REAL_RESOLVE_VERIFIED_KEYS = sync._resolve_verified_keys
_REAL_TENANT_IDENTITY = sync._tenant_identity
from tests.gym_media_fakes import (FakeMediaStore, FakeDrive, photo, video,  # noqa: E402
                                   make_asset, make_source, _Resp)


@pytest.fixture(autouse=True)
def _established_tenant_identity(monkeypatch):
    """The 2026-10-05 binding guard requires an ESTABLISHED tenant identity before
    any walk/insert. These tests exercise sync mechanics, not the identity plane,
    so every stored gym_id is stubbed as proven-live-and-itself by default; the
    guard tests below override this stub explicitly."""
    monkeypatch.setattr(sync, "_tenant_identity", lambda g: (True, g))
    # The nightly run() path resolves identity ONCE per run via the batch seam
    # (2026-10-05 independent-review: no per-source full-fleet reads); stub it
    # to the same proven-live-and-itself identity by default.
    monkeypatch.setattr(sync, "_resolve_verified_keys",
                        lambda ids, log=None: {g: g for g in ids})


def _src(store=None):
    """The default source. When `store` is given the row is REGISTERED in it
    first: the 2026-10-05 defense-in-depth guard re-reads the persisted row by
    ID and refuses a source the store cannot return."""
    s = make_source("src1", gym_id="pierce", folder_id="fold1")
    if store is not None:
        store.sources[s["id"]] = dict(s)
    return s


def test_sync_indexes_photos_and_videos(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    drive = FakeDrive(files=[photo("p1"), video("v1")])
    store = FakeMediaStore()
    res = sync.sync_source(_src(store), drive=drive, store=store)
    assert res["ok"] is True
    assert res["inserted"] == 2
    assert store.assets["p1"]["eligible"] is True       # photo has dims -> gated
    assert store.assets["v1"]["eligible"] is None        # video unprobed


def test_shared_drive_file_keeps_first_source_owner(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest", lambda *a, **k: None)
    store = FakeMediaStore(sources=[make_source("src1"),
                                    make_source("src2", folder_id="fold2")],
                           assets=[make_asset("shared", gym_id="pierce",
                                             source_id="src1")])
    second = store.sources["src2"]
    result = sync.sync_source(second, drive=FakeDrive(files=[photo("shared")]),
                              store=store)
    assert result["ok"] and result["inserted"] == 0
    assert store.assets["shared"]["source_id"] == "src1"
    assert not store.updates


def test_cross_gym_drive_id_collision_fails_before_insert():
    store = FakeMediaStore(assets=[make_asset("shared", gym_id="other",
                                             source_id="other-src")])
    try:
        sync.sync_source(_src(store), drive=FakeDrive(files=[photo("shared")]), store=store)
    except ValueError as exc:
        assert "another gym" in str(exc)
    else:
        raise AssertionError("cross-gym Drive id collision must fail")
    assert store.assets["shared"]["gym_id"] == "other"


def test_racing_source_keeps_shared_owner_and_indexes_unique_file(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest", lambda *a, **k: None)

    class RacingStore(FakeMediaStore):
        def insert_assets_ignore_conflicts(self, rows):
            # The competing worker inserts after this worker's precheck.
            self.assets["shared"] = make_asset("shared", gym_id="pierce",
                                                source_id="src1")
            return super().insert_assets_ignore_conflicts(rows)

    store = RacingStore(sources=[make_source("src1"),
                                 make_source("src2", folder_id="fold2")])
    second = store.sources["src2"]
    result = sync.sync_source(second,
                              drive=FakeDrive(files=[photo("shared"), photo("unique")]),
                              store=store)
    assert result["ok"] and result["inserted"] == 1
    assert store.assets["shared"]["source_id"] == "src1"
    assert store.assets["unique"]["source_id"] == "src2"


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
    res = sync.sync_source(_src(store), drive=drive, store=store)
    assert store.assets["p_gone"]["eligible"] is False
    assert store.assets["p_gone"]["reject_reason"] == "removed_from_drive"
    assert "p_gone" in flipped


def test_queued_import_does_not_change_vanished_asset_or_pending_post(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest", lambda *a, **k: None)
    pending = {"media_asset_id": "p_gone", "status": "pending"}

    def flip(_gym, ids, _log):
        if ids:
            pending["status"] = "media_not_ready"
        return len(ids)

    monkeypatch.setattr("agent.jobs.sync_gym_media._flip_pending_for_missing", flip)
    original = make_asset("p_gone", gym_id="pierce", source_id="src1")
    store = FakeMediaStore(assets=[original])
    result = sync.sync_source(_src(store), drive=FakeDrive(files=[]), store=store,
                              sweep_missing=False)
    assert result["ok"] is True
    assert store.assets["p_gone"] == original
    assert pending == {"media_asset_id": "p_gone", "status": "pending"}


def test_queued_import_emits_no_client_digest_on_success_or_revocation(monkeypatch):
    sent = []
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: sent.append("asset-or-revoked"))
    monkeypatch.setattr("agent.config.story_classifier_enabled", lambda: True)
    monkeypatch.setattr("agent.jobs.sync_gym_media._sort_ambiguous",
                        lambda *a, **k: 0)
    monkeypatch.setattr("agent.story_sort_queue.post_digest",
                        lambda *a, **k: sent.append("sort"))
    store = FakeMediaStore()
    result = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]),
                              store=store, sweep_missing=False, emit_digest=False)
    assert result["ok"] and result["inserted"] == 1
    revoked = sync.sync_source(_src(store), drive=FakeDrive(walk_raises=_Resp(403)),
                               store=store, sweep_missing=False, emit_digest=False)
    assert revoked["revoked"] is True
    assert sent == []


def test_unshare_marks_revoked_and_notifies(monkeypatch):
    notes = []
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda text, channel=None, poster=None: notes.append(text))
    # walk raises a 403 (the SA lost access).
    drive = FakeDrive(walk_raises=_Resp(403))
    store = FakeMediaStore(sources=[_src()])
    res = sync.sync_source(_src(store), drive=drive, store=store)
    assert res.get("revoked") is True                    # no crash
    assert store.sources["src1"]["revoked_externally"] is True
    assert notes and "revoked" in notes[0].lower()


def test_revoked_notice_does_not_reach_a_client_channel_when_not_armed(monkeypatch):
    """GAP 3 (audit of PR #68): the Drive-revoked notice used to post straight to
    the gym's own coach Slack channel via _coach_channel(gym_id), with no reference
    to compose(), the outbox, or the three-flag client_dm_support interlock at all
    -- reachable on AGENT_CLIENT_DM_AUTOFIX alone. It must now require the SAME
    flag every other client-facing send in this repo requires
    (SLACK_CONVO_ECHO_CLIENT_REPLY), which is unset/false here."""
    import os
    monkeypatch.delenv("SLACK_CONVO_ECHO_CLIENT_REPLY", raising=False)
    monkeypatch.setattr(sync, "_coach_channel", lambda gym_id: "C_CLIENT_CHANNEL")
    seen = []
    monkeypatch.setattr(sync, "_post_digest",
                        lambda text, channel=None, poster=None: seen.append(channel))
    drive = FakeDrive(walk_raises=_Resp(403))
    store = FakeMediaStore(sources=[_src()])
    res = sync.sync_source(_src(store), drive=drive, store=store)
    assert res.get("revoked") is True
    assert seen == [""], (
        "the revoked notice reached a client channel with no client-reply flag armed: "
        f"{seen!r}")


def test_revoked_notice_reaches_the_client_channel_once_armed(monkeypatch):
    """The negative case: with the SAME flag every other client-facing send needs,
    this notice may reach the gym's own channel -- it is not disabled outright."""
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setattr(sync, "_coach_channel", lambda gym_id: "C_CLIENT_CHANNEL")
    seen = []
    monkeypatch.setattr(sync, "_post_digest",
                        lambda text, channel=None, poster=None: seen.append(channel))
    drive = FakeDrive(walk_raises=_Resp(403))
    store = FakeMediaStore(sources=[_src()])
    sync.sync_source(_src(store), drive=drive, store=store)
    assert seen == ["C_CLIENT_CHANNEL"]


def test_sync_probes_videos_and_writes_eligibility(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)

    def fake_probe(path):
        return {"duration_sec": 30.0, "width": 1080, "height": 1920, "codec": "h264"}

    drive = FakeDrive(files=[video("v1")])
    store = FakeMediaStore()
    res = sync.sync_source(_src(store), drive=drive, store=store, probe_fn=fake_probe)
    assert store.assets["v1"]["eligible"] is True
    assert store.assets["v1"]["duration_sec"] == 30.0


def test_sync_unprobed_video_stays_ineligible(monkeypatch):
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    drive = FakeDrive(files=[video("v1")])
    store = FakeMediaStore()
    res = sync.sync_source(_src(store), drive=drive, store=store,
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
    res = sync.sync_source(_src(store), drive=drive, store=store, probe_fn=lambda p: None)
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
    res = sync.sync_source(_src(store), drive=drive, store=store, probe_fn=lambda p: None)
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
    res = sync.sync_source(_src(store), drive=drive, store=store, probe_fn=probe_landscape)
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


def test_run_refuses_stale_fingerprint_source(monkeypatch):
    """TENANT-SOURCE BINDING GUARD (2026-10-05), the CrossFit Reverb class: a
    media_source row landed under a stale account-key fingerprint. The nightly
    sync used to resolve it IN MEMORY and insert assets under the resolved
    tenant — that is exactly how production ended up with media_asset rows whose
    gym_id differs from their media_source.gym_id (95 rows; they must not be
    silently used or multiplied). Now the source is REFUSED for the pass: no
    walk, no asset rows, and the media_source row itself is never rewritten."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    # Identity established, but the stored key is a stale fingerprint that uniquely
    # maps onto the live tenant (the 2026-10-05 binding guard refuses before any
    # walk/insert — never silently re-keyed in memory).
    monkeypatch.setattr(
        sync, "_resolve_verified_keys",
        lambda ids, log=None: {
            g: ("crossfitreverb30b5b2" if g == "crossfitreverb6cdf33" else g)
            for g in ids})
    drive = FakeDrive(files=[photo("p1")])
    store = FakeMediaStore(sources=[
        make_source("srev", gym_id="crossfitreverb6cdf33", folder_id="frev")])
    res = sync.run(drive=drive, store=store, sleep=lambda s: None)
    assert res["sources"] == 1
    r0 = res["results"][0]
    assert r0["ok"] is False
    assert r0["refused"] == "source_gym_mismatch"
    assert r0["gym_id"] == "crossfitreverb6cdf33"   # STORED key, not the resolved one
    assert r0["resolved_gym_id"] == "crossfitreverb30b5b2"
    assert store.assets == {}, "no asset rows may be built for a refused source"
    assert "p1" not in store.assets
    # ownership never rewritten: the source row keeps its stored gym_id and no
    # update call touched it
    assert store.sources["srev"]["gym_id"] == "crossfitreverb6cdf33"
    assert not store.source_updates, "ownership must never be rewritten by sync"
    assert not store.updates


def test_sync_source_direct_call_also_refuses_stale_key(monkeypatch):
    """The guard lives INSIDE sync_source, not only run(): a direct caller (the
    Tough Temple re-stage recipe) passing the raw stale-keyed row must hit the
    same refusal instead of listing zero assets under the stale key and
    re-inserting every file."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    monkeypatch.setattr(
        sync, "_tenant_identity",
        lambda g: (True, "toughtemple52040e") if g == "toughtemple086f51"
        else (True, g))
    drive = FakeDrive(files=[photo("p1"), photo("p2")])
    store = FakeMediaStore(sources=[
        make_source("stale", gym_id="toughtemple086f51", folder_id="foldstale")])
    src = store.sources["stale"]
    res = sync.sync_source(src, drive=drive, store=store)
    assert res["ok"] is False
    assert res["refused"] == "source_gym_mismatch"
    assert store.assets == {}
    assert store.sources["stale"]["gym_id"] == "toughtemple086f51"


def test_sync_source_fails_closed_when_identity_cannot_be_established(monkeypatch):
    """Independent-review P2 (2026-10-05): the request-path resolver swallows
    failures and returns the key UNCHANGED, so a source whose tenant identity
    cannot be established at all used to sail through the old `resolved !=
    stored` check (unchanged == unchanged) and sync under an unproven key. The
    guard now distinguishes 'established' from 'unchanged-but-uncertain': an
    unreadable/truncated identity plane or a key that is not a uniquely
    registered tenant is REFUSED before any walk/insert."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    # _tenant_identity could not establish ANY identity for this key
    monkeypatch.setattr(sync, "_tenant_identity", lambda g: (False, g))
    drive = FakeDrive(files=[photo("p1"), photo("p2")])
    store = FakeMediaStore(sources=[
        make_source("sghost", gym_id="whokeys000000", folder_id="fghost")])
    res = sync.sync_source(store.sources["sghost"], drive=drive, store=store)
    assert res["ok"] is False
    assert res["refused"] == "tenant_identity_unestablished"
    assert res["gym_id"] == "whokeys000000"
    assert store.assets == {}, "no asset rows may be built when identity is unestablished"
    assert not store.source_updates, "ownership must never be rewritten by sync"


def test_unestablished_identity_stays_unarmed_inside_run_as_well(monkeypatch):
    """Independent-review P1 (2026-10-05): the nightly allowlist reads the SAME
    fresh batched identity result as sync_source — never the request-path
    _resolve_stale_fingerprint cache. A source whose tenant identity the batch
    could not prove stays UNARMED (skipped before any sync_source call), so the
    allowlist and the guard can never disagree and no walk/insert happens."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    monkeypatch.setattr(sync, "_resolve_verified_keys", lambda ids, log=None: {})
    walked = []
    drive = FakeDrive(files=[photo("p1")])
    orig_walk = drive.walk
    drive.walk = lambda *a, **k: walked.append(a) or orig_walk(*a, **k)
    store = FakeMediaStore(sources=[
        make_source("sghost", gym_id="whokeys000000", folder_id="fghost")])
    res = sync.run(drive=drive, store=store, sleep=lambda s: None)
    assert res["sources"] == 0, "unproven identity stays unarmed"
    assert res["results"] == []
    assert walked == [], "an unarmed source is never walked"
    assert store.assets == {}


def test_matching_source_still_syncs_after_guard(monkeypatch):
    """Fail-closed must not become fail-everything: a source whose stored gym_id
    already resolves to itself still walks and inserts normally."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    monkeypatch.setattr("agent.calendar_autopublish.client_gym_bases",
                        lambda: ["pierce"])
    drive = FakeDrive(files=[photo("p1")])
    store = FakeMediaStore(sources=[_src()])
    res = sync.run(drive=drive, store=store, sleep=lambda s: None)
    assert res["results"][0]["ok"] is True
    assert store.assets["p1"]["gym_id"] == "pierce"


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


# ---- per-run BATCHED tenant-identity resolution (independent review 2026-10-05) ----

def test_run_resolves_tenant_identity_once_for_the_whole_fleet(monkeypatch):
    """resolve_known_source_keys is a FULL-FLEET snapshot; calling it per source
    would multiply an expensive fleet-wide read by the fleet size. run() must do
    exactly ONE batched resolve for the fleet and hand the SAME trusted per-run
    result to both the allowlist and sync_source (which must not re-resolve)."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    # Restore the REAL batch seam (the autouse fixture stubs it) and count the
    # underlying full-fleet resolver calls.
    monkeypatch.setattr(sync, "_resolve_verified_keys", _REAL_RESOLVE_VERIFIED_KEYS)
    import agent.account_key_resolve as akr
    calls = []

    def counting_resolve(keys, **kw):
        calls.append(list(keys))
        return {k: k for k in keys}

    monkeypatch.setattr(akr, "resolve_known_source_keys", counting_resolve)

    class PerFolderDrive(FakeDrive):
        def walk(self, folder_id, max_depth=4, use_cache=True):
            # distinct Drive ids per folder so no cross-source ownership overlap
            return [photo(f"p-{folder_id}")]

    drive = PerFolderDrive()
    store = FakeMediaStore(sources=[
        make_source("s1", gym_id="pierce", folder_id="f1"),
        make_source("s2", gym_id="acme", folder_id="f2"),
        make_source("s3", gym_id="pierce", folder_id="f3")])  # duplicate gym
    res = sync.run(drive=drive, store=store, sleep=lambda s: None)
    assert res["sources"] == 3
    assert all(r["ok"] for r in res["results"])
    assert len(calls) == 1, ("exactly one batched full-fleet resolve per run; "
                             f"got {len(calls)}")
    assert sorted(calls[0]) == ["acme", "pierce"], ("one resolve covering every "
                                                    "armed gym_id, deduped")


def test_run_batch_resolution_failure_refuses_every_source_fail_closed(monkeypatch):
    """If the one batched identity read FAILS (exception or empty/uncertain
    result), every source this run stays UNARMED (the allowlist and the guard
    read the same failed batch): one skipped pass, never a write on an
    unproven identity. A multi-source fleet must not degrade into per-source
    fleet-wide reads either — still exactly one resolver call."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    monkeypatch.setattr(sync, "_resolve_verified_keys", _REAL_RESOLVE_VERIFIED_KEYS)
    import agent.account_key_resolve as akr
    calls = []

    def boom(keys, **kw):
        calls.append(list(keys))
        raise RuntimeError("identity plane read truncated")

    monkeypatch.setattr(akr, "resolve_known_source_keys", boom)
    drive = FakeDrive(files=[photo("p1")])
    store = FakeMediaStore(sources=[
        make_source("s1", gym_id="pierce", folder_id="f1"),
        make_source("s2", gym_id="acme", folder_id="f2")])
    res = sync.run(drive=drive, store=store, sleep=lambda s: None)
    assert len(calls) == 1
    # The SAME failed batch drives the allowlist too: every source stays
    # unarmed (skipped before sync_source), so nothing is walked or written.
    assert res["sources"] == 0
    assert res["results"] == []
    assert store.assets == {}, "no writes on an unproven identity"
    assert not store.source_updates


def test_direct_sync_source_still_does_its_own_fresh_verification(monkeypatch):
    """Direct sync_source callers (no trusted per-run batch) must NOT trust any
    cached/prior result: each call performs its OWN fresh complete verification
    through _tenant_identity. Two direct calls -> two resolver calls."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    import agent.account_key_resolve as akr
    calls = []

    def counting_resolve(keys, **kw):
        calls.append(list(keys))
        return {k: k for k in keys}

    monkeypatch.setattr(akr, "resolve_known_source_keys", counting_resolve)
    # Undo the autouse _tenant_identity stub so the REAL fresh-verify path runs.
    monkeypatch.setattr(sync, "_tenant_identity", _REAL_TENANT_IDENTITY)
    drive = FakeDrive(files=[photo("p1")])
    store = FakeMediaStore(sources=[_src()])
    r1 = sync.sync_source(_src(store), drive=drive, store=store)
    r2 = sync.sync_source(_src(store), drive=drive, store=store)
    assert r1["ok"] is True and r2["ok"] is True
    assert len(calls) >= 2, "each direct call re-verifies freshly"


def test_sync_source_with_trusted_run_keys_does_not_re_resolve(monkeypatch):
    """The trusted per-run batch is authoritative inside its run: sync_source
    given verified_keys must NOT call the fleet resolver again, and must still
    fail closed on a key the batch could not prove (absent) or proved stale."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    import agent.account_key_resolve as akr
    calls = []
    monkeypatch.setattr(akr, "resolve_known_source_keys",
                        lambda keys, **kw: calls.append(list(keys)) or {})
    drive = FakeDrive(files=[photo("p1")])
    store = FakeMediaStore(sources=[_src()])
    # (a) proven-live-and-itself via the batch: syncs without re-resolving
    ok = sync.sync_source(_src(store), drive=drive, store=store,
                          verified_keys={"pierce": "pierce"})
    assert ok["ok"] is True
    # (b) batch proved the stored key STALE: refused, still no re-resolve
    stale = sync.sync_source(_src(store), drive=drive, store=store,
                             verified_keys={"pierce": "pierce-live"})
    assert stale["refused"] == "source_gym_mismatch"
    # (c) batch could not prove the key at all: refused unestablished
    un = sync.sync_source(_src(store), drive=drive, store=store, verified_keys={})
    assert un["refused"] == "tenant_identity_unestablished"
    assert calls == [], ("sync_source with a trusted per-run batch never calls "
                         "the fleet resolver itself")


# ---- defense in depth: persisted-row re-read (independent-review P0, 2026-10-05) ----

def test_sync_source_refuses_a_caller_forged_gym_id(monkeypatch):
    """A caller that rewrote gym_id/folder_id on an in-memory copy (e.g. the old
    stale-key remap) is refused BEFORE any walk/insert even when the identity
    guard alone would pass the forged key: the persisted row is authoritative."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    store = FakeMediaStore(sources=[_src()])
    forged = _src()
    forged["gym_id"] = "othergym"          # caller-side rewrite attempt
    walked = []
    drive = FakeDrive(files=[photo("p1")])
    orig_walk = drive.walk
    drive.walk = lambda *a, **k: walked.append(a) or orig_walk(*a, **k)
    res = sync.sync_source(forged, drive=drive, store=store)
    assert res["ok"] is False and res["refused"] == "source_row_mismatch"
    assert res["fields"] == ["gym_id"]
    assert res["gym_id"] == "pierce", "reports the PERSISTED owner"
    assert walked == [] and store.assets == {}
    assert not store.source_updates


def test_sync_source_refuses_a_source_the_store_cannot_return(monkeypatch):
    """Fail closed, never fail open: a source row the store cannot re-read by ID
    (missing row, or a store without get_source) refuses the pass."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    store = FakeMediaStore()               # no sources registered
    res = sync.sync_source(_src(), drive=FakeDrive(files=[photo("p1")]),
                           store=store)
    assert res["ok"] is False and res["refused"] == "source_row_unreadable"
    assert store.assets == {}

    class NoGetSource(FakeMediaStore):
        get_source = None                  # no re-read capability at all

    store2 = NoGetSource(sources=[_src()])
    res2 = sync.sync_source(_src(), drive=FakeDrive(files=[photo("p1")]),
                            store=store2)
    assert res2["ok"] is False and res2["refused"] == "source_row_unreadable"
    assert store2.assets == {}


def test_sync_source_refuses_a_persisted_inactive_source(monkeypatch):
    """A caller may not resurrect a disconnected (persisted-inactive) source by
    passing an in-memory copy with active=True."""
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    store = FakeMediaStore(sources=[make_source("src1", active=False)])
    res = sync.sync_source(_src(), drive=FakeDrive(files=[photo("p1")]),
                           store=store)
    assert res["ok"] is False and res["refused"] == "source_inactive"
    assert store.assets == {}



# ---- inventory mutation receipt fence (default OFF) -------------------------
# Mirrors tests/test_intake_web_inventory_guard.py: fully offline — fake
# mutation authority (no PG), real tmp library/SQLite/journal paths.
import json  # noqa: E402
import sqlite3  # noqa: E402
import uuid  # noqa: E402

from agent import config as _config  # noqa: E402
from agent import local_inventory_mutation as _mutation  # noqa: E402


class _FakeAuthority:
    """Fake mutation authority; fail='begin'/'complete' simulates a lost ack."""

    def __init__(self, fail=None):
        self.fail = fail

    def begin(self, request):
        self.pending = dict(request, state="pending", generation=3,
                            result_digest=None,
                            begun_at="2026-10-08T00:00:00+00:00",
                            completed_at=None)
        if self.fail == "begin":
            raise _mutation.MutationHold("ack_lost")
        return dict(self.pending)

    def complete(self, request, result_digest):
        if self.fail == "complete":
            raise _mutation.MutationHold("ack_lost")
        self.pending.update(state="complete", result_digest=result_digest,
                            completed_at="2026-10-08T00:00:01+00:00")
        return dict(self.pending)

    def close(self):
        pass


@pytest.fixture
def fenced(tmp_path, monkeypatch):
    """Mutation fence armed with real tmp durable paths for gym 'pierce'."""
    root = tmp_path.resolve()
    monkeypatch.setenv("AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_INVENTORY_MUTATION_EPOCH", str(uuid.uuid4()))
    database = root / "echo.db"
    sqlite3.connect(database).close()
    monkeypatch.setenv("AGENT_DB_PATH", str(database))
    monkeypatch.setattr(_config, "LIBRARY_PATH", str(root))
    (root / "pierce").mkdir()
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    return root


def _journals(root):
    directory = root / "inventory-mutation-receipts"
    return [json.loads(p.read_text()) for p in directory.glob("*.json")] \
        if directory.exists() else []


def _install_authority(monkeypatch, authority):
    monkeypatch.setattr(_mutation.MutationAuthority, "from_environment",
                        staticmethod(lambda: authority))


def test_fence_off_by_default_leaves_no_journal(tmp_path, monkeypatch):
    """DEFAULT OFF: with the flag unset, sync behaves exactly as legacy — no
    receipt, no journal directory, no library requirement."""
    monkeypatch.delenv("AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED", raising=False)
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: None)
    store = FakeMediaStore()
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]),
                           store=store)
    assert res["ok"] is True and res["inserted"] == 1
    assert not (tmp_path / "inventory-mutation-receipts").exists()


def test_fenced_sync_completes_with_receipt_and_verified_effects(fenced,
                                                                 monkeypatch):
    _install_authority(monkeypatch, _FakeAuthority())
    store = FakeMediaStore()
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]),
                           store=store)
    assert res["ok"] is True and res["inserted"] == 1
    assert store.assets["p1"]["gym_id"] == "pierce"
    (record,) = _journals(fenced)
    assert record["local_state"] == "complete"
    assert record["gym_id"] == "pierce" and record["kind"] == "gym_media_sync"
    assert record["complete_receipt"]["state"] == "complete"
    assert record["complete_receipt"]["result_digest"] == record["result_digest"]


def test_fenced_sync_holds_on_readback_mismatch(fenced, monkeypatch):
    """An index effect that does not re-read clean holds the receipt: held
    summary, no digest, never COMPLETE."""

    class CorruptingStore(FakeMediaStore):
        def insert_assets_ignore_conflicts(self, rows):
            rows = [dict(r, title="tampered") for r in rows]
            return super().insert_assets_ignore_conflicts(rows)

    digests = []
    monkeypatch.setattr("agent.jobs.sync_gym_media._post_digest",
                        lambda *a, **k: digests.append(a))
    _install_authority(monkeypatch, _FakeAuthority())
    store = CorruptingStore()
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]),
                           store=store)
    assert res["ok"] is False and res["held"] is True
    assert digests == []                       # no certified-success digest
    (record,) = _journals(fenced)
    # The local effect committed but completion never happened: pending forever.
    assert record["local_state"] != "complete"
    assert record.get("complete_receipt") is None


def test_fenced_sync_holds_before_any_write_when_authority_unavailable(fenced):
    """Fail closed: no dedicated mutator login configured -> nothing indexed."""
    store = FakeMediaStore()
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]),
                           store=store)
    assert res["ok"] is False and res["held"] is True
    assert store.assets == {}


def test_fenced_sync_uncertain_begin_writes_nothing(fenced, monkeypatch):
    _install_authority(monkeypatch, _FakeAuthority(fail="begin"))
    store = FakeMediaStore()
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]),
                           store=store)
    assert res["ok"] is False and res["held"] is True
    assert store.assets == {}                  # begin never settled: no effect
    (record,) = _journals(fenced)
    assert record["local_state"] == "prepared"  # exact identity retained pending


def test_fenced_sync_uncertain_complete_stays_pending(fenced, monkeypatch):
    _install_authority(monkeypatch, _FakeAuthority(fail="complete"))
    store = FakeMediaStore()
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]),
                           store=store)
    assert res["ok"] is False and res["held"] is True
    (record,) = _journals(fenced)
    # Local effects happened but PG completion is uncertain: pending, never
    # certified complete; a retry is blocked by assert_settled.
    assert record["local_state"] == "local_committed"
    with pytest.raises(_mutation.MutationHold):
        _mutation.assert_settled(
            _mutation.configured("pierce", (fenced / "pierce").absolute()))


def test_fenced_sync_holds_not_raises_on_cross_gym_collision(fenced,
                                                             monkeypatch):
    """A duplicate/collision is a HOLD in armed mode, not a raw exception."""
    _install_authority(monkeypatch, _FakeAuthority())
    store = FakeMediaStore(assets=[make_asset("shared", gym_id="other",
                                              source_id="other-src")])
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("shared")]),
                           store=store)
    assert res["ok"] is False and res["held"] is True
    assert store.assets["shared"]["gym_id"] == "other"   # other gym untouched
    (record,) = _journals(fenced)
    assert record["local_state"] != "complete"


def test_fenced_revoked_mark_settles_under_its_own_receipt(fenced, monkeypatch):
    _install_authority(monkeypatch, _FakeAuthority())
    store = FakeMediaStore(sources=[_src()])
    res = sync.sync_source(_src(store), drive=FakeDrive(walk_raises=_Resp(403)),
                           store=store)
    assert res.get("revoked") is True
    assert store.sources["src1"]["revoked_externally"] is True
    (record,) = _journals(fenced)
    assert record["kind"] == "gym_media_source_revoke"
    assert record["local_state"] == "complete"


@pytest.mark.parametrize("outcome", ["rejected", "exception", "still_pending", "bad_readback"])
def test_fenced_calendar_flip_uncertainty_holds(fenced, monkeypatch, outcome):
    import requests
    _install_authority(monkeypatch, _FakeAuthority())
    monkeypatch.setattr(_config, "supabase_url", lambda: "https://example.invalid")
    monkeypatch.setattr(_config, "supabase_service_key", lambda: "test-key")

    class Response:
        status_code = 500 if outcome == "rejected" else 200
        text = "rejected"
        def json(self):
            return [{"id": "pending"}] if outcome == "still_pending" else {}

    def patch(*args, **kwargs):
        if outcome == "exception":
            raise RuntimeError("transport down")
        return Response()

    monkeypatch.setattr(requests, "patch", patch)
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response())
    store = FakeMediaStore(assets=[make_asset("gone", source_id="src1")])
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[]), store=store)
    assert res["held"] and res["hold_reason"] == "local_mutation_pending_reconciliation"
    assert _journals(fenced)[0]["local_state"] != "complete"


@pytest.mark.parametrize("revoked", [True, False])
@pytest.mark.parametrize("write_error", [False, True])
def test_fenced_source_flag_requires_persisted_readback(fenced, monkeypatch,
                                                       revoked, write_error):
    _install_authority(monkeypatch, _FakeAuthority())
    class NoWriteStore(FakeMediaStore):
        def update_source(self, identifier, fields):
            if write_error:
                raise RuntimeError("source write failed")
            return True
    store = NoWriteStore()
    source = _src(store)
    source["revoked_externally"] = not revoked
    store.sources["src1"] = dict(source)
    drive = FakeDrive(walk_raises=_Resp(403)) if revoked else FakeDrive(files=[])
    res = sync.sync_source(source, drive=drive, store=store)
    assert res["ok"] is False and res["held"] is True
    assert _journals(fenced)[0]["local_state"] != "complete"


@pytest.mark.parametrize("field", ["width", "height", "aspect", "eligible", "reject_reason"])
def test_fenced_probe_requires_all_written_fields(fenced, monkeypatch, field):
    _install_authority(monkeypatch, _FakeAuthority())
    monkeypatch.setattr(_config, "story_classifier_enabled", lambda: False)
    class PartialStore(FakeMediaStore):
        def update_asset(self, identifier, fields):
            fields = dict(fields)
            if "duration_sec" in fields:
                fields.pop(field, None)
            return super().update_asset(identifier, fields)
    store = PartialStore()
    # Landscape, long video gives distinct eligibility/rejection values.
    probe = lambda p: {"duration_sec": 180., "width": 1920, "height": 1080, "codec": "h264"}
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[video("v1")]),
                           store=store, probe_fn=probe, render_budget=0)
    assert res["held"] and _journals(fenced)[0]["local_state"] != "complete"


@pytest.mark.parametrize("write_error", [False, True])
def test_fenced_rendition_noop_or_swallowed_error_holds(fenced, monkeypatch, write_error):
    _install_authority(monkeypatch, _FakeAuthority())
    monkeypatch.setattr(_config, "story_classifier_enabled", lambda: False)
    class NoRenditionStore(FakeMediaStore):
        def update_asset(self, identifier, fields):
            if "rendition_url" in fields:
                if write_error:
                    raise RuntimeError("rendition write failed")
                return True
            return super().update_asset(identifier, fields)
    store = NoRenditionStore()
    probe = lambda p: {"duration_sec": 30., "width": 1080, "height": 1920, "codec": "h264"}
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[video("v1")]),
                           store=store, probe_fn=probe, render_budget=1,
                           host_fn=lambda *a: "https://example.invalid/clip.mp4")
    assert res["held"] and _journals(fenced)[0]["local_state"] != "complete"


def test_fenced_classifier_quarantine_noop_holds(fenced, monkeypatch):
    from types import SimpleNamespace
    from agent import story_classifier as sc
    _install_authority(monkeypatch, _FakeAuthority())
    monkeypatch.setattr(_config, "story_classifier_enabled", lambda: True)
    monkeypatch.setattr(sc, "classify", lambda sig: SimpleNamespace(
        verdict=sc.FINISHED, reasons=["finished"]))
    class NoQuarantineStore(FakeMediaStore):
        def update_asset(self, identifier, fields):
            if fields.get("reject_reason") == sync._idx.REJECT_FINISHED_CONTENT:
                return True
            return super().update_asset(identifier, fields)
    store = NoQuarantineStore()
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]), store=store)
    assert res["held"] and _journals(fenced)[0]["local_state"] != "complete"


@pytest.mark.parametrize("field", ["consent_status", "review_status", "review_content_hash",
                                    "release_ref", "moderation_status"])
def test_fenced_changed_bytes_requires_consent_review_invalidation(fenced, monkeypatch, field):
    _install_authority(monkeypatch, _FakeAuthority())
    class PartialResetStore(FakeMediaStore):
        def update_indexed_asset_if_hash(self, gym_id, identifier, old_hash, fields):
            fields = dict(fields)
            fields.pop(field, None)
            return super().update_indexed_asset_if_hash(gym_id, identifier, old_hash, fields)
    asset = make_asset("p1", source_id="src1", content_hash="old")
    asset.update(consent_status="granted", review_status="approved",
                 review_content_hash="old", release_ref="old-release",
                 moderation_status="passed")
    store = PartialResetStore(assets=[asset])
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]),
                           store=store, render_budget=0)
    assert res["held"] and _journals(fenced)[0]["local_state"] != "complete"


def test_fenced_probe_and_rendition_persisted_effects_complete(fenced, monkeypatch):
    _install_authority(monkeypatch, _FakeAuthority())
    monkeypatch.setattr(_config, "story_classifier_enabled", lambda: False)
    store = FakeMediaStore()
    probe = lambda p: {"duration_sec": 30., "width": 1080, "height": 1920, "codec": "h264"}
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[video("v1")]),
                           store=store, probe_fn=probe, render_budget=1,
                           host_fn=lambda *a: "https://example.invalid/clip.mp4")
    assert res["ok"] and res["probed"] == 1 and res["prehosted"] == 1
    assert _journals(fenced)[0]["local_state"] == "complete"


def test_fence_off_preserves_best_effort_calendar_failure(monkeypatch):
    import requests
    monkeypatch.delenv("AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED", raising=False)
    monkeypatch.setattr(_config, "supabase_url", lambda: "https://example.invalid")
    monkeypatch.setattr(_config, "supabase_service_key", lambda: "test-key")
    monkeypatch.setattr(sync, "_post_digest", lambda *a, **k: None)
    class Response:
        status_code = 500
        text = "rejected"
    monkeypatch.setattr(requests, "patch", lambda *a, **k: Response())
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("legacy readback"))
    store = FakeMediaStore(assets=[make_asset("gone", source_id="src1")])
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[]), store=store, emit_digest=False)
    assert res["ok"] and res["removed"] == 1


@pytest.mark.parametrize("corruption", [
    {"eligible": True, "reject_reason": None, "width": 9999},
    {"eligible": True}, {"reject_reason": None}, {"width": 9999},
    {"height": 9999}, {"used_count": 4},
])
def test_fenced_insert_requires_all_initial_fields(fenced, monkeypatch, corruption):
    _install_authority(monkeypatch, _FakeAuthority())
    monkeypatch.setattr(_config, "story_classifier_enabled", lambda: False)
    class CorruptInsertStore(FakeMediaStore):
        def insert_assets_ignore_conflicts(self, rows):
            inserted = super().insert_assets_ignore_conflicts(rows)
            for identifier in inserted:
                self.assets[identifier].update(corruption)
            return inserted
    store = CorruptInsertStore()
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("tiny", w=100, h=100)]),
                           store=store, render_budget=0)
    assert res["ok"] is False and res["held"] is True
    assert _journals(fenced)[0]["local_state"] != "complete"


def test_fenced_insert_conflict_preserves_existing_effect_fields(fenced, monkeypatch):
    _install_authority(monkeypatch, _FakeAuthority())
    monkeypatch.setattr(_config, "story_classifier_enabled", lambda: False)
    class SameSourceRaceStore(FakeMediaStore):
        def insert_assets_ignore_conflicts(self, rows):
            # Another pass already inserted the same source-owned bytes and
            # inspected them. Ignoring conflict must not demand insertion defaults.
            for row in rows:
                self.assets[row["id"]] = dict(row, eligible=False,
                                               reject_reason="operator_hold", used_count=5)
            return set()
    store = SameSourceRaceStore()
    res = sync.sync_source(_src(store), drive=FakeDrive(files=[photo("p1")]),
                           store=store, render_budget=0)
    assert res["ok"] is True and res["inserted"] == 0
    assert store.assets["p1"]["reject_reason"] == "operator_hold"
    assert _journals(fenced)[0]["local_state"] == "complete"
