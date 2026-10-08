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
    provenance = json.loads(
        r2.objects["intake/gyma/review/20260702T100000Z_photo.json"])
    assert provenance["status"] == "review"
    assert provenance["original_key"].endswith("20260702T100000Z_photo.jpg")
    assert provenance["source_fingerprint"].startswith("source:sha256:")
    assert provenance["converted_fingerprint"].startswith("derived:sha256:")


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


# ---- source-byte fingerprint + no destructive pHash deletion (global no-reuse) ----
def _alerts(monkeypatch):
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **k: fired.append(msg))
    return fired


def test_accept_records_stable_source_fingerprint(monkeypatch, tmp_path):
    """The manifest records strong source identity plus explicit Drive alias."""
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2, data=b"IMGBYTES")
    out = _run(r2)
    assert out["gyma"]["accepted"] == 1
    manifest = json.loads(r2.objects["intake/gyma/manifest.json"])
    from agent import visual_fingerprint as vf
    fingerprint = vf.fingerprint(b"IMGBYTES")
    assert manifest["source_fingerprints"] == [fingerprint]
    assert manifest["source_fingerprint_aliases"] == {
        fingerprint: [f"source:md5:{vf.source_md5(b'IMGBYTES')}"]}
    assert manifest["phash"] == [_fake_phash(b"IMGBYTES", "ignored.jpg")]
    incoming_key = "intake/gyma/incoming/20260702T100000Z_photo.jpg"
    binding = manifest["asset_provenance"][incoming_key]
    assert binding["status"] == "accepted"
    assert binding["filename"] == "20260702T100000Z_photo.jpg"
    assert binding["asset_id"] is None
    assert binding["source_fingerprint"] == fingerprint
    assert binding["converted_fingerprint"].startswith("derived:sha256:")
    filed_sidecar = json.loads(
        (tmp_path / "library" / "gyma" /
         "20260702T100000Z_photo.json").read_text())
    assert filed_sidecar["original_key"] == incoming_key
    assert filed_sidecar["source_fingerprint"] == fingerprint


