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

Watermarks (last createdAt per source), ingested clip ids, content hashes,
failure counts, and the daily alert stamps persist to /data so re-pulls are
idempotent and restarts forget nothing.

ALERT HYGIENE: pinned project ids that look like placeholders (P1 pattern or
under 6 chars) are warned about once and never sent to the API, at startup and
at every ingest pass. Ingest failure alerts debounce to ONE Slack line per
source per day; repeats the same day go to the audit log only. Dedupe is by sha256 of the clip bytes: the library filename carries the
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


class OpusScanError(Exception):
    """Non-2xx or transport failure from the Opus API. Carries the HTTP status
    and a scrubbed body snippet; NEVER the auth token. Raised instead of a
    silent empty-list return so callers can surface the failure loudly."""
    def __init__(self, http_status, body_snippet=""):
        self.http_status = http_status
        self.body_snippet = body_snippet
        super().__init__(f"Opus API HTTP {http_status}: {body_snippet}")
FAILURE_DEADLETTER_AT = 3
HOST_TENANT = "lasso_library"

# A pinned AGENT_OPUS_PROJECT_IDS value that is really the docs' example
# (P1, p2, ...) or too short to be a real OpusClip project id. Such a value
# must NEVER reach the API: it is warned about once and skipped.
_PLACEHOLDER_ID_RE = re.compile(r"^[Pp]\d+$")
_MIN_PROJECT_ID_LEN = 6


def split_placeholder_project_ids(ids):
    """(real, placeholders) from a pinned project id list. A placeholder is the
    example pattern P<digits> (any length) or any value under 6 characters."""
    real, bad = [], []
    for pid in ids:
        pid = str(pid).strip()
        if not pid:
            continue
        if _PLACEHOLDER_ID_RE.match(pid) or len(pid) < _MIN_PROJECT_ID_LEN:
            bad.append(pid)
        else:
            real.append(pid)
    return real, bad


