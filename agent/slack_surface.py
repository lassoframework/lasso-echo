"""
Slack control surface (Ranger-style home-channel poster).

Posts an approval card per draft: target account, scheduled time, the creative
reference, the caption + hashtags, and the action protocol (Approve / Edit /
Skip). Buttons are included for when Slack interactivity is wired; the reply
protocol is the robust Stage 1 path and is documented on the card itself.

The Slack client is injectable so tests never hit the network. Tokens are read
from env at send time and never logged.
"""

import json
import os

from . import config
from .meta_publisher import _is_video


def _requests():
    import requests
    return requests


class SlackPoster:
    def __init__(self, http=None, token=None, channel=None):
        self._http = http
        self._token = token or os.environ.get(config.SLACK_BOT_TOKEN_ENV)
        self._channel = channel or config.SLACK_CHANNEL_ID

    def post_approval_card(self, draft):
        """Post one approval card. Returns the Slack API response dict."""
        blocks = build_card_blocks(draft)
        return self._chat_post(text=_fallback_text(draft), blocks=blocks)

    def post_notice(self, text):
        """Plain notice, e.g. 'voice doc missing, not drafting'."""
        return self._chat_post(text=text, blocks=None)

    def post_thread_reply(self, channel, ts, text):
        """Plain reply inside a card's thread (e.g. the LIVE permalink after a
        confirmed publish). Falls back to a channel notice when no thread ref."""
        if not ts:
            return self.post_notice(text)
        return self._chat_post(text=text, blocks=None, channel=channel, thread_ts=ts)

    def update_card(self, channel, ts, text, blocks):
        """Edit an existing card in place via chat.update (e.g. to a SUPERSEDED
        state). Returns the Slack response dict; {"ok": False} without a ts."""
        if not ts:
            return {"ok": False, "error": "no_message_ref"}
        client = self._http or _requests()
        payload = {"channel": channel or self._channel, "ts": ts, "text": text}
        if blocks:
            payload["blocks"] = blocks
        resp = client.post(
            "https://slack.com/api/chat.update",
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json; charset=utf-8"},
            data=json.dumps(payload),
            timeout=30,
        )
        try:
            return resp.json()
        except Exception:
            return {"ok": False}

    def mark_superseded(self, draft):
        """Rewrite a superseded draft's card in place: header flipped to SUPERSEDED,
        buttons gone, one line saying which card to use instead."""
        return self.update_card(
            draft.slack_channel, draft.slack_ts,
            text=f"SUPERSEDED: {draft.account_key} draft {draft.draft_id}",
            blocks=build_superseded_blocks(draft),
        )

    def mark_expired(self, draft):
        """Rewrite an expired draft's card in place: header flipped to EXPIRED,
        buttons gone, same edit-in-place path as a supersede."""
        return self.update_card(
            draft.slack_channel, draft.slack_ts,
            text=f"EXPIRED: {draft.account_key} draft {draft.draft_id}",
            blocks=build_expired_blocks(draft),
        )

    def _chat_post(self, text, blocks, channel=None, thread_ts=None):
        client = self._http or _requests()
        payload = {"channel": channel or self._channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        resp = client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json; charset=utf-8"},
            data=json.dumps(payload),
            timeout=30,
        )
        try:
            return resp.json()
        except Exception:
            return {"ok": False}


def _fallback_text(draft):
    kind = "STORY" if getattr(draft, "is_story", False) else "post"
    return f"Approval needed: {draft.account_key} {kind} {draft.draft_id}"


def _is_hosted_image(url):
    """True if `url` is a hosted still image (.png/.jpg/.jpeg/.webp), case-insensitive."""
    return bool(url) and str(url).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))


def _preview_blocks(draft):
    """
    Inline creative preview for the approval card. ADDITIVE — this never replaces
    the *Creative:* text line, the fields, the buttons, or the reply protocol.
    Returns a list of 0+ Block Kit blocks:
      - carousel (2+ slides) with hosted slide_urls -> image of slide 1 + a note
      - single hosted still image                   -> an image block
      - video / Reel                                -> a note (not previewed inline)
      - no hosted url                               -> a note (shows once hosted)
    """
    slides = getattr(draft, "slides", None) or []
    slide_urls = getattr(draft, "slide_urls", None) or []

    def _ctx(text):
        return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}

    if len(slides) > 1 and slide_urls:
        n = len(slides)
        return [
            {"type": "image", "image_url": slide_urls[0],
             "alt_text": f"carousel cover, slide 1 of {n}"},
            _ctx(f"Carousel preview: slide 1 of {n}. The remaining slides post in order."),
        ]
    if _is_video(draft.creative_public_url) or _is_video(draft.creative_path):
        return [_ctx("Video creative (Reel): not previewed inline in Slack.")]
    if _is_hosted_image(draft.creative_public_url):
        return [{"type": "image", "image_url": draft.creative_public_url,
                 "alt_text": "creative preview"}]
    return [_ctx("The image will show here once the creative is hosted at a public URL.")]


