"""
Texted-link intake: the processing half, INSIDE the existing listener loop (the
one process that has both /data and R2).

Per pass, for each client with objects under intake/<client>/incoming/:
  1. quarantine zero-byte uploads to deadletter/ with a specific ops alert,
  2. dedupe the RAW bytes by SHA-256 (the same file uploaded twice lands once,
     no matter what the converter does with it),
  3. convert HEIC to JPG (EXIF orientation normalized) and MOV to MP4 (ffmpeg
     stream-copy remux when ffmpeg is available, unchanged pass-through when
     not); every conversion archives the ORIGINAL to intake/<client>/originals/
     before the incoming object is deleted, so no conversion loses a file; a
     conversion failure dead-letters the file, it never crashes the loop,
  4. dedupe the converted bytes by SHA-256 plus perceptual hash against
     everything already accepted,
  5. run the moderation hook (a stub interface today: moderate(data, name) ->
     (ok, reason); anything flagged moves to intake/<client>/review/ and posts one
     Slack notice line),
  6. file accepted media into the client's content library prefix with the
     client's sentence saved as the caption note file the drafter already reads.

Idempotent via a processed manifest stored in R2 (intake/<client>/manifest.json);
a re-run of an already-processed batch is a no-op. Any per-file failure goes to
intake/<client>/deadletter/ with ONE ops alert and processing continues.

Same flag as the upload page: AGENT_INTAKE_ENABLED, default OFF (dormant).
"""

import hashlib
import io
import json
import os

from . import config, ops_alerts
from .accounts import get_account

MANIFEST = "manifest.json"


# ---- default media transforms (lazy imports; injectable for tests) -------------
def _remux_mov(data, name, runner=None, which=None):
    """MOV -> MP4 container remux via ffmpeg (stream copy: lossless, cheap).
    Returns (bytes, new_name) or None when ffmpeg is unavailable or the remux
    fails — the caller then passes the MOV through unchanged (IG accepts MOV;
    a playable original always beats a failed conversion)."""
    import shutil
    import subprocess
    import tempfile
    which = which or shutil.which
    runner = runner or subprocess.run
    if which("ffmpeg") is None:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, name)
            dst = os.path.join(td, os.path.splitext(name)[0] + ".mp4")
            with open(src, "wb") as fh:
                fh.write(data)
            runner(["ffmpeg", "-y", "-i", src, "-c", "copy", dst],
                   check=True, capture_output=True, timeout=120)
            with open(dst, "rb") as fh:
                return fh.read(), os.path.basename(dst)
    except Exception:
        return None


def _convert_default(data, name):
    """(new_bytes, new_name): HEIC/HEIF -> JPG (orientation normalized);
    MOV -> MP4 (ffmpeg remux when available, else unchanged); MP4 passes
    through. The ORIGINAL bytes are archived by the pipeline whenever the
    name changes, so no conversion ever loses the source file."""
    lower = name.lower()
    if lower.endswith(".mp4"):
        return data, name
    if lower.endswith(".mov"):
        remuxed = _remux_mov(data, name)
        return remuxed if remuxed is not None else (data, name)
    from PIL import Image, ImageOps  # lazy
    if lower.endswith((".heic", ".heif")):
        import pillow_heif  # lazy
        pillow_heif.register_heif_opener()
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=92)
    stem = os.path.splitext(name)[0]
    return out.getvalue(), f"{stem}.jpg"


def _phash_default(data, name):
    """8x8 average hash for near-duplicate detection; None for video/unreadable."""
    if name.lower().endswith((".mp4", ".mov")):
        return None
    try:
        from PIL import Image  # lazy
        img = Image.open(io.BytesIO(data)).convert("L").resize((8, 8))
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        return "".join("1" if p > avg else "0" for p in pixels)
    except Exception:
        return None


def _moderate_default(data, name):
    """Moderation hook STUB: everything passes today. The interface is the contract;
    a real classifier slots in here without touching the pipeline."""
    return True, ""


