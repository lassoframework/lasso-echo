"""
Deployed Monday auto-ingest: pull the newest episode from the Drive folder, edit
it, and schedule the week as HELD drafts. Runs HEADLESS on Railway (so the drafts
land in the store the Slack listener reads and Approve works).

Headless note: motion b-roll (Higgsfield) and the still-card Gemini path are the
only interactive/keyed pieces. Motion b-roll is SKIPPED headless (no MCP in cron);
the reels still get the approved technique headless: animated intro card, host +
word-highlight captions, Treatment B side panels, and Nano Banana still cards
(when AGENT_VIDEO_STILLS_ENABLED + AGENT_NANO_API_KEY are set). Nothing publishes;
every clip is a held PENDING draft awaiting approval.

All behind AGENT_PODCAST_AUTO_ENABLED (default OFF).
"""

import datetime
import os

from . import config, schedule


def _next_posting_days(n, start=None):
    """The next n posting days (schedule.should_post_on True), starting today."""
    d = start or datetime.date.today()
    out, i = [], 0
    while len(out) < n and i < 60:
        dk = (d + datetime.timedelta(days=i)).isoformat()
        if schedule.should_post_on(dk):
            out.append(dk)
        i += 1
    return out


def run(source=None, render=False, account_key=None, client=None, poster=None,
        transcriber=None, llm=None, today=None):
    """Auto-edit the newest episode and schedule its clips across the week as held
    drafts. Returns a summary dict, or None when the flag is OFF."""
    if not config.podcast_auto_enabled():
        print("podcast-auto: OFF (set AGENT_PODCAST_AUTO_ENABLED=true). Nothing done.",
              flush=True)
        return None

    from . import video_editor, media_host, clipper

    acct = account_key or config.podcast_account_key()

    # Default the R2 client so the approval card gets a hosted video URL when the
    # daemon calls run() with no args (mirrors episode_inbox.poll()). A missing
    # client is non-fatal: the draft still saves, just without a preview URL.
    if client is None:
        try:
            client = media_host._default_client()
        except Exception:
            client = None

    # 1. get the newest episode (Drive pull, headless) unless one was passed in
    if source is None:
        from . import podcast_source
        source = podcast_source.newest_episode(
            os.path.join(config.clipper_cache_dir(), "episodes"))
    print(f"podcast-auto: source = {source}", flush=True)

    # 2. edit the episode (headless treatment; motion b-roll skipped without MCP)
    result = video_editor.edit_episode(source, render=render, client=client,
                                       transcriber=transcriber, llm=llm,
                                       account_key=acct)
    if not result:
        print("podcast-auto: editor returned nothing (flag off or no clips).",
              flush=True)
        return None

    clips = result.get("clips", [])[:config.podcast_auto_max_clips()]
    if not clips:
        print("podcast-auto: no clips to schedule.", flush=True)
        return {"scheduled": []}

    # 3. spread the clips across the next posting days, one per day
    start = today or datetime.date.today()
    days = _next_posting_days(len(clips), start=start)
    staged = result.get("staged") or {}
    ep_title = os.path.basename(staged.get("r2_key", source)).rsplit(".", 1)[0]

    scheduled = []
    for clip, day in zip(clips, days):
        m = clip["moment"]
        files = clip.get("files", {})
        primary = files.get("9:16_cap") or next(iter(files.values()), "")
        if not primary or not os.path.isfile(primary):
            print(f"podcast-auto: no rendered file for [{m.start_ts:.0f}], skipping",
                  flush=True)
            continue
        url = ""
        if client:
            try:
                url = media_host.host_media(primary, acct, client=client) or ""
            except Exception as exc:
                print(f"podcast-auto: host failed: {exc}", flush=True)
        sched = schedule.scheduled_for(day)
        try:
            d = clipper.save_clip_draft(m, primary, url, acct,
                                        scheduled_for=sched, episode_title=ep_title,
                                        poster=poster)
            scheduled.append({"draft_id": getattr(d, "draft_id", ""),
                              "day": day, "scheduled_for": sched,
                              "hook": getattr(m, "hook", "")})
            print(f"podcast-auto: scheduled {day} {schedule.weekday_abbr(day)} "
                  f"-> draft {getattr(d, 'draft_id', '?')} (held)", flush=True)
        except Exception as exc:
            print(f"podcast-auto: draft failed [{m.start_ts:.0f}]: {exc}", flush=True)

    print(f"podcast-auto: {len(scheduled)} clip(s) scheduled across the week, "
          f"all HELD for approval. Nothing published.", flush=True)
    return {"episode": ep_title, "scheduled": scheduled}
