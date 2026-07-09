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
