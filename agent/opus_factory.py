"""
Opus video factory (master flag AGENT_OPUS_FACTORY_ENABLED, default OFF).

Turns the back catalogue of finished Opus clips into DRAFT posts held for
approval. It EXTENDS the existing Opus client + poller (opus_ingest.OpusAPI); it
does not replace them and it never publishes.

Pipeline, one part per stage (this file grows a stage per commit):
  1. scan        finished clips across ALL collections (no allowlist) -> ClipRecord
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

ROUTE CONTRACT (verified 2026-07-09, source of truth = the legacy pull-opus poller):
The documented Opus API (https://help.opus.pro/api-reference) has NO bulk
project-listing endpoint. Discovery is by collection. The proven routes the
legacy poller (opus_ingest) authenticates and lists with are:
  - GET /api/collections?q=mine                        -> the account's collections
  - GET /api/exportable-clips?q=findByCollectionId&collectionId=<id>
  - GET /api/exportable-clips?q=findByProjectId&projectId=<id>   (pinned ids only)
  - Auth: Authorization: Bearer <OPUS_API_KEY>  (+ optional x-opus-org-id)
  - Base: AGENT_OPUS_API_BASE (default https://api.opus.pro)
The factory originally GUESSED GET /api/projects?q=mine for its "all-project
scan". That path does not exist and returns 404 NotFoundException, so the scan
saw zero clips even with a valid key. scan() now discovers via the proven
collections route (an all-COLLECTION scan, no hand-maintained allowlist), plus
any pinned AGENT_OPUS_PROJECT_IDS. The base URL and auth header were never the
problem: both are shared with the legacy poller through OpusAPI._get.
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


# The documented discovery endpoint is /api/exportable-clips: by its name and
# contract it returns ONLY exportable (finished) clips, each carrying a
# uriForExport (id/title/description/durationMs/uriForExport/createdAt). So the
# presence of an export URL IS the finished signal, and that is the primary
# filter below. This set is a DEFENSIVE fallback for any shape that omits the
# URL but carries an explicit status: a clip with no URL AND a status not in
# this set is excluded. Verbose scan prints the raw status of every excluded
# clip so the operator can spot and add a new live value here.
_FINISHED_STATUSES = frozenset({
    "done", "completed", "finished", "ready", "exported",
    "success", "succeeded", "published",
})


def normalize_clip(clip, project_id, source_title=""):
    """One raw Opus clip dict -> a ClipRecord, or None when it is not a finished,
    exportable clip. A clip is excluded when it has no usable export URL AND its
    status field (if present) is not in _FINISHED_STATUSES."""
    clip_id = str(clip.get("id", "") or "")
    download_url = str(clip.get("uriForExport", "") or clip.get("downloadUrl", "")
                       or clip.get("download_url", "") or clip.get("exportUrl", "")
                       or clip.get("export_url", "") or "")
    if not clip_id:
        return None
    if not download_url:
        raw_status = clip.get("status") or clip.get("clipStatus") or ""
        if raw_status and str(raw_status).lower() not in _FINISHED_STATUSES:
            return None  # still processing / failed
        # status is "done" or equivalent but URL is missing — exclude and let
        # verbose mode show the raw status so the operator can investigate
        return None
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
    """True when a clip's source title names the podcast show (case-insensitive).
    The source title is the clip's collection name (or pinned project id), set at
    scan time; organize the show's clips into a collection named after the show
    (AGENT_OPUS_PODCAST_SHOW). Podcast-sourced clips tag bucket=podcast without
    the lexicon."""
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


# ---- Part 6: dedupe + no-repost ledger -------------------------------------------------
# The clip_id ledger lives in the volume-backed kv store (like every other
# "seen" ledger: comment_seen, podcast_release_carded). A clip is pulled and
# drafted at most once; a posted clip is tracked so reporting can measure
# before/after and it is never re-drafted.
_LEDGER_DRAFTED = "opus_drafted_"
_LEDGER_POSTED = "opus_posted_"


def is_drafted(clip_id):
    from . import db
    return bool(db.kv_get(f"{_LEDGER_DRAFTED}{clip_id}"))


def is_posted(clip_id):
    from . import db
    return bool(db.kv_get(f"{_LEDGER_POSTED}{clip_id}"))


def already_seen(clip_id):
    """True once a clip has been drafted OR posted: it never enters the plan again."""
    return is_drafted(clip_id) or is_posted(clip_id)


def mark_drafted(clip_id, when="1"):
    """Stamp a clip as drafted (called when a draft is actually built, Part 7)."""
    from . import db
    db.kv_set(f"{_LEDGER_DRAFTED}{clip_id}", when or "1")


def mark_posted(clip_id, when="1"):
    """Stamp a clip as posted for the reporting before/after ledger."""
    from . import db
    db.kv_set(f"{_LEDGER_POSTED}{clip_id}", when or "1")
    db.audit("opus_factory", clip_id, "clip posted (ledger)")


def dedupe(records):
    """
    Split records into (fresh, seen). A clip already in the ledger (drafted or
    posted) is marked status='dupe' and never re-drafted; fresh clips pass
    through untouched. Records already dropped/held/shortlisted keep their state.
    """
    fresh, seen = [], []
    for r in records:
        if already_seen(r.clip_id):
            r.status = "dupe"
            r.reason = "already drafted or posted (ledger)"
            seen.append(r)
        else:
            fresh.append(r)
    return fresh, seen


# ---- Part 7: calendar routing ----------------------------------------------------------
def _video_slots(start_day, weeks):
    """
    (day, bucket) for every VIDEO-format slot on or after start_day across the
    horizon, in day order. The bucket is the category the week plan actually
    assigns that day (so book/doctrine cycling is respected); the format comes
    from the seven-day schedule. Podcast keeps Thu, platform keeps Tue/Sat.
    """
    from datetime import date, timedelta
    from . import category_plan
    from .content_categories import _DAILY_SCHEDULE
    start = str(start_day)[:10]
    monday = date.fromisoformat(category_plan._monday_of(start))
    slots, seq = [], 0
    for w in range(weeks):
        entries, seq = category_plan.week_plan(
            (monday + timedelta(days=7 * w)).isoformat(), seq)
        for e in entries:
            fmt = _DAILY_SCHEDULE.get(e["weekday"], (None, None, None))[1]
            if fmt == "video" and e["day"] >= start:
                slots.append((e["day"], e["category"]))
    return slots


def _iso_week(day):
    from datetime import date
    return date.fromisoformat(str(day)[:10]).isocalendar()[:2]


def route(records, start_day, account_key="lasso_ig", platform="instagram",
          weeks=4, store=None, commit=True):
    """
    Place eligible clips (status '', bucketed, captioned) into VIDEO slots on
    their bucket's cadence and build a DRAFT for each, held for approval.

    Honors: the per-week Opus cap (AGENT_OPUS_WEEKLY_CAP across all buckets),
    one clip per day (no-repeat spacing), and the bucket cadence (a clip only
    lands on a day whose plan category is its bucket). Every draft is PENDING
    and never published; the trust ladder + first-post gate stay the publish
    path's authority (a PENDING draft can never auto-publish). Placement stamps
    the ledger so a re-run never re-drafts the clip.

    Returns the list of drafts (empty while the master flag is OFF).
    """
    if not config.opus_factory_enabled():
        return []
    from .drafter import Draft, DraftStatus, _make_id
    from . import schedule
    eligible = [r for r in records if r.status == "" and r.bucket and r.caption]
    cap = config.opus_weekly_cap()
    per_week, used_days, drafts = {}, set(), []
    for day, bucket in _video_slots(start_day, weeks):
        if day in used_days:
            continue
        wk = _iso_week(day)
        if per_week.get(wk, 0) >= cap:
            continue
        clip = next((r for r in eligible
                     if r.status == "" and r.bucket == bucket), None)
        if clip is None:
            continue
        draft = Draft(
            draft_id=_make_id(account_key, f"opus_{clip.clip_id}", day),
            account_key=account_key, platform=platform,
            caption=clip.caption, hashtags=[],
            creative_path="", creative_public_url=clip.download_url,
            scheduled_for=schedule.scheduled_for(day), status=DraftStatus.PENDING,
            source_fragments=[f"cite:opus_{clip.clip_id}"],
            day_key=day, draft_type="opus_clip", category=bucket)
        # commit=False (dry-run) plans the placement in memory only: no ledger
        # stamp, no store write, so a dry-run is truly side-effect free.
        if commit:
            if store is not None:
                store.put(draft)
            mark_drafted(clip.clip_id, day)
        clip.status = "draft"
        clip.scheduled_for = draft.scheduled_for
        used_days.add(day)
        per_week[wk] = per_week.get(wk, 0) + 1
        drafts.append(draft)
    return drafts


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
    Every finished clip the account can reach, normalized to ClipRecords.

    Discovery uses the PROVEN documented routes (see ROUTE CONTRACT at the top of
    this module): GET /api/collections?q=mine (api.list_collections_detailed) for
    the account's collections, plus any pinned AGENT_OPUS_PROJECT_IDS (the
    documented manual escape hatch). There is NO bulk project-listing endpoint,
    so this is an all-COLLECTION scan, not an all-project one, and it needs no
    hand-maintained allowlist. For each source it lists clips via
    GET /api/exportable-clips. Read only: normalizes, downloads nothing. An auth
    or transport failure (OpusScanError) propagates so the caller surfaces it
    loudly instead of returning a misleading empty list. Returns [] while
    AGENT_OPUS_FACTORY_ENABLED is OFF.
    """
    if not config.opus_factory_enabled():
        return []
    from . import opus_ingest
    from .opus_ingest import OpusScanError
    api = api or opus_ingest._default_api()
    if api is None:
        return []
    vprint = print if verbose else (lambda *a, **k: None)

    # (q, {"id","title"}) sources: collections (proven discovery) + pinned projects.
    sources = []
    try:
        for c in api.list_collections_detailed():
            sources.append(("findByCollectionId", c))
    except OpusScanError:
        raise  # auth / transport / wrong-endpoint error — surface it loudly
    except Exception as e:
        vprint(f"[opus-factory] list_collections_detailed failed: "
               f"{type(e).__name__}: {e}")
        return []
    for pid in opus_ingest.validated_project_ids(config.opus_project_ids()):
        sources.append(("findByProjectId", {"id": pid, "title": ""}))
    vprint(f"[opus-factory] scanning {len(sources)} source(s) "
           f"(collections + pinned projects)")

    records, seen = [], set()
    for q, src in sources:
        sid = src.get("id") if isinstance(src, dict) else str(src)
        title = src.get("title", "") if isinstance(src, dict) else ""
        if not sid:
            continue
        try:
            clips = api.list_exportable_clips(q, sid)
        except OpusScanError:
            raise  # auth / transport error — propagate
        except Exception as e:
            vprint(f"[opus-factory] list clips failed for {q}:{sid}: "
                   f"{type(e).__name__}: {e}")
            continue
        included, excluded_status = 0, []
        for clip in clips:
            rec = normalize_clip(clip, sid, title)
            if rec is None:
                raw_st = clip.get("status") or clip.get("clipStatus") or "(no status)"
                excluded_status.append(str(raw_st))
                continue
            if rec.clip_id in seen:
                continue
            seen.add(rec.clip_id)
            records.append(rec)
            included += 1
        vprint(f"[opus-factory] source {q}:{sid}: {included} clip(s) included, "
               f"{len(excluded_status)} excluded (raw statuses seen: "
               f"{sorted(set(excluded_status)) or 'none'})")
    return records


