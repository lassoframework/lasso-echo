"""
Publish confirmation loop: after a REAL publish, read the post back from the
Graph API (by media id), fetch its permalink, and reply it into the original
card's Slack thread ("LIVE: <permalink>").

OFF BY DEFAULT (`config.publish_confirm_enabled()`). Guarantees:
  - READ-ONLY on Meta: one GET per confirm. It NEVER publishes, re-publishes,
    edits, or deletes.
  - A failed verify is NOT a failure. confirm_publish only runs after the publish
    POST already succeeded (mode=="published"), so the post IS live. If the
    read-back cannot confirm it, we post ONE soft note into the card thread and
    emit NO ops alert. We never imply a live post failed. (The loud alarm for an
    ACTUAL failed publish lives in approvals.py and is unchanged.)
  - Dormant unless a real publish happened: would_publish (draft-only) results
    return immediately, so with the publish flag OFF this module does nothing.
  - No secrets: the token is read at call time for the GET only, never stored,
    logged, or included in any message.
"""

from . import config, db
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
    after mode=="published"), so the post IS live; only the read-back failed.
    This is NOT a failure: we post ONE soft, honest note into the card thread
    and emit NO ops alert (never the alarm of a real publish failure; that loud
    alert lives in approvals and is unchanged). NEVER re-publish.

    Deduped per draft: Slack can retry the tap webhook, running confirm_publish
    twice for the same draft (observed: lasso_fb draft 1527038d4e). Without this
    the same soft note posts twice."""
    note_key = f"verify_noted_{draft.draft_id}"
    if not db.kv_get(note_key):
        db.kv_set(note_key, "1")
        _reply(poster, draft,
               f"NOTE: draft {draft.draft_id} published to {account.key} (post is "
               f"live), but automatic verification could not confirm it ({detail}). "
               "No action needed; check the page manually if you want to be sure.")
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
    # (not the pageid_postid PagePost id); Photo and PagePost nodes disagree on
    # which fields exist, and asking for one the node lacks (or one needing a
    # scope we do not have) 400s while the post is perfectly live (the lasso_fb
    # false alarm). `id` exists on EVERY node and needs no extra scope, so it is
    # the safest existence proof. IG media also carries permalink (instagram_basic,
    # which we already hold to publish), so IG keeps the LIVE link; we never
    # request an insights-scoped field here.
    if account.platform == Platform.INSTAGRAM:
        fields = "id,permalink"
    else:
        fields = "id"
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
