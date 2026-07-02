"""
Pending-draft store (runtime memory).

When Echo drafts a post and posts the Slack card, the draft has to live somewhere
until Blake taps Approve / Edit / Skip. This is that store. JSON-backed, simple,
and it never holds a token (drafts carry captions and a creative reference, not
credentials).

This is "runtime memory" in the storage split: it lives in a data file the host
backs up, separate from the git-tracked voice doc and config.
"""

import json
import os

from . import config, ops_alerts
from .drafter import Draft, DraftStatus

STORE_PATH_DEFAULT = os.environ.get("AGENT_PENDING_PATH", "pending_drafts.json")


def _to_dict(d: Draft):
    return {
        "draft_id": d.draft_id,
        "account_key": d.account_key,
        "platform": d.platform,
        "caption": d.caption,
        "hashtags": d.hashtags,
        "creative_path": d.creative_path,
        "creative_public_url": d.creative_public_url,
        "scheduled_for": d.scheduled_for,
        "status": d.status.value,
        "blocked_reason": d.blocked_reason,
        "source_fragments": d.source_fragments,
        "slides": d.slides,
        "slide_urls": d.slide_urls,
        "is_story": d.is_story,
        "day_key": d.day_key,
        "draft_type": d.draft_type,
        "slack_channel": d.slack_channel,
        "slack_ts": d.slack_ts,
    }


def _from_dict(r):
    return Draft(
        draft_id=r["draft_id"],
        account_key=r["account_key"],
        platform=r["platform"],
        caption=r["caption"],
        hashtags=r.get("hashtags", []),
        creative_path=r.get("creative_path", ""),
        creative_public_url=r.get("creative_public_url", ""),
        scheduled_for=r.get("scheduled_for", ""),
        status=DraftStatus(r.get("status", "pending")),
        blocked_reason=r.get("blocked_reason", ""),
        source_fragments=r.get("source_fragments", []),
        slides=r.get("slides", []),
        slide_urls=r.get("slide_urls", []),
        is_story=bool(r.get("is_story", False)),
        day_key=r.get("day_key", ""),
        draft_type=r.get("draft_type", ""),
        slack_channel=r.get("slack_channel", ""),
        slack_ts=r.get("slack_ts", ""),
    )


class PendingStore:
    def __init__(self, path=None):
        self.path = path or STORE_PATH_DEFAULT

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, data):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            # Behavior unchanged (the error still raises); it is just never silent:
            # logged always, plus one ops alert when the flag is armed.
            msg = f"store write failed for {self.path}: {type(e).__name__}: {e}"
            print(f"[store] {ops_alerts.scrub(msg)}")
            ops_alerts.alert(msg)
            raise

    def put(self, draft: Draft):
        data = self._load()
        data[draft.draft_id] = _to_dict(draft)
        self._save(data)
        return draft

    def get(self, draft_id):
        data = self._load()
        r = data.get(draft_id)
        return _from_dict(r) if r else None

    def remove(self, draft_id):
        data = self._load()
        if draft_id in data:
            del data[draft_id]
            self._save(data)
            return True
        return False

    def list_pending(self):
        data = self._load()
        return [_from_dict(r) for r in data.values()
                if r.get("status") == DraftStatus.PENDING.value]

    def find_pending(self, account_key, day_key, draft_type):
        """
        The PENDING draft for (account, day, type), or None. This is the idempotency
        lookup: run-daily uses it to return an existing draft instead of creating a
        duplicate. Only drafts written with the idempotent flag ON carry day_key and
        draft_type, so older records simply never match.
        """
        data = self._load()
        for r in data.values():
            if (r.get("status") == DraftStatus.PENDING.value
                    and r.get("account_key") == account_key
                    and r.get("day_key") == day_key
                    and r.get("draft_type") == draft_type):
                return _from_dict(r)
        return None
