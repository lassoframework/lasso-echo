"""mentions.py — the gym's consent-gated mention allowlist, one read helper.

publish_guard rail 3 (proof/results must carry an allowlisted @mention) reads
allowlisted_handles(gym_id): every handle on gym_tag_allowlist for the gym
WHERE kind <> 'member' OR consent IS TRUE — i.e. own/coach/partner handles are
always mentionable, a MEMBER handle only with explicit consent. Backed by the
same loader tag_allowlist uses (one Supabase read path, no duplicate REST
plumbing). Offline/creds-absent -> [] (callers fail closed).
"""
from __future__ import annotations


def allowlisted_handles(gym_id: str, store=None) -> list:
    """Handles LASSO/publish_guard may mention for this gym: gym_tag_allowlist
    rows WHERE gym_id = %s AND (kind <> 'member' OR consent IS TRUE)."""
    from .tag_allowlist import _load_allowlist
    rows = _load_allowlist(gym_id, store=store)
    out = []
    for row in rows or []:
        if row.get("kind") == "member" and not row.get("consent"):
            continue
        h = str(row.get("handle") or "").strip().lstrip("@")
        if h and h not in out:
            out.append(h)
    return out
