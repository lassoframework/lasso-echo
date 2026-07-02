"""
Append-only post log for later reporting.

Records what we published (or would have published in draft-only mode). It NEVER
contains a token. The reporting dashboard (later stage) reads from this.
"""

import json
from datetime import datetime, timezone

from . import config

# explicit allowlist of fields. token is not here and never will be.
_FIELDS = ["account_key", "platform", "published_at", "caption",
           "media_id", "mode", "draft_id"]


def log_post(account_key, platform, caption, media_id, mode, draft_id,
             path=None):
    """
    mode is "published" (real Meta write) or "would_publish" (draft-only).
    Returns the record dict that was written.
    """
    record = {
        "account_key": account_key,
        "platform": platform,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "caption": caption,
        "media_id": media_id,
        "mode": mode,
        "draft_id": draft_id,
    }
    # safety: drop anything not on the allowlist (defends against leaks)
    record = {k: v for k, v in record.items() if k in _FIELDS}
    with open(path or config.POST_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    # STORAGE SWAP (Tier 2): the same record also lands in the posts table on
    # /data (agent/db.py) so reporting can query it. The jsonl stays for compat.
    try:
        from . import db as _db
        with _db.connect() as conn:
            conn.execute(
                "INSERT INTO posts (draft_id, account_key, platform, caption, "
                "media_id, mode, published_at) VALUES (?,?,?,?,?,?,?)",
                (record["draft_id"], record["account_key"], record["platform"],
                 record["caption"], record["media_id"], record["mode"],
                 record["published_at"]))
            conn.commit()
    except Exception as e:
        print(f"[postlog] db mirror failed (jsonl still written): "
              f"{type(e).__name__}: {e}")
    return record


def read_log(path=None):
    out = []
    p = path or config.POST_LOG_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except FileNotFoundError:
        pass
    return out


def used_creatives_for(account_key, path=None):
    """Which creatives have we posted for this account (for LRU selection)."""
    # caption-based history is not creative-path; we track via draft side log later.
    # Stage 1 keeps it simple: return [] unless a future store is wired.
    return []