def validated_project_ids(ids=None):
    """The pinned project ids that are safe to call the API with. Placeholders
    are skipped with ONE warning line naming every bad value (printed once per
    validation pass: listener startup and each ingest pass, never per call)."""
    real, bad = split_placeholder_project_ids(
        config.OPUS_PROJECT_IDS if ids is None else ids)
    if bad:
        print("[opus] WARNING: AGENT_OPUS_PROJECT_IDS value(s) look like "
              f"placeholders and were SKIPPED: {', '.join(bad)}. Set the real "
              "id from each project URL; the API is never called with these.")
    return real


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
        r = requests.get(f"{config.opus_api_base()}{path}", params=params or {},
                         headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            snippet = ops_alerts.scrub((r.text or "")[:400])
            raise OpusScanError(r.status_code, snippet)
        return r.json()

    def list_collections(self):
        """GET /api/collections?q=mine -> [collection ids], paginated. ID
        EXTRACTION IS SHAPE-TOLERANT: the live response carried objects whose
        id key is not top-level "id" (opus-check saw 3 objects, the old parse
        extracted 0). Every plausible key is tried; a string item is its own
        id; and an extraction mismatch is a LOUD warning naming the keys seen,
        never a silent zero."""
        ids, total_objects, page = [], 0, 1
        while True:
            body = self._get("/api/collections",
                             {"q": "mine", "pageNum": page, "pageSize": 50}) or {}
            items = normalize_list_response(body)
            total_objects += len(items)
            for c in items:
                cid = _extract_id(c)
                if cid:
                    ids.append(cid)
            if len(items) < 50:
                break
            page += 1
        if total_objects and len(ids) != total_objects:
            sample_keys = sorted((items[0] or {}).keys()) if items and isinstance(
                items[0], dict) else type(items[0]).__name__ if items else "?"
            print(f"[opus] WARNING: extracted {len(ids)} id(s) from "
                  f"{total_objects} collection object(s). Response keys seen: "
                  f"{sample_keys}. Fix _extract_id for this shape.")
        elif total_objects:
            print(f"[opus] collections: {len(ids)} id(s) from "
                  f"{total_objects} object(s)")
        return ids

    def list_collections_detailed(self):
        """
        GET /api/collections?q=mine -> [{"id", "title"}], paginated. The PROVEN
        discovery route for the video factory (the documented Opus API has NO
        bulk project-listing endpoint, so /api/projects does not exist). Id
        extraction reuses the shape-tolerant _extract_id; the title falls back
        across title/name/collectionName. A collection with no resolvable id is
        skipped, never guessed. Parallel to list_collections (ids only), which
        the legacy poller still uses.
        """
        out, page = [], 1
        while True:
            body = self._get("/api/collections",
                             {"q": "mine", "pageNum": page, "pageSize": 50}) or {}
            items = normalize_list_response(body)
            for c in items:
                cid = _extract_id(c)
                if not cid:
                    continue
                title = ""
                if isinstance(c, dict):
                    title = str(c.get("title", "") or c.get("name", "")
                                or c.get("collectionName", "") or "")
                out.append({"id": cid, "title": title})
            if len(items) < 50:
                return out
            page += 1

    def list_exportable_clips(self, q, source_id):
        """GET /api/exportable-clips, paginated. q = findByProjectId|findByCollectionId."""
        id_param = "projectId" if q == "findByProjectId" else "collectionId"
        clips, page = [], 1
        while True:
            body = self._get("/api/exportable-clips",
                             {"q": q, id_param: source_id,
                              "pageNum": page, "pageSize": 50}) or {}
            items = normalize_list_response(body)
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


def _shape_desc(body):
    """A short STRUCTURE-ONLY description of a parsed JSON body: its top-level
    type and (for a dict) its key names, capped. Never prints values, so it is
    safe to log even when the body could carry tokens or PII. Used to diagnose
    the real response shape (list vs id-keyed dict vs wrapper)."""
    if isinstance(body, list):
        return f"list[{len(body)}]"
    if isinstance(body, dict):
        keys = list(body.keys())
        shown = keys[:10]
        more = "" if len(keys) <= 10 else f" (+{len(keys) - 10} more)"
        return f"dict keys={shown}{more}"
    return type(body).__name__


def _extract_id(item):
    """A collection object's id, whatever the shape: string items are their own
    id; dicts are tried across every plausible key, then any *Id-suffixed key."""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    for key in ("id", "collectionId", "collection_id", "uuid", "_id"):
        v = item.get(key)
        if v:
            return str(v)
    for key, v in item.items():
        if key.lower().endswith("id") and isinstance(v, (str, int)) and v:
            return str(v)
    return ""


# Wrapper keys a list-style Opus response may nest its records under.
_LIST_WRAPPER_KEYS = ("data", "collections", "clips", "items", "results", "docs")


def _dict_to_records(d):
    """A bare dict container -> a flat list of record dicts. When the dict is an
    id-keyed map ({<id>: {..record..}}) each key is injected as the record's id
    (only if the record has none); non-dict values are skipped. A scalar-only
    dict (e.g. metadata like {"total": 0}) yields [], never a fabricated record."""
    if not d:
        return []
    dict_items = [(k, v) for k, v in d.items() if isinstance(v, dict)]
    if not dict_items:
        return []
    out = []
    for k, v in dict_items:
        rec = dict(v)
        rec.setdefault("id", k)
        out.append(rec)
    return out


def normalize_list_response(body):
    """
    THE single normalizer for Opus list-style responses (collections and clips),
    used by both the client list methods (so scan() is covered) and opus-doctor.

    Returns a plain list of record dicts regardless of the wire shape:
      - a bare list                          -> the list (None items dropped)
      - a wrapper {data|collections|clips|items|results|docs: <list or dict>}
                                             -> the unwrapped, normalized container
      - a bare id-keyed dict {<id>: {...}}   -> [{"id": <id>, ...}, ...]
      - a single record dict                 -> [that record]
      - None / empty / unknown               -> []
    NEVER indexes and never assumes a list; safe on any shape.
    """
    if body is None:
        return []
    if isinstance(body, list):
        return [x for x in body if x is not None]
    if isinstance(body, dict):
        for key in _LIST_WRAPPER_KEYS:
            if key in body:
                return normalize_list_response(body[key])
        return _dict_to_records(body)
    return []


def _default_api():
    key = os.environ.get(config.OPUS_API_KEY_ENV)
    if not key:
        return None
    print(f"[opus] key prefix: {key[:6]}... (confirm this matches the active key in Railway)")
    return OpusAPI(key, config.opus_org_id())


def _sources(api, vprint=lambda *a, **k: None):
    """(q, id) pairs to scan: pinned project ids + pinned collections, else every
    collection the account owns (the documented discovery path). Placeholder
    project ids (P1 pattern / under 6 chars) are warned about and never scanned;
    a pin list that is ALL placeholders behaves like no pin list at all."""
    project_ids = validated_project_ids()
    sources = [("findByProjectId", pid) for pid in project_ids]
    collections = config.OPUS_COLLECTION_IDS
    if project_ids or collections:
        vprint(f"[opus] discovery: PINNED ids ({len(project_ids)} project, "
               f"{len(collections)} collection)")
    if not collections and not sources:
        try:
            collections = api.list_collections()
            vprint(f"[opus] discovery: collections endpoint (q=mine) returned "
                   f"{len(collections)} collection id(s): {collections}")
        except Exception as e:
            print(f"[opus] could not list collections: {type(e).__name__}: {e}")
            collections = []
    sources.extend(("findByCollectionId", cid) for cid in collections)
    vprint(f"[opus] scanning {len(sources)} source(s)")
    return sources


def _slug(text, limit=40):
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")[:limit]


def _alert_failure(state, source_key, message):
    """Debounced ingest failure alert: ONE Slack alert per source (project or
    collection) per day, not per hourly run. Repeat failures within the same
    day keep the same honest message but land in the audit log only. The
    dead-letter escalation does NOT pass through here: it fires at most once
    per clip ever (the clip enters the dead set), so it is never spam and a
    terminal give-up must stay visible. The stamp persists in opus_state.json,
    so restarts within the day do not re-alert."""
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).date().isoformat()
    alerted = state.setdefault("alert_days", {})
    if alerted.get(source_key) == day:
        from . import db
        db.audit("ops_alert", "debounced", message, day=day)  # audit scrubs
        return
    for k in [k for k, v in alerted.items() if v != day]:  # drop stale days
        alerted.pop(k)
    alerted[source_key] = day
    ops_alerts.alert(message)


