"""
client_infographic_fill.py — fill a client gym's EMPTY upcoming days with on-brand
INFOGRAPHIC posts built from its own APPROVED sources.

Blake's ruling (2026-08-25): "if they don't upload, scan their website and make an
infographic using echo!" A gym that stops uploading photos used to simply go dark
(the MEDIA-ONLY law held every photo-less day). This lane fills those gaps with a
nano (Gemini image) infographic card — the SAME creative system LASSO's own account
posts — grounded ONLY in the gym's approved client sources (which the intake pipeline
derives from its website + forms). Nothing is invented: the on-image headline and the
caption both trace to an approved source, the caption clears the full A+ gate, and
every row lands PENDING (the owner approves before anything publishes).

Scope guards:
  - Behind AGENT_CLIENT_INFOGRAPHIC_FILL (default OFF). OFF = days stay empty, the
    pre-2026-08-25 behavior.
  - INSERT-only: never deletes or replaces an existing row; a day that has ANY active
    feed row is never touched, so a real photo always wins over an infographic.
  - Capped per run (default 2) so a long-dark gym drips instead of flooding.
  - Requires the gym's voice doc + approved sources; missing either -> no-op (the
    stall alerts already cover those).
"""

import os
from datetime import date, timedelta

from . import config

FILL_DAYS_AHEAD = 7          # look this many days ahead for empty days
FILL_MAX_PER_RUN = 2         # cards per scan pass (drip, never flood)
_ARCHETYPES = ("flow", "split", "hero", "path", "headline")


def fill_enabled() -> bool:
    """AGENT_CLIENT_INFOGRAPHIC_FILL: fill photo-less upcoming days with approved-source
    infographic cards. Default OFF."""
    return (os.environ.get("AGENT_CLIENT_INFOGRAPHIC_FILL", "false") or "") \
        .strip().lower() in ("1", "true", "yes", "on")


def _headline_from(source):
    """A short, hard-rule-safe on-image headline drawn from the source text: the first
    clause, dash-scrubbed, trimmed to ~8 words. The caption carries the full words."""
    text = (getattr(source, "text", "") or "").strip()
    for stop in (".", "!", "?", ";", ":"):
        cut = text.find(stop)
        if 0 < cut < len(text):
            text = text[:cut]
            break
    words = text.split()
    return " ".join(words[:8]).strip()


def _empty_upcoming_days(store, base, tz_name, days_ahead, now=None):
    """Upcoming gym-local dates (tomorrow .. +days_ahead) with NO active IG feed row.
    'Active' excludes denied/killed/deleted, mirroring the grow-guard's counting."""
    from .calendar_autopublish import _local_now
    today = _local_now(now, tz_name).date()
    wanted = [(today + timedelta(days=i)).isoformat() for i in range(1, days_ahead + 1)]
    months = sorted({d[:7] for d in wanted})
    have = set()
    list_month = getattr(store, "list_month", None)
    if list_month is None:
        return []
    for month in months:
        try:
            rows = list_month(base, month) or []
        except Exception:  # noqa: BLE001 - unreadable calendar: fill nothing this pass
            return []
        for r in rows:
            if not isinstance(r, dict):
                continue
            if str(r.get("status") or "").lower() in ("denied", "killed", "deleted"):
                continue
            if str(r.get("format") or "").lower() == "feed" and \
                    str(r.get("account") or "").lower() in ("instagram", "ig", ""):
                have.add(str(r.get("post_date") or "")[:10])
    return [d for d in wanted if d not in have]


