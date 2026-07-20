"""
Facebook connect page (client onboarding surface).
Flag: AGENT_CONNECT_ENABLED (default OFF; the whole surface 404s while off).

Runs INSIDE the listener process (the one with /data, so the page token can
land in the store) as a small stdlib HTTP server on AGENT_CONNECT_PORT, the
same plumbing pattern as the intake web page. Routes:

  GET  /connect            the cream V3 landing page, one Login with Facebook button
  GET  /connect/callback   OAuth code exchange -> pick a Page (radio list)
  POST /connect/select     resolve the linked IG professional account, store the
                           page token, render the Connected state

Facebook Login for Business, requesting EXACTLY these scopes:
  pages_show_list, pages_read_engagement, pages_manage_posts,
  instagram_basic, instagram_content_publish

TOKEN DISCIPLINE, unchanged: META_APP_ID / META_APP_SECRET from env, set by
hand, never logged, never echoed; the exchanged token is held in memory only
during the flow, stored in the /data kv store keyed to the page, never rendered
in HTML, never in Slack, redacted in audit entries (the audit writer scrubs).
Publish gates untouched: connecting an account changes NOTHING about posting;
every post still cards for approval before anything publishes.
"""

import json
import os
import secrets
import urllib.parse

from . import config

SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts",
          "instagram_basic", "instagram_content_publish")
FB_DIALOG = f"https://www.facebook.com/{config.GRAPH_API_VERSION}/dialog/oauth"

# in-flight OAuth sessions (single listener process): state nonce -> user token
_sessions = {}

_PAGE_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect to LASSO</title></head>
<body style="font-family:Helvetica,Arial,sans-serif;background:#FAF6F0;color:#121E3C;
margin:0;padding:48px 24px;text-align:center">
<div style="max-width:460px;margin:0 auto">
<div style="font-size:28px;font-weight:bold;color:#121E3C">LASSO</div>
{body}
</div></body></html>"""


def _requests():
    import requests  # lazy
    return requests


def _base_url():
    return os.environ.get("AGENT_CONNECT_BASE_URL", "").rstrip("/")


def login_url(state):
    """The Facebook Login for Business dialog URL with EXACTLY the five scopes."""
    params = {
        "client_id": os.environ.get("META_APP_ID", ""),
        "redirect_uri": f"{_base_url()}/connect/callback",
        "state": state,
        "scope": ",".join(SCOPES),
        "response_type": "code",
    }
    return f"{FB_DIALOG}?{urllib.parse.urlencode(params)}"


# ---- pure handlers: (status, html) ------------------------------------------------
def handle_connect():
    if not config.connect_enabled():
        return 404, "not found"
    state = secrets.token_urlsafe(24)
    _sessions[state] = None  # reserved; filled by the callback
    body = f"""
<p style="font-size:17px;margin:28px 0">Connect your gym's Facebook and Instagram
to LASSO Social Poster.</p>
<a href="{login_url(state)}" style="display:inline-block;background:#121E3C;
color:#FAF6F0;padding:14px 28px;border-radius:8px;text-decoration:none;
font-weight:bold">Login with Facebook</a>
<p style="color:#5EB9E6;margin-top:24px;font-size:13px">Posts will appear for
approval before anything publishes.</p>"""
    return 200, _PAGE_SHELL.format(body=body)


def handle_callback(params, http=None):
    """Exchange the code, list the user's Pages (radio select)."""
    if not config.connect_enabled():
        return 404, "not found"
    code = (params.get("code") or [""])[0] if isinstance(params.get("code"), list) \
        else params.get("code", "")
    state = (params.get("state") or [""])[0] if isinstance(params.get("state"), list) \
        else params.get("state", "")
    if not code or state not in _sessions:
        return 400, _PAGE_SHELL.format(
            body="<p>Connection did not complete. Close this tab and use the "
                 "link again.</p>")
    client = http or _requests()
    r = client.get(f"{config.GRAPH_API_BASE}/oauth/access_token",
                   params={"client_id": os.environ.get("META_APP_ID", ""),
                           "client_secret": os.environ.get("META_APP_SECRET", ""),
                           "redirect_uri": f"{_base_url()}/connect/callback",
                           "code": code},
                   timeout=30)
    user_token = (r.json() or {}).get("access_token", "")
    if not user_token:
        return 400, _PAGE_SHELL.format(
            body="<p>Facebook did not return access. Try the link again.</p>")
    _sessions[state] = user_token

    r2 = client.get(f"{config.GRAPH_API_BASE}/me/accounts",
                    params={"access_token": user_token, "fields": "name,id"},
                    timeout=30)
    pages = (r2.json() or {}).get("data", []) or []
    if not pages:
        return 200, _PAGE_SHELL.format(
            body="<p>No Facebook Pages found on this login. You need admin "
                 "access to your gym's Page.</p>")
    radios = "".join(
        f'<label style="display:block;text-align:left;background:#FFFFFF;'
        f'border-radius:8px;padding:12px;margin:8px 0">'
        f'<input type="radio" name="page_id" value="{p.get("id", "")}" required> '
        f'{p.get("name", "")} <span style="color:#5EB9E6">({p.get("id", "")})</span>'
        f"</label>" for p in pages)
    body = f"""
<p style="font-size:17px;margin:24px 0">Choose your gym's Page:</p>
<form method="post" action="/connect/select">
<input type="hidden" name="state" value="{state}">
{radios}
<button type="submit" style="background:#FF0000;color:#FFFFFF;border:none;
padding:14px 28px;border-radius:8px;font-weight:bold;margin-top:16px">
Connect this Page</button></form>"""
    return 200, _PAGE_SHELL.format(body=body)


