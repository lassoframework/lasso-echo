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
import uuid
from datetime import date, timedelta

from . import config

FILL_DAYS_AHEAD = 2          # look this many days ahead for empty days
FILL_MAX_PER_RUN = 2         # cards per scan pass (drip, never flood)


def _logical_post_ids_enabled():
    """Read the forward-only identity flag; old deployments default to OFF."""
    try:
        enabled = getattr(config, "logical_post_id_enabled", None)
        return bool(enabled()) if callable(enabled) else False
    except Exception:  # noqa: BLE001 - flag uncertainty never changes legacy behavior
        return False


def _ensure_logical_post_id(draft):
    """Stamp a newly generated standalone feed draft when the feature is armed.

    Preserve valid identity on retry of the same draft object. Invalid existing
    identity or an assignment failure returns False so the caller holds it.
    """
    if not _logical_post_ids_enabled():
        return True
    existing = getattr(draft, "logical_post_id", None)
    # None or "" (the Draft dataclass default on mirror-built drafts) is ABSENT
    # identity, not a malformed one: mint below. A NON-EMPTY invalid value still
    # fails closed so the caller holds the draft.
    if existing:
        try:
            uuid.UUID(str(existing))
        except (ValueError, TypeError, AttributeError):
            return False
        return True
    try:
        draft.logical_post_id = str(uuid.uuid4())
        uuid.UUID(draft.logical_post_id)
        return True
    except Exception:  # noqa: BLE001 - never stage an unkeyed generated post
        return False

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


# PHOTO-FIRST MEDIA ELIGIBILITY (2026-10-06): the Astra infographic fallback is
# a LAST RESORT behind eligible same-gym photos. The depletion question therefore
# has THREE answers, not two:
#   * MEDIA_AVAILABLE  -- a usable same-gym photo/video is proven to exist; the
#     fallback must not run.
#   * MEDIA_DEPLETED   -- every inventory read succeeded and PROVED no usable
#     same-gym media remains; only then may the fallback be considered.
#   * MEDIA_UNCERTAIN  -- inventory is incomplete or repeat-use provenance is
#     unknown (unreadable library, unavailable index, no completed sync, a photo
#     awaiting moderation, a claim read failure, or a usable asset excluded only
#     because its media_source linkage is stale/unverifiable). Never claim photos
#     are exhausted: hold the fallback explicitly.
# A stale source_id is NEVER repeat evidence by itself: only the gym's own
# proven use counters (used_count / last_used_at and same-byte aliases) mark an
# asset used.
MEDIA_AVAILABLE = "available"
MEDIA_DEPLETED = "depleted"
MEDIA_UNCERTAIN = "uncertain"


