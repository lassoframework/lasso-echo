"""
Ingest worker hardening (launch hardening Part 3). Fully OFFLINE: fake R2,
injected converter/phash/moderator, injected ffmpeg runner. Asserts: the same
file uploaded twice lands once (raw-bytes dedupe, converter-independent); a
conversion archives the ORIGINAL before the incoming object is deleted; a
zero-byte upload quarantines with a specific ops alert; a corrupt file
dead-letters from the bytes already in memory; the worker survives a whole bad
batch and still accepts the good files; MOV remuxes to MP4 when ffmpeg runs and
passes through unchanged when it cannot.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, intake_ingest, ops_alerts  # noqa: E402


class FakeR2:
    def __init__(self):
        self.objects = {}

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get_bytes(self, key):
        return self.objects[key]

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data

    def delete(self, key):
        self.objects.pop(key, None)


def _fake_converter(data, name):
    if name.lower().endswith((".heic", ".heif")):
        return b"JPG:" + data, os.path.splitext(name)[0] + ".jpg"
    return data, name


def _fake_phash(data, name):
    return "ph:" + data[:8].hex()


def _pass_all(data, name):
    return True, ""


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path / "library"))


def _wire_alerts(monkeypatch):
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: fired.append(m))
    return fired


def _seed(r2, name, data, client="gyma"):
    r2.put_bytes(f"intake/{client}/incoming/{name}", data)


def _run(r2, converter=None, moderator=None):
    return intake_ingest.process_all(r2=r2, poster=None,
                                     converter=converter or _fake_converter,
                                     phash=_fake_phash,
                                     moderator=moderator or _pass_all)


# ---- 1. duplicate upload dedupes (raw bytes, before any conversion) ------------
def test_duplicate_upload_lands_once(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2, "20260714T090000Z_a.jpg", b"SAMEBYTES")
    out1 = _run(r2)
    assert out1["gyma"]["accepted"] == 1
    # the identical file arrives again under a NEW name (a re-text, a re-upload)
    _seed(r2, "20260714T100000Z_b.jpg", b"SAMEBYTES")
    out2 = _run(r2)
    assert out2["gyma"]["duplicates"] == 1
    assert out2["gyma"]["accepted"] == 0
    lib = tmp_path / "library" / "gyma"
    files = [n for n in os.listdir(lib) if n.endswith(".jpg")]
    assert len(files) == 1                          # landed exactly once


# ---- 2. a conversion keeps the original ----------------------------------------
def test_conversion_archives_the_original(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2, "20260714T090000Z_kitchen.heic", b"HEICBYTES")
    out = _run(r2)
    assert out["gyma"]["accepted"] == 1
    # the converted JPG is in the library
    lib = tmp_path / "library" / "gyma"
    assert (lib / "20260714T090000Z_kitchen.jpg").read_bytes() == b"JPG:HEICBYTES"
    # the UNTOUCHED original is archived in R2
    assert r2.objects["intake/gyma/originals/20260714T090000Z_kitchen.heic"] == b"HEICBYTES"
    # and the incoming object is gone
    assert "intake/gyma/incoming/20260714T090000Z_kitchen.heic" not in r2.objects


def test_unconverted_file_archives_nothing(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2, "20260714T090000Z_a.jpg", b"PLAINJPG")
    _run(r2)
    assert not any(k.startswith("intake/gyma/originals/") for k in r2.objects)


# ---- 3. zero-byte upload quarantines with a specific alert ---------------------
def test_zero_byte_quarantines_with_alert(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    fired = _wire_alerts(monkeypatch)
    r2 = FakeR2()
    _seed(r2, "20260714T090000Z_empty.jpg", b"")
    out = _run(r2)
    assert out["gyma"]["deadlettered"] == 1
    assert out["gyma"]["accepted"] == 0
    assert "intake/gyma/deadletter/20260714T090000Z_empty.jpg" in r2.objects
    assert len(fired) == 1 and "zero-byte" in fired[0]
    # re-run: incoming is empty (the file was consumed into deadletter), so
    # there is nothing to process and no second alert
    out2 = _run(r2)
    assert "gyma" not in (out2 or {})
    assert len(fired) == 1


# ---- 4. corrupt file quarantines (converter blows up) and worker continues ------
def test_corrupt_file_quarantines_and_batch_survives(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    fired = _wire_alerts(monkeypatch)
    r2 = FakeR2()
    _seed(r2, "20260714T090000Z_bad.heic", b"NOTREALHEIC")
    _seed(r2, "20260714T091000Z_good.jpg", b"GOODBYTES")
    _seed(r2, "20260714T092000Z_empty.jpg", b"")
    _seed(r2, "20260714T093000Z_also_good.jpg", b"MOREGOOD")

    def _fragile(data, name):
        if name.endswith(".heic"):
            raise ValueError("corrupt HEIC container")
        return data, name

    out = _run(r2, converter=_fragile)
    stats = out["gyma"]
    # the good files landed despite the bad batch
    assert stats["accepted"] == 2
    # the corrupt file dead-lettered with its bytes intact, the empty quarantined
    assert stats["deadlettered"] == 2
    assert r2.objects["intake/gyma/deadletter/20260714T090000Z_bad.heic"] == b"NOTREALHEIC"
    assert "intake/gyma/deadletter/20260714T092000Z_empty.jpg" in r2.objects
    # one alert per quarantined file, none for the good ones
    assert len(fired) == 2
    # nothing left in incoming
    assert not any(k.startswith("intake/gyma/incoming/") for k in r2.objects)


# ---- 5. MOV -> MP4 remux ---------------------------------------------------------
def test_mov_remuxes_to_mp4_when_ffmpeg_runs():
    def _fake_runner(cmd, check, capture_output, timeout):
        # the dst path is the last argument; "remux" by writing tagged bytes
        with open(cmd[-1], "wb") as fh:
            with open(cmd[cmd.index("-i") + 1], "rb") as src:
                fh.write(b"MP4:" + src.read())

    out = intake_ingest._remux_mov(b"MOVBYTES", "clip.mov",
                                   runner=_fake_runner, which=lambda n: "/usr/bin/ffmpeg")
    assert out == (b"MP4:MOVBYTES", "clip.mp4")


def test_mov_passes_through_when_ffmpeg_missing():
    out = intake_ingest._remux_mov(b"MOVBYTES", "clip.mov", which=lambda n: None)
    assert out is None                     # caller keeps the playable MOV as is


def test_mov_passes_through_when_remux_fails():
    def _boom(cmd, check, capture_output, timeout):
        raise RuntimeError("ffmpeg exploded")

    out = intake_ingest._remux_mov(b"MOVBYTES", "clip.mov",
                                   runner=_boom, which=lambda n: "/usr/bin/ffmpeg")
    assert out is None
