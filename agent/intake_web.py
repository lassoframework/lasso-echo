"""
Texted-link intake: the client-facing upload page + the client intake FORM.

A SEPARATE web process (own start command: `python -m agent intake-web`), deployable
as its own Railway service. HARD CONSTRAINT honored: this process touches R2 ONLY,
never /data (the volume belongs to the listener service; the listener's ingest step
picks uploads AND form submissions up from R2).

Upload flow: the client taps their private tokenized link (/u/<token>), picks
photos or videos, types one optional sentence, hits send. Files land in R2 under
intake/<client>/incoming/ with a sidecar JSON (note, client + token fingerprint,
timestamp, filenames). The raw token is never logged AND never persisted; the
sidecar carries a sha256 fingerprint instead.

Intake form flow: the gym fills the LASSO social intake at /intake/<token>
(seven sections: gym basics, brand voice, offers and services with the exact
wording pricing rule, audience, proof, media notes, approver). The submission
lands in R2 as <stamp>_intake.json; the LISTENER's ingest pass routes the fact
sections through client_sources.submit_intake() as PENDING per account sources
(never auto approved) and holds the approver/basics as an account proposal. The
confirmation page immediately offers the media upload link for the same token so
photos come in the same sitting.

Gates: everything is 404 unless AGENT_INTAKE_ENABLED=true (default OFF) and the
token authenticates. Tokens are SIGNED with one shared secret
(AGENT_INTAKE_SIGNING_SECRET) so no per-gym env var is ever needed; legacy
per-client env values (AGENT_INTAKE_TOKEN_<CLIENTKEY>) still verify for a
zero-downtime cutover. Guardrails: content-type allowlist (images + common
video), per-file and per-request size caps, a basic per-IP rate limit, and no
directory listing (only /u/<token> and /intake/<token> exist; every other path
404s).
"""

import hashlib
import hmac
import io
import json
import os
import re
import time
from datetime import datetime, timezone

from . import config, ghl_intake, intake_tokens, whatsapp_intake
from . import portal_routes as _pr
from . import portal_social as _ps
from . import zernio_routes as _zr

_TOKEN_ENV_PREFIX = "AGENT_INTAKE_TOKEN_"
_TRACKER_TOKEN_ENV = "AGENT_TRACKER_TOKEN"   # name only; value is set by hand

ALLOWED_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
    "video/mp4", "video/quicktime",
}


def _max_file_bytes():
    return int(os.environ.get("AGENT_INTAKE_MAX_FILE_MB", "100")) * 1024 * 1024


def _max_request_bytes():
    return int(os.environ.get("AGENT_INTAKE_MAX_REQUEST_MB", "300")) * 1024 * 1024


def _rate_per_minute():
    return int(os.environ.get("AGENT_INTAKE_RATE_PER_MINUTE", "10"))


def client_for_token(token):
    """The client key a token authenticates, or None. A SIGNED token verifies
    against the shared secret (no per-gym env var needed); a legacy per-client env
    value (AGENT_INTAKE_TOKEN_<KEY>) still matches so the cutover is zero-downtime.
    The token value is never logged. Pure: no I/O beyond reading env. Revocation
    is enforced separately (see is_revoked)."""
    if not token:
        return None
    signed = intake_tokens.verify(token)
    if signed is not None:
        return signed
    for name, value in os.environ.items():
        if name.startswith(_TOKEN_ENV_PREFIX) and value and value == token:
            return name[len(_TOKEN_ENV_PREFIX):].lower()
    return None


# The token charset: b64url alphabet plus the '.' that separates a signed
# token's payload from its signature. Anchored, slash-free, so no path traversal.
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_.-]{8,}$")


def token_from_path(path, prefix):
    """The token in /`prefix`/<token>, or None. Pure and unit-testable.

    A signed token is b64url(account_key).b64url(sig): the DOT is load-bearing.
    A gym opens this link from a text message, and messaging clients, link
    previewers, and some HTTP stacks routinely percent-encode the '.' (to %2E),
    append a trailing slash, or tack on trailing whitespace (a stray %0A/%20).
    Any of those made the raw-path regex miss and returned a 404 on a perfectly
    valid link (this is the Dale Suslick 'not found' bug). So we:
      1. drop the query string,
      2. URL-decode the path once (so %2E becomes '.', %20 becomes ' '),
      3. strip one trailing slash and surrounding whitespace,
    THEN match the anchored token charset. The token itself is never widened:
    only in-transit mangling is undone, so a genuinely bad path still 404s and
    the value never reaches client_for_token unverified."""
    from urllib.parse import unquote
    raw = (path or "").split("?", 1)[0]
    marker = f"/{prefix}/"
    if not raw.startswith(marker):
        return None
    tail = unquote(raw[len(marker):])
    tail = tail.strip().rstrip("/").strip()
    if "/" in tail:               # never span a path segment (no traversal)
        return None
    return tail if _TOKEN_CHARS.match(tail) else None


# ---- per-gym revocation (an R2 denylist; this service touches R2 only) ----------
# Rotating the shared secret kills EVERY link; the denylist kills ONE gym's link
# without touching the rest. It lives in R2 (never /data) so the intake-web
# process can read it at verify time. Read fresh each request so a kill switch
# takes effect immediately; fail OPEN on a flaky read (a denylist outage never
# takes every intake link down, and a revoked link is re-killed when R2 recovers).
_DENYLIST_KEY = "intake/_control/denylist.json"


def _read_denylist(r2):
    """The denylist dict from R2; {'revoked': []} when the object does not exist.
    RAISES on a real storage error, so the WRITE path never clobbers a denylist it
    failed to read (get_bytes returns None only on a missing key)."""
    raw = r2.get_bytes(_DENYLIST_KEY)
    if not raw:
        return {"revoked": []}
    data = json.loads(raw)
    if isinstance(data, dict) and isinstance(data.get("revoked"), list):
        return data
    return {"revoked": []}


def revoked_clients(r2=None):
    """The set of revoked client keys, or empty on any problem (fail OPEN on the
    verify path). Read fresh so revocation is immediate."""
    r2 = r2 or _default_r2()
    if r2 is None:
        return set()
    try:
        data = _read_denylist(r2)
    except Exception:
        return set()
    return {str(k).strip().lower()
            for k in data.get("revoked", []) if str(k).strip()}


def is_revoked(client, r2=None):
    """True when this client key is on the R2 denylist. A revoked link is a 404
    everywhere, exactly like an unknown token."""
    if not client:
        return False
    return client.strip().lower() in revoked_clients(r2)


def _write_denylist(client_key, r2, now, add):
    client_key = (client_key or "").strip().lower()
    if not client_key:
        raise ValueError("client_key is required")
    r2 = r2 or _default_r2()
    if r2 is None:
        raise RuntimeError("storage unavailable")
    data = _read_denylist(r2)   # RAISES on a real read error (never clobbers)
    current = {str(k).strip().lower()
               for k in data.get("revoked", []) if str(k).strip()}
    if add:
        current.add(client_key)
    else:
        current.discard(client_key)
    data["revoked"] = sorted(current)
    data["updated"] = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    r2.put_bytes(_DENYLIST_KEY, json.dumps(data).encode("utf-8"),
                 content_type="application/json")
    return data["revoked"]


def revoke(client_key, r2=None, now=None):
    """Add a client key to the R2 denylist (idempotent). Its link 404s on the next
    verify. Returns the new sorted revoked list."""
    return _write_denylist(client_key, r2, now, add=True)


def unrevoke(client_key, r2=None, now=None):
    """Remove a client key from the R2 denylist (idempotent). Its link works
    again. Returns the new sorted revoked list."""
    return _write_denylist(client_key, r2, now, add=False)


# The canonical public origin of THIS service (echo-intake-web on Railway). Used
# as the fallback when AGENT_UPLOAD_BASE_URL is unset or still carries a setup
# placeholder, so a forgotten env var can never leak a "<paste ...>/u/<token>"
# link to a client. Override by setting AGENT_UPLOAD_BASE_URL to a real https URL.
_DEFAULT_UPLOAD_BASE_URL = "https://echo-intake-web-production.up.railway.app"


def _upload_base_url():
    """The absolute base URL for tokenized links, always without a trailing slash.
    Reads AGENT_UPLOAD_BASE_URL; a blank value, a value that is not http(s), or a
    leftover setup placeholder (contains '<' or the word 'paste') is treated as
    unset and falls back to the canonical service origin. This is the ONE place the
    base URL is resolved, so every link builder gets the same guard."""
    raw = os.environ.get("AGENT_UPLOAD_BASE_URL", "").strip()
    if (not raw) or ("<" in raw) or ("paste" in raw.lower()) \
            or not raw.lower().startswith(("http://", "https://")):
        return _DEFAULT_UPLOAD_BASE_URL
    return raw.rstrip("/")


def link_for(client_key, kind="u"):
    """The full signed intake link for a client key, or '' when it cannot be built
    (no signing secret set). kind='u' is the media upload page; kind='intake' is
    the seven-section form. This is the ONE place a link is minted: the intake-link
    CLI calls it today and a future authenticated mint endpoint calls the same
    function, so the signing secret never leaves this service and never reaches the
    ops portal. Absolute when AGENT_UPLOAD_BASE_URL is set, else a relative path."""
    try:
        token = intake_tokens.mint(client_key)
    except ValueError:
        return ""
    path = "intake" if kind == "intake" else "u"
    base = _upload_base_url()
    return f"{base}/{path}/{token}"


