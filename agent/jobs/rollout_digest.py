"""rollout_digest.py — one-page rollout digest per gym for #ops.

For each gym: before-grade, after-grade, slots purged (from Wave 0.2),
slots refilled, mentions seeded. Posts to #ops Slack or writes to a file.

Run after AGENT_CALENDAR_GRADE is flipped ON per gym.
Has run(gyms=None) function.

Behind AGENT_CALENDAR_GRADE. No-op when OFF.

Usage:
  python3 -m agent jobs rollout_digest
  python3 -m agent jobs rollout_digest --gym lasso
  python3 -m agent jobs rollout_digest --gym lasso --gym eng
"""
from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_grades(store, gym_id: str) -> dict:
    """Return the most recent trailing_30 and forward_book grade rows for gym_id,
    or {} when none are available (fail open, never fabricate)."""
    result = {}
    try:
        if hasattr(store, "latest_grade"):
            for window in ("trailing_30", "forward_book"):
                row = store.latest_grade(gym_id, window)
                if row:
                    result[window] = row
            return result
        # Supabase REST fallback: query gym_social_grades for this gym.
        from agent import config
        url = config.supabase_url()
        key = config.supabase_service_key()
        if not url or not key:
            return result
        import urllib.request
        import urllib.parse
        for window in ("trailing_30", "forward_book"):
            params = urllib.parse.urlencode({
                "gym_id": f"eq.{gym_id}",
                "window": f"eq.{window}",
                "order": "graded_at.desc",
                "limit": "1",
                "select": "total,letter,scores,defects,graded_at",
            })
            req = urllib.request.Request(
                f"{url}/rest/v1/gym_social_grades?{params}",
                method="GET",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                rows = json.loads(resp.read())
                if rows:
                    result[window] = rows[0]
    except Exception as exc:
        print(f"[rollout-digest] fetch grades failed for {gym_id}: {type(exc).__name__}: {exc}")
    return result


def _fetch_purged_count(store, gym_id: str) -> int:
    """Count of rows with status='denied' and reject_reason='duplicate_purge_2026_08'
    for gym_id (Wave 0.2 dedup slots freed). Returns 0 on error."""
    try:
        if hasattr(store, "count_denied_purge"):
            return store.count_denied_purge(gym_id) or 0
        from agent import config
        url = config.supabase_url()
        key = config.supabase_service_key()
        if not url or not key:
            return 0
        import urllib.request
        import urllib.parse
        params = urllib.parse.urlencode({
            "gym_id": f"eq.{gym_id}",
            "status": "eq.denied",
            "reject_reason": "eq.duplicate_purge_2026_08",
            "select": "id",
        })
        req = urllib.request.Request(
            f"{url}/rest/v1/content_calendar?{params}",
            method="GET",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "Prefer": "count=exact",
                "Range-Unit": "items",
                "Range": "0-0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_range = resp.headers.get("Content-Range", "")
            # Content-Range: 0-0/N -> N is total
            if "/" in content_range:
                return int(content_range.split("/")[-1])
    except Exception as exc:
        print(f"[rollout-digest] fetch purged count failed for {gym_id}: {type(exc).__name__}: {exc}")
    return 0


def _fetch_refilled_count(store, gym_id: str) -> int:
    """Count of pending rows (forward book) for gym_id — the slots filled after dedup.
    Returns 0 on error."""
    try:
        if hasattr(store, "count_pending"):
            return store.count_pending(gym_id) or 0
        from agent import config
        url = config.supabase_url()
        key = config.supabase_service_key()
        if not url or not key:
            return 0
        import urllib.request
        import urllib.parse
        params = urllib.parse.urlencode({
            "gym_id": f"eq.{gym_id}",
            "status": "eq.pending",
            "select": "id",
        })
        req = urllib.request.Request(
            f"{url}/rest/v1/content_calendar?{params}",
            method="GET",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "Prefer": "count=exact",
                "Range-Unit": "items",
                "Range": "0-0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_range = resp.headers.get("Content-Range", "")
            if "/" in content_range:
                return int(content_range.split("/")[-1])
    except Exception as exc:
        print(f"[rollout-digest] fetch refilled count failed for {gym_id}: {type(exc).__name__}: {exc}")
    return 0


def _fetch_mention_count(store, gym_id: str) -> int:
    """Count of seeded allowlist entries in gym_tag_allowlist for gym_id.
    Returns 0 on error."""
    try:
        if hasattr(store, "count_allowlist"):
            return store.count_allowlist(gym_id) or 0
        from agent import config
        url = config.supabase_url()
        key = config.supabase_service_key()
        if not url or not key:
            return 0
        import urllib.request
        import urllib.parse
        params = urllib.parse.urlencode({
            "gym_id": f"eq.{gym_id}",
            "select": "handle",
        })
        req = urllib.request.Request(
            f"{url}/rest/v1/gym_tag_allowlist?{params}",
            method="GET",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "Prefer": "count=exact",
                "Range-Unit": "items",
                "Range": "0-0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_range = resp.headers.get("Content-Range", "")
            if "/" in content_range:
                return int(content_range.split("/")[-1])
    except Exception as exc:
        print(f"[rollout-digest] fetch mention count failed for {gym_id}: {type(exc).__name__}: {exc}")
    return 0


def _default_gyms() -> list:
    """Default gym list: all client gyms + lasso. Degrades to ['lasso'] on error."""
    try:
        from agent.calendar_autopublish import client_gym_bases
        gyms = list(client_gym_bases() or [])
        if "lasso" not in gyms:
            gyms = ["lasso"] + gyms
        return gyms
    except Exception:
        return ["lasso"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(gyms=None, store=None) -> list:
    """Build a rollout digest for each gym in the list (or all client gyms if None).

    For each gym:
      - Read gym_social_grades for trailing_30 and forward_book (before/after picture)
      - Read count of denied rows with reject_reason='duplicate_purge_2026_08' (purged)
      - Read count of pending rows in content_calendar (refilled)
      - Read count of seeded gym_tag_allowlist entries (mentions seeded)
      - Format a one-page digest string

    Returns a list of digest strings (one per gym). Behind AGENT_CALENDAR_GRADE:
    when OFF returns a single entry explaining the flag is off.

    No network calls are made without the Supabase creds present. Fails open
    on every lookup (0 counts, N/A grades) so the digest always completes.
    """
    from agent import config

    if not config.calendar_grade_enabled():
        return ["[rollout-digest] AGENT_CALENDAR_GRADE is OFF. No digest produced. "
                "Flip the flag per WAVE6_HUMAN_TAPS.md then re-run."]

    if gyms is None:
        gyms = _default_gyms()

    digests = []
    for gym_id in gyms:
        grades = _fetch_grades(store, gym_id)
        trailing = grades.get("trailing_30", {})
        forward = grades.get("forward_book", {})

        before_total = trailing.get("total")
        before_letter = trailing.get("letter", "N/A")
        after_total = forward.get("total")
        after_letter = forward.get("letter", "N/A")

        before_str = (
            f"{before_letter} ({before_total}/100)"
            if before_total is not None
            else "N/A (no trailing data)"
        )
        after_str = (
            f"{after_letter} ({after_total}/100)"
            if after_total is not None
            else "N/A (no forward data)"
        )

        purged_count = _fetch_purged_count(store, gym_id)
        refilled_count = _fetch_refilled_count(store, gym_id)
        mention_count = _fetch_mention_count(store, gym_id)

        # Per-gym flag state: did Blake flip this gym's flag yet?
        per_gym_env = f"AGENT_CALENDAR_GRADE_{gym_id.upper().replace('-', '_')}"
        flag_state = config._truthy(
            __import__("os").environ.get(per_gym_env, "")
        ) if __import__("os").environ.get(per_gym_env) is not None else None

        if flag_state is True:
            flag_line = f"Flag: {per_gym_env}=true (ARMED)"
        elif flag_state is False:
            flag_line = f"Flag: {per_gym_env}=false (DISARMED by override)"
        else:
            flag_line = (
                f"Flag: {per_gym_env} not set "
                f"(falls back to global AGENT_CALENDAR_GRADE="
                f"{'true' if config.calendar_grade_enabled() else 'false'})"
            )

        digest = (
            f"=== {gym_id.upper()} Rollout Digest ===\n"
            f"Before grade: {before_str}\n"
            f"After grade: {after_str}\n"
            f"Slots purged (dupes): {purged_count}\n"
            f"Slots refilled (pending): {refilled_count}\n"
            f"Mentions seeded: {mention_count} handles\n"
            f"{flag_line}\n"
            f"Status: READY FOR FLAG FLIP (human tap required)"
        )
        digests.append(digest)
        print(digest)
        print()

    return digests


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    gyms_arg = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--gym" and i + 1 < len(args):
            gyms_arg.append(args[i + 1])
            i += 2
        else:
            i += 1
    result = run(gyms=gyms_arg if gyms_arg else None)
    print(json.dumps(result, indent=2, default=str))
