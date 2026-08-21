"""
Client content: a client (non-LASSO) account drafts a full, varied month from its
OWN approved source docs (client_sources) paired with its uploaded library.

Behind AGENT_CLIENT_SOURCES (config.client_sources_enabled), OFF by default. When
OFF a client account behaves exactly as before (a library pick, or a blocked card
when the library is thin). When ON, this builder fills the daily slot: it spreads
across the account's categories (offer / service / testimonial / faq / about /
promo) the same way LASSO's doctrine spreads across pillars, pairs the day's fact
with an image from the account's uploaded library, and holds the draft for the tap.

Two laws are absolute here:
  1. The fabrication gate is the SOLE authority on claims. A client caption only
     ever states a fact present in THAT account's APPROVED sources (raw or its
     dash/vendor-cleaned form). A pending source never clears a claim. LASSO's
     global stats never clear a client's claim.
  2. Book and summit are LASSO-only and are never reached from here.

Thin-library grace (a caption-ready day with no image) lands in Part 4; Part 3
drafts only when the account has both an approved source for the day AND an image.
"""

import os
from datetime import date

from . import client_sources, config, media_host, rotation, schedule
from .content_categories import filter_platform_copy
from .drafter import (Draft, DraftStatus, _make_id, _pick_cta, _select_hashtags,
                      variant_hashtags)
from .library import list_creatives


def _day_ordinal(day_key):
    """A stable integer per calendar day, for deterministic category/source/image
    rotation that never drifts across re-runs."""
    return date.fromisoformat(str(day_key)[:10]).toordinal()


def _pillars_for(account_key, present=None):
    """The pillar-rotation list for an account: the categories it has approved content
    in, spread evenly. When AGENT_EDUCATIONAL_PILLAR is ON and the account has an
    educational-eligible source (its own 'educational' sources, or a reframeable
    service/about/faq), 'educational' is guaranteed EXACTLY ONCE in the rotation so it
    lands roughly 1-in-N days. Flag OFF => the list is exactly categories_present (today's
    behavior). Returns [] when the account has no approved sources at all."""
    present = present if present is not None \
        else client_sources.categories_present(account_key)
    pillars = list(present)
    if config.educational_pillar_enabled():
        eligible = client_sources.educational_source_for(account_key) is not None
        if eligible and "educational" not in pillars:
            pillars.append("educational")
    return pillars


def category_for_day(account_key, day_key, present=None):
    """The client category this day draws from, spread evenly across the account's
    pillar rotation (see _pillars_for). None when the account has no approved sources
    at all. Flag OFF => byte-for-byte the old spread over categories_present."""
    pillars = _pillars_for(account_key, present)
    if not pillars:
        return None
    return pillars[_day_ordinal(day_key) % len(pillars)]


def _source_for_day(account_key, day_key, category, present):
    """One approved source in the day's category, rotated across the days this
    category comes up so the same fact does not repeat back to back."""
    items = client_sources.approved_sources(account_key, category=category)
    if not items:
        return None
    cycle = _day_ordinal(day_key) // max(1, len(present))
    return items[cycle % len(items)]


def _image_key(creative):
    return os.path.basename(creative.path)