def pull(api=None, s3_client=None, poster=None, out_dir=None, verbose=False):
    """
    One ingest pass: list new finished clips since the watermark, download, host to
    R2 (content addressed, dedupe by hash), file into the library as a video asset
    with its sidecar, and print one hosted URL per clip. Returns a summary dict, or
    None while AGENT_OPUS_ENABLED is OFF. Never raises for a single bad clip.

    verbose prints per-step debug (discovery route, per-source clip counts, and the
    WHY for every skipped clip). The API key and auth headers are NEVER printed.
    """
    vprint = print if verbose else (lambda *a, **k: None)
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

    sources_scanned = []
    for q, source_id in _sources(api, vprint):
        sources_scanned.append(f"{q}:{source_id}")
        source_key = f"{q}:{source_id}"
        watermark = watermarks.get(source_key, "")
        try:
            clips = api.list_exportable_clips(q, source_id)
        except Exception as e:
            summary["failed"] += 1
            _alert_failure(state, source_key,
                           f"opus ingest could not list clips for {source_id}: "
                           f"{type(e).__name__}: {e}")
            continue
        vprint(f"[opus] {source_key}: {len(clips)} clip(s) listed "
               f"(watermark: {watermark or 'none'})")

        # The watermark only advances past RESOLVED clips (ingested, deduped, or
        # dead-lettered). It stalls at the first unresolved failure so the next
        # pull retries that clip instead of silently losing it.
        newest = watermark
        stalled = False
        for clip in sorted(clips, key=lambda c: c.get("createdAt", "")):
            created = clip.get("createdAt", "")
            clip_id = clip.get("id", "")
            title = clip.get("title", "")

            def _vskip(reason):
                vprint(f"[opus]   clip {clip_id or '(no id)'} '{title[:40]}': "
                       f"SKIPPED, {reason}")

            if not clip_id or not clip.get("uriForExport"):
                _vskip("not exportable (missing id or export URL)")
                continue
            if created and created <= watermark:
                _vskip(f"watermark ({created} <= {watermark})")
                continue  # already covered by the watermark
            if clip_id in ingested or clip_id in dead:
                summary["skipped"] += 1
                _vskip("dead-lettered" if clip_id in dead else "already ingested")
                if not stalled:
                    newest = max(newest, created)
                continue
            try:
                data = api.download(clip["uriForExport"])
                sha = hashlib.sha256(data).hexdigest()
                if sha in hashes:
                    summary["skipped"] += 1     # same content pulled before
                    _vskip("hash dedupe (same bytes already in the library)")
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
                    _alert_failure(state, source_key,
                                   f"opus ingest failed for clip {clip_id} "
                                   f"(attempt {failures[clip_id]}): {type(e).__name__}: {e}")
        watermarks[source_key] = newest

    state["ingested_ids"] = sorted(ingested)
    state["hashes"] = sorted(hashes)
    state["deadletter"] = sorted(dead)
    save_state(state)
    if summary == {"pulled": 0, "skipped": 0, "failed": 0}:
        if not sources_scanned:
            print("pull-opus: ZERO sources to scan. Projects are not collections "
                  "and auto discovery only sees collections. Either add your "
                  "clips to a collection in the OpusClip dashboard, or set "
                  "AGENT_OPUS_PROJECT_IDS to the real ids copied from each "
                  "project URL (comma separated; example tokens like P1 are "
                  "rejected) on the listener service. Run opus-check for the probe.")
        else:
            print(f"pull-opus: scanned {len(sources_scanned)} source(s), zero "
                  "new clips. Nothing matched the watermark window; use "
                  "--verbose to see per clip reasons.")
    return summary


