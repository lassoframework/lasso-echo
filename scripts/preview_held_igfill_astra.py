#!/usr/bin/env python3
"""Create private Astra brief previews for exact PR249 held future cards.

This operator never renders, hosts, inserts, approves, releases, or publishes.
Briefs are previews for a human to review before any separately authorized render.
The hash proves the captured source bytes are unchanged. A separate private human
approval receipt attests that the source URL and listed colors match those bytes;
the script cannot independently establish that visual judgment.
The original private hold receipt and live photo inventory are mandatory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import astra_prompt, client_sources, gym_media_index
from agent.portal_calendar_store import SupabaseCalendarStore
from agent.voice import AUTO_DRAFTED_MARKER
from scripts.hold_future_igfill_photos import REASON, _image, _is_fill, _receipt
from scripts.release_future_igfill_photos import _original_receipt
from scripts.restage_future_igfill_photos import preflight as photo_preflight


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _voice(base, account_key):
    path = Path(astra_prompt._voice_path_for(account_key))
    if path.resolve() == Path(astra_prompt.config.VOICE_DOC_PATH).resolve():
        raise ValueError(f"{base}: client voice resolved to LASSO voice")
    raw = path.read_text(encoding="utf-8")
    unresolved = re.search(
        r"(?im)(?:\bTODO\b|\bTBD\b|\bPLACEHOLDER\b|\bFILL\s+IN\b|"
        r"\bINSERT\s+HERE\b|\{\{[^}]+\}\}|\[[^\]\n]*(?:insert|your|gym name|example)[^\]\n]*\])",
        raw)
    if not raw.strip() or AUTO_DRAFTED_MARKER in raw or unresolved:
        raise ValueError(f"{base}: voice absent, auto drafted, or has unresolved template")
    return path


def _source(base, account_key):
    sources = client_sources.approved_sources(account_key) or []
    cited = [s for s in sources if s.status == "approved" and s.text.strip()
             and s.citation.strip() and s.citation.startswith("https://")]
    if not cited:
        raise ValueError(f"{base}: no approved status cited factual source")
    return sorted(cited, key=lambda s: (str(s.id), s.text))[0]


def _palette_manifest(path, gym):
    """Require operator supplied colors bound to captured source bytes."""
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    item = manifest.get(gym) if isinstance(manifest, dict) else None
    if not isinstance(item, dict):
        raise ValueError(f"{gym}: palette manifest entry missing")
    colors = item.get("colors")
    source_url = item.get("source_url")
    source_file = item.get("source_file")
    source_sha256 = item.get("source_sha256")
    if (not isinstance(colors, list) or not colors or
            any(not isinstance(c, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", c)
                for c in colors) or
            not isinstance(source_url, str) or not source_url.startswith("https://") or
            not isinstance(source_file, str) or not source_file or
            not isinstance(source_sha256, str) or
            not re.fullmatch(r"[0-9a-f]{64}", source_sha256) or
            _sha256(source_file) != source_sha256):
        raise ValueError(f"{gym}: palette source citation, colors, or source hash invalid")
    return {"colors": colors, "path": str(Path(path).resolve()),
            "source_url": source_url, "source_file": source_file,
            "source_sha256": source_sha256}


def _approved_palette(path, manifest_path, gym, palette):
    """Bind a separate owned private human approval receipt to exact evidence."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()):
            raise ValueError("palette approval receipt must be an owned private regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            approvals = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    item = approvals.get(gym) if isinstance(approvals, dict) else None
    if (not isinstance(item, dict) or item.get("decision") != "approved"
            or item.get("manifest_sha256") != _sha256(manifest_path)
            or item.get("source_sha256") != palette["source_sha256"]
            or item.get("colors") != palette["colors"]
            or not str(item.get("approved_by") or "").strip()
            or not str(item.get("approved_at") or "").strip()
            or not str(item.get("approval_reference") or "").startswith("https://")):
        raise ValueError(f"{gym}: exact human palette approval evidence missing")
    approved_at = datetime.fromisoformat(item["approved_at"].replace("Z", "+00:00"))
    if approved_at.tzinfo is None:
        raise ValueError(f"{gym}: palette approval timestamp lacks timezone")
    return {"approved_by": item["approved_by"], "approved_at": item["approved_at"],
            "approval_reference": item["approval_reference"],
            "receipt_sha256": _sha256(path)}


def build_previews(hold_receipt_path, *, palette_manifest_path, palette_approval_path,
                   store=None,
                   media_store=None, today=None):
    """Read exact held rows and return briefs. No writes or provider calls."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    source_receipt, held = _original_receipt(hold_receipt_path)
    store = store or SupabaseCalendarStore()
    media_store = media_store or gym_media_index.default_store()
    photo, _ = photo_preflight(store, media_store, hold_receipt_path, today=today)
    if photo["conflict_ids"]:
        raise ValueError(f"held rows changed: {photo['conflict_ids']}")
    blocked = {str(row_id): item["reason"] for item in photo["blocked"]
               for row_id in item["row_ids"]}
    rows = []
    for held_row in held:
        gym, row_id = held_row["gym_id"], str(held_row["id"])
        current = store.get_row(gym, row_id)
        if current is None or _image(current) != held_row:
            raise ValueError(f"{row_id}: held row changed")
        if (blocked.get(row_id) != "no_unused_approved_gym_photo"
                or not _is_fill(current) or current["media_not_ready_reason"] != REASON
                or current["variant_status"] != "active"
                or current["status"] not in ("pending", "approved")
                or current.get("published_at") or current.get("late_post_id")
                or date.fromisoformat(str(current["post_date"])[:10]) < date.fromisoformat(today)):
            continue
        account_key = f"{gym}_ig"
        if astra_prompt._account_base(account_key) != gym:
            raise ValueError(f"{row_id}: tenant mismatch")
        palette = _palette_manifest(palette_manifest_path, gym)
        palette_approval = _approved_palette(palette_approval_path, palette_manifest_path,
                                             gym, palette)
        voice_path = _voice(gym, account_key)
        fact = _source(gym, account_key)
        headline = " ".join(fact.text.split()[:8]).strip(".,:;!? ")
        if not headline:
            raise ValueError(f"{gym}: empty source headline")
        brief = astra_prompt.build_infographic_brief(
            headline, [fact.text], surface="feed post", account_key=account_key,
            voice_path=str(voice_path), gym_palette=palette)
        rows.append({"row_id": row_id, "gym_id": gym,
                     "post_date": current["post_date"], "status": current["status"],
                     "source_id": fact.id, "source_citation": fact.citation,
                     "source_status": fact.status, "source_text": fact.text,
                     "palette_path": palette["path"], "palette_sha256": _sha256(palette["path"]),
                     "palette_colors": palette["colors"],
                     "palette_source_url": palette["source_url"],
                     "palette_source_file": palette["source_file"],
                     "palette_source_sha256": palette["source_sha256"],
                     "palette_approval": palette_approval,
                     "voice_path": str(voice_path), "voice_sha256": _sha256(voice_path),
                     "headline": headline, "brief": brief})
    return {"operation": "preview_held_igfill_astra", "state": "preview_only",
            "today": today, "original_hold_receipt": str(Path(hold_receipt_path).resolve()),
            "original_target_digest": source_receipt["target_digest"],
            "photo_preflight_digest": photo["target_digest"],
            "held_row_count": len(held), "preview_count": len(rows), "previews": rows,
            "blocked": photo["blocked"], "approval_provenance": "unverified",
            "calendar_mutations": 0, "provider_calls": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold-receipt", required=True)
    parser.add_argument("--palette-manifest", required=True,
                        help="private JSON with gym colors and hash-bound source evidence")
    parser.add_argument("--palette-approval", required=True,
                        help="separate private human approval receipt bound to manifest")
    parser.add_argument("--output", required=True, help="new private JSON preview path")
    parser.add_argument("--today", help="UTC date YYYY-MM-DD")
    args = parser.parse_args(argv)
    if Path(args.output).resolve() == Path(args.hold_receipt).resolve():
        parser.error("preview output must differ from hold receipt")
    result = build_previews(args.hold_receipt, palette_manifest_path=args.palette_manifest,
                            palette_approval_path=args.palette_approval,
                            today=args.today)
    _receipt(args.output, result, create=True)
    print(json.dumps({k: v for k, v in result.items() if k != "previews"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