# ---- basic per-IP rate limit (in-memory; this is one small service) -----------
_hits = {}


def allow_request(ip, now=None):
    now = now if now is not None else time.monotonic()
    window = [t for t in _hits.get(ip, []) if now - t < 60.0]
    if len(window) >= _rate_per_minute():
        _hits[ip] = window
        return False
    window.append(now)
    _hits[ip] = window
    return True


# ---- per-token rate limit (separate from IP; blocks only abuse) ----------------
# 20 requests per minute per token (keyed by SHA-256 hash prefix, not raw token).
_TOKEN_RATE_PER_MINUTE = 20
_token_hits = {}


def _token_hash_prefix(token):
    """First 16 hex chars of the SHA-256 of the token (never the raw token)."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def allow_token_request(token_hash, now=None):
    """
    Sliding-window rate limit keyed by the first 16 chars of the SHA-256 hash.
    Returns False when over 20 requests per minute; True otherwise.
    The raw token is never passed here; callers pass the hash prefix.
    """
    now = now if now is not None else time.monotonic()
    window = [t for t in _token_hits.get(token_hash, []) if now - t < 60.0]
    if len(window) >= _TOKEN_RATE_PER_MINUTE:
        _token_hits[token_hash] = window
        return False
    window.append(now)
    _token_hits[token_hash] = window
    return True


def validate_files(files):
    """(ok, reason). files = [(filename, content_type, data_bytes), ...]"""
    if not files:
        return False, "no files"
    total = 0
    for filename, ctype, data in files:
        if (ctype or "").lower() not in ALLOWED_TYPES:
            return False, f"file type not allowed: {ctype or 'unknown'}"
        if len(data) > _max_file_bytes():
            return False, f"file too large: {filename}"
        total += len(data)
    if total > _max_request_bytes():
        return False, "upload too large"
    return True, ""


def _safe_name(filename):
    base = os.path.basename(filename or "upload")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "upload"


def handle_upload(token, files, note="", captions=None, r2=None, now=None):
    """
    The whole upload decision, pure and offline-testable. Returns (status, body).
    404 whenever the feature is off or the token is unknown (indistinguishable on
    purpose); 429 rate-limited (handled by the HTTP layer); 400 bad files; 200 ok.

    captions: an optional list, one caption per file IN THE SAME ORDER as `files`
    (the gallery page sends one caption field per media field). Each caption is the
    gym's one line about that specific photo or video. It is optional and never
    required: a missing or blank caption is simply omitted. The sidecar keeps a
    per-file `captions` map keyed by the STORED basename, alongside the existing
    batch-wide `note`, so a plain single `note` still works with nothing else set.
    """
    if not config.intake_enabled():
        return 404, {"error": "not found"}
    client = client_for_token(token)
    if client is None or is_revoked(client, r2):
        return 404, {"error": "not found"}

    ok, reason = validate_files(files)
    if not ok:
        return 400, {"error": reason}

    r2 = r2 or _default_r2()
    if r2 is None:
        return 503, {"error": "storage unavailable"}

    # Per-tenant storage quota (Part 9): a MEASURED total over the tenant's cap
    # refuses the upload (413); storage that cannot report a total, or a client
    # with no tenant record (legacy env-token clients), never blocks. Originals
    # are streamed to R2 unmodified below (HEIC/MOV allowed, EXIF kept).
    from . import quotas
    incoming = sum(len(data) for _f, _c, data in files)
    used = None
    try:
        used = r2.total_bytes(f"intake/{client}/")
    except AttributeError:
        pass  # this wrapper cannot measure; quota unenforceable, never guessed
    except Exception:
        pass  # a flaky listing never blocks an upload
    if quotas.over_quota(client, used, incoming):
        return 413, {"error": "storage quota exceeded; ask us to raise it"}

    captions = list(captions or [])
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    stored = []
    caption_map = {}
    # Every R2 write is wrapped: a storage failure MID-upload (creds accepted at
    # construction but rejected by R2, a transient network fault, a bad bucket)
    # must never bubble out of the HTTP handler as an unhandled 500/503 with a
    # misleading "not found" body. We return an honest 503 the UI can show. The
    # exception is logged (scrubbed) so a live misconfig is diagnosable.
    try:
        for idx, (filename, ctype, data) in enumerate(files):
            key = f"intake/{client}/incoming/{stamp}_{_safe_name(filename)}"
            r2.put_bytes(key, data, content_type=ctype)
            base = os.path.basename(key)
            stored.append(base)
            cap = ""
            if idx < len(captions):
                cap = (captions[idx] or "").strip()[:200]
            if cap:
                caption_map[base] = cap
        sidecar = {
            "note": (note or "").strip()[:500],
            "client": client,
            # never the raw token: a fingerprint traces which link was used
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "timestamp": stamp,
            "filenames": stored,
            # per-file caption map (STORED basename -> the gym's one line about it);
            # backward compatible: absent/empty when only a batch note was sent.
            "captions": caption_map,
        }
        r2.put_bytes(f"intake/{client}/incoming/{stamp}_upload.json",
                     json.dumps(sidecar).encode("utf-8"),
                     content_type="application/json")
    except Exception as e:
        from . import ops_alerts as _oa
        print(f"[intake-web] upload write failed for {client}: "
              f"{type(e).__name__}: {_oa.scrub(str(e))}")
        return 503, {"error": "storage unavailable"}
    return 200, {"ok": True, "stored": len(stored)}


# The intake form's field names, one tuple per section (section order preserved).
FORM_FIELDS = (
    "gym_name", "city", "website", "about",          # 1. gym basics
    "voice",                                          # 2. brand voice
    "offers", "services", "pricing_rule",             # 3. offers and services
    "audience",                                       # 4. audience
    "proof",                                          # 5. proof
    "media_notes",                                    # 6. media notes
    "approver_name", "approver_contact",              # 7. approver
)

_FIELD_MAX = 4000


def handle_intake_form(token, fields, r2=None, now=None):
    """
    The whole form-submission decision, pure and offline-testable. Returns
    (status, body). 404 whenever the feature is off or the token is unknown
    (indistinguishable on purpose); 400 when the form is effectively empty;
    503 without storage; 200 ok. The payload lands in R2 as
    intake/<client>/incoming/<stamp>_intake.json for the LISTENER's ingest pass
    to route through submit_intake() — this process never touches /data.
    """
    if not config.intake_enabled():
        return 404, {"error": "not found"}
    client = client_for_token(token)
    if client is None or is_revoked(client, r2):
        return 404, {"error": "not found"}

    answers = {k: (fields.get(k) or "").strip()[:_FIELD_MAX] for k in FORM_FIELDS}
    if not answers["gym_name"]:
        return 400, {"error": "the gym name is required"}
    if not any(answers[k] for k in FORM_FIELDS if k != "gym_name"):
        return 400, {"error": "the form is empty"}

    r2 = r2 or _default_r2()
    if r2 is None:
        return 503, {"error": "storage unavailable"}

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "kind": "intake_form",
        "client": client,
        "answers": answers,
        # never the raw token: a fingerprint traces which link was used
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "timestamp": stamp,
    }
    r2.put_bytes(f"intake/{client}/incoming/{stamp}_intake.json",
                 json.dumps(payload).encode("utf-8"),
                 content_type="application/json")
    return 200, {"ok": True, "client": client}


# ---- the portal intake API (JSON POST from the ops portal) ----------------------
def portal_origin():
    """The single origin allowed to call the JSON intake endpoint cross-origin
    (env AGENT_INTAKE_PORTAL_ORIGIN, e.g. https://portal.lassoframework.com).
    Default empty = same-origin only; never all origins."""
    return os.environ.get("AGENT_INTAKE_PORTAL_ORIGIN", "").strip().rstrip("/")


def origin_allowed(origin, host):
    """True when a request Origin may hit the JSON endpoint: absent (server to
    server, no CORS in play), same-origin (its host equals our Host header), or
    exactly the configured portal origin."""
    if not origin:
        return True
    from urllib.parse import urlparse
    if host and urlparse(origin).netloc == host:
        return True
    allowed = portal_origin()
    return bool(allowed) and origin.rstrip("/") == allowed


def _lines(value):
    """A JSON value as newline-joined text: lists join, strings pass, else ''. """
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip() if value else ""


def normalize_portal_intake(body):
    """The portal's nested 7-section JSON flattened to the intake answers shape
    the listener's ingest already lands (fact sections -> PENDING sources; gym
    basics + approver -> the HELD account proposal; the rest is bible material
    kept in the archived payload)."""
    body = body or {}
    gym = body.get("gym") or {}
    voice = body.get("voice") or {}
    offers = body.get("offers") or {}
    audience = body.get("audience") or {}
    proof = body.get("proof") or {}
    approver = body.get("approver") or {}

    voice_parts = [
        f"Vibe: {_lines(voice.get('vibe'))}" if voice.get("vibe") else "",
        f"Words to use: {_lines(voice.get('words_to_use'))}"
        if voice.get("words_to_use") else "",
        f"Words to never use: {_lines(voice.get('words_to_never_use'))}"
        if voice.get("words_to_never_use") else "",
        f"Sample posts: {_lines(voice.get('sample_post_links'))}"
        if voice.get("sample_post_links") else "",
    ]
    audience_parts = [
        f"Ideal member: {_lines(audience.get('ideal_member'))}"
        if audience.get("ideal_member") else "",
        f"Prior struggles: {_lines(audience.get('prior_struggles'))}"
        if audience.get("prior_struggles") else "",
    ]
    name = str(approver.get("name", "")).strip()
    role = str(approver.get("role", "")).strip()
    contact = ", ".join(v for v in (str(approver.get("cell", "")).strip(),
                                    str(approver.get("email", "")).strip()) if v)
    return {
        "gym_name": str(gym.get("name", "")).strip(),
        "city": _lines(gym.get("locations")),
        "website": str(gym.get("website", "")).strip(),
        "ig_handle": str(gym.get("ig_handle", "")).strip(),
        "fb_page": str(gym.get("fb_page", "")).strip(),
        "about": "",
        "voice": "\n".join(p for p in voice_parts if p),
        "offers": _lines(offers.get("front_door_offer")),
        "services": _lines(offers.get("services")),
        "pricing_rule": _lines(offers.get("exact_pricing_wording")),
        "audience": "\n".join(p for p in audience_parts if p),
        "proof": "\n".join(v for v in (_lines(proof.get("wins")),
                                       _lines(proof.get("verifiable_numbers"))) if v),
        "media_notes": _lines(body.get("media_notes")),
        "approver_name": f"{name} ({role})" if name and role else (name or role),
        "approver_contact": contact,
    }


def _count_source_facts(answers):
    """How many facts this submission sends toward PENDING sources (the ingest
    lands them; anything already on file is collapsed there, so a re-POST can
    land fewer than this count)."""
    from .intake_ingest import _FORM_SOURCE_SECTIONS  # single source of truth
    n = 0
    for field, _category, _citation in _FORM_SOURCE_SECTIONS:
        for line in (answers.get(field) or "").splitlines():
            if line.strip().lstrip("-*").strip():
                n += 1
    return n


def handle_portal_intake(token, body, r2=None, now=None):
    """
    The whole portal-POST decision, pure and offline-testable. Returns
    (status, response_dict). 404 whenever the feature is off or the token is
    unknown (indistinguishable on purpose); 400 on an empty/invalid body; 503
    without storage; 200 with {status, account_key, pending_source_count,
    upload_url}. The payload lands in R2 for the LISTENER's ingest to route
    through submit_intake() as PENDING sources (this process never touches
    /data); a re-POST lands a fresh payload whose sources dedupe at ingest and
    whose account proposal replaces the held one in place.
    """
    if not config.intake_enabled():
        return 404, {"error": "not found"}
    client = client_for_token(token)
    if client is None or is_revoked(client, r2):
        return 404, {"error": "not found"}
    if not isinstance(body, dict):
        return 400, {"error": "a JSON object is required"}

    answers = {k: v[:_FIELD_MAX] for k, v in normalize_portal_intake(body).items()}
    if not answers["gym_name"]:
        return 400, {"error": "gym.name is required"}
    if not any(v for k, v in answers.items() if k != "gym_name"):
        return 400, {"error": "the intake is empty"}

    r2 = r2 or _default_r2()

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "kind": "intake_form",
        "source": "portal",
        "client": client,
        "answers": answers,
        "portal": body,   # the raw sections, archived for the bible draft
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "timestamp": stamp,
    }
    if r2 is not None:
        r2.put_bytes(f"intake/{client}/incoming/{stamp}_intake.json",
                     json.dumps(payload).encode("utf-8"),
                     content_type="application/json")
    else:
        # R2 not yet configured — log the payload locally so nothing is lost.
        import logging
        logging.getLogger(__name__).warning(
            "R2 unavailable; intake payload not archived: client=%s stamp=%s answers_keys=%s",
            client, stamp, list(answers.keys()),
        )

    base = _upload_base_url()
    resp = {
        "status": "received",
        "account_key": client,
        "pending_source_count": _count_source_facts(answers),
        "upload_url": f"{base}/u/{token}",
    }
    return 200, resp


def handle_portal_gym_status(account_key, r2=None):
    """
    Portal gym status endpoint (GET /portal/gym/<account_key>).

    Gated by AGENT_PORTAL_APPROVALS (config.portal_approvals_enabled()).
    Returns (status_code, response_dict).

    Response shape:
      account_key    - the gym's account key
      upload_link    - the reconstructed upload link (decrypted when
                       AGENT_INTAKE_ENC_KEY is set, else from upload_link column),
                       or null when unavailable
      token_status   - ACTIVE, REVOKED, or NOT_SET
      last_upload_at - timestamp of most recent object in R2 incoming/, or null
      upload_count   - count of objects in R2 incoming/, or null
      intake_status  - same as token_status (alias for the portal UI)

    Returns 403 when AGENT_PORTAL_APPROVALS is OFF.
    Returns 404 when the account_key is not found in the gyms table.
    """
    if not config.portal_approvals_enabled():
        return 403, {"error": "portal access is disabled"}

    from . import db as _db
    gym_row = _db.gym_get(account_key)
    if gym_row is None:
        return 404, {"error": "gym not found"}

    token_status_val = (gym_row.get("token_status") or "NOT_SET").upper()

    # Upload link: prefer decrypted reconstruction (AGENT_INTAKE_ENC_KEY set),
    # fall back to the plaintext upload_link column stored at onboard time.
    upload_link = gym_row.get("upload_link")
    try:
        from . import intake_tokens as _it
        raw = _it.decrypt_token(account_key)
        if raw:
            upload_link = _upload_base_url() + "/u/" + raw
    except Exception:
        pass  # encryption unavailable: use stored plaintext link

    # R2 metadata: last upload and count from intake/<account_key>/incoming/.
    last_upload_at = None
    upload_count = None
    if r2 is not None:
        prefix = f"intake/{account_key}/incoming/"
        try:
            keys = r2.list_keys(prefix) if hasattr(r2, "list_keys") else None
            if keys is not None:
                upload_count = len(keys)
                # last_upload_at from most recent key name (keys are stamped)
                media_keys = sorted(
                    (k for k in keys if not k.endswith("_upload.json")
                     and not k.endswith("_intake.json")),
                    reverse=True,
                )
                if media_keys:
                    # Extract the timestamp from the key basename (YYYYMMDDTHHMMSSz prefix)
                    basename = media_keys[0].rsplit("/", 1)[-1]
                    ts_match = re.match(r"(\d{8}T\d{6}Z)", basename)
                    if ts_match:
                        last_upload_at = ts_match.group(1)
        except Exception:
            pass  # R2 unavailable: report null, never guess

    return 200, {
        "account_key": account_key,
        "upload_link": upload_link,
        "token_status": token_status_val,
        "last_upload_at": last_upload_at,
        "upload_count": upload_count,
        "intake_status": token_status_val,
    }


# Account keys are lowercase slugs: the env-suffix convention (mint() lowercases
# before signing) and the same charset onboard.run() writes files under.
_ONBOARD_KEY_RE = re.compile(r"^[a-z0-9]+$")


def handle_portal_onboard(body):
    """
    The LASSO portal's self-serve onboard call, pure and offline-testable.
    Auth and the AGENT_PORTAL_APPROVALS gate are enforced by the caller (do_POST)
    BEFORE this runs. Returns (status_code, response_dict).

    Request body (already-parsed JSON):
      account_key   - lowercase [a-z0-9] slug (rejected otherwise -> 400)
      display_name  - required, non-empty gym name

    On success returns 200 with:
      account_key  - the slug
      raw_token    - the signed intake token (the portal encrypts + stores it)
      publish_off  - always True (publishing is OFF for every new gym)
      onboarded    - always True

    IDEMPOTENT: mint() is deterministic (HMAC-SHA256 of the lowercased key under the
    shared signing secret), so onboarding an already-onboarded gym returns the SAME
    live token, never rotating it and never erroring. onboard.run() itself is
    idempotent (it skips existing files and never re-randomizes).

    The raw token is NEVER logged. On any onboard failure a generic 500 is returned
    with no token and no secret detail.
    """
    if not isinstance(body, dict):
        return 400, {"error": "invalid body"}
    account_key = str(body.get("account_key", "")).strip()
    display_name = str(body.get("display_name", "")).strip()
    if not account_key or not _ONBOARD_KEY_RE.match(account_key):
        return 400, {"error": "invalid account_key: must be a lowercase [a-z0-9] slug"}
    if not display_name:
        return 400, {"error": "display_name is required"}

    # Force automint FOR THIS CALL ONLY so a token is minted the same way the CLI
    # does when AGENT_ONBOARD_AUTOMINT is armed. The signing secret must exist for
    # a real token; onboard.run() mints deterministically when it does. We restore
    # the prior env value afterward so we never leave the flag flipped globally.
    from . import onboard as _onboard
    _AUTOMINT = "AGENT_ONBOARD_AUTOMINT"
    _prev = os.environ.get(_AUTOMINT)
    os.environ[_AUTOMINT] = "true"
    try:
        result = _onboard.run(account_key, display_name, base_url=_upload_base_url())
    except Exception as exc:
        # Generic failure: no token, no secret, no internals leaked to the portal.
        print(f"[portal] onboard failed for {account_key}: {type(exc).__name__}")
        return 500, {"error": "onboard failed"}
    finally:
        if _prev is None:
            os.environ.pop(_AUTOMINT, None)
        else:
            os.environ[_AUTOMINT] = _prev

    # onboard.run sets token_minted to the raw token on a fresh mint, False on an
    # idempotent re-run (DB-backed mode), or None when minting was skipped. Since
    # mint() is deterministic, we ALWAYS recover the current live token from the
    # key so an idempotent re-run returns the same valid token, never nothing.
    raw_token = result.get("token_minted")
    if not isinstance(raw_token, str) or not raw_token:
        raw_token = _current_token_for(account_key)
    if not raw_token:
        # No signing secret configured: onboarding cannot mint a link.
        print(f"[portal] onboard for {account_key}: no signing secret, token unavailable")
        return 500, {"error": "onboard failed"}

    return 200, {
        "account_key": account_key,
        "raw_token": raw_token,
        "publish_off": True,
        "onboarded": True,
    }


def _current_token_for(account_key):
    """The gym's CURRENT valid signed token, recomputed from the shared secret, or
    None when no secret is configured. Deterministic: same token every call, so an
    already-onboarded gym gets its live token back without rotation. Never logged."""
    try:
        if not intake_tokens.secret_present():
            return None
        return intake_tokens.mint(account_key)
    except Exception:
        return None


def _is_socialapi_account(account_key):
    """True when this gym is routed to the SocialAPI lane. The connect/status
    endpoints exist ONLY for such gyms; a meta_direct gym gets a 404 so nothing
    leaks about routing it does not use."""
    try:
        from .accounts import get_account as _ga
        acct = _ga(account_key)
        return acct is not None and getattr(acct, "publish_route", "meta_direct") == "socialapi"
    except Exception:
        return False


def handle_portal_social_connect(account_key, http=None):
    """
    GET /portal/<token>/social-connect  (token already resolved to account_key).

    Returns the SocialAPI OAuth connect URL(s) the gym clicks to authorize their
    IG and FB. Gated by AGENT_PORTAL_APPROVALS. 404 for a non-SocialAPI gym so a
    gym's token can never reveal routing it does not use. Returns (status, dict).
    """
    if not config.portal_approvals_enabled():
        return 403, {"error": "portal access is disabled"}
    if not _is_socialapi_account(account_key):
        return 404, {"error": "not found"}

    from . import socialapi_store as _sstore
    brand_id = _sstore.get_brand_id(account_key)
    if not brand_id:
        return 409, {"error": "brand not created yet",
                     "detail": "run socialapi-onboard for this gym first"}

    if not config.socialapi_key():
        return 503, {"error": "SocialAPI not configured"}

    redirect_uri = os.environ.get("AGENT_SOCIALAPI_REDIRECT_URI", "")
    from . import socialapi_client as _sclient
    connect = {}
    for platform in ("instagram", "facebook"):
        try:
            body = _sclient.connect_account(
                platform, brand_id=brand_id, redirect_uri=redirect_uri,
                state=account_key, http=http)
            connect[platform] = body.get("auth_url", "")
        except Exception as e:
            from . import ops_alerts as _oa
            connect[platform] = ""
            print(f"[portal] social-connect {platform} failed for {account_key}: "
                  f"{type(e).__name__}: {_oa.scrub(str(e))}")

    return 200, {"account_key": account_key, "brand_id": brand_id, "connect": connect}


def handle_portal_social_status(account_key, http=None):
    """
    GET /portal/<token>/social-status  (token already resolved to account_key).

    Returns per-platform connection status (connected / disconnected / expired)
    for the gym's brand, for the portal to display. Gated by AGENT_PORTAL_APPROVALS.
    404 for a non-SocialAPI gym. Falls back to the cached status when the live
    read fails, so the portal never sees a hard error. Returns (status, dict).
    """
    if not config.portal_approvals_enabled():
        return 403, {"error": "portal access is disabled"}
    if not _is_socialapi_account(account_key):
        return 404, {"error": "not found"}

    from . import socialapi_store as _sstore
    brand_id = _sstore.get_brand_id(account_key)
    status_map = {"instagram": "disconnected", "facebook": "disconnected"}

    if brand_id and config.socialapi_key():
        try:
            from . import socialapi_client as _sclient
            accounts = _sclient.list_accounts(brand_id=brand_id, http=http)
            for acc in accounts:
                plat = str(acc.get("platform", "")).lower()
                if plat in status_map:
                    st = str(acc.get("status", "connected")).lower()
                    status_map[plat] = st or "connected"
                    # remember the connected account id for the publisher
                    acc_id = acc.get("id") or acc.get("account_id")
                    if acc_id and st in ("", "connected", "active"):
                        _sstore.set_account_id(account_key, plat, acc_id)
            _sstore.set_connection_status(account_key, status_map)
        except Exception as e:
            from . import ops_alerts as _oa
            print(f"[portal] social-status live read failed for {account_key}: "
                  f"{type(e).__name__}: {_oa.scrub(str(e))}")
            cached = _sstore.get_connection_status(account_key)
            if cached:
                status_map = cached

    return 200, {"account_key": account_key, "brand_id": brand_id,
                 "status": status_map}


class _R2:
    """Bytes-oriented R2/S3 wrapper for the upload path. Credentials from the same
    env names media hosting uses; read lazily, passed to boto3, never logged."""

    def __init__(self, s3, bucket):
        self._s3 = s3
        self._bucket = bucket

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data,
                            ContentType=content_type)

    def get_bytes(self, key):
        """Bytes at a key, or None ONLY when the key does not exist. Re-raises any
        real storage error so callers that must not clobber (the denylist writer)
        can tell 'empty' from 'unreadable'."""
        from botocore.exceptions import ClientError
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "NoSuchBucket", "404"):
                return None
            raise

    def total_bytes(self, prefix):
        """Measured bytes under a prefix (the quota gate's input), paginated."""
        total, token = 0, None
        while True:
            kw = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            total += sum(o.get("Size", 0) for o in resp.get("Contents", []))
            token = resp.get("NextContinuationToken")
            if not token:
                return total


def _looks_like_placeholder(value):
    """True when a value is an unfilled setup-runbook placeholder (angle brackets),
    e.g. '<from R2 dashboard>' or 'https://<your-account>.r2.cloudflarestorage.com'.
    Those are NOT real credentials; boto3 rejects them and the upload cannot land."""
    v = (value or "").strip()
    return "<" in v and ">" in v


def _default_r2():
    key_id = os.environ.get(config.S3_ACCESS_KEY_ID_ENV)
    secret = os.environ.get(config.S3_SECRET_ACCESS_KEY_ENV)
    if not key_id or not secret or not config.S3_BUCKET:
        print("[intake-web] R2 not configured: one of "
              f"{config.S3_ACCESS_KEY_ID_ENV}/{config.S3_SECRET_ACCESS_KEY_ENV}/"
              "AGENT_S3_BUCKET is unset. Uploads will 503 until real R2 "
              "credentials are set on this service.")
        return None
    # Placeholder values from the setup runbook (e.g. "<your-account>.r2...",
    # "<from R2 dashboard>") are invalid boto3 endpoints/creds and raise at client
    # construction time. Detect them explicitly and say so LOUDLY in the logs: a
    # silent None here is exactly what made the live 503 look like a code bug when
    # it was an unfilled env var. Uploads still 503 (storage genuinely unavailable),
    # but the reason is now diagnosable.
    for name, val in (("AGENT_S3_ENDPOINT", config.S3_ENDPOINT),
                      (config.S3_ACCESS_KEY_ID_ENV, key_id),
                      (config.S3_SECRET_ACCESS_KEY_ENV, secret),
                      ("AGENT_S3_BUCKET", config.S3_BUCKET)):
        if _looks_like_placeholder(val):
            print(f"[intake-web] R2 not configured: {name} is still the setup "
                  "placeholder (contains '<...>'). Set the real Cloudflare R2 "
                  "value on the echo-intake-web service. Uploads will 503 until then.")
            return None
    try:
        import boto3  # lazy
        s3 = boto3.client("s3", endpoint_url=config.S3_ENDPOINT or None,
                          region_name=config.S3_REGION or None,
                          aws_access_key_id=key_id, aws_secret_access_key=secret)
        return _R2(s3, config.S3_BUCKET)
    except Exception as e:
        from . import ops_alerts as _oa
        print(f"[intake-web] R2 client construction failed: "
              f"{type(e).__name__}: {_oa.scrub(str(e))}. Uploads will 503.")
        return None


# ---- the tiny mobile-first page + stdlib HTTP layer ----------------------------
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send content</title>
<style>
 :root{--navy:#121E3C;--red:#FF2A2A;--sky:#5EB9E6;--cream:#FAF6F0;--steel:#D8E3EE}
 *{box-sizing:border-box}
 body{font-family:-apple-system,'Inter',Helvetica,Arial,sans-serif;background:var(--navy);
      color:var(--cream);margin:0;padding:22px 16px 60px;display:flex;justify-content:center}
 .card{max-width:480px;width:100%}
 h1{font-size:23px;margin:0 0 6px}
 h1 .a{color:var(--red)}
 p.deck{color:var(--steel);font-size:14px;margin:0 0 18px;line-height:1.45}
 .pick{display:block;width:100%;background:var(--cream);color:var(--navy);border:none;
      border-radius:12px;padding:16px;font-size:16px;font-weight:700;text-align:center;cursor:pointer}
 .pick.more{background:transparent;color:var(--sky);border:2px dashed rgba(94,185,230,.5);
      margin-top:12px;padding:13px}
 #gallery{margin:14px 0 0;padding:0;list-style:none}
 .item{display:flex;gap:12px;background:rgba(255,255,255,.05);border-radius:12px;
      padding:10px;margin:0 0 12px;align-items:flex-start}
 .item.gone{display:none}
 .thumb{width:76px;height:76px;flex:0 0 76px;border-radius:9px;object-fit:cover;
      background:#0c1730;display:flex;align-items:center;justify-content:center;
      color:var(--sky);font-size:11px;font-weight:700;text-align:center;overflow:hidden}
 .thumb video,.thumb img{width:100%;height:100%;object-fit:cover}
 .meta{flex:1;min-width:0}
 .fname{font-size:12px;color:var(--steel);margin:0 0 6px;white-space:nowrap;
      overflow:hidden;text-overflow:ellipsis}
 .cap{width:100%;background:var(--cream);color:var(--navy);border:none;border-radius:8px;
      padding:9px 10px;font-size:14px}
 .row2{display:flex;justify-content:space-between;align-items:center;margin-top:7px}
 .state{font-size:12px;color:var(--steel)}
 .state.ok{color:var(--sky);font-weight:700}
 .state.err{color:#FFB4B4;font-weight:700}
 .rm{background:none;border:none;color:#FFB4B4;font-size:13px;font-weight:700;
      padding:2px 4px;cursor:pointer}
 .note{width:100%;background:var(--cream);color:var(--navy);border:none;border-radius:10px;
      padding:12px;font-size:15px;margin:16px 0 0;min-height:60px}
 .send{width:100%;background:var(--red);color:#fff;border:none;border-radius:12px;
      padding:16px;font-size:17px;font-weight:700;margin-top:18px}
 .send:disabled{opacity:.45}
 .empty{color:var(--steel);font-size:13px;text-align:center;padding:14px 0}
 #banner{border-radius:10px;padding:12px 14px;font-size:14px;font-weight:600;margin:16px 0 0;display:none}
 #banner.ok{display:block;background:rgba(94,185,230,.15);color:var(--sky)}
 #banner.err{display:block;background:rgba(255,120,120,.15);color:#FFB4B4}
</style></head><body><div class="card">
<h1>Send us your <span class="a">content</span></h1>
<p class="deck">Pick the photos and videos from your gym. Add one quick line next to
each so we know what is happening in it. All of it optional. We take it from there.</p>

<label class="pick" for="filepick">Choose photos or videos</label>
<input id="filepick" type="file" accept="image/*,video/mp4,video/quicktime" multiple hidden>
<ul id="gallery"><li class="empty" id="emptymsg">Nothing picked yet.</li></ul>
<label class="pick more" for="filepick" id="addmore" style="display:none">Add more</label>

<textarea class="note" id="note" maxlength="500"
  placeholder="Anything about the whole batch? (optional)"></textarea>
<div id="banner"></div>
<button class="send" id="send" disabled>Send it to LASSO</button>
</div>
<script>
(function(){
 var picked=[];            // {file, id, caption, sent}
 var seq=0;
 var input=document.getElementById('filepick');
 var gallery=document.getElementById('gallery');
 var emptymsg=document.getElementById('emptymsg');
 var addmore=document.getElementById('addmore');
 var sendBtn=document.getElementById('send');
 var banner=document.getElementById('banner');
 var note=document.getElementById('note');

 function isVideo(f){return (f.type||'').indexOf('video')===0
    || /\\.(mp4|mov|m4v|qt)$/i.test(f.name||'');}

 function render(){
   var live=picked.filter(function(p){return !p.removed;});
   emptymsg.style.display = live.length ? 'none':'block';
   addmore.style.display = live.length ? 'block':'none';
   sendBtn.disabled = live.length===0;
   sendBtn.textContent = live.length>1
     ? ('Send '+live.length+' items to LASSO') : 'Send it to LASSO';
 }

 function addFiles(files){
   for(var i=0;i<files.length;i++){(function(f){
     var id='it'+(seq++);
     var rec={file:f,id:id,caption:'',removed:false,sent:false};
     picked.push(rec);
     var li=document.createElement('li');
     li.className='item'; li.id=id;
     var thumb=document.createElement('div'); thumb.className='thumb';
     if(isVideo(f)){thumb.textContent='VIDEO';}
     else{var img=document.createElement('img');
          img.src=URL.createObjectURL(f);
          img.onload=function(){URL.revokeObjectURL(img.src);};
          thumb.textContent=''; thumb.appendChild(img);}
     var meta=document.createElement('div'); meta.className='meta';
     var fname=document.createElement('div'); fname.className='fname';
     fname.textContent=f.name||'file';
     var cap=document.createElement('input'); cap.className='cap'; cap.type='text';
     cap.maxLength=200; cap.placeholder='What is happening in this one? (optional)';
     cap.addEventListener('input',function(){rec.caption=cap.value;});
     var row2=document.createElement('div'); row2.className='row2';
     var state=document.createElement('span'); state.className='state'; state.textContent='ready';
     var rm=document.createElement('button'); rm.className='rm'; rm.type='button';
     rm.textContent='Remove';
     rm.addEventListener('click',function(){
       rec.removed=true; li.className='item gone'; render();});
     rec._state=state;
     row2.appendChild(state); row2.appendChild(rm);
     meta.appendChild(fname); meta.appendChild(cap); meta.appendChild(row2);
     li.appendChild(thumb); li.appendChild(meta);
     gallery.appendChild(li);
   })(files[i]);}
   input.value='';
   render();
 }

 input.addEventListener('change',function(e){if(e.target.files&&e.target.files.length)addFiles(e.target.files);});

 sendBtn.addEventListener('click',function(){
   var live=picked.filter(function(p){return !p.removed;});
   if(!live.length) return;
   sendBtn.disabled=true; banner.className=''; banner.textContent='';
   var fd=new FormData();
   live.forEach(function(p){
     fd.append('media',p.file,p.file.name||'upload');
     fd.append('caption',p.caption||'');
     p._state.textContent='sending'; p._state.className='state';
   });
   fd.append('note',note.value||'');
   fetch(window.location.pathname,{method:'POST',body:fd}).then(function(r){
     if(r.ok){
       live.forEach(function(p){p.sent=true;p._state.textContent='sent';p._state.className='state ok';});
       banner.className='ok';
       banner.textContent='Got it. '+live.length+(live.length>1?' items are':' item is')+
         ' in. We will take it from here.';
       sendBtn.textContent='Sent'; sendBtn.disabled=true;
     } else {
       live.forEach(function(p){p._state.textContent='not sent';p._state.className='state err';});
       banner.className='err';
       banner.textContent = r.status===413
         ? 'That batch was too large. Remove a few and try again.'
         : (r.status===400 ? 'One of those files was not a photo or video.'
         : (r.status===503 ? 'Our upload storage is briefly unavailable. Please try again in a few minutes.'
                           : 'Something went wrong. Please try again.'));
       sendBtn.disabled=false;
     }
   }).catch(function(){
     banner.className='err'; banner.textContent='Network hiccup. Please try again.';
     sendBtn.disabled=false;
     live.forEach(function(p){p._state.textContent='not sent';p._state.className='state err';});
   });
 });

 render();
})();
</script>
</body></html>"""

# Kept for the no-JS fallback POST and any legacy client: a full-page success view.
DONE = ("<!doctype html><html><body style='font-family:sans-serif;background:#121E3C;"
        "color:#FAF6F0;padding:40px;text-align:center'><h1>Got it.</h1>"
        "<p>Your content is in. We will take it from here.</p></body></html>")


# ---- the LASSO social intake form (V3 palette, mobile first) --------------------
# Client facing copy law: no dash characters, never the word vendor.
FORM_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LASSO Social Intake</title>
<style>
 :root{--navy:#121E3C;--red:#FF2A2A;--sky:#5EB9E6;--cream:#FAF6F0;--steel:#D8E3EE}
 body{font-family:-apple-system,'Inter',Helvetica,Arial,sans-serif;background:var(--navy);
      color:var(--cream);margin:0;padding:20px 16px 48px;display:flex;justify-content:center}
 .card{max-width:520px;width:100%}
 h1{font-size:24px;line-height:1.1;margin:0 0 6px}
 h1 .a{color:var(--red)}
 .deck{color:var(--steel);font-size:14px;margin:0 0 22px;line-height:1.45}
 h2{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--sky);
    margin:26px 0 10px}
 label{display:block;font-size:13px;font-weight:600;color:var(--steel);margin:12px 0 5px}
 input,textarea{width:100%;box-sizing:border-box;background:var(--cream);color:var(--navy);
    border:none;border-radius:10px;padding:12px;font-size:15px}
 textarea{min-height:76px;resize:vertical}
 .hint{font-size:12px;color:var(--steel);margin:5px 0 0;line-height:1.4}
 .rule{background:rgba(94,185,230,.12);border-left:4px solid var(--sky);border-radius:8px;
    padding:10px 12px;font-size:12.5px;color:var(--steel);margin:8px 0 0;line-height:1.45}
 button{width:100%;background:var(--red);color:#fff;border:none;border-radius:10px;
    padding:15px;font-size:16px;font-weight:700;margin-top:28px}
</style></head><body><div class="card">
<h1>Welcome to <span class="a">LASSO</span> Social</h1>
<p class="deck">Seven quick sections. Fill in what you have and hit send. Everything
you share here waits for your approval before a single post goes out.</p>
<form method="post">

<h2>1. Gym basics</h2>
<label>Gym name</label>
<input name="gym_name" maxlength="200" required>
<label>City</label>
<input name="city" maxlength="200">
<label>Website</label>
<input name="website" maxlength="200" inputmode="url" placeholder="https://">
<label>About the gym</label>
<textarea name="about" placeholder="Who you are in a sentence or two. Family owned since 2015, coach led small groups, that kind of thing."></textarea>

<h2>2. Brand voice</h2>
<label>How do you talk?</label>
<textarea name="voice" placeholder="Words you love, words you avoid, how a post should sound coming from you."></textarea>

<h2>3. Offers and services</h2>
<label>Current offers</label>
<textarea name="offers" placeholder="One per line. Example: 6 week kickstart for new members"></textarea>
<label>Services and programs</label>
<textarea name="services" placeholder="One per line. Example: small group personal training"></textarea>
<label>Pricing rule (exact wording)</label>
<textarea name="pricing_rule" placeholder="The exact words we may use for pricing, if any."></textarea>
<div class="rule">We never post a price, discount, or guarantee unless it is written
here exactly as you want it to appear. If this box is empty, no prices are ever posted.</div>

<h2>4. Audience</h2>
<label>Who are we talking to?</label>
<textarea name="audience" placeholder="Busy parents? Beginners? People getting back into it after a break?"></textarea>

<h2>5. Proof</h2>
<label>Member wins we may share</label>
<textarea name="proof" placeholder="One per line, with the member's permission. Example: Sarah lost 30 pounds in 3 months"></textarea>
<div class="rule">Only share wins the member has agreed to make public. We hold every
one for your approval before it can appear in a post.</div>

<h2>6. Media notes</h2>
<label>Anything we should know about your photos and videos?</label>
<textarea name="media_notes" placeholder="What to feature, what to avoid, members who prefer to stay off camera."></textarea>

<h2>7. Approver</h2>
<label>Who approves posts?</label>
<input name="approver_name" maxlength="200" placeholder="Name">
<label>Best way to reach them</label>
<input name="approver_contact" maxlength="200" placeholder="Phone, email, or Slack">

<button type="submit">Send it to LASSO</button>
</form></div></body></html>"""

FORM_DONE_TMPL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Received</title>
<style>
 body{font-family:-apple-system,'Inter',Helvetica,Arial,sans-serif;background:#121E3C;
      color:#FAF6F0;margin:0;padding:48px 20px;display:flex;justify-content:center;text-align:center}
 .card{max-width:440px;width:100%}
 h1{font-size:26px;margin:0 0 10px}
 p{color:#D8E3EE;font-size:15px;line-height:1.5;margin:0 0 26px}
 a.btn{display:block;background:#FF2A2A;color:#fff;text-decoration:none;border-radius:10px;
      padding:15px;font-size:16px;font-weight:700}
</style></head><body><div class="card">
<h1>Got it. Thank you.</h1>
<p>Your answers are in and nothing posts until you approve it.
One more step while you are here: send us your photos and videos.</p>
<a class="btn" href="__UPLOAD_PATH__">Upload your media now</a>
</div></body></html>"""


# ---- admin tracker: /admin/tracker/<token>[/handoff] ----------------------------
# Read-only: serves two static HTML files from the deployed repo (docs/).
# Gated by a single long random token in the URL path (AGENT_TRACKER_TOKEN, set by
# hand in Railway env). No flag — the route 404s whenever the env var is unset.
# Raw token is never logged (same discipline as upload tokens).
_TRACKER_PAGES = {
    "tracker": "echo_build_tracker.html",
    "handoff": "ECHO_HANDOFF.html",
}
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tracker_token():
    """The admin tracker token, or empty string when not set."""
    return (os.environ.get(_TRACKER_TOKEN_ENV) or "").strip()


def handle_tracker(token, which="tracker"):
    """
    Returns (status, html_bytes). 404 when the tracker token is unset, does not
    match, or the requested page is unknown; 200 with the file contents on match.
    The raw token is never logged; a file that does not yet exist is a 404.

    For the "handoff" page, first checks /data/handoff_live.html (written by the
    scheduler at 12pm + 4pm ET via gen-handoff). If that file exists and is < 25h
    old, it takes precedence over the static ECHO_HANDOFF.html in the repo.
    """
    expected = _tracker_token()
    if not expected or token != expected:
        return 404, b"not found"
    rel = _TRACKER_PAGES.get(which)
    if rel is None:
        return 404, b"not found"

    # For the handoff page, prefer a live-generated version if recent.
    if which == "handoff":
        import time as _time
        data_dir = os.environ.get("AGENT_DATA_DIR", "/data")
        live = os.path.join(data_dir, "handoff_live.html")
        try:
            age = _time.time() - os.path.getmtime(live)
            if age < 25 * 3600:
                with open(live, "rb") as fh:
                    return 200, fh.read()
        except (OSError, IOError):
            pass  # fall through to static file

    full = os.path.join(_REPO_ROOT, rel)
    try:
        with open(full, "rb") as fh:
            return 200, fh.read()
    except (OSError, IOError):
        return 404, b"not found"


def build_server(port=None):
    """Build the HTTP server (bound, not serving). serve() runs it; tests bind
    port 0 and drive real requests against it without blocking."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from email.parser import BytesParser
    from email.policy import default as email_default

    class Handler(BaseHTTPRequestHandler):
        def _token(self):
            # The signed-token upload page. See token_from_path for why the raw
            # path is URL-decoded and trimmed before matching (a texted link
            # commonly arrives with the '.' percent-encoded or a trailing slash).
            return token_from_path(self.path, "u")

        def _form_token(self):
            return token_from_path(self.path, "intake")

        def _send_html(self, body_str, status=200):
            body = body_str.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj, status=200, cors_origin=""):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)

        def _origin_ok(self):
            """(allowed, origin). Absent Origin (server to server) and
            same-origin always pass; cross-origin passes ONLY when it equals
            AGENT_INTAKE_PORTAL_ORIGIN. Never all origins."""
            origin = (self.headers.get("Origin") or "").strip()
            return origin_allowed(origin, self.headers.get("Host") or ""), origin

        def _deny(self, code=404, msg="not found"):
            body = msg.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _portal_gym_key(self):
            m = re.match(r"^/portal/gym/([A-Za-z0-9_-]+)$",
                         self.path.split("?")[0])
            return m.group(1) if m else None

        def _portal_token_route(self):
            """Returns (token, sub) for /portal/<token>/<sub> routes, else (None, None).
            sub is one of: calendar, library, approve, edit, deny, kill.

            The token class allows '.' because SIGNED (minted) tokens are
            base64url(account).signature — the dot is the separator. Legacy
            per-gym env tokens are dotless and still match. Without the dot
            every minted token 404s here before client_for_token is reached."""
            m = re.match(
                r"^/portal/([A-Za-z0-9_.-]{8,})/"
                r"(calendar|library|report|approve|edit|deny|kill"
                r"|social-connect|social-status|social-disconnect"
                r"|facebook-pages|facebook-page-select)$",
                self.path.split("?")[0],
            )
            if m:
                return m.group(1), m.group(2)
            return None, None

        def _portal_social_route(self):
            """Returns (token, sub) for /portal/<token>/social-{connect|status},
            else (None, None). sub is 'social-connect' or 'social-status'."""
            m = re.match(
                r"^/portal/([A-Za-z0-9_.-]{8,})/"
                r"(social-connect|social-status)$",
                self.path.split("?")[0],
            )
            if m:
                return m.group(1), m.group(2)
            return None, None

        def _portal_clientsocial_route(self):
            """Part B token-scoped client-social READ routes.
            Returns (token, sub) for /portal/<token>/social and /portal/<token>/metrics,
            else (None, None). sub is 'social' or 'metrics'. Gated by
            AGENT_PORTAL_SOCIAL_ENABLED at the handler; a disabled route 404s."""
            m = re.match(
                r"^/portal/([A-Za-z0-9_.-]{8,})/(social|metrics)$",
                self.path.split("?")[0],
            )
            if m:
                return m.group(1), m.group(2)
            return None, None

        def _portal_autonomy_route(self):
            """Part B per-account autonomy toggle: POST /portal/<token>/autonomy.
            Returns the token, else None. Gated by AGENT_PORTAL_SOCIAL_ENABLED at the
            handler; a disabled route 404s. Body is JSON {"autonomous": true|false}."""
            m = re.match(
                r"^/portal/([A-Za-z0-9_.-]{8,})/autonomy$",
                self.path.split("?")[0],
            )
            return m.group(1) if m else None

        def _portal_post_action_route(self):
            """Part B token-scoped client-social ACTION routes.
            Returns (token, post_id, action) for
            /portal/<token>/posts/<id>/{approve|edit|deny|kill}, else (None,None,None).
            Gated by AGENT_PORTAL_SOCIAL_ENABLED at the handler; a disabled route 404s."""
            m = re.match(
                r"^/portal/([A-Za-z0-9_.-]{8,})/posts/([A-Za-z0-9_-]+)/"
                r"(approve|edit|deny|kill)$",
                self.path.split("?")[0],
            )
            if m:
                return m.group(1), m.group(2), m.group(3)
            return None, None, None

        def _tracker_route(self):
            """Returns (token, page) for admin tracker URLs, else (None, None)."""
            m = re.match(r"^/admin/tracker/([A-Za-z0-9_-]{8,})(/handoff)?$",
                         self.path.split("?")[0])
            if m:
                return m.group(1), ("handoff" if m.group(2) else "tracker")
            return None, None

        def do_GET(self):
            # Portal gym status: GET /portal/gym/<account_key>
            # Gated by AGENT_PORTAL_APPROVALS. Returns JSON. No token in path.
            portal_key = self._portal_gym_key()
            if portal_key is not None:
                status, body = handle_portal_gym_status(portal_key)
                return self._send_json(body, status)

            # Portal token routes: /portal/<token>/calendar and /portal/<token>/library.
            # Token resolves to account_key; unknown/revoked token = 404 (indistinguishable).
            pt_token, pt_sub = self._portal_token_route()
            if pt_token is not None and pt_sub in ("calendar", "library", "report"):
                account_key = client_for_token(pt_token)
                if account_key is None or is_revoked(account_key):
                    return self._deny(404)
                if pt_sub == "calendar":
                    from urllib.parse import urlparse, parse_qs
                    month = (parse_qs(urlparse(self.path).query).get("month") or [""])[0]
                    status, body = _pr.handle_portal_calendar(account_key, month)
                elif pt_sub == "report":
                    from urllib.parse import urlparse, parse_qs
                    days = (parse_qs(urlparse(self.path).query).get("days") or ["30"])[0]
                    status, body = _pr.handle_portal_report(account_key, days)
                else:
                    status, body = _pr.handle_portal_library(account_key)
                return self._send_json(body, status)

            # Zernio social-connect read routes (Blake ruling 2026-07-29: Zernio is the vendor;
            # SocialAPI.ai is retired). Per-platform social-connect (?platform=), social-status,
            # facebook-pages. Token->account_key; gated by ZERNIO_API_KEY. A revoked gym is a 404
            # everywhere (its OAuth links + connection state go dark on kill), mirroring the retired
            # SocialAPI route's guardrail.
            if pt_token is not None and pt_sub in ("social-connect", "social-status", "facebook-pages"):
                account_key = client_for_token(pt_token)
                if account_key is None or is_revoked(account_key):
                    return self._deny(404)
                if pt_sub == "social-connect":
                    from urllib.parse import urlparse, parse_qs
                    platform = (parse_qs(urlparse(self.path).query).get("platform") or [""])[0]
                    status, body = _zr.handle_social_connect(account_key, platform)
                elif pt_sub == "social-status":
                    status, body = _zr.handle_social_status(account_key)
                else:
                    status, body = _zr.handle_facebook_pages(account_key)
                return self._send_json(body, status)

            # Part B client-social READ routes: /portal/<token>/social (month calendar)
            # and /portal/<token>/metrics (Part D report shape). Gated by
            # AGENT_PORTAL_SOCIAL_ENABLED (handler returns 404 when off). Token->account;
            # unknown/revoked token = 404 (indistinguishable). TOKEN ISOLATION is enforced
            # inside the handlers, which scope every read to account_key.
            cs_token, cs_sub = self._portal_clientsocial_route()
            if cs_token is not None:
                account_key = client_for_token(cs_token)
                if account_key is None or is_revoked(account_key):
                    return self._deny(404)
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                if cs_sub == "social":
                    month = (qs.get("month") or [""])[0]
                    status, body = _ps.handle_social(account_key, month)
                else:
                    days = (qs.get("days") or ["30"])[0]
                    status, body = _ps.handle_metrics(account_key, days)
                return self._send_json(body, status)

            # Health check: answers even while AGENT_INTAKE_ENABLED is OFF —
            # the SERVICE being up and the FEATURE being armed are different
            # facts, and Railway's health check must not kill a dark service.
            # Reveals liveness + flag state only, never tokens or clients.
            if self.path.split("?")[0] == "/healthz":
                body = json.dumps({"ok": True,
                                   "intake_enabled": config.intake_enabled()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Admin tracker: /admin/tracker/<token>[/handoff]
            # Read-only dashboard; 404 for wrong/absent token (indistinguishable).
            tracker_tok, tracker_page = self._tracker_route()
            if tracker_tok is not None:
                status, body = handle_tracker(tracker_tok, tracker_page)
                if status == 200:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._deny()
                return
            # WhatsApp hub challenge verification (GET /whatsapp).
            # 404 while the flag is off; 403 on a wrong token; 200 + challenge text on match.
            if self.path.split("?")[0] == "/whatsapp":
                if not config.whatsapp_intake_enabled():
                    return self._deny(404, "not found")
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                mode = (qs.get("hub.mode") or [""])[0]
                challenge = (qs.get("hub.challenge") or [""])[0]
                verify_token = (qs.get("hub.verify_token") or [""])[0]
                expected_token = os.environ.get("AGENT_WHATSAPP_VERIFY_TOKEN", "")
                if mode == "subscribe" and expected_token and verify_token == expected_token:
                    body_bytes = challenge.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
                else:
                    self._deny(403, "forbidden")
                return
            # The intake FORM: /intake/<token>, same gate as the upload page
            # (flag off or unknown token = the same 404, on purpose).
            form_token = self._form_token()
            if form_token is not None:
                if not config.intake_enabled():
                    return self._deny()
                client = client_for_token(form_token)
                if client is None or is_revoked(client):
                    return self._deny()
                return self._send_html(FORM_PAGE)
            token = self._token()
            if not config.intake_enabled() or not token:
                return self._deny()
            client = client_for_token(token)
            if client is None or is_revoked(client):
                return self._deny()
            return self._send_html(PAGE)

        def do_OPTIONS(self):
            # CORS preflight for the portal's JSON POST. Answered ONLY for the
            # intake route and ONLY for an allowed origin; everything else 404s.
            if self._form_token() is None:
                return self._deny()
            allowed, origin = self._origin_ok()
            if not allowed or not origin:
                return self._deny(403, "origin not allowed")
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            self.end_headers()

        def do_POST(self):
            # Self-serve onboard: POST /portal/onboard {account_key, display_name}.
            # Auth: X-Portal-Key must equal AGENT_PORTAL_ONBOARD_KEY (constant-time
            # compare). Missing/wrong key -> 401. Also gated by AGENT_PORTAL_APPROVALS
            # (403 when off), mirroring every other portal route. The body is always
            # read first so the socket stays clean on an early return. Idempotent:
            # onboarding an already-onboarded gym returns its CURRENT live token.
            if self.path.split("?")[0] == "/portal/onboard":
                # Consume the body up front (bounded), regardless of the outcome.
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 64 * 1024:
                    return self._deny(413, "too large")
                raw = self.rfile.read(length) if length else b""
                # Auth FIRST: a wrong/missing key never reveals the flag state.
                shared_key = config.portal_onboard_key()
                supplied = self.headers.get("X-Portal-Key", "") or ""
                if (not shared_key) or (not hmac.compare_digest(supplied, shared_key)):
                    return self._send_json({"error": "unauthorized"}, 401)
                if not config.portal_approvals_enabled():
                    return self._send_json({"error": "portal access is disabled"}, 403)
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    return self._send_json({"error": "invalid JSON"}, 400)
                status, resp = handle_portal_onboard(body)
                return self._send_json(resp, status)

            # Portal draft actions: /portal/<token>/{approve|edit|deny|kill}
            # Gated by AGENT_PORTAL_APPROVALS. Token resolves to account_key.
            # Unknown/revoked token = 404. Body is JSON: {draft_id, actor_id, note?}.
            pt_token, pt_action = self._portal_token_route()
            if pt_token is not None and pt_action in ("approve", "edit", "deny", "kill"):
                account_key = client_for_token(pt_token)
                if account_key is None or is_revoked(account_key):
                    return self._deny(404)
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 64 * 1024:
                    return self._deny(413, "too large")
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    return self._send_json({"error": "invalid JSON"}, 400)
                draft_id = body.get("draft_id", "")
                actor_id = body.get("actor_id", "")
                note = body.get("note", "")
                status, resp = _pr.handle_portal_action(
                    pt_action, account_key, draft_id, actor_id, note=note
                )
                return self._send_json(resp, status)

            # Zernio Facebook Page select: POST /portal/<token>/facebook-page-select {page_id}.
            if pt_token is not None and pt_action == "facebook-page-select":
                account_key = client_for_token(pt_token)
                if account_key is None or is_revoked(account_key):
                    return self._deny(404)
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 64 * 1024:
                    return self._deny(413, "too large")
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    return self._send_json({"error": "invalid JSON"}, 400)
                status, resp = _zr.handle_facebook_page_select(account_key, body.get("page_id", ""))
                return self._send_json(resp, status)

            # Zernio DISCONNECT: POST /portal/<token>/social-disconnect?platform=instagram|facebook.
            # Lets a gym owner remove a wrongly-connected account (e.g. a personal or a
            # spouse's IG) so they can reconnect the right one. Token->account; revoked = 404.
            if pt_token is not None and pt_action == "social-disconnect":
                account_key = client_for_token(pt_token)
                if account_key is None or is_revoked(account_key):
                    return self._deny(404)
                from urllib.parse import urlparse, parse_qs
                platform = (parse_qs(urlparse(self.path).query).get("platform") or [""])[0]
                status, resp = _zr.handle_social_disconnect(account_key, platform)
                return self._send_json(resp, status)

            # Part B per-account autonomy toggle: POST /portal/<token>/autonomy.
            # Gated by AGENT_PORTAL_SOCIAL_ENABLED (handler 404s when off). Token->account;
            # unknown/revoked token = 404. Body is JSON {"autonomous": true|false}. On true
            # the handler auto-approves every currently-pending post for THIS account through
            # the same gated approve path (isolation: only this account's drafts are touched).
            au_token = self._portal_autonomy_route()
            if au_token is not None:
                account_key = client_for_token(au_token)
                if account_key is None or is_revoked(account_key):
                    return self._deny(404)
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 64 * 1024:
                    return self._deny(413, "too large")
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                except Exception:
                    return self._send_json({"error": "invalid JSON"}, 400)
                autonomous = bool(body.get("autonomous", False))
                from .store import PendingStore
                status, resp = _ps.handle_autonomy(
                    account_key, autonomous,
                    actor_id=str(body.get("actor_id", "") or ""),
                    store=PendingStore())
                return self._send_json(resp, status)

            # Part B client-social ACTION routes: POST /portal/<token>/posts/<id>/{approve
            # |edit|deny|kill}. Gated by AGENT_PORTAL_SOCIAL_ENABLED (handler returns 404
            # when off). Token->account_key; unknown/revoked token = 404. Body is JSON:
            # {actor_id, note?, confirm?}. TOKEN ISOLATION: the handler proves the draft
            # belongs to account_key before acting (a cross-gym id is a 404, never acted on).
            ps_token, ps_post_id, ps_action = self._portal_post_action_route()
            if ps_token is not None:
                account_key = client_for_token(ps_token)
                if account_key is None or is_revoked(account_key):
                    return self._deny(404)
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 64 * 1024:
                    return self._deny(413, "too large")
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                except Exception:
                    return self._send_json({"error": "invalid JSON"}, 400)
                actor_id = body.get("actor_id", "")
                note = body.get("note", "")
                confirm = bool(body.get("confirm", False))
                from .store import PendingStore
                store = PendingStore()
                if ps_action == "approve":
                    status, resp = _ps.handle_approve(account_key, ps_post_id, actor_id,
                                                      store=store)
                elif ps_action == "edit":
                    status, resp = _ps.handle_edit(account_key, ps_post_id, actor_id,
                                                   note=note, store=store)
                elif ps_action == "deny":
                    status, resp = _ps.handle_deny(account_key, ps_post_id, actor_id,
                                                   note=note, store=store)
                else:  # kill
                    status, resp = _ps.handle_kill(account_key, ps_post_id, actor_id,
                                                   confirm=confirm, store=store)
                return self._send_json(resp, status)

            # GHL inbound webhook (POST /ghl/inbound).
            # 404 while the flag is off; 403 on signature failure; 200 on success.
            if self.path.split("?")[0] == "/ghl/inbound":
                if not config.ghl_intake_enabled():
                    return self._deny(404, "not found")
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw_body = self.rfile.read(length)
                result = ghl_intake.handle_webhook(dict(self.headers), raw_body)
                if result is None:
                    return self._deny(404, "not found")
                if not result.get("ok"):
                    return self._deny(403, "forbidden")
                body_bytes = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return
            # WhatsApp incoming webhook (POST /whatsapp).
            # 404 while the flag is off; 403 on signature failure; 200 on success.
            # Raw body is read first (signature covers the exact bytes).
            if self.path.split("?")[0] == "/whatsapp":
                if not config.whatsapp_intake_enabled():
                    return self._deny(404, "not found")
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw_body = self.rfile.read(length)
                result = whatsapp_intake.handle_webhook(
                    dict(self.headers), raw_body)
                if result is None:
                    return self._deny(404, "not found")
                if not result.get("ok"):
                    return self._deny(403, "forbidden")
                body_bytes = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return
            # The intake route: a JSON body is the ops portal's API call; a
            # urlencoded body is the gym-facing form. Both land in R2 for the
            # listener's ingest to route through submit_intake() as PENDING
            # sources; this process never touches /data.
            form_token = self._form_token()
            if form_token is not None:
                allowed, origin = self._origin_ok()
                if not allowed:
                    return self._deny(403, "origin not allowed")
                if not allow_request(self.client_address[0]):
                    return self._deny(429, "slow down")
                if not allow_token_request(_token_hash_prefix(form_token)):
                    return self._deny(429, "upload limit reached, try again soon")
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > _max_request_bytes():
                    return self._deny(413, "too large")
                raw = self.rfile.read(length)
                ctype = (self.headers.get("Content-Type") or "").lower()
                if ctype.startswith("application/json"):
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except Exception:
                        return self._send_json({"error": "invalid JSON"}, 400,
                                               cors_origin=origin)
                    status, resp = handle_portal_intake(form_token, body)
                    return self._send_json(resp, status, cors_origin=origin)
                from urllib.parse import parse_qs
                parsed = parse_qs(raw.decode("utf-8", "replace"))
                fields = {k: v[0] for k, v in parsed.items() if v}
                status, _body = handle_intake_form(form_token, fields)
                if status == 200:
                    return self._send_html(FORM_DONE_TMPL.replace(
                        "__UPLOAD_PATH__", f"/u/{form_token}"))
                return self._deny(status,
                                  "form rejected" if status == 400 else "not found")
            token = self._token()
            if not token:
                return self._deny()
            ip = self.client_address[0]
            if not allow_request(ip):
                return self._deny(429, "slow down")
            if not allow_token_request(_token_hash_prefix(token)):
                return self._deny(429, "upload limit reached, try again soon")
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > _max_request_bytes():
                return self._deny(413, "too large")
            raw = self.rfile.read(length)
            msg = BytesParser(policy=email_default).parsebytes(
                b"Content-Type: " + (self.headers.get("Content-Type") or "").encode()
                + b"\r\n\r\n" + raw)
            # Parse the gallery submission. The page appends one 'media' file part
            # immediately followed by its 'caption' part, per item, in order. We
            # pair a caption to the media that PRECEDES it, so captions[i] is the
            # caption for files[i] regardless of how many items were sent. A plain
            # single 'note' (batch-wide) still works with no captions at all.
            files, note, captions = [], "", []
            for part in msg.iter_parts() if msg.is_multipart() else []:
                name = part.get_param("name", header="content-disposition")
                if name == "note":
                    content = part.get_content()
                    note = content.strip() if isinstance(content, str) else ""
                elif name == "media":
                    payload = part.get_payload(decode=True) or b""
                    files.append((part.get_filename() or "upload",
                                  part.get_content_type(), payload))
                    # keep captions index-aligned to files: pad this slot now,
                    # a following 'caption' part fills it (else it stays blank).
                    captions.append("")
                elif name == "caption":
                    content = part.get_content()
                    cap = content.strip() if isinstance(content, str) else ""
                    if files:                 # attach to the most recent media
                        captions[len(files) - 1] = cap
            status, _body = handle_upload(token, files, note=note, captions=captions)
            if status == 200:
                body = DONE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif status == 400:
                self._deny(400, "upload rejected")
            elif status == 413:
                self._deny(413, "upload too large")
            elif status == 503:
                # HONEST error: storage is unavailable (R2 not configured or a write
                # failed). Never "not found" — that misled the whole first diagnosis
                # and gives the UI nothing true to show.
                self._deny(503, "storage unavailable")
            else:
                self._deny(status, "not found")

        def log_message(self, fmt, *args):
            # Never log the path: it carries the token. Method + status only.
            print(f"[intake-web] {self.command} -> done")

    port = int(port if port is not None else os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Intake web online on :{server.server_address[1]} "
          f"(enabled: {config.intake_enabled()})")
    return server


def serve(port=None):  # pragma: no cover - blocking loop over build_server
    """Run the intake web service (its OWN process/service; R2 only, no /data)."""
    build_server(port).serve_forever()