def opus_check(http=None):
    """
    READ-ONLY connectivity probe (CLI: opus-check). Calls the collections endpoint
    directly, prints the HTTP status and how many collections came back, and when
    zero (or on an error / non-JSON body) prints the raw response body, TRUNCATED
    and key-scrubbed, so we can see whether the account looks empty to this key.
    Never prints the API key or any auth header. Returns a small summary dict.
    """
    key = os.environ.get(config.OPUS_API_KEY_ENV)
    if not key:
        print("opus-check: OPUS_API_KEY is not set.")
        return {"status": None, "collections": None}

    if http is None:
        import requests  # lazy
        http = requests
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    org = config.opus_org_id()
    if org:
        headers["x-opus-org-id"] = org
    url = f"{config.opus_api_base()}/api/collections"
    try:
        r = http.get(url, params={"q": "mine"}, headers=headers, timeout=30)
    except Exception as e:
        print(f"opus-check: request failed: {type(e).__name__}: {e}")
        return {"status": None, "collections": None}

    status = getattr(r, "status_code", 0)
    body_text = getattr(r, "text", "") or ""
    count = None
    try:
        body = r.json()
        items = body if isinstance(body, list) else (body or {}).get("data", []) or []
        count = len(items)
    except Exception:
        pass  # non-JSON body; shown below

    print(f"opus-check: GET /api/collections?q=mine -> HTTP {status}")
    if count is not None:
        print(f"opus-check: {count} collection(s) returned")
    else:
        print("opus-check: response body is not JSON")
    pinned = config.OPUS_PROJECT_IDS
    print(f"opus-check: AGENT_OPUS_PROJECT_IDS pinned: "
          f"{', '.join(pinned) if pinned else 'none'}")
    # EXACT remediation per detected case
    if status in (401, 403):
        print("opus-check: REMEDIATION: the key was rejected. Regenerate "
              "OPUS_API_KEY in the OpusClip dashboard (lower left) and set it "
              "on the listener service; multi org accounts also need "
              "AGENT_OPUS_ORG_ID.")
    elif status < 400 and count == 0 and not pinned:
        print("opus-check: REMEDIATION: your key sees NO collections and no "
              "project ids are pinned. Projects are not collections. Either "
              "add the clips to a collection in the OpusClip dashboard, or set "
              "AGENT_OPUS_PROJECT_IDS to the real ids copied from each project "
              "URL (comma separated; example tokens like P1 are rejected).")
    elif status < 400 and count == 0 and pinned:
        print("opus-check: pinned project ids will be scanned directly; the "
              "empty collection list is fine. Run pull-opus --verbose.")
    elif status < 400 and (count or 0) > 0:
        print("opus-check: READY: collections are visible; pull-opus will scan "
              "them (plus any pinned project ids).")
    if status >= 400 or not count:
        from .ops_alerts import scrub  # key-scrub anything echoed back
        snippet = scrub(body_text)[:500]
        print(f"opus-check: raw body (truncated, scrubbed): {snippet!r}")
    return {"status": status, "collections": count}