def test_pending_caption_persists_source_to_converted_binding(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    name = "20260702T100000Z_uncaptioned.jpg"
    media_key = f"intake/gyma/incoming/{name}"
    r2.put_bytes(media_key, b"UNCAPTIONED")
    r2.put_bytes("intake/gyma/incoming/20260702T100000Z_upload.json",
                 json.dumps({"note": "", "client": "gyma", "filenames": [name]}).encode())
    out = _run(r2)
    assert out["gyma"]["needs_caption"] == 1
    sidecar = json.loads(
        r2.objects["intake/gyma/pending_caption/20260702T100000Z_uncaptioned.json"])
    assert sidecar["status"] == "needs_caption"
    assert sidecar["original_key"] == media_key
    assert sidecar["current_key"].endswith("pending_caption/" + name)
    assert sidecar["source_fingerprint"].startswith("source:sha256:")
    assert sidecar["converted_fingerprint"].startswith("derived:sha256:")
    manifest = json.loads(r2.objects["intake/gyma/manifest.json"])
    assert manifest["asset_provenance"][media_key]["status"] == "needs_caption"


def test_phash_collision_holds_and_preserves_source_never_deletes(monkeypatch, tmp_path):
    """A perceptual hash is similarity, NOT identity. Two DIFFERENT photos that
    collide on pHash: the first files, the second is QUARANTINED to hold/ with its
    raw source bytes + an honest sidecar + one ops alert — never silently deleted."""
    _arm(monkeypatch, tmp_path)
    fired = _alerts(monkeypatch)
    r2 = FakeR2()
    _seed(r2, name="20260702T100000Z_a.jpg", data=b"PHOTO-A")
    _seed(r2, name="20260702T100001Z_b.jpg", data=b"PHOTO-B-DIFFERENT")
    out = intake_ingest.process_all(
        r2=r2, converter=_fake_converter,
        phash=lambda d, n: "collision", moderator=_pass_all)
    assert out["gyma"]["accepted"] == 1
    assert out["gyma"]["held"] == 1
    assert out["gyma"]["duplicates"] == 0
    # source preserved under hold/, incoming consumed
    assert r2.objects["intake/gyma/hold/20260702T100001Z_b.jpg"] == b"PHOTO-B-DIFFERENT"
    side = json.loads(r2.objects["intake/gyma/hold/20260702T100001Z_b.json"])
    assert side["status"] == "held_near_duplicate"
    from agent import visual_fingerprint as vf
    assert side["source_fingerprint"] == vf.fingerprint(b"PHOTO-B-DIFFERENT")
    assert side["source_fingerprint_aliases"] == [
        f"source:md5:{vf.source_md5(b'PHOTO-B-DIFFERENT')}"]
    assert side["similarity_alias"] == "perceptual:collision"
    assert not any(k.startswith("intake/gyma/incoming/")
                   and not k.endswith(".json") for k in r2.objects)
    # only the FIRST photo filed; nothing destroyed
    lib = tmp_path / "library" / "gyma"
    assert [p.name for p in lib.glob("*.jpg")] == ["20260702T100000Z_a.jpg"]
    assert any("HELD" in m for m in fired)


def test_exact_converted_dup_archives_raw_source_before_drop(monkeypatch, tmp_path):
    """Two different HEIC originals converting to the SAME JPG bytes: the second is
    a true converted-byte duplicate, but its RAW source is archived to originals/
    BEFORE the incoming object is deleted. No dedupe path destroys the only copy
    of a client's source."""
    _arm(monkeypatch, tmp_path)
    r2 = FakeR2()
    _seed(r2, name="20260702T100000Z_a.heic", data=b"HEIC-ONE")
    _seed(r2, name="20260702T100001Z_b.heic", data=b"HEIC-TWO")
    out = intake_ingest.process_all(
        r2=r2, converter=lambda _data, name: (b"SAME-JPG", name[:-5] + ".jpg"),
        phash=_fake_phash, moderator=_pass_all)
    assert out["gyma"]["accepted"] == 1
    assert out["gyma"]["duplicates"] == 1
    # both distinct raw sources survive in originals/ before either incoming
    # object is removed, even though their converted bytes are identical.
    originals = [k for k in r2.objects
                 if k.startswith("intake/gyma/originals/") and not k.endswith(".json")]
    assert {r2.objects[orig] for orig in originals} == {b"HEIC-ONE", b"HEIC-TWO"}


# ---- inventory mutation receipt protocol (guarded mode) -------------------------
# With AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED armed, every durable intake effect
# (admission, quarantine, thumbnails, sidecars, deletions, the manifest commit)
# runs through the begin/complete receipt fence in agent.local_inventory_mutation.
# These tests arm the fence against a throwaway SQLite + journal dir and a fake
# authority, and assert both that behavior is preserved (quarantine, lineage,
# originals never mutated in place) and that a hold is never swallowed.
import sqlite3  # noqa: E402
import uuid  # noqa: E402

from agent import local_inventory_mutation as mutation  # noqa: E402


class FakeAuthority:
    """Stands in for MutationAuthority: receipts shaped exactly as _receipt()
    requires; fail='begin'/'complete' simulates a lost acknowledgement."""

    def __init__(self, fail=None):
        self.fail = fail
        self.events = []
        self.pending = None

    def begin(self, request):
        self.events.append(("begin", request["kind"]))
        self.pending = dict(request, state="pending", generation=1,
                            result_digest=None,
                            begun_at="2026-10-08T00:00:00+00:00",
                            completed_at=None)
        if self.fail == "begin":
            raise mutation.MutationHold("ack_lost")
        return dict(self.pending)

    def complete(self, request, result_digest):
        self.events.append(("complete", request["kind"]))
        self.pending.update(state="complete", result_digest=result_digest,
                            completed_at="2026-10-08T00:00:01+00:00")
        if self.fail == "complete":
            raise mutation.MutationHold("ack_lost")
        return dict(self.pending)

    def close(self):
        pass


def _arm_fence(monkeypatch, tmp_path, authority):
    """Arm BOTH the intake flag and the mutation fence, fully offline."""
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_INVENTORY_MUTATION_EPOCH", str(uuid.uuid4()))
    db_file = tmp_path / "echo.db"
    sqlite3.connect(db_file).close()
    monkeypatch.setenv("AGENT_DB_PATH", str(db_file))
    monkeypatch.setattr(mutation.MutationAuthority, "from_environment",
                        lambda: authority)
    return tmp_path / "inventory-mutation-receipts"


def _journals(receipts_dir):
    return [json.loads(p.read_text())
            for p in sorted(receipts_dir.glob("*.json"))]


def test_unguarded_mode_writes_no_receipts(monkeypatch, tmp_path):
    """Fence OFF (default): identical legacy behavior, zero journal files."""
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    r2 = FakeR2()
    _seed(r2)
    out = _run(r2)
    assert out["gyma"]["accepted"] == 1
    assert not (tmp_path / "inventory-mutation-receipts").exists()


def test_guarded_acceptance_thumbnail_sidecar_and_manifest_receipts(
        monkeypatch, tmp_path):
    """Guarded admission: library file + note + provenance sidecar land, the
    incoming object is consumed, the manifest commits — and every durable step
    (admission, thumbnail, manifest) belongs to one COMPLETE batch receipt."""
    auth = FakeAuthority()
    receipts_dir = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2, data=_real_jpeg_bytes())   # real JPEG so the thumbnail path fires
    out = intake_ingest.process_all(r2=r2, converter=None, phash=_fake_phash,
                                    moderator=_pass_all)
    assert out["gyma"]["accepted"] == 1
    lib = tmp_path / "library" / "gyma"
    assert (lib / "20260702T100000Z_photo.jpg").exists()
    assert (lib / "20260702T100000Z_photo.txt").read_text() == "Saturday open house"
    # the thumbnail was generated and stored under the receipt fence
    assert any(k.startswith("intake/gyma/thumbs/") for k in r2.objects)
    # lineage survives guarding: the filed sidecar keeps the source identity
    filed = json.loads((lib / "20260702T100000Z_photo.json").read_text())
    assert filed["original_key"].endswith(
        "intake/gyma/incoming/20260702T100000Z_photo.jpg")
    assert filed["source_fingerprint"].startswith("source:sha256:")
    assert filed["status"] == "accepted"
    assert not any(k.startswith("intake/gyma/incoming/")
                   and not k.endswith(".json") for k in r2.objects)
    manifest = json.loads(r2.objects["intake/gyma/manifest.json"])
    binding = manifest["asset_provenance"][
        "intake/gyma/incoming/20260702T100000Z_photo.jpg"]
    assert binding["status"] == "accepted"
    assert binding["converted_fingerprint"].startswith("derived:sha256:")
    # every mutation journaled begin -> local effects -> complete
    records = _journals(receipts_dir)
    kinds = {r["kind"] for r in records}
    assert kinds == {"intake_batch"}
    assert all(r["local_state"] == "complete" for r in records)
    assert all(r["gym_id"] == "gyma" for r in records)
    begun = [k for ev, k in auth.events if ev == "begin"]
    completed = [k for ev, k in auth.events if ev == "complete"]
    assert begun == completed and len(begun) == len(records)


