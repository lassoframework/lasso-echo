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
token matches a per-client env value (AGENT_INTAKE_TOKEN_<CLIENTKEY>, set by hand).
Guardrails: content-type allowlist (images + common video), per-file and per-request
size caps, a basic per-IP rate limit, and no directory listing (only /u/<token>
and /intake/<token> exist; every other path 404s).
"""

import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timezone

from . import config, whatsapp_intake

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
    """The client key a token belongs to, or None. The token value is never logged."""
    if not token:
        return None
    if config.onboard_automint_enabled():
        # Data store first: revoked or unknown tokens return None immediately.
        from . import intake_tokens as _it
        client = _it.client_for_token_data(token)
        if client is not None:
            return client
        # A token missing from the store may still be an env token; fall through.
    # Fallback: env vars (original behavior, unchanged).
    for name, value in os.environ.items():
        if name.startswith(_TOKEN_ENV_PREFIX) and value and value == token:
            return name[len(_TOKEN_ENV_PREFIX):].lower()
    return None


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


def handle_upload(token, files, note="", r2=None, now=None):
    """
    The whole upload decision, pure and offline-testable. Returns (status, body).
    404 whenever the feature is off or the token is unknown (indistinguishable on
    purpose); 429 rate-limited (handled by the HTTP layer); 400 bad files; 200 ok.
    """
    if not config.intake_enabled():
        return 404, {"error": "not found"}
    client = client_for_token(token)
    if client is None:
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

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    stored = []
    for filename, ctype, data in files:
        key = f"intake/{client}/incoming/{stamp}_{_safe_name(filename)}"
        r2.put_bytes(key, data, content_type=ctype)
        stored.append(os.path.basename(key))
    sidecar = {
        "note": (note or "").strip()[:500],
        "client": client,
        # never the raw token: a fingerprint is enough to trace which link was used
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "timestamp": stamp,
        "filenames": stored,
    }
    r2.put_bytes(f"intake/{client}/incoming/{stamp}_upload.json",
                 json.dumps(sidecar).encode("utf-8"), content_type="application/json")
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
    if client is None:
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
    if client is None:
        return 404, {"error": "not found"}
    if not isinstance(body, dict):
        return 400, {"error": "a JSON object is required"}

    answers = {k: v[:_FIELD_MAX] for k, v in normalize_portal_intake(body).items()}
    if not answers["gym_name"]:
        return 400, {"error": "gym.name is required"}
    if not any(v for k, v in answers.items() if k != "gym_name"):
        return 400, {"error": "the intake is empty"}

    r2 = r2 or _default_r2()
    if r2 is None:
        return 503, {"error": "storage unavailable"}

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
    r2.put_bytes(f"intake/{client}/incoming/{stamp}_intake.json",
                 json.dumps(payload).encode("utf-8"),
                 content_type="application/json")
    base = os.environ.get("AGENT_UPLOAD_BASE_URL", "").strip().rstrip("/")
    return 200, {
        "status": "received",
        "account_key": client,
        "pending_source_count": _count_source_facts(answers),
        "upload_url": f"{base}/u/{token}" if base else f"/u/{token}",
    }


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
            base = os.environ.get("AGENT_UPLOAD_BASE_URL", "").rstrip("/")
            if base:
                upload_link = base + "/u/" + raw
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


class _R2:
    """Bytes-oriented R2/S3 wrapper for the upload path. Credentials from the same
    env names media hosting uses; read lazily, passed to boto3, never logged."""

    def __init__(self, s3, bucket):
        self._s3 = s3
        self._bucket = bucket

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data,
                            ContentType=content_type)

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


# ---- the tiny mobile-first page + stdlib HTTP layer ----------------------------
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send content</title>
<style>
 body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#121E3C;color:#FAF6F0;
      margin:0;padding:24px;display:flex;justify-content:center}
 .card{max-width:440px;width:100%%}
 h1{font-size:22px;margin:0 0 6px} p{color:#D8E3EE;font-size:14px;margin:0 0 18px}
 input[type=file],textarea{width:100%%;box-sizing:border-box;background:#FAF6F0;color:#121E3C;
      border:none;border-radius:10px;padding:12px;font-size:15px;margin:0 0 12px}
 textarea{min-height:70px}
 button{width:100%%;background:#FF0000;color:#fff;border:none;border-radius:10px;
      padding:14px;font-size:16px;font-weight:700}
 .ok{color:#5EB9E6;font-weight:700}
</style></head><body><div class="card">
<h1>Send us your content</h1>
<p>Pick photos or videos from your gym and add one line about what is happening. We take it from there.</p>
<form method="post" enctype="multipart/form-data">
 <input type="file" name="media" accept="image/*,video/mp4,video/quicktime" multiple required>
 <textarea name="note" maxlength="500" placeholder="One sentence about these (optional)"></textarea>
 <button type="submit">Send</button>
</form></div></body></html>"""

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
            m = re.match(r"^/u/([A-Za-z0-9_-]{8,})$", self.path.split("?")[0])
            return m.group(1) if m else None

        def _form_token(self):
            m = re.match(r"^/intake/([A-Za-z0-9_-]{8,})$", self.path.split("?")[0])
            return m.group(1) if m else None

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
                if not config.intake_enabled() or client_for_token(form_token) is None:
                    return self._deny()
                return self._send_html(FORM_PAGE)
            token = self._token()
            if not config.intake_enabled() or not token or client_for_token(token) is None:
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
            files, note = [], ""
            for part in msg.iter_parts() if msg.is_multipart() else []:
                name = part.get_param("name", header="content-disposition")
                if name == "note":
                    note = part.get_content().strip() if isinstance(part.get_content(), str) else ""
                elif name == "media":
                    payload = part.get_payload(decode=True) or b""
                    files.append((part.get_filename() or "upload",
                                  part.get_content_type(), payload))
            status, _body = handle_upload(token, files, note=note)
            if status == 200:
                body = DONE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._deny(status, "upload rejected" if status == 400 else "not found")

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
