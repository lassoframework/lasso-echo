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
_SCRIM_NAVY_HEXSTR = "121E3C"   # ffmpeg 0x color for the navy fields
_BRAND_RED_HEX = "FF0000"       # LASSO V3 house-style red
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

# ---- Treatment B: text side panel (house standard for concept beats) --------
# A semi-transparent navy gradient panel over ~left 60%, fading to transparent
# over the live host (which keeps playing on the right). Panel + text slide/fade
# in, hold, then clear. NEVER a full-screen static text takeover.
_PANEL_COVERAGE = 0.60          # panel opaque zone as a fraction of width
_PANEL_NAVY = (18, 30, 60)      # #121E3C
_PANEL_MAX_ALPHA = 205          # panel opacity (semi-transparent, host faint behind)
_SKYBLUE_BGR = "E6B95E"         # ASS BGR for sky blue #5EB9E6
_PANEL_HEAD_FRAC = 0.060        # headline font size as fraction of frame height
_PANEL_EYE_FRAC = 0.022         # eyebrow font size


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

    # Guard: never let snapping collapse a clip. If the snapped span is degenerate
    # (inverted or under 1s), revert to the original timestamps and log it.
    if moment.end_ts - moment.start_ts < 1.0:
        print(f"[video] snap produced a degenerate span "
              f"({moment.start_ts:.2f}-{moment.end_ts:.2f}); keeping original "
              f"{s:.2f}-{e:.2f}", flush=True)
        moment.start_ts, moment.end_ts = s, e
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
            # on-card headline is the grounded concept, scrubbed for on-screen rules.
            # The card itself is rendered by still_card_renderer through the shared
            # house-style pipeline (editorial archetype + grade gate) — there is no
            # per-beat styling prompt. `prompt` here is only the cache-key basis.
            b["card_text"] = clipper_render.scrub_onscreen(b["concept"].upper())
            b["prompt"] = f"house-editorial-card|{b['card_text']}"
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


def still_card_renderer(beat, out_path, kind, account_key=None):
    """Still-card renderer. Routes through the SAME house-style card pipeline the
    organic feed cards use: creative_studio.generate() -> build_prompt (editorial
    archetype, Section 8 house style) -> Gemini image -> the SAME six-question
    grade gate with auto-retry. There is NO separate text-on-navy path for video.

    A card that grades a fail (centered/flat/no anchor) is auto-retried and, if it
    still fails 3x, generate() returns None and we raise so the beat is skipped
    rather than shipping an off-brand card. 9:16 with story safe zones so the
    composition stays in the upper-middle, clear of the caption lower-third and
    the bottom treatment.

    on-card headline = the grounded, scrubbed concept; facts = grounded context
    (not rendered as text). Fabrication-safe."""
    from . import creative_studio
    headline = beat.get("card_text") or clipper_render.scrub_onscreen(
        str(beat.get("concept", "")).upper())
    facts = [f for f in (beat.get("source_span"), beat.get("concept")) if f] \
        or [headline]
    art = creative_studio.generate(
        headline, facts,
        aspect="9:16", pixels="1080x1920",
        surface="reel still card (9:16 story safe area)",
        archetype="editorial", account_key=account_key)
    if not art or not art.get("path"):
        raise VideoEditorError(
            "still card: creative_studio.generate returned None (studio flag off, "
            "no Nano key, spend cap, or failed the house-style grade gate 3x).")
    shutil.copyfile(art["path"], out_path)
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

    # A+ finish: cross-dissolve each cutaway in/out instead of a hard cut.
    dissolve = config.video_polish_enabled()
    fd = 0.25  # dissolve duration (seconds)

    filter_parts = []
    current = "0:v"
    for i, pr in enumerate(prepared):
        idx = i + 1
        off = float(pr["offset"])
        dur = float(overlays[i]["duration"])
        shifted = f"s{i}"
        out_label = f"o{i}"
        if dissolve:
            fout = max(0.0, dur - fd)
            filter_parts.append(
                f"[{idx}:v]format=yuva420p,"
                f"fade=t=in:st=0:d={fd}:alpha=1,"
                f"fade=t=out:st={fout:.3f}:d={fd}:alpha=1,"
                f"setpts=PTS-STARTPTS+{off:.3f}/TB[{shifted}]")
        else:
            filter_parts.append(
                f"[{idx}:v]setpts=PTS-STARTPTS+{off:.3f}/TB[{shifted}]")
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
                             width, height, margin_v, motion=False,
                             skip_windows=None):
    """Word Highlight ASS: Anton ALL CAPS, groups of _WH_WORDS_PER_GROUP, the ONE
    active (currently spoken) word in brand RED, the rest white, heavy outline +
    shadow. One event per word so exactly one line is visible (no ghost/duplicate).
    Fabrication-safe: only words within the segment; dashes/vendor scrubbed.
    motion=True adds an A+ pop: the active word scales in from ~118% to 100% and
    the line fades in fast (ASS \\t transform + \\fad); colour tags are unchanged
    so the one-red-word invariant holds."""
    start_ts = float(start_ts)
    end_ts = float(end_ts)
    words = [
        w for w in transcript.get("words", [])
        if float(w.get("start", 0)) >= start_ts - 0.05
        and float(w.get("start", 0)) < end_ts + 0.05
    ]
    # Suppress captions inside Treatment B panel windows (clip-relative seconds) so
    # the side-panel headline never collides with the lower-third captions.
    sw = skip_windows or []
    if sw:
        def _in_win(rel):
            return any(a - 0.15 <= rel <= b + 0.15 for a, b in sw)
        words = [w for w in words if not _in_win(float(w.get("start", 0)) - start_ts)]
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
                # STATIC captions: only the colour changes per active word (one red
                # word, rest white). No scale pop / fade — motion hurt readability.
                color = _WH_ACTIVE_BGR if ci == word_idx else _WH_WHITE_BGR
                parts.append(f"{{\\c&H00{color}&\\fscx100\\fscy100}}{text}")
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
                        width, height, face_bottom_frac=None, motion=None,
                        skip_windows=None):
    """Burn Word Highlight captions (Anton, one red active word) into the video,
    placed in the lower third below the detected face. Loads Anton via fontsdir.
    skip_windows: clip-relative (start,end) spans where captions are suppressed
    (a Treatment B panel is showing there)."""
    clipper_render._require_render()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    margin_v = _caption_margin_v(height, face_bottom_frac)
    if motion is None:
        motion = config.video_polish_enabled()

    with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as tf:
        ass_path = tf.name
    try:
        _make_word_highlight_ass(transcript, start_ts, end_ts, ass_path,
                                 width, height, margin_v, motion=motion,
                                 skip_windows=skip_windows)
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


