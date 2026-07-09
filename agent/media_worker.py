"""
Media inbox ingest worker (Stage 2 Part 6): STAGED rows -> the tenant library.

Rides AGENT_MEDIA_INBOX_ENABLED (no second flag: the worker only ever sees rows
the Part 5 queue staged, and that queue is OFF by default). Per STAGED row:

  1. PERCEPTUAL dedupe on top of the queue's sha256: an 8x8 average hash match
     against the tenant's accepted media marks the row duplicate (near-identical
     re-shots never pile up). sha256 already blocked exact bytes at receive.
  2. CONSENT GUARD hook: consent(data, name, tenant) -> (ok, reason). The
     default passes (a stub interface, like intake moderation); a real checker
     slots in without touching the pipeline. Refused = row rejected + one notice.
  3. CAPTION GATE: a row with no texted sentence is NOT filed. It flips to
     awaiting_caption, ONE auto-ask fires (ops alert naming tenant + file), and
     the media cannot be drafted from (it never reaches the library) until
     attach_caption() supplies the sentence and the next pass files it.
  4. File into the tenant library (content_library/<tenant>/) with the sentence
     as the .txt caption sidecar the drafter already reads; AUTOTAG hook rides
     along (errors contained). A THUMBNAIL lands beside the asset (failure is
     never fatal).
  5. Host to R2 through media_host.host_media(path, tenant): the existing
     tenant isolation (echo/<tenant-slug>/<sha>/) scopes every key.

HELD rows (unknown sender) are NEVER processed: they wait for a human to map
the phone or discard. Nothing here publishes or drafts.
"""

import io
import os

from . import config, db, media_host, media_inbox, ops_alerts

_THUMB_MAX = 320


# ---- default hooks (lazy imports; injectable for tests) -----------------------------------
def _phash_default(data, name):
    """8x8 average hash; None for video/unreadable (video never phash-dedupes)."""
    if name.lower().endswith((".mp4", ".mov", ".m4v")):
        return None
    try:
        from PIL import Image  # lazy
        img = Image.open(io.BytesIO(data)).convert("L").resize((8, 8))
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        return "".join("1" if p > avg else "0" for p in pixels)
    except Exception:
        return None


def _consent_default(data, name, tenant):
    """Consent guard STUB: passes today. The interface is the contract."""
    return True, ""


def _thumbnail_default(data, name, out_path):
    """A small JPEG preview beside the filed asset; False = no thumbnail (never fatal)."""
    if name.lower().endswith((".mp4", ".mov", ".m4v")):
        return False
    try:
        from PIL import Image  # lazy
        img = Image.open(io.BytesIO(data))
        img.thumbnail((_THUMB_MAX, _THUMB_MAX))
        img.convert("RGB").save(out_path, format="JPEG", quality=80)
        return True
    except Exception:
        return False


def _autotag_default(path):
    try:
        from . import dam
        dam.autotag(path)
    except Exception:
        pass  # autotag errors are contained, exactly like the intake path


# ---- per-tenant phash memory ----------------------------------------------------------------
_PHASH_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_phashes (
  tenant_key TEXT,
  phash TEXT,
  PRIMARY KEY (tenant_key, phash));
"""


def _phash_seen(conn, tenant, ph):
    return conn.execute("SELECT 1 FROM media_phashes WHERE tenant_key=? AND phash=?",
                        (tenant, ph)).fetchone() is not None


def _library_dir(tenant):
    return os.path.join(config.LIBRARY_PATH, tenant)


def attach_caption(row_id, note):
    """Supply the missing texted sentence for an awaiting_caption row; the next
    process() pass files it. Empty notes are refused (the gate stands)."""
    note = str(note or "").strip()
    if not note:
        return False
    media_inbox.set_status(row_id, "staged", note=note)
    db.audit("media_worker", str(row_id), "caption attached; row re-staged")
    return True


def process(s3_client=None, phash=None, consent=None, thumbnail=None,
            autotag=None, base_dir=None):
    """
    One worker pass over every STAGED row. Returns
    {"processed": n, "duplicates": n, "rejected": n, "awaiting_caption": n}
    or None while AGENT_MEDIA_INBOX_ENABLED is OFF. Never raises for one bad row.
    """
    if not config.media_inbox_enabled():
        return None
    phash = phash or _phash_default
    consent = consent or _consent_default
    thumbnail = thumbnail or _thumbnail_default
    autotag = autotag or _autotag_default

    out = {"processed": 0, "duplicates": 0, "rejected": 0, "awaiting_caption": 0}
    for row in media_inbox.rows(status="staged"):
        rid, tenant = row["id"], row["tenant_key"]
        if not tenant:
            continue  # held rows are never processed; belt and suspenders
        try:
            with open(row["staged_path"], "rb") as fh:
                data = fh.read()
        except OSError as e:
            media_inbox.set_status(rid, "rejected")
            ops_alerts.alert(f"media worker: staged file missing for row {rid} "
                             f"({tenant}); rejected. {type(e).__name__}")
            out["rejected"] += 1
            continue

        # 1. perceptual dedupe (sha256 exact-dupe already blocked at receive)
        ph = phash(data, row["name"])
        with db._lock, db.connect() as conn:
            conn.executescript(_PHASH_SCHEMA)
            is_dupe = ph is not None and _phash_seen(conn, tenant, ph)
        if is_dupe:
            media_inbox.set_status(rid, "duplicate")
            out["duplicates"] += 1
            continue

        # 2. consent guard
        ok, reason = consent(data, row["name"], tenant)
        if not ok:
            media_inbox.set_status(rid, "rejected")
            ops_alerts.alert(f"media worker: {tenant} file {row['name']} refused "
                             f"by the consent guard ({reason}); not filed.")
            out["rejected"] += 1
            continue

        # 3. caption gate: no sentence = not filed, one auto-ask, drafting blocked
        note = (row["caption_note"] or "").strip()
        if not note:
            media_inbox.set_status(rid, "awaiting_caption")
            ask_key = f"caption_ask_{rid}"
            if not db.kv_get(ask_key):
                ops_alerts.alert(
                    f"media inbox: {tenant} sent {row['name']} with no sentence. "
                    "Ask them what it shows; the media stays out of the library "
                    "(nothing drafts from it) until the caption arrives.",
                    force=True)
                db.kv_set(ask_key, "1")
            out["awaiting_caption"] += 1
            continue

        # 4. file into the tenant library + sidecar note + thumbnail + autotag
        lib = _library_dir(tenant)
        os.makedirs(lib, exist_ok=True)
        dest = os.path.join(lib, row["name"])
        with open(dest, "wb") as fh:
            fh.write(data)
        stem = os.path.splitext(row["name"])[0]
        with open(os.path.join(lib, f"{stem}.txt"), "w", encoding="utf-8") as fh:
            fh.write(note)
        thumbnail(data, row["name"], os.path.join(lib, f"{stem}_thumb.jpg"))
        autotag(dest)

        # 5. host tenant-scoped (echo/<tenant-slug>/... via media_host isolation)
        hosted = media_host.host_media(dest, tenant, client=s3_client)

        with db._lock, db.connect() as conn:
            conn.executescript(_PHASH_SCHEMA)
            if ph is not None:
                conn.execute("INSERT OR IGNORE INTO media_phashes "
                             "(tenant_key, phash) VALUES (?,?)", (tenant, ph))
            conn.commit()
        media_inbox.set_status(rid, "processed")
        db.audit("media_worker", tenant,
                 f"row {rid} filed to {dest}" + (f", hosted {hosted}" if hosted
                                                 else ", hosting unavailable"))
        out["processed"] += 1
    return out
