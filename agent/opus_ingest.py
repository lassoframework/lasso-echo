"""
Opus Clip ingest: pull finished clips from the Opus Clip API and file each as a
video asset that becomes a Reel DRAFT through the existing path (video = Reel,
one approval card, held for Blake's tap like every draft).

Built ONLY against the documented API (https://help.opus.pro/api-reference,
OpenAPI at /api-reference/openapi.json):
  - GET /api/exportable-clips?q=findByProjectId|findByCollectionId (paginated)
    -> id, title, description, durationMs, uriForExport, createdAt
  - GET /api/collections?q=mine -> collection ids (there is NO bulk project
    listing endpoint, so discovery = pinned AGENT_OPUS_PROJECT_IDS plus
    collections)
  - Auth: Authorization Bearer key (env OPUS_API_KEY, read lazily, NEVER logged
    or printed) plus optional x-opus-org-id.

Watermarks (last createdAt per source), ingested clip ids, content hashes, and
failure counts persist to /data so re-pulls are idempotent and restarts forget
nothing. Dedupe is by sha256 of the clip bytes: the library filename carries the
hash (opus_<sha16>.mp4), so the rotation no-repeat window keys on content.

GATES UNCHANGED: nothing here publishes. A clip reaches Meta only through the
daily draft plus the approver's tap, behind the publish flag. The caption drafts
from the approved voice bible plus the clip's OWN title and words (the sidecar
note); the fabrication gate still benches any stat-bearing title.
"""

import hashlib
import json
import os
import re

from . import config, media_host, ops_alerts

STATE_FILE = "opus_state.json"
FAILURE_DEADLETTER_AT = 3
HOST_TENANT = "lasso_library"


# ---- state (/data) --------------------------------------------------------------
def _state_path():
    return os.path.join(os.environ.get("AGENT_OPUS_STATE_DIR", "/data"), STATE_FILE)


