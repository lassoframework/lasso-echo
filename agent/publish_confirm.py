"""
Publish confirmation loop: after a REAL publish, read the post back from the
Graph API (by media id), fetch its permalink, and reply it into the original
card's Slack thread ("LIVE: <permalink>").

OFF BY DEFAULT (`config.publish_confirm_enabled()`). Guarantees:
  - READ-ONLY on Meta: one GET per confirm. It NEVER publishes, re-publishes,
    edits, or deletes; a failed verify warns in the Slack thread and emits one
    ops alert, nothing more.
  - Dormant unless a real publish happened: would_publish (draft-only) results
    return immediately, so with the publish flag OFF this module does nothing.
  - No secrets: the token is read at call time for the GET only, never stored,
    logged, or included in any message.
"""

from . import config, ops_alerts
from .accounts import Platform


def _requests():
    import requests
    return requests


def _default_poster():
    """Injection seam for tests; the real SlackPoster in production."""
    from .slack_surface import SlackPoster
    return SlackPoster()


def _reply(poster, draft, text):
    """Into the card's thread when we hold its message ref; else a channel notice."""
    return poster.post_thread_reply(
        getattr(draft, "slack_channel", ""), getattr(draft, "slack_ts", ""), text)


def _audit(kind_detail, draft, reason):
    try:
        from datetime import datetime, timezone
        from . import db as _db
        _db.audit("publish_confirm", draft.draft_id, f"{kind_detail}: {reason}",
                  draft.account_key, datetime.now(timezone.utc).date().isoformat())
    except Exception:
        pass


def _unconfirmed(poster, draft, account, detail):
    """PUBLISHED BUT UNVERIFIED: the publish call itself SUCCEEDED (we only run
    after mode=="published"), so the post is live; only the read back failed.
    Honest, softer wording: never the same alarm as a real publish failure
    (that loud alert lives in approvals and is unchanged). NEVER re-publish."""
    _reply(poster, draft,
           f"NOTE: draft {draft.draft_id} published to {account.key} (post is "
           f"live), but verification could not confirm it ({detail}). Check the "
           "page manually when convenient.")
    ops_alerts.alert(f"published but verify read failed for {account.key} draft "
                     f"{draft.draft_id}: {detail}. The post itself is live; "
                     "check manually.")
    _audit("published, verify unconfirmed", draft, detail)
    return {"verified": False, "permalink": ""}


def confirm_publish(draft, account, result, http=None, poster=None):
    """
    Verify a just-published post exists and surface its permalink in the card's
    Slack thread. Returns {"verified": bool, "permalink": str}, or None when
    dormant (flag OFF, or the result was not a real publish).
    """
    if not config.publish_confirm_enabled():
        return None
    if result is None or getattr(result, "mode", "") != "published":
        return None  # draft-only / would_publish: nothing real to confirm

    poster = poster or _default_poster()

    media_id = getattr(result, "media_id", "")
    if not media_id:
        return _unconfirmed(poster, draft, account, "publish returned no media id")

    token = account.get_token()
    if not token:
        return _unconfirmed(poster, draft, account,
                            "no token available for the read back")

    # MINIMAL existence read. A FB /photos publish can return the PHOTO node id
    # (not the pageid_postid PagePost id), and a Photo node has no permalink_url
    # field: requesting it 400s while the post is perfectly live (the 2026-07-03
    # lasso_fb false alarm). id + created_time exist on Photo AND PagePost nodes
    # and prove existence with the page token alone. IG media additionally
    # carries permalink, so IG keeps the LIVE link.
    if account.platform == Platform.INSTAGRAM:
        fields = "id,permalink"
    else:
        fields = "id,created_time"
    client = http or _requests()
    try:
        r = client.get(
            f"{config.GRAPH_API_BASE}/{media_id}",
            params={"fields": fields, "access_token": token},
            timeout=30,
        )
        if getattr(r, "status_code", 200) >= 400:
            return _unconfirmed(poster, draft, account,
                                f"read back returned HTTP {r.status_code}")
        body = r.json() or {}
    except Exception as e:
        return _unconfirmed(poster, draft, account,
                            f"read back errored: {type(e).__name__}: {e}")

    if not body.get("id"):
        return _unconfirmed(poster, draft, account,
                            "read back returned no id for the post")

    permalink = body.get("permalink") or body.get("permalink_url") or ""
    if permalink:
        _reply(poster, draft, f"LIVE: {permalink}")
    else:
        _reply(poster, draft,
               f"LIVE on {account.key}: post verified (id {media_id}).")
    _audit("verified live", draft, permalink or f"id {media_id}")
    return {"verified": True, "permalink": permalink}
