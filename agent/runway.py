"""
Creative runway card (Stage 3), gated by AGENT_RUNWAY_ENABLED (default OFF).

Runway = unused approved gate-clean creatives divided by posts per day, per
account. Only assets that could actually ship count: moderation-clean and
consent-clear by construction (they are in the approved library), unposted
(never served and never logged as posted), in-style (not on the off-style
exclusion list, not a story variant), and fabrication-gate clean.

Armed, one line lands with the day's Slack card: the big number, a green /
amber / red read, and the projected zero date. Below AGENT_RUNWAY_ALERT_DAYS
(default 7) ONE debounced ops alert asks for specific raw material (the ask is
drafted from the approved source doc's own pillars, nothing invented).
"""

import json
import os
from datetime import date, timedelta

from . import config, content_planner, db, ops_alerts, rotation
from .library import Creative, list_creatives


def v2_library_concepts(library_path):
    """Creative objects for every non-story regen library concept definition.
    The path is the conventional lasso_v2_<name>.png location. If the file does
    not exist on disk (R2-only render) the Creative still enters the candidate
    pool. client_note is always empty — v2 concepts carry no stat claims in
    their spec, so the fabrication gate is clean by construction."""
    from .regen_library import CONCEPTS
    out = []
    for name in CONCEPTS:
        filename = f"lasso_v2_{name}.png"
        path = os.path.join(library_path, filename)
        public_url = ""
        sidecar = os.path.join(library_path, f"lasso_v2_{name}.json")
        if os.path.isfile(sidecar):
            try:
                with open(sidecar, encoding="utf-8") as fh:
                    public_url = (json.load(fh) or {}).get("public_url", "")
            except Exception:
                pass
        out.append(Creative(path=path, media_type="image",
                            client_note="", public_url=str(public_url)))
    return out


def _posts_per_day():
    posting_days = 7 - len(config.POSTING_SKIP_DAYS)
    return max(posting_days, 1) / 7.0


def _used_keys(account_key):
    used = set()
    for e in rotation.load_served().get(account_key, []):
        used.add(e.get("key", ""))
    try:
        with db.connect() as conn:
            for r in conn.execute(
                    "SELECT creative_key FROM posts WHERE account_key=?",
                    (account_key,)).fetchall():
                if r["creative_key"]:
                    used.add(r["creative_key"])
    except Exception:
        pass
    return used


def classify_creatives(account_key, library_path):
    """
    (eligible, excluded) for one account: THE single implementation both the
    digest's runway_days and the runway --explain CLI read (never two copies
    of the rules). `eligible` is the creative list; `excluded` maps each
    excluded basename to its one reason, first rule that hit.
    """
    off_style = rotation.style_exclusions(library_path)
    used = _used_keys(account_key)
    approved_claims = rotation._approved_claims()
    # Tenant brain (AGENT_TENANT_BRAIN_ENABLED, default OFF -> empty set): a
    # concept THIS tenant's approver killed never runs again for this tenant.
    # Per account only; other tenants' rotations never see the kill.
    from . import tenant_brain
    killed = tenant_brain.killed_concepts(account_key)
    eligible, excluded = [], {}
    # Both sources: old-format physical files from the library folder AND all
    # v2 regen library concept definitions (which may be R2-only, not on disk).
    seen_bases = set()
    all_candidates = list(list_creatives(library_path)) + v2_library_concepts(library_path)
    for c in all_candidates:
        base = os.path.basename(c.path)
        if base in seen_bases:
            continue
        seen_bases.add(base)
        # Off-style exclusion is for pre-house-style era files only; v2 concepts
        # (lasso_v2_*) post-date style_exclusions.json and are never off-style.
        if not base.startswith("lasso_v2_") and base in off_style:
            excluded[base] = "off style (style exclusion list)"
            continue
        if base in used:
            excluded[base] = "already used (served or posted)"
            continue
        if base in killed or os.path.splitext(base)[0] in killed:
            excluded[base] = "killed by the approver (tenant brain)"
            continue
        if base.startswith("lasso_v2_") and os.path.splitext(base)[0].endswith("_story"):
            excluded[base] = "story variant (never a feed candidate)"
            continue
        if not rotation.is_gate_clean(getattr(c, "client_note", ""), approved_claims):
            excluded[base] = "fabrication gate (uncleared claim in the note)"
            continue
        from . import dam
        if dam.consent_blocked(c.path):
            excluded[base] = "consent blocked"
            continue
        eligible.append(c)
    return eligible, excluded


def eligible_creatives(account_key, library_path):
    """The assets runway may count: in-style, gate-clean, unposted."""
    return classify_creatives(account_key, library_path)[0]


