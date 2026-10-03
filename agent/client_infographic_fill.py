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
import time
from datetime import date, timedelta

from . import config

FILL_DAYS_AHEAD = 2          # look this many days ahead for empty days
FILL_MAX_PER_RUN = 2         # cards per scan pass (drip, never flood)

# CLIENT-SAFE REVIEW MARK (2026-09-11, same requirement as no_media_astra_seed.py's
# NEEDS_CLIENT_SAFE_REVIEW_PILLAR): every row here is Echo's own scrape-grounded
# infographic, not a client-submitted photo -- a reviewer must be able to tell it
# apart from an ordinary pending draft at a glance. Unlike no_media_astra_seed's
# pillar (a throwaway label with no other meaning), THIS module's pillar carries
# real taxonomy (the source's own category: service/about/offer/educational/faq)
# that a human reviewer benefits from seeing, so the mark is a SUFFIX, not a
# replacement -- "offer" becomes "offer::needs_client_safe_review", still legible
# as "offer" at a glance, still a distinct string. Verified before choosing this:
# content_categories.GYM_PILLARS and day_shape.PROOF_PILLARS/INVITATION_PILLARS
# are the only fixed pillar lists in the codebase and neither is read by ANY
# other module (grepped 2026-09-11) -- there is no live rotation/day-shape logic
# for this suffix to disturb.
_NEEDS_CLIENT_SAFE_REVIEW_SUFFIX = "::needs_client_safe_review"


def _with_review_mark(category):
    base = (category or "educational").strip() or "educational"
    return f"{base}{_NEEDS_CLIENT_SAFE_REVIEW_SUFFIX}"
_ARCHETYPES = ("flow", "split", "hero", "path", "headline")


def real_media_depleted(base, *, now=None):
    """Return True only when Echo can confirm this gym has no usable real media.

    A calendar gap is deliberately not evidence of depletion: a gym may have uploads
    waiting for a later planner pass.  The alert lane is therefore fail-closed.  A
    usable file in the client library suppresses it; an unreadable library or an
    unavailable active Drive inventory also suppresses it rather than asking a client
    to upload media Echo may already have.

    When the Drive lane is active, its selector is the inventory contract.  Its
    pickable set already applies the eligibility, coach-hide, and reuse rules used by
    the planner, so an empty set means there is no Drive photo or video left for a
    new post right now.
    """
    from . import rotation
    from .library import list_creatives
    from .client_media_sync import usable_local_creative

    library_path = os.path.join(config.LIBRARY_PATH, base)
    try:
        names = os.listdir(library_path)
    except FileNotFoundError:
        names = []
    except OSError:
        return False
    try:
        served = rotation.load_served().get(f"{base}_ig", [])
        used = {str(row.get("key")) for row in served}
        local = []
        for creative in list_creatives(library_path):
            if usable_local_creative(creative, f"{base}_ig", used=used):
                local.append(creative.path)
        from .media_bridge import observe_local_inventory
        observe_local_inventory(base, local)
    except Exception:
        return False

    if not (config.gym_drive_stage_enabled()
            and config.gym_drive_connect_active_for(base)):
        return not local
    try:
        from . import gym_media_index, gym_media_selector
        media_store = gym_media_index.default_store()
        if not media_store.available():
            return False
        # pickable() intentionally converts store errors to [] for planning.
        # For a client depletion notice, distinguish a failed read from empty.
        assets = media_store.list_assets(base)
        # An indexed client photo awaiting the normal hash-bound moderation is
        # supply waiting for Echo, not evidence that the gym has no photos.
        # Hold the infographic while the moderation worker catches up. A known
        # rejected or coach-hidden asset cannot block the last-resort lane.
        if any(str(a.get("gym_id") or "") == base
               and a.get("kind") == "photo"
               and a.get("eligible") is not False
               and not a.get("excluded_by_coach")
               and a.get("review_status") == "pending_review"
               and a.get("moderation_status") == "pending"
               and a.get("content_hash") for a in assets):
            return False
        class Snapshot:
            def available(self):
                return True
            def list_assets(self, gym):
                return assets
        drive = gym_media_selector.pickable(base, store=Snapshot(), now=now)
        from .media_bridge import observe_drive_inventory
        observe_drive_inventory(base, [a.get("id") for a in drive])
        return not local and not drive
    except Exception:  # noqa: BLE001 - inventory uncertainty must never alert a client
        return False


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


