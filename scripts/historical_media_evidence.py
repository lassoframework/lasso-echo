"""Read-only historical-media evidence helper.

Joins locally saved published R2 bytes (keyed by published row ID) to
media_asset.content_hash via an MD5 of the published bytes, within one gym.
Emits deterministic JSON: known_used receipts for exactly-one-match rows,
held rows with reasons for everything else. These receipts are CANDIDATE
known-used byte matches for operator review only; this tool never touches
the network, credentials, databases, or production state, and never
automatically clears, marks, or mutates historical assets.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

MAX_BYTES = 512 * 1024 * 1024  # hard cap per-row byte file, enforced while reading
_CHUNK = 1024 * 1024
_MD5_HEX = re.compile(r"^[0-9a-f]{32}$")


class _OversizedFile(Exception):
    pass


def _md5_file(path: Path) -> str:
    """Stream MD5 of path, enforcing a hard MAX_BYTES cap (covers stat/read races)."""
    h = hashlib.md5()
    total = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(min(_CHUNK, MAX_BYTES - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise _OversizedFile
            h.update(chunk)
    return h.hexdigest()


def _safe_bytes_path(media_dir: Path, row_id: str) -> Path | None:
    """Resolve <media_dir>/<row_id>, rejecting traversal; None if unsafe/missing."""
    if not row_id or "\x00" in row_id or "/" in row_id or "\\" in row_id:
        return None
    base = media_dir.resolve()
    p = (base / row_id).resolve()
    if p.parent != base or not p.is_file():
        return None
    return p


def _is_valid_asset(a) -> bool:
    """Fail closed: only dict assets with a nonempty asset ID and a valid
    32-hex lowercase/uppercase MD5 content_hash participate in matching."""
    if not isinstance(a, dict):
        return False
    asset_id = str(a.get("asset_id") or a.get("media_asset_id") or "")
    if not asset_id:
        return False
    h = str(a.get("content_hash") or "").lower()
    return bool(_MD5_HEX.match(h))


def build_evidence(evidence_input: dict, media_dir: Path) -> dict:
    gym_id = str(evidence_input.get("gym_id") or evidence_input.get("tenant_id") or "")
    raw_assets = evidence_input.get("media_assets", [])
    raw_rows = evidence_input.get("published_rows", [])
    if not isinstance(raw_assets, list):
        raw_assets = []
    if not isinstance(raw_rows, list):
        raw_rows = []

    by_hash: dict[str, list[str]] = {}
    if gym_id:
        for a in raw_assets:
            if not _is_valid_asset(a):
                continue
            if str(a.get("gym_id")) != gym_id:
                continue
            h = str(a.get("content_hash") or "").lower()
            by_hash.setdefault(h, []).append(str(a.get("asset_id") or a.get("media_asset_id")))

    known_used, held = [], []
    for row in raw_rows:
        if not isinstance(row, dict):
            held.append({"row_id": "", "late_post_id": "", "reason": "malformed_row"})
            continue
        row_id = str(row.get("row_id") or row.get("id") or "")
        post_id = str(row.get("late_post_id") or "")
        entry = {"row_id": row_id, "late_post_id": post_id}
        if not gym_id:
            held.append({**entry, "reason": "missing_gym_id"})
            continue
        if str(row.get("gym_id") or "") != gym_id:
            held.append({**entry, "reason": "gym_mismatch"})
            continue
        if not post_id:
            held.append({**entry, "reason": "missing_post_id"})
            continue
        p = _safe_bytes_path(media_dir, row_id)
        if p is None:
            held.append({**entry, "reason": "missing_bytes"})
            continue
        try:
            digest = _md5_file(p)
        except _OversizedFile:
            held.append({**entry, "reason": "oversized_file"})
            continue
        except OSError:
            held.append({**entry, "reason": "missing_bytes"})
            continue
        matches = sorted(set(by_hash.get(digest, [])))
        if len(matches) == 1:
            known_used.append({
                "row_id": row_id,
                "late_post_id": post_id,
                "media_asset_id": matches[0],
                "content_hash": digest,
                "published_md5": digest,
            })
        elif not matches:
            held.append({**entry, "published_md5": digest, "reason": "no_hash_match"})
        else:
            held.append({**entry, "published_md5": digest, "reason": "ambiguous_hash_match",
                         "candidate_asset_ids": matches})

    # Stable, input-order-independent output: reordering rows/assets yields
    # identical JSON.
    known_used.sort(key=lambda r: (r["row_id"], r["late_post_id"], r["media_asset_id"]))
    held.sort(key=lambda r: (r["row_id"], r["late_post_id"], r["reason"]))
    return {"gym_id": gym_id, "known_used": known_used, "held": held}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only historical-media evidence: MD5 published bytes and "
                    "join to media_asset.content_hash within one gym. Emits "
                    "CANDIDATE known_used receipts only for exact, unique, "
                    "same-gym matches. Never clears or mutates historical "
                    "assets; operator review required.")
    ap.add_argument("--input", help="Operator-supplied JSON path (default: stdin).")
    ap.add_argument("--media-dir", required=True,
                    help="Directory of locally saved published R2 bytes keyed by row ID.")
    ap.add_argument("--output", help="Output JSON path (default: stdout).")
    args = ap.parse_args(argv)

    try:
        raw = Path(args.input).read_text() if args.input else sys.stdin.read()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: malformed input JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict) or not isinstance(payload.get("published_rows", []), list):
        print("error: input must be a JSON object with a published_rows list", file=sys.stderr)
        return 2
    if not str(payload.get("gym_id") or payload.get("tenant_id") or ""):
        print("error: input must supply a nonempty gym_id (or tenant_id)", file=sys.stderr)
        return 2
    media_dir = Path(args.media_dir)
    if not media_dir.is_dir():
        print(f"error: media dir not found: {media_dir}", file=sys.stderr)
        return 2

    out = json.dumps(build_evidence(payload, media_dir), indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(out)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