# ---- manifest -------------------------------------------------------------------
def _load_manifest(r2, client):
    try:
        raw = r2.get_bytes(f"intake/{client}/{MANIFEST}")
        manifest = json.loads(raw.decode("utf-8"))
    except Exception:
        manifest = {"processed": [], "sha256": [], "phash": []}
    # additive key for raw-bytes dedupe; old manifests gain it on first touch
    manifest.setdefault("sha256_raw", [])
    return manifest


def _save_manifest(r2, client, manifest):
    r2.put_bytes(f"intake/{client}/{MANIFEST}",
                 json.dumps(manifest).encode("utf-8"),
                 content_type="application/json")


def _library_dir_for(client):
    """The client's content library prefix: the account's own library when
    configured (multi-client), else a per-client folder under the global library."""
    acct = get_account(client)
    if acct is not None and getattr(acct, "library_prefix", ""):
        return acct.library_prefix
    return os.path.join(config.LIBRARY_PATH, client)


def _clients_with_incoming(r2):
    clients = set()
    for key in r2.list_keys("intake/"):
        parts = key.split("/")
        if len(parts) >= 4 and parts[2] == "incoming" and parts[3]:
            clients.add(parts[1])
    return sorted(clients)


def process_all(r2=None, poster=None, converter=None, phash=None, moderator=None):
    """
    One ingest pass over every client. Returns {client: {"accepted": n, ...}} or
    None while the flag is OFF. Never raises for a single bad file.
    """
    if not config.intake_enabled():
        return None
    r2 = r2 or _default_r2()
    if r2 is None:
        return {}
    converter = converter or _convert_default
    phash = phash or _phash_default
    moderator = moderator or _moderate_default

    results = {}
    for client in _clients_with_incoming(r2):
        results[client] = _process_client(client, r2, poster, converter, phash, moderator)
    return results


