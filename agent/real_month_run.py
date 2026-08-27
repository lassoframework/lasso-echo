"""
real_month_run.py — wire the REAL category builders into the pure month planner and
run a full month for a LASSO account.

real_month_planner is pure and injectable: it sequences builders it is handed, it never
imports them. This module is the thin, IMPURE seam that:

  1. real_builders_map(account) -> {category: builder(account_or_key, day_key) -> Draft|None}
     maps each LASSO pillar to its EXISTING approved builder (the same ones the daily
     runner chains), so the month plan is built from the real content sources, never
     invented:
        podcast  -> podcast_month.build_month_podcast_draft (the month-ahead episode
                    topic pool; the daily release slot only ever cards the newest
                    episode once, so it cannot fill a month)
        platform -> daily_studio.build_daily_infographic_draft (the four platform PDFs
                    via the content brain / doctrine)
        b2b      -> rotation.build_rotated_draft (regen_library gym-owner concepts)
        doctrine -> daily_studio.build_daily_infographic_draft (lasso_now.md pillars)
        summit   -> summit.build_summit_draft (the Growth Playbook, campaign window only)
        book     -> book_queue.build_book_queue_draft (the real dated launch posts)
        welcome  -> welcome_queue.build_welcome_queue_draft (a real queued new client)
     Each real builder is gated by its OWN flag and returns None when dark; the planner
     then falls back to the next real pillar, so an unarmed pillar never leaves a blank.

  2. plan_and_build(account_key, start_date, days) -> the real Drafts for the full span
     (feed + paired 9:16 story per day, every day filled when any real pillar can build),
     behind AGENT_REAL_MONTH_PLAN. Flag OFF -> [] and NOTHING is invoked (byte-for-byte
     today's behavior). The story builder is stories.build_story_draft (the genuine-9:16
     guard), NEVER a cropped feed.

Nothing here publishes or hosts beyond whatever the injected real builders do; it writes
no calendar rows (apply is real_month_planner.apply_month_plan, called separately behind
the same flag). Every draft is PENDING, held for the tap.
"""

from . import config, real_month_planner as _rmp


def real_builders_map(account):
    """The category -> feed-builder map for `account`, wrapping the EXISTING approved
    builders. Every wrapped builder takes (target, day_key) and returns Draft|None; the
    planner passes account=None as the target and the wrappers ignore it and close over
    `account` so a builder that needs the Account object (all of them) gets it.

    No fabrication: each builder draws only from its approved source and returns None when
    it cannot (flag off, source missing, studio/hosting dark, nothing queued). Book and
    welcome are included because they take their real dated days via the plan override;
    they are NOT in the planner's gap-fill fallback order, so they only ever land on their
    own real dates."""
    from . import daily_studio, rotation, summit, book_queue, welcome_queue, podcast_month
    from . import accounts as _accts

    acct = account
    if isinstance(account, str):
        acct = _accts.get_account(account)

    def _podcast(_target, day_key):
        # PODCAST LIBRARY (PODCAST_LIBRARY_STAGE, default OFF): when armed, a
        # podcast slot FIRST tries the real Drive clip lane — pick_clip ->
        # notes-grounded caption -> download+ffprobe validate -> Zernio upload
        # -> a PENDING Draft. None (empty pool, missing notes Doc, failed
        # validation/upload) falls through to the existing month podcast
        # builder, exactly the spec's flow; the lane's own deduped alerts have
        # already fired. Flag OFF -> byte-for-byte today's behavior.
        if config.podcast_library_stage_enabled():
            try:
                from . import podcast_library_builder as _plib
                draft = _plib.build_podcast_clip_draft(acct, day_key)
                if draft is not None:
                    return draft
            except Exception as e:  # noqa: BLE001 - the lane never sinks the slot
                print(f"[podcast-library] builder failed: {type(e).__name__}: {e}; "
                      "falling through to the existing podcast builder")
        return podcast_month.build_month_podcast_draft(acct, day_key)

    def _platform(_target, day_key):
        return daily_studio.build_daily_infographic_draft(acct, day_key)

    def _b2b(_target, day_key):
        acct_lib = _library_for(acct)
        return rotation.build_rotated_draft(acct, day_key, _voice_for(acct), acct_lib)

    def _doctrine(_target, day_key):
        return daily_studio.build_daily_infographic_draft(acct, day_key)

    def _summit(_target, day_key):
        return summit.build_summit_draft(acct, day_key, voice=_voice_for(acct))

    def _book(_target, day_key):
        return book_queue.build_book_queue_draft(acct, day_key)

    def _welcome(_target, day_key):
        return welcome_queue.build_welcome_queue_draft(acct, day_key)

    return {
        "podcast": _podcast,
        "platform": _platform,
        "b2b": _b2b,
        "doctrine": _doctrine,
        "summit": _summit,
        "book": _book,
        "welcome": _welcome,
    }


