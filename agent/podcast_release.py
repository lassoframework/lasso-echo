"""
Podcast release card (Part B of the podcast pipeline).

Dormant behind AGENT_PODCAST_ENABLED (default OFF = zero behavior change: the
runner's podcast slot returns None and the daily chain runs exactly as today).
Armed, a newly detected episode auto-drafts ONE release card as that day's feed
post candidate and cards it to the approval channel like every other post.

THE TEMPLATE CONCEPT `podcast_release` lives here in the exact shape the 18
house-style renders use (headline / concept context / archetype) and renders
through the SAME locked builder (creative_studio.build_prompt via generate),
composed per episode from three dynamic text slots:

    EPISODE <N>   +   <TITLE>          -> the card headline
    one line about                     -> concept context + the caption

The about line is derived ONLY from the feed's episode description: stripped of
markup, trimmed to its first sentence, and DASH FREE (every dash family
character removed; asserted at build time, belt and suspenders).

PRIORITY: the runner calls this slot AFTER the book campaign queue and BEFORE
pillar rotation. An episode cards exactly ONCE per account (a re-poll changes
nothing here: detection state and carding state are separate), at most one
podcast draft per account per day. Every draft is PENDING and held for Blake's
tap; NOTHING here publishes.
"""

import html
import re

from . import config, creative_studio, db, media_host, ops_alerts, schedule
from . import podcast_feed
from .drafter import Draft, DraftStatus, _make_id

# Every dash family character (em, en, figure, horizontal bar, minus, ASCII
# hyphen): none may survive into client-facing copy.
_DASH_RE = re.compile(r"[‐‑‒–—―−-]")
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_EP_PREFIX = re.compile(r"^\s*(?:episode|ep\.?)\s*\d+\s*[:.]?\s*", re.IGNORECASE)

RELEASE_HASHTAGS = ["#lassoframework", "#gymowner", "#podcast"]


def _dash_free(text):
    """Remove every dash family character; collapse the spaces removals leave."""
    return re.sub(r"\s{2,}", " ", _DASH_RE.sub(" ", text or "")).strip()


def about_line(description):
    """
    The card's one line about, derived ONLY from the feed description: markup
    stripped, entities unescaped, trimmed to the FIRST sentence, dash free.
    Empty when the feed gave no description (nothing is invented).
    """
    text = html.unescape(_TAG_RE.sub(" ", description or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    first = re.split(r"(?<=[.!?])\s+", text)[0].strip()
    return _dash_free(first)


def _title_slot(title):
    """The TITLE text slot: the feed title minus any 'Episode N:' prefix (the
    EPISODE <N> slot carries the number), dash free for the on-image law."""
    return _dash_free(_TITLE_EP_PREFIX.sub("", title or "").strip())


def release_concept(episode):
    """
    The `podcast_release` template concept, composed per episode in the exact
    shape the 18 house-style renders use. The headline is the only on-image
    text (the locked one-headline law); the about line rides as concept context
    for the focal graphic, never rendered as image text.
    """
    n = episode["episode"]
    title = _title_slot(episode["title"])
    about = about_line(episode["description"])
    concept = [
        "A new podcast episode announcement: a line icon microphone with "
        "soundwaves, one small uppercase label NEW EPISODE.",
    ]
    if about:
        concept.append(f"Episode context (never rendered as text): {about}")
    return {
        "headline": f"EPISODE {n}: {title}",
        "concept": concept,
        "archetype": "hero",
        "set": "brand",
    }


def build_podcast_slot_draft(account, day_key, nano_client=None, s3_client=None):
    """
    The podcast slot in the daily chain: the release card for the NEWEST
    detected episode, once per account. A release announcement is time
    sensitive, so ONLY the latest episode is ever a candidate: arming the flag
    on a feed with history can never blast a backlog of stale release cards.
    None while the flag is OFF, when the latest episode already carded, when
    this account already drafted a podcast card today (max one per day), or
    when the studio/hosting is unavailable (the episode stays waiting; the
    normal path takes the day).
    """
    if not config.podcast_enabled():
        return None
    if db.kv_get(f"podcast_served_{account.key}_{day_key}"):
        return None  # spacing law: at most one podcast draft per account per day
    episodes = podcast_feed.list_episodes()
    if not episodes:
        return None
    latest = episodes[-1]  # detection order: the last row is the newest episode
    if latest["episode"] is None:
        # Client content only: without an episode number the EPISODE slot cannot
        # be filled, and numbering is never invented. Say so once, loud.
        if not db.kv_get(f"podcast_no_number_alerted_{latest['guid']}"):
            db.kv_set(f"podcast_no_number_alerted_{latest['guid']}", "1")
            ops_alerts.alert("podcast episode has no number in the feed "
                             f"({latest['title'][:80]!r}); no release card until "
                             "the feed carries one. Never guessed.")
        return None
    if db.kv_get(f"podcast_release_carded_{latest['guid']}_{account.key}"):
        return None  # already carded once; a re-poll never re-cards
    draft = _build_release_draft(account, day_key, latest, nano_client, s3_client)
    if draft is None:
        return None
    db.kv_set(f"podcast_release_carded_{latest['guid']}_{account.key}", day_key)
    db.kv_set(f"podcast_served_{account.key}_{day_key}", latest["guid"])
    db.audit("podcast_release", draft.draft_id,
             f"episode {latest['episode']} release card drafted (held for approval)",
             account.key, day_key)
    return draft


def _build_release_draft(account, day_key, ep, nano_client, s3_client):
    n = ep["episode"]
    title = _title_slot(ep["title"])
    about = about_line(ep["description"])
    # The about line is dash free BY CONSTRUCTION; assert it anyway (the law).
    assert not _DASH_RE.search(about), "about line carries a dash character"
    spec = release_concept(ep)
    art = creative_studio.generate(spec["headline"], spec["concept"],
                                   client=nano_client, archetype=spec["archetype"])
    if art is None:
        return None  # studio unavailable/dark: the caller's normal path runs
    hosted = media_host.host_media(art["path"], account.key, client=s3_client)
    if not hosted:
        return None
    caption_parts = [f"EPISODE {n}: {title}"]
    if about:
        caption_parts.append(about)
    caption_parts.append("New episode of our podcast is live. Listen now.")
    return Draft(
        draft_id=_make_id(account.key, f"podcast_release_{n}", day_key),
        account_key=account.key, platform=account.platform,
        caption="\n\n".join(caption_parts), hashtags=list(RELEASE_HASHTAGS),
        creative_path=art["path"], creative_public_url=hosted,
        scheduled_for=schedule.scheduled_for(day_key), status=DraftStatus.PENDING,
        source_fragments=[f"cite:podcast_ep{n}", ep["title"], about],
        day_key=day_key, draft_type="podcast",
    )
