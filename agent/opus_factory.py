"""
Opus video factory (master flag AGENT_OPUS_FACTORY_ENABLED, default OFF).

Turns the back catalogue of finished Opus clips into DRAFT posts held for
approval. It EXTENDS the existing Opus client + poller (opus_ingest.OpusAPI); it
does not replace them and it never publishes.

Pipeline, one part per stage (this file grows a stage per commit):
  1. scan        finished clips across ALL projects (no allowlist) -> ClipRecord
  2. score gate  drop anything below the score floor FIRST, and outside the
                 duration window, before any other work
  3. tag         bucket from the transcript (podcast-sourced -> podcast; no
                 confident theme -> HOLD + alert, never drafted)
  4. hook        the opening must carry a claim/number/question, else shortlist
  5. caption     evergreen caption from transcript + approved facts only
  6. dedupe      clip_id ledger on the volume: pulled/drafted/posted at most once
  7. route       calendar slot on the bucket cadence -> DRAFT held for approval
  8. CLI         opus-pull dry-run plan / write held drafts + ops surface

Everything is read-only until Part 7 builds a draft, and even then a draft is
PENDING and held for the tap. The fabrication gate stays the sole authority on
claims: a caption may assert only what the transcript or the approved facts file
already say.
"""

import re
from dataclasses import dataclass

from . import config


@dataclass
class ClipRecord:
    """One finished Opus clip, normalized. Later stages fill the lower fields."""
    clip_id: str
    project_id: str
    source_title: str            # the project's title (its show/source name)
    title: str
    opus_score: float
    duration_s: float
    transcript: str
    download_url: str
    # filled by later stages (score/tag/hook/caption/route):
    bucket: str = ""             # one of content_categories.CATEGORIES
    confidence: float = 0.0
    caption: str = ""
    status: str = ""             # "" | drop | hold | shortlist | draft | posted
    reason: str = ""             # why dropped/held/shortlisted
    scheduled_for: str = ""      # set by routing (Part 7)


# ---- field normalization (shape tolerant: the API's key names vary) --------------
_SCORE_KEYS = ("opus_score", "score", "viralityScore", "virality_score",
               "virality", "clipScore")
_TRANSCRIPT_KEYS = ("transcript", "transcriptText", "transcript_text",
                    "text", "captions", "caption")


def _first_num(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.strip().rstrip("%"))
            except ValueError:
                continue
    return 0.0


def _transcript_text(clip):
    for k in _TRANSCRIPT_KEYS:
        v = clip.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v:
            parts = []
            for seg in v:
                if isinstance(seg, str):
                    parts.append(seg)
                elif isinstance(seg, dict):
                    parts.append(str(seg.get("text", "") or seg.get("content", "")))
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                return joined
    return ""


def normalize_clip(clip, project_id, source_title=""):
    """One raw Opus clip dict -> a ClipRecord, or None when it is not a finished,
    exportable clip (no id or no export url)."""
    clip_id = str(clip.get("id", "") or "")
    download_url = str(clip.get("uriForExport", "") or clip.get("downloadUrl", "")
                       or clip.get("download_url", "") or "")
    if not clip_id or not download_url:
        return None  # unfinished / not exportable: excluded
    duration_ms = _first_num(clip, ("durationMs", "duration_ms"))
    duration_s = duration_ms / 1000.0 if duration_ms else _first_num(
        clip, ("durationSeconds", "duration_s", "duration"))
    return ClipRecord(
        clip_id=clip_id,
        project_id=str(project_id or ""),
        source_title=str(source_title or ""),
        title=str(clip.get("title", "") or ""),
        opus_score=_first_num(clip, _SCORE_KEYS),
        duration_s=round(duration_s, 2),
        transcript=_transcript_text(clip),
        download_url=download_url,
    )


