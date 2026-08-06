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
    Defaults to config.LIBRARY_PATH."""
    return getattr(account, "library_path", None) or config.LIBRARY_PATH


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


def plan_and_build(account_key, start_date, days=30, *, book_dates=None,
                   summit_day_fn=None, welcome_dates=None, account=None, logger=None):
    """Plan and build the REAL month for `account_key`: a feed + paired 9:16 story per day,
    every day filled when any real pillar can build for it, all pillars represented via the
    varied base rotation plus book/summit/welcome overrides, no pillar dominating.

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

    plan = _rmp.plan_month(account_key, start_date, days, book_dates=book_dates,
                           summit_day_fn=summit_day_fn, welcome_dates=welcome_dates)
    builders = real_builders_map(acct if acct is not None else account_key)
    story_builder = _real_story_builder(acct if acct is not None else account_key)
    return _rmp.build_month_drafts(plan, builders, story_builder=story_builder,
                                   account=None, logger=logger)