# ---- Part 8: pipeline + opus-pull CLI + ops surface ------------------------------------
def run_pipeline(api=None, start_day=None, dry=True, account_key="lasso_ig",
                 platform="instagram", store=None):
    """
    The whole factory in order: scan -> dedupe -> score gate -> tag -> hook ->
    caption -> route. Returns a plan dict:
      {"drafts": [Draft], "drafted": [rec], "held": [rec], "shortlist": [rec],
       "dropped": [rec], "dupes": [rec]}
    dry=True plans without any side effect (no ledger stamp, no store, no post).
    Returns None while the master flag is OFF.
    """
    if not config.opus_factory_enabled():
        return None
    from datetime import date, timedelta
    if start_day is None:
        # next Monday from an env-provided anchor, or fall back to a fixed plan
        # start; callers (the CLI) pass the real day. Never uses Date.now here.
        start_day = "2026-08-03"
    records = scan(api=api)
    fresh, dupes = dedupe(records)
    survivors, dropped = score_gate(fresh)
    tag_all(survivors)
    hook_check_all(survivors)
    for r in survivors:
        if r.status == "":
            write_caption(r)
    drafts = route([r for r in survivors if r.status == ""], start_day,
                   account_key=account_key, platform=platform,
                   store=store, commit=not dry)
    return {
        "drafts": drafts,
        "drafted": [r for r in survivors if r.status == "draft"],
        "held": [r for r in survivors if r.status == "hold"],
        "shortlist": [r for r in survivors if r.status == "shortlist"],
        "dropped": dropped,
        "dupes": dupes,
    }


