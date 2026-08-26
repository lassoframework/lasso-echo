"""tag_allowlist.py — consent-gated handle allowlist for @mentions in captions.

Rules (hard):
- Only handles in gym_tag_allowlist may appear in a draft mention list
- member handles require consent=true
- Never tag a member without consent. Never tag an account not on the list.

Tagging rules by category:
- results / proof: tag the member (if consented) or the gym's own handle
- faces: tag the coach
- LASSO proof: tag the client gym (every time — the "0 tags in 219 posts" fix)
"""
from __future__ import annotations
from typing import Optional

# Category -> which kinds of handles to return
_CATEGORY_KINDS: dict[str, list[str]] = {
    "results": ["member", "own"],
    "proof": ["member", "own"],
    "faces": ["coach"],
    "community": ["own"],
    "education": ["own"],
    "offer": ["own"],
    "invite": ["own"],
    "call": ["own"],
    # LASSO B2B categories
    "b2b": ["partner"],
    "doctrine": ["own"],
    "podcast": ["own"],
    "summit": ["own"],
    "book": ["own"],
}


def _load_allowlist(gym_id: str, store=None) -> list[dict]:
    """Load all allowlist rows for a gym. Returns list of {handle, kind, consent}."""
    if store is not None:
        return store.get_allowlist(gym_id)
    # Live Supabase path
    from . import config as _config
    url = _config.supabase_url()
    key = _config.supabase_service_key()
    if not url or not key:
        return []
    try:
        import urllib.request
        import json
        req = urllib.request.Request(
            f"{url}/rest/v1/gym_tag_allowlist?gym_id=eq.{gym_id}&select=handle,kind,consent",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


def allowlisted_handles(
    gym_id: str,
    kind: Optional[str] = None,
    consent_only: bool = False,
    store=None,
) -> list[str]:
    """Return handles from the allowlist, optionally filtered by kind and consent."""
    rows = _load_allowlist(gym_id, store=store)
    out = []
    for row in rows:
        if kind is not None and row.get("kind") != kind:
            continue
        if consent_only and not row.get("consent"):
            continue
        out.append(row["handle"])
    return out


def validate_mentions(gym_id: str, mentions: list[str], store=None) -> list[str]:
    """Returns only the mentions that are on the allowlist (with consent if member).
    Silently drops any handle not on the list or a member without consent."""
    if not mentions:
        return []
    rows = _load_allowlist(gym_id, store=store)
    # Build a lookup: handle -> row
    lookup: dict[str, dict] = {row["handle"]: row for row in rows}
    out = []
    for handle in mentions:
        # Normalize: strip leading @ if present
        h = handle.lstrip("@")
        row = lookup.get(h)
        if row is None:
            # Not on the list — drop silently
            continue
        if row.get("kind") == "member" and not row.get("consent"):
            # Member without consent — drop silently
            continue
        out.append(h)
    return out


def handles_for_category(gym_id: str, category: str, store=None) -> list[str]:
    """Returns the appropriate handles to tag for this category and gym.
    Returns [] when AGENT_MENTIONS is OFF or gym has no allowlist."""
    from . import config as _config
    if not _config.mentions_enabled():
        return []
    kinds = _CATEGORY_KINDS.get(category, ["own"])
    rows = _load_allowlist(gym_id, store=store)
    if not rows:
        return []
    out = []
    seen: set[str] = set()
    for kind in kinds:
        for row in rows:
            if row.get("kind") != kind:
                continue
            if kind == "member" and not row.get("consent"):
                continue
            h = row["handle"]
            if h not in seen:
                out.append(h)
                seen.add(h)
    return out