def test_guarded_zero_byte_quarantine_receipt(monkeypatch, tmp_path):
    """Guarded quarantine: a zero-byte upload still dead-letters with the
    specific alert, and the move belongs to a complete intake_batch receipt."""
    auth = FakeAuthority()
    receipts_dir = _arm_fence(monkeypatch, tmp_path, auth)
    fired = _alerts(monkeypatch)
    r2 = FakeR2()
    r2.put_bytes("intake/gyma/incoming/20260702T100000Z_empty.jpg", b"")
    out = _run(r2)
    assert out["gyma"]["deadlettered"] == 1
    assert "intake/gyma/deadletter/20260702T100000Z_empty.jpg" in r2.objects
    assert "intake/gyma/incoming/20260702T100000Z_empty.jpg" not in r2.objects
    assert any("zero-byte" in m for m in fired)
    records = _journals(receipts_dir)
    kinds = [r["kind"] for r in records]
    assert kinds == ["intake_batch"]
    assert all(r["local_state"] == "complete" for r in records)


def test_guarded_phash_hold_preserves_source_and_lineage(monkeypatch, tmp_path):
    """Guarded hold: a pHash collision is still a QUARANTINE, never a delete —
    raw source + honest sidecar under hold/, lineage intact, receipts complete."""
    auth = FakeAuthority()
    receipts_dir = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2, name="20260702T100000Z_a.jpg", data=b"PHOTO-A")
    _seed(r2, name="20260702T100001Z_b.jpg", data=b"PHOTO-B-DIFFERENT")
    out = intake_ingest.process_all(
        r2=r2, converter=_fake_converter,
        phash=lambda d, n: "collision", moderator=_pass_all)
    assert out["gyma"]["accepted"] == 1 and out["gyma"]["held"] == 1
    assert r2.objects["intake/gyma/hold/20260702T100001Z_b.jpg"] == \
        b"PHOTO-B-DIFFERENT"
    side = json.loads(r2.objects["intake/gyma/hold/20260702T100001Z_b.json"])
    assert side["status"] == "held_near_duplicate"
    from agent import visual_fingerprint as vf
    assert side["source_fingerprint"] == vf.fingerprint(b"PHOTO-B-DIFFERENT")
    assert side["similarity_alias"] == "perceptual:collision"
    records = _journals(receipts_dir)
    assert all(r["local_state"] == "complete" for r in records)
    assert {r["kind"] for r in records} == {"intake_batch"}