def handle_select(params, http=None, poster=None):
    """Resolve the linked IG account, store the page token, confirm."""
    if not config.connect_enabled():
        return 404, "not found"

    def _one(key):
        v = params.get(key, "")
        return v[0] if isinstance(v, list) else v

    state, page_id = _one("state"), _one("page_id")
    user_token = _sessions.get(state)
    if not user_token or not page_id:
        return 400, _PAGE_SHELL.format(
            body="<p>Session expired. Start again from the connect link.</p>")
    client = http or _requests()
    r = client.get(f"{config.GRAPH_API_BASE}/{page_id}",
                   params={"access_token": user_token,
                           "fields": "name,access_token,"
                                     "instagram_business_account{username,id}"},
                   timeout=30)
    body = r.json() or {}
    page_name = body.get("name", "")
    page_token = body.get("access_token", "")
    ig = body.get("instagram_business_account") or {}
    ig_username = ig.get("username", "")

    if page_token:
        from . import db
        db.kv_set(f"connect_page_token_{page_id}", page_token)
        db.kv_set(f"connect_page_meta_{page_id}", json.dumps(
            {"page_name": page_name, "ig_username": ig_username,
             "ig_id": ig.get("id", "")}))
        db.audit("connect", page_name or page_id,
                 f"page connected; ig={ig_username or 'none linked'}")
        # Social Grade baseline (AGENT_CONNECT_GRADE_ENABLED, OFF: byte
        # identical connect). ON: queue exactly one baseline read per connect
        # and post one informational BASELINE line. No publish path.
        if config.connect_grade_enabled():
            _queue_grade_baseline(page_id, page_name, ig_username, poster=poster)
    _sessions.pop(state, None)  # single use

    ig_line = (f"Instagram: <b>@{ig_username}</b>" if ig_username
               else "No Instagram professional account is linked to this Page yet.")
    body_html = f"""
<p style="font-size:22px;font-weight:bold;color:#121E3C;margin:28px 0 8px">
Connected</p>
<p style="font-size:17px">Page: <b>{page_name}</b><br>{ig_line}</p>
<p style="color:#5EB9E6;margin-top:24px">Posts will appear for approval before anything publishes.</p>"""
    return 200, _PAGE_SHELL.format(body=body_html)


# ---- the thin stdlib server (started by the listener when armed) -------------------
def serve(port=None):  # pragma: no cover - thin stdlib wiring over the pure core
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, html):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            # Admin tracker: /admin/tracker/<token>[/handoff] (read-only, token-gated)
            import re as _re
            m = _re.match(r"^/admin/tracker/([A-Za-z0-9_-]{8,})(/handoff)?$",
                          parsed.path)
            if m:
                from .intake_web import handle_tracker
                status, body = handle_tracker(
                    m.group(1), "handoff" if m.group(2) else "tracker")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/connect":
                self._send(*handle_connect())
            elif parsed.path == "/connect/callback":
                self._send(*handle_callback(urllib.parse.parse_qs(parsed.query)))
            else:
                self._send(404, "not found")

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/connect/select":
                length = int(self.headers.get("Content-Length", 0) or 0)
                params = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
                self._send(*handle_select(params))
            else:
                self._send(404, "not found")

        def log_message(self, fmt, *args):  # never log query strings (codes/state)
            print(f"[connect] {self.command} {urllib.parse.urlparse(self.path).path}")

    port = int(port or os.environ.get("AGENT_CONNECT_PORT", "8090"))
    print(f"[connect] serving on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def _queue_grade_baseline(page_id, page_name, ig_username, poster=None):
    """Queue ONE Social Grade baseline read for a freshly connected page and
    post the informational BASELINE line. Dash free, informational only."""
    from . import db
    import json as _json
    try:
        queue = _json.loads(db.kv_get("grade_baseline_queue", "") or "[]")
    except Exception:
        queue = []
    queue.append({"page_id": page_id, "page_name": page_name,
                  "ig_username": ig_username})
    db.kv_set("grade_baseline_queue", _json.dumps(queue))
    ig_bit = f" and @{ig_username}" if ig_username else ""
    note = (f"BASELINE queued: Social Grade baseline read for {page_name}"
            f"{ig_bit}. The first grade card lands after the first snapshot "
            "cycle. Informational only; nothing publishes from this.")
    if poster is None:
        try:
            from .ops_alerts import _default_poster
            poster = _default_poster()
        except Exception:
            poster = None
    if poster is not None:
        try:
            poster.post_notice(note)
        except Exception:
            pass
    db.audit("connect_grade", page_name or page_id, "baseline queued")