def _process_client(client, r2, poster, converter, phash, moderator):
    stats = {"accepted": 0, "duplicates": 0, "flagged": 0, "deadlettered": 0, "skipped": 0}
    manifest = _load_manifest(r2, client)
    prefix = f"intake/{client}/incoming/"
    keys = sorted(r2.list_keys(prefix))
    sidecars = {k: None for k in keys if k.endswith("_upload.json")}
    media_keys = [k for k in keys if not k.endswith(".json")]

    # note lookup: a media file's sidecar shares its timestamp prefix
    def _note_for(media_key):
        stamp = os.path.basename(media_key).split("_", 1)[0]
        for sk in sidecars:
            if os.path.basename(sk).startswith(stamp):
                try:
                    return (json.loads(r2.get_bytes(sk).decode("utf-8")) or {}).get("note", "")
                except Exception:
                    return ""
        return ""

    lib_dir = _library_dir_for(client)
    for key in media_keys:
        if key in manifest["processed"]:
            stats["skipped"] += 1
            continue
        name = os.path.basename(key)
        raw = None   # kept for dead-letter-from-memory + the originals archive
        try:
            raw = r2.get_bytes(key)

            # ZERO-BYTE GUARD: an empty upload can never be media. Quarantine to
            # the dead-letter prefix with a specific alert; never crash, never
            # hand empty bytes to a converter.
            if not raw:
                stats["deadlettered"] += 1
                r2.put_bytes(f"intake/{client}/deadletter/{name}", b"")
                r2.delete(key)
                manifest["processed"].append(key)
                ops_alerts.alert(f"intake ingest quarantined {client}/{name}: "
                                 "zero-byte upload (empty file, nothing filed)")
                continue

            # RAW dedupe FIRST: the same file uploaded twice lands once, no
            # matter what the converter does with it.
            raw_sha = hashlib.sha256(raw).hexdigest()
            if raw_sha in manifest["sha256_raw"]:
                stats["duplicates"] += 1
                r2.delete(key)
                manifest["processed"].append(key)
                continue

            data, name = converter(raw, name)

            sha = hashlib.sha256(data).hexdigest()
            ph = phash(data, name)
            if sha in manifest["sha256"] or (ph is not None and ph in manifest["phash"]):
                stats["duplicates"] += 1
                manifest["sha256_raw"].append(raw_sha)   # remember the raw form too
                r2.delete(key)
                manifest["processed"].append(key)
                continue

            ok, reason = moderator(data, name)
            if not ok:
                r2.put_bytes(f"intake/{client}/review/{name}", data)
                r2.delete(key)
                manifest["processed"].append(key)
                stats["flagged"] += 1
                if poster is not None:
                    poster.post_notice(f"Intake: {client} file {name} sent to review "
                                       f"({reason}); nothing filed to the library.")
                continue

            # ORIGINALS KEPT: a conversion (name changed: HEIC->JPG, MOV->MP4)
            # archives the untouched source bytes to intake/<client>/originals/
            # BEFORE the incoming object is deleted. No conversion loses a file.
            if name != os.path.basename(key):
                r2.put_bytes(f"intake/{client}/originals/{os.path.basename(key)}",
                             raw)

            os.makedirs(lib_dir, exist_ok=True)
            with open(os.path.join(lib_dir, name), "wb") as fh:
                fh.write(data)
            note = _note_for(key)
            if note:
                stem = os.path.splitext(name)[0]
                with open(os.path.join(lib_dir, f"{stem}.txt"), "w", encoding="utf-8") as fh:
                    fh.write(note.strip())

            manifest["processed"].append(key)
            manifest["sha256"].append(sha)
            manifest["sha256_raw"].append(raw_sha)
            if ph is not None:
                manifest["phash"].append(ph)
            r2.delete(key)
            stats["accepted"] += 1
            # DAM auto-tag on the freshly filed asset (AGENT_AUTOTAG_ENABLED,
            # OFF by default; errors are contained inside autotag)
            try:
                from . import dam
                dam.autotag(os.path.join(lib_dir, name))
            except Exception:
                pass
        except Exception as e:
            stats["deadlettered"] += 1
            try:
                # quarantine from the bytes already in memory when we have them
                # (a corrupt object can be unreadable a second time); re-fetch
                # only if the original get itself was what failed.
                r2.put_bytes(f"intake/{client}/deadletter/{os.path.basename(key)}",
                             raw if raw is not None else r2.get_bytes(key))
                r2.delete(key)
            except Exception as dl_err:
                # even dead-lettering must never crash the loop, but a failed
                # dead-letter is LOUD, and the key is still marked processed
                # below so the same bad file is never re-picked forever.
                print(f"[intake] dead-letter itself failed for {client}/"
                      f"{os.path.basename(key)}: {type(dl_err).__name__}")
            manifest["processed"].append(key)
            ops_alerts.alert(f"intake ingest dead-lettered {client}/{os.path.basename(key)}: "
                             f"{type(e).__name__}: {e}")

    _save_manifest(r2, client, manifest)
    return stats


class _R2:
    """List/get/put/delete R2 wrapper (listener side). Credentials lazy, never logged."""

    def __init__(self, s3, bucket):
        self._s3 = s3
        self._bucket = bucket

    def list_keys(self, prefix):
        keys, token = [], None
        while True:
            kw = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            keys.extend(o["Key"] for o in resp.get("Contents", []))
            token = resp.get("NextContinuationToken")
            if not token:
                return keys

    def get_bytes(self, key):
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data,
                            ContentType=content_type)

    def delete(self, key):
        self._s3.delete_object(Bucket=self._bucket, Key=key)


def _default_r2():
    key_id = os.environ.get(config.S3_ACCESS_KEY_ID_ENV)
    secret = os.environ.get(config.S3_SECRET_ACCESS_KEY_ENV)
    if not key_id or not secret or not config.S3_BUCKET:
        return None
    import boto3  # lazy
    s3 = boto3.client("s3", endpoint_url=config.S3_ENDPOINT or None,
                      region_name=config.S3_REGION or None,
                      aws_access_key_id=key_id, aws_secret_access_key=secret)
    return _R2(s3, config.S3_BUCKET)
