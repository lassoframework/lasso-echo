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

WHERE THIS IS WIRED (2026-09-06 — it is no longer unconsumed)
  agent/drafter.py, StoryBrandGenerator._cross_gym_form_block, called from
  StoryBrandGenerator.build and emitted LAST of the prompt's hint blocks: below
  the brand voice doc, below the approved source, and below this gym's own
  learned preferences (_brain_guidance). FORM hints only, never a content
  instruction. That consumer is behind AGENT_BRAIN_FEEDS_CAPTIONS (default OFF)
  on top of this module's own AGENT_CROSS_GYM_BRAIN, so BOTH flags must be armed
  before a single line reaches a prompt.

  The previous version of this docstring said the module shipped "built and
  tested but unconsumed" because the drafter was owned by a concurrent change.
  That was the "built but not wired" pattern D68 catalogues, and closing it is
  what this change is for. tests/test_cross_gym_brain.py now asserts the consumer
  exists BY NAME in the drafter, so this note cannot go stale again silently.
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


# ---------------------------------------------------------------------------
# RENDERING, and why it is a fixed phrase table rather than the raw tokens
#
# The first version of prompt_lines emitted the lever tokens directly:
#     "avoid ask_present yes on feed posts (fleet form signal, n=23 ...)"
# That was measured against the real model on real LASSO material, 15 captions
# per arm with an OFF/OFF control, and it MOVED THE TARGETED LEVER BACKWARDS:
# control OFF vs OFF was 0/15 vs 0/15 (p = 1.0), and turning the hints on took
# the caption body's ask rate from 0/30 to 5/15 (Fisher exact p = 0.0025) — the
# exact opposite of what "avoid an ask" was asking for.
#
# The cause is ordinary and worth remembering: "ask_present" is not an
# instruction, it is a column name, and a negated instruction that NAMES the
# thing to avoid makes that thing more salient, not less. The model saw "ask" and
# wrote one.
#
# So each (lever, value) is rendered through a FIXED phrase table below, and the
# guidance is expressed as something to DO. Two consequences worth stating:
#   * the isolation guarantee gets STRONGER, not weaker. A rendered line is now
#     assembled entirely from this module's own constant text plus the two
#     integers n and gyms. The lever VALUE selects a phrase; it is never printed.
#     There is now literally no path by which any string that came out of a
#     database reaches a prompt.
#   * only levers a CAPTION WRITER can act on are rendered. A finding about
#     pillar, time slot, format or media product type is real and is kept in the
#     rollup, but the body writer cannot act on it (the pillar and the slot were
#     decided upstream, before this prompt existed), so shipping it here would be
#     noise competing with the brand bible for the model's attention.
# ---------------------------------------------------------------------------

WRITABLE_LEVERS = ("hook_family", "caption_len_band", "sentence_band",
                   "ask_present", "ask_type")

_PHRASES = {
    "hook_family": {
        "question": "open with one real question the reader is asking themselves",
        "bold_claim": "open with a short, flat, declarative claim",
        "story_open": "open on a moment or a scene rather than a diagnosis of "
                      "the reader",
        "number_lead": "open with a figure, and only one that is already stated "
                       "verbatim in the approved source above",
        "pain_callout": "open by naming the reader's problem directly",
    },
    "caption_len_band": {
        "short": "keep the body short, under roughly 150 characters",
        "mid": "keep the body medium, roughly 150 to 500 characters",
        "long": "let the body run long, over roughly 500 characters",
    },
    "sentence_band": {
        "one_or_two": "write one or two sentences",
        "three_to_five": "write three to five sentences",
        "six_plus": "write six or more short sentences",
    },
    "ask_present": {
        # phrased as the ACTION, never as "avoid an ask": see the note above
        "no": "end the body without asking the reader to do anything. The call "
              "to action is added separately after the body, so the body itself "
              "should simply land its point and stop",
        "yes": "put one clear call to action inside the body",
    },
    "ask_type": {
        "booking_link": "when the body asks for anything, point at booking",
        "dm": "when the body asks for anything, ask for a DM",
        "comment_keyword": "when the body asks for anything, ask for a comment",
        "bio": "when the body asks for anything, point at the link in bio",
        "none": "do not ask for anything in the body itself",
    },
}

# The direction flips which VALUE is being recommended, and for a two value
# lever "avoid X" is the same instruction as "favor the other one". Rendering
# both would say the same thing twice and re-introduce the salience problem, so
# an avoid is turned into a favor of its opposite where one exists, and dropped
# where it does not (there is no single "not a question" hook to recommend).
_OPPOSITE = {
    "ask_present": {"yes": "no", "no": "yes"},
    "has_member_face": {"yes": "no", "no": "yes"},
}


def _instruction(item):
    """The imperative phrase for one guidance item, or None when the item is not
    something a caption body writer can act on. Pure, and the return value is
    always a constant from _PHRASES — never a value read from the rollup."""
    lever, value = item.get("lever"), item.get("value")
    if lever not in WRITABLE_LEVERS:
        return None
    if item.get("direction") == "avoid":
        value = (_OPPOSITE.get(lever) or {}).get(value)
        if value is None:
            return None
    return (_PHRASES.get(lever) or {}).get(value)


def prompt_lines(gym_id, now=None, store=None,
                 max_age_days=DEFAULT_MAX_AGE_DAYS):
    """The qualifying guidance rendered as actionable FORM hint lines.

    Each line is a fixed phrase from _PHRASES plus this run's n and gyms counts.
    Nothing read from the database is ever printed, so a line cannot contain a
    caption fragment, a stat, an offer, a member name or a handle even if a
    poisoned row somehow survived the whitelist upstream.

    Empty list when there is no qualifying guidance, when nothing that qualified
    is a lever the body writer can act on, or when the flag is off — the caller
    then adds NOTHING to the prompt (not an empty block)."""
    res = guidance_for(gym_id, now=now, store=store, max_age_days=max_age_days)
    lines, seen = [], set()
    for it in res.get("guidance") or []:
        phrase = _instruction(it)
        if not phrase or phrase in seen:
            continue          # one instruction per lever, never the same twice
        seen.add(phrase)
        lines.append(f"{phrase} (fleet form signal across {it.get('gyms')} gyms, "
                     f"n={it.get('n')})")
    return lines