def _plan_lines(plan):
    """The ranked plan + held/rejected list as printable lines (dry-run output)."""
    lines = ["opus-pull PLAN (ranked, held for approval):"]
    for r in sorted(plan["drafted"], key=lambda r: r.opus_score, reverse=True):
        hook = hook_opening(r.transcript) or r.title
        lines.append(f"  score {r.opus_score:g}  {r.bucket:9s}  {r.scheduled_for[:10]}  "
                     f"{hook[:60]}")
    rejected = ([("dupe", r) for r in plan["dupes"]]
                + [("dropped", r) for r in plan["dropped"]]
                + [("held", r) for r in plan["held"]]
                + [("shortlist", r) for r in plan["shortlist"]])
    if rejected:
        lines.append("held / rejected:")
        for kind, r in rejected:
            lines.append(f"  [{kind}] {r.clip_id}: {r.reason}")
    lines.append(f"summary: {len(plan['drafted'])} drafted, "
                 f"{len(plan['shortlist'])} shortlisted, {len(plan['held'])} held, "
                 f"{len(plan['dropped'])} dropped, {len(plan['dupes'])} dupe(s)")
    return lines


def opus_pull_cli(write=False, api=None, start_day=None, poster=None, store=None,
                  account_key="lasso_ig", platform="instagram"):
    """
    python -m agent opus-pull [--write]

    Dry-run (default): prints the ranked plan + the held/rejected list with
    reasons; writes NOTHING (no ledger, no store, no Slack). --write: builds the
    held drafts, posts each to the ops channel with its preview/caption/bucket/
    score for the tap, and one digest line. Behind the master flag.
    """
    if not config.opus_factory_enabled():
        print("opus-pull: OFF (set AGENT_OPUS_FACTORY_ENABLED=true). Nothing done.")
        return None
    from .opus_ingest import OpusScanError
    try:
        plan = run_pipeline(api=api, start_day=start_day, dry=not write,
                            account_key=account_key, platform=platform, store=store)
    except OpusScanError as exc:
        print(f"opus-pull: AUTH ERROR (HTTP {exc.http_status}) — the scan could not "
              f"complete. Run 'agent opus-doctor' to diagnose. "
              f"Body: {exc.body_snippet}")
        return None
    for line in _plan_lines(plan):
        print(line)
    if not write:
        print("opus-pull: DRY RUN, nothing was written or posted.")
        return plan
    if poster is not None:
        for d, r in zip(plan["drafts"], sorted(plan["drafted"],
                                               key=lambda r: r.scheduled_for)):
            try:
                poster.post_approval_card(d)
            except Exception as e:
                print(f"[opus-pull] card post failed for {d.draft_id}: "
                      f"{type(e).__name__}: {e}")
        try:
            poster.post_notice(
                f"opus-pull: {len(plan['drafted'])} clip(s) drafted and held for "
                f"the tap; {len(plan['shortlist'])} shortlisted, "
                f"{len(plan['held'])} held, {len(plan['dropped'])} below bar, "
                f"{len(plan['dupes'])} already seen.")
        except Exception as e:
            print(f"[opus-pull] digest post failed: {type(e).__name__}: {e}")
    return plan