def test_guarded_raw_dedupe_delete_receipt(monkeypatch, tmp_path):
    """Guarded deletion: the second raw-identical upload is still dropped once
    (the survivor holds the bytes), and the delete is receipted."""
    auth = FakeAuthority()
    receipts_dir = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2, name="20260702T100000Z_a.jpg", data=b"SAMEBYTES")
    _seed(r2, name="20260702T100001Z_b.jpg", data=b"SAMEBYTES")
    out = _run(r2)
    assert out["gyma"]["accepted"] == 1 and out["gyma"]["duplicates"] == 1
    lib = tmp_path / "library" / "gyma"
    assert len([p for p in os.listdir(lib) if p.endswith(".jpg")]) == 1
    records = _journals(receipts_dir)
    assert {r["kind"] for r in records} == {"intake_batch"}
    assert all(r["local_state"] == "complete" for r in records)


def test_guarded_converted_dup_archives_originals_never_mutates_source(
        monkeypatch, tmp_path):
    """Guarded original admission: distinct HEIC originals converting to the
    same JPG both survive in originals/ (copied, never mutated in place)."""
    auth = FakeAuthority()
    receipts_dir = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2, name="20260702T100000Z_a.heic", data=b"HEIC-ONE")
    _seed(r2, name="20260702T100001Z_b.heic", data=b"HEIC-TWO")
    out = intake_ingest.process_all(
        r2=r2, converter=lambda _data, name: (b"SAME-JPG", name[:-5] + ".jpg"),
        phash=_fake_phash, moderator=_pass_all)
    assert out["gyma"]["accepted"] == 1 and out["gyma"]["duplicates"] == 1
    originals = [k for k in r2.objects
                 if k.startswith("intake/gyma/originals/")
                 and not k.endswith(".json")]
    assert {r2.objects[o] for o in originals} == {b"HEIC-ONE", b"HEIC-TWO"}
    assert all(r["local_state"] == "complete" for r in _journals(receipts_dir))


def test_guarded_begin_hold_is_never_swallowed_or_marked_processed(
        monkeypatch, tmp_path):
    """A lost begin ack leaves the exact mutation 'prepared' in the journal,
    aborts the pass loudly (per-client error), touches NO local supply, and the
    incoming object is NOT consumed. A retry is fenced by reconciliation."""
    auth = FakeAuthority(fail="begin")
    receipts_dir = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2)
    out = _run(r2)
    assert "error" in out["gyma"]
    # the source object is untouched; nothing filed; manifest never committed
    assert "intake/gyma/incoming/20260702T100000Z_photo.jpg" in r2.objects
    assert not (tmp_path / "library" / "gyma" /
                "20260702T100000Z_photo.jpg").exists()
    assert "intake/gyma/manifest.json" not in r2.objects
    (record,) = _journals(receipts_dir)
    assert record["local_state"] == "prepared"
    # the unresolved attempt blocks any further mutation for this gym
    again = _run(r2)
    assert "error" in again["gyma"]


