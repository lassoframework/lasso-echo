"""
podcast_index.py — classify + index the LASSO podcast Drive library into the
`podcast_asset` table (PODCAST_LIBRARY_BUILD_SPEC.md Wave 2).

The table lives in Supabase next to the other Echo shared-plane tables
(migrations/podcast_asset_20260827.sql — applied BY HAND, an arming step). All
access goes through SupabasePodcastStore (PostgREST, injectable http), the same
pattern as tag_allowlist / portal_calendar_store, so every test runs offline
against a fake store.

CLASSIFIER (spec §2.2) — rules in order, never crashes on a weird name:
  1. Google Doc mime                       -> notes
  2. audio/mpeg mime                       -> audio
  3. CLIP_RE filename match                -> clip (clip_index from the capture)
  4. filename contains "audiogram"         -> audiogram
  5. any other video/mp4                   -> full_video
  anything else                            -> logged + skipped (never raised)

The EPISODE NUMBER always comes from the NEAREST ANCESTOR FOLDER whose title is
a bare 2-3 digit number — never from the filename. Filenames lie (§0: at least
12 naming conventions); the folder titles do not.

POSTABILITY GATE (spec §2.3) — fail closed:
  postable = kind in ('clip','audiogram')
             and size_bytes <= 900 MB
             and 3 <= duration_sec <= 90
             and aspect in ('9:16','1:1')
  full_video is NEVER postable. duration/aspect are unknown until ffprobe runs
  on first download; until then postable is NULL and the asset is NOT
  selectable. An unprobed file never posts.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone

from . import config

# ---- classifier (spec §2.2, regexes verbatim) --------------------------------
EP_FROM_FOLDER = re.compile(r"^\s*(\d{2,3})\s*$")
CLIP_RE = re.compile(r"^GMMS[-_ ]?(?:EP)?(\d{2,3})[-_ ]?S(\d)\.mp4$", re.I)
AUDIOGRAM_RE = re.compile(r"audiogram", re.I)

DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
AUDIO_MIME = "audio/mpeg"
VIDEO_MIME = "video/mp4"

KIND_NOTES = "notes"
KIND_AUDIO = "audio"
KIND_CLIP = "clip"
KIND_AUDIOGRAM = "audiogram"
KIND_FULL = "full_video"
POSTABLE_KINDS = (KIND_CLIP, KIND_AUDIOGRAM)

# ---- postability constants (spec §2.3) ---------------------------------------
MAX_POSTABLE_BYTES = 900_000_000     # IG API practical ceiling
MIN_DURATION_SEC = 3                 # IG Reels sweet spot
MAX_DURATION_SEC = 90
POSTABLE_ASPECTS = ("9:16", "1:1")

REJECT_KIND = "kind_not_postable"            # full_video / audio / notes
REJECT_SIZE = "size_over_900mb"
REJECT_DURATION = "duration_out_of_range"
REJECT_ASPECT = "aspect_not_9_16_or_1_1"
REJECT_REMOVED = "removed_from_drive"


def classify(title, mime_type):
    """(kind, clip_index) for one file, or (None, None) when the classifier
    cannot place it (caller logs + skips; NEVER raises on a weird name)."""
    name = str(title or "").strip()
    mime = str(mime_type or "").strip()
    if mime == DOC_MIME:
        return KIND_NOTES, None
    if mime == AUDIO_MIME:
        return KIND_AUDIO, None
    m = CLIP_RE.match(name)
    if m:
        return KIND_CLIP, int(m.group(2))
    if AUDIOGRAM_RE.search(name):
        return KIND_AUDIOGRAM, None
    if mime == VIDEO_MIME:
        return KIND_FULL, None
    return None, None


def episode_for(file, folders_by_id, max_hops=10):
    """The episode number from the NEAREST ANCESTOR folder whose title matches
    EP_FROM_FOLDER — never from the filename (spec §2.2). None when no ancestor
    folder carries a bare episode number (the file is then skipped)."""
    fid = getattr(file, "parent_id", "") or ""
    for _ in range(max_hops):
        folder = folders_by_id.get(fid)
        if folder is None:
            return None
        m = EP_FROM_FOLDER.match(getattr(folder, "title", "") or "")
        if m:
            return int(m.group(1))
        fid = getattr(folder, "parent_id", "") or ""
    return None


def aspect_of(width, height):
    """'9:16' | '1:1' | '16:9' | 'other' from probed pixel dimensions, with a
    small tolerance for encoder rounding (1080x1920 and 1088x1920 both read
    9:16). Unknown/zero dims -> 'other' (fails the gate; fail closed)."""
    try:
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return "other"
    if w <= 0 or h <= 0:
        return "other"
    ratio = w / h
    for label, target in (("9:16", 9 / 16), ("1:1", 1.0), ("16:9", 16 / 9)):
        if abs(ratio - target) <= 0.03 * target:
            return label
    return "other"


def postability(kind, size_bytes, duration_sec=None, aspect=None):
    """(postable, reject_reason) per the spec §2.3 gate.

    postable is True / False / None. None == not yet probed (duration unknown)
    but still a candidate — NOT selectable until probed. FAIL CLOSED: anything
    unknowable that the gate needs reads as not postable."""
    if kind not in POSTABLE_KINDS:
        return False, REJECT_KIND
    try:
        size = int(size_bytes or 0)
    except (TypeError, ValueError):
        size = 0
    if size > MAX_POSTABLE_BYTES:
        return False, REJECT_SIZE
    if duration_sec is None:
        return None, None  # unprobed: unknown, never selectable
    try:
        dur = float(duration_sec)
    except (TypeError, ValueError):
        return False, REJECT_DURATION
    if not (MIN_DURATION_SEC <= dur <= MAX_DURATION_SEC):
        return False, REJECT_DURATION
    if aspect not in POSTABLE_ASPECTS:
        return False, REJECT_ASPECT
    return True, None


def probe_video(path, runner=None):
    """{'duration_sec', 'width', 'height'} via ffprobe, or None when ffprobe is
    missing/fails (the asset then stays unprobed — not selectable, fail closed)."""
    run = runner or subprocess.run
    try:
        proc = run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120)
        data = json.loads(proc.stdout or "{}")
    except Exception as e:  # noqa: BLE001 - a probe failure is a skip, not a crash
        print(f"[podcast-index] ffprobe failed for {path}: {type(e).__name__}: {e}")
        return None
    duration = None
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        pass
    width = height = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            width, height = stream.get("width"), stream.get("height")
            if duration is None:
                try:
                    duration = float(stream.get("duration"))
                except (TypeError, ValueError):
                    pass
            break
    if duration is None or not width or not height:
        return None
    return {"duration_sec": duration, "width": int(width), "height": int(height)}


def build_rows(files, now_iso=None, log=None):
    """Classify a walked Drive listing into podcast_asset row dicts.

    Returns (rows, skipped): `rows` carry the indexer-owned columns only
    (id/episode/kind/clip_index/title/size_bytes/notes_doc_id/indexed_at plus
    the index-time postability verdict); `skipped` is [(title, why)] for every
    file the classifier could not place — logged, never raised."""
    log = log or (lambda m: print(f"[podcast-index] {m}"))
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    folders_by_id = {f.id: f for f in files if f.mime_type == FOLDER_MIME}

    rows, skipped = [], []
    notes_by_episode = {}
    for f in files:
        if f.mime_type == FOLDER_MIME:
            continue
        kind, clip_index = classify(f.title, f.mime_type)
        if kind is None:
            skipped.append((f.title, "unclassifiable name/mime"))
            log(f"skip {f.title!r}: classifier could not place it (logged, not indexed)")
            continue
        episode = episode_for(f, folders_by_id)
        if episode is None:
            skipped.append((f.title, "no episode-number ancestor folder"))
            log(f"skip {f.title!r}: no ancestor folder titled with a bare episode number")
            continue
        postable, reject = postability(kind, f.size_bytes)
        rows.append({
            "id": f.id,
            "episode": episode,
            "kind": kind,
            "clip_index": clip_index,
            "title": f.title,
            "size_bytes": f.size_bytes,
            "postable": postable,
            "reject_reason": reject,
            "notes_doc_id": None,
            "indexed_at": now_iso,
        })
        if kind == KIND_NOTES:
            # An episode can carry MORE THAN ONE notes Doc. Pick deterministically
            # (smallest id wins) so a re-index links the SAME Doc every run —
            # last-seen-wins would flip notes_doc_id run to run and thrash the
            # PATCH. EVERY clip/audiogram of an episode with a Doc gets linked.
            prev = notes_by_episode.get(episode)
            if prev is None or str(f.id) < str(prev):
                notes_by_episode[episode] = f.id
    for row in rows:
        row["notes_doc_id"] = notes_by_episode.get(row["episode"])
    return rows, skipped


# ---- deduped ops alert (shared by the selector + caption lanes) ---------------

def dedup_alert(stamp_key, message):
    """Fire ops_alerts.alert(message) at most ONCE per stamp_key, durable-or-
    silent (db.kv_is_durable — the 2026-08-27 gritx storm rule: a process whose
    kv stamps die with it must not alert at all, or it re-fires every run).
    Returns True when the alert actually fired this call."""
    from . import db, ops_alerts
    if not db.kv_is_durable():
        print(f"[podcast] alert suppressed (kv not durable): {message}")
        return False
    key = f"podcast_alert:{stamp_key}"
    if db.kv_get(key, ""):
        return False
    db.kv_set(key, datetime.now(timezone.utc).isoformat())
    ops_alerts.alert(message)
    return True


def clear_alert_stamp(stamp_key):
    """Reset a dedup stamp so the NEXT occurrence of the condition alerts once
    again (e.g. the pool refilled, then emptied again)."""
    from . import db
    try:
        db.kv_set(f"podcast_alert:{stamp_key}", "")
    except Exception:
        pass


# ---- Supabase store (PostgREST; injectable http; offline -> unavailable) -----

class PodcastStoreError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"podcast_asset store {status}: {detail}")


class SupabasePodcastStore:
    """podcast_asset reads/writes over PostgREST, mirroring the
    portal_calendar_store pattern (service key, scrubbed errors, injectable
    http). NEVER touches used_count/last_used_at on the indexer paths — those
    columns belong to the selector."""

    _TABLE = "podcast_asset"
    _PAGE = 1000  # PostgREST caps responses; page explicitly, never assume one shot

    def __init__(self, url=None, service_key=None, http=None):
        self.url = (url if url is not None else config.supabase_url()).rstrip("/")
        self.key = service_key if service_key is not None else config.supabase_service_key()
        self._http = http

    def available(self):
        return bool(self.url and self.key)

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, repo pattern
        return requests

    def _headers(self, extra=None):
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        if extra:
            h.update(extra)
        return h

    def _rest(self):
        return f"{self.url}/rest/v1/{self._TABLE}"

    def _scrubbed(self, r):
        from . import ops_alerts
        return ops_alerts.scrub((getattr(r, "text", "") or "")[:200])

    def list_assets(self):
        """Every podcast_asset row, explicitly paged (the 1000-row PostgREST cap
        must never silently truncate the pool)."""
        out, offset = [], 0
        while True:
            r = self._client().get(
                self._rest(),
                params={"select": "*", "order": "id.asc",
                        "limit": str(self._PAGE), "offset": str(offset)},
                headers=self._headers(), timeout=30)
            if r.status_code >= 400:
                raise PodcastStoreError(r.status_code, self._scrubbed(r))
            batch = r.json() or []
            out.extend(batch)
            if len(batch) < self._PAGE:
                return out
            offset += self._PAGE

    def insert_assets(self, rows):
        """Bulk-insert NEW asset rows. Insert-only (no upsert): existing rows are
        updated field-by-field via update_asset so probe data, used_count and
        last_used_at are never clobbered by a re-index."""
        if not rows:
            return 0
        r = self._client().post(
            self._rest(), json=list(rows),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            timeout=30)
        if r.status_code >= 400:
            raise PodcastStoreError(r.status_code, self._scrubbed(r))
        return len(rows)

    def update_asset(self, asset_id, fields):
        """PATCH one row by id. Only the passed fields change."""
        r = self._client().patch(
            self._rest(), params={"id": f"eq.{asset_id}"}, json=dict(fields),
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=minimal"}),
            timeout=30)
        if r.status_code >= 400:
            raise PodcastStoreError(r.status_code, self._scrubbed(r))
        return True


def default_store():
    return SupabasePodcastStore()
