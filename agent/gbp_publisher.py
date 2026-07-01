"""
Google Business Profile publisher (Local Posts). A first-class publishing channel
alongside Meta, with the SAME gates:

  - DRAFT-ONLY GUARD: publish() makes NO network call and returns a WouldPublish
    result when publish_enabled() is False OR gbp_enabled() is False. A real write
    needs BOTH flags on (the publish flag governs every real write).
  - TOKEN BY HAND: the Bearer token is read lazily from env, used only in the
    Authorization header, and never logged, returned, or stored on an object.

GBP post shape is its OWN variant, never a copy of the IG caption: one PHOTO image,
up to GBP_SUMMARY_LIMIT chars, a structured CTA button, no hashtags. See
BUILD_SPEC.md Addendum A.
"""

import os
from dataclasses import dataclass

from . import config


class GbpError(Exception):
    pass


class MissingToken(GbpError):
    pass


@dataclass
class PublishResult:
    ok: bool
    mode: str          # "published" or "would_publish"
    post_id: str = ""
    detail: str = ""


# requests is imported lazily so draft-only / flag-off mode has zero network dependency.
def _requests():
    import requests
    return requests


def _token():
    """Read the GBP access token from env at call time. Never logged or returned."""
    return os.environ.get(config.GBP_TOKEN_ENV)


def build_local_post(summary, image_url="", cta_type=None, cta_url="", topic_type="STANDARD"):
    """
    Assemble a GBP localPost body from approved input.

      - summary is trimmed to GBP_SUMMARY_LIMIT (1500).
      - the CTA type is validated against GBP_CTA_TYPES; an invalid type is DROPPED
        (no button) rather than raising — a bad button never blocks a post. A
        callToAction is attached only with a valid type AND a url (CALL needs no url).
      - one PHOTO media item is attached when an image_url is present (no video/carousel).
    """
    body = {
        "languageCode": "en-US",
        "summary": (summary or "")[: config.GBP_SUMMARY_LIMIT],
        "topicType": topic_type or "STANDARD",
    }
    ctype = cta_type or config.GBP_DEFAULT_CTA
    if ctype in config.GBP_CTA_TYPES and (cta_url or ctype == "CALL"):
        action = {"actionType": ctype}
        if cta_url:
            action["url"] = cta_url
        body["callToAction"] = action
    if image_url:
        body["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": image_url}]
    return body


def publish(draft, account, http=None):
    """
    Publish a draft as a GBP local post. Returns a PublishResult.

    Draft-only short-circuit: if publishing is not armed OR the GBP branch is off, we
    do NOT touch Google — we return a would_publish result and make no network call.
    """
    if not config.publish_enabled() or not config.gbp_enabled():
        return PublishResult(ok=True, mode="would_publish",
                             detail="draft-only (publish or GBP flag OFF)")

    token = _token()
    if not token:
        raise MissingToken("No GBP access token set for this location.")

    body = build_local_post(
        summary=draft.caption,
        image_url=getattr(draft, "creative_public_url", "") or "",
        cta_type=getattr(draft, "cta_type", "") or config.GBP_DEFAULT_CTA,
        cta_url=getattr(draft, "cta_url", "") or "",
        topic_type=getattr(draft, "topic_type", "") or "STANDARD",
    )

    client = http or _requests()
    url = (f"{config.GBP_API_BASE}/accounts/{config.GBP_ACCOUNT_ID}"
           f"/locations/{config.GBP_LOCATION_ID}/localPosts")
    resp = client.post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    _raise_for_status(resp)
    return PublishResult(ok=True, mode="published", post_id=(resp.json() or {}).get("name", ""))


def _raise_for_status(resp):
    if getattr(resp, "status_code", 200) >= 400:
        raise GbpError(f"GBP API error {resp.status_code}: {resp.text}")
