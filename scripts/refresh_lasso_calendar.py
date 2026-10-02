#!/usr/bin/env python3
"""Plan, but never silently change, LASSO's daily Summit calendar refresh.

The default path is snapshot-only and emits a review manifest.  Local rendering
uses only exact recorded copy.  A later explicit ``--apply`` hosts a separately
reviewed file, persists its artifact evidence, then changes each row through an
atomic PostgREST compare-and-swap; it never publishes or changes copy/date/status.
"""

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse


TARGET_GYM = "lasso"
TARGET_ACCOUNT = "instagram"
TARGET_FORMAT = "feed"
TARGET_COUNT = 3
MACHINE_PENDING = {"pending", "draft", "queued"}
PROTECTED_STATUSES = {"approved", "denied", "killed", "published", "publishing", "failed"}
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".m4v")
APPROVED_HEADLINE_CORRECTIONS = {
    "You did not open a gym to run ads at 11pm.": "You did not open a gym to run your own ads.",
}


def _active(row):
    return str(row.get("variant_status") or "active").lower() == "active"


def _text(row):
    return " ".join(str(row.get(k) or "") for k in
                    ("caption", "pillar", "category", "draft_type", "image_url")).lower()


def _summit_filenames(root):
    """Known Summit originals are evidence, not a filename guess."""
    try:
        from agent.summit_queue import SUMMIT_POSTS
        return {str(item["filename"]).lower() for item in SUMMIT_POSTS}
    except Exception:
        manifest = root / "summit_manifest.json"
        try:
            return {str(k).lower() for k in json.loads(manifest.read_text())}
        except (OSError, ValueError):
            return set()


def is_summit(row, summit_files=()):
    name = Path(urlparse(str(row.get("image_url") or "")).path).name.lower()
    if name in set(summit_files):
        return True
    text = _text(row)
    return any(token in text for token in (
        "growth summit", "virgin hotel nashville", "nashville", "november 7", "november 8"))


def _is_video(row):
    url = str(row.get("image_url") or row.get("source_media_url") or "").lower()
    return bool(row.get("thumbnail_url")) or url.endswith(VIDEO_EXTENSIONS)


def canonical_approved_copy(copy):
    """Match creative_studio's display normalization before hashing or review compare."""
    from agent.creative_studio import _scrub_dashes
    from agent.infographic_copy_style import display_text
    headline = APPROVED_HEADLINE_CORRECTIONS.get(str(copy.get("headline") or ""),
                                                  str(copy.get("headline") or ""))
    return {"headline": _scrub_dashes(display_text(headline)),
            "facts": [_scrub_dashes(display_text(item)) for item in copy.get("facts", [])],
            "cta": _scrub_dashes(display_text(copy.get("cta", ""))),
            "footer": display_text(copy.get("footer", ""))}


def _campaign_book_urls(root):
    """Return only user-supplied book originals, which must never be regenerated."""
    urls = set()
    for rel in ("book_manifest.json", "brand_voice/lasso_calendar_campaign.json"):
        try:
            payload = json.loads((root / rel).read_text())
        except (OSError, ValueError):
            continue
        values = payload.get("assets", []) if isinstance(payload, dict) else []
        if isinstance(payload, dict) and rel == "book_manifest.json":
            values = [{"feed_url": value} for value in payload.values() if isinstance(value, str)]
        for item in values:
            if str(item.get("category") or "").lower() == "book" or rel == "book_manifest.json":
                for key in ("feed_url", "story_url"):
                    if item.get(key):
                        urls.add(str(item[key]))
    return urls


