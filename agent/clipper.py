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

import json
import os
import re
from dataclasses import dataclass

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


# ---- Part 2: transcription with word-level timestamps (cached on the R2 key) ---------
def _cache_path(r2_key, cache_dir=None):
    cache_dir = cache_dir or config.clipper_cache_dir()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(r2_key or "")).strip("_") or "episode"
    return os.path.join(cache_dir, safe + ".transcript.json")


def _validate_transcript(t):
    """A transcript must carry a list of words each with start/end times (word-level
    timestamps are required for precise cuts and Phase 2 karaoke captions). Loud on
    malformed data, never a silent empty."""
    if not isinstance(t, dict) or not isinstance(t.get("words"), list) or not t["words"]:
        raise ClipperError("transcription returned no word-level timestamps.")
    for w in t["words"]:
        if not isinstance(w, dict) or "start" not in w or "end" not in w \
                or not str(w.get("word", "")).strip():
            raise ClipperError(
                "transcription word is missing word/start/end; word-level "
                "timestamps are required.")
    t.setdefault("segments", [])
    return t


def _default_transcriber(media_path):
    """Local faster-whisper backend with word timestamps. Raises ClipperError when
    it is not installed, naming the env-var key for an API backend (value never
    read here). Injected/mocked in tests."""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        raise ClipperError(
            "no transcriber available: install faster-whisper, or pass a "
            "transcriber that returns word-level timestamps. An API backend's key "
            f"is read from the env var named {config.CLIPPER_TRANSCRIBE_KEY_ENV}.")
    model = WhisperModel(os.environ.get("AGENT_WHISPER_MODEL", "base"))
    segments, _info = model.transcribe(media_path, word_timestamps=True)
    words, segs = [], []
    for seg in segments:
        segs.append({"speaker": "", "start": seg.start, "end": seg.end,
                     "text": seg.text})
        for w in (seg.words or []):
            words.append({"word": w.word, "start": w.start, "end": w.end})
    return {"words": words, "segments": segs}


def transcribe(r2_key, media_path=None, transcriber=None, cache_dir=None):
    """
    Word-level transcript for a staged episode, cached on the R2 key so re-runs
    never re-transcribe. Returns {"words":[{word,start,end}], "segments":[...]}.
    Needs the local media file on a cache miss; raises ClipperError when neither a
    cache nor a local file is available, or when the result has no word timestamps.
    """
    cache = _cache_path(r2_key, cache_dir)
    if os.path.isfile(cache):
        try:
            with open(cache, encoding="utf-8") as fh:
                return _validate_transcript(json.load(fh))
        except ClipperError:
            raise
        except Exception:
            pass  # unreadable cache: fall through and re-transcribe
    if not media_path or not os.path.isfile(media_path):
        raise ClipperError(
            "transcription needs the local episode file on a cache miss; re-run "
            "clip-episode with the local video path.")
    transcriber = transcriber or _default_transcriber
    result = _validate_transcript(transcriber(media_path))
    os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    return result


def transcript_text(transcript):
    """The plain spoken text of a transcript (words joined), for the LLM prompt."""
    return " ".join(w["word"].strip() for w in transcript.get("words", [])
                    if str(w.get("word", "")).strip())


def text_between(transcript, start, end):
    """The exact spoken words whose start time falls in [start, end)."""
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        return ""
    return " ".join(
        w["word"].strip() for w in transcript.get("words", [])
        if str(w.get("word", "")).strip()
        and start <= float(w.get("start", 0) or 0) < end)


def _timestamped_transcript(transcript):
    """A compact, timestamped rendering for the LLM prompt: one line per segment
    (or per word when there are no segments), so Claude can choose start/end."""
    segs = transcript.get("segments") or []
    if segs:
        return "\n".join(
            f"[{float(s.get('start', 0)):.1f}-{float(s.get('end', 0)):.1f}] "
            f"{str(s.get('text', '')).strip()}" for s in segs)
    return "\n".join(
        f"[{float(w.get('start', 0)):.1f}] {str(w.get('word', '')).strip()}"
        for w in transcript.get("words", []))


# ---- Part 3: Claude moment selection (THE CORE) --------------------------------------
@dataclass
class Moment:
    start_ts: float
    end_ts: float
    duration: float
    hook: str
    rationale: str
    bucket: str
    score: int
    transcript_text: str
    status: str = ""        # "" accepted | drop
    reason: str = ""


_SYSTEM_PROMPT = (
    "You are the moment selector for LASSO, a gym-marketing agency. You are given "
    "a timestamped transcript of one podcast/video episode. Choose the {n} strongest "
    "self-contained moments to cut into vertical Reels.\n\n"
    "Each pick MUST:\n"
    "- open on a claim, number, or question in the first ~2 seconds\n"
    "- be one complete idea that stands alone out of context\n"
    "- contain a payoff or emotional peak\n"
    "- map to something LASSO actually teaches (lead follow-up, speed to lead, the "
    "funnel diagnostic order, the six growth engines, retention, positioning, the "
    "podcast, the book, the summit, b2b/agency lessons)\n"
    "- target {lo:.0f}-{hi:.0f} seconds\n"
    "- assert NO claim, number, or stat that is not spoken in the transcript\n\n"
    "Return ONLY JSON: a list under the key \"moments\". Each moment is an object "
    "with: start_ts (seconds, number), end_ts (seconds, number), hook (the opening "
    "line, verbatim from the transcript), rationale (why it will perform, and WHY "
    "you gave the score), bucket (one of: {buckets}), score (honest integer 0-100; "
    "NOT decorative). Do not invent facts. Do not use em dashes or the word vendor."
)