def runway_days(account_key, library_path):
    return round(len(eligible_creatives(account_key, library_path)) / _posts_per_day(), 1)


def explain(account_key, library_path=None):
    """
    runway --account <key> --explain: the math in plain lines, READ ONLY.
    Eligible concepts by name, excluded counts with reasons summarized, the
    consumption assumption, and the resulting days from the SAME runway_days
    the digest reads. Output is dash free.
    """
    library_path = library_path or config.LIBRARY_PATH
    eligible, excluded = classify_creatives(account_key, library_path)
    per_day = _posts_per_day()
    days = runway_days(account_key, library_path)
    lines = [f"runway explain for {account_key}:",
             f"  eligible: {len(eligible)} creative(s)"]
    # Per-set breakdown: house (brand+service), b2b, platform, platform_ads, summit, library.
    _SET_MAP = {"brand": "house", "service": "house",
                "b2b": "b2b", "platform": "platform", "platform_ads": "platform_ads",
                "summit_campaign": "summit_campaign"}
    from .regen_library import CONCEPTS as _CONCEPTS
    set_counts: dict = {}
    for _c in eligible:
        _base = os.path.basename(_c.path)
        _stem = os.path.splitext(_base)[0]
        if _stem.startswith("lasso_v2_"):
            _name = _stem[len("lasso_v2_"):]
            _group = _SET_MAP.get(_CONCEPTS.get(_name, {}).get("set", ""), "other")
        else:
            _group = "library"
        set_counts[_group] = set_counts.get(_group, 0) + 1
    if set_counts:
        _parts = " / ".join(f"{v} {k}" for k, v in sorted(set_counts.items()))
        lines.append(f"  by set: {_parts}")
    for c in sorted(os.path.basename(c.path) for c in eligible):
        lines.append(f"    {c}")
    lines.append(f"  excluded: {len(excluded)} creative(s)")
    reasons = {}
    for reason in excluded.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    for reason in sorted(reasons):
        lines.append(f"    {reasons[reason]} x {reason}")
    posting_days = 7 - len(config.POSTING_SKIP_DAYS)
    lines.append(f"  consumption: {posting_days} posting day(s) per week = "
                 f"{per_day:.2f} post(s) per day")
    lines.append(f"  runway: {len(eligible)} / {per_day:.2f} = {days} day(s), "
                 "the same number the digest prints")
    out = "\n".join(lines)
    print(out)
    return {"eligible": [os.path.basename(c.path) for c in eligible],
            "excluded": excluded, "posts_per_day": per_day, "days": days,
            "text": out}


def _color(days, threshold):
    if days < threshold:
        return "RED"
    if days < threshold * 2:
        return "AMBER"
    return "GREEN"


def status_line(account_key, library_path, day_key):
    """The one-line runway status for the day's card thread."""
    threshold = int(os.environ.get("AGENT_RUNWAY_ALERT_DAYS", "7"))
    days = runway_days(account_key, library_path)
    zero = (date.fromisoformat(day_key) + timedelta(days=int(days))).isoformat()
    return (f"RUNWAY {account_key}: {days} days of approved content left "
            f"({_color(days, threshold)}). Projected zero: {zero}."), days


def _ask_text(account_key):
    """The specific ask, drafted from the approved source doc's own pillars."""
    doc = content_planner.load_source_doc()
    pillars = doc.pillars_with_copy() if doc is not None else []
    focus = f" for {pillars[0]}" if pillars else ""
    return (f"Runway is low for {account_key}. Please send raw material{focus}: "
            "three recent member photos or short clips with permission, and one "
            "member win in the member's own words with permission on record.")


def daily_runway(account_key, library_path, day_key, poster=None):
    """
    The daily runway pass for one account: post the status line, and below the
    threshold send ONE debounced ops alert (at most one per 7 days per account).
    Returns the line, or None while AGENT_RUNWAY_ENABLED is OFF.
    """
    if not config.runway_enabled():
        return None
    line, days = status_line(account_key, library_path, day_key)
    if poster is not None:
        poster.post_notice(line)
    threshold = int(os.environ.get("AGENT_RUNWAY_ALERT_DAYS", "7"))
    if days < threshold:
        last = db.kv_get(f"runway_alert_{account_key}", "")
        cutoff = (date.fromisoformat(day_key) - timedelta(days=7)).isoformat()
        if not last or last <= cutoff:
            ops_alerts.alert(_ask_text(account_key))
            db.kv_set(f"runway_alert_{account_key}", day_key)
    return line