def _generate_astra_only(prompt, opts, *, account_key, subject, draft_id,
                         sleep=None):
    """Astra, and ONLY Astra, for a client gym's last-resort infographic.

    Blake's global ruling (2026-10-02): an approved client photo always wins;
    a generated infographic is the LAST resort, and when it renders it must
    come through the ASTRA path -- there is no silent Gemini rung and no
    generic LASSO-branded fallback for a client gym. A total Astra failure
    marks the slot NEEDS HUMAN (same ops alert + audit row as the shared
    chain in image_engine.generate_image) and returns None so the caller
    holds the day instead of filling it with off-brand art. Never raises."""
    from . import image_engine as _ie
    key = os.environ.get(_ie.OPENAI_API_KEY_ENV, "")
    if not key:
        _ie.mark_needs_human(
            subject=subject, account_key=account_key,
            failures=("astra route required but no Astra API key is set",),
            draft_id=draft_id)
        return None
    engine = _ie.AstraImageEngine(key)
    tries = _ie._attempts_for(engine)
    sleep = sleep or time.sleep
    failures = []
    for attempt in range(1, tries + 1):
        try:
            result = engine.generate(prompt, opts)
        except Exception as exc:  # noqa: BLE001 - a provider bug may not kill the run
            from . import ops_alerts
            detail = ops_alerts.scrub(f"{type(exc).__name__}: {exc}")
            failures.append(f"astra attempt {attempt}/{tries}: {detail}")
        else:
            if result is not None and result.ok():
                _ie.record_cost(result.cost_estimate, account_key=account_key)
                return result
            failures.append(f"astra attempt {attempt}/{tries}: empty result")
        if attempt < tries:
            sleep(_ie.ASTRA_RETRY_BACKOFF_SECS * attempt)
    _ie.mark_needs_human(subject=subject, account_key=account_key,
                         failures=failures, draft_id=draft_id)
    return None


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

    depleted = real_media_depleted(base, now=now)
    if not depleted:
        return {"ok": True, "filled": 0, "reason": "usable media available"}
    from .media_bridge import bridge_days, episode, retry_existing_notice
    existing_notice = bool(episode(base, now=now, create=False))
    allowed_days = set(bridge_days(base, now=now, days_ahead=days_ahead))
    if existing_notice:
        retry_existing_notice(base, account, store, now=now, logger=log)

    tz_name = config.posting_timezone_for(base)
    gaps = [day for day in _empty_upcoming_days(
        store, base, tz_name, min(days_ahead, 2), now=now) if day in allowed_days]
    if not gaps:
        return {"ok": True, "filled": 0, "gaps": 0}
    if not existing_notice:
        from .media_bridge import notify_bridge
        notify_bridge(base, account, now=now, logger=log)
    sources = client_sources.approved_sources(f"{base}_ig") or []
    if not sources:
        return {"ok": False, "reason": "no sources"}

    # Blake's global ruling (2026-10-02): a generated infographic is a LAST
    # RESORT behind approved client photos, it MUST render through the Astra
    # path, and it MUST carry this gym's own VERIFIED brand colors. No
    # verified palette on file -> fail CLOSED: hold the fill and surface the
    # reason, never invent colors from the voice doc's tone and never reach
    # for LASSO's or a generic palette. LASSO's own account is exempt (its
    # locked V3 palette governs its cards).
    from . import astra_prompt as _ap
    account_base = _ap._account_base(account.key)
    if account_base != base:
        reason = (f"account/gym mismatch ({account_base!r} account for {base!r} gym); "
                  "infographic fallback held")
        log(reason)
        return {"ok": False, "reason": reason}
    gym_palette = None
    if not _ap.is_lasso_account(account.key):
        gym_palette = _ap.load_gym_brand_palette(account.key)
        if not gym_palette:
            reason = (f"no verified brand colors for {base} "
                      f"(expected {_ap._gym_brand_colors_path(base)}); "
                      "infographic fallback held")
            log(reason)
            return {"ok": False, "reason": reason}

    filled = 0
    drafts = []
    for i, day in enumerate(gaps):
        if filled >= max_per_run:
            break
        # GENERATION-TIME RECHECK (Blake 2026-10-02, photos FIRST): the
        # depletion and gap scans above ran before any rendering happened.
        # Re-verify BOTH right before this card is drawn so a photo that
        # landed (or a row another lane inserted) between the scan and now
        # always wins over an infographic.
        if not real_media_depleted(base, now=now):
            log(f"{base}: usable approved photos available at generation "
                "time; holding infographic fill (photos first)")
            break
        if day not in set(_empty_upcoming_days(
                store, base, tz_name, min(days_ahead, 2), now=now)):
            log(f"{base} {day}: day no longer empty at generation time; skipped")
            continue
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
        # ASTRA ROUTE ONLY (Blake's global ruling, 2026-10-02): this gym's
        # last-resort infographic renders through Astra with its OWN verified
        # brand palette threaded explicitly (gym_palette), or it does not
        # render at all. There is NO Gemini rung and NO generic LASSO-branded
        # fallback here; a failed Astra chain marks the slot NEEDS HUMAN (ops
        # alert + audit row, same contract as image_engine.generate_image)
        # and the day stays empty for a human.
        #
        # The brief is built directly (not via creative_studio._astra_brief_for,
        # which cannot thread a verified palette): this gym's OWN voice doc
        # (astra_prompt._voice_path_for) and its OWN verified colors
        # (astra_prompt.gym_brand_palette_section) instead of LASSO's.
        try:
            astra_brief = _ap.build_infographic_brief(
                headline, [getattr(source, "text", "") or ""],
                surface="feed post", account_key=account.key,
                gym_palette=gym_palette)
        except Exception as e:  # noqa: BLE001 - a brief we cannot build is a held day
            log(f"{base} {day}: Astra brief could not be built "
                f"({type(e).__name__}); held (no generic fallback)")
            continue
        draft_id = f"igfill_{base}_{day}"
        from . import image_engine as _ie
        _res = _generate_astra_only(
            astra_brief,
            {"kind": "infographic", "surface": "feed post",
             "has_text_overlay": bool(str(headline or "").strip()),
             "require_astra": True},
            account_key=account.key,
            subject=f"{day} {headline}"[:120], draft_id=draft_id)
        img = _res.image_bytes if _res is not None else None
        if not img:
            log(f"{base} {day}: image render failed on every engine; "
                "marked NEEDS HUMAN and skipped")
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
            draft_id=draft_id,
            account_key=account.key,
            platform=account.platform,
            caption=caption,
            hashtags=hashtags,
            creative_path=out,
            creative_public_url=hosted,
            scheduled_for=f"{day}T12:00:00",
            status=DraftStatus.PENDING,
            infographic_copy={},
            source_fragments=[getattr(source, "text", "") or "",
                              f"cite:{getattr(source, 'citation', '')}",
                              "infographic_fill"],
            day_key=day,
            category=_with_review_mark(getattr(source, "category", "")),
            image_engine=f"{_res.engine}:{_res.model}" if _res is not None else "",
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
    # The render and upload can take long enough for a real client photo or a
    # competing calendar row to arrive. Recheck immediately before the write:
    # no generated fallback may be inserted once either condition is known.
    # The store does not expose an atomic conditional insert, so this is the
    # final best-effort guard; its check-to-insert interval remains necessarily
    # subject to a concurrent writer and must stay fail-closed at the store
    # boundary when that capability is added.
    if not real_media_depleted(base, now=now):
        log(f"{base}: usable approved photos available before insert; holding "
            "all infographic drafts")
        return {"ok": True, "filled": 0, "gaps": len(gaps),
                "reason": "usable media available before insert"}
    insertable_days = set(_empty_upcoming_days(
        store, base, tz_name, min(days_ahead, 2), now=now))
    drafts = [draft for draft in drafts if draft.day_key in insertable_days]
    if not drafts:
        log(f"{base}: infographic day taken before insert; holding drafts")
        return {"ok": True, "filled": 0, "gaps": len(gaps),
                "reason": "calendar day taken before insert"}
    rows = _to_rows(base, drafts)
    clean = [{k: v for k, v in r.items() if k != "id"} for r in rows]
    try:
        inserted = len(store.insert_rows(base, clean) or [])
    except Exception as e:  # noqa: BLE001
        log(f"{base}: infographic insert failed: {type(e).__name__}")
        return {"ok": False, "reason": f"insert failed: {type(e).__name__}"}
    log(f"{base}: filled {filled} empty day(s) with approved-source infographic "
        f"card(s) ({inserted} pending row(s); {len(gaps)} gap(s) seen)")
    if inserted and depleted:
        from .media_bridge import notify_bridge
        notify_bridge(base, account, logger=log)
    return {"ok": True, "filled": filled, "rows": inserted, "gaps": len(gaps)}