def _build_prompts(transcript, count):
    from .content_categories import CATEGORIES
    system = _SYSTEM_PROMPT.format(
        n=count, lo=config.clipper_min_sec(), hi=config.clipper_max_sec(),
        buckets=", ".join(CATEGORIES))
    user = ("Transcript (timestamps in seconds):\n\n"
            + _timestamped_transcript(transcript)
            + f"\n\nReturn the {count} strongest moments as JSON.")
    return system, user


def _default_llm(system, user):
    """Default Claude backend: reads the key by env NAME (never logged), calls the
    configured model. Injected/mocked in tests."""
    key = os.environ.get(config.CLIPPER_LLM_KEY_ENV)
    if not key:
        raise ClipperError(
            f"no LLM key: set the env var named {config.CLIPPER_LLM_KEY_ENV}.")
    try:
        import anthropic
    except Exception:
        raise ClipperError(
            "anthropic SDK not installed; pass an llm callable or install anthropic.")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=config.clipper_model(), max_tokens=2000,
        system=system, messages=[{"role": "user", "content": user}])
    parts = getattr(resp, "content", []) or []
    return "".join(getattr(p, "text", "") or "" for p in parts)


def _parse_moments(raw):
    """Parse the LLM's JSON, tolerant of a ```json fence and of a bare list or a
    {"moments": [...]} wrapper. Raises ClipperError on unparseable output."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
        if not m:
            raise ClipperError("moment selection returned no parseable JSON.")
        data = json.loads(m.group(1))
    if isinstance(data, dict):
        data = data.get("moments") or data.get("data") or []
    if not isinstance(data, list):
        raise ClipperError("moment selection JSON is not a list of moments.")
    return data


def _approved_claims_for(transcript, account_key=None):
    """What a hook/rationale may assert: the transcript itself + the approved facts
    file + (if given) the tenant's approved claims."""
    claims = [transcript_text(transcript)]
    try:
        from . import rotation
        claims += list(rotation._approved_claims() or [])
    except Exception:
        pass
    if account_key:
        try:
            from . import tenants
            claims += list(tenants.tenant_approved_claims(account_key) or [])
        except Exception:
            pass
    return [c for c in claims if c]


def select_moments(transcript, llm=None, approved_claims=None, account_key=None,
                   count=None):
    """
    THE CORE. Feed the transcript to Claude and return scored, gated candidate
    moments. Returns {"accepted": [Moment sorted by score desc], "dropped": [Moment]}.

    Every candidate is checked: duration must sit in the configured window; score
    must reach AGENT_CLIPPER_SCORE_FLOOR; and the hook + rationale must pass the
    fabrication gate (assert only what the transcript or the approved facts say).
    A candidate failing any check is dropped with an honest reason, never silently.
    """
    from . import rotation
    from .content_categories import CATEGORIES
    count = count or config.clipper_target_count()
    if approved_claims is None:
        approved_claims = _approved_claims_for(transcript, account_key)
    floor = config.clipper_score_floor()
    lo, hi = config.clipper_min_sec(), config.clipper_max_sec()

    system, user = _build_prompts(transcript, count)
    raw = (llm or _default_llm)(system, user)
    candidates = _parse_moments(raw)

    accepted, dropped = [], []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        try:
            start = float(c.get("start_ts"))
            end = float(c.get("end_ts"))
        except (TypeError, ValueError):
            continue
        duration = round(end - start, 2)
        seg_text = text_between(transcript, start, end)
        hook = str(c.get("hook", "") or "").strip()
        rationale = str(c.get("rationale", "") or "").strip()
        bucket = str(c.get("bucket", "") or "").strip().lower()
        if bucket not in CATEGORIES:
            bucket = ""     # invalid/absent bucket is not guessed here
        try:
            score = int(round(float(c.get("score", 0))))
        except (TypeError, ValueError):
            score = 0

        m = Moment(start_ts=start, end_ts=end, duration=duration, hook=hook,
                   rationale=rationale, bucket=bucket, score=score,
                   transcript_text=seg_text)

        if duration <= 0 or duration < lo or duration > hi:
            m.status, m.reason = "drop", (
                f"duration {duration:g}s outside window {lo:g}..{hi:g}s")
            dropped.append(m); continue
        if score < floor:
            m.status, m.reason = "drop", f"score {score} below floor {floor:g}"
            dropped.append(m); continue
        # Fabrication gate: the hook and the rationale each may assert only what the
        # transcript or the approved facts say. Checked SEPARATELY so we never
        # introduce punctuation that would break a verbatim substring match.
        if not (rotation.is_gate_clean(hook, approved_claims)
                and rotation.is_gate_clean(rationale, approved_claims)):
            m.status, m.reason = "drop", (
                "asserts a claim not in the transcript or approved facts")
            dropped.append(m); continue
        accepted.append(m)

    accepted.sort(key=lambda x: x.score, reverse=True)
    return {"accepted": accepted, "dropped": dropped}


# ---- orchestrator (grows one stage per part; Phase 1 ends at the dry-run plan) --------
def clip_episode(source, tenant=HOST_TENANT, render=False, client=None,
                 transcriber=None, llm=None, account_key=None):
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

    media_path = staged["source"] if staged["staged"] else None
    transcript = transcribe(staged["r2_key"], media_path=media_path,
                            transcriber=transcriber)
    print(f"clip-episode: transcript {len(transcript['words'])} word(s), "
          f"{len(transcript.get('segments', []))} segment(s)")

    selection = select_moments(transcript, llm=llm, account_key=account_key)
    print(f"clip-episode: {len(selection['accepted'])} moment(s) pass, "
          f"{len(selection['dropped'])} dropped")
    # Part 4 adds: the dry-run plan print.
    return {"staged": staged, "transcript": transcript, "selection": selection}


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
