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


def test_audio_path_empty_holds_when_ops_asset_absent(monkeypatch):
    monkeypatch.delenv("AGENT_STORY_MUSIC_DIR", raising=False)
    sel = sm.select(sm.SHELF_HYPE)  # stub has metadata but no real file path
    assert sm.audio_path_for(sel) == ""  # burn step must HOLD, not post silently
    assert sm.audio_path_for(sm.select(sm.SHELF_NONE)) == ""


# ---- the directory (ops) library: AGENT_STORY_MUSIC_DIR ---------------------
import json  # noqa: E402


def _write_library(tmp_path, tracks, *, files=(), manifest_text=None):
    """Build a music dir: manifest.json (or raw manifest_text) + fake audio files."""
    for name in files:
        (tmp_path / name).write_bytes(b"ID3fakeaudio")
    mpath = tmp_path / "manifest.json"
    if manifest_text is not None:
        mpath.write_text(manifest_text, encoding="utf-8")
    else:
        mpath.write_text(json.dumps({"tracks": tracks}), encoding="utf-8")
    return str(tmp_path)


def test_dir_library_valid_manifest_loads_and_selects_hype(tmp_path, monkeypatch):
    root = _write_library(tmp_path, [
        {"track_id": "h1", "shelf": "hype", "title": "Big Drums 01", "bpm": 128,
         "file": "h1.mp3", "license_ref": "artlist:LIC-2026-H1"},
        {"track_id": "c1", "shelf": "chill", "title": "Chill 01",
         "file": "c1.mp3", "license_ref": "artlist:LIC-2026-C1"},
    ], files=("h1.mp3", "c1.mp3"))
    monkeypatch.setenv("AGENT_STORY_MUSIC_DIR", root)
    monkeypatch.delenv("STORY_STUDIO_MUSIC_SHELF", raising=False)
    lib = sm.default_library()
    assert isinstance(lib, sm.DirMusicLibrary)
    sel = sm.select(shelf=None)  # default shelf = hype
    assert sel.shelf == sm.SHELF_HYPE
    assert sel.track_id == "h1"
    assert sel.license_ref == "artlist:LIC-2026-H1"
    assert sel.held is False
    # the burn step gets a real absolute path -> the studio hold does NOT trip
    path = sm.audio_path_for(sel)
    assert path == str(tmp_path / "h1.mp3")
    assert path.startswith("/")


def test_dir_library_missing_file_excluded(tmp_path, monkeypatch):
    root = _write_library(tmp_path, [
        {"track_id": "gone", "shelf": "hype", "file": "not_on_disk.mp3",
         "license_ref": "artlist:LIC-GONE"},
    ], files=())
    monkeypatch.setenv("AGENT_STORY_MUSIC_DIR", root)
    lib = sm.default_library()
    # zero valid tracks -> stub fallback, so behavior is exactly today's (hold at burn)
    assert isinstance(lib, sm.StubMusicLibrary)
    assert not isinstance(lib, sm.DirMusicLibrary)
    assert sm.DirMusicLibrary(root).pick(sm.SHELF_HYPE) is None


def test_dir_library_missing_license_ref_excluded(tmp_path, monkeypatch):
    root = _write_library(tmp_path, [
        {"track_id": "nolic", "shelf": "hype", "file": "nolic.mp3"},          # no ref
        {"track_id": "blank", "shelf": "hype", "file": "nolic.mp3",
         "license_ref": "   "},                                                # blank ref
        {"track_id": "ok", "shelf": "hype", "file": "ok.mp3",
         "license_ref": "artlist:LIC-OK"},
    ], files=("nolic.mp3", "ok.mp3"))
    monkeypatch.setenv("AGENT_STORY_MUSIC_DIR", root)
    lib = sm.default_library()
    assert isinstance(lib, sm.DirMusicLibrary)
    ids = {t.track_id for t in lib._tracks}
    assert ids == {"ok"}  # unlicensed rows never enter the library
    sel = sm.select(sm.SHELF_HYPE)
    assert sel.track_id == "ok" and sel.license_ref == "artlist:LIC-OK"


def test_dir_library_malformed_manifest_falls_back_to_stub(tmp_path, monkeypatch):
    root = _write_library(tmp_path, None, manifest_text="{not valid json !!!")
    monkeypatch.setenv("AGENT_STORY_MUSIC_DIR", root)
    lib = sm.default_library()  # must not raise
    assert isinstance(lib, sm.StubMusicLibrary)
    assert not isinstance(lib, sm.DirMusicLibrary)
    sel = sm.select(sm.SHELF_HYPE)
    assert sel.held is False and sel.track_id  # stub metadata, exactly as today
    assert sm.audio_path_for(sel) == ""        # ... and the burn step still HOLDS


def test_dir_library_manifest_without_tracks_list_falls_back(tmp_path, monkeypatch):
    root = _write_library(tmp_path, None, manifest_text='{"nope": 1}')
    monkeypatch.setenv("AGENT_STORY_MUSIC_DIR", root)
    assert not isinstance(sm.default_library(), sm.DirMusicLibrary)


def test_env_unset_keeps_stub(monkeypatch):
    monkeypatch.delenv("AGENT_STORY_MUSIC_DIR", raising=False)
    lib = sm.default_library()
    assert isinstance(lib, sm.StubMusicLibrary)
    assert not isinstance(lib, sm.DirMusicLibrary)


def test_default_library_cache_reloads_on_env_change(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_STORY_MUSIC_DIR", raising=False)
    assert not isinstance(sm.default_library(), sm.DirMusicLibrary)
    root = _write_library(tmp_path, [
        {"track_id": "h1", "shelf": "hype", "file": "h1.mp3",
         "license_ref": "artlist:LIC-2026-H1"},
    ], files=("h1.mp3",))
    monkeypatch.setenv("AGENT_STORY_MUSIC_DIR", root)
    assert isinstance(sm.default_library(), sm.DirMusicLibrary)  # cache rebuilt
    monkeypatch.delenv("AGENT_STORY_MUSIC_DIR", raising=False)
    assert not isinstance(sm.default_library(), sm.DirMusicLibrary)


def test_dir_library_pick_deterministic_by_seed(tmp_path, monkeypatch):
    root = _write_library(tmp_path, [
        {"track_id": "h1", "shelf": "hype", "file": "h1.mp3",
         "license_ref": "artlist:LIC-1"},
        {"track_id": "h2", "shelf": "hype", "file": "h2.mp3",
         "license_ref": "artlist:LIC-2"},
    ], files=("h1.mp3", "h2.mp3"))
    lib = sm.DirMusicLibrary(root)
    a = sm.select(sm.SHELF_HYPE, library=lib, seed="req-7")
    b = sm.select(sm.SHELF_HYPE, library=lib, seed="req-7")
    assert a.track_id == b.track_id