def test_guarded_complete_hold_keeps_mutation_pending_and_blocks_retry(
        monkeypatch, tmp_path):
    """A lost complete ack after local commit: the journal keeps the exact
    identity at local_committed, the pass reports an error (never a silent
    success), the manifest records the committed disposition, and every
    later pass holds on reconciliation."""
    auth = FakeAuthority(fail="complete")
    receipts_dir = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2)
    out = _run(r2)
    assert "error" in out["gyma"]
    (record,) = _journals(receipts_dir)
    assert record["local_state"] == "local_committed"
    assert record["result_digest"]
    # Manifest and source disposition committed together before uncertain COMPLETE.
    assert "intake/gyma/manifest.json" in r2.objects
    assert "intake/gyma/incoming/20260702T100000Z_photo.jpg" not in r2.objects
    again = _run(r2)
    assert "error" in again["gyma"]
    assert len(_journals(receipts_dir)) == 1   # no new mutation could begin


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("rejected", [False, True])
def test_same_name_jpeg_reencode_preserves_exact_original(
        monkeypatch, tmp_path, guarded, rejected):
    if guarded:
        _arm_fence(monkeypatch, tmp_path, FakeAuthority())
    else:
        _arm(monkeypatch, tmp_path)
    raw = _real_jpeg_bytes()
    converted, name = intake_ingest._convert_default(raw, "20260702T100000Z_photo.jpg")
    assert converted != raw and name == "20260702T100000Z_photo.jpg"
    r2 = FakeR2()
    _seed(r2, data=raw)
    result = intake_ingest.process_all(
        r2=r2, phash=_fake_phash,
        moderator=lambda *_: (not rejected, "test review"))
    assert result["gyma"]["flagged" if rejected else "accepted"] == 1
    assert r2.objects["intake/gyma/originals/20260702T100000Z_photo.jpg"] == raw
    assert "intake/gyma/incoming/20260702T100000Z_photo.jpg" not in r2.objects


def _assert_pending_source(r2, auth, receipts_dir):
    assert "intake/gyma/incoming/20260702T100000Z_photo.jpg" in r2.objects
    assert "intake/gyma/manifest.json" not in r2.objects
    assert not any(event == "complete" for event, _ in auth.events)
    (record,) = _journals(receipts_dir)
    assert record["local_state"] == "pending"


@pytest.mark.parametrize("payload", [b"{broken", b"[]", b"null", b'{"note": 1}'])
def test_guarded_corrupt_upload_sidecar_stays_pending(
        monkeypatch, tmp_path, payload):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2)
    r2.objects["intake/gyma/incoming/20260702T100000Z_upload.json"] = payload
    assert "error" in _run(r2)["gyma"]
    _assert_pending_source(r2, auth, receipts)
    assert not (tmp_path / "library/gyma/20260702T100000Z_photo.jpg").exists()