def _sidecar_evidence(row, root, copy_evidence=None):
    """Use a matching local sidecar's recorded brief when it is present.

    The calendar snapshot alone cannot prove a remote object has a sidecar, so an
    absent local match is reported as ``row_caption`` rather than invented via a
    generic copy selector.
    """
    filename = Path(urlparse(str(row.get("image_url") or "")).path).name
    if not filename:
        return {"kind": "row_caption", "caption": str(row.get("caption") or "")}
    exact = (copy_evidence or {}).get(filename)
    if isinstance(exact, dict) and isinstance(exact.get("infographic_copy"), dict):
        # This was recorded with the existing pixels. Keep it verbatim for a
        # later renderer; do not run the row through select_copy again.
        return {"kind": "library_copy", "image_sha256": exact.get("image_sha256", ""),
                "grade_status": exact.get("grade_status", ""),
                "infographic_copy": exact["infographic_copy"]}
    for path in (root / "content_library").rglob(Path(filename).stem + ".json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if str(data.get("public_url") or "") in ("", str(row.get("image_url") or "")):
            return {"kind": "sidecar", "path": str(path.relative_to(root)),
                    "note": data.get("note", ""), "cite": data.get("cite", []),
                    "concept": data.get("concept", "")}
    return {"kind": "row_caption", "caption": str(row.get("caption") or "")}


def load_snapshot(path):
    payload = json.loads(Path(path).read_text())
    rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("snapshot must be a list or an object with a rows list")
    return rows


def load_copy_evidence(path):
    if not path:
        return {}
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise ValueError("copy evidence must map image basenames to metadata")
    return payload


def plan(rows, *, start, end, root, copy_evidence=None):
    """Build a deterministic review manifest for the requested date range."""
    start_day = date.fromisoformat(str(start))
    end_day = date.fromisoformat(str(end))
    if end_day < start_day:
        raise ValueError("end precedes start")
    summit_files = _summit_filenames(root)
    book_urls = _campaign_book_urls(root)
    by_day = defaultdict(list)
    # All active LASSO feed rows in scope are retained for media grouping. IG rows
    # define the requested daily shape; their pending FB mirrors are regenerated as
    # one logical creative only when they carry the exact same current URL.
    feed_rows = []
    protected = []
    for row in rows:
        if (str(row.get("gym_id")) != TARGET_GYM or not _active(row)
                or str(row.get("format") or "feed").lower() != TARGET_FORMAT):
            continue
        day = str(row.get("post_date") or "")[:10]
        if not day or not (start_day <= date.fromisoformat(day) <= end_day):
            continue
        feed_rows.append(row)
        if str(row.get("account") or "").lower() == TARGET_ACCOUNT:
            status = str(row.get("status") or "pending").lower()
            if status in PROTECTED_STATUSES:
                protected.append({"id": row.get("id"), "date": day, "status": status})
            by_day[day].append(row)

    days, regenerate = [], {}
    current = start_day
    while current <= end_day:
        key = current.isoformat()
        active = by_day.get(key, [])
        # A denied or killed historical row remains immutable evidence but is not
        # an upcoming post. Counting it would suppress the replacement Summit
        # promotion and effectively resurrect the rejected creative as coverage.
        scheduled = [r for r in active if str(r.get("status") or "pending").lower()
                     not in {"denied", "killed", "deleted", "failed"}]
        pending = [r for r in active if str(r.get("status") or "pending").lower() in MACHINE_PENDING]
        summit = [r for r in scheduled if is_summit(r, summit_files)]
        pending_summit = [r for r in pending if is_summit(r, summit_files)]
        regular = [r for r in scheduled if not is_summit(r, summit_files)]
        desired_gap = max(0, TARGET_COUNT - len(scheduled))
        days.append({
            "date": key, "active_feed_count": len(scheduled), "pending_feed_count": len(pending),
            "summit_active_count": len(summit), "regular_active_count": len(regular),
            "desired": {"regular": 2, "summit": 1, "total": TARGET_COUNT},
            "summit_action": "keep" if summit else "needs_new_summit_pending_post",
            "calendar_action": ("review_over_capacity" if len(scheduled) > TARGET_COUNT else
                                "needs_pending_posts" if desired_gap else "shape_complete"),
            "pending_rows_eligible_for_reconciliation": [r.get("id") for r in pending],
            "protected_row_ids": [r.get("id") for r in active if r not in pending],
        })
        for row in pending:
            url = str(row.get("image_url") or "")
            # Lead-owned correction: never regenerate any iteration of the old
            # "run ads at 11pm" card in this batch. Its corrected feed is handled
            # separately and the exclusion applies even if another date reuses it.
            if _is_video(row) or url in book_urls or "11pm" in url.lower():
                continue
            group = regenerate.setdefault(url, {"id": hashlib.sha256(url.encode()).hexdigest()[:16],
                                                "image_url": url, "anchor_row_ids": [],
                                                "anchors": [], "slots": [], "evidence": []})
            # Include the IG anchor and only its exact-url pending FB sibling(s).
            # Candidates are already absent from feed_rows, so they can never be
            # mistaken for a second post or a mutation target.
            related = [r for r in feed_rows if str(r.get("image_url") or "") == url
                       and str(r.get("status") or "pending").lower() in MACHINE_PENDING
                       and str(r.get("account") or "").lower() in ("instagram", "facebook")]
            for sibling in related:
                if sibling.get("id") in group["anchor_row_ids"]:
                    continue
                group["anchor_row_ids"].append(sibling.get("id"))
                group["anchors"].append({"id": sibling.get("id"), "gym_id": TARGET_GYM,
                    "status": str(sibling.get("status") or "pending").lower(),
                    "variant_status": sibling.get("variant_status") or "active",
                    "post_date": str(sibling.get("post_date") or "")[:10],
                    "image_url": str(sibling.get("image_url") or ""),
                    "caption": str(sibling.get("caption") or ""),
                    "late_post_id": sibling.get("late_post_id"),
                    "published_at": sibling.get("published_at")})
                group["slots"].append({"date": str(sibling.get("post_date") or "")[:10],
                    "account": sibling.get("account"), "format": sibling.get("format"),
                    "status": sibling.get("status"),
                    "variant_status": sibling.get("variant_status") or "active"})
                group["evidence"].append(_sidecar_evidence(sibling, root, copy_evidence))
        current += timedelta(days=1)
    for group in regenerate.values():
        approved = next((item for item in group["evidence"]
                         if item.get("kind") == "library_copy"), None)
        if approved:
            source_id = "content_calendar:" + str(group["anchor_row_ids"][0])
            approved_copy = canonical_approved_copy(approved["infographic_copy"])
            canonical = json.dumps(approved_copy, sort_keys=True,
                                   separators=(",", ":"))
            group["generation"] = {"eligible": True, "source_id": source_id,
                "source_hash": hashlib.sha256((source_id + "|" + canonical).encode()).hexdigest(),
                "approved_copy": approved_copy,
                "prior_image_sha256": approved.get("image_sha256", ""),
                "prior_grade_status": approved.get("grade_status", "")}
        else:
            group["generation"] = {"eligible": False,
                "reason": "exact_approved_copy_unavailable"}
    return {"version": 2, "mode": "review_only", "approved_headline_corrections": APPROVED_HEADLINE_CORRECTIONS,
            "scope": {"gym_id": TARGET_GYM,
            "account": TARGET_ACCOUNT, "format": TARGET_FORMAT, "start": str(start), "end": str(end)},
            "guards": {"active_variant_only": True, "protected_statuses": sorted(PROTECTED_STATUSES),
                       "candidate_rows_counted": False, "no_delete_reinsert": True,
                       "apply_requires_atomic_current_row_guard": True},
            "days": days, "protected_rows": protected,
            "regeneration_groups": list(regenerate.values())}


def generate_review_assets(manifest, output_dir, *, generate_fn=None, workers=3):
    """Render eligible groups locally, resumably, with no hosting or calendar write."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    def one(group):
        generation = group.get("generation") or {}
        if not generation.get("eligible"):
            return {"id": group.get("id"), "status": "skipped", "reason": generation.get("reason")}
        target = output / (str(group["id"]) + ".png")
        receipt = output / (str(group["id"]) + ".json")
        if target.exists() and receipt.exists():
            saved = json.loads(receipt.read_text())
            if saved.get("image_sha256") == hashlib.sha256(target.read_bytes()).hexdigest():
                return saved
        copy = generation["approved_copy"]
        renderer = generate_fn
        if renderer is None:
            from agent import creative_studio
            renderer = creative_studio.generate
        try:
            result = renderer(copy["headline"], copy["facts"], cta=copy.get("cta", ""),
                              footer=copy.get("footer"), account_key=TARGET_GYM,
                              surface="feed post", out_path=str(target))
            path_value = (result or {}).get("path")
            path = Path(path_value) if path_value else None
            if path is None or not path.is_file():
                return {"id": group.get("id"), "status": "failed", "reason": "render_failed"}
            if path != target:
                target.write_bytes(path.read_bytes())
            out = {"id": group["id"], "status": "generated_local_review_required",
                   "path": str(target), "image_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                   "source_id": generation["source_id"], "source_hash": generation["source_hash"]}
            receipt.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
            return out
        except Exception as exc:
            return {"id": group.get("id"), "status": "failed", "reason": type(exc).__name__}

    groups = list(manifest.get("regeneration_groups") or [])
    results = []
    with ThreadPoolExecutor(max_workers=min(3, max(1, int(workers)))) as pool:
        futures = [pool.submit(one, group) for group in groups]
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: str(item.get("id") or ""))


def _row_matches(row, expected):
    return bool(row) and all(str(row.get(key) or "") == str(expected.get(key) or "")
                             for key in ("id", "gym_id", "status", "variant_status", "post_date", "image_url", "caption")) \
        and row.get("late_post_id") is None and row.get("published_at") is None


def _readback_matches(row, expected, new_url):
    expected_after = dict(expected, image_url=new_url)
    return _row_matches(row, expected_after)


def _conditional_swap(store, expected, new_url):
    """One PostgREST compare-and-swap; a zero-row result is a conflict, never retried."""
    r = store._client().patch(store._rest("content_calendar"), params={
        "id": "eq." + str(expected["id"]), "gym_id": "eq.lasso",
        "status": "eq." + str(expected["status"]), "variant_status": "eq.active",
        "post_date": "eq." + str(expected["post_date"]), "image_url": "eq." + str(expected["image_url"]),
        "caption": "eq." + str(expected["caption"]), "late_post_id": "is.null", "published_at": "is.null"},
        headers=store._headers({"Content-Type": "application/json", "Prefer": "return=representation"}),
        json={"image_url": new_url}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError("conditional calendar swap failed")
    rows = r.json() or []
    return rows[0] if len(rows) == 1 else None


def apply_reviewed_swaps(manifest, review_manifest, store, receipt_dir, *, swap_fn=None,
                         host_fn=None, artifact_store=None, review_fn=None):
    """Apply only externally reviewed URLs using row-level compare-and-swap guards."""
    reviewed = {item.get("id"): item for item in review_manifest.get("reviewed_groups", [])
                if item.get("review_status") == "PASS" and item.get("image_sha256") and item.get("path")}
    if not reviewed:
        raise ValueError("review manifest has no explicitly passing hosted image groups")
    folder = Path(receipt_dir)
    folder.mkdir(parents=True, exist_ok=True)
    receipts, before = [], []
    before_path, receipts_path = folder / "before-apply.json", folder / "apply-receipts.json"

    def persist():
        # Persist before any first PATCH, then after every row outcome. A crash
        # therefore leaves both the immutable before state and partial receipts.
        before_path.write_text(json.dumps(before, indent=2, default=str) + "\n")
        receipts_path.write_text(json.dumps(receipts, indent=2, sort_keys=True) + "\n")

    for group in manifest.get("regeneration_groups", []):
        review = reviewed.get(group.get("id"))
        if not review:
            continue
        path = Path(review["path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != review["image_sha256"]:
            receipts.append({"group_id": group["id"], "status": "review_file_conflict"})
            persist()
            continue
        generation = group.get("generation") or {}
        if not generation.get("eligible"):
            receipts.append({"group_id": group["id"], "status": "missing_exact_copy"})
            persist()
            continue
        try:
            if review_fn is None:
                from agent.infographic_evidence import reviewed_asset
                review_fn = reviewed_asset
            evidence = review_fn(str(path))
            if (not evidence or evidence.get("grade_status") != "PASS"
                    or evidence.get("image_sha256") != review["image_sha256"]
                    or evidence.get("infographic_copy") != generation["approved_copy"]):
                raise ValueError("review_evidence_mismatch")
        except Exception as exc:
            receipts.append({"group_id": group["id"], "status": "review_evidence_failed",
                             "error": type(exc).__name__})
            persist()
            continue
        ready = []
        for expected in group.get("anchors", []):
            current = store.get_row(TARGET_GYM, expected["id"])
            before.append(current)
            persist()  # durable snapshot before this row can ever reach PATCH
            if expected.get("status") != "pending" or not _row_matches(current, expected):
                receipts.append({"id": expected["id"], "group_id": group["id"], "status": "conflict"})
                persist()
                continue
            ready.append(expected)
        if not ready:
            continue
        try:
            if host_fn is None:
                from agent import media_host
                host_fn = media_host.host_media
            if artifact_store is None:
                from agent.infographic_artifacts import ArtifactStore
                artifact_store = ArtifactStore()
            new_url = host_fn(str(path), TARGET_GYM)
            if not new_url:
                raise RuntimeError("hosting_failed")
            artifact_store.save(TARGET_GYM, new_url, str(path), {
                "source_id": generation["source_id"], "source_hash": generation["source_hash"]})
        except Exception as exc:
            receipts.append({"group_id": group["id"], "status": "artifact_or_host_failed",
                             "error": type(exc).__name__})
            persist()
            continue
        for expected in ready:
            try:
                changed = (swap_fn or _conditional_swap)(store, expected, new_url)
            except Exception as exc:
                receipts.append({"id": expected["id"], "group_id": group["id"],
                                 "status": "swap_failed", "error": type(exc).__name__})
                persist()
                continue
            if not _readback_matches(changed, expected, new_url):
                receipts.append({"id": expected["id"], "group_id": group["id"], "status": "conflict"})
                persist()
                continue
            try:
                readback = store.get_row(TARGET_GYM, expected["id"])
            except Exception as exc:
                receipts.append({"id": expected["id"], "group_id": group["id"],
                                 "status": "readback_failed", "error": type(exc).__name__})
                persist()
                continue
            receipts.append({"id": expected["id"], "group_id": group["id"],
                             "status": "applied" if _readback_matches(readback, expected, new_url)
                             else "readback_failed"})
            persist()
    return receipts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="read-only calendar snapshot JSON")
    parser.add_argument("--start", default=date.today().isoformat())
    parser.add_argument("--end", default="2026-11-08")
    parser.add_argument("--copy-evidence", help="read-only library-copy.json exact-copy mapping")
    parser.add_argument("--output", required=True)
    parser.add_argument("--generate-local", help="explicit local review output directory; never hosts or writes calendar")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--review-manifest", help="explicit PASS review JSON with group id, path, image_sha256")
    parser.add_argument("--receipt-dir")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    manifest = plan(load_snapshot(args.snapshot), start=args.start, end=args.end, root=root,
                    copy_evidence=load_copy_evidence(args.copy_evidence))
    Path(args.output).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if args.generate_local:
        manifest["local_generation"] = generate_review_assets(manifest, args.generate_local, workers=args.workers)
        Path(args.output).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if args.apply:
        if not args.review_manifest or not args.receipt_dir:
            raise SystemExit("--apply requires --review-manifest and --receipt-dir")
        from agent.portal_calendar_store import SupabaseCalendarStore
        receipts = apply_reviewed_swaps(manifest, json.loads(Path(args.review_manifest).read_text()),
                                        SupabaseCalendarStore(), args.receipt_dir)
        print(json.dumps({"applied": sum(r["status"] == "applied" for r in receipts),
                          "conflicts": sum(r["status"] == "conflict" for r in receipts)}))
    print(json.dumps({"mode": manifest["mode"], "days": len(manifest["days"]),
                      "regeneration_groups": len(manifest["regeneration_groups"])}))


if __name__ == "__main__":
    main()