def opus_doctor(http=None):
    """
    READ-ONLY preflight for the Opus video factory (CLI: opus-doctor). Gated by
    AGENT_OPUS_FACTORY_ENABLED. Makes ONE lightweight call to the PROVEN discovery
    route GET /api/collections?q=mine (pageSize=1) and reports:
      • key prefix (first 6 chars only — which key is in use, never the value)
      • resolved base URL
      • HTTP status
      • collection count visible to this key
      • the first collection's raw id, title, and status field

    This is the operator's is-it-key-or-route test. It NEVER collapses the two:
      - 404  -> ENDPOINT WRONG (the route/base URL is bad, not the key)
      - 401/403 -> AUTH WRONG (the route exists, the key was rejected)
    Returns a small dict. Never prints the full key or auth headers.
    """
    from . import config as _cfg
    if not _cfg.opus_factory_enabled():
        print("opus-doctor: AGENT_OPUS_FACTORY_ENABLED is OFF. "
              "Set it to true to run this check.")
        return {"enabled": False}

    key = os.environ.get(_cfg.OPUS_API_KEY_ENV)
    if not key:
        print("opus-doctor: OPUS_API_KEY is not set in the environment. "
              "Set it by hand in Railway env vars (never commit it).")
        return {"enabled": True, "key_present": False}

    base = _cfg.opus_api_base()
    print(f"opus-doctor: key prefix {key[:6]}... "
          f"(if this is 'sk-2vtU' the key is the rotated/leaked one — "
          f"set the NEW key in Railway and redeploy)")
    print(f"opus-doctor: resolved base URL {base}")

    if http is None:
        import requests
        http = requests
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    org = _cfg.opus_org_id()
    if org:
        headers["x-opus-org-id"] = org
    url = f"{base}/api/collections"   # the PROVEN discovery route (not /api/projects)
    try:
        r = http.get(url, params={"q": "mine", "pageNum": 1, "pageSize": 1},
                     headers=headers, timeout=30)
    except Exception as e:
        print(f"opus-doctor: request failed: {type(e).__name__}: {e}")
        return {"enabled": True, "key_present": True, "status": None,
                "base_url": base, "endpoint_ok": None, "auth_ok": None,
                "collections": None}

    status = getattr(r, "status_code", 0)
    body_text = getattr(r, "text", "") or ""
    print(f"opus-doctor: GET /api/collections?q=mine -> HTTP {status}")

    if status == 404:
        print("opus-doctor: ENDPOINT WRONG (404). The route does not exist at "
              f"{base}. This is NOT the key. Check AGENT_OPUS_API_BASE and that "
              "the /api/collections path matches the current Opus API docs.")
        snippet = ops_alerts.scrub(body_text)[:400]
        print(f"opus-doctor: raw body (scrubbed): {snippet!r}")
        return {"enabled": True, "key_present": True, "status": 404,
                "base_url": base, "endpoint_ok": False, "auth_ok": None,
                "collections": None}

    if status in (401, 403):
        print("opus-doctor: AUTH WRONG. The endpoint exists but the key was "
              "rejected. This is NOT the route. Generate a new API key in the "
              "OpusClip dashboard and set OPUS_API_KEY in Railway, then redeploy.")
        snippet = ops_alerts.scrub(body_text)[:400]
        print(f"opus-doctor: raw body (scrubbed): {snippet!r}")
        return {"enabled": True, "key_present": True, "status": status,
                "base_url": base, "endpoint_ok": True, "auth_ok": False,
                "collections": None}

    if status >= 400:
        snippet = ops_alerts.scrub(body_text)[:400]
        print(f"opus-doctor: unexpected HTTP {status}. Body: {snippet!r}")
        return {"enabled": True, "key_present": True, "status": status,
                "base_url": base, "endpoint_ok": None, "auth_ok": None,
                "collections": None}

    try:
        body = r.json()
    except Exception:
        print(f"opus-doctor: response is not JSON. Body: "
              f"{ops_alerts.scrub(body_text)[:200]!r}")
        return {"enabled": True, "key_present": True, "status": status,
                "base_url": base, "endpoint_ok": True, "auth_ok": True,
                "collections": None}

    # STRUCTURE ONLY (never values that could carry tokens/PII): log the top-level
    # type and key names so the real collections shape is captured for diagnosis.
    print(f"opus-doctor: response shape: {_shape_desc(body)}")

    items = body if isinstance(body, list) else (body or {}).get("data", []) or []
    total_hint = (body or {}).get("total") or (body or {}).get("totalCount")
    print(f"opus-doctor: {len(items)} collection(s) in this page "
          f"(total hint from API: {total_hint})")
    if items:
        first = items[0] if isinstance(items[0], dict) else {}
        cid = _extract_id(items[0]) or "(no id field)"
        ctitle = (first.get("title") or first.get("name")
                  or first.get("collectionName") or "")
        cstatus = (first.get("status") or first.get("collectionStatus")
                   or "(no status field)")
        print(f"opus-doctor: first collection  id={cid!r}  title={ctitle!r}  "
              f"raw status={cstatus!r}")
    else:
        print("opus-doctor: endpoint + key OK, but NO collections are visible to "
              "this key. Organize the clips into a collection in the OpusClip "
              "dashboard, or check org-id scoping (AGENT_OPUS_ORG_ID).")
    return {"enabled": True, "key_present": True, "status": status,
            "base_url": base, "endpoint_ok": True, "auth_ok": True,
            "collections": len(items)}
