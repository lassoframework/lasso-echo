"""Classifier tests (spec §0 naming chaos + §2.2 rules): every observed naming
pattern classifies correctly, the episode number ALWAYS comes from the nearest
ancestor folder, and unknown names log + skip, never raise."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import podcast_index as idx
from agent.integrations.drive_client import DriveFile
from tests.podcast_fakes import audio, doc, folder, video


# ---- classify(): every naming pattern from the spec --------------------------

@pytest.mark.parametrize("name,mime,kind,clip_index", [
    # GMMS-{ep}-S{n}
    ("GMMS-140-S1.mp4", "video/mp4", "clip", 1),
    ("GMMS-129-S4.mp4", "video/mp4", "clip", 4),
    # GMMS-EP{ep}-S{n}
    ("GMMS-EP139-S3.mp4", "video/mp4", "clip", 3),
    ("GMMS-EP125-S2.mp4", "video/mp4", "clip", 2),
    # GMMS{ep}-S{n}
    ("GMMS136-S1.mp4", "video/mp4", "clip", 1),
    # underscore separators
    ("GMMS_137_S2.mp4", "video/mp4", "clip", 2),
    # audiogram
    ("140-audiogram.mp4", "video/mp4", "audiogram", None),
    ("141-Audiogram.mp4", "video/mp4", "audiogram", None),
    # full-episode conventions (all -> full_video, never postable)
    ("141-Video.mp4", "video/mp4", "full_video", None),
    ("140-Video.mp4", "video/mp4", "full_video", None),
    ("GMMS-104-V1.mp4", "video/mp4", "full_video", None),
    ("108-GMMS-V1.mp4", "video/mp4", "full_video", None),
    ("GMMS-VIDEO-98.mp4", "video/mp4", "full_video", None),
    ("86_GMMS.mp4", "video/mp4", "full_video", None),
    ("GMMS_82.mp4", "video/mp4", "full_video", None),
    ("GMMS-92.mp4", "video/mp4", "full_video", None),
    ("GMMS-61-V2.mp4", "video/mp4", "full_video", None),
    ("113-GMMS-V1.mp4", "video/mp4", "full_video", None),
    ("GMMS-103-VIDEO-V1.mp4", "video/mp4", "full_video", None),
    ("GMMS_121_V1.mp4", "video/mp4", "full_video", None),
    # mime-driven kinds
    ("GMMS 140", "application/vnd.google-apps.document", "notes", None),
    ("140-Audio.mp3", "audio/mpeg", "audio", None),
])
def test_every_observed_naming_pattern(name, mime, kind, clip_index):
    got_kind, got_index = idx.classify(name, mime)
    assert got_kind == kind
    assert got_index == clip_index


@pytest.mark.parametrize("name,mime", [
    ("thumbnail.png", "image/png"),
    ("promo-copy.txt", "text/plain"),
    ("weird-clip.mov", "video/quicktime"),  # not video/mp4 -> not placeable
    ("", ""),
])
def test_unknown_names_return_none_never_raise(name, mime):
    assert idx.classify(name, mime) == (None, None)


# ---- episode number: nearest ancestor folder, NEVER the filename -------------

def _tree():
    return [
        folder("root", "Podcast Episodes"),
        folder("f140", "140", "root"),
        folder("f141", "141", "root"),
        folder("promo140", "Promo (Canva, Reels, Audiogram)", "f140"),
        folder("f139", "139", "root"),
        folder("promo139", "Promo Materials", "f139"),
    ]


def test_episode_from_nearest_ancestor_folder():
    files = _tree()
    folders = {f.id: f for f in files}
    clip = video("c1", "GMMS-EP139-S3.mp4", "promo139")
    assert idx.episode_for(clip, folders) == 139


def test_folder_beats_a_lying_filename():
    # A clip NAMED 140 sitting in folder 141: the folder wins (filenames lie).
    files = _tree()
    folders = {f.id: f for f in files}
    liar = video("c2", "GMMS-140-S1.mp4", "f141")
    assert idx.episode_for(liar, folders) == 141


def test_no_numeric_ancestor_means_no_episode():
    files = [folder("root", "Podcast Episodes"), folder("misc", "Misc", "root")]
    folders = {f.id: f for f in files}
    orphan = video("c3", "GMMS-140-S1.mp4", "misc")
    assert idx.episode_for(orphan, folders) is None


# ---- build_rows: full-tree classification, skips logged not raised -----------

def test_build_rows_classifies_and_links_notes():
    files = _tree() + [
        doc("doc140", "GMMS 140", "f140"),
        audio("aud140", "140-Audio.mp3", "f140"),
        video("full140", "140-Video.mp4", "f140", size=1_590_000_000),
        video("clip140s1", "GMMS-140-S1.mp4", "promo140", size=248_000_000),
        video("ag140", "140-audiogram.mp4", "promo140", size=12_000_000),
        video("clip139s3", "GMMS-EP139-S3.mp4", "promo139", size=162_000_000),
        # unclassifiable + orphan: skipped, never raised
        DriveFile(id="png1", title="cover.png", mime_type="image/png",
                  size_bytes=1000, parent_id="promo140", modified_time=""),
    ]
    rows, skipped = idx.build_rows(files, now_iso="2026-08-27T00:00:00+00:00",
                                   log=lambda m: None)
    by_id = {r["id"]: r for r in rows}
    assert by_id["doc140"]["kind"] == "notes"
    assert by_id["aud140"]["kind"] == "audio"
    assert by_id["full140"]["kind"] == "full_video"
    assert by_id["clip140s1"]["kind"] == "clip"
    assert by_id["clip140s1"]["clip_index"] == 1
    assert by_id["ag140"]["kind"] == "audiogram"
    assert by_id["clip139s3"]["episode"] == 139
    # notes doc id fans out to every asset of the episode
    assert by_id["clip140s1"]["notes_doc_id"] == "doc140"
    assert by_id["full140"]["notes_doc_id"] == "doc140"
    assert by_id["clip139s3"]["notes_doc_id"] is None  # ep 139 has no doc here
    # the png was skipped, not raised
    assert any("cover.png" in t for t, _ in skipped)
    assert "png1" not in by_id
