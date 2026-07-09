"""
Native clipper (master flag AGENT_CLIPPER_ENABLED, default OFF).

Episode video in, 4-5 finished vertical Reels out, entirely inside Echo. No
third-party clip platform. Claude selects the moments; mechanical layers cut and
caption (Phase 2). This module is PHASE 1: prove the SELECTION.

Pipeline (Phase 1 stops at the dry-run plan; nothing renders, nothing publishes):
  1. intake       stage the episode video to a tenant-scoped R2 key (read-only src)
  2. transcribe   word-level timestamps + speaker segments, cached on the R2 key
  3. select       Claude returns 4-5 candidate moments, scored + gated
  4. dry-run      print the ranked plan for Blake to confirm the picks

Hard lines:
  - Human approval owns everything downstream; Phase 1 only PLANS.
  - Fabrication gate is the sole authority: a hook or rationale may assert only
    what the transcript or the approved facts file already says.
  - Secrets (transcription + LLM keys) are read by env var NAME only, never logged.
"""

import os

from . import config, media_host

HOST_TENANT = "lasso_episodes"

_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")


class ClipperError(Exception):
    """A clipper step could not proceed (bad source, no transcriber, no key).
    Raised loudly rather than returning a misleading empty result."""


# ---- Part 1: episode intake ----------------------------------------------------------
def stage_episode(source, tenant=HOST_TENANT, client=None):
    """
    Resolve an episode source to a staged R2 key. READ-ONLY on the source.
      - a local video file  -> upload via media_host.host_media, return its key/url
      - an existing R2 key  -> verified with the client, returned as-is (no upload)
    Returns {"source", "r2_key", "public_url", "staged"}. Raises ClipperError when
    the source is neither a readable local file nor a resolvable R2 key.
    """
    if not source:
        raise ClipperError("clip-episode: no --source given.")
    client = client or media_host._default_client()

    if os.path.isfile(source):
        ext = os.path.splitext(source)[1].lower()
        if ext not in _VIDEO_EXTS:
            raise ClipperError(
                f"clip-episode: source is not a video file ({ext or 'no ext'}); "
                f"expected one of {', '.join(_VIDEO_EXTS)}.")
        key = media_host.key_for(source, tenant)
        url = media_host.host_media(source, tenant, client=client)
        if not url:
            raise ClipperError(
                "clip-episode: staging failed. Hosting must be armed "
                "(AGENT_HOSTING_ENABLED + R2 credentials) to stage an episode.")
        return {"source": source, "r2_key": key, "public_url": url, "staged": True}

    # Not a local file: treat the source as an already-staged R2 key.
    if client is not None:
        try:
            present = client.exists(source)
        except Exception:
            present = False
        if present:
            return {"source": source, "r2_key": source,
                    "public_url": media_host.public_url_for(source), "staged": False}

    raise ClipperError(
        f"clip-episode: source not found as a local video file or an R2 key: {source}")


# ---- orchestrator (grows one stage per part; Phase 1 ends at the dry-run plan) --------
def clip_episode(source, tenant=HOST_TENANT, render=False, client=None,
                 transcriber=None, llm=None):
    """
    Phase 1 pipeline: stage -> (transcribe -> select -> plan land in later parts).
    Returns a result dict. Renders nothing (Phase 2). Returns None while the master
    flag is OFF.
    """
    if not config.clipper_enabled():
        print("clip-episode: OFF (set AGENT_CLIPPER_ENABLED=true). Nothing done.")
        return None
    if render:
        print("clip-episode: rendering is Phase 2 and not built yet; producing the "
              "selection plan only.")

    staged = stage_episode(source, tenant, client=client)
    print(f"clip-episode: staged episode -> {staged['r2_key']} "
          f"({'uploaded' if staged['staged'] else 'already in R2'})")
    # Parts 2-4 add: transcription, moment selection, and the dry-run plan.
    return {"staged": staged}


def clip_episode_cli(argv):
    """python -m agent clip-episode --source <path-or-R2-key> [--render]"""
    source, render, i = None, False, 0
    while i < len(argv):
        if argv[i] == "--source" and i + 1 < len(argv):
            source = argv[i + 1]; i += 2; continue
        if argv[i] == "--render":
            render = True
        i += 1
    if not source:
        print("usage: python -m agent clip-episode --source <path-or-R2-key> [--render]")
        return
    try:
        clip_episode(source, render=render)
    except ClipperError as exc:
        print(str(exc))