def test_guarded_corrupt_local_provenance_never_truncates_or_consumes(
        monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    lib = tmp_path / "library/gyma"
    lib.mkdir(parents=True)
    media = lib / "20260702T100000Z_photo.jpg"
    media.write_bytes(b"existing media")
    sidecar = media.with_suffix(".json")
    sidecar.write_bytes(b"{broken")
    r2 = FakeR2()
    _seed(r2)
    assert "error" in _run(r2)["gyma"]
    _assert_pending_source(r2, auth, receipts)
    assert media.read_bytes() == b"existing media"
    assert sidecar.read_bytes() == b"{broken"


@pytest.mark.parametrize("suffix", [".jpg", ".txt", ".json"])
def test_guarded_atomic_local_write_failure_retains_source(
        monkeypatch, tmp_path, suffix):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    original_write = mutation.atomic_write_bytes
    def fail_selected(path, data):
        if path.parent == tmp_path / "library/gyma" and path.suffix == suffix:
            raise OSError("disk full")
        return original_write(path, data)
    monkeypatch.setattr(mutation, "atomic_write_bytes", fail_selected)
    r2 = FakeR2()
    _seed(r2)
    assert "error" in _run(r2)["gyma"]
    _assert_pending_source(r2, auth, receipts)


@pytest.mark.parametrize("suffix", [".jpg", ".txt", ".json"])
def test_guarded_symlink_cannot_modify_other_gym(
        monkeypatch, tmp_path, suffix):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    lib = tmp_path / "library/gyma"
    lib.mkdir(parents=True)
    foreign = tmp_path / "library/gymb"
    foreign.mkdir()
    target = foreign / ("other" + suffix)
    target.write_bytes(b"other gym data")
    (lib / ("20260702T100000Z_photo" + suffix)).symlink_to(target)
    r2 = FakeR2()
    _seed(r2)
    assert "error" in _run(r2)["gyma"]
    _assert_pending_source(r2, auth, receipts)
    assert target.read_bytes() == b"other gym data"


@pytest.mark.parametrize("failure", ["unreadable", "invalid_json", "wrong_shape",
                                     "untyped_aliases", "internal_keyerror"])
def test_guarded_uncertain_manifest_is_never_empty(
        monkeypatch, tmp_path, failure):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2)
    key = "intake/gyma/manifest.json"
    if failure in ("unreadable", "internal_keyerror"):
        original_get = r2.get_bytes
        def failed_get(request_key):
            if request_key == key:
                if failure == "internal_keyerror":
                    raise KeyError("Body")
                raise PermissionError("remote denied")
            return original_get(request_key)
        monkeypatch.setattr(r2, "get_bytes", failed_get)
    else:
        r2.objects[key] = {"invalid_json": b"{broken", "wrong_shape": b"[]",
                           "untyped_aliases": b'{"source_fingerprint_aliases":{"fp":"alias"}}'}[failure]
    before = dict(r2.objects)
    result = _run(r2)
    assert "error" in result["gyma"]
    assert r2.objects == before
    assert not any(event == "complete" for event, _ in auth.events)
    assert _journals(receipts)[0]["local_state"] == "pending"


def test_guarded_confirmed_missing_manifest_initializes_inside_receipt(
        monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2)
    assert _run(r2)["gyma"]["accepted"] == 1
    assert _journals(receipts)[0]["local_state"] == "complete"
    assert json.loads(r2.objects["intake/gyma/manifest.json"])["processed"] == [
        "intake/gyma/incoming/20260702T100000Z_photo.jpg"]


def test_guarded_different_sources_cannot_overwrite_same_converted_basename(
        monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2, name="20260702T100000Z_photo.heic", data=b"HEIC-SOURCE")
    assert _run(r2)["gyma"]["accepted"] == 1
    media = tmp_path / "library/gyma/20260702T100000Z_photo.jpg"
    original_media = media.read_bytes()
    original_provenance = json.loads(
        r2.objects["intake/gyma/manifest.json"])["asset_provenance"]

    _seed(r2, name="20260702T100000Z_photo.jpg", data=b"DIFFERENT-JPEG")
    incoming = "intake/gyma/incoming/20260702T100000Z_photo.jpg"
    result = _run(r2)

    assert "error" in result["gyma"]
    assert r2.objects[incoming] == b"DIFFERENT-JPEG"
    assert media.read_bytes() == original_media
    manifest = json.loads(r2.objects["intake/gyma/manifest.json"])
    assert manifest["asset_provenance"] == original_provenance
    assert any(record["local_state"] == "pending"
               for record in _journals(receipts))


