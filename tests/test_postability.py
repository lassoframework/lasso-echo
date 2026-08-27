"""Postability gate tests (spec §2.3): the 1.59 GB full episode is rejected,
the 12 MB audiogram is accepted, and an UNPROBED asset is never selectable
(fail closed)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import podcast_index as idx
from agent import podcast_selector as sel
from tests.podcast_fakes import FakeStore, make_asset


def test_full_episode_1_59gb_rejected():
    # kind alone already kills it (full_video is NEVER postable)...
    postable, reason = idx.postability("full_video", 1_590_000_000, 3600, "16:9")
    assert postable is False
    assert reason == idx.REJECT_KIND
    # ...and even a mislabeled 'clip' of that size dies on the 900 MB ceiling.
    postable, reason = idx.postability("clip", 1_590_000_000)
    assert postable is False
    assert reason == idx.REJECT_SIZE


def test_12mb_audiogram_accepted_once_probed():
    postable, reason = idx.postability("audiogram", 12_000_000, 45.0, "1:1")
    assert postable is True
    assert reason is None


def test_unprobed_is_null_not_true():
    postable, reason = idx.postability("clip", 248_000_000)  # no duration yet
    assert postable is None
    assert reason is None


def test_probed_gate_edges():
    assert idx.postability("clip", 100, 2.9, "9:16") == (False, idx.REJECT_DURATION)
    assert idx.postability("clip", 100, 90.1, "9:16") == (False, idx.REJECT_DURATION)
    assert idx.postability("clip", 100, 3.0, "9:16") == (True, None)
    assert idx.postability("clip", 100, 90.0, "1:1") == (True, None)
    assert idx.postability("clip", 100, 45.0, "16:9") == (False, idx.REJECT_ASPECT)
    assert idx.postability("clip", 100, 45.0, "other") == (False, idx.REJECT_ASPECT)
    assert idx.postability("notes", 100, 45.0, "9:16") == (False, idx.REJECT_KIND)
    assert idx.postability("audio", 100, 45.0, "9:16") == (False, idx.REJECT_KIND)


def test_aspect_of_tolerates_encoder_rounding():
    assert idx.aspect_of(1080, 1920) == "9:16"
    assert idx.aspect_of(1088, 1920) == "9:16"   # encoder padding still reads 9:16
    assert idx.aspect_of(1080, 1080) == "1:1"
    assert idx.aspect_of(1920, 1080) == "16:9"
    assert idx.aspect_of(1234, 777) == "other"
    assert idx.aspect_of(None, 0) == "other"     # unknowable -> fails the gate


def test_unprobed_asset_not_selectable(monkeypatch):
    alerts = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert",
                        lambda msg, **kw: alerts.append(msg))
    store = FakeStore([
        make_asset(fid="unprobed", duration=None, width=None, height=None,
                   aspect=None, postable=None),                 # never selectable
        make_asset(fid="rejected", postable=False, reject=idx.REJECT_SIZE),
    ])
    assert sel.pick_clip(store=store) is None
    assert len(alerts) == 1  # the one deduped pool-empty alert


def test_postable_true_is_selectable():
    store = FakeStore([make_asset(fid="good", postable=True)])
    picked = sel.pick_clip(store=store)
    assert picked is not None and picked["id"] == "good"
