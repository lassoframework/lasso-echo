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

from . import config
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
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

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
