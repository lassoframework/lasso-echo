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

    def _chat_post(self, text, blocks):
        client = self._http or _requests()
        payload = {"channel": self._channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
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
