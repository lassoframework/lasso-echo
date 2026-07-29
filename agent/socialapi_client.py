"""
Thin REST client for SocialAPI.ai (https://docs.social-api.ai).

Server-side use only: Echo is a server, so this speaks the REST API with a
Bearer key, NOT the MCP client surface. The key is read by name from env at
call time (config.socialapi_key()) and is NEVER logged, printed, or returned.
Every error string is scrubbed before it can surface.

Endpoints used (verified against docs.social-api.ai, 2026-07):
  POST /brands                      create a gym's brand (IG + FB grouped under it)
  POST /accounts/connect            returns an OAuth auth_url the gym clicks
  GET  /accounts                    list connected accounts (+ per-platform status)
  POST /media/upload                multipart byte upload -> {media_id}
  POST /posts                       publish a feed post or story
  GET  /posts/{id}                  read post status/targets
  GET  /posts/{id}/metrics          per-post engagement (likes/comments/saves/shares)

Media note: a raw public URL in media_ids is silently ignored by the vendor, so
the publisher uploads bytes here and passes the returned media_id. R2 stays the
source of truth; this only hands the bytes to SocialAPI's media store.
"""

from . import config, ops_alerts


class SocialApiError(Exception):
    """A SocialAPI REST call failed. Message is always scrubbed of secrets."""


class MissingKey(SocialApiError):
    """AGENT_SOCIALAPI_KEY is not set; no call can be made."""


def _requests():
    import requests
    return requests


def _key():
    key = config.socialapi_key()
    if not key:
        raise MissingKey(
            f"{config.SOCIALAPI_KEY_ENV} is not set. Set it by hand in Railway env."
        )
    return key


def _auth_headers():
    """Authorization only. Never logged. Built fresh per call so a rotated key
    takes effect immediately."""
    return {"Authorization": f"Bearer {_key()}"}


def _base():
    return config.socialapi_base_url().rstrip("/")


def _check(resp, what):
    """Raise SocialApiError (scrubbed) on a non-2xx response; else return the
    parsed JSON body (or {} when there is none)."""
    code = getattr(resp, "status_code", 0)
    if 200 <= code < 300:
        try:
            return resp.json()
        except Exception:
            return {}
    # Pull a short body for the message, then scrub it (defends against a key or
    # token echoed back in an error payload).
    try:
        body = resp.text[:300]
    except Exception:
        body = ""
    raise SocialApiError(ops_alerts.scrub(f"{what} failed: HTTP {code} {body}"))


# ---- brands ----------------------------------------------------------------

def create_brand(name, http=None):
    """Create a brand and return its id. One brand per gym."""
    client = http or _requests()
    resp = client.post(f"{_base()}/brands",
                       headers={**_auth_headers(), "Content-Type": "application/json"},
                       json={"name": name}, timeout=30)
    body = _check(resp, "create_brand")
    return body.get("id") or body.get("brand_id") or ""


# ---- accounts (connect + status) -------------------------------------------

def connect_account(platform, brand_id="", redirect_uri="", state="", http=None):
    """Start the OAuth connect flow for one platform under a brand. Returns the
    body containing auth_url (the link the gym clicks to authorize their IG/FB)."""
    client = http or _requests()
    payload = {"platform": platform}
    if brand_id:
        payload["brand_id"] = brand_id
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri
    if state:
        payload["state"] = state
    resp = client.post(f"{_base()}/accounts/connect",
                       headers={**_auth_headers(), "Content-Type": "application/json"},
                       json=payload, timeout=30)
    return _check(resp, "connect_account")


def list_accounts(brand_id="", http=None):
    """List connected accounts, optionally scoped to a brand. Returns a list of
    account dicts (id, platform, username, status)."""
    client = http or _requests()
    url = f"{_base()}/accounts"
    params = {"brand_id": brand_id} if brand_id else None
    resp = client.get(url, headers=_auth_headers(), params=params, timeout=30)
    body = _check(resp, "list_accounts")
    if isinstance(body, list):
        return body
    return body.get("accounts") or body.get("data") or []


# ---- media -----------------------------------------------------------------

def upload_media(data_bytes, filename, content_type, http=None):
    """Upload raw bytes and return the media_id. Multipart; the vendor ignores
    raw public URLs, so bytes are the only path. Files up to 50MB."""
    client = http or _requests()
    files = {"file": (filename, data_bytes, content_type)}
    # NOTE: do not set Content-Type here; requests sets the multipart boundary.
    resp = client.post(f"{_base()}/media/upload",
                       headers=_auth_headers(), files=files, timeout=120)
    body = _check(resp, "upload_media")
    return body.get("media_id") or body.get("id") or ""


# ---- posts -----------------------------------------------------------------

def create_post(account_id, text, media_ids, content_type="feed", http=None,
                idempotency_key=""):
    """Publish immediately to one connected account. content_type is 'feed' or
    'stories'. Text passes through verbatim (newlines preserved by JSON encoding).
    Returns the full post response (id, status, targets[])."""
    client = http or _requests()
    target = {"account_id": account_id}
    if content_type:
        target["platform_data"] = {"content_type": content_type}
    payload = {
        "text": text,
        "media_ids": list(media_ids or []),
        "publish_now": True,
        "targets": [target],
    }
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    if idempotency_key:
        # If the vendor honors it, great; if not, our own DB idempotency guard
        # in the publisher is the real backstop. Harmless either way.
        headers["Idempotency-Key"] = idempotency_key
    resp = client.post(f"{_base()}/posts", headers=headers, json=payload, timeout=60)
    return _check(resp, "create_post")


def get_post(post_id, http=None):
    client = http or _requests()
    resp = client.get(f"{_base()}/posts/{post_id}", headers=_auth_headers(), timeout=30)
    return _check(resp, "get_post")


def get_post_metrics(post_id, http=None):
    """Per-post engagement. The vendor exposes likes/comments/saves/shares only;
    impressions, reach, and follower count are NOT available from this API."""
    client = http or _requests()
    resp = client.get(f"{_base()}/posts/{post_id}/metrics",
                      headers=_auth_headers(), timeout=30)
    return _check(resp, "get_post_metrics")