@pytest.mark.parametrize("failure", ["write", "readback"])
def test_guarded_manifest_persistence_precedes_source_delete(
        monkeypatch, tmp_path, failure):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2)
    original_put = r2.put_bytes
    def failed_put(key, data, content_type="application/octet-stream"):
        if key.endswith("/manifest.json"):
            if failure == "write":
                raise OSError("remote unavailable")
            data = b"wrong stored bytes"
        original_put(key, data, content_type)
    monkeypatch.setattr(r2, "put_bytes", failed_put)
    assert "error" in _run(r2)["gyma"]
    assert "intake/gyma/incoming/20260702T100000Z_photo.jpg" in r2.objects
    assert not any(event == "complete" for event, _ in auth.events)
    assert _journals(receipts)[0]["local_state"] == "pending"


def test_guarded_deletion_requires_manifest_and_remote_absence(
        monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2)
    def unacknowledged_delete(key):
        assert key in json.loads(r2.objects["intake/gyma/manifest.json"])["processed"]
        # A silent provider no-op is not evidence of source deletion.
    monkeypatch.setattr(r2, "delete", unacknowledged_delete)
    assert "error" in _run(r2)["gyma"]
    assert "intake/gyma/incoming/20260702T100000Z_photo.jpg" in r2.objects
    assert _journals(receipts)[0]["local_state"] == "pending"
    assert not any(event == "complete" for event, _ in auth.events)


