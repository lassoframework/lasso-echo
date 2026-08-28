"""
Intake ingest tests. Fully OFFLINE: fake R2, injected converter/phash/moderator (no
Pillow, no network). Asserts: flag OFF no-op; dedupe by hash; the HEIC path converts
to JPG; the client note lands as the drafter's .txt sidecar; a bad file dead-letters
with ONE ops alert and the loop continues; a re-run is idempotent (manifest).
"""

import io
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


# ---- truncated-but-decodable JPEG is SALVAGED, not dead-lettered ------------------
# These two tests use the REAL default converter (converter=None) so the PIL decode
# path itself is exercised: a mobile-Safari/interrupted upload commonly cuts the
# final ~100 bytes off an otherwise-intact JPEG, and a client photo must never be
# silently lost when it is still decodable.
def _real_jpeg_bytes(px=160):
    """A real JPEG a few KB big (noisy pixels so cutting the tail leaves the
    header and most scan data intact)."""
    from PIL import Image
    img = Image.new("RGB", (px, px))
    img.putdata([((x * 7) % 256, (x * 13) % 256, (x * 29) % 256)
                 for x in range(px * px)])
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


def test_truncated_jpeg_is_salvaged_not_deadlettered(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch, tmp_path)
    from PIL import Image, ImageFile
    r2 = FakeR2()
    _seed(r2, name="20260825T155821Z_photo.jpeg", data=_real_jpeg_bytes()[:-100])
    out = intake_ingest.process_all(r2=r2, converter=None,   # REAL PIL converter
                                    phash=_fake_phash, moderator=_pass_all)
    assert out["gyma"]["accepted"] == 1
    assert out["gyma"]["deadlettered"] == 0
    assert not any(k.startswith("intake/gyma/deadletter/") for k in r2.objects)
    # the salvage is LOUD in the log, with the unprocessed byte count
    assert "[intake-ingest] salvaged truncated image 20260825T155821Z_photo.jpeg" \
        in capsys.readouterr().out
    # the filed file is a clean re-encoded JPEG: full decode works WITHOUT the flag
    lib = tmp_path / "library" / "gyma"
    filed = Image.open(lib / "20260825T155821Z_photo.jpg")
    filed.load()                                        # no OSError = clean file
    # the process-global Pillow flag was restored, never left on
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False


def test_undecodable_bytes_still_deadletter(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    r2 = FakeR2()
    _seed(r2, name="20260825T160000Z_junk.jpg",
          data=b"this is not an image at all, just garbage bytes" * 4)
    out = intake_ingest.process_all(r2=r2, converter=None,   # REAL PIL converter
                                    phash=_fake_phash, moderator=_pass_all)
    assert out["gyma"]["deadlettered"] == 1
    assert out["gyma"]["accepted"] == 0
    assert any(k.startswith("intake/gyma/deadletter/") for k in r2.objects)
    assert len([n for n in rec.notices if "dead-lettered" in n]) == 1
    assert not (tmp_path / "library" / "gyma").exists()   # nothing filed


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


# ---- audit #4: a moderation reject raises an ops alert (false-positive visibility) -
def test_moderation_reject_raises_ops_alert(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    r2 = FakeR2()
    _seed(r2)
    poster = RecordingPoster()
    out = _run(r2, poster=poster,
               moderator=lambda d, n: (False, "possible face without consent"))
    assert out["gyma"]["flagged"] == 1
    # the client-facing notice is still posted AND an ops alert now fires so a false
    # positive is visible instead of the photo silently vanishing into review/.
    assert any("review" in a.lower() and "false positive" in a.lower()
               for a in rec.notices)


# ---- audit #5: one gym's failure never aborts ingest for the others ---------------
def test_one_gym_failure_does_not_abort_others(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)

    class ExplodingListR2(FakeR2):
        def list_keys(self, prefix):
            # gymbad's per-client pass blows up on its incoming list; every other
            # gym must still be processed.
            if prefix == "intake/gymbad/incoming/":
                raise RuntimeError("R2 list failed for gymbad")
            return super().list_keys(prefix)

    r2 = ExplodingListR2()
    _seed(r2, client="gymbad", name="20260702T100000Z_x.jpg")
    _seed(r2, client="gymgood", name="20260702T100001Z_y.jpg")
    out = _run(r2)
    # gymgood still landed its photo; gymbad recorded an error, did not sink the pass.
    assert out["gymgood"]["accepted"] == 1
    assert "error" in out["gymbad"]
    assert any("ABORTED for gymbad" in a for a in rec.notices)


# ---- audit #3: a whole-batch dead-letter fires ONE loud escalation alert ----------
def test_whole_batch_deadletter_escalates(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)

    def always_explodes(data, name):
        raise ValueError("no decoder in image")  # the pillow-heif/ffmpeg-missing symptom

    r2 = FakeR2()
    for i in range(3):
        _seed(r2, name=f"20260702T10000{i}Z_p.heic", data=b"HEICBYTES%d" % i)
    out = intake_ingest.process_all(r2=r2, converter=always_explodes,
                                    phash=_fake_phash, moderator=_pass_all)
    assert out["gyma"]["deadlettered"] == 3 and out["gyma"]["accepted"] == 0
    # exactly one loud BATCH FAILURE escalation (distinct from the per-file alerts)
    assert len([a for a in rec.notices if "BATCH FAILURE" in a]) == 1
