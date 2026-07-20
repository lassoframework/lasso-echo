"""
Echo video editor (Option A: Echo directs, Higgsfield renders).

Echo is the DIRECTOR. It reuses the clipper stages (transcribe, select) and adds:
  - B-roll MANIFEST planning: from a clip's transcript, decide WHERE an AI overlay
    helps (restrained), with a Higgsfield prompt + offset + duration per beat.
  - A pluggable overlay RENDERER interface with content-hash caching (re-runs never
    re-pay) and a hard per-episode cost cap (stops and surfaces, never silently spends).
  - ASSEMBLY with ffmpeg: cut -> frame (9:16 AND 1:1) -> composite overlays ->
    optional captions (toggle: organic captioned vs caption-free ad) -> brand frame.

Why the renderer is pluggable / Claude-in-the-loop:
  Higgsfield is reachable ONLY through the interactive claude.ai MCP connector, never
  the headless Railway cron. So the render arm (AGENT_VIDEO_RENDER) is driven by a
  Claude session that reads the manifest, calls Higgsfield, and drops assets into the
  overlay cache; the pipeline then assembles them. Headless, the renderer is None (or
  the text-card fallback) and the pipeline plans + projects cost but spends nothing.

Flags (all default OFF, layered):
  AGENT_VIDEO_EDITOR_ENABLED  master
  AGENT_VIDEO_BROLL_ENABLED   plan + composite overlays
  AGENT_VIDEO_RENDER          call Higgsfield (real credits)

Nothing here publishes. Finished clips land as held review cards in #echoclaude.
No em/en dashes or hyphens in any on-screen text. Fabrication gate: every b-roll
concept must be grounded in the spoken transcript, never invented.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import config
from . import clipper_render
from . import video_assets

# ---- Minimal Broadcast bottom treatment (house standard) --------------------
# navy scrim color (LASSO V3 navy), semi-transparent gradient for legibility
_SCRIM_NAVY = (18, 30, 60)      # #121E3C
_SCRIM_MAX_ALPHA = 150          # bottom of the gradient
_HANDLE_TEXT = "@GYMMARKETINGMADESIMPLE"
# logo (LASSO wordmark) sized by frame WIDTH; other insets by HEIGHT
_LOGO_W_FRAC = 0.20
_INSET_FRAC = 0.036
_HANDLE_FS_FRAC = 0.014
_SCRIM_H_FRAC = 0.09

# ---- Word Highlight caption style (house standard) --------------------------
_WH_ACTIVE_BGR = "2A2AFF"       # ASS &HBBGGRR for red (255,42,42)
_WH_WHITE_BGR = "FFFFFF"
_WH_WORDS_PER_GROUP = 3
_WH_FONT_FRAC = 0.062           # caption font size as fraction of frame height


class VideoEditorError(Exception):
    """A video-editor stage could not proceed."""


# ---- aspect ratios ----------------------------------------------------------

ASPECT_DIMS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

# Platforms each aspect targets (for the review card).
ASPECT_PLATFORMS = {
    "9:16": "Reels, TikTok, Shorts, Stories",
    "1:1": "Feed (Instagram, Facebook, LinkedIn)",
}


def _dims(aspect):
    if aspect not in ASPECT_DIMS:
        raise VideoEditorError(f"unknown aspect '{aspect}' (use 9:16 or 1:1)")
    return ASPECT_DIMS[aspect]


# ---- Part 1: B-roll manifest planning ---------------------------------------

# A visual is worth adding on maybe a third of lines, never every sentence.
# The planner is capped hard by config.video_broll_cap().
_BROLL_MIN_OFFSET = 4.0     # earliest overlay offset from clip start
_BROLL_MIN_GAP = 8.0        # minimum seconds between two overlays
_DEFAULT_OVERLAY_DUR = 4.0  # seconds an overlay stays on screen

_HOUSE_STYLE_WRAP = (
    "Editorial cinematic b-roll for a gym marketing brand. "
    "Concrete real-world scene from a boutique gym owner's world, "
    "documentary photography style, natural light, shallow depth of field. "
    "Palette leans navy #121E3C and warm cream #FAF6F0 with one restrained red accent. "
    "No text, no words, no captions, no logos, no watermarks anywhere in frame. "
    "Not clip art, not an icon, not an infographic. "
    "Busy professional gym owners and members, never competitive athletes. "
    "Scene: "
)


def _clip_words(transcript, clip_start, clip_end):
    return [
        w for w in transcript.get("words", [])
        if float(w.get("start", 0)) >= clip_start - 0.1
        and float(w.get("start", 0)) <= clip_end + 0.1
    ]


def _clip_text(transcript, clip_start, clip_end):
    return " ".join(
        str(w.get("word", "")).strip()
        for w in _clip_words(transcript, clip_start, clip_end)
        if str(w.get("word", "")).strip()
    )


_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokens(text):
    return set(_WORD_RE.findall((text or "").lower()))


def build_higgsfield_prompt(visual):
    """Wrap a scene description in the house-style guidance to produce the final
    Higgsfield prompt. Strips any dashes so no on-image text rule stays intact."""
    scene = str(visual or "").strip()
    scene = scene.replace("—", " ").replace("–", " ").replace("-", " ")
    scene = re.sub(r"(?i)\bvendors?\b", "partner", scene)
    scene = re.sub(r"\s+", " ", scene).strip()
    if not scene.endswith("."):
        scene += "."
    return _HOUSE_STYLE_WRAP + scene


def _parse_manifest_json(raw):
    """Parse the planner's JSON list, tolerant of a markdown fence / wrapper."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except Exception:
            return None
    if isinstance(data, dict):
        data = data.get("beats") or data.get("broll") or data.get("moments") or []
    return data if isinstance(data, list) else None