def build_card_blocks(draft):
    """Block Kit card. Buttons carry the draft_id in their value."""
    if draft.status.value == "blocked":
        return [{
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f":no_entry: *Blocked* for *{draft.account_key}*\n{draft.blocked_reason}"},
        }]

    tags = " ".join(draft.hashtags)
    caption_preview = draft.caption if draft.caption else "(empty caption)"

    is_story = getattr(draft, "is_story", False)
    slides = getattr(draft, "slides", None) or []
    if slides:
        names = ", ".join(os.path.basename(s) for s in slides)
        creative_ref = f"Carousel — {len(slides)} slides\n{names}"
    elif _is_video(draft.creative_public_url) or _is_video(draft.creative_path):
        fname = os.path.basename(draft.creative_path or draft.creative_public_url)
        creative_ref = f"Reel — {fname}"
    else:
        creative_ref = draft.creative_public_url or draft.creative_path or "(no creative)"
    if is_story:
        # Label a Story loudly so it can never be confused with a feed post.
        creative_ref = "STORY (9:16 vertical)\n" + creative_ref

    header = (f"Approve STORY: {draft.account_key}" if is_story
              else f"Approve post — {draft.account_key}")
    return [
        {"type": "header",
         "text": {"type": "plain_text", "text": header}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Account:*\n{draft.account_key} ({draft.platform})"},
            {"type": "mrkdwn", "text": f"*Scheduled:*\n{draft.scheduled_for}"},
        ]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*Creative:*\n{creative_ref}"}},
        *_preview_blocks(draft),
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*Caption:*\n{caption_preview}"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": f"*Hashtags:* {tags or '(none)'}"}]},
        {"type": "actions", "block_id": f"approve_block::{draft.draft_id}", "elements": [
            {"type": "button", "style": "primary",
             "text": {"type": "plain_text", "text": "Approve"},
             "action_id": "approve", "value": draft.draft_id},
            {"type": "button",
             "text": {"type": "plain_text", "text": "Edit"},
             "action_id": "edit", "value": draft.draft_id},
            {"type": "button", "style": "danger",
             "text": {"type": "plain_text", "text": "Skip"},
             "action_id": "skip", "value": draft.draft_id},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": ("Or reply:  `approve " + draft.draft_id + "`  |  `edit " +
                  draft.draft_id + " <your note>`  |  `skip " + draft.draft_id + "`")}]},
    ]


def build_superseded_blocks(draft):
    """
    The SUPERSEDED card state: header rewritten, NO buttons (nothing left to tap),
    one clear line pointing at the newest card. Approving via the reply protocol
    still hits the approvals gate, which refuses a superseded draft.
    """
    kind = "STORY" if getattr(draft, "is_story", False) else "post"
    return [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"SUPERSEDED: {draft.account_key}"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": (f":no_entry_sign: This {kind} draft ({draft.draft_id}) was "
                           "replaced by a newer draft for the same account and day. "
                           "Use the newest card; this one can no longer be approved.")}},
    ]


def build_expired_blocks(draft):
    """
    The EXPIRED card state: header rewritten, NO buttons (nothing left to tap),
    one clear line saying the posting day passed. Approving via the reply protocol
    still hits the approvals gate, which refuses an expired draft.
    """
    kind = "STORY" if getattr(draft, "is_story", False) else "post"
    day = getattr(draft, "day_key", "") or "its posting day"
    return [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"EXPIRED: {draft.account_key}"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": (f":hourglass: This {kind} draft ({draft.draft_id}) was for "
                           f"{day} and that day has passed, so it can no longer be "
                           "approved. Use today's card instead.")}},
    ]


def parse_reply(text):
    """
    Parse a reply-protocol command. Returns (action, draft_id, note) or None.
      approve <id>
      edit <id> <note...>
      skip <id>
    """
    if not text:
        return None
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2:
        return None
    action = parts[0].lower()
    if action not in {"approve", "edit", "skip"}:
        return None
    draft_id = parts[1]
    note = parts[2] if len(parts) > 2 else ""
    if action == "edit" and not note:
        return None
    return (action, draft_id, note)
