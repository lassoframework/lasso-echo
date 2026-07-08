"""
Three podcast touches per episode (category rotation Part 3).

Every new episode earns THREE posts across its week, one per slot in the
seven-day schedule (content_categories._DAILY_SCHEDULE):

  Mon  release card       (infographic)  -> podcast_release.release_draft_for_episode
  Thu  Opus clip          (video)        -> build_clip_touch (this module)
  Sun  episode infographic (infographic) -> podcast_cards.build_episode_card

Every touch is PENDING and held for the approver's tap; nothing here publishes.
The whole controller rides AGENT_PODCAST_ENABLED (the podcast pipeline flag);
OFF = zero behavior change (touches_for_episode returns None).

The clip touch is drawn from THAT episode's Opus clips only: an ingested clip
whose sidecar title or note names the episode number (via the same 'episode N'
/ 'ep N' / '#N' convention the feed uses). Its caption is the clip's OWN words
(the sidecar note), verbatim, dash free, and passed through the fabrication
gate so a stat bearing clip title is benched, never shipped. When no clip names
the episode the clip touch is None (the runner's Part 2 fallback then swaps in
an infographic and fires the empty-video-slot alert).
"""

import json
import os
import re
from datetime import date, timedelta

from . import config, media_host, ops_alerts, podcast_cards, podcast_release, rotation, schedule
from .drafter import Draft, DraftStatus, _make_id
from .podcast_release import RELEASE_HASHTAGS, _dash_free, _DASH_RE

# Episode reference in a clip's title/note: 'episode 7', 'ep. 7', 'ep7', '#7'.
_EP_REF_RE = re.compile(r"(?:\b(?:episode|ep\.?)\s*|#)(\d+)\b", re.IGNORECASE)

_VIDEO_EXTS = (".mp4", ".mov", ".m4v")

# Slot offsets from the week's Monday, matching _DAILY_SCHEDULE.
_RELEASE_OFFSET = 0   # Mon
_CLIP_OFFSET = 3      # Thu
_INFOGRAPHIC_OFFSET = 6  # Sun


def _clip_refs_episode(sidecar, episode_n):
    """True when an opus sidecar's title or note names this episode number."""
    for field in ("title", "note", "description"):
        for m in _EP_REF_RE.finditer(str(sidecar.get(field, "") or "")):
            if int(m.group(1)) == int(episode_n):
                return True
    return False


def clip_for_episode(episode_n, library_path=None):
    """
    The first ingested Opus clip whose sidecar names this episode, as a
    (video_path, sidecar) pair, or None. Scans the library in sorted order so
    the pick is stable across re-runs. Only source=='opus' clips are eligible.
    """
    lib = library_path or config.LIBRARY_PATH
    if not lib or not os.path.isdir(lib):
        return None
    for name in sorted(os.listdir(lib)):
        if os.path.splitext(name)[1].lower() not in _VIDEO_EXTS:
            continue
        stem = os.path.splitext(name)[0]
        side_path = os.path.join(lib, stem + ".json")
        try:
            with open(side_path, encoding="utf-8") as fh:
                sidecar = json.load(fh) or {}
        except (OSError, ValueError):
            continue
        if sidecar.get("source") != "opus":
            continue
        if _clip_refs_episode(sidecar, episode_n):
            return os.path.join(lib, name), sidecar
    return None


def build_clip_touch(account, episode_n, day_key, s3_client=None, library_path=None):
    """
    The Thursday video touch: a Reel draft from one of this episode's Opus
    clips. Caption is the clip's own note, verbatim + dash free, gate cleaned
    (a stat bearing clip note that no approved source clears is benched: the
    touch returns None rather than ship an unverified claim). PENDING, cited
    podcast_ep<N>, held for the tap.

    None when: the flag is OFF, no clip names this episode, the note fails the
    fabrication gate, or the clip has no hosted URL and hosting is unavailable.
    """
    if not config.podcast_enabled():
        return None
    n = int(episode_n)
    found = clip_for_episode(n, library_path)
    if found is None:
        return None
    clip_path, sidecar = found

    note = _dash_free(sidecar.get("note", "") or sidecar.get("title", "") or "")
    if note and not rotation.is_gate_clean(note):
        ops_alerts.alert(
            f"podcast clip touch: episode {n} clip note carries an unverified "
            f"claim ({account.key}); benched, not shipped. Note: {note[:80]!r}"
        )
        return None

    public_url = sidecar.get("public_url", "") or ""
    if not public_url:
        public_url = media_host.host_media(clip_path, account.key, client=s3_client) or ""
    if not public_url:
        return None  # a Reel needs a hosted URL; hosting unavailable -> skip the touch

    caption_parts = []
    if note:
        caption_parts.append(note)
    caption_parts.append(f"A clip from episode {n} of our podcast. Listen to the full episode.")
    caption = "\n\n".join(caption_parts)
    assert not _DASH_RE.search(caption), "podcast clip caption carries a dash"

    fragments = [f"cite:podcast_ep{n}"]
    if note:
        fragments.append(note)
    draft = Draft(
        draft_id=_make_id(account.key, f"podcast_clip_{n}", day_key),
        account_key=account.key, platform=account.platform,
        caption=caption, hashtags=list(RELEASE_HASHTAGS),
        creative_path=clip_path, creative_public_url=public_url,
        scheduled_for=schedule.scheduled_for(day_key), status=DraftStatus.PENDING,
        source_fragments=fragments, day_key=day_key, draft_type="podcast",
    )
    return draft


def touches_for_episode(account, episode_n, week_monday, nano_client=None,
                        s3_client=None, library_path=None):
    """
    The three touches for one episode across its week, keyed by slot:
      {"release": Draft|None, "clip": Draft|None, "infographic": Draft|None}
    day_keys are computed from week_monday (a YYYY-MM-DD Monday): Mon/Thu/Sun.
    Each value is a PENDING draft held for the tap, or None when that touch
    cannot build (missing clip, dark studio, no transcript). None (not a dict)
    when AGENT_PODCAST_ENABLED is OFF.
    """
    if not config.podcast_enabled():
        return None
    mon = date.fromisoformat(week_monday)
    release_day = (mon + timedelta(days=_RELEASE_OFFSET)).isoformat()
    clip_day = (mon + timedelta(days=_CLIP_OFFSET)).isoformat()
    info_day = (mon + timedelta(days=_INFOGRAPHIC_OFFSET)).isoformat()

    release = podcast_release.release_draft_for_episode(
        account, episode_n, release_day, nano_client, s3_client)
    clip = build_clip_touch(account, episode_n, clip_day,
                            s3_client=s3_client, library_path=library_path)
    infographic = podcast_cards.build_episode_card(
        account, episode_n, info_day, nano_client, s3_client)
    return {"release": release, "clip": clip, "infographic": infographic}