_DIGIT_RE = re.compile(r"\d")


def _fabrication_ok(concept, source_span, clip_tokens):
    """A b-roll concept is grounded when its concept/source words are drawn from
    the clip transcript, never invented. We require most meaningful concept tokens
    to be present in the clip's spoken words.

    Numbers get NO length exemption: any numeric token (a stat/percentage/figure)
    must appear verbatim in the transcript, else the beat is rejected. This closes
    the gap where an invented figure like "70" (too short for the len>3 filter)
    could otherwise ride onto a still card as a fabricated stat."""
    concept_tokens = _tokens(concept) | _tokens(source_span)
    # any numeric token must be spoken verbatim in the clip
    for t in concept_tokens:
        if _DIGIT_RE.search(t) and t not in clip_tokens:
            return False
    # ignore tiny filler words (non-numeric) when judging grounding
    meaningful = {t for t in concept_tokens if len(t) > 3 and not _DIGIT_RE.search(t)}
    if not meaningful:
        # only numeric/short tokens: grounded only if every one was verbatim (checked above)
        return bool(concept_tokens)
    hits = sum(1 for t in meaningful if t in clip_tokens)
    return hits >= max(1, int(len(meaningful) * 0.5))


_STILL_SIGNAL_RE = re.compile(
    r"\d|\bpercent\b|\bdollars?\b|\bstep\b|\bframework\b|\bthree\b|\bfour\b|"
    r"\bfive\b|\bstat\b|\bnumber\b|\bquote\b|\brate\b|\bformula\b")


def _classify_route(concept, source_span, visual=""):
    """Route a beat: 'still' when the beat is a stat/number/quote/framework meant
    to be READ (Nano Banana card); otherwise 'motion' scene/action (Higgsfield
    video). Restraint on stills: only when there is a readable signal."""
    blob = f"{concept} {source_span}".lower()
    return "still" if _STILL_SIGNAL_RE.search(blob) else "motion"


def snap_to_word_boundaries(moment, transcript, window=1.2):
    """Adjust a moment's start/end to the nearest word boundary in the transcript
    so clips never cut mid-word. Start snaps to the nearest word START, end to the
    nearest word END, each within `window` seconds. Mutates and returns the moment.
    No-op when no word falls in the window (keeps the original timestamp)."""
    words = transcript.get("words", [])
    if not words:
        return moment
    s = float(moment.start_ts)
    e = float(moment.end_ts)

    # Start: if it lands INSIDE a word, expand out to that word's start (never clip
    # the front of a word). Otherwise snap to the nearest word start within window.
    inside_s = [w for w in words
                if float(w.get("start", 0)) <= s <= float(w.get("end", 0))]
    if inside_s:
        moment.start_ts = min(float(w.get("start", 0)) for w in inside_s)
    else:
        near = [float(w.get("start", 0)) for w in words
                if abs(float(w.get("start", 0)) - s) <= window]
        if near:
            moment.start_ts = min(near, key=lambda t: abs(t - s))

    # End: if inside a word, expand out to that word's end (never clip the tail).
    inside_e = [w for w in words
                if float(w.get("start", 0)) <= e <= float(w.get("end", 0))]
    if inside_e:
        moment.end_ts = max(float(w.get("end", 0)) for w in inside_e)
    else:
        near = [float(w.get("end", 0)) for w in words
                if abs(float(w.get("end", 0)) - e) <= window]
        if near:
            moment.end_ts = min(near, key=lambda t: abs(t - e))

    try:
        moment.duration = round(moment.end_ts - moment.start_ts, 2)
    except Exception:
        pass
    return moment


def _dedup_and_space(beats, clip_dur):
    """Sort by offset, enforce min offset / min gap / in-bounds. Drops are logged
    (never silent) so the review reflects what was discarded."""
    out = []
    last = -1e9
    for b in sorted(beats, key=lambda x: x["offset"]):
        off = b["offset"]
        dur = b.get("duration", _DEFAULT_OVERLAY_DUR)
        if off < _BROLL_MIN_OFFSET:
            print(f"[video] b-roll beat dropped (before {_BROLL_MIN_OFFSET:.0f}s "
                  f"min offset): '{b.get('concept')}'", flush=True)
            continue
        if off + dur > clip_dur - 1.0:
            print(f"[video] b-roll beat dropped (runs past clip end): "
                  f"'{b.get('concept')}'", flush=True)
            continue
        if off - last < _BROLL_MIN_GAP:
            print(f"[video] b-roll beat dropped (within {_BROLL_MIN_GAP:.0f}s of "
                  f"prior beat): '{b.get('concept')}'", flush=True)
            continue
        out.append(b)
        last = off
    return out


