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
import time

from . import config
from .meta_publisher import _is_video


def _requests():
    """Stdlib urllib adapter — same interface as requests, zero extra deps.
    SlackPoster._send calls client.post(url, headers=..., data=..., timeout=...)
    and reads resp.status_code / resp.json() / resp.headers."""
    import json as _json
    import urllib.error as _ue
    import urllib.request as _ur

    class _Resp:
        def __init__(self, status_code, body_bytes, headers):
            self.status_code = status_code
            self._body = body_bytes
            self.headers = headers or {}

        def json(self):
            return _json.loads(self._body.decode())

    class _Client:
        def post(self, url, headers=None, data=None, timeout=30):
            if isinstance(data, str):
                data = data.encode()
            req = _ur.Request(url, data=data, headers=headers or {}, method="POST")
            try:
                with _ur.urlopen(req, timeout=timeout) as r:
                    return _Resp(r.status, r.read(), dict(r.headers))
            except _ue.HTTPError as e:
                # 4xx / 5xx: return a response so _send can read the Slack error
                # body and status rather than propagating an exception.
                return _Resp(e.code, e.read(), dict(e.headers))

    return _Client()


# Rate-limit backoff: a 12-account morning fan-out must not drop cards when
# Slack answers 429. Every send retries up to SLACK_MAX_RETRIES times, honoring
# the Retry-After header (falling back to exponential 1s/2s/4s). A hard failure
# after the retries returns ok:False like any other failed send — the runner
# alerts and moves to the next account, never crashes the fan-out.
SLACK_MAX_RETRIES = 3
SLACK_BACKOFF_BASE_SEC = 1.0


class SlackPoster:
    def __init__(self, http=None, token=None, channel=None, sleep=None):
        self._http = http
        self._token = token or os.environ.get(config.SLACK_BOT_TOKEN_ENV)
        self._channel = channel or config.SLACK_CHANNEL_ID
        self._sleep = sleep or time.sleep

    def _send(self, url, payload):
        """THE one Slack transport: every send (post, thread reply, card edit)
        goes through here. Retries rate limits with backoff; degrades transport
        errors to a failed-send dict; never raises into a caller."""
        client = self._http or _requests()
        delay = SLACK_BACKOFF_BASE_SEC
        for attempt in range(SLACK_MAX_RETRIES + 1):
            try:
                resp = client.post(
                    url,
                    headers={"Authorization": f"Bearer {self._token}",
                             "Content-Type": "application/json; charset=utf-8"},
                    data=json.dumps(payload),
                    timeout=30,
                )
            except Exception as e:
                # A Slack outage/timeout must degrade to a failed send, never
                # raise into the daily run or a card sweep.
                print(f"[slack] transport error on {url.rsplit('/', 1)[-1]}: "
                      f"{type(e).__name__}")
                return {"ok": False, "error": "transport"}
            status = getattr(resp, "status_code", 200)
            rate_limited = status == 429
            body = None
            if not rate_limited:
                try:
                    body = resp.json()
                except Exception:
                    return {"ok": False}
                rate_limited = (body or {}).get("error") == "ratelimited"
            if rate_limited and attempt < SLACK_MAX_RETRIES:
                try:
                    wait = float((getattr(resp, "headers", None) or {})
                                 .get("Retry-After") or delay)
                except (TypeError, ValueError):
                    wait = delay
                print(f"[slack] rate limited (attempt {attempt + 1}); "
                      f"retrying in {wait:.0f}s")
                self._sleep(wait)
                delay *= 2
                continue
            if rate_limited:
                return {"ok": False, "error": "ratelimited"}
            return body if body is not None else {"ok": False}
        return {"ok": False, "error": "ratelimited"}

    def post_approval_card(self, draft):
        """Post one approval card. Returns the Slack API response dict.
        The card routes to the draft account's own approval channel when the
        account sets one (per-client channels); otherwise this poster's
        default channel. Ops notices stay on the default channel."""
        blocks = build_card_blocks(draft)
        return self._chat_post(text=_fallback_text(draft), blocks=blocks,
                               channel=self._channel_for(draft))

    def _channel_for(self, draft):
        try:
            from .accounts import get_account
            acct = get_account(getattr(draft, "account_key", "") or "")
            if acct is not None and acct.slack_channel:
                return acct.slack_channel
        except Exception:
            pass
        return self._channel

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
        payload = {"channel": channel or self._channel, "ts": ts, "text": text}
        if blocks:
            payload["blocks"] = blocks
        return self._send("https://slack.com/api/chat.update", payload)

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
        payload = {"channel": channel or self._channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._send("https://slack.com/api/chat.postMessage", payload)


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
    warnings = getattr(draft, "warnings", None) or []

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
        # quality-guard warnings (e.g. the OCR headline check): visible, never blocking
        *[{"type": "context",
           "elements": [{"type": "mrkdwn", "text": f":warning: {w}"}]}
          for w in warnings],
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
