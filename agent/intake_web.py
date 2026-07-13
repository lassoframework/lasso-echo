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

from . import config

_TOKEN_ENV_PREFIX = "AGENT_INTAKE_TOKEN_"

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

        def _deny(self, code=404, msg="not found"):
            body = msg.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
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

        def do_POST(self):
            # The intake FORM submission: urlencoded, lands in R2 for the
            # listener's ingest to route through submit_intake() as PENDING
            # sources. The confirmation offers the upload page for the same
            # token so photos come in the same sitting.
            form_token = self._form_token()
            if form_token is not None:
                if not allow_request(self.client_address[0]):
                    return self._deny(429, "slow down")
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > _max_request_bytes():
                    return self._deny(413, "too large")
                from urllib.parse import parse_qs
                parsed = parse_qs(self.rfile.read(length).decode("utf-8",
                                                                 "replace"))
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