def _plan_fallback(transcript, clip_start, clip_end, cap):
    """Heuristic planner (no LLM): pick evenly spaced beats anchored on the
    longest concrete words spoken near each position. Grounded by construction."""
    clip_dur = clip_end - clip_start
    words = _clip_words(transcript, clip_start, clip_end)
    if not words or clip_dur < _BROLL_MIN_OFFSET + 2:
        return []

    # target ~ one beat per 18s of clip, capped
    n = max(1, min(cap, int(clip_dur // 18) or 1))
    beats = []
    for i in range(n):
        frac = (i + 1) / (n + 1)
        target_t = clip_start + clip_dur * frac
        window = sorted(
            [w for w in words if abs(float(w.get("start", 0)) - target_t) < 5.0],
            key=lambda w: len(str(w.get("word", "")).strip()),
            reverse=True,
        )
        concept_words = [
            str(w.get("word", "")).strip()
            for w in window[:3]
            if len(str(w.get("word", "")).strip()) > 3
        ]
        if not concept_words:
            continue
        concept = " ".join(concept_words)
        beats.append({
            "offset": round(clip_dur * frac, 2),
            "duration": _DEFAULT_OVERLAY_DUR,
            "concept": concept,
            "visual": f"a gym owner scene evoking {concept.lower()}",
            "source_span": concept,
        })
    return _dedup_and_space(beats, clip_dur)


def plan_broll_manifest(moment, transcript, llm=None, cap=None, kind=None):
    """
    Plan the b-roll overlay beats for one clip.

    Returns a manifest dict:
      {
        "clip_start", "clip_end", "clip_dur",
        "kind": "video"|"image",
        "beats": [ {offset, duration, concept, visual, source_span, prompt} ],
        "projected_cost": float (credits),
        "cost_per_overlay": float,
        "cap": int,
        "dropped_for_cap": int,   # beats the planner wanted beyond the cap
      }

    Restraint is enforced: at most `cap` beats, spaced out, never one per line.
    Every beat is fabrication-gated against the clip transcript.
    """
    cap = config.video_broll_cap() if cap is None else cap
    kind = kind or config.video_broll_kind()
    clip_start = float(getattr(moment, "start_ts", 0))
    clip_end = float(getattr(moment, "end_ts", clip_start))
    clip_dur = clip_end - clip_start
    clip_tokens = _tokens(_clip_text(transcript, clip_start, clip_end))

    planned = []
    if llm:
        clip_text = _clip_text(transcript, clip_start, clip_end)
        system = (
            "You are a restrained video editor choosing B-roll overlay moments for "
            "one short clip from a gym-marketing podcast. A good editor adds B-roll on "
            "maybe a third of lines, NEVER every sentence. Choose only the moments where "
            "a concrete visual makes the point land harder. "
            "Return ONLY a JSON list. Each object: "
            "'offset' (float seconds from clip start), "
            "'duration' (float seconds, 3 to 5), "
            "'concept' (2 to 5 words naming the idea, drawn from what is actually said), "
            "'visual' (one sentence describing a CONCRETE gym-owner scene to show, no text "
            "in the image), "
            "'source_span' (the spoken words that triggered it, verbatim from the transcript), "
            "'route' (either 'motion' for a scene/action/movement beat, or 'still' for a "
            "stat/number/quote/framework beat that is meant to be READ). Prefer 'motion'; "
            "use 'still' only when the point is a number or a quotable line. "
            "Never invent a concept not spoken. No dashes of any kind in any field."
        )
        user = (
            f"Clip is {clip_dur:.0f} seconds. Choose at most {cap} B-roll beats.\n"
            f"First beat no earlier than {_BROLL_MIN_OFFSET:.0f}s; beats at least "
            f"{_BROLL_MIN_GAP:.0f}s apart.\n\n"
            f"Transcript of the clip:\n{clip_text}\n\n"
            f"Return only the JSON list."
        )
        try:
            parsed = _parse_manifest_json(llm(system, user)) or []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                try:
                    off = float(item.get("offset", 0))
                    dur = float(item.get("duration", _DEFAULT_OVERLAY_DUR))
                except (TypeError, ValueError):
                    continue
                concept = str(item.get("concept", "")).strip()
                visual = str(item.get("visual", "")).strip()
                span = str(item.get("source_span", "")).strip()
                if not concept or not visual:
                    continue
                if not _fabrication_ok(concept, span, clip_tokens):
                    print(f"[video] b-roll beat dropped (not grounded): '{concept}'",
                          flush=True)
                    continue
                route = str(item.get("route", "")).strip().lower()
                if route not in ("motion", "still"):
                    route = _classify_route(concept, span, visual)
                clamped = max(2.0, min(6.0, dur))
                if abs(clamped - dur) > 0.01:
                    print(f"[video] b-roll beat duration clamped {dur:.1f}s -> "
                          f"{clamped:.1f}s: '{concept}'", flush=True)
                planned.append({
                    "offset": round(off, 2),
                    "duration": clamped,
                    "concept": concept,
                    "visual": visual,
                    "source_span": span,
                    "route": route,
                })
        except Exception as exc:
            print(f"[video] planner LLM error: {exc} — using heuristic fallback",
                  flush=True)

    if not planned:
        planned = _plan_fallback(transcript, clip_start, clip_end, cap)

    planned = _dedup_and_space(planned, clip_dur)

    # ensure every beat carries a route (fallback beats classify heuristically)
    for b in planned:
        if b.get("route") not in ("motion", "still"):
            b["route"] = _classify_route(b.get("concept", ""),
                                         b.get("source_span", ""), b.get("visual", ""))

    # Per-route caps: motion beats -> Higgsfield video cap; still beats -> Nano cap.
    stills_cap = config.video_stills_cap()
    motion, still = [], []
    dropped_for_cap = 0
    for b in planned:
        if b["route"] == "still":
            if len(still) < stills_cap:
                still.append(b)
            else:
                dropped_for_cap += 1
        else:
            if len(motion) < cap:
                motion.append(b)
            else:
                dropped_for_cap += 1
    if dropped_for_cap:
        print(f"[video] b-roll plan capped: dropped {dropped_for_cap} beat(s) beyond "
              f"caps (motion {cap}, stills {stills_cap})", flush=True)

    beats = sorted(motion + still, key=lambda x: x["offset"])
    for b in beats:
        if b["route"] == "still":
            b["kind"] = "image"
            # on-card text is the grounded concept, scrubbed for on-screen rules
            b["card_text"] = clipper_render.scrub_onscreen(b["concept"].upper())
            b["prompt"] = _build_still_prompt(b)
        else:
            b["kind"] = "video"
            b["prompt"] = build_higgsfield_prompt(b["visual"])

    cost_video = config.video_cost_per_overlay()
    cost_still = config.video_cost_per_still()
    projected = round(len(motion) * cost_video + len(still) * cost_still, 2)
    return {
        "clip_start": clip_start,
        "clip_end": clip_end,
        "clip_dur": clip_dur,
        "kind": kind,
        "beats": beats,
        "motion_count": len(motion),
        "still_count": len(still),
        "cost_per_overlay": cost_video,
        "cost_per_still": cost_still,
        "projected_cost": projected,
        "cap": cap,
        "stills_cap": stills_cap,
        "dropped_for_cap": dropped_for_cap,
    }


def _build_still_prompt(beat):
    """House-style Nano Banana card prompt for a stat/quote/framework beat. The
    on-card text is the grounded, scrubbed concept (fabrication-safe). Palette and
    layout follow the LASSO V3 house style; no dashes, no vendor."""
    text = beat.get("card_text") or clipper_render.scrub_onscreen(
        beat.get("concept", "").upper())
    return (
        "LASSO V3 house-style editorial card, vertical. "
        "Navy #121E3C background, one bold headline in white set large and "
        "left-aligned, one restrained red #FF0000 accent element only. "
        "Anton or a bold condensed sans headline. Clean, magazine editorial, "
        "not clip art, not an infographic, no photo. "
        "The headline text reads exactly: "
        f"\"{text}\". "
        "No other text anywhere. No dashes, no hyphens."
    )


def project_episode_cost(manifests):
    """Total projected Higgsfield credit cost across a list of clip manifests."""
    return round(sum(m.get("projected_cost", 0) for m in manifests), 2)


# ---- Part 2: overlay renderer interface + cache -----------------------------

class RenderBudget:
    """Episode-level overlay render budget (the hard cost guard).

    One budget is created per episode and threaded through every clip's
    render_overlays call, so the WHOLE episode never renders more than `total`
    overlays no matter how many clips it has. Cached hits are free and do not
    decrement. When the budget is exhausted the render loop stops and logs
    (surfaces) rather than silently spending past it.
    """

    def __init__(self, total):
        self.total = max(0, int(total))
        self.used = 0

    @property
    def remaining(self):
        return max(0, self.total - self.used)


def overlay_cache_key(beat, kind):
    """Content hash of an overlay beat: same prompt+kind+duration -> same key,
    so a re-run reuses the cached asset and never re-pays."""
    basis = f"{kind}|{beat.get('duration')}|{beat.get('prompt', beat.get('visual', ''))}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def overlay_cache_path(cache_dir, beat, kind):
    """Absolute path where a rendered overlay asset for this beat is cached."""
    ext = ".mp4" if kind == "video" else ".png"
    return os.path.join(cache_dir, overlay_cache_key(beat, kind) + ext)


def _beat_kind(beat, manifest):
    """A beat's asset kind: still beats -> image (Nano card), else the manifest
    default (motion video)."""
    if beat.get("kind"):
        return beat["kind"]
    if beat.get("route") == "still":
        return "image"
    return manifest.get("kind", "video")


def render_overlays(manifest, renderer=None, cache_dir=None, cap=None, budget=None,
                    still_renderer=None, still_budget=None):
    """
    Turn manifest beats into rendered overlay assets, ROUTING each beat:
      route == 'motion' -> `renderer` + `budget`      (Higgsfield video)
      route == 'still'  -> `still_renderer` + `still_budget` (Nano Banana card)
    A beat with no route defaults to motion (backward compatible).

    renderer/still_renderer: callable(beat, out_path, kind) that WRITES the asset.
      In an interactive Claude session these drive Higgsfield / the Gemini card
      pipeline; None means only already-cached assets are used (no spend).

    Caching: content-hash cache path per beat. A cache HIT is reused (never
    re-pays). A cache MISS calls the route's renderer, decrementing that route's
    budget. When a route's episode budget is exhausted the loop skips further NEW
    renders of that route and LOGS (surfaces), never spending past it.

    cap=int (only when no budget given) -> per-CALL motion guard that RAISES when
    exceeded. Used in isolation/tests.

    Returns overlay dicts {offset, duration, asset_path, kind, route, cached}.
    """
    cache_dir = cache_dir or config.video_overlay_cache_dir()
    cap = config.video_broll_cap() if cap is None else cap
    os.makedirs(cache_dir, exist_ok=True)

    overlays = []
    new_motion = 0
    for beat in manifest.get("beats", []):
        route = beat.get("route", "motion")
        kind = _beat_kind(beat, manifest)
        rndr = still_renderer if route == "still" else renderer
        bdgt = still_budget if route == "still" else budget
        path = overlay_cache_path(cache_dir, beat, kind)

        if os.path.isfile(path) and os.path.getsize(path) > 0:
            overlays.append({
                "offset": beat["offset"], "duration": beat["duration"],
                "asset_path": path, "kind": kind, "route": route, "cached": True,
            })
            continue
        if rndr is None:
            print(f"[video] {route} overlay not cached and no renderer: skipping "
                  f"'{beat.get('concept')}'", flush=True)
            continue
        if bdgt is not None:
            if bdgt.remaining <= 0:
                print(f"[video] episode {route} cap reached ({bdgt.total}); skipping "
                      f"remaining {route} overlay(s) this episode", flush=True)
                continue
        elif route == "motion" and new_motion >= cap:
            raise VideoEditorError(
                f"b-roll cost cap reached ({cap} renders). Stopping before spending "
                f"more. Raise AGENT_VIDEO_BROLL_CAP to allow more overlays.")
        rndr(beat, path, kind)
        if not (os.path.isfile(path) and os.path.getsize(path) > 0):
            print(f"[video] renderer produced no asset for '{beat.get('concept')}'",
                  flush=True)
            continue
        if route == "motion":
            new_motion += 1
        if bdgt is not None:
            bdgt.used += 1
        overlays.append({
            "offset": beat["offset"], "duration": beat["duration"],
            "asset_path": path, "kind": kind, "route": route, "cached": False,
        })
    return overlays


def still_card_renderer(beat, out_path, kind):
    """Nano Banana still-card renderer: reuses Echo's EXISTING creative_studio
    Gemini pipeline (same model config + key as organic cards, one source of
    truth). Writes a PNG card to out_path. Raises if the pipeline is unavailable
    so the caller can skip the beat rather than spend blindly."""
    from . import creative_studio
    client = creative_studio._default_client()
    if client is None:
        raise VideoEditorError(
            "still card needs the creative_studio Gemini pipeline "
            "(AGENT_CREATIVE_STUDIO_ENABLED + AGENT_NANO_API_KEY).")
    image_bytes = client.generate_image(prompt=beat["prompt"], model=config.NANO_MODEL)
    with open(out_path, "wb") as fh:
        fh.write(image_bytes)
    return out_path


def textcard_renderer(beat, out_path, kind):
    """Headless fallback renderer: a branded text card (no Higgsfield, no credits).
    Reuses the clipper_broll card so the pipeline still produces overlays when
    Higgsfield is not available."""
    from . import clipper_broll
    clipper_broll._make_broll_card(beat.get("concept", ""), out_path,
                                   duration=beat.get("duration", _DEFAULT_OVERLAY_DUR))
    return out_path


# ---- Part 3: assembly -------------------------------------------------------

def _prepare_overlay(asset_path, out_path, width, height, duration, kind):
    """Normalize any overlay asset to a WxH mp4 of exactly `duration` seconds.
    Image -> Ken-Burns slow zoom (motion). Video -> fill-scale + center-crop,
    trimmed/looped to duration. Keeps the pipeline's composite step uniform."""
    ffmpeg = clipper_render._ffmpeg()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fps = 30
    total_frames = int(duration * fps)

    if kind == "image":
        # zoompan Ken-Burns over the still, filling WxH.
        vf = (
            f"scale={width*2}:-2,"
            f"zoompan=z='min(zoom+0.0009,1.15)':d={total_frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps},"
            f"trim=duration={duration},setpts=PTS-STARTPTS"
        )
        cmd = [
            ffmpeg, "-y", "-loop", "1", "-i", asset_path,
            "-vf", vf, "-t", str(duration),
            "-c:v", "libx264", "-crf", "22", "-preset", "fast", "-pix_fmt", "yuv420p",
            out_path,
        ]
    else:
        vf = (
            f"scale=w='if(gt(iw/ih,{width}/{height}),-2,{width})':"
            f"h='if(gt(iw/ih,{width}/{height}),{height},-2)',"
            f"scale=w='if(lt(iw,{width}),{width},iw)':"
            f"h='if(lt(ih,{height}),{height},ih)',"
            f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2,"
            f"trim=duration={duration},setpts=PTS-STARTPTS"
        )
        cmd = [
            ffmpeg, "-y", "-stream_loop", "-1", "-i", asset_path,
            "-vf", vf, "-t", str(duration), "-an",
            "-c:v", "libx264", "-crf", "22", "-preset", "fast", "-pix_fmt", "yuv420p",
            out_path,
        ]
    clipper_render._run(cmd, "prepare_overlay")
    return out_path


def _composite_overlays(video_path, overlays, output_path, width, height, work_dir):
    """Composite prepared overlays (full-frame cutaways) over video_path at their
    offsets. Main clip audio continues under the overlays. Returns video_path
    unchanged when there are no overlays."""
    if not overlays:
        return video_path
    ffmpeg = clipper_render._ffmpeg()
    os.makedirs(work_dir, exist_ok=True)

    prepared = []
    for i, ov in enumerate(overlays):
        p = os.path.join(work_dir, f"ovprep_{i:02d}.mp4")
        _prepare_overlay(ov["asset_path"], p, width, height,
                         ov["duration"], ov.get("kind", "video"))
        prepared.append({"path": p, "offset": ov["offset"]})

    inputs = ["-i", video_path]
    for pr in prepared:
        inputs += ["-i", pr["path"]]

    filter_parts = []
    current = "0:v"
    for i, pr in enumerate(prepared):
        idx = i + 1
        off = float(pr["offset"])
        shifted = f"s{i}"
        out_label = f"o{i}"
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{off:.3f}/TB[{shifted}]")
        filter_parts.append(
            f"[{current}][{shifted}]overlay=x=0:y=0:eof_action=pass[{out_label}]")
        current = out_label

    cmd = (
        [ffmpeg, "-y"] + inputs
        + [
            "-filter_complex", ";".join(filter_parts),
            "-map", f"[{current}]", "-map", "0:a",
            "-c:v", "libx264", "-crf", "22", "-preset", "fast",
            "-c:a", "copy",
            output_path,
        ]
    )
    clipper_render._run(cmd, "composite_overlays")
    return output_path


def _make_scrim_png(width, scrim_h, out_path):
    """Vertical gradient PNG: transparent at top -> navy@_SCRIM_MAX_ALPHA at bottom."""
    from PIL import Image
    img = Image.new("RGBA", (max(2, width), max(2, scrim_h)), (0, 0, 0, 0))
    px = img.load()
    r, g, b = _SCRIM_NAVY
    for y in range(img.height):
        a = int(_SCRIM_MAX_ALPHA * (y / max(1, img.height - 1)))
        for x in range(img.width):
            px[x, y] = (r, g, b, a)
    img.save(out_path)
    return out_path


def apply_bottom_treatment(input_path, output_path, width, height, work_dir):
    """
    Minimal Broadcast bottom treatment (house standard, every clip):
      - soft bottom gradient scrim (transparent -> navy@150 over bottom ~9%)
      - LASSO red mark bottom-LEFT (~4.2% frame-h, 92% opacity, ~3.6% inset)
      - @GYMMARKETINGMADESIMPLE bottom-RIGHT, Oswald tracked caps, white ~92%
      - NO solid bar, NO red line
    Uses ffmpeg 'ih'/'iw' where possible so it holds for 9:16 and 1:1.
    """
    clipper_render._require_render()
    os.makedirs(work_dir, exist_ok=True)
    ffmpeg = clipper_render._ffmpeg()

    logo_w = int(width * _LOGO_W_FRAC)
    inset = int(height * _INSET_FRAC)
    handle_fs = int(height * _HANDLE_FS_FRAC)
    scrim_h = int(height * _SCRIM_H_FRAC)

    scrim_png = os.path.join(work_dir, "scrim.png")
    _make_scrim_png(width, scrim_h, scrim_png)
    logo_png = video_assets.lasso_logo_path()
    oswald = video_assets.oswald_font_path()

    # tracked caps: insert a thin space (U+2009) between characters for spacing
    handle_tracked = " ".join(list(_HANDLE_TEXT))
    handle_esc = handle_tracked.replace("\\", "\\\\").replace(":", "\\:").replace(
        "'", "\\'")
    font_esc = oswald.replace("\\", "\\\\").replace(":", "\\:")

    filt = (
        f"[2:v]scale={logo_w}:-1,format=rgba,colorchannelmixer=aa=0.95[logo];"
        f"[0:v][1:v]overlay=x=0:y=H-{scrim_h}[a];"
        f"[a][logo]overlay=x={inset}:y=H-{inset}-overlay_h[b];"
        f"[b]drawtext=fontfile='{font_esc}':text='{handle_esc}':"
        f"fontcolor=white@0.92:fontsize={handle_fs}:"
        f"x=W-tw-{inset}:y=H-{inset}-th[v]"
    )
    cmd = [
        ffmpeg, "-y",
        "-i", input_path,
        "-i", scrim_png,
        "-i", logo_png,
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast",
        "-c:a", "copy",
        output_path,
    ]
    clipper_render._run(cmd, "bottom_treatment")
    return output_path


def _caption_margin_v(height, face_bottom_frac=None):
    """Bottom-anchored MarginV (px) placing captions in the lower third, below the
    speaker's face when detected, and clear of the bottom treatment."""
    scrim_top_frac = 1.0 - _SCRIM_H_FRAC          # captions must stay above this
    default_center = 0.72                          # lower third
    if face_bottom_frac and face_bottom_frac > 0.55:
        center = min(scrim_top_frac - 0.08, face_bottom_frac + 0.07)
    else:
        center = default_center
    center = max(0.62, min(center, scrim_top_frac - 0.06))
    return int(height * (1.0 - center))


def _make_word_highlight_ass(transcript, start_ts, end_ts, ass_path,
                             width, height, margin_v):
    """Word Highlight ASS: Anton ALL CAPS, groups of _WH_WORDS_PER_GROUP, the ONE
    active (currently spoken) word in brand RED, the rest white, heavy outline +
    shadow. One event per word so exactly one line is visible (no ghost/duplicate).
    Fabrication-safe: only words within the segment; dashes/vendor scrubbed."""
    start_ts = float(start_ts)
    end_ts = float(end_ts)
    words = [
        w for w in transcript.get("words", [])
        if float(w.get("start", 0)) >= start_ts - 0.05
        and float(w.get("start", 0)) < end_ts + 0.05
    ]
    chunks = [words[i:i + _WH_WORDS_PER_GROUP]
              for i in range(0, len(words), _WH_WORDS_PER_GROUP)]

    font_size = int(height * _WH_FONT_FRAC)
    outline = max(4, int(font_size * 0.09))
    shadow = max(2, int(font_size * 0.04))
    lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {width}", f"PlayResY: {height}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # Anton, white default, heavy black outline + shadow, bottom-center
        f"Style: WH,Anton,{font_size},&H00{_WH_WHITE_BGR},&H00{_WH_WHITE_BGR},"
        f"&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},"
        f"2,40,40,{margin_v},0",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for chunk in chunks:
        for word_idx, w in enumerate(chunk):
            w_start = max(0.0, float(w.get("start", 0)) - start_ts)
            w_end = max(w_start + 0.05, float(w.get("end", 0)) - start_ts)
            parts = []
            for ci, cw in enumerate(chunk):
                text = clipper_render.scrub_onscreen(
                    str(cw.get("word", "") or "").strip().upper())
                if not text:
                    continue
                color = _WH_ACTIVE_BGR if ci == word_idx else _WH_WHITE_BGR
                parts.append(f"{{\\c&H00{color}&}}{text}")
            if not parts:
                continue
            lines.append(
                f"Dialogue: 0,{clipper_render._fmt_ass_ts(w_start)},"
                f"{clipper_render._fmt_ass_ts(w_end)},WH,,0,0,0,,{' '.join(parts)}")

    os.makedirs(os.path.dirname(os.path.abspath(ass_path)), exist_ok=True)
    with open(ass_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return ass_path


def burn_word_highlight(input_path, output_path, transcript, start_ts, end_ts,
                        width, height, face_bottom_frac=None):
    """Burn Word Highlight captions (Anton, one red active word) into the video,
    placed in the lower third below the detected face. Loads Anton via fontsdir."""
    clipper_render._require_render()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    margin_v = _caption_margin_v(height, face_bottom_frac)

    with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as tf:
        ass_path = tf.name
    try:
        _make_word_highlight_ass(transcript, start_ts, end_ts, ass_path,
                                 width, height, margin_v)
        safe = os.path.abspath(ass_path).replace("\\", "/").replace(":", "\\:")
        fonts = video_assets.FONTS_DIR.replace("\\", "/").replace(":", "\\:")
        cmd = [
            clipper_render._ffmpeg(), "-y", "-i", input_path,
            "-vf", f"ass={safe}:fontsdir={fonts}",
            "-c:v", "libx264", "-crf", "20", "-preset", "fast",
            "-c:a", "copy",
            output_path,
        ]
        clipper_render._run(cmd, "burn_word_highlight")
    finally:
        try:
            os.unlink(ass_path)
        except OSError:
            pass
    return output_path


def assemble_clip(moment, media_path, transcript, overlays, output_dir, base,
                  aspect="9:16", captioned=True):
    """
    Assemble one finished clip (house standard):
      cut -> frame(aspect) -> composite overlays -> [Word Highlight captions] ->
      Minimal Broadcast bottom treatment.
    Real host footage is the spine; overlays are cutaways only, never AI-altering
    the host. captioned=False produces the caption-FREE ad cut from the same
    timeline (it still gets the bottom treatment). Requires the render flag.
    """
    width, height = _dims(aspect)
    tag = aspect.replace(":", "x") + ("_cap" if captioned else "_ad")
    work = os.path.join(output_dir, f"{base}_{tag}_work")
    os.makedirs(work, exist_ok=True)

    framed = os.path.join(work, "framed.mp4")
    composited = os.path.join(work, "composited.mp4")
    captioned_out = os.path.join(work, "captioned.mp4")
    final_out = os.path.join(output_dir, f"{base}_{tag}.mp4")

    # cut (stream copy) then frame to the target aspect
    clipper_render.cut_segment(media_path, moment.start_ts, moment.end_ts, work,
                               label="src")
    cut_files = [f for f in os.listdir(work) if f.startswith("src_")]
    if not cut_files:
        raise VideoEditorError("cut_segment produced no file")
    src_cut = os.path.join(work, cut_files[0])

    clipper_render.frame_vertical(src_cut, framed, width=width, height=height)

    # Detect the speaker's face on the framed host footage so captions dodge it.
    face_bottom = video_assets.detect_face_bottom_frac(framed)

    stage = framed
    if overlays:
        _composite_overlays(framed, overlays, composited, width, height, work)
        stage = composited

    if captioned:
        burn_word_highlight(stage, captioned_out, transcript,
                            moment.start_ts, moment.end_ts,
                            width, height, face_bottom_frac=face_bottom)
        stage = captioned_out

    apply_bottom_treatment(stage, final_out, width, height, work)
    return final_out


# ---- Part 4: orchestrator ---------------------------------------------------

def edit_episode(source, render=False, client=None, transcriber=None, llm=None,
                 account_key=None, renderer=None, aspects=None):
    """
    Full video-editor pipeline for one episode:
      stage -> transcribe -> select -> per clip: plan b-roll manifest ->
      render overlays (if renderer + render flag) -> assemble 9:16 + 1:1,
      captioned + ad -> return structured result (Slack surfacing is done by
      the CLI wrapper).

    Gated by AGENT_VIDEO_EDITOR_ENABLED (returns None when OFF). B-roll planning
    gated by AGENT_VIDEO_BROLL_ENABLED. Overlays are only rendered when a
    `renderer` is supplied AND AGENT_VIDEO_RENDER is on (real credit spend);
    otherwise cached overlays are reused and nothing is spent.

    Returns:
      {
        "staged", "transcript", "selection",
        "aspects": [...],
        "projected_cost": float,
        "clips": [ {moment, manifest, overlays, files: {aspect_capmode: path}} ],
      }
    """
    from . import clipper

    if not config.video_editor_enabled():
        print("video-episode: OFF (set AGENT_VIDEO_EDITOR_ENABLED=true). Nothing done.",
              flush=True)
        return None

    aspects = aspects or config.video_aspects()

    staged = clipper.stage_episode(source, client=client)
    media_path = staged["source"] if staged.get("staged") else None
    print("video-episode: transcribing...", flush=True)
    transcript = clipper.transcribe(staged["r2_key"], media_path=media_path,
                                    transcriber=transcriber)
    print(f"video-episode: transcript {len(transcript['words'])} word(s) "
          f"(source: {transcript.get('source', 'unknown')})", flush=True)

    selection = clipper.select_moments(transcript, llm=llm, account_key=account_key)
    accepted = selection.get("accepted", [])
    # Snap each clip's in/out to the nearest word boundary so we never cut mid-word.
    for m in accepted:
        snap_to_word_boundaries(m, transcript)
    print(f"video-episode: {len(accepted)} moment(s) pass "
          f"(snapped to word boundaries)", flush=True)
    clipper.print_plan(selection)

    if media_path is None:
        raise VideoEditorError(
            "video-episode needs the local episode file to assemble clips; "
            "re-run with the local video path as --source.")

    output_dir = config.video_output_dir()
    os.makedirs(output_dir, exist_ok=True)

    # Plan all manifests first so we can project + report cost before rendering.
    plan_broll = config.video_broll_enabled()
    manifests = []
    for m in accepted:
        if plan_broll:
            manifests.append(plan_broll_manifest(m, transcript, llm=llm))
        else:
            manifests.append({"beats": [], "projected_cost": 0.0, "kind":
                              config.video_broll_kind(), "clip_dur":
                              float(m.end_ts) - float(m.start_ts)})

    projected = project_episode_cost(manifests)
    motion_beats = sum(mf.get("motion_count", 0) for mf in manifests)
    still_beats = sum(mf.get("still_count", 0) for mf in manifests)
    if plan_broll:
        print(f"video-episode: b-roll plan = {motion_beats} motion (Higgsfield) + "
              f"{still_beats} still (Nano Banana) across {len(accepted)} clip(s); "
              f"projected cost ~{projected} credits (motion cap "
              f"{config.video_broll_cap()}, stills cap {config.video_stills_cap()} "
              f"per episode). "
              f"{'RENDERING' if (render and renderer) else 'NOT rendering overlays'}.",
              flush=True)

    # ONE episode-level budget PER ROUTE (hard cost guards), shared across all clips
    # so the whole episode never renders more than each cap, no matter clip count.
    motion_budget = RenderBudget(config.video_broll_cap())
    stills_budget = RenderBudget(config.video_stills_cap())
    # Still renderer: reuse Echo's Gemini pipeline (armed by AGENT_VIDEO_STILLS_ENABLED).
    use_still_renderer = (still_card_renderer
                          if (render and config.video_stills_enabled()) else None)

    clips = []
    for m, manifest in zip(accepted, manifests):
        base = f"clip_{int(m.start_ts):05d}_{int(m.end_ts):05d}"

        overlays = []
        if plan_broll and manifest.get("beats"):
            use_renderer = renderer if (render and config.video_render_enabled()) else None
            try:
                overlays = render_overlays(
                    manifest, renderer=use_renderer, budget=motion_budget,
                    still_renderer=use_still_renderer, still_budget=stills_budget)
            except VideoEditorError as exc:
                print(f"video-episode: overlay render stopped: {exc}", flush=True)

        files = {}
        for aspect in aspects:
            for captioned in (True, False):
                mode = "cap" if captioned else "ad"
                try:
                    path = assemble_clip(m, media_path, transcript, overlays,
                                         output_dir, base, aspect=aspect,
                                         captioned=captioned)
                    files[f"{aspect}_{mode}"] = path
                    print(f"video-episode: assembled {aspect} {mode} -> {path}",
                          flush=True)
                except Exception as exc:
                    print(f"video-episode: assemble {aspect} {mode} failed "
                          f"[{base}]: {exc}", flush=True)

        clips.append({"moment": m, "manifest": manifest,
                      "overlays": overlays, "files": files})

    return {
        "staged": staged, "transcript": transcript, "selection": selection,
        "aspects": aspects, "projected_cost": projected, "clips": clips,
    }


# ---- Part 5: CLI + Slack surfacing ------------------------------------------

def _rationale_line(clip):
    """One-line rationale + cost for the review card."""
    m = clip["moment"]
    mf = clip.get("manifest", {})
    n = len(mf.get("beats", []))
    cost = mf.get("projected_cost", 0)
    concepts = ", ".join(b["concept"] for b in mf.get("beats", [])[:3])
    bits = [f"score {m.score}", f"{m.bucket}"]
    if n:
        bits.append(f"{n} b-roll overlay(s) ~{cost} credits")
        if concepts:
            bits.append(f"visuals: {concepts}")
    return " | ".join(bits)


def video_episode_cli(argv):
    """python -m agent video-episode --source <path> [--render] [--account <key>]

    --render arms the Higgsfield overlay call (needs AGENT_VIDEO_RENDER and an
    interactive Claude session driving Higgsfield). Without it, overlays come from
    cache only and nothing is spent. Assembles clips and posts held review cards.
    """
    from . import clipper
    from . import media_host

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
        print("usage: python -m agent video-episode --source <path> "
              "[--render] [--account <key>]")
        return

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
                aws_access_key_id=key_id, aws_secret_access_key=secret,
                config=_BC(retries={"max_attempts": 2, "mode": "standard"}))
            client = media_host._S3Client(s3, config.S3_BUCKET)
        except Exception:
            pass

    try:
        result = edit_episode(source, render=render, client=client,
                              account_key=account_key)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"video-episode: error: {exc}", flush=True)
        return
    if result is None:
        return

    clips = result.get("clips", [])
    staged = result.get("staged") or {}
    r2_key = staged.get("r2_key", source)
    episode_title = os.path.basename(r2_key).rsplit(".", 1)[0]
    acct = account_key or os.environ.get("AGENT_CLIPPER_ACCOUNT_KEY") \
        or config.episode_inbox_tenant()

    print(f"video-episode: projected episode Higgsfield cost "
          f"~{result.get('projected_cost', 0)} credits.", flush=True)

    # Slack poster (optional).
    slack_poster = None
    slack_token = os.environ.get(config.SLACK_BOT_TOKEN_ENV, "")
    slack_channel = os.environ.get("AGENT_SLACK_CHANNEL_ID", "")
    if slack_token and slack_channel:
        try:
            from .slack_surface import SlackPoster
            slack_poster = SlackPoster(token=slack_token, channel=slack_channel)
        except Exception:
            pass

    posted = 0
    for clip in clips:
        m = clip["moment"]
        # Prefer the 9:16 captioned render as the primary card asset.
        files = clip.get("files", {})
        primary = files.get("9:16_cap") or next(iter(files.values()), "")
        reel_url = ""
        if primary and client:
            try:
                reel_url = media_host.host_media(primary, acct, client=client) or ""
            except Exception as exc:
                print(f"video-episode: upload failed: {exc}", flush=True)

        rationale = _rationale_line(clip)
        try:
            clipper.save_clip_draft(m, primary, reel_url, acct,
                                    episode_title=f"{episode_title} ({rationale})",
                                    poster=slack_poster)
            print(f"video-episode: held review card posted "
                  f"[{clipper._fmt_ts(m.start_ts)}-{clipper._fmt_ts(m.end_ts)}] "
                  f"| {rationale} | renders: {', '.join(files.keys())}", flush=True)
            posted += 1
        except Exception as exc:
            print(f"video-episode: card failed: {exc}", flush=True)

    if posted:
        print(f"video-episode: {posted} clip(s) assembled + held for approval. "
              "Nothing published.", flush=True)