# ---- Part 3: bucket tagger -------------------------------------------------------------
# The LASSO theme lexicon: theme -> (word-boundary keywords, the bucket it routes
# to). Buckets are from content_categories.CATEGORIES. A non-podcast clip never
# routes to podcast (that bucket is reserved for podcast-sourced clips).
# Classification reads the transcript ONLY and never invents a topic.
THEME_LEXICON = {
    "follow_up_problem": (
        ["follow up", "follow-up", "followup", "chase", "chasing", "nurture",
         "dead lead", "dead leads", "ghost", "ghosted"], "doctrine"),
    "speed_to_lead": (
        ["speed to lead", "five minutes", "5 minutes", "respond fast",
         "first to respond", "within minutes", "response time"], "doctrine"),
    "diagnostic_order": (
        ["close rate", "show rate", "booking rate", "diagnose", "diagnosis",
         "funnel", "the first leg", "in order"], "platform"),
    "six_engines": (
        ["six engines", "ad engine", "google ads", "one platform", "one login",
         "the portal", "done for you"], "platform"),
    "booking_gap": (
        ["booked", "booking", "appointments", "on the calendar", "71.9", "18.5",
         "no shows", "no-shows"], "platform"),
    "retention": (
        ["retention", "churn", "cancellations", "cancel", "stay longer",
         "lifetime value", "ltv", "member for"], "doctrine"),
    "positioning": (
        ["positioning", "your message", "message clarity", "stand out",
         "differentiate", "who you serve", "your avatar"], "platform"),
}


def is_podcast_sourced(record):
    """True when a clip's project title names the podcast show (case-insensitive).
    Podcast-sourced clips tag bucket=podcast without the lexicon."""
    show = config.opus_podcast_show().strip().lower()
    return bool(show) and show in (record.source_title or "").lower()


def classify_transcript(transcript):
    """
    Classify a transcript against the theme lexicon. Returns
    {"bucket", "confidence" (0..1), "themes": [matched theme names]}. Pure and
    transcript-only: no theme match -> empty bucket, 0 confidence (nothing
    invented). Confidence rises with keyword coverage; one clean theme hit lands
    exactly at the default relevance floor (0.65).
    """
    text = (transcript or "").lower()
    theme_hits = {}
    for theme, (keywords, bucket) in THEME_LEXICON.items():
        hits = 0
        for kw in keywords:
            hits += len(re.findall(r"\b" + re.escape(kw) + r"\b", text))
        if hits:
            theme_hits[theme] = (hits, bucket)
    if not theme_hits:
        return {"bucket": "", "confidence": 0.0, "themes": []}
    total = sum(h for h, _b in theme_hits.values())
    top = max(sorted(theme_hits.items()), key=lambda kv: kv[1][0])
    confidence = min(1.0, 0.5 + 0.15 * total)
    return {"bucket": top[1][1], "confidence": round(confidence, 3),
            "themes": sorted(theme_hits)}


def tag_clip(record, poster=None):
    """
    Tag ONE score-gate survivor with its bucket, in place. Podcast-sourced clips
    tag bucket=podcast directly. Otherwise classify from the transcript: no
    theme, or confidence below AGENT_OPUS_RELEVANCE_FLOOR, sets status='hold'
    with a reason and ONE ops alert; it is never drafted. On-topic clips get
    their bucket + confidence and stay eligible.
    """
    if is_podcast_sourced(record):
        record.bucket = "podcast"
        record.confidence = 1.0
        return record
    result = classify_transcript(record.transcript)
    floor = config.opus_relevance_floor()
    record.confidence = result["confidence"]
    if not result["bucket"] or result["confidence"] < floor:
        record.status = "hold"
        record.reason = ("off topic (no LASSO theme in the transcript)"
                         if not result["bucket"]
                         else f"relevance {result['confidence']:.2f} below floor "
                              f"{floor:.2f}")
        from . import ops_alerts
        ops_alerts.alert(
            f"opus factory: clip {record.clip_id} HELD, {record.reason}. "
            f"Title: {record.title[:60]!r}. Not drafted; a human decides.",
            poster=poster)
        return record
    record.bucket = result["bucket"]
    return record


