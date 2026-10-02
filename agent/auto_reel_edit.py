"""Bounded montage pacing for already verified source windows.

A reference-inspired edit, not a claim of semantic storytelling or beat detection.
Keep landscape footage as a brief context insert; open and close with portrait
footage when available. Split long windows without repeating source frames.
"""
from dataclasses import replace
import shutil

from .auto_reel_render import _source_layout


def arrange_shots(plan):
    probe = shutil.which('ffprobe')
    if not probe:
        raise ValueError('ffprobe required to plan source framing')
    portrait, landscape = [], []
    original_ends = {}
    for segment in plan.segments:
        aspect, _, _ = _source_layout(segment.source_path, probe)
        if aspect > .70:
            landscape.append(replace(segment, end_ts=min(segment.end_ts, segment.start_ts+3)))
            original_ends[(segment.asset_id, segment.start_ts)] = segment.end_ts
        else:
            portrait.append(segment)
    # All-landscape inputs keep their full framing; do not invent portrait coverage.
    if not portrait:
        return plan
    first, remainder = [], []
    for segment in portrait:
        if segment.duration >= 6:
            mid = round((segment.start_ts+segment.end_ts)/2, 2)
            first.append(replace(segment, end_ts=mid))
            remainder.append(replace(segment, start_ts=mid))
        else:
            first.append(segment)
    if remainder:
        shots = first + landscape + remainder
    elif len(first)>1:
        shots = first[:-1] + landscape + first[-1:]
    else:
        shots = first + landscape
    if len(shots)>10:
        # Keep the existing segment cap; no late silent discard of selected assets.
        return plan
    total = round(sum(s.duration for s in shots),2)
    # Keep the existing fifteen-second automatic montage contract. Restore only
    # already-verified landscape coverage when the brief insert would undershoot.
    for index, shot in enumerate(shots):
        limit = original_ends.get((shot.asset_id, shot.start_ts), shot.end_ts)
        extra = min(max(0, 15-total), max(0, limit-shot.end_ts))
        if extra:
            shots[index] = replace(shot, end_ts=shot.end_ts+extra)
            total = round(total+extra, 2)
    if total < 15:
        # Do not extend outside verified windows to meet an arbitrary duration.
        raise ValueError('At least fifteen seconds of usable montage footage are required')
    # A restored landscape window need not become one long visual interruption.
    # Interleave its disjoint halves with the existing portrait coverage.
    tall = [s for s in shots if (s.asset_id, s.start_ts) not in original_ends]
    wide = [s for s in shots if (s.asset_id, s.start_ts) in original_ends]
    if len(wide) == 1 and wide[0].duration >= 4.5 and len(tall) >= 3:
        w = wide[0]
        mid = round((w.start_ts+w.end_ts)/2, 2)
        shots = [tall[0], replace(w, end_ts=mid), *tall[1:-1],
                 replace(w, start_ts=mid), tall[-1]]
    plan.segments = shots
    plan.total_sec = total
    return plan