def pick_image(account_key, day_key, library_path, exclude_keys=(), pillar=None,
               allow_reuse=False):
    """A creative from the account's uploaded library.

    LEGACY (vision off): least-recently-served within the no-repeat window, cluster-keyed
    (dam.rotation_key collapses a near-dupe burst to one key; it falls back to the basename
    when the library is not clustered, so non-vision behavior is unchanged).

    VISION (AGENT_VISION_GYMS + a pillar, §4): IMAGES only (videos are coach hand-pick under
    vision); flagged/unanalyzed images are never auto-picked (guardrail 13); a cluster inside
    a per-platform reuse window is skipped (§3); the rest are scored on slot-job fit
    (vision.content_score) and the best is chosen deterministically. Below VISION_SCORE_FLOOR
    the best available is still returned but flagged `weak_match` for the coach (never silent,
    §4). None when nothing plannable remains.

    exclude_keys: creative basenames that must NOT be picked (photos already on the gym's
    approved/published rows + this build's placements).

    allow_reuse (denied-slot backfill only): when True, the §3 per-platform reuse window is
    IGNORED for the vision branch, so a photo still inside its reuse window is a valid pick.
    This is the ONE case a photo may be reused — replacing a human-denied slot for a gym at
    its creative cap. Default False = the reuse window is enforced exactly as before."""
    from . import dam
    imgs = [c for c in list_creatives(library_path)
            if c.media_type in ("image", "video")]
    excl = rotation.style_exclusions(library_path)
    imgs = [c for c in imgs if _image_key(c) not in excl]
    if exclude_keys:
        skip = {str(k) for k in exclude_keys}
        imgs = [c for c in imgs if _image_key(c) not in skip]
    if not imgs:
        return None
    served = rotation.load_served().get(account_key, [])
    last_served = {}
    for e in served:                       # oldest..newest, so newest date wins
        last_served[e["key"]] = e["date"]
    window_start = rotation._days_ago(day_key, config.ROTATION_WINDOW_DAYS)

    def _rkey(c):
        return dam.rotation_key(c.path)

    if config.vision_enabled_for(account_key) and pillar:
        from . import vision
        cands = []
        for c in imgs:
            if c.media_type != "image":
                continue                   # §2.1: videos are out of scope for vision auto-pick
            analysis = vision.stored_analysis(c.path)
            ok, _ = vision.auto_plannable(analysis)
            if not ok:
                continue                   # guardrail 13: flagged/unanalyzed never auto-planned
            rk = _rkey(c)
            if not allow_reuse and rotation.reuse_blocked(
                    rk, account_key, day_key, served={account_key: served}):
                continue                   # §3 per-platform reuse window (skipped on backfill)
            recency = 1.0 if last_served.get(rk, "") < window_start else 0.2
            score, ok_slot = vision.content_score(analysis, pillar, recency=recency)
            if ok_slot:
                cands.append((score, rk, _image_key(c), c))
        if not cands:
            return None
        cands.sort(key=lambda t: (-t[0], t[1], t[2]))   # high score, deterministic tie-break
        best_score, _rk, _name, best = cands[0]
        if best_score < vision.VISION_SCORE_FLOOR:
            try:
                best.weak_match = True     # planned but flagged for the coach (§4, never silent)
            except Exception:
                pass
        return best

    fresh = [c for c in imgs if last_served.get(_rkey(c), "") < window_start]
    pool = fresh if fresh else imgs
    pool.sort(key=lambda c: (last_served.get(_rkey(c), ""), _image_key(c)))
    legacy = pool[0]
    # §9.4 SHADOW: for a shadow (not enabled) gym, compute what vision WOULD pick and log the
    # diff, but SHIP the legacy pick unchanged. A plumbing smoke test, zero effect on posts.
    if pillar and config.vision_shadow_for(account_key):
        _shadow_log_pick(account_key, day_key, pillar, legacy, imgs,
                         last_served, window_start, served)
    return legacy


def _shadow_log_pick(account_key, day_key, pillar, legacy, imgs,
                     last_served, window_start, served):
    """§9.4: print the vision-vs-legacy pick for a shadow gym (best-effort, never raises,
    one line per account/day/pillar). Selection mirrors the vision branch above but its
    result is DISCARDED — the caller already shipped the legacy pick."""
    try:
        from . import dam, vision, db
        dkey = f"vision_shadow_logged_{account_key}_{day_key}_{pillar}"
        if db.kv_get(dkey):
            return
        best, best_score = None, -99.0
        for c in imgs:
            if c.media_type != "image":
                continue
            a = vision.stored_analysis(c.path)
            if not vision.auto_plannable(a)[0]:
                continue
            rk = dam.rotation_key(c.path)
            if rotation.reuse_blocked(rk, account_key, day_key, served={account_key: served}):
                continue
            recency = 1.0 if last_served.get(rk, "") < window_start else 0.2
            score, ok_slot = vision.content_score(a, pillar, recency=recency)
            if ok_slot and score > best_score:
                best, best_score = c, score
        v_name = os.path.basename(best.path) if best else None
        l_name = os.path.basename(legacy.path) if legacy else None
        print(f"[vision-shadow] {account_key} {day_key} {pillar}: "
              f"legacy={l_name} vision_would={v_name} "
              f"({'DIFFERENT' if v_name != l_name else 'same'}, score={best_score:.1f})")
        db.kv_set(dkey, "1")
    except Exception:  # noqa: BLE001 - shadow logging must never affect a build
        pass