def tag_all(records, poster=None):
    """Tag every score-gate survivor; returns the same list (mutated in place)."""
    for r in records:
        tag_clip(r, poster=poster)
    return records


# ---- Part 4: hook check ----------------------------------------------------------------
# ~2 seconds of speech is roughly the first 10 words; a strong hook lands a
# claim, a number, or a question inside that opening.
_HOOK_WORDS = 10
_HOOK_NUM_RE = re.compile(r"[\d$%]")
_HOOK_CLAIM_WORDS = (
    "most", "every", "never", "always", "nobody", "everyone", "stop",
    "biggest", "truth", "reason", "secret", "why", "how", "what", "if you",
    "you need", "here is", "the problem", "no one",
)


def hook_opening(transcript, words=_HOOK_WORDS):
    """The clip's opening ~2s: the first `words` words of the transcript."""
    return " ".join((transcript or "").split()[:words])


def has_strong_hook(transcript):
    """True when the opening carries a claim, a number, or a question. Reads the
    transcript's own words only; nothing is inferred beyond them."""
    opening = hook_opening(transcript).lower()
    if not opening:
        return False
    if "?" in opening:
        return True
    if _HOOK_NUM_RE.search(opening):
        return True
    return any(w in opening for w in _HOOK_CLAIM_WORDS)


def hook_check(record):
    """
    Demote an otherwise-eligible clip to the shortlist when its opening ~2s
    carries no claim, number, or question. Only acts on still-eligible records
    (status == ''); dropped/held records are left as they are. A strong hook
    leaves the record eligible to draft.
    """
    if record.status:
        return record  # already dropped or held; the hook check does not revive it
    if not has_strong_hook(record.transcript):
        record.status = "shortlist"
        record.reason = "weak hook (opening carries no claim, number, or question)"
    return record


def hook_check_all(records):
    for r in records:
        hook_check(r)
    return records


# ---- Part 5: caption writer from transcript --------------------------------------------
# Evergreen: these are back-catalog clips, so the podcast CTA points at the full
# episode, never "new episode is live". The footer rides podcast clips only.
PODCAST_FOOTER = ("Gym Marketing Made Simple by LASSO. Hosted by Sherman "
                  "Merricks and Blake Ruff.")
PODCAST_CTA = "Hear the full conversation on the podcast."
BUCKET_CTA = {
    "platform": "See what one platform for your whole gym looks like.",
    "doctrine": "Save this and put it to work this week.",
    "b2b": "Send this to a gym owner who needs it.",
    "summit": "Claim your seat at the summit.",
    "book": "Get the book.",
}
_DEFAULT_CTA = "Save this and put it to work this week."

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(text):
    return [s.strip() for s in _SENT_RE.split((text or "").strip()) if s.strip()]


def _sanitize(text):
    """The standing copy laws on client text: no vendor, no dash characters.
    Reuses the wording filter (vendor -> company/companies, dashes -> space)."""
    from .content_categories import filter_platform_copy
    return filter_platform_copy(text)