def _voice_for(account):
    """The account's approved voice doc, or None (the builders tolerate a missing voice;
    they draw copy from their own approved source, not the voice hashtags)."""
    try:
        from .voice import load_voice
        return load_voice(getattr(account, "voice_path", None) or config.VOICE_DOC_PATH)
    except Exception:
        return None


def _library_for(account):
    """The account's creative library path (rotation reads b2b concept cards from here).
    Defaults to config.LIBRARY_PATH. account.library_path may be a method (bound
    accessor) rather than a string, so call it when callable before falling back."""
    lib = getattr(account, "library_path", None)
    if callable(lib):
        try:
            lib = lib()
        except Exception:
            lib = None
    return lib or config.LIBRARY_PATH


def _real_story_builder(account):
    """The genuine-9:16 story builder for `account`, anchored to the day's feed draft.
    Reuses stories.build_story_draft (the sole story source; never a cropped feed)."""
    from . import stories, accounts as _accts
    acct = account
    if isinstance(account, str):
        acct = _accts.get_account(account)

    def _story(_target, day_key, feed_draft):
        return stories.build_story_draft(acct, day_key, feed_draft=feed_draft)

    return _story


def _story_filename_for(feed_filename):
    """The paired 9:16 story filename for a sprint FEED card, by the render convention
    `<stem>_story.png` (summit_render.render_all_stories). Concept cards have a paired
    story render; the agenda / panel cards do NOT (they are feed only), so their story
    slot is honestly skipped downstream."""
    import os as _os
    stem, ext = _os.path.splitext(feed_filename)
    return f"{stem}_story{ext or '.png'}"


def _sprint_slot_map(posts_per_day=None):
    """Map (date, slot_index) -> {filename, caption, scheduled_for} across the whole laid
    out sprint, from summit_queue's own sprint serve path. Reuses summit_queue.sprint_assets
    (filename + approved caption) and summit_queue.sprint_calendar (the day/slot schedule),
    so the sprint is NOT re-implemented here. Pure over the queue."""
    from . import summit_queue as _sq
    assets = _sq.sprint_assets()
    caption_by_file = dict(assets)
    filenames = [f for f, _ in assets]
    ppd = posts_per_day if posts_per_day is not None else _sq.SPRINT_MAX_FEED_PER_DAY
    out = {}
    for slot in _sq.sprint_calendar(filenames, posts_per_day=ppd):
        out[(slot["date"], slot["slot_index"])] = {
            "filename": slot["filename"],
            "caption": caption_by_file.get(slot["filename"], ""),
            "scheduled_for": slot["scheduled_for"],
        }
    return out


