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

import importlib
import json
import os
import re
import shutil
from dataclasses import dataclass

from . import config, media_host

HOST_TENANT = "lasso_episodes"


def detect_prereqs():
    """Detect Phase 2 prerequisites at call time (never module-level so tests can
    monkeypatch). Returns a plain dict; safe to print or log — never reads a key value.

    Keys:
      HAS_FFMPEG              bool  — ffmpeg found on PATH
      FFMPEG_PATH             str|None
      HAS_FASTER_WHISPER      bool  — faster_whisper importable
      HAS_TRANSCRIBE_API_KEY  bool  — env var AGENT_TRANSCRIBE_API_KEY is non-empty
    """
    ffmpeg_path = shutil.which("ffmpeg")
    has_whisper = False
    try:
        importlib.import_module("faster_whisper")
        has_whisper = True
    except Exception:
        pass
    return {
        "HAS_FFMPEG": bool(ffmpeg_path),
        "FFMPEG_PATH": ffmpeg_path,
        "HAS_FASTER_WHISPER": has_whisper,
        "HAS_TRANSCRIBE_API_KEY": bool(os.environ.get(config.CLIPPER_TRANSCRIBE_KEY_ENV)),
    }

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


def _deepgram_transcriber(media_path):
    """Deepgram Nova-2 API backend. Used when AGENT_TRANSCRIBE_API_KEY is set."""
    import urllib.request as _urlreq
    import json as _json
    key = os.environ.get(config.CLIPPER_TRANSCRIBE_KEY_ENV, "")
    if not key:
        raise ClipperError(f"{config.CLIPPER_TRANSCRIBE_KEY_ENV} not set")
    ext = os.path.splitext(media_path)[1].lower()
    content_type = {
        ".mp4": "video/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
        ".wav": "audio/wav", ".mov": "video/quicktime",
    }.get(ext, "video/mp4")
    with open(media_path, "rb") as fh:
        audio_data = fh.read()
    url = ("https://api.deepgram.com/v1/listen"
           "?model=nova-2&smart_format=true&utterances=true&words=true")
    req = _urlreq.Request(url, data=audio_data, method="POST",
                          headers={"Authorization": f"Token {key}",
                                   "Content-Type": content_type})
    try:
        with _urlreq.urlopen(req, timeout=600) as resp:
            data = _json.loads(resp.read())
    except Exception as exc:
        raise ClipperError(f"Deepgram API error: {exc}")
    try:
        alt = data["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError) as exc:
        raise ClipperError(f"Unexpected Deepgram response shape: {exc}")
    words = [{"word": w.get("punctuated_word", w.get("word", "")),
               "start": w["start"], "end": w["end"]}
              for w in alt.get("words", [])]
    segs = [{"speaker": str(u.get("speaker", "")), "start": u["start"],
              "end": u["end"], "text": u.get("transcript", "")}
             for u in (data.get("results", {}).get("utterances") or [])]
    if not segs:
        for para in (alt.get("paragraphs", {}).get("paragraphs") or []):
            for sent in para.get("sentences", []):
                segs.append({"speaker": "", "start": sent["start"],
                              "end": sent["end"], "text": sent.get("text", "")})
    return {"words": words, "segments": segs, "source": "deepgram"}


def _default_transcriber(media_path):
    """Deepgram API when AGENT_TRANSCRIBE_API_KEY is set, else local faster-whisper."""
    if os.environ.get(config.CLIPPER_TRANSCRIBE_KEY_ENV):
        return _deepgram_transcriber(media_path)
    try:
        from faster_whisper import WhisperModel
    except Exception:
        raise ClipperError(
            "no transcriber available: install faster-whisper, or set "
            f"{config.CLIPPER_TRANSCRIBE_KEY_ENV} for the Deepgram API backend.")
    model = WhisperModel(os.environ.get("AGENT_WHISPER_MODEL", "base"))
    segments, _info = model.transcribe(media_path, word_timestamps=True)
    words, segs = [], []
    for seg in segments:
        segs.append({"speaker": "", "start": seg.start, "end": seg.end,
                     "text": seg.text})
        for w in (seg.words or []):
            words.append({"word": w.word, "start": w.start, "end": w.end})
    return {"words": words, "segments": segs, "source": "faster-whisper"}


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


# ---- Part 4: dry-run plan (the approval checkpoint; renders nothing, writes nothing) -
def _fmt_ts(seconds):
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "0:00"
    return f"{s // 60}:{s % 60:02d}"


def format_plan(selection) -> str:
    """Return the ranked selection plan as a string (no side effects)."""
    accepted = selection.get("accepted", [])
    dropped = selection.get("dropped", [])
    lines = ["\nclip-episode PLAN (SELECTION ONLY, nothing rendered, nothing written):"]
    if not accepted:
        lines.append("  no moments passed the score floor + fabrication gate.")
    for i, m in enumerate(accepted, 1):
        lines.append(f"  #{i}  score {m.score}  {m.bucket or '(no bucket)':10s}  "
                     f"[{_fmt_ts(m.start_ts)}-{_fmt_ts(m.end_ts)}]  {m.duration:g}s")
        lines.append(f"      hook : {m.hook}")
        lines.append(f"      why  : {m.rationale}")
        lines.append(f"      text : {m.transcript_text}")
    if dropped:
        lines.append("  dropped:")
        for m in dropped:
            lines.append(f"    [{_fmt_ts(m.start_ts)}-{_fmt_ts(m.end_ts)}] score {m.score}: "
                         f"{m.reason}")
    if not accepted:
        lines.append(f"  summary: 0 of {len(accepted) + len(dropped)} candidate "
                     "moment(s) passed the score, duration, and fabrication gates — "
                     "nothing to render. See the dropped reasons above.")
    else:
        lines.append(f"  summary: {len(accepted)} pick(s) held for confirmation, "
                     f"{len(dropped)} dropped.")
    return "\n".join(lines)


def print_plan(selection):
    """Print the ranked selection plan for Blake to confirm the picks BEFORE any
    video work. Returns the formatted string."""
    text = format_plan(selection)
    print(text)
    return text


# ---- Part 9: save each finished Reel as a HELD draft --------------------------------
_NEWNESS_PHRASES = (
    "new episode", "out now", "just dropped", "just released",
    "new podcast", "new video", "listen now", "watch now", "tune in",
)


def _evergreen_warning(caption):
    """Return a warning string if the caption implies recency, else empty string."""
    low = caption.lower()
    for phrase in _NEWNESS_PHRASES:
        if phrase in low:
            return f"caption may imply recency ('{phrase}') — review before approving"
    return ""


def save_clip_draft(moment, reel_path, reel_url, account_key,
                    scheduled_for="", platform="instagram",
                    episode_title="", store=None, poster=None):
    """
    Save a rendered Reel as a HELD draft in the approval store, then post its
    Slack approval card.

    Always PENDING regardless of account trust ladder; clipper drafts never
    auto-publish. Caption = moment.hook (verbatim opening line from transcript).
    Extra metadata (source, kind, score, bucket, rationale, timestamps) stored in
    source_fragments for the no-fabrication audit trail and visible in warnings.
    Returns the saved Draft.
    """
    import hashlib
    import datetime
    from .drafter import Draft, DraftStatus

    if not account_key:
        raise ClipperError("save_clip_draft: account_key is required.")

    day = scheduled_for or datetime.date.today().isoformat()
    draft_id = hashlib.sha1(
        f"clipper|{account_key}|{reel_path}|{day}".encode()
    ).hexdigest()[:10]

    caption = (moment.hook or f"[Clip: {episode_title or 'episode'}]").strip()

    frags = [
        f"source=clipper kind=reel score={moment.score} bucket={moment.bucket}",
        f"clip={moment.start_ts:.1f}-{moment.end_ts:.1f}s duration={moment.duration:.0f}s",
        moment.rationale or "",
    ]
    if episode_title:
        frags.append(f"episode={episode_title}")

    warn = []
    ew = _evergreen_warning(caption)
    if ew:
        warn.append(ew)
    warn.append(
        f"source=clipper score={moment.score} bucket={moment.bucket or '(none)'} "
        f"[{_fmt_ts(moment.start_ts)}-{_fmt_ts(moment.end_ts)}]"
    )

    draft = Draft(
        draft_id=draft_id,
        account_key=account_key,
        platform=platform,
        caption=caption,
        hashtags=[],
        creative_path=reel_path or "",
        creative_public_url=reel_url or "",
        scheduled_for=day,
        status=DraftStatus.PENDING,
        blocked_reason="",
        source_fragments=frags,
        is_story=False,
        day_key=f"clipper_{account_key}_{draft_id[:8]}",
        draft_type="clipper_reel",
        warnings=warn,
        category=moment.bucket,
    )

    from .store import PendingStore
    _store = store or PendingStore()
    _store.put(draft)

    if poster is None:
        try:
            from .slack_surface import SlackPoster
            poster = SlackPoster()
        except Exception:
            poster = None

    if poster is not None:
        try:
            resp = poster.post_approval_card(draft) or {}
            ts = str(resp.get("ts") or "")
            channel = str(resp.get("channel") or "")
            if ts:
                draft.slack_ts = ts
                draft.slack_channel = channel
                _store.put(draft)   # save ts/channel for future edit-in-place
        except Exception as exc:
            print(f"[clipper] Slack card failed: {exc}")

    return draft


# ---- Part 10: per-episode cost logging ----------------------------------------------
# Opus 4.8 pricing as of 2026 (approximate; update when pricing changes).
_COST_PER_1K_IN = 0.015   # USD per 1 000 input tokens
_COST_PER_1K_OUT = 0.075  # USD per 1 000 output tokens


def _estimate_cost(tokens_in, tokens_out):
    return round(
        (int(tokens_in or 0) / 1000.0) * _COST_PER_1K_IN
        + (int(tokens_out or 0) / 1000.0) * _COST_PER_1K_OUT,
        6)


def log_episode_cost(episode_key, tokens_in=0, tokens_out=0, transcribe_sec=0.0):
    """
    Write per-episode processing cost to the db kv store.
    Key format: clipper_cost_{day}_{episode_key[:20]}
    Returns the cost dict (safe to print — no key values).
    """
    import datetime
    from . import db

    day = datetime.date.today().isoformat()
    cost = {
        "day": day,
        "episode_key": episode_key,
        "tokens_in": int(tokens_in or 0),
        "tokens_out": int(tokens_out or 0),
        "transcribe_sec": float(transcribe_sec or 0),
        "estimated_usd": _estimate_cost(tokens_in, tokens_out),
    }
    safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", str(episode_key or ""))[:20]
    db.kv_set(f"clipper_cost_{day}_{safe_key}", json.dumps(cost))
    return cost


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

    staged = stage_episode(source, tenant, client=client)
    print(f"clip-episode: staged episode -> {staged['r2_key']} "
          f"({'uploaded' if staged['staged'] else 'already in R2'})", flush=True)

    media_path = staged["source"] if staged["staged"] else None
    print("clip-episode: transcribing (this may take 30-90s for a full episode)...",
          flush=True)
    transcript = transcribe(staged["r2_key"], media_path=media_path,
                            transcriber=transcriber)
    print(f"clip-episode: transcript {len(transcript['words'])} word(s), "
          f"{len(transcript.get('segments', []))} segment(s)", flush=True)

    selection = select_moments(transcript, llm=llm, account_key=account_key)
    print(f"clip-episode: {len(selection['accepted'])} moment(s) pass, "
          f"{len(selection['dropped'])} dropped")
    print_plan(selection)

    reels = []
    if render and config.clipper_render_enabled():
        from . import clipper_render
        render_dir = config.clipper_render_output_dir()
        for m in selection["accepted"]:
            try:
                result = clipper_render.render_clip(m, media_path, transcript, render_dir, llm=llm)
                if result:
                    reels.append({"moment": m, "reel_path": result["reel_path"]})
                    print(f"clip-episode: rendered {result['reel_path']}")
            except clipper_render.RenderError as exc:
                print(f"clip-episode: render skipped for [{_fmt_ts(m.start_ts)}]: {exc}")
    elif render:
        print("clip-episode: --render given but AGENT_CLIPPER_RENDER_ENABLED is OFF "
              "or Phase 2 is not armed. Producing the selection plan only.")

    return {"staged": staged, "transcript": transcript,
            "selection": selection, "reels": reels}


def clip_episode_cli(argv):
    """python -m agent clip-episode --source <path-or-R2-key> [--render] [--account <key>]"""
    source, render, account_key, i = None, False, None, 0
    while i < len(argv):
        if argv[i] == "--source" and i + 1 < len(argv):
            source = argv[i + 1]; i += 2; continue
        if argv[i] == "--render":
            render = True; i += 1; continue
        if argv[i] == "--account" and i + 1 < len(argv):
            account_key = argv[i + 1]; i += 2; continue
        i += 1
    if not source:
        print("usage: python -m agent clip-episode --source <path-or-R2-key> "
              "[--render] [--account <key>]")
        return

    # Default render ON when AGENT_CLIPPER_RENDER_ENABLED is armed.
    if not render and config.clipper_render_enabled():
        render = True
        print("clip-episode: AGENT_CLIPPER_RENDER_ENABLED is on — rendering clips.",
              flush=True)

    client = None
    key_id = os.environ.get(config.S3_ACCESS_KEY_ID_ENV)
    secret = os.environ.get(config.S3_SECRET_ACCESS_KEY_ENV)
    if key_id and secret and config.S3_BUCKET:
        try:
            import boto3
            from botocore.config import Config as _BC
            s3 = boto3.client("s3",
                endpoint_url=config.S3_ENDPOINT or None,
                region_name=config.S3_REGION or None,
                aws_access_key_id=key_id,
                aws_secret_access_key=secret,
                config=_BC(retries={"max_attempts": 2, "mode": "standard"}),
            )
            client = media_host._S3Client(s3, config.S3_BUCKET)
        except Exception:
            pass

    try:
        result = clip_episode(source, render=render, client=client)
    except ClipperError as exc:
        print(str(exc), flush=True)
        return
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"clip-episode: unexpected error: {exc}", flush=True)
        return

    if result is None:
        return

    selection = result.get("selection") or {}
    accepted = selection.get("accepted", [])
    reels = result.get("reels", [])
    staged = result.get("staged") or {}
    r2_key = staged.get("r2_key", source)
    episode_public_url = staged.get("public_url", "")
    episode_title = os.path.basename(r2_key).rsplit(".", 1)[0]
    acct = account_key or os.environ.get("AGENT_CLIPPER_ACCOUNT_KEY") \
           or config.episode_inbox_tenant()

    # Build moment-id -> (reel_path, reel_url) for rendered clips.
    reel_map = {}
    for r in reels:
        m = r.get("moment")
        reel_path = r.get("reel_path", "")
        reel_url = ""
        if reel_path and client:
            try:
                reel_url = media_host.host_media(reel_path, acct, client=client) or ""
                if reel_url:
                    print(f"clip-episode: uploaded {os.path.basename(reel_path)}",
                          flush=True)
            except Exception as exc:
                print(f"clip-episode: reel upload failed: {exc}", flush=True)
        if m is not None:
            reel_map[id(m)] = (reel_path, reel_url)

    if not accepted:
        print("clip-episode: no moments passed — nothing to post.", flush=True)
        return

    # Build Slack poster once for the upload + card flow.
    slack_poster = None
    slack_token = os.environ.get(config.SLACK_BOT_TOKEN_ENV, "")
    slack_channel = os.environ.get("AGENT_SLACK_CHANNEL_ID", "")
    if slack_token and slack_channel:
        try:
            from .slack_surface import SlackPoster
            slack_poster = SlackPoster(token=slack_token, channel=slack_channel)
        except Exception:
            pass

    # Post one proper approval card per accepted moment.
    posted = 0
    for m in accepted:
        reel_path, reel_url = reel_map.get(id(m), ("", episode_public_url))
        clip_ts = None

        # Upload the rendered clip directly to Slack for inline video playback.
        if reel_path and os.path.isfile(reel_path) and slack_poster:
            clip_title = (f"Clip [{_fmt_ts(m.start_ts)}-{_fmt_ts(m.end_ts)}] "
                          f"score={m.score} — {episode_title}")
            print(f"clip-episode: uploading clip to Slack "
                  f"[{_fmt_ts(m.start_ts)}-{_fmt_ts(m.end_ts)}]...", flush=True)
            resp = slack_poster.upload_clip(reel_path, title=clip_title,
                                            initial_comment=f"*{m.hook}*")
            if (resp or {}).get("ok"):
                # Get the message ts so we can thread the approval card under the video.
                files = (resp.get("files") or [{}])
                f = files[0] if files else {}
                shares = f.get("shares", {})
                ch_shares = shares.get("private", {}) or shares.get("public", {})
                for _ch, msgs in ch_shares.items():
                    if msgs:
                        clip_ts = msgs[0].get("ts")
                        break
                reel_url = f.get("permalink", reel_url)
                print(f"clip-episode: clip uploaded to Slack", flush=True)
            else:
                err = (resp or {}).get("error", "unknown")
                if err == "missing_scope":
                    print("clip-episode: Slack upload needs 'files:write' scope on the bot token. "
                          "Go to api.slack.com/apps → Echo → OAuth & Permissions, add "
                          "'files:write', reinstall, update AGENT_SLACK_BOT_TOKEN.", flush=True)
                else:
                    print(f"clip-episode: Slack upload failed ({err}): {resp}", flush=True)

        try:
            # Post the approval card (in the clip's thread when we have a ts).
            custom_poster = None
            if clip_ts and slack_poster:
                from .slack_surface import SlackPoster
                custom_poster = SlackPoster(token=slack_token, channel=slack_channel)
                custom_poster._thread_ts = clip_ts
                _orig_chat_post = custom_poster._chat_post
                def _threaded_post(text, blocks, channel=None, thread_ts=None):
                    return _orig_chat_post(text, blocks, channel,
                                           thread_ts=clip_ts)
                custom_poster._chat_post = _threaded_post

            save_clip_draft(m, reel_path, reel_url, acct,
                            episode_title=episode_title, poster=custom_poster)
            print(f"clip-episode: approval card posted "
                  f"[{_fmt_ts(m.start_ts)}-{_fmt_ts(m.end_ts)}] "
                  f"score={m.score}", flush=True)
            posted += 1
        except Exception as exc:
            print(f"clip-episode: card failed [{_fmt_ts(m.start_ts)}]: {exc}",
                  flush=True)

    if posted:
        print(f"clip-episode: {posted} clip(s) uploaded + approval card(s) posted. "
              "Tap Approve / Edit / Skip on each thread.", flush=True)