def plan_keep_intervals(words_rel, clip_dur, gap, keep):
    """Given words with clip-relative (start,end) times, return the list of
    (a,b) intervals to KEEP after removing inter-word dead air longer than `gap`
    (each removed gap leaves `keep` seconds of breathing room), plus a piecewise
    time_map(orig_clip_t) -> tightened_clip_t. Speech is never cut; only silence
    between words is compressed."""
    words_rel = sorted(words_rel, key=lambda w: w[0])
    intervals = []
    seg_start = 0.0
    prev_end = 0.0
    for (ws, we) in words_rel:
        if ws - prev_end > gap and prev_end > 0:
            # close the current keep interval shortly after the last word, then
            # resume at this word (the middle silence is dropped)
            intervals.append((seg_start, min(prev_end + keep, ws)))
            seg_start = ws
        prev_end = max(prev_end, we)
    intervals.append((seg_start, clip_dur))
    # merge/clean
    intervals = [(round(a, 3), round(b, 3)) for a, b in intervals if b - a > 0.02]

    # cumulative kept duration before each interval, for the time map
    cum = []
    total = 0.0
    for (a, b) in intervals:
        cum.append(total)
        total += (b - a)

    def time_map(t):
        t = float(t)
        for i, (a, b) in enumerate(intervals):
            if t < a:
                return cum[i]              # inside a removed gap -> snap to next kept start
            if a <= t <= b:
                return cum[i] + (t - a)
        return total

    return intervals, time_map, total