def write_caption(record):
    """
    Build the clip's evergreen caption FROM ITS TRANSCRIPT, in place, and return
    it. Shape: a hook (the clip's own opening sentence), a one or two line payoff
    (the next transcript sentences), then the CTA (soft CTA to the full episode +
    the podcast footer for podcast clips; the bucket CTA and NO footer otherwise).

    Every asserted line is lifted verbatim from the transcript and then
    sanitized (vendor + dash). The fabrication gate is the sole authority on
    claims: the caption is checked against the transcript's own sentences plus
    the approved facts file, so it can assert nothing the transcript or an
    approved source does not already say. A caption that cannot clear the gate
    is HELD, never drafted.
    """
    from . import rotation
    sents = _sentences(record.transcript)
    if not sents:
        record.status = "hold"
        record.reason = "no transcript to caption from"
        return ""
    hook = sents[0]
    payoff = " ".join(sents[1:3])
    cta = (PODCAST_CTA if record.bucket == "podcast"
           else BUCKET_CTA.get(record.bucket, _DEFAULT_CTA))
    parts = [hook] + ([payoff] if payoff else []) + [cta]
    caption = "\n\n".join(parts)
    if record.bucket == "podcast":
        caption += "\n\n" + PODCAST_FOOTER
    caption = _sanitize(caption)

    # Fabrication gate: approved = the (sanitized) transcript sentences + the
    # approved facts file. A claim-bearing caption line that neither the
    # transcript nor an approved source clears fails the gate -> HELD.
    approved = [_sanitize(s) for s in sents] + list(rotation._approved_claims())
    if not rotation.is_gate_clean(caption, approved_claims=approved):
        record.status = "hold"
        record.reason = "caption failed the fabrication gate"
        record.caption = ""
        return ""
    record.caption = caption
    return caption


# ---- Part 2: score gate FIRST ----------------------------------------------------------
def passes_score_gate(record):
    """
    (ok, reason). The hard gate that runs BEFORE any tagging, hook check, or
    caption work: a clip below AGENT_OPUS_SCORE_FLOOR (default 90), or outside
    the AGENT_OPUS_DURATION_MIN/MAX window (default 15..95s), is dropped. A
    survivor returns (True, "").
    """
    floor = config.opus_score_floor()
    if record.opus_score < floor:
        return False, f"score {record.opus_score:g} below floor {floor:g}"
    lo, hi = config.opus_duration_min(), config.opus_duration_max()
    if record.duration_s < lo or record.duration_s > hi:
        return False, (f"duration {record.duration_s:g}s outside window "
                       f"{lo:g}..{hi:g}s")
    return True, ""


def score_gate(records):
    """
    Split scanned records into (survivors, dropped). Dropped records are marked
    status='drop' with the reason; survivors pass through untouched for the next
    stage. This is the FIRST filter after the scan (score before everything).
    """
    survivors, dropped = [], []
    for r in records:
        ok, reason = passes_score_gate(r)
        if ok:
            survivors.append(r)
        else:
            r.status = "drop"
            r.reason = reason
            dropped.append(r)
    return survivors, dropped


def scan(api=None, verbose=False):
    """
    Every finished clip across ALL Opus projects, normalized to ClipRecords.
    Read only: enumerates projects (api.list_projects), lists each project's
    exportable clips, normalizes, downloads nothing. NO pinned allowlist is
    required or consulted. Returns [] while AGENT_OPUS_FACTORY_ENABLED is OFF.
    """
    if not config.opus_factory_enabled():
        return []
    from . import opus_ingest
    api = api or opus_ingest._default_api()
    if api is None:
        return []
    vprint = print if verbose else (lambda *a, **k: None)

    try:
        projects = api.list_projects()
    except Exception as e:
        vprint(f"[opus-factory] list_projects failed: {type(e).__name__}: {e}")
        return []
    vprint(f"[opus-factory] scanning {len(projects)} project(s)")

    records, seen = [], set()
    for proj in projects:
        pid = proj.get("id") if isinstance(proj, dict) else str(proj)
        title = proj.get("title", "") if isinstance(proj, dict) else ""
        if not pid:
            continue
        try:
            clips = api.list_exportable_clips("findByProjectId", pid)
        except Exception as e:
            vprint(f"[opus-factory] list clips failed for {pid}: "
                   f"{type(e).__name__}: {e}")
            continue
        for clip in clips:
            rec = normalize_clip(clip, pid, title)
            if rec is None or rec.clip_id in seen:
                continue
            seen.add(rec.clip_id)
            records.append(rec)
    return records