def _humanize_stem(stem):
    """A human-readable phrase from a media filename stem, used as a photo-grounding
    HINT only (never a source of facts). Strips the intake timestamp prefix
    (20260812T181128Z_), splits on separators, drops pure-noise tokens (hex hashes,
    IMG_1234, bare numbers, extensions), and returns the descriptive words a client
    put in the filename ('Dale_Peace_Run' -> 'Dale Peace Run', 'Youth_Wall_Sit_w_
    smiles' -> 'Youth Wall Sit w smiles'). Returns '' when nothing descriptive remains
    (e.g. a UUID/hash-named upload), so a meaningless filename adds no noise."""
    import re
    s = os.path.splitext(str(stem or ""))[0]
    # drop the intake timestamp prefix: 20260812T181128Z_
    s = re.sub(r"^\d{8}T\d{6}Z_", "", s)
    words = []
    for tok in re.split(r"[\s._\-]+", s):
        tok = tok.strip()
        if not tok:
            continue
        low = tok.lower()
        if low in ("img", "image", "photo", "video", "vid", "final", "original",
                   "edit", "copy"):
            continue
        if tok.isdigit():                       # bare counter like 1946
            continue
        if re.fullmatch(r"[0-9a-f]{8,}", low):  # hex hash / uuid chunk
            continue
        if re.fullmatch(r"[A-Za-z0-9]{16,}", tok) and any(ch.isdigit() for ch in tok):
            continue                            # long mixed alnum id
        words.append(tok)
    return " ".join(words).strip()


def photo_grounding(creative):
    """Real, client-provided signals about WHAT THIS PHOTO/VIDEO SHOWS, so the caption
    can reference the actual shot instead of talking past it (Dale: 'the copy isn't
    quite matching the actual photo or video'). Two signals, both already present, both
    non-fabricated:

      * the picked creative's OWN sidecar note (<file>.json "note" / <file>.txt), the
        client's own words about the shot ('Youth fitness fun with smiles');
      * a humanized filename hint ('Dale_Peace_Run' -> 'Dale Peace Run').

    Returns '' when neither signal is descriptive (a hash-named upload with no note), so
    a photo with no real signal changes nothing. This is a HINT for grounding only: the
    figure-fabrication gate, banned-word gate, and no-dash law still run on the output,
    and the caption's CLAIMS still come only from the approved source."""
    if creative is None:
        return ""
    note = (getattr(creative, "client_note", "") or "").strip()
    stem = getattr(creative, "stem", None)
    if stem is None:
        stem = os.path.basename(getattr(creative, "path", "") or "")
    hint = _humanize_stem(stem)
    parts = []
    if note:
        parts.append(note)
    # only add the filename hint when it adds something the note doesn't already say
    if hint and hint.lower() not in " ".join(parts).lower():
        parts.append(hint)
    return "; ".join(p for p in parts if p).strip()


class _SourceCreative:
    """Adapter so the SB7 caption generator (which keys off a creative's client_note +
    stem) can run on a source-driven client draft: the day's approved fact IS the note
    SB7 writes a real StoryBrand caption around, grounded in the gym's voice doc.

    photo_hint (optional): real client-provided signals about what the PICKED photo/video
    actually shows (its sidecar note + humanized filename). When present it is appended
    to the note SB7 sees, tagged as a scene hint, so the copy references the shot instead
    of talking about an unrelated topic. It NEVER carries claims (the figure gate still
    runs on the output); it only steers the caption toward the visible subject."""

    def __init__(self, source, stem, photo_hint=""):
        note = getattr(source, "text", "") or ""
        photo_hint = (photo_hint or "").strip()
        if photo_hint:
            note = (note.rstrip()
                    + "\n\nWHAT THIS POST'S PHOTO/VIDEO SHOWS (reference this so the "
                      "caption matches the image; it is a scene hint, NOT a source of "
                      "facts, numbers, offers, or names to state): " + photo_hint)
        self.client_note = note
        self.stem = stem
        self.path = stem


def _grounded_hint(base_hint, verified):
    """§4: fold the CROP-VERIFIED elements into the SB7 scene hint so the caption is written
    from what actually shipped — with count honesty. Steering only; the post_quality grounding
    gate is the hard enforcement (enforced twice, §10)."""
    bucket = verified.get("bucket") or "unknown"
    details = verified.get("verified_details") or []
    parts = [base_hint] if base_hint else []
    parts.append(f"VERIFIED IN THE IMAGE: a {bucket}"
                 + (f"; visible: {', '.join(details)}" if details else "")
                 + ". Write from these. Do NOT call it a crowd/packed unless the grouping is "
                   "crowd; do NOT claim one-on-one unless it is solo/pair. Name no one; no "
                   "gender/age/body; invent no numbers or objects not listed.")
    return "\n".join(p for p in parts if p)