def _apply_jumpcuts(input_path, intervals, output_path):
    """Cut input_path into the keep `intervals` and concat them into output_path
    (re-encode). Returns output_path. No-op passthrough if a single full interval."""
    ffmpeg = clipper_render._ffmpeg()
    if len(intervals) <= 1:
        shutil.copyfile(input_path, output_path)
        return output_path
    parts = []
    for i, (a, b) in enumerate(intervals):
        parts.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}]")
    concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(intervals)))
    parts.append(f"{concat_in}concat=n={len(intervals)}:v=1:a=1[v][a]")
    cmd = [
        ffmpeg, "-y", "-i", input_path,
        "-filter_complex", ";".join(parts),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    clipper_render._run(cmd, "jumpcuts")
    return output_path


def _remap_transcript(transcript, clip_start, clip_end, time_map):
    """Return a NEW transcript whose words are shifted to the tightened clip
    timeline (0-based), for caption timing. Words outside the clip are dropped."""
    out_words = []
    for w in transcript.get("words", []):
        ws = float(w.get("start", 0))
        if ws < clip_start - 0.05 or ws > clip_end + 0.05:
            continue
        rel_s = ws - clip_start
        rel_e = float(w.get("end", 0)) - clip_start
        out_words.append({"word": w.get("word", ""),
                          "start": time_map(rel_s), "end": time_map(rel_e)})
    return {"words": out_words, "segments": []}


def _polish_host(input_path, output_path, width, height, duration):
    """A+ finish on the HOST base only: a subtle unifying color grade so the
    talking-head matches the cinematic AI b-roll (contrast + saturation + a touch
    of gamma). Frame-safe (eq is per-pixel, preserves every frame and A/V sync).
    Overlays are composited AFTER as full-frame cutaways, so they are unaffected.
    Never alters the host content, only the grade."""
    ffmpeg = clipper_render._ffmpeg()
    vf = "eq=contrast=1.06:saturation=1.10:brightness=0.01:gamma=0.98"
    cmd = [
        ffmpeg, "-y", "-i", input_path, "-vf", vf,
        "-c:v", "libx264", "-crf", "20", "-preset", "fast",
        "-c:a", "copy", output_path,
    ]
    clipper_render._run(cmd, "polish_host")
    return output_path


def _make_radial_glow_png(width, height, out_path):
    """Navy field with a soft off-center radial glow (a real depth layer, never a
    flat fill) for the intro/outro card background."""
    from PIL import Image
    import math
    # render small then upscale (the glow is soft, so scaling is invisible + fast)
    sw, sh = max(64, width // 5), max(64, height // 5)
    img = Image.new("RGB", (sw, sh), _PANEL_NAVY)
    px = img.load()
    cx, cy = int(sw * 0.62), int(sh * 0.34)
    maxd = math.hypot(sw, sh) * 0.6
    gr, gg, gb = 42, 60, 104
    br, bg, bb = _PANEL_NAVY
    for y in range(sh):
        for x in range(sw):
            t = max(0.0, 1.0 - math.hypot(x - cx, y - cy) / maxd)
            t *= t
            px[x, y] = (int(br + (gr - br) * t), int(bg + (gg - bg) * t),
                        int(bb + (gb - bb) * t))
    img.resize((width, height)).save(out_path)
    return out_path


def _make_intro_card(out_path, width, height, duration, eyebrow, headline,
                     red_word, anchor, deck):
    """Animated house-style INTRO/OUTRO card (full screen, ~2.5s), Blake's approved
    look, ANIMATED:
      - deep navy + radial-glow depth layer + subtle grain
      - giant ghosted anchor (numeral or key word) bleeding off frame, slow drift
      - Oswald sky-blue tracked eyebrow + short red rule (fade in)
      - Anton oversized left-aligned headline, builds line by line, ONE red word
        that pops (scale) last
      - Montserrat deck (<=2 lines), muted, fades in
      - Minimal Broadcast bottom treatment
    Passes the house-style six-question gate by construction (left-aligned,
    asymmetric, scale contrast, one red accent, one depth layer, feed-stopping)."""
    ffmpeg = clipper_render._ffmpeg()
    work = os.path.dirname(os.path.abspath(out_path))
    anton = video_assets.anton_font_path().replace("\\", "\\\\").replace(":", "\\:")

    def esc(t):
        return str(t).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    glow = os.path.join(work, "glow.png")
    _make_radial_glow_png(width, height, glow)

    # Stage 1: background = glow + grain + (only when the anchor is a spoken NUMBER)
    # a giant ghosted numeral drifting off frame. A random word fragment ghosted huge
    # reads as a glitch, so text anchors are dropped for a cleaner, premium open.
    anchor_txt = clipper_render.scrub_onscreen(str(anchor or "").strip().upper())
    anchor_is_num = anchor_txt.replace(",", "").replace(".", "").isdigit()
    bg = os.path.join(work, "introbg.mp4")
    vf1_parts = ["noise=alls=7:allf=t"]
    if anchor_is_num:
        anchor_fs = int(height * (0.95 if len(anchor_txt) <= 2 else 0.55))
        vf1_parts.append(
            f"drawtext=fontfile='{anton}':text='{esc(anchor_txt)}':"
            f"fontcolor=white@0.12:fontsize={anchor_fs}:"
            f"x=w-tw*0.62:y='(h-th)/2 - 30*t/{max(0.5,duration):.2f}'")
    vf1 = ",".join(vf1_parts)
    clipper_render._run([
        ffmpeg, "-y", "-loop", "1", "-i", glow,
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-vf", vf1, "-t", str(duration),
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", bg], "intro_bg")

    # Stage 2: animated text (eyebrow + red rule + headline build + deck) via ASS.
    ass = os.path.join(work, "intro.ass")
    _make_intro_ass(ass, width, height, duration, eyebrow, headline, red_word, deck)
    txt = os.path.join(work, "introtxt.mp4")
    safe = os.path.abspath(ass).replace("\\", "/").replace(":", "\\:")
    fonts = video_assets.FONTS_DIR.replace("\\", "/").replace(":", "\\:")
    clipper_render._run([
        ffmpeg, "-y", "-i", bg, "-vf", f"ass={safe}:fontsdir={fonts}",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-c:a", "copy", txt],
        "intro_text")

    # Stage 3: Minimal Broadcast bottom treatment.
    apply_bottom_treatment(txt, out_path, width, height, work)
    return out_path


def _make_intro_ass(ass_path, width, height, duration, eyebrow, headline,
                    red_word, deck):
    eye_fs = int(height * 0.028)
    head_fs = int(height * 0.076)
    deck_fs = int(height * 0.024)
    left = int(width * 0.07)
    ms = clipper_render._fmt_ass_ts
    dur = float(duration)
    lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: IEye,Oswald,{eye_fs},&H00{_SKYBLUE_BGR},&H00{_SKYBLUE_BGR},"
        f"&H00000000,&H00000000,-1,0,0,0,100,100,9,0,1,2,0,7,{left},10,0,0",
        f"Style: IHead,Anton,{head_fs},&H00{_WH_WHITE_BGR},&H00{_WH_WHITE_BGR},"
        f"&H64000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,1,7,{left},10,0,0",
        f"Style: IDeck,Montserrat,{deck_fs},&H00C8C8C8,&H00C8C8C8,"
        f"&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,7,{left},10,0,0",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    eye = clipper_render.scrub_onscreen(str(eyebrow or "").strip().upper())
    head = clipper_render.scrub_onscreen(str(headline or "").strip().upper())
    red = clipper_render.scrub_onscreen(str(red_word or "").strip().upper())
    deck_t = clipper_render.scrub_onscreen(str(deck or "").strip())
    eye_y = int(height * 0.27)
    head_y = int(height * 0.345)

    # Red square stamp: sized off frame height (confident brand mark, not a dot)
    sq = int(height * 0.04)
    sq_y = eye_y + int(eye_fs * 0.1)
    lines.append(f"Dialogue: 0,{ms(0.15)},{ms(dur)},IHead,,0,0,0,,"
                 f"{{\\pos({left},{sq_y})\\an7\\fad(260,0)\\1c&H0000FF&\\p1}}"
                 f"m 0 0 l {sq} 0 l {sq} {sq} l 0 {sq}{{\\p0}}")
    if eye:
        ex = left + sq + int(eye_fs * 0.55)
        lines.append(f"Dialogue: 0,{ms(0.15)},{ms(dur)},IEye,,0,0,0,,"
                     f"{{\\pos({ex},{eye_y})\\an7\\fad(260,0)}}{eye}")
    # Thin red rule between eyebrow block and headline (editorial separator)
    rule_w = int(width * 0.12)
    rule_h = max(3, int(height * 0.0019))
    rule_y = eye_y + sq + int((head_y - eye_y - sq) * 0.35)
    lines.append(f"Dialogue: 0,{ms(0.30)},{ms(dur)},IHead,,0,0,0,,"
                 f"{{\\pos({left},{rule_y})\\an7\\fad(180,0)\\1c&H0000FF&\\p1}}"
                 f"m 0 0 l {rule_w} 0 l {rule_w} {rule_h} l 0 {rule_h}{{\\p0}}")
    # headline builds line-by-line; wrap wider for 3 punchy lines instead of 4
    hl_lines = _wrap_headline(head, 16)
    line_h = int(head_fs * 1.02)
    red_used = False   # color ONLY the first occurrence -> exactly one red word
    for i, ln in enumerate(hl_lines):
        y = head_y + i * line_h
        start = 0.5 + i * 0.22
        words = []
        has_red = False
        for w in ln.split():
            if red and w == red and not red_used:
                has_red = True
                red_used = True
                words.append(f"{{\\c&H00{_WH_ACTIVE_BGR}&}}{w}{{\\c&H00{_WH_WHITE_BGR}&}}")
            else:
                words.append(w)
        txt = " ".join(words)
        pop = "\\t(0,160,\\fscx100\\fscy100)\\fscx88\\fscy88" if has_red else ""
        lines.append(f"Dialogue: 0,{ms(start)},{ms(dur)},IHead,,0,0,0,,"
                     f"{{\\pos({left},{y})\\an7\\fad(220,0){pop}}}{txt}")
    if deck_t:
        dy = head_y + len(hl_lines) * line_h + int(height * 0.02)
        for j, dl in enumerate(_wrap_text(deck_t, 40)[:2]):
            lines.append(f"Dialogue: 0,{ms(1.5 + j * 0.15)},{ms(dur)},IDeck,,0,0,0,,"
                         f"{{\\pos({left},{dy + j * int(deck_fs*1.3)})\\an7\\fad(300,0)}}{dl}")

    os.makedirs(os.path.dirname(os.path.abspath(ass_path)), exist_ok=True)
    with open(ass_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return ass_path


def _wrap_text(text, width_chars=16):
    """Word-wrap ALL-CAPS text into lines of ~width_chars for a title card."""
    words = str(text or "").split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[:4]  # cap at 4 lines


def _make_title_card(headline, subline, out_path, width, height, duration,
                     accent=True):
    """Branded full-frame title card (navy, Anton headline, red underline accent,
    fade in/out, silent audio) used for the hook open and the end CTA. Text is
    scrubbed (no dashes/vendor) before it is drawn."""
    ffmpeg = clipper_render._ffmpeg()
    anton = video_assets.anton_font_path().replace("\\", "\\\\").replace(":", "\\:")
    oswald = video_assets.oswald_font_path().replace("\\", "\\\\").replace(":", "\\:")
    head = clipper_render.scrub_onscreen(str(headline or "").strip().upper())
    lines = _wrap_text(head, 16)
    fs = int(height * 0.075)
    line_h = int(fs * 1.12)
    block_h = line_h * len(lines)
    top = int(height * 0.30)

    def esc(t):
        return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    vf = [f"drawbox=x=0:y=0:w=iw:h=ih:color=0x{_SCRIM_NAVY_HEXSTR}@1.0:t=fill"]
    for i, ln in enumerate(lines):
        y = top + i * line_h
        vf.append(
            f"drawtext=fontfile='{anton}':text='{esc(ln)}':fontcolor=white:"
            f"fontsize={fs}:x=(w-tw)/2:y={y}")
    if accent:
        ay = top + block_h + int(height * 0.02)
        vf.append(
            f"drawbox=x=(iw-{int(width*0.16)})/2:y={ay}:w={int(width*0.16)}:h=6:"
            f"color=0x{_BRAND_RED_HEX}@1.0:t=fill")
    if subline:
        sub = clipper_render.scrub_onscreen(str(subline).strip().upper())
        sy = top + block_h + int(height * 0.06)
        sfs = int(height * 0.028)
        vf.append(
            f"drawtext=fontfile='{oswald}':text='{esc(sub)}':fontcolor=white@0.85:"
            f"fontsize={sfs}:x=(w-tw)/2:y={sy}")
    fade_out = max(0.0, float(duration) - 0.3)
    vf.append(f"fade=t=in:st=0:d=0.3,fade=t=out:st={fade_out:.2f}:d=0.3")

    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i", f"color=c=0x{_SCRIM_NAVY_HEXSTR}:s={width}x{height}:r=30",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-vf", ",".join(vf), "-t", str(float(duration)),
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    clipper_render._run(cmd, "title_card")
    return out_path


def _concat_av(paths, out_path, width, height):
    """Concatenate clips (video+audio) via the concat filter (re-encode) so hook +
    main + CTA join cleanly. Every input's video (WxH, sar, 30fps) and audio
    (44.1kHz stereo fltp) is NORMALIZED first, because concat requires identical
    stream parameters across inputs (a mismatch silently fails otherwise)."""
    ffmpeg = clipper_render._ffmpeg()
    inputs = []
    for p in paths:
        inputs += ["-i", p]
    parts = []
    for i in range(len(paths)):
        parts.append(
            f"[{i}:v]scale={width}:{height},setsar=1,fps=30,format=yuv420p[v{i}]")
        parts.append(
            f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
    streams = "".join(f"[v{i}][a{i}]" for i in range(len(paths)))
    parts.append(f"{streams}concat=n={len(paths)}:v=1:a=1[v][a]")
    cmd = [
        ffmpeg, "-y", *inputs,
        "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    clipper_render._run(cmd, "concat_av")
    return out_path


def _make_panel_png(width, height, out_path):
    """Left gradient panel: navy opaque on the far left, fading to fully
    transparent by _PANEL_COVERAGE of the width, so the host stays visible on the
    right. RGBA PNG."""
    from PIL import Image
    # alpha varies only by x, so build a width x 1 strip and stretch to height.
    r, g, b = _PANEL_NAVY
    strip = Image.new("RGBA", (width, 1), (0, 0, 0, 0))
    sp = strip.load()
    solid_to = int(width * 0.50)          # panel body: strong, even alpha
    fade_to = int(width * 0.68)           # transparent by here
    for x in range(width):
        if x <= solid_to:
            a = _PANEL_MAX_ALPHA
        elif x >= fade_to:
            a = 0
        else:
            a = int(_PANEL_MAX_ALPHA * (1 - (x - solid_to) / (fade_to - solid_to)))
        sp[x, 0] = (r, g, b, a)
    strip.resize((width, height)).save(out_path)
    return out_path


def _wrap_headline(text, width_chars=13):
    words = str(text or "").split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width_chars:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[:4]


def _make_side_panel_ass(specs, width, height, ass_path):
    """ASS for Treatment B panel text: Oswald tracked eyebrow in sky blue, a short
    red rule, Anton oversized headline with ONE red word, all left-aligned inside
    the left panel. Each block slides + fades in over its window (animated, never
    static-pop). Text is scrubbed (no dashes/vendor)."""
    head_fs = int(height * _PANEL_HEAD_FRAC)
    eye_fs = int(height * _PANEL_EYE_FRAC)
    left = int(width * 0.06)
    lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: SPEye,Oswald,{eye_fs},&H00{_SKYBLUE_BGR},&H00{_SKYBLUE_BGR},"
        f"&H00000000,&H64000000,-1,0,0,0,100,100,4,0,1,2,0,7,{left},10,0,0",
        f"Style: SPHead,Anton,{head_fs},&H00{_WH_WHITE_BGR},&H00{_WH_WHITE_BGR},"
        f"&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,2,7,{left},10,0,0",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ts = clipper_render._fmt_ass_ts
    for sp in specs:
        s = float(sp["start"]); e = float(sp["end"])
        eye = clipper_render.scrub_onscreen(str(sp.get("eyebrow", "")).strip().upper())
        head = clipper_render.scrub_onscreen(str(sp.get("headline", "")).strip().upper())
        red = clipper_render.scrub_onscreen(str(sp.get("red_word", "")).strip().upper())
        eye_y = int(height * 0.34)
        rule_y = int(height * 0.38)
        head_y = int(height * 0.40)
        # eyebrow: slide up + fade in
        if eye:
            lines.append(
                f"Dialogue: 0,{ts(s)},{ts(e)},SPEye,,0,0,0,,"
                f"{{\\pos({left},{eye_y})\\an7\\fad(220,180)"
                f"\\move({left-30},{eye_y},{left},{eye_y},0,260)}}{eye}")
        # red rule: a short red drawing rectangle
        rw = int(width * 0.10)
        lines.append(
            f"Dialogue: 0,{ts(s)},{ts(e)},SPHead,,0,0,0,,"
            f"{{\\pos({left},{rule_y})\\an7\\fad(220,180)\\1c&H0000FF&\\p1}}"
            f"m 0 0 l {rw} 0 l {rw} 6 l 0 6{{\\p0}}")
        # headline: EXACTLY one red word (first occurrence only), wrapped
        hl_lines = _wrap_headline(head, 13)
        parts = []
        red_used = False
        for ln in hl_lines:
            words = []
            for w in ln.split():
                if red and w == red and not red_used:
                    red_used = True
                    words.append(f"{{\\c&H00{_WH_ACTIVE_BGR}&}}{w}{{\\c&H00{_WH_WHITE_BGR}&}}")
                else:
                    words.append(w)
            parts.append(" ".join(words))
        headline_text = "\\N".join(parts)
        lines.append(
            f"Dialogue: 0,{ts(s)},{ts(e)},SPHead,,0,0,0,,"
            f"{{\\pos({left},{head_y})\\an7\\fad(240,200)"
            f"\\move({left-40},{head_y},{left},{head_y},0,300)}}{headline_text}")

    os.makedirs(os.path.dirname(os.path.abspath(ass_path)), exist_ok=True)
    with open(ass_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return ass_path


def burn_side_panels(input_path, output_path, specs, width, height, work_dir):
    """Treatment B: composite an animated navy gradient panel + house-style text
    over the LIVE footage for each spec window (host keeps playing underneath).
    Panel fades in/out; text slides + fades in. Never a full-frame takeover.
    specs: [{start, end, eyebrow, headline, red_word}]. No-op if empty."""
    specs = [s for s in (specs or []) if s.get("headline")]
    if not specs:
        shutil.copyfile(input_path, output_path)
        return output_path
    ffmpeg = clipper_render._ffmpeg()
    os.makedirs(work_dir, exist_ok=True)
    panel_png = os.path.join(work_dir, "panel.png")
    _make_panel_png(width, height, panel_png)

    # Step 1: fade the gradient panel in/out over each window, composited on host.
    inputs = ["-i", input_path]
    for _ in specs:
        inputs += ["-loop", "1", "-i", panel_png]
    parts = []
    cur = "0:v"
    for i, sp in enumerate(specs):
        s = float(sp["start"]); e = float(sp["end"]); d = max(0.5, e - s)
        idx = i + 1
        fo = max(0.0, d - 0.35)
        parts.append(
            f"[{idx}:v]format=yuva420p,trim=duration={d},setpts=PTS-STARTPTS,"
            f"fade=t=in:st=0:d=0.35:alpha=1,fade=t=out:st={fo:.2f}:d=0.35:alpha=1,"
            f"setpts=PTS-STARTPTS+{s:.3f}/TB[p{i}]")
        parts.append(f"[{cur}][p{i}]overlay=x=0:y=0:eof_action=pass[pb{i}]")
        cur = f"pb{i}"
    panel_bg = os.path.join(work_dir, "panel_bg.mp4")
    cmd = ([ffmpeg, "-y"] + inputs + [
        "-filter_complex", ";".join(parts), "-map", f"[{cur}]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-c:a", "copy",
        panel_bg])
    clipper_render._run(cmd, "side_panel_bg")

    # Step 2: burn the animated house-style text into the panels.
    with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as tf:
        ass_path = tf.name
    try:
        _make_side_panel_ass(specs, width, height, ass_path)
        safe = os.path.abspath(ass_path).replace("\\", "/").replace(":", "\\:")
        fonts = video_assets.FONTS_DIR.replace("\\", "/").replace(":", "\\:")
        cmd = [ffmpeg, "-y", "-i", panel_bg, "-vf", f"ass={safe}:fontsdir={fonts}",
               "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-c:a", "copy",
               output_path]
        clipper_render._run(cmd, "side_panel_text")
    finally:
        try:
            os.unlink(ass_path)
        except OSError:
            pass
    return output_path


def _eyebrow_for(text):
    """A short 1-2 word ALL-CAPS eyebrow derived from the grounded concept (first
    meaningful words). Grounded, not invented."""
    words = [w for w in str(text or "").split() if len(w) > 2]
    return " ".join(words[:2]).upper()


def _make_nano_intro_png(moment, out_path, account_key=None):
    """Generate the reel's OPENING infographic through the SAME house-style Gemini
    pipeline + six-question grade gate the feed cards use (creative_studio.generate,
    9:16, illustration archetype). Headline = the clip's verbatim hook (scrubbed);
    no fabrication. Returns the png path, or None on any failure (studio flag off,
    no Nano key, spend cap, or 3x grade fail) so the caller falls back to the
    code-built intro card and the reel is never blocked."""
    try:
        from . import creative_studio
        hook = clipper_render.scrub_onscreen(str(getattr(moment, "hook", "") or "").strip())
        if not hook:
            return None
        facts = [f for f in (getattr(moment, "hook", ""),
                             getattr(moment, "transcript_text", "")) if f] or [hook]
        art = creative_studio.generate(
            hook, facts, aspect="9:16", pixels="1080x1920",
            surface="reel opening infographic (9:16, slides away to host)",
            archetype="hero", account_key=account_key)
        if art and art.get("path"):
            shutil.copyfile(art["path"], out_path)
            return out_path
    except Exception as exc:
        print(f"[video] nano intro generation skipped: {exc}", flush=True)
    return None


def _slide_away_intro(intro_png, body_path, out_path, width, height,
                      hold=2.0, slide=0.6):
    """Open on the still infographic for `hold` seconds, then SLIDE it up and off
    over `slide` seconds to reveal the host body (xfade slideup). The host audio is
    delayed to start exactly when the host video begins revealing, so lip-sync is
    intact and the intro plays silent."""
    intro_dur = hold + slide
    delay_ms = int(hold * 1000)
    vf = (
        f"[0:v]scale={width}:{height},setsar=1,fps=30,format=yuv420p[iv];"
        f"[1:v]scale={width}:{height},setsar=1,fps=30,format=yuv420p[bv];"
        f"[iv][bv]xfade=transition=slideup:duration={slide}:offset={hold}[v];"
        f"[1:a]adelay={delay_ms}|{delay_ms},aresample=44100[a]"
    )
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-t", f"{intro_dur:.2f}", "-i", intro_png,
        "-i", body_path, "-filter_complex", vf, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path,
    ]
    clipper_render._run(cmd, "nano_intro_slide")
    return out_path


def assemble_clip(moment, media_path, transcript, overlays, output_dir, base,
                  aspect="9:16", captioned=True, panel_specs=None, account_key=None):
    """
    Assemble one finished clip (house standard):
      cut -> frame(aspect) -> [jump-cut pacing] -> [A+ host grade] -> overlays ->
      [Word Highlight captions] -> Minimal Broadcast bottom treatment.
    Real host footage is the spine; overlays are cutaways only, never AI-altering
    the host. captioned=False produces the caption-FREE ad cut from the same
    timeline (it still gets the bottom treatment). Requires the render flag.
    """
    width, height = _dims(aspect)
    tag = aspect.replace(":", "x") + ("_cap" if captioned else "_ad")
    work = os.path.join(output_dir, f"{base}_{tag}_work")
    os.makedirs(work, exist_ok=True)

    framed = os.path.join(work, "framed.mp4")
    tightened = os.path.join(work, "tightened.mp4")
    polished = os.path.join(work, "polished.mp4")
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

    clip_dur = float(moment.end_ts) - float(moment.start_ts)
    host_base = framed
    # caption timeline + overlay offsets default to the original clip timeline
    cap_transcript = transcript
    cap_start, cap_end = float(moment.start_ts), float(moment.end_ts)
    # Motion b-roll cutaways only (moving footage). Text NEVER becomes a full-frame
    # cutaway — it is a Treatment B side panel over the live footage (below).
    motion_overlays = [o for o in (overlays or []) if o.get("kind", "video") == "video"]
    eff_overlays = motion_overlays
    panels = [dict(p) for p in (panel_specs or [])]   # still/concept text beats
    eff_dur = clip_dur

    # A+ pacing: remove dead air, then remap overlays + captions + panels onto the
    # tighter timeline so everything stays in sync.
    if config.video_jumpcuts_enabled():
        words_rel = [
            (float(w["start"]) - float(moment.start_ts),
             float(w["end"]) - float(moment.start_ts))
            for w in transcript.get("words", [])
            if float(moment.start_ts) - 0.05 <= float(w.get("start", 0))
            <= float(moment.end_ts) + 0.05
        ]
        intervals, time_map, new_dur = plan_keep_intervals(
            words_rel, clip_dur, config.video_jumpcut_gap(),
            config.video_jumpcut_keep())
        if new_dur > 2.0 and new_dur < clip_dur - 0.2:
            _apply_jumpcuts(framed, intervals, tightened)
            removed = clip_dur - new_dur
            print(f"[video] jump-cuts removed {removed:.1f}s of dead air "
                  f"({clip_dur:.0f}s -> {new_dur:.0f}s)", flush=True)
            host_base = tightened
            eff_dur = new_dur
            eff_overlays = [dict(ov, offset=time_map(ov["offset"]))
                            for ov in motion_overlays]
            panels = [dict(p, start=time_map(p["start"]), end=time_map(p["end"]))
                      for p in panels]
            cap_transcript = _remap_transcript(
                transcript, float(moment.start_ts), float(moment.end_ts), time_map)
            cap_start, cap_end = 0.0, new_dur

    # Mid-body concept beats stay as Treatment B side panels (overlay on live
    # footage). The HOOK is now the animated INTRO card and the CTA the OUTRO card
    # (full-screen designed cards, INTRO/OUTRO only) — added by concat below.

    # clamp/clean panels to the effective timeline
    panels = [p for p in panels
              if p.get("headline") and float(p["end"]) > float(p["start"])
              and float(p["start"]) < eff_dur]
    for p in panels:
        p["end"] = min(float(p["end"]), eff_dur - 0.05)

    if config.video_polish_enabled():
        _polish_host(host_base, polished, width, height, eff_dur)
        host_base = polished

    # Detect the speaker's face on the host footage so captions dodge it.
    face_bottom = video_assets.detect_face_bottom_frac(host_base)

    stage = host_base
    if eff_overlays:
        _composite_overlays(host_base, eff_overlays, composited, width, height, work)
        stage = composited

    if panels:
        panelled = os.path.join(work, "panelled.mp4")
        burn_side_panels(stage, panelled, panels, width, height, work)
        stage = panelled

    if captioned:
        # suppress captions wherever a Treatment B panel is on screen (no collision)
        skip_windows = [(float(p["start"]), float(p["end"])) for p in panels]
        burn_word_highlight(stage, captioned_out, cap_transcript,
                            cap_start, cap_end,
                            width, height, face_bottom_frac=face_bottom,
                            skip_windows=skip_windows)
        stage = captioned_out

    apply_bottom_treatment(stage, final_out, width, height, work)

    # INTRO (hook) + OUTRO (CTA) designed animated cards, INTRO/OUTRO only, over
    # the captioned cut. The ad cut stays clean (no cards) for paid placements.
    if config.video_polish_enabled() and captioned:
        try:
            hook_txt = (getattr(moment, "hook", "") or "").strip()
            bucket = (getattr(moment, "bucket", "") or "").replace("_", " ")
            cards = []
            # Reel opener: a Gemini/Nano house-style infographic that holds ~2s then
            # SLIDES AWAY to reveal the host (AGENT_VIDEO_NANO_INTRO, 9:16 reel only).
            # Falls back to the code-built intro card if generation is off or fails.
            nano_png = None
            if config.video_nano_intro_enabled() and aspect == "9:16":
                nano_png = _make_nano_intro_png(
                    moment, os.path.join(work, "nano_intro.png"), account_key)
            if nano_png:
                opener = os.path.join(work, "opener.mp4")
                _slide_away_intro(nano_png, final_out, opener, width, height)
                cards.append(opener)  # infographic slide-away + host body, combined
            else:
                if hook_txt:
                    intro = os.path.join(work, "intro.mp4")
                    _make_intro_card(intro, width, height, 2.6,
                                     eyebrow=bucket or "LASSO", headline=hook_txt,
                                     red_word=_pick_red_word(hook_txt),
                                     anchor=_anchor_for(hook_txt, _pick_red_word(hook_txt)),
                                     deck="")
                    cards.append(intro)
                cards.append(final_out)
            outro = os.path.join(work, "outro.mp4")
            _make_intro_card(outro, width, height, 2.2,
                             eyebrow="LASSO", headline="FOLLOW FOR MORE",
                             red_word="MORE", anchor="LASSO", deck="")
            cards.append(outro)
            if len(cards) > 1:
                booked = os.path.join(output_dir, f"{base}_{tag}_final.mp4")
                _concat_av(cards, booked, width, height)
                return booked
        except Exception as exc:
            print(f"[video] intro/outro cards skipped: {exc}", flush=True)
    return final_out


def _anchor_for(text, fallback):
    """Giant ghosted anchor for the intro card: a spoken number if present, else a
    fallback word (grounded, never invented)."""
    import re as _re
    m = _re.search(r"\b(\d{1,3})\b", str(text or ""))
    if m:
        return m.group(1)
    nums = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
            "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
    for w in str(text or "").lower().split():
        if w.strip(".,!?") in nums:
            return nums[w.strip(".,!?")]
    return str(fallback or "").upper()


def _pick_red_word(headline):
    """Pick ONE word to accent red: the longest meaningful word in the headline."""
    words = [w for w in str(headline or "").split() if len(w) > 2]
    if not words:
        return ""
    return max(words, key=len).upper()


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
              f"{still_beats} text panels across {len(accepted)} clip(s); "
              f"projected motion cost ~{projected} credits (motion cap "
              f"{config.video_broll_cap()} per episode). Text beats render as "
              f"Treatment B side panels over live footage (no image cost). "
              f"{'RENDERING' if (render and renderer) else 'NOT rendering overlays'}.",
              flush=True)

    # ONE episode-level motion budget (hard cost guard), shared across all clips.
    motion_budget = RenderBudget(config.video_broll_cap())

    clips = []
    for m, manifest in zip(accepted, manifests):
        base = f"clip_{int(m.start_ts):05d}_{int(m.end_ts):05d}"

        overlays = []
        panel_specs = []
        if plan_broll and manifest.get("beats"):
            use_renderer = renderer if (render and config.video_render_enabled()) else None
            # MOTION beats -> Higgsfield b-roll cutaways. STILL/concept beats ->
            # Treatment B text side panels over the live footage (no image render,
            # never a full-frame takeover), so still_renderer stays None.
            try:
                overlays = render_overlays(
                    manifest, renderer=use_renderer, budget=motion_budget,
                    still_renderer=None)
            except VideoEditorError as exc:
                print(f"video-episode: overlay render stopped: {exc}", flush=True)
            for b in manifest["beats"]:
                if b.get("route") == "still":
                    ct = b.get("card_text") or b.get("concept", "")
                    panel_specs.append({
                        "start": float(b["offset"]),
                        "end": float(b["offset"]) + float(b.get("duration", 4.0)),
                        "eyebrow": _eyebrow_for(b.get("concept", "")),
                        "headline": ct, "red_word": _pick_red_word(ct)})

        files = {}
        for aspect in aspects:
            for captioned in (True, False):
                mode = "cap" if captioned else "ad"
                try:
                    path = assemble_clip(m, media_path, transcript, overlays,
                                         output_dir, base, aspect=aspect,
                                         captioned=captioned, panel_specs=panel_specs,
                                         account_key=account_key)
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