def load_state():
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def save_state(state):
    try:
        with open(_state_path(), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception as e:
        print(f"[opus] could not persist state: {type(e).__name__}: {e}")


# ---- the API client (documented endpoints only; the key is never logged) --------
class OpusAPI:
    def __init__(self, key, org_id=""):
        self._key = key          # held for headers only; never logged or printed
        self._org_id = org_id

    def _headers(self):
        h = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        if self._org_id:
            h["x-opus-org-id"] = self._org_id
        return h

    def _get(self, path, params=None):
        import requests  # lazy
        r = requests.get(f"{config.OPUS_API_BASE}{path}", params=params or {},
                         headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Opus API {r.status_code} on {path}")
        return r.json()

    def list_collections(self):
        """GET /api/collections?q=mine -> [collection ids]."""
        body = self._get("/api/collections", {"q": "mine"}) or {}
        items = body if isinstance(body, list) else body.get("data", []) or []
        return [c.get("id") for c in items if isinstance(c, dict) and c.get("id")]

    def list_exportable_clips(self, q, source_id):
        """GET /api/exportable-clips, paginated. q = findByProjectId|findByCollectionId."""
        id_param = "projectId" if q == "findByProjectId" else "collectionId"
        clips, page = [], 1
        while True:
            body = self._get("/api/exportable-clips",
                             {"q": q, id_param: source_id,
                              "pageNum": page, "pageSize": 50}) or {}
            items = body if isinstance(body, list) else body.get("data", []) or []
            clips.extend(i for i in items if isinstance(i, dict))
            if len(items) < 50:
                return clips
            page += 1

    def download(self, url):
        import requests  # lazy; export URLs are signed CDN links (no auth header)
        r = requests.get(url, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"clip download failed: {r.status_code}")
        return r.content


def _default_api():
    key = os.environ.get(config.OPUS_API_KEY_ENV)
    if not key:
        return None
    return OpusAPI(key, config.OPUS_ORG_ID)


def _sources(api):
    """(q, id) pairs to scan: pinned project ids + pinned collections, else every
    collection the account owns (the documented discovery path)."""
    sources = [("findByProjectId", pid) for pid in config.OPUS_PROJECT_IDS]
    collections = config.OPUS_COLLECTION_IDS
    if not collections and not sources:
        try:
            collections = api.list_collections()
        except Exception as e:
            print(f"[opus] could not list collections: {type(e).__name__}: {e}")
            collections = []
    sources.extend(("findByCollectionId", cid) for cid in collections)
    return sources


def _slug(text, limit=40):
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")[:limit]


def pull(api=None, s3_client=None, poster=None, out_dir=None):
    """
    One ingest pass: list new finished clips since the watermark, download, host to
    R2 (content addressed, dedupe by hash), file into the library as a video asset
    with its sidecar, and print one hosted URL per clip. Returns a summary dict, or
    None while AGENT_OPUS_ENABLED is OFF. Never raises for a single bad clip.
    """
    if not config.opus_enabled():
        return None
    api = api or _default_api()
    if api is None:
        print("[opus] OPUS_API_KEY is not set; nothing pulled.")
        return {"pulled": 0, "skipped": 0, "failed": 0}

    state = load_state()
    watermarks = state.setdefault("watermarks", {})
    ingested = set(state.setdefault("ingested_ids", []))
    hashes = set(state.setdefault("hashes", []))
    failures = state.setdefault("failures", {})
    dead = set(state.setdefault("deadletter", []))

    out_dir = out_dir or config.LIBRARY_PATH
    summary = {"pulled": 0, "skipped": 0, "failed": 0}

    for q, source_id in _sources(api):
        source_key = f"{q}:{source_id}"
        watermark = watermarks.get(source_key, "")
        try:
            clips = api.list_exportable_clips(q, source_id)
        except Exception as e:
            summary["failed"] += 1
            ops_alerts.alert(f"opus ingest could not list clips for {source_id}: "
                             f"{type(e).__name__}: {e}")
            continue

        # The watermark only advances past RESOLVED clips (ingested, deduped, or
        # dead-lettered). It stalls at the first unresolved failure so the next
        # pull retries that clip instead of silently losing it.
        newest = watermark
        stalled = False
        for clip in sorted(clips, key=lambda c: c.get("createdAt", "")):
            created = clip.get("createdAt", "")
            clip_id = clip.get("id", "")
            if not clip_id or not clip.get("uriForExport"):
                continue
            if created and created <= watermark:
                continue  # already covered by the watermark
            if clip_id in ingested or clip_id in dead:
                summary["skipped"] += 1
                if not stalled:
                    newest = max(newest, created)
                continue
            try:
                data = api.download(clip["uriForExport"])
                sha = hashlib.sha256(data).hexdigest()
                if sha in hashes:
                    summary["skipped"] += 1     # same content pulled before
                    ingested.add(clip_id)
                    if not stalled:
                        newest = max(newest, created)
                    continue
                name = f"opus_{sha[:16]}.mp4"
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, name)
                with open(path, "wb") as fh:
                    fh.write(data)
                hosted = media_host.host_media(path, HOST_TENANT, client=s3_client)
                sidecar = {
                    "source": "opus",
                    "opus_clip_id": clip_id,
                    "title": clip.get("title", ""),
                    "duration_ms": clip.get("durationMs"),
                    "pulled": created or "",
                    # the caption's raw material: the clip's OWN title and words
                    # only (plus the approved voice bible downstream). Never invented.
                    "note": " ".join(x for x in (clip.get("title", ""),
                                                 clip.get("description", "")) if x).strip(),
                }
                if hosted:
                    sidecar["public_url"] = hosted
                with open(os.path.join(out_dir, f"opus_{sha[:16]}.json"),
                          "w", encoding="utf-8") as fh:
                    json.dump(sidecar, fh, indent=2)
                ingested.add(clip_id)
                hashes.add(sha)
                summary["pulled"] += 1
                if not stalled:
                    newest = max(newest, created)
                print(f"opus {clip_id}: {hosted or 'HOSTING UNAVAILABLE, local only'}")
            except Exception as e:
                summary["failed"] += 1
                failures[clip_id] = failures.get(clip_id, 0) + 1
                if failures[clip_id] >= FAILURE_DEADLETTER_AT:
                    dead.add(clip_id)
                    if not stalled:
                        newest = max(newest, created)  # dead = resolved; move on
                    ops_alerts.alert(f"opus ingest dead-lettered clip {clip_id} after "
                                     f"{failures[clip_id]} failures: {type(e).__name__}: {e}")
                else:
                    stalled = True  # retry next pull; watermark must not pass it
                    ops_alerts.alert(f"opus ingest failed for clip {clip_id} "
                                     f"(attempt {failures[clip_id]}): {type(e).__name__}: {e}")
        watermarks[source_key] = newest

    state["ingested_ids"] = sorted(ingested)
    state["hashes"] = sorted(hashes)
    state["deadletter"] = sorted(dead)
    save_state(state)
    return summary