def fill_gaps(base, account, store, *, voice, logger=None, now=None,
              days_ahead=FILL_DAYS_AHEAD, max_per_run=FILL_MAX_PER_RUN):
    """Generate + insert up to max_per_run PENDING infographic feed posts for the gym's
    empty upcoming days. Returns a summary dict; never raises out of the scan."""
    log = logger or (lambda m: print(f"[infographic-fill] {m}"))
    if not fill_enabled():
        return {"ok": False, "reason": "flag off"}
    if not config.creative_studio_enabled():
        return {"ok": False, "reason": "creative studio off"}
    if voice is None:
        return {"ok": False, "reason": "no voice"}
    from . import client_sources, creative_studio, media_host, post_quality
    from . import client_content
    from .client_month_run import _to_rows
    from .drafter import Draft, DraftStatus

    sources = client_sources.approved_sources(f"{base}_ig") or []
    if not sources:
        return {"ok": False, "reason": "no sources"}
    tz_name = config.posting_timezone_for(base)
    gaps = _empty_upcoming_days(store, base, tz_name, days_ahead, now=now)
    if not gaps:
        return {"ok": True, "filled": 0, "gaps": 0}

    client = creative_studio._default_client()
    if client is None:
        return {"ok": False, "reason": "no image client"}

    filled = 0
    drafts = []
    for i, day in enumerate(gaps):
        if filled >= max_per_run:
            break
        # rotate source + archetype deterministically by date so re-runs are stable
        seed = sum(ord(c) for c in f"{base}{day}")
        source = sources[seed % len(sources)]
        archetype = _ARCHETYPES[seed % len(_ARCHETYPES)]
        headline = _headline_from(source)
        if not headline:
            continue
        try:
            prompt = creative_studio.build_prompt(
                headline, [getattr(source, "text", "") or ""],
                surface="feed", archetype=archetype)
        except Exception as e:  # noqa: BLE001 - a hard-rule miss skips the day
            log(f"{base} {day}: headline failed hard rules ({type(e).__name__}); skipped")
            continue
        img = creative_studio._render_with_timeout(
            lambda: client.generate_image(prompt=prompt, model=config.NANO_MODEL))
        if not img:
            log(f"{base} {day}: nano render failed; skipped")
            continue
        out = os.path.join(config.LIBRARY_PATH, base,
                           f"igfill_{day}_{archetype}.png")
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(img)
        except OSError as e:
            log(f"{base} {day}: could not write card: {type(e).__name__}")
            continue
        hosted = media_host.host_media(out, account.key)
        if not hosted:
            log(f"{base} {day}: hosting failed; skipped")
            continue
        caption, hashtags = client_content.make_caption(
            account, source, voice, f"igfill_{day}")
        draft = Draft(
            draft_id=f"igfill_{base}_{day}",
            account_key=account.key,
            platform=account.platform,
            caption=caption,
            hashtags=hashtags,
            creative_path=out,
            creative_public_url=hosted,
            scheduled_for=f"{day}T12:00:00",
            status=DraftStatus.PENDING,
            source_fragments=[getattr(source, "text", "") or "",
                              f"cite:{getattr(source, 'citation', '')}",
                              "infographic_fill"],
            day_key=day,
            category=getattr(source, "category", "") or "educational",
        )
        draft.is_story = False
        issues = post_quality.post_issues(draft)
        if issues:
            log(f"{base} {day}: infographic caption not A+ ({'; '.join(issues)}); skipped")
            continue
        drafts.append(draft)
        filled += 1

    if not drafts:
        return {"ok": True, "filled": 0, "gaps": len(gaps)}
    rows = _to_rows(base, drafts)
    clean = [{k: v for k, v in r.items() if k != "id"} for r in rows]
    try:
        inserted = len(store.insert_rows(base, clean) or [])
    except Exception as e:  # noqa: BLE001
        log(f"{base}: infographic insert failed: {type(e).__name__}")
        return {"ok": False, "reason": f"insert failed: {type(e).__name__}"}
    log(f"{base}: filled {filled} empty day(s) with approved-source infographic "
        f"card(s) ({inserted} pending row(s); {len(gaps)} gap(s) seen)")
    return {"ok": True, "filled": filled, "rows": inserted, "gaps": len(gaps)}
