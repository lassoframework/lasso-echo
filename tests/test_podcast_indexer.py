"""Indexer job tests (spec §2.4): degrades cleanly without the SA key,
idempotent re-runs, vanished Drive ids marked removed_from_drive, budgeted
probe pass writes real data back, and probe/selector columns survive a
re-index."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import podcast_index as idx
from agent.jobs import index_podcast_library as job
from tests.podcast_fakes import (FakeDrive, FakeStore, doc, folder, video)


def _tree():
    return [
        folder("root", "Podcast Episodes"),
        folder("f140", "140", "root"),
        folder("promo140", "Promo (Canva, Reels, Audiogram)", "f140"),
        doc("doc140", "GMMS 140", "f140"),
        video("full140", "140-Video.mp4", "f140", size=1_590_000_000),
        video("clip140s1", "GMMS-140-S1.mp4", "promo140", size=248_000_000),
        video("ag140", "140-audiogram.mp4", "promo140", size=12_000_000),
    ]


class _NoSlack:
    def post_notice(self, text):
        return None


def _run(drive, store, probe_fn=None, budget=0):
    return job.run(drive=drive, store=store, probe_fn=probe_fn,
                   log=lambda m: None, now_iso="2026-08-28T00:00:00+00:00",
                   poster=_NoSlack(), probe_budget=budget)


def test_no_sa_key_noops_with_one_line():
    lines = []
    out = job.run(drive=FakeDrive(up=False), store=FakeStore(),
                  log=lines.append, poster=_NoSlack())
    assert out["ok"] is False
    assert "unarmed" in lines[0]
    assert len(lines) == 1  # exactly one log line, nothing else happened


def test_no_store_noops():
    out = job.run(drive=FakeDrive(_tree()), store=FakeStore(up=False),
                  log=lambda m: None, poster=_NoSlack())
    assert out["ok"] is False
    assert "store" in out["reason"]


def test_index_inserts_then_rerun_is_idempotent():
    store = FakeStore()
    drive = FakeDrive(_tree())
    out = _run(drive, store)
    assert out["ok"] and out["inserted"] == 4
    assert store.assets["full140"]["postable"] is False   # never postable
    assert store.assets["full140"]["reject_reason"] == idx.REJECT_KIND
    assert store.assets["clip140s1"]["postable"] is None   # unprobed, unselectable
    assert store.assets["clip140s1"]["notes_doc_id"] == "doc140"

    # Re-run, nothing changed in Drive: zero inserts, zero updates.
    store.updates.clear()
    out2 = _run(drive, store)
    assert out2["inserted"] == 0 and out2["updated"] == 0
    assert store.updates == []


def test_vanished_drive_id_marked_removed():
    store = FakeStore()
    _run(FakeDrive(_tree()), store)
    # the audiogram disappears from Drive
    smaller = [f for f in _tree() if f.id != "ag140"]
    out = _run(FakeDrive(smaller), store)
    assert out["removed"] == 1
    assert store.assets["ag140"]["postable"] is False
    assert store.assets["ag140"]["reject_reason"] == idx.REJECT_REMOVED
    # idempotent: a third run does not re-mark it
    assert _run(FakeDrive(smaller), store)["removed"] == 0


def test_reappeared_asset_is_restored():
    store = FakeStore()
    _run(FakeDrive(_tree()), store)
    smaller = [f for f in _tree() if f.id != "ag140"]
    _run(FakeDrive(smaller), store)
    assert store.assets["ag140"]["reject_reason"] == idx.REJECT_REMOVED
    _run(FakeDrive(_tree()), store)
    # back in the walk: the gate is recomputed (unprobed -> null again)
    assert store.assets["ag140"]["reject_reason"] is None
    assert store.assets["ag140"]["postable"] is None


def test_probe_pass_writes_back_and_gates():
    store = FakeStore()
    drive = FakeDrive(_tree())

    def probe(path):
        return {"duration_sec": 42.0, "width": 1080, "height": 1920}

    out = _run(drive, store, probe_fn=probe, budget=10)
    assert out["probed"] == 2               # the clip + the audiogram
    assert out["newly_postable"] == 2
    assert store.assets["clip140s1"]["postable"] is True
    assert store.assets["clip140s1"]["aspect"] == "9:16"
    assert store.assets["ag140"]["duration_sec"] == 42.0
    # the full episode was never downloaded or probed
    assert "full140" not in drive.downloads

    # A re-index never clobbers probe data or selector columns.
    store.assets["clip140s1"]["used_count"] = 3
    store.updates.clear()
    _run(drive, store, probe_fn=probe, budget=10)
    assert store.assets["clip140s1"]["postable"] is True
    assert store.assets["clip140s1"]["duration_sec"] == 42.0
    assert store.assets["clip140s1"]["used_count"] == 3


def test_probe_budget_bounds_the_pass():
    store = FakeStore()
    drive = FakeDrive(_tree())
    out = _run(drive, store,
               probe_fn=lambda p: {"duration_sec": 40.0, "width": 1080,
                                   "height": 1080},
               budget=1)
    assert out["probed"] == 1
    # the second candidate stays unprobed (and unselectable) until tomorrow
    unprobed = [a for a in store.assets.values()
                if a.get("kind") in idx.POSTABLE_KINDS
                and a.get("duration_sec") is None]
    assert len(unprobed) == 1


def test_probe_failure_stays_unprobed_never_postable():
    store = FakeStore()
    out = _run(FakeDrive(_tree()), store, probe_fn=lambda p: None, budget=10)
    assert out["probed"] == 0
    assert store.assets["clip140s1"]["postable"] is None  # fail closed


def test_notes_link_stamps_every_clip_of_an_episode():
    # An episode with a Doc: EVERY clip/audiogram of it carries notes_doc_id.
    tree = _tree() + [
        video("clip140s2", "GMMS-140-S2.mp4", "promo140", size=200_000_000),
    ]
    store = FakeStore()
    _run(FakeDrive(tree), store)
    assert store.assets["clip140s1"]["notes_doc_id"] == "doc140"
    assert store.assets["clip140s2"]["notes_doc_id"] == "doc140"
    assert store.assets["ag140"]["notes_doc_id"] == "doc140"


def test_reindex_links_notes_added_after_the_clip():
    # A linking-GAP repair: clips indexed BEFORE the Doc existed get linked on a
    # later re-index once the Doc is in the walk — idempotently.
    no_doc = [f for f in _tree() if f.id != "doc140"]
    store = FakeStore()
    _run(FakeDrive(no_doc), store)
    assert store.assets["clip140s1"]["notes_doc_id"] is None  # no Doc yet
    # Doc appears; a standalone re-index stamps notes_doc_id on the clip.
    out = _run(FakeDrive(_tree()), store)
    assert store.assets["clip140s1"]["notes_doc_id"] == "doc140"
    assert out["updated"] >= 1
    # Idempotent: a third run with the Doc present writes nothing new.
    store.updates.clear()
    _run(FakeDrive(_tree()), store)
    assert store.updates == []


def test_multi_doc_episode_links_deterministically():
    # An episode with TWO notes Docs: the SAME Doc (smallest id) links every run,
    # so a re-index never thrashes notes_doc_id back and forth.
    tree = _tree() + [doc("doc140b", "GMMS 140 alt notes", "f140")]
    store = FakeStore()
    _run(FakeDrive(tree), store)
    linked = store.assets["clip140s1"]["notes_doc_id"]
    assert linked == "doc140"  # 'doc140' < 'doc140b'
    store.updates.clear()
    _run(FakeDrive(tree), store)
    assert store.updates == []  # deterministic: nothing flips on re-run
