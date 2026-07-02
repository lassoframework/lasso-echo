"""
Texted-link intake: the client-facing upload page.

A SEPARATE web process (own start command: `python -m agent intake-web`), deployable
as its own Railway service. HARD CONSTRAINT honored: this process touches R2 ONLY,
never /data (the volume belongs to the listener service; the listener's ingest step
picks uploads up from R2).

Flow: the client taps their private tokenized link (/u/<token>), picks photos or
videos, types one optional sentence, hits send. Files land in R2 under
intake/<client>/incoming/ with a sidecar JSON (note, client + token fingerprint,
timestamp, filenames). The raw token is never logged AND never persisted; the
sidecar carries a sha256 fingerprint instead.

Gates: everything is 404 unless AGENT_INTAKE_ENABLED=true (default OFF) and the
token matches a per-client env value (AGENT_INTAKE_TOKEN_<CLIENTKEY>, set by hand).
Guardrails: content-type allowlist (images + common video), per-file and per-request
size caps, a basic per-IP rate limit, and no directory listing (only /u/<token>
exists; every other path 404s).
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


class _R2:
    """Bytes-oriented R2/S3 wrapper for the upload path. Credentials from the same
    env names media hosting uses; read lazily, passed to boto3, never logged."""

    def __init__(self, s3, bucket):
        self._s3 = s3
        self._bucket = bucket

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data,
                            ContentType=content_type)


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


def serve(port=None):  # pragma: no cover - thin stdlib wiring over the pure core
    """Run the intake web service (its OWN process/service; R2 only, no /data)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from email.parser import BytesParser
    from email.policy import default as email_default

    class Handler(BaseHTTPRequestHandler):
        def _token(self):
            m = re.match(r"^/u/([A-Za-z0-9_-]{8,})$", self.path.split("?")[0])
            return m.group(1) if m else None

        def _deny(self, code=404, msg="not found"):
            body = msg.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            token = self._token()
            if not config.intake_enabled() or not token or client_for_token(token) is None:
                return self._deny()
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
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

    port = int(port or os.environ.get("PORT", "8080"))
    print(f"Intake web online on :{port} (enabled: {config.intake_enabled()})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