def real_media_status(base, *, now=None):
    """(status, detail) for Echo's photo-first depletion question.

    status is MEDIA_AVAILABLE / MEDIA_DEPLETED / MEDIA_UNCERTAIN as documented
    above; detail is a short human/ops-legible reason. An eligible same-gym
    photo always wins over the Astra infographic fallback; an incomplete
    inventory or unknown repeat-use provenance is an explicit UNCERTAIN hold,
    never a claim that photos are exhausted.

    The Drive selector remains the inventory contract: its pickable set already
    applies eligibility, coach-hide, claim, proven-repeat and cross-gym source
    rules. This classifier only interprets the reads -- it never marks repeats
    and never mutates state.
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
        return (MEDIA_UNCERTAIN, "local media library unreadable")
    try:
        served = rotation.load_served().get(f"{base}_ig", [])
        used = {str(row.get("key")) for row in served}
        local_creatives = []
        for creative in list_creatives(library_path):
            if usable_local_creative(creative, f"{base}_ig", used=used):
                local_creatives.append(creative)
        # Selection and depletion must use the same global exact-byte view.
        # With PR235's flag on, an unavailable / ambiguous ledger is not proof
        # that local supply is exhausted, so the outer fail-closed handler keeps
        # the Astra fallback held.  Flag OFF returns this same list unchanged.
        from .client_content import _global_photo_paths
        photo_paths = tuple(path for creative in local_creatives
                            if creative.media_type != "video"
                            for path in _global_photo_paths(creative))
        available = rotation.globally_available_local_paths(f"{base}_ig", photo_paths)
        local = [creative.path for creative in local_creatives
                 if creative.media_type == "video" or (
                     (paths := _global_photo_paths(creative))
                     and all(path in available for path in paths))]
        from .media_bridge import observe_local_inventory
        observe_local_inventory(base, local)
    except Exception as exc:  # noqa: BLE001
        return (MEDIA_UNCERTAIN,
                f"local media inventory check failed ({type(exc).__name__})")
    if local:
        return (MEDIA_AVAILABLE, "eligible same-gym local media exists")

    # The indexed Drive inventory is authoritative even when the staging lane is
    # currently disabled.  A disabled writer must not make approved client media
    # look absent and unlock the infographic fallback.  If the index is
    # unavailable or unreadable, return UNCERTAIN below (fail closed).
    try:
        from . import gym_media_index, gym_media_selector
        media_store = gym_media_index.default_store()
        if not media_store.available():
            return (MEDIA_UNCERTAIN, "Drive media index unavailable")
        list_sources = getattr(media_store, "list_sources", None)
        if not callable(list_sources):
            return (MEDIA_UNCERTAIN, "Drive media index cannot prove source rows")
        sources = list_sources(base) or []
        # A successful authoritative source read with no Drive rows means this
        # gym has never connected Drive. There is no remote supply to wait for,
        # so an empty local library may use the verified-palette Astra fallback.
        # Exceptions still land in the outer fail-closed handler below.
        drive_sources = [s for s in sources
                         if str(s.get("kind") or "") == "gym_drive"]
        if not drive_sources:
            if config.gym_drive_connect_active_for(base):
                return (MEDIA_UNCERTAIN,
                        "Drive connect is active but no media_source rows exist yet")
            if local:
                return (MEDIA_AVAILABLE, "usable local media and no Drive supply")
            return (MEDIA_DEPLETED,
                    "no usable local media and this gym never connected Drive")
        ready = [s for s in drive_sources
                 if s.get("active") is not False
                 and not s.get("revoked_externally")
                 and str(s.get("sync_status") or "").lower() == "ready"
                 and s.get("sync_finished_at")]
        # An empty asset response is meaningful only after a successful sync.
        # Without that proof, an empty/stale DB must never unlock Astra fallback.
        if not ready:
            return (MEDIA_UNCERTAIN, "no completed Drive sync proves the inventory")
        # The selector normally converts claim errors to [] for planning; this
        # gate requests strict claim reads before interpreting [] as depletion.
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
            return (MEDIA_UNCERTAIN, "a client photo is awaiting moderation")
        class Snapshot:
            def available(self):
                return True
            def list_assets(self, gym):
                return assets
            # The cross-gym source guard (gym_media_selector.verified_source_ids)
            # needs the same source evidence this function already read above;
            # delegate so the depletion read keeps one consistent snapshot.
            def list_sources(self, gym, include_inactive=False):
                return media_store.list_sources(
                    gym, include_inactive=include_inactive)
        # pickable() expects a timezone-aware datetime (its `_now_utc` passes a
        # truthy `now` through untouched). Callers hand us ISO strings, so parse
        # first -- a TypeError here would be swallowed below as "inventory
        # uncertain" and the lane would hold forever.
        parsed_now = now
        from datetime import date as _date, datetime as _dt, timezone as _tz
        if isinstance(parsed_now, _date) and not isinstance(parsed_now, _dt):
            # no_media_astra_seed hands a plain date; pickable's cooldown math
            # compares against tz-aware datetimes.
            parsed_now = _dt(parsed_now.year, parsed_now.month, parsed_now.day,
                             tzinfo=_tz.utc)
        elif isinstance(parsed_now, str):
            try:
                parsed_now = _dt.fromisoformat(parsed_now.replace("Z", "+00:00"))
            except ValueError:
                parsed_now = None
            if (parsed_now is not None
                    and parsed_now.tzinfo is None):
                parsed_now = parsed_now.replace(tzinfo=_tz.utc)
        drive = gym_media_selector.pickable(
            base, store=Snapshot(), now=parsed_now, strict_claims=True)
        from .media_bridge import observe_drive_inventory
        observe_drive_inventory(base, [a.get("id") for a in drive])
        if local or drive:
            return (MEDIA_AVAILABLE,
                    "eligible same-gym media exists; photos win over the fallback")
        # An empty pickable set is only a DEPLETED proof when no exclusion rests
        # on unproven evidence. A usable asset kept out solely by a stale or
        # unverifiable media_source link, or by an unreadable claim set, makes
        # the inventory UNCERTAIN -- never a claim that photos are exhausted.
        detail = _unproven_empty_pool_detail(base, assets, Snapshot())
        if detail:
            return (MEDIA_UNCERTAIN, detail)
        return (MEDIA_DEPLETED,
                "verified Drive inventory and local library both prove no usable media")
    except Exception as exc:  # noqa: BLE001 - inventory uncertainty must never alert a client
        return (MEDIA_UNCERTAIN,
                f"inventory read failed ({type(exc).__name__})")


def _unproven_empty_pool_detail(base, assets, store):
    """Why an empty pickable set is NOT proof of exhaustion, or "".

    Runs only after a strict pickable read returned []. Re-derives each
    same-gym usable asset's exclusion reason: a proven repeat (this gym's own
    use counters / same-byte alias) or a clean global-ledger/scene exclusion
    stays out silently, but a stale/unverifiable media_source link, an
    in-flight claim, or any other unproven gate means the pool cannot be
    declared exhausted. A stale source_id is never repeat evidence on its own.
    """
    from . import gym_media_selector as gms
    try:
        source_ids = gms.verified_source_ids(store, base)
    except Exception:  # noqa: BLE001 - source evidence itself is unproven
        return "media_source evidence unreadable; photo supply not proven exhausted"
    used_hashes = set()
    uncertain_hashes = set()
    for alias in assets:
        if str(alias.get("gym_id") or "") != base:
            continue
        digest = gms._byte_hash(alias)
        if not digest:
            continue
        try:
            int(alias.get("used_count") or 0)
        except (TypeError, ValueError):
            uncertain_hashes.add(digest)
        else:
            if gms._has_prior_use(alias):
                used_hashes.add(digest)
    try:
        from . import db
        claimed_ids = set(db.drive_asset_claimed_ids(base) or ())
        claimed_hashes = gms._claimed_hashes(assets, claimed_ids, base)
    except Exception:  # noqa: BLE001 - unknown claims are unknown provenance
        return "in-flight claim evidence unreadable; photo supply not proven exhausted"
    for a in assets:
        if str(a.get("gym_id") or "") != base:
            continue
        if not gms.is_usable(a):
            continue
        try:
            int(a.get("used_count") or 0)
        except (TypeError, ValueError):
            # The selector fails closed and treats this row as used, but an
            # unreadable counter is UNKNOWN repeat-use provenance, not proof of
            # a repeat: it cannot support an exhaustion claim either.
            return ("an asset's use counters are unreadable; repeat-use "
                    "provenance unknown, photo supply not proven exhausted")
        if str(a.get("source_id") or "") not in source_ids:
            return ("a usable asset's media_source link is stale or unverifiable; "
                    "photo supply not proven exhausted")
        if gms._byte_hash(a) in uncertain_hashes:
            return ("a same-byte alias has unreadable use counters; repeat-use "
                    "provenance unknown, photo supply not proven exhausted")
        if gms._has_prior_use(a) or (gms._byte_hash(a)
                                     and gms._byte_hash(a) in used_hashes):
            continue            # proven repeat: the exclusion is justified
        if str(a.get("id")) in claimed_ids or (gms._byte_hash(a)
                                               and gms._byte_hash(a) in claimed_hashes):
            return ("usable media is reserved by an in-flight claim; "
                    "photo supply not proven exhausted")
        if gms.global_ledger_flag() or gms.scene_guard_flag():
            # A clean ledger/scene exclusion under an armed guard is proven;
            # uncertainty in those reads already raised under strict_claims.
            continue
        return ("a usable asset was excluded by an unproven gate; "
                "photo supply not proven exhausted")
    return ""


def real_media_depleted(base, *, now=None):
    """Legacy boolean view of :func:`real_media_status`.

    True ONLY on a proven MEDIA_DEPLETED; MEDIA_AVAILABLE and MEDIA_UNCERTAIN
    both read False so every existing caller stays fail-closed (an uncertain
    inventory never unlocks the Astra fallback). Callers that must SAY why the
    fallback is held should read real_media_status directly.
    """
    return real_media_status(base, now=now)[0] == MEDIA_DEPLETED


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


def _stamp_same_object_source(draft, hosted, log, day):
    """Global visual writer guard (AGENT_VISUAL_GLOBAL_WRITER_PREP) provenance.

    This lane renders the Astra card to ``out`` and ``media_host.host_media``
    uploads those exact bytes with no transformation, so the hosted URL IS the
    generated source object.  When the guard is armed the row must carry an
    explicit ``source_media_url`` equal to ``image_url`` (the same-object row
    contract in visual_writer_prepare.prepare); the writer's privileged
    boundary performs the byte proof.  Guard OFF: no stamp, so pre-migration
    inserts never carry the column and scheduling behavior is unchanged.

    Never infer a transformed source from a delivered URL: if this lane ever
    gains a rendition lane between render and host, that lane must instead
    hold or attach genuine owner-attested render evidence.  A stamp that
    cannot be retained holds the day rather than inserting an unproven row.
    Returns True when the draft is insertable under the current guard state.
    """
    from .client_month_run import _visual_writer_guard_enabled
    if not _visual_writer_guard_enabled():
        return True
    if not isinstance(hosted, str) or not hosted.strip():
        log(f"{day}: visual provenance requires the exact hosted source URL; held")
        return False
    try:
        draft.source_media_url = hosted
    except Exception:  # noqa: BLE001 - never silently lose provenance on an immutable draft
        log(f"{day}: visual provenance could not retain the hosted source; held")
        return False
    return True


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

    media_status, media_detail = real_media_status(base, now=now)
    if media_status == MEDIA_AVAILABLE:
        return {"ok": True, "filled": 0, "reason": "usable media available"}
    if media_status == MEDIA_UNCERTAIN:
        # Explicit hold: inventory incomplete or repeat-use provenance unknown.
        # Never claim photos are exhausted and never run the Astra fallback.
        reason = (f"media inventory uncertain — infographic fallback held "
                  f"({media_detail})")
        log(f"{base}: {reason}")
        return {"ok": False, "filled": 0, "held": True, "reason": reason}
    depleted = True
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
        generation_status, generation_detail = real_media_status(base, now=now)
        if generation_status == MEDIA_UNCERTAIN:
            reason = ("media inventory uncertain at generation time; "
                      f"infographic fallback held ({generation_detail})")
            log(f"{base}: {reason}")
            return {"ok": False, "filled": 0, "gaps": len(gaps),
                    "held": True, "reason": reason}
        if generation_status == MEDIA_AVAILABLE:
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
        if not _ensure_logical_post_id(draft):
            log(f"{base} {day}: logical post identity unavailable; holding infographic")
            continue
        draft.is_story = False
        if not _stamp_same_object_source(draft, hosted, log, f"{base} {day}"):
            continue
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
    pre_status, pre_detail = real_media_status(base, now=now)
    if pre_status != MEDIA_DEPLETED:
        if pre_status == MEDIA_AVAILABLE:
            log(f"{base}: usable approved photos available before insert; holding "
                "all infographic drafts")
            return {"ok": True, "filled": 0, "gaps": len(gaps),
                    "reason": "usable media available before insert"}
        log(f"{base}: media inventory uncertain before insert "
            f"({pre_detail}); holding all infographic drafts")
        return {"ok": False, "filled": 0, "gaps": len(gaps), "held": True,
                "reason": f"media inventory uncertain before insert "
                          f"({pre_detail})"}
    insertable_days = set(_empty_upcoming_days(
        store, base, tz_name, min(days_ahead, 2), now=now))
    drafts = [draft for draft in drafts if draft.day_key in insertable_days]
    if not drafts:
        log(f"{base}: infographic day taken before insert; holding drafts")
        return {"ok": True, "filled": 0, "gaps": len(gaps),
                "reason": "calendar day taken before insert"}
    # Convert each generated draft independently. _to_rows explicitly creates
    # the Instagram feed and its Facebook cross-post together; carrying the
    # draft's ID onto those rows preserves that known relationship without
    # matching unrelated rows by date, caption, or image.
    if not _logical_post_ids_enabled():
        # Preserve the legacy batch conversion exactly while the feature is off.
        rows = _to_rows(base, drafts)
    else:
        rows = []
        for draft in drafts:
            draft_rows = _to_rows(base, [draft])
            logical_post_id = getattr(draft, "logical_post_id", None)
            if not logical_post_id:
                log(f"{base}: logical post identity missing before insert; holding batch")
                return {"ok": False, "reason": "logical post identity unavailable",
                        "filled": 0, "gaps": len(gaps)}
            for row in draft_rows:
                row["logical_post_id"] = logical_post_id
            rows.extend(draft_rows)
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


def prepare_verified_infographic_candidate(request, snapshot, *, jobs, provider,
                                          reviewer, storage=None, enabled=False,
                                          now=None):
    """Prepare the autonomous fallback without legacy coach tags/calendar writes.

    The owner supplies current approved copy/palette/history and complete Drive
    inventory evidence. Existing photo selection is checked again before costly
    generation. Only the final owner transaction may reserve/stage this result.
    """
    from .generated_infographic_preparation import prepare_candidate
    if not enabled:
        return {"ok": False, "held": True, "reason": "generated_preparation_disabled"}
    if not isinstance(request, dict) or not request.get("gym_id"):
        return {"ok": False, "held": True, "reason": "generated_request_invalid"}
    state, _ = real_media_status(request["gym_id"], now=now)
    if state != MEDIA_DEPLETED:
        return {"ok": False, "held": True,
                "reason": "generated_photo_available" if state == MEDIA_AVAILABLE
                else "generated_photo_inventory_uncertain"}
    return prepare_candidate(request, snapshot, jobs=jobs, provider=provider,
                             reviewer=reviewer, storage=storage, enabled=True)
