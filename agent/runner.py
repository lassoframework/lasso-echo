"""
Daily runner.

Once a day, for each connected account:
  - master flag OFF        -> do nothing.
  - voice doc missing      -> post ONE notice to Slack, draft nothing.
  - otherwise              -> draft exactly ONE post and post an approval card.

Nothing publishes here. This job only drafts and surfaces. Publishing happens
later, only on a human Approve, and only if the publish flag is armed.
"""

from datetime import datetime, timezone

from . import config, schedule
from .accounts import active_accounts
from .daily_studio import build_daily_infographic_draft
from .drafter import draft_post
from .library import pick_next
from .postlog import used_creatives_for
from .slack_surface import SlackPoster
from .stories import build_story_draft
from .voice import load_voice


def run_daily(poster=None, voice_path=None, library_path=None,
              scheduled_for=None, accounts=None, store=None):
    """
    Returns a list of Draft objects produced this run (one per account, or a
    blocked marker). Side effects: posts approval cards to Slack AND saves each
    non-blocked draft to the pending store so the listener can act on it later.
    """
    results = []

    if not config.master_enabled():
        # agent disarmed. say nothing publicly; just report state to the caller.
        return {"status": "disabled", "drafts": []}

    poster = poster or SlackPoster()
    voice = load_voice(voice_path or config.VOICE_DOC_PATH)

    if voice is None:
        poster.post_notice(":warning: Brand voice doc missing or empty. "
                           "Drafting nothing until it's in place.")
        return {"status": "no_voice", "drafts": []}

    if store is None:
        from .store import PendingStore
        store = PendingStore()

    when = scheduled_for or datetime.now(timezone.utc).isoformat()
    day_key = when[:10]  # YYYY-MM-DD, the day this post is for
    lib = library_path or config.LIBRARY_PATH

    for account in (accounts or active_accounts()):
        # Cadence gate FIRST: a skip day (default Saturday) produces no draft and no
        # card for this account.
        if not schedule.should_post_on(day_key):
            continue

        draft = None
        # For a LASSO account, try the fully-automated infographic path FIRST. It is
        # dormant unless all three flags are armed; None -> fall back to the library
        # path unchanged. (A BLOCKED draft is still a draft: it surfaces, not falls back.)
        if account.key.startswith("lasso"):
            draft = build_daily_infographic_draft(account, day_key)
        if draft is None:
            creative = pick_next(account, lib, used_creatives_for(account.key))
            # Schedule the fallback draft to the same cadence slot.
            draft = draft_post(account, creative, schedule.scheduled_for(day_key), voice=voice)
        poster.post_approval_card(draft)
        if draft.status.value != "blocked":
            store.put(draft)
        results.append(draft)

        # Stories: FULLY DORMANT unless AGENT_STORIES_ENABLED. Armed, draft one
        # 9:16 Story per account reusing the day's creative; PENDING, its own
        # approval card, clearly labeled STORY. Nothing publishes here.
        story = build_story_draft(account, day_key, feed_draft=draft)
        if story is not None:
            poster.post_approval_card(story)
            store.put(story)
            results.append(story)

    return {"status": "drafted", "drafts": results}
