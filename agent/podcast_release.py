"""
Podcast release card (Part B of the podcast pipeline).

Dormant behind AGENT_PODCAST_ENABLED (default OFF = zero behavior change: the
runner's podcast slot returns None and the daily chain runs exactly as today).
Armed, a newly detected episode auto-drafts ONE release card as that day's feed
post candidate and cards it to the approval channel like every other post.

FOUR LOCKED TEMPLATES (podcast_release_a/b/c/e) live here in the same
concept-library shape the house renders use and render through the SAME locked
builder (creative_studio.generate); each template is a scoped palette
exception exactly like the book cover (deep navy poster system, never the
cream house canvas, never applied to anything but release cards). Three
dynamic text slots per template, encoded faithfully with no drift:

    EPISODE <N>     -> the episode number, always 3 digits (007, 131)
    <TITLE>         -> at most 2 lines, about 40 characters per line,
                       truncated at the last full word, never mid word
    one line about  -> derived ONLY from the feed's episode description:
                       markup stripped, first sentence, DASH FREE (asserted)

TEMPLATE ROTATION is deterministic, never random: episode number modulo 4
over the set A, B, C, E (131 = E, 132 = A, 133 = B, 134 = C, and so on), so
the pick is stable across re-drafts. The chosen template is logged in the
audit row.

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


def title_lines(title, width=40, max_lines=2):
    """The 2 line title slot: at most `max_lines` lines of about `width`
    characters, broken and truncated at the LAST FULL WORD only, never mid
    word. A single word longer than the width stands alone rather than being
    cut. Overflow past the second line is dropped at the word boundary."""
    words = _title_slot(title).split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) <= width or not cur:
            cur = trial
            continue
        lines.append(cur)
        cur = w
        if len(lines) == max_lines:
            return lines  # truncated at the last full word that fit
    if cur:
        lines.append(cur)
    return lines[:max_lines]


# ---- the four LOCKED templates (encoded faithfully from the brief, no drift) --------
TEMPLATE_ORDER = ("a", "b", "c", "e")

_FOOTER = "HOSTED BY SHERMAN MERRICKS AND BLAKE RUFF . LASSOFRAMEWORK.COM"

_TEMPLATE_SPECS = {
    "a": (
        "LOCKED TEMPLATE podcast_release_a, the CLASSIC POSTER. Canvas: deep "
        "navy #1A2340, dark vignette, film grain, soft top spotlight. A white "
        "line art studio microphone with a glowing red horizontal waveform "
        "behind it. Render EXACTLY these text elements and no other text: a "
        "white condensed bold masthead GYM MARKETING MADE SIMPLE with red "
        "letterspaced BY LASSO . NEW EPISODE EVERY MONDAY beneath it. Red "
        "letterspaced EPISODE {ep}. Then the title in white on at most two "
        "lines: {title}. Then the about line: {about}. Footer: {footer}"
    ),
    "b": (
        "LOCKED TEMPLATE podcast_release_b, the BOLD SPLIT. Canvas: a diagonal "
        "split, upper left two thirds deep navy #1A2340, lower right third a "
        "solid red panel. Render EXACTLY these text elements and no other "
        "text: top left masthead GYM MARKETING MADE SIMPLE / BY LASSO. "
        "Colossal white stacked NEW EPISODE IS LIVE. with LIVE in glowing red "
        "plus a red pulse dot. Red EPISODE {ep}. The title in white on at "
        "most two lines: {title}. The about line: {about}. On the red panel: "
        "a white play button in a thin circle, a white waveform, and small "
        "EVERY MONDAY. Footer: {footer}"
    ),
    "c": (
        "LOCKED TEMPLATE podcast_release_c, the ON AIR STUDIO. Canvas: deep "
        "navy #1A2340, moody haze, film grain. A glowing red neon ON AIR sign "
        "in a thin rounded rectangle top center. Render EXACTLY these text "
        "elements and no other text: masthead GYM MARKETING MADE SIMPLE / BY "
        "LASSO beneath the sign. Red EPISODE {ep} . NEW EVERY MONDAY. The "
        "title in white on at most two lines: {title}. The about line: "
        "{about}. Lower third: white line art headphones beside a small mic "
        "joined by a thin red waveform. Footer: {footer}"
    ),
    "e": (
        "LOCKED TEMPLATE podcast_release_e, the PODCAST PLAYER. Canvas: deep "
        "navy #1A2340, dark vignette, film grain. Render EXACTLY these text "
        "elements and no other text: top center masthead GYM MARKETING MADE "
        "SIMPLE with red BY LASSO beneath it. Center: a floating dark rounded "
        "podcast player card, slightly lighter navy with a soft shadow, "
        "containing red letterspaced NOW PLAYING . EPISODE {ep}, the title in "
        "white on at most two lines: {title}, a thin white progress bar with "
        "a glowing red played portion and a round red scrubber dot, small "
        "white timestamps at each end, and three player icons: previous, a "
        "large glowing red circular play button center, next. The about line "
        "sits below the player card: {about}. Footer: NEW EVERY MONDAY . "
        "{footer}"
    ),
}


def template_for_episode(n):
    """Deterministic template pick: episode number modulo 4 over A, B, C, E
    (131 = E, 132 = A, 133 = B, 134 = C). Stable across re-drafts, never
    random."""
    return TEMPLATE_ORDER[int(n) % 4]


def release_concept(episode):
    """
    The composed LOCKED template for this episode, in the concept-library shape
    (headline / concept / set), slots filled with real values: the 3 digit
    episode number, the word boundary 2 line title, and the dash free about
    line. `template` names the pick; `palette` carries the full locked design
    spec into the builder (the scoped exception, exactly like the book cover).
    """
    n = int(episode["episode"])
    t = template_for_episode(n)
    lines = title_lines(episode["title"])
    about = about_line(episode["description"])
    spec = _TEMPLATE_SPECS[t].format(
        ep=f"{n:03d}",
        title=" / ".join(lines),
        about=about or "(the feed gave no description; render no about line)",
        footer=_FOOTER)
    return {
        "headline": f"EPISODE {n}: {_title_slot(episode['title'])}",
        "concept": [spec],
        "template": f"podcast_release_{t}",
        "palette": spec,
        "set": "brand",
    }


def build_podcast_slot_draft(account, day_key, nano_client=None, s3_client=None):
    """
    The podcast slot in the daily chain: the release card for the NEWEST
    detected episode first, else one queued episode infographic (Part D,
    podcast_cards). At most ONE podcast draft per account per day (the spacing
    law), once per episode for releases, held for the tap always. None while
    the flag is OFF or nothing is waiting or the studio/hosting is unavailable
    (content stays queued; the normal path takes the day).
    """
    if not config.podcast_enabled():
        return None
    if db.kv_get(f"podcast_served_{account.key}_{day_key}"):
        return None  # spacing law: at most one podcast draft per account per day
    draft = _next_release_draft(account, day_key, nano_client, s3_client)
    if draft is None:
        from . import podcast_cards
        draft = podcast_cards.build_card_draft(account, day_key,
                                               nano_client, s3_client)
    if draft is None:
        return None
    db.kv_set(f"podcast_served_{account.key}_{day_key}", draft.draft_id)
    return draft


def _next_release_draft(account, day_key, nano_client, s3_client):
    """
    The release card for the newest detected episode, once per account. A
    release announcement is time sensitive, so ONLY the latest episode is ever
    a candidate: arming the flag on a feed with history can never blast a
    backlog of stale release cards.
    """
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
        # State deliberately NOT advanced: episode stays eligible on the next
        # poll so a transient studio outage is self-healing without operator
        # intervention.
        ops_alerts.alert(
            f"podcast release card skipped: studio unavailable for episode "
            f"{latest['episode']} ({latest['title'][:60]!r}); "
            f"episode stays eligible"
        )
        return None
    db.kv_set(f"podcast_release_carded_{latest['guid']}_{account.key}", day_key)
    db.audit("podcast_release", draft.draft_id,
             f"episode {latest['episode']} release card drafted via "
             f"{release_concept(latest)['template']} (held for approval)",
             account.key, day_key)
    return draft


def _build_release_draft(account, day_key, ep, nano_client, s3_client):
    n = ep["episode"]
    title = _title_slot(ep["title"])
    about = about_line(ep["description"])
    # The about line is dash free BY CONSTRUCTION; assert it anyway (the law).
    assert not _DASH_RE.search(about), "about line carries a dash character"
    spec = release_concept(ep)
    # The SAME house builder; the template block rides as the scoped palette
    # exception (like the book cover), never as a style override elsewhere.
    art = creative_studio.generate(spec["headline"], spec["concept"],
                                   client=nano_client, palette=spec["palette"])
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


def release_draft_for_episode(account, episode_n, day_key, nano_client=None,
                               s3_client=None):
    """
    Manual redraft: build a release card for a specific episode on demand,
    bypassing the once-per-episode guard. The draft is PENDING and held for
    approval; nothing publishes. Use when the scheduled poll was skipped due
    to a dark studio and the episode needs to be recovered by hand.

    Returns the Draft or None (not found / studio unavailable).
    """
    if not config.podcast_enabled():
        return None
    ep = podcast_feed.get_episode(episode_n)
    if ep is None:
        return None
    if ep["episode"] is None:
        return None
    draft = _build_release_draft(account, day_key, ep, nano_client, s3_client)
    if draft is None:
        ops_alerts.alert(
            f"podcast-draft: studio unavailable for episode {episode_n} "
            f"manual redraft ({account.key})"
        )
        return None
    db.kv_set(f"podcast_release_carded_{ep['guid']}_{account.key}", day_key)
    db.audit("podcast_release", draft.draft_id,
             f"episode {episode_n} release card MANUALLY drafted via "
             f"{release_concept(ep)['template']} (held for approval)",
             account.key, day_key)
    return draft