def test_overlapping_guarded_passes_load_manifest_under_same_lock(
        monkeypatch, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    _arm_fence(monkeypatch, tmp_path, FakeAuthority())
    monkeypatch.setattr(mutation.MutationAuthority, "from_environment", FakeAuthority)
    # Both passes clear the settled preflight before either creates its journal.
    # Their actual disposition callbacks must then serialize on the real flock.
    barrier = threading.Barrier(2)
    original_settled = mutation.assert_settled
    def concurrent_preflight(cfg):
        original_settled(cfg)
        barrier.wait(timeout=5)
    monkeypatch.setattr(mutation, "assert_settled", concurrent_preflight)
    converting = threading.Event()
    resume = threading.Event()
    r2 = FakeR2()
    _seed(r2, data=b"PHOTO-A")
    def converter(data, name):
        if data == b"PHOTO-A":
            converting.set()
            assert resume.wait(timeout=5)
        return data, name
    def process():
        return intake_ingest._process_client(
            "gyma", r2, None, converter, _fake_phash, _pass_all)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = workers.submit(process), workers.submit(process)
        assert converting.wait(timeout=5)
        _seed(r2, name="20260702T100001Z_second.jpg", data=b"PHOTO-B")
        resume.set()
        results = first.result(timeout=10), second.result(timeout=10)
    assert sum(result["accepted"] for result in results) == 2
    manifest = json.loads(r2.objects["intake/gyma/manifest.json"])
    assert set(manifest["processed"]) == {
        "intake/gyma/incoming/20260702T100000Z_photo.jpg",
        "intake/gyma/incoming/20260702T100001Z_second.jpg"}
    assert len(manifest["asset_provenance"]) == 2
    assert len(manifest["sha256_raw"]) == 2
    assert all(record["local_state"] == "complete" for record in
               _journals(tmp_path / "inventory-mutation-receipts"))


def test_guarded_forms_explicitly_hold_without_running_separate_db_writers(
        monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2)
    r2.put_bytes("intake/gyma/incoming/20260702T100000Z_intake.json",
                 b'{"answers":{"offers":"reported offer"}}')
    before = dict(r2.objects)
    monkeypatch.setattr(intake_ingest, "_land_intake_form",
                        lambda *_: pytest.fail("unsafe nested DB writer"))
    result = _run(r2)
    assert "error" in result["gyma"]
    assert "intake_form_transaction_adapter_required" in result["gyma"]["error"]
    assert r2.objects == before
    assert _journals(receipts)[0]["local_state"] == "pending"
    assert not any(event == "complete" for event, _ in auth.events)


def test_guarded_raw_dedupe_does_not_trust_missing_historical_original(
        monkeypatch, tmp_path):
    import hashlib
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    raw = _real_jpeg_bytes()
    _seed(r2, data=raw)
    r2.put_bytes("intake/gyma/manifest.json", json.dumps({
        "processed": [], "sha256": [], "phash": [],
        "sha256_raw": [hashlib.sha256(raw).hexdigest()]}).encode())
    assert _run(r2)["gyma"]["duplicates"] == 1
    archive = "intake/gyma/originals/20260702T100000Z_photo.jpg"
    assert r2.objects[archive] == raw
    manifest = json.loads(r2.objects["intake/gyma/manifest.json"])
    assert manifest["asset_provenance"][
        "intake/gyma/incoming/20260702T100000Z_photo.jpg"]["status"] == "duplicate_raw"
    assert _journals(receipts)[0]["local_state"] == "complete"


def test_guarded_callbacks_run_after_complete_with_database_available(
        monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2, name="20260702T100000Z_first.jpg", data=b"PHOTO-A")
    _seed(r2, name="20260702T100001Z_second.jpg", data=b"PHOTO-B")
    notifications = []
    def alert(*args, **kwargs):
        assert _journals(receipts)[0]["local_state"] == "complete"
        with sqlite3.connect(tmp_path / "echo.db", timeout=0) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS callback_check (id integer)")
            conn.execute("INSERT INTO callback_check VALUES (1)")
        notifications.append(args)
    monkeypatch.setattr(ops_alerts, "alert", alert)
    result = intake_ingest.process_all(
        r2=r2, converter=_fake_converter,
        phash=lambda *_: "collision", moderator=_pass_all)
    assert result["gyma"]["accepted"] == 1 and result["gyma"]["held"] == 1
    assert len(notifications) == 1


def test_guarded_originals_are_immutable_on_conflict(monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    raw = _real_jpeg_bytes()
    _seed(r2, data=raw)
    archive = "intake/gyma/originals/20260702T100000Z_photo.jpg"
    r2.put_bytes(archive, b"older preserved source")
    assert "error" in intake_ingest.process_all(
        r2=r2, phash=_fake_phash, moderator=_pass_all)["gyma"]
    _assert_pending_source(r2, auth, receipts)
    assert r2.objects[archive] == b"older preserved source"


def test_guarded_original_readback_drift_holds_before_source_delete(
        monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    raw = _real_jpeg_bytes()
    _seed(r2, data=raw)
    archive = "intake/gyma/originals/20260702T100000Z_photo.jpg"
    original_get = r2.get_bytes
    reads = []
    def drifting_get(key):
        data = original_get(key)
        if key == archive:
            reads.append(key)
            if len(reads) >= 2:
                return b"changed remote original"
        return data
    monkeypatch.setattr(r2, "get_bytes", drifting_get)
    assert "error" in intake_ingest.process_all(
        r2=r2, phash=_fake_phash, moderator=_pass_all)["gyma"]
    assert "intake/gyma/incoming/20260702T100000Z_photo.jpg" in r2.objects
    assert not any(event == "complete" for event, _ in auth.events)
    assert _journals(receipts)[0]["local_state"] == "pending"


def test_guarded_changed_incoming_source_is_not_consumed(
        monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2, data=b"first source")
    def source_changes(data, name):
        r2.objects["intake/gyma/incoming/20260702T100000Z_photo.jpg"] = b"replacement source"
        return data, name
    assert "error" in intake_ingest.process_all(
        r2=r2, converter=source_changes, phash=_fake_phash,
        moderator=_pass_all)["gyma"]
    _assert_pending_source(r2, auth, receipts)
    assert r2.objects["intake/gyma/incoming/20260702T100000Z_photo.jpg"] == b"replacement source"


def test_guarded_unreadable_upload_sidecar_is_not_a_captionless_upload(
        monkeypatch, tmp_path):
    auth = FakeAuthority()
    receipts = _arm_fence(monkeypatch, tmp_path, auth)
    r2 = FakeR2()
    _seed(r2)
    original_get = r2.get_bytes
    def denied_get(key):
        if key.endswith("_upload.json"):
            raise PermissionError("remote denied")
        return original_get(key)
    monkeypatch.setattr(r2, "get_bytes", denied_get)
    assert "error" in _run(r2)["gyma"]
    _assert_pending_source(r2, auth, receipts)
    assert not any("pending_caption/" in key for key in r2.objects)
