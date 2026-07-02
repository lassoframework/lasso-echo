"""
Intake ingest tests. Fully OFFLINE: fake R2, injected converter/phash/moderator (no
Pillow, no network). Asserts: flag OFF no-op; dedupe by hash; the HEIC path converts
to JPG; the client note lands as the drafter's .txt sidecar; a bad file dead-letters
with ONE ops alert and the loop continues; a re-run is idempotent (manifest).
"""

import json
import os
import sys

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


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _fake_converter(data, name):
    """Records the HEIC path without Pillow: .heic renames to .jpg, bytes tagged."""
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


def _seed(r2, client="gyma", name="20260702T100000Z_photo.jpg", data=b"IMGBYTES",
          note="Saturday open house"):
    r2.put_bytes(f"intake/{client}/incoming/{name}", data)
    stamp = name.split("_", 1)[0]
    r2.put_bytes(f"intake/{client}/incoming/{stamp}_upload.json",
                 json.dumps({"note": note, "client": client,
                             "timestamp": stamp, "filenames": [name]}).encode())


def _run(r2, poster=None, moderator=None):
    return intake_ingest.process_all(r2=r2, poster=poster,
                                     converter=_fake_converter, phash=_fake_phash,
                                     moderator=moderator or _pass_all)


# ---- flag OFF -> dormant no-op ---------------------------------------------------
def test_flag_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_INTAKE_ENABLED", raising=False)
    r2 = FakeR2()
    _seed(r2)
    assert intake_ingest.process_all(r2=r2) is None
    assert any("incoming" in k for k in r2.objects)   # untouched


# ---- accepted media files into the library with the note sidecar ----------------
def test_accepts_media_and_attaches_note(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2)
    out = _run(r2)
    assert out["gyma"]["accepted"] == 1
    lib = tmp_path / "library" / "gyma"
    assert (lib / "20260702T100000Z_photo.jpg").read_bytes() == b"IMGBYTES"
    assert (lib / "20260702T100000Z_photo.txt").read_text() == "Saturday open house"
    assert not any(k.startswith("intake/gyma/incoming/") and not k.endswith(".json")
                   for k in r2.objects)               # incoming media consumed


# ---- HEIC path -------------------------------------------------------------------
def test_heic_converts_to_jpg(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2, name="20260702T110000Z_kitchen.heic", data=b"HEICBYTES")
    out = _run(r2)
    assert out["gyma"]["accepted"] == 1
    lib = tmp_path / "library" / "gyma"
    assert (lib / "20260702T110000Z_kitchen.jpg").read_bytes() == b"JPG:HEICBYTES"
    assert not (lib / "20260702T110000Z_kitchen.heic").exists()


# ---- dedupe ------------------------------------------------------------------------
def test_dedupe_by_hash(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2, name="20260702T100000Z_a.jpg", data=b"SAMEBYTES")
    _seed(r2, name="20260702T100001Z_b.jpg", data=b"SAMEBYTES")
    out = _run(r2)
    assert out["gyma"]["accepted"] == 1
    assert out["gyma"]["duplicates"] == 1
    lib = tmp_path / "library" / "gyma"
    media = [p for p in os.listdir(lib) if p.endswith(".jpg")]
    assert len(media) == 1                             # only one copy filed


# ---- moderation flag -> review/ + one notice --------------------------------------
def test_flagged_file_goes_to_review_with_notice(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2)
    poster = RecordingPoster()
    out = _run(r2, poster=poster, moderator=lambda d, n: (False, "possible face without consent"))
    assert out["gyma"]["flagged"] == 1
    assert any(k.startswith("intake/gyma/review/") for k in r2.objects)
    assert len(poster.notices) == 1
    assert "review" in poster.notices[0].lower()
    assert not (tmp_path / "library" / "gyma").exists()   # nothing filed


# ---- dead-letter + one ops alert, loop continues ----------------------------------
def test_deadletter_with_alert_and_continue(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)

    def exploding_converter(data, name):
        if b"BAD" in data:
            raise ValueError("corrupt media")
        return _fake_converter(data, name)

    r2 = FakeR2()
    _seed(r2, name="20260702T100000Z_bad.jpg", data=b"BAD")
    _seed(r2, name="20260702T100001Z_good.jpg", data=b"GOODBYTES")
    out = intake_ingest.process_all(r2=r2, converter=exploding_converter,
                                    phash=_fake_phash, moderator=_pass_all)
    assert out["gyma"]["deadlettered"] == 1
    assert out["gyma"]["accepted"] == 1                 # the good file still landed
    assert any(k.startswith("intake/gyma/deadletter/") for k in r2.objects)
    assert len([n for n in rec.notices if "dead-lettered" in n]) == 1


# ---- idempotent re-run --------------------------------------------------------------
def test_idempotent_rerun(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2)
    first = _run(r2)
    assert first["gyma"]["accepted"] == 1
    second = _run(r2)                                    # nothing left in incoming
    accepted_again = second.get("gyma", {}).get("accepted", 0)
    assert accepted_again == 0
    lib = tmp_path / "library" / "gyma"
    assert len([p for p in os.listdir(lib) if p.endswith(".jpg")]) == 1
