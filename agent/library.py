"""
Stage 1 content library: a local folder Blake points the agent at.

Each creative is a media file (image/video). An optional sidecar file with the
same stem and a .txt or .json extension carries CLIENT-PROVIDED notes (facts the
client gave us). The agent may use those notes; it may never invent new ones.

Portal-backed library is a later stage. See stubs.py: read_portal_library().
"""

import json
import os
from dataclasses import dataclass, field

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}


@dataclass
class Creative:
    path: str
    media_type: str            # "image" or "video"
    client_note: str = ""      # client-provided context ONLY. May be empty.
    public_url: str = ""       # set by Blake-by-hand hosting; required for IG publish

    @property
    def stem(self):
        return os.path.splitext(os.path.basename(self.path))[0]


def list_creatives(library_path):
    creatives = []
    if not library_path or not os.path.isdir(library_path):
        return creatives
    for name in sorted(os.listdir(library_path)):
        full = os.path.join(library_path, name)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in IMAGE_EXTS:
            mtype = "image"
        elif ext in VIDEO_EXTS:
            mtype = "video"
        else:
            continue  # skip sidecars and junk
        creatives.append(
            Creative(path=full, media_type=mtype, **_load_sidecar(library_path, name))
        )
    return creatives


def _load_sidecar(library_path, media_name):
    """Load client-provided note + optional public_url. Never fabricated here."""
    stem = os.path.splitext(media_name)[0]
    out = {"client_note": "", "public_url": ""}
    txt = os.path.join(library_path, stem + ".txt")
    js = os.path.join(library_path, stem + ".json")
    if os.path.exists(js):
        try:
            with open(js, "r", encoding="utf-8") as f:
                data = json.load(f)
            out["client_note"] = str(data.get("note", "")).strip()
            out["public_url"] = str(data.get("public_url", "")).strip()
        except Exception:
            pass
    elif os.path.exists(txt):
        with open(txt, "r", encoding="utf-8") as f:
            out["client_note"] = f.read().strip()
    return out


def pick_next(account, library_path, already_used):
    """
    Pick one creative for this account: least-recently-used, deterministic.
    `already_used` is a list of creative paths we've posted for this account.
    Returns a Creative or None if the library is empty.
    """
    creatives = list_creatives(library_path)
    if not creatives:
        return None
    used = set(already_used or [])
    fresh = [c for c in creatives if c.path not in used]
    pool = fresh if fresh else creatives  # if all used, cycle from the top
    return pool[0]