def make_caption(account, source, voice, creative_key, creative=None,
                 avoid_openings=(), verified=None, angle="", avoid_angles=()):
    """The day's caption + hashtags. When AGENT_SB7_ENABLED, write a real StoryBrand
    caption via the SB7 generator (problem-first, gym-as-guide, grounded ONLY in the
    gym's voice doc + this source, fabrication-gated on figures) instead of dumping the
    raw one-line source. Any failure or a blank result falls back to compose_caption
    (the deterministic source+CTA baseline), so a caption is always produced.

    This is the fix for a client post whose caption was just the raw intake word (e.g.
    'HYROX'): the same SB7 engine LASSO uses now writes every client caption too.

    ALIGNMENT (Dale, 2026-08-15): `creative` is the actual picked photo/video. Its own
    sidecar note + filename hint are passed to SB7 as a SCENE HINT so the copy references
    what is actually in the shot (a youth photo gets a youth-shaped caption), instead of
    the caption talking about a rotated source topic unrelated to the image. Grounding
    only, never fabrication: the figure gate, banned-word gate, and no-dash law all still
    run on the output, and the caption's CLAIMS still come only from the approved source.

    OPENING VARIETY (Ryan Parr, 2026-08-17): `avoid_openings` is the set of opening
    phrases used on recent planned days. It is passed to the SB7 generator as STYLE-only
    guidance so consecutive days stop leading with the same hook. It never carries a
    fact and never blocks a post; the baseline fallback ignores it (it only ever emits
    the verbatim approved source, which the A+ / banned-word gates already govern).

    ANGLE ROTATION (Bryan/Pierce, 2026-08, AGENT_CAPTION_ANGLE_ROTATION): `angle` is the
    SB7 problem/entry angle this day should LEAD from (or the special 'educational' post
    type); `avoid_angles` are the recent angles to steer away from. Both are STYLE-only
    (never a fact, never an override of the approved source) and are handed straight to
    the SB7 generator; the baseline fallback ignores them. Empty (the default / flag OFF)
    => no angle guidance, exactly today's behavior."""
    if config.sb7_enabled():
        try:
            from .drafter import StoryBrandGenerator
            hint = photo_grounding(creative)
            if verified and verified.get("ok"):
                hint = _grounded_hint(hint, verified)
            cap, tags, _frags = StoryBrandGenerator().build(
                voice, _SourceCreative(source, creative_key, photo_hint=hint),
                account=account, avoid_openings=avoid_openings,
                angle=angle, avoid_angles=avoid_angles)
            cap = (cap or "").strip()
            if cap and cap.lower() != (getattr(source, "text", "") or "").strip().lower():
                return filter_platform_copy(cap).strip(), tags
        except Exception as exc:  # noqa: BLE001 - never block on the LLM
            print(f"[client-caption] SB7 failed for {account.key} "
                  f"({type(exc).__name__}); using the baseline")
    return compose_caption(account, source, voice, creative_key)


def compose_caption(account, source, voice, creative_key):
    """Caption from the approved fact (dash/vendor cleaned) + one CTA from the
    account's approved voice doc. Returns (caption, hashtags). The claim content
    is unchanged by cleaning; cleaning only enforces the copy law."""
    body = filter_platform_copy(source.text).strip()
    cta = _pick_cta(voice, _CtaKey(creative_key))
    caption = body
    if cta:
        cta = filter_platform_copy(cta).strip()
        if cta and cta.lower() not in caption.lower():
            caption = (body + "\n\n" + cta).strip()
    hashtags = variant_hashtags(account.platform,
                                _select_hashtags(voice, _CtaKey(creative_key)))
    return caption, hashtags


class _CtaKey:
    """Minimal stand-in so drafter's CTA/hashtag rotation (which keys off a
    creative's stem) works for a source-driven draft."""

    def __init__(self, stem):
        self.stem = stem
        self.path = stem


def _alert_needs_media(account_key, day_key, category):
    """One ops alert per account per day when a caption is ready but no image is
    available. Deduped so a re-run never storms the channel."""
    from . import db, ops_alerts
    key = f"needs_media_alerted_{account_key}_{day_key}"
    if db.kv_get(key):
        return
    db.kv_set(key, "1")
    ops_alerts.alert(
        f"{account_key} {day_key}: caption ready ({category}) but the library has "
        "no image. Held as needs-media; add a photo to publish. Not blocked.")
    db.audit("client_needs_media", account_key,
             f"{category}: caption ready, no image", account_key, day_key)


