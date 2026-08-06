"""
podcast_month.py — the MONTH-AHEAD podcast builder for the real month planner.

The daily podcast slot (podcast_release.build_podcast_slot_draft) is release-driven:
it only ever cards the single NEWEST detected episode, once, then serves queued
transcript cards. That is correct for the live daily chain but it cannot FILL a
month: a 30-day plan hits many podcast rotation days and the daily slot would card
nothing on all but one of them, leaving the calendar empty.

This module fills those days from the REAL podcast content pool WITHOUT fabrication:

  pool, in priority order, ALL real:
    1. every stored episode's real title + its real one-line "about" (podcast_feed,
       the same about_line podcast_release renders on the release card), and
    2. every stored episode's verbatim transcript hook/support concepts
       (podcast_cards.extract_concepts, the exact verbatim-cited copy the daily
       card path uses).

  determinism: for a given (account, day_key) the topic is chosen by the day's
  stable ordinal (content_planner._day_seq) modulo the pool size, so DIFFERENT
  podcast days get DIFFERENT real topics and the same day always resolves to the
  same one. No randomness, no Date.now.

  exhaustion: an empty pool (podcast flag OFF, no stored episodes, no transcripts,
  or the studio/hosting dark) returns None. The month planner then falls back to
  the next real pillar for that slot. NOTHING here invents a topic, a number, or a
  line of copy; every card is rendered from a real episode's real title/about or a
  real verbatim transcript sentence, cited podcast_ep<N>.

Rendered through the SAME house builder as the daily podcast card
(creative_studio.generate + media_host.host_media), so a month-ahead podcast card
is byte-for-byte the same kind of asset the daily path would produce. PENDING, held
for the tap. Nothing here publishes.
"""

from . import config, creative_studio, media_host, schedule
from .drafter import Draft, DraftStatus, _make_id
from .podcast_release import RELEASE_HASHTAGS, _DASH_RE, _title_slot, about_line
from . import podcast_feed


def _episode_topics():
    """Every stored episode as a real (n, title, about) topic, in a stable order.
    Only NUMBERED episodes (the EPISODE slot / citation needs the number) with a
    non-empty title are kept; the about line may be empty (the feed gave none) and
    the caption then simply omits it. No fabrication: title and about come straight
    from the stored feed record."""
    topics = []
    for ep in podcast_feed.list_episodes():
        n = ep.get("episode")
        title = _title_slot(ep.get("title") or "")
        if n is None or not title:
            continue
        topics.append((int(n), title, about_line(ep.get("description") or "")))
    # deterministic, ascending by episode number (detection order can vary)
    topics.sort(key=lambda t: t[0])
    return topics


def _transcript_concepts():
    """Every stored episode's verbatim (n, hook, support) concepts, in a stable
    order. These are the exact verbatim-cited pairs the daily card path extracts;
    an episode with no stored transcript / no clean pair contributes nothing (the
    extractor raises or returns []), never a fabricated line."""
    from . import podcast_cards
    concepts = []
    for n, _title, _about in _episode_topics():
        try:
            picks = podcast_cards.extract_concepts(n, 2)
        except ValueError:
            continue  # no transcript stored / no clean pairs: contributes nothing
        for hook, support in picks or []:
            if _DASH_RE.search(hook) or _DASH_RE.search(support):
                continue  # verbatim only; a dashed pair is skipped, never rewritten
            concepts.append((int(n), hook, support))
    return concepts


def build_month_podcast_draft(account, day_key, *, nano_client=None, s3_client=None):
    """A real podcast-topic infographic for (account, day_key), chosen deterministically
    from the real podcast content pool. PENDING, held for the tap.

    Returns None (the planner then falls back to the next real pillar) when the podcast
    flag is OFF, the pool is empty (no numbered episodes and no transcript concepts),
    or the studio / hosting is unavailable. Never fabricates: every card is a real
    episode's real title+about or a real verbatim transcript hook/support, cited
    podcast_ep<N>."""
    if not config.podcast_enabled():
        return None
    if account is None:
        return None

    from .content_planner import _day_seq
    seq = _day_seq(day_key)

    # POOL: transcript concepts first (they carry a verbatim support line the way the
    # daily card does), then episode title/about topics. Both are real; concatenating
    # them and indexing by the day's stable ordinal gives distinct real topics across
    # distinct podcast days, deterministically.
    concepts = _transcript_concepts()
    topics = _episode_topics()
    pool = []  # each item: ("concept", n, hook, support) | ("topic", n, title, about)
    for n, hook, support in concepts:
        pool.append(("concept", n, hook, support))
    for n, title, about in topics:
        pool.append(("topic", n, title, about))
    if not pool:
        return None  # exhausted / nothing stored: planner falls back, never fakes

    kind, n, a, b = pool[seq % len(pool)]
    if kind == "concept":
        headline, support = a, b
        caption = (f"{headline}\n\n{support}\n\n"
                   f"We break it down in episode {n} of our podcast. Listen now.")
        fragments = [f"cite:podcast_ep{n}", headline, support]
        facts = [support]
    else:
        title, about = a, b
        headline = f"Episode {n}: {title}"
        if about:
            caption = (f"{headline}\n\n{about}\n\n"
                       "A look back at the show. Listen now.")
            facts = [about]
            fragments = [f"cite:podcast_ep{n}", title, about]
        else:
            caption = f"{headline}\n\nA look back at the show. Listen now."
            facts = [title]
            fragments = [f"cite:podcast_ep{n}", title]

    # SAME house builder as the daily podcast card: default palette + archetype
    # rotation, the headline as the one on-image line. Dark studio/hosting -> None.
    art = creative_studio.generate(
        headline, facts, client=nano_client, account_key=account.key,
        archetype=creative_studio.archetype_for_day(day_key))
    if art is None:
        return None
    hosted = media_host.host_media(art["path"], account.key, client=s3_client)
    if not hosted:
        return None

    assert not _DASH_RE.search(caption), "podcast month card caption carries a dash"
    return Draft(
        draft_id=_make_id(account.key, f"podcast_month_{n}_{kind}", day_key),
        account_key=account.key, platform=account.platform,
        caption=caption, hashtags=list(RELEASE_HASHTAGS),
        creative_path=art["path"], creative_public_url=hosted,
        scheduled_for=schedule.scheduled_for(day_key), status=DraftStatus.PENDING,
        source_fragments=fragments, day_key=day_key, draft_type="podcast",
    )