def sprint_builders(account, manifest=None, posts_per_day=None):
    """Return (sprint_feed_builder, sprint_story_builder) that serve the laid-out summit
    sprint from summit_queue's real rendered assets. A slot with no hosted URL for its
    rendered file (or, for a story, no paired *_story render) is SKIPPED (returns None):
    never fabricated, never platform-padded. `manifest` maps rendered filename -> hosted
    URL (defaults to summit_queue's on-disk manifest)."""
    from . import summit_queue as _sq
    from .drafter import Draft, DraftStatus

    acct = account
    if isinstance(account, str):
        from . import accounts as _accts
        acct = _accts.get_account(account)
    platform = getattr(acct, "platform", None) or getattr(acct, "key", "") or ""
    acct_key = getattr(acct, "key", "") or (account if isinstance(account, str) else "")

    man = manifest if manifest is not None else _sq._load_manifest()
    slot_map = _sprint_slot_map(posts_per_day=posts_per_day)

    def _feed(_target, day_key, slot_index):
        info = slot_map.get((day_key, slot_index))
        if not info:
            return None
        url = (man or {}).get(info["filename"])
        if not url:
            return None  # rendered asset not hosted yet: skip, never fabricate
        did = _sq._draft_id(acct_key, f"sprint|{info['filename']}|{slot_index}", day_key)
        return Draft(
            draft_id=did, account_key=acct_key, platform=platform,
            caption=info["caption"], hashtags=[],
            creative_path=info["filename"], creative_public_url=url,
            scheduled_for=info["scheduled_for"], status=DraftStatus.PENDING,
            day_key=day_key, draft_type="summit", category="summit",
            slides=[], slide_urls=[])

    def _story(_target, day_key, slot_index, feed_draft):
        info = slot_map.get((day_key, slot_index))
        if not info:
            return None
        story_file = _story_filename_for(info["filename"])
        url = (man or {}).get(story_file)
        if not url:
            # No paired 9:16 render for this card (agenda/panel, or not yet hosted):
            # honestly skip the story. A story is NEVER a cropped feed card.
            return None
        did = _sq._draft_id(acct_key, f"sprint_story|{story_file}|{slot_index}", day_key)
        return Draft(
            draft_id=did, account_key=acct_key, platform=platform,
            caption="", hashtags=[],
            creative_path=story_file, creative_public_url=url,
            scheduled_for=info["scheduled_for"], status=DraftStatus.PENDING,
            day_key=day_key, is_story=True, draft_type="story", category="summit",
            source_fragments=list(getattr(feed_draft, "source_fragments", []) or []))

    return _feed, _story


def plan_and_build(account_key, start_date, days=30, *, book_dates=None,
                   summit_day_fn=None, welcome_dates=None, account=None, logger=None,
                   sprint_day_fn=None, sprint_feed_count_fn=None, sprint_manifest=None):
    """Plan and build the REAL month for `account_key`: a feed + paired 9:16 story per day
    on non-sprint days, and the laid-out SUMMIT SPRINT (up to 3 feed/day plus paired 9:16
    stories, served from summit_queue's real rendered assets) on its cycle dates. Every day
    filled when any real pillar can build for it; platform is capped on non-sprint days; all
    pillars represented; no pillar dominating.

    Behind AGENT_REAL_MONTH_PLAN: flag OFF -> [] and NOTHING is invoked (byte-for-byte
    today). Returns the flat list of real Drafts (feed + story), skipped/exhausted slots
    omitted. Nothing here publishes; apply the rows separately via
    real_month_planner.apply_month_plan (same flag)."""
    if not config.real_month_plan_enabled():
        return []
    acct = account
    if acct is None:
        from . import accounts as _accts
        acct = _accts.get_account(account_key)

    # 2x cadence (CADENCE_SPEC.md): the gym's effective posts/day. Flag off -> 1
    # before any I/O, so the pre-cadence plan is byte-for-byte unchanged.
    from .cadence import resolve_posts_per_day_live
    _base = account_key or ""
    for _suf in ("_ig", "_fb"):
        if _base.endswith(_suf):
            _base = _base[: -len(_suf)]
            break
    _ppd = resolve_posts_per_day_live(_base)
    plan = _rmp.plan_month(account_key, start_date, days, book_dates=book_dates,
                           summit_day_fn=summit_day_fn, welcome_dates=welcome_dates,
                           sprint_day_fn=sprint_day_fn,
                           sprint_feed_count_fn=sprint_feed_count_fn,
                           posts_per_day=_ppd)
    builders = real_builders_map(acct if acct is not None else account_key)
    story_builder = _real_story_builder(acct if acct is not None else account_key)
    sprint_feed, sprint_story = sprint_builders(
        acct if acct is not None else account_key, manifest=sprint_manifest)
    return _rmp.build_month_drafts(plan, builders, story_builder=story_builder,
                                   account=None, logger=logger,
                                   sprint_builder=sprint_feed,
                                   sprint_story_builder=sprint_story)
