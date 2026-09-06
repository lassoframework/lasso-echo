"""cross_gym_guidance.py — the READ side of the cross gym brain.

`guidance_for(gym_id)` is the clean, no-op-by-default API the caption generator
calls to ask: across every gym Echo posts for, which FORM choices actually
correlate with better engagement, at a level that survived a real significance
test and a multiple comparisons correction?

CONTRACT
  * Behind AGENT_CROSS_GYM_BRAIN (the same flag as the writer). Flag OFF -> an
    empty result, no store constructed, nothing read. Zero behavior change.
  * Empty when nothing cleared the bar. On today's fleet volume that is the
    expected answer for most levers and often for all of them. An empty list is
    a CORRECT answer, never a reason to relax a threshold.
  * Empty when the newest rollup is older than `max_age_days` (stale guidance is
    worse than none).
  * Every item is re-validated against the FORM ONLY whitelist on the way out
    (cross_gym_brain.form_only_violations). A stored row that somehow carried a
    caption fragment, a stat, an offer, or a member name cannot be served: the
    item is dropped, not sanitized.

CROSS GYM ISOLATION
  The guidance is FLEET FORM STATISTICS. It is IDENTICAL for every gym — that is
  the isolation guarantee, made structural: there is no branch in this module
  along which one gym's data could be routed into another gym's prompt.
  `gym_id` is required (it is the caller's own tenant scope, and the natural home
  for a future per gym opt out) but it never selects, filters, or shapes the
  content that comes back. tests/test_cross_gym_brain.py asserts two different
  gyms receive byte identical guidance.

WHERE THIS SHOULD BE WIRED (deliberately NOT wired in this change)
  agent/drafter.py, in the caption prompt assembly, alongside the per gym
  gym_playbook read: append `prompt_lines()` as FORM hints only, BELOW the brand
  bible and the approved source, and never as a content instruction. The drafter
  is owned by a concurrent change, so this module ships built and tested but
  unconsumed. Nothing in Echo calls it yet.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent import config
from agent.jobs import cross_gym_brain as brain

DEFAULT_MAX_AGE_DAYS = 14

EMPTY = {"ok": True, "reason": "", "guidance": [], "run_at": None,
         "window": None}


def _parse(ts):
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _clean(items):
    """Keep only guidance items that are pure FORM: a whitelisted lever, a
    whitelisted value for that lever, a known direction, and nothing else in the
    dict that the whitelist does not recognise. A failing item is DROPPED."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        lever = it.get("lever")
        value = it.get("value")
        allowed = brain.ALLOWED_LEVER_VALUES.get(lever)
        if not allowed or value not in allowed:
            continue
        if it.get("direction") not in brain.DIRECTIONS:
            continue
        if brain.form_only_violations(it):
            continue
        out.append(dict(it))
    return out


def guidance_for(gym_id, now=None, store=None,
                 max_age_days=DEFAULT_MAX_AGE_DAYS):
    """The fleet FORM guidance, or an empty result.

    Returns {"ok", "reason", "guidance": [...], "run_at", "window"}. `guidance`
    is a list of {lever, value, format_stratum, direction, effect_size, n, gyms,
    q_value} — FORM tokens and numbers only, never text from any post.

    Never raises: any read failure degrades to the empty result."""
    if not str(gym_id or "").strip():
        return dict(EMPTY, ok=False, reason="gym_id is required")
    if not config.cross_gym_brain_enabled():
        return dict(EMPTY, reason="AGENT_CROSS_GYM_BRAIN is OFF (default). "
                                  "No cross gym guidance.")
    try:
        store = store or brain.SupabaseBrainStore()
        row = store.latest_rollup()
    except Exception as exc:  # noqa: BLE001
        return dict(EMPTY, reason=f"rollup read failed: {type(exc).__name__}")
    if not isinstance(row, dict) or not row:
        return dict(EMPTY, reason="no cross gym rollup yet")

    now = now or datetime.now(timezone.utc)
    ran = _parse(row.get("run_at"))
    if ran is not None and max_age_days is not None:
        age_days = (_parse(now) - ran).total_seconds() / 86400.0
        if age_days > float(max_age_days):
            return dict(EMPTY,
                        reason=f"newest rollup is older than {max_age_days} "
                               "days; stale guidance is not served",
                        run_at=row.get("run_at"))

    items = _clean(row.get("guidance"))
    return {
        "ok": True,
        "reason": "" if items else "nothing cleared the significance bar",
        "guidance": items,
        "run_at": row.get("run_at"),
        "window": {"start": row.get("window_start"),
                   "end": row.get("window_end"),
                   "days": row.get("window_days")},
    }


def prompt_lines(gym_id, now=None, store=None,
                 max_age_days=DEFAULT_MAX_AGE_DAYS):
    """The same guidance rendered as plain FORM hint lines for a prompt.

    Built ONLY from whitelisted tokens, so it cannot contain a caption fragment,
    a stat, or an offer. Empty list when there is no qualifying guidance — the
    caller then adds nothing to the prompt."""
    res = guidance_for(gym_id, now=now, store=store, max_age_days=max_age_days)
    lines = []
    for it in res.get("guidance") or []:
        verb = "favor" if it["direction"] == "favor" else "avoid"
        stratum = it.get("format_stratum")
        where = "" if stratum in (None, brain.UNSTRATIFIED) else f" on {stratum} posts"
        lines.append(
            f"{verb} {it['lever']} {it['value']}{where} "
            f"(fleet form signal, n={it.get('n')} across {it.get('gyms')} gyms)")
    return lines
