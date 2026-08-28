"""
Story Studio Wave 2: the licensed music bed engine. Honesty rails: licensed bed not
trending audio; every bed carries track_id + license_ref; a template can NEVER
default to chill; 'none' carries neither field; an empty shelf HOLDS.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_music as sm  # noqa: E402


def test_default_shelf_is_hype_never_chill(monkeypatch):
    monkeypatch.delenv("STORY_STUDIO_MUSIC_SHELF", raising=False)
    assert sm.default_shelf() == sm.SHELF_HYPE


def test_config_cannot_default_to_chill(monkeypatch):
    # even if someone sets chill in env, the default is coerced to hype.
    monkeypatch.setenv("STORY_STUDIO_MUSIC_SHELF", "chill")
    assert sm.default_shelf() == sm.SHELF_HYPE
    sel = sm.select(shelf=None)
    assert sel.shelf == sm.SHELF_HYPE


def test_hype_bed_carries_track_id_and_license_ref():
    sel = sm.select(sm.SHELF_HYPE)
    assert sel.track_id
    assert sel.license_ref
    assert "licensed" in sel.note.lower()
    assert "trending" in sel.note.lower()  # honesty note names the limitation


def test_explicit_chill_optout_is_honored():
    # a coach can pick chill explicitly (the opt-out); it is not a default.
    sel = sm.select(sm.SHELF_CHILL)
    assert sel.shelf == sm.SHELF_CHILL
    assert sel.track_id and sel.license_ref


def test_none_carries_neither_field():
    sel = sm.select(sm.SHELF_NONE)
    assert sel.shelf == sm.SHELF_NONE
    assert sel.track_id == ""
    assert sel.license_ref == ""
    assert sel.note == ""
    assert sel.held is False


def test_empty_shelf_holds_render_not_silent():
    empty = sm.StubMusicLibrary(tracks=[])  # ops library not curated
    sel = sm.select(sm.SHELF_HYPE, library=empty)
    assert sel.held is True
    assert sel.hold_reason
    assert sel.track_id == ""  # never fabricates a track


def test_deterministic_pick_by_seed():
    lib = sm.StubMusicLibrary(tracks=[
        sm.Track("h1", "lic1", sm.SHELF_HYPE),
        sm.Track("h2", "lic2", sm.SHELF_HYPE),
    ])
    a = sm.select(sm.SHELF_HYPE, library=lib, seed="req-42")
    b = sm.select(sm.SHELF_HYPE, library=lib, seed="req-42")
    assert a.track_id == b.track_id


def test_audio_path_empty_holds_when_ops_asset_absent():
    sel = sm.select(sm.SHELF_HYPE)  # stub has metadata but no real file path
    assert sm.audio_path_for(sel) == ""  # burn step must HOLD, not post silently
    assert sm.audio_path_for(sm.select(sm.SHELF_NONE)) == ""