def classify(draft):
    """The day's state for a client draft: 'ready' (caption + creative, held for
    the tap), 'needs-media' (caption ready, no image yet), or 'blocked' (nothing
    to say and nothing to show)."""
    if draft is None or draft.status == DraftStatus.BLOCKED:
        return "blocked"
    if getattr(draft, "needs_media", False):
        return "needs-media"
    return "ready"


def build_client_draft(account, day_key, voice, library_path, poster=None,
                       s3_client=None, template_fn=None, exclude_keys=(),
                       avoid_openings=(), allow_reuse=False,
                       angle="", avoid_angles=()):
    """
    The day's client draft, sourced from the account's approved sources + library.
    Returns None only when the client-sources flag is off, the voice doc is
    missing, or the account has no approved source for the day (the caller then
    falls back to the library pick, which blocks with a clear reason when the
    library is also empty — so a day is blocked ONLY when there is neither
    approved text nor a usable creative).

    Thin-library grace: when the account HAS an approved source for the day but
    NO image, the day is still caption-ready. If a source-backed template card can
    be produced (template_fn wired + generation armed) it fills the slot; otherwise
    the draft is held as needs-media with one ops alert. Never a hard blocked card.

    Never fabricates: the caption's fact comes verbatim from one approved source
    and is re-checked against the fabrication gate before it can ship.

    avoid_openings (Ryan Parr, 2026-08-17): opening phrases used on recent planned
    days, threaded to the caption generator so consecutive days do not lead with the
    same hook. STYLE-only, never a fact, never a block.

    angle / avoid_angles (Bryan/Pierce, 2026-08, AGENT_CAPTION_ANGLE_ROTATION): the SB7
    problem/entry angle this day should LEAD from and the recent angles to avoid; STYLE-
    only, threaded straight to the SB7 generator (never a fact, never a block). Empty
    (flag OFF) => today's behavior.

    EDUCATIONAL pillar (Bryan, AGENT_EDUCATIONAL_PILLAR): when the day's rotated pillar is
    'educational', the source is resolved from the gym's APPROVED educational material
    (its own 'educational' sources, else a reframeable service/about/faq source) and the
    SB7 caption is written with the 'educational' angle (TEACH one true point). If nothing
    is eligible, the day returns None and is SKIPPED — never a fabricated educational fact.
    """
    if not config.client_sources_enabled():
        return None
    if voice is None:
        return None
    present = client_sources.categories_present(account.key)
    if not present:
        return None                        # no approved sources: caller falls back
    # The rotation list (may inject 'educational' when the pillar flag is armed + eligible),
    # used for BOTH the day's pillar and the source-rotation cycle math so they stay aligned.
    pillars = _pillars_for(account.key, present)
    category = category_for_day(account.key, day_key, present)
    if category == "educational":
        # EDUCATIONAL source resolution: the gym's own approved 'educational' sources, else
        # a reframed approved service/about/faq source (facts stay verbatim). None -> SKIP
        # the day (the fabrication gate is never bypassed for an educational post).
        source = client_sources.educational_source_for(account.key, day_key)
        # Lead the SB7 caption from the educational angle unless the caller forced one.
        if not (angle or "").strip():
            angle = "educational"
    else:
        source = _source_for_day(account.key, day_key, category, pillars)
    if source is None:
        return None
    # Fabrication gate: the fact must be an approved claim for THIS account. It is,
    # by construction (it is an approved source), but we never skip the check. This runs
    # identically for an educational source (its text is an approved-source claim), so an
    # educational post can never smuggle in an unverified fact.
    claims = client_sources.approved_claims(account.key)
    if not rotation.is_gate_clean(source.text, approved_claims=claims):
        return None

    scheduled_for = schedule.scheduled_for(day_key)
    fragments = [source.text, f"cite:{source.citation}"]

    from . import dam
    # §4: pass the day's pillar so vision content-scores the pick to the slot job (a no-op
    # for non-vision gyms, which keep least-recently-served rotation).
    image = pick_image(account.key, day_key, library_path,
                       exclude_keys=exclude_keys, pillar=category,
                       allow_reuse=allow_reuse)
    if image is not None:
        # §3.5 CROP-VERIFY (vision gyms): re-check the SHIPPED pixels (IG/FB = the original,
        # ruling 4) before drafting, so the caption may lean only on details that survived.
        # Verify-then-draft, never draft-then-verify. Best-effort: a verify failure degrades
        # to safe generality, never blocks the day.
        verified = grounding = None
        if config.vision_enabled_for(account.key):
            from . import vision, dam
            analysis = vision.stored_analysis(image.path)
            try:
                with open(image.path, "rb") as _fh:
                    _img_bytes = _fh.read()
            except OSError:
                _img_bytes = b""
            verified = vision.crop_verify(_img_bytes, analysis)
            # §8: the gym's own context unlocks an identity/number claim ONLY with the consent
            # checkbox AND after the context clears the platform-policy + hard-fail screen.
            side = dam.read_sidecar(image.path)
            ctx = side.get("client_context", "") or ""
            consent = str(side.get("consent", "")).lower() == "granted"
            ctx_ok, _ctx_reasons = vision.context_usable(ctx)
            if not ctx_ok:
                ctx = ""                  # policy-violating context is never used (coach handles)
            grounding = {"analysis": analysis, "verified": verified, "claims": claims,
                         "consent": consent, "client_context": ctx}
        # Pass the ACTUAL picked creative so the caption is grounded in what the photo/
        # video shows (its sidecar note + filename + crop-verified elements).
        caption, hashtags = make_caption(account, source, voice,
                                         _image_key(image), creative=image,
                                         avoid_openings=avoid_openings, verified=verified,
                                         angle=angle, avoid_angles=avoid_angles)
        public_url = getattr(image, "public_url", "")
        if config.hosting_enabled():
            hosted = media_host.host_media(image.path, account.key)
            if hosted:
                public_url = hosted
        # Record on the CLUSTER key (dam.rotation_key; basename when unclustered) so the
        # per-platform reuse windows + no-repeat window see a near-dupe burst as one asset.
        rotation.record_served(account.key, dam.rotation_key(image.path), category, day_key)
        draft = Draft(
            draft_id=_make_id(account.key, image.path, scheduled_for),
            account_key=account.key,
            platform=account.platform,
            caption=caption,
            hashtags=hashtags,
            creative_path=image.path,
            creative_public_url=public_url,
            scheduled_for=scheduled_for,
            status=DraftStatus.PENDING,
            source_fragments=fragments,
            day_key=day_key,
            category=category,
        )
        # §4 weak_match: no image cleared the score floor -> the best available was planned
        # and flagged for the coach. Never silent: carry it on the draft + log it.
        if getattr(image, "weak_match", False):
            try:
                draft.weak_match = True
            except Exception:
                pass
            print(f"[vision] weak_match pick for {account.key} {day_key} "
                  f"(pillar {category}) -> coach review")
        # §5: carry the grounding context so the A+ gate can reject a caption that
        # CONTRADICTS the crop-verified image (a contradiction is not A+ -> the month
        # builder walks alternatives = the §7 regen/swap; exhausted -> the day drops).
        if grounding is not None:
            try:
                draft.grounding = grounding
            except Exception:
                pass
        return draft

    # THIN-LIBRARY GRACE: caption is ready, but there is no image.
    caption, hashtags = make_caption(account, source, voice, f"src_{source.id}",
                                     avoid_openings=avoid_openings,
                                     angle=angle, avoid_angles=avoid_angles)
    # Option A: a source-backed template card, when a generator is wired + armed.
    template_url = template_fn(account, source, day_key) if template_fn else None
    if template_url:
        return Draft(
            draft_id=_make_id(account.key, f"tmpl_{source.id}", scheduled_for),
            account_key=account.key,
            platform=account.platform,
            caption=caption,
            hashtags=hashtags,
            creative_path="",
            creative_public_url=template_url,
            scheduled_for=scheduled_for,
            status=DraftStatus.PENDING,
            source_fragments=fragments + ["template_card"],
            day_key=day_key,
            category=category,
        )
    # Option B: mark the day needs-media (held, one ops alert). NOT blocked.
    _alert_needs_media(account.key, day_key, category)
    return Draft(
        draft_id=_make_id(account.key, f"needsmedia_{source.id}", scheduled_for),
        account_key=account.key,
        platform=account.platform,
        caption=caption,
        hashtags=hashtags,
        creative_path="",
        creative_public_url="",
        scheduled_for=scheduled_for,
        status=DraftStatus.PENDING,
        source_fragments=fragments,
        day_key=day_key,
        category=category,
        needs_media=True,
        warnings=["needs-media: caption ready, add an image to publish"],
    )
