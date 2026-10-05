"""Review and atomically stage missing LASSO Instagram third-slot Stories.

The input is a frozen manifest of already rendered and reviewed 9:16 objects.
This job never crops a feed card, renders unreviewed media, or uses the monthly
delete/reinsert mirror. Dry-run is the default. Apply uses one guarded SQL RPC
per date and records each receipt before moving to the next date.

Manifest: {"stories": [{"date": "YYYY-MM-DD", "feed_id": "uuid",
  "story_image_url": "https://...", "story_sha256": "64 lowercase hex",
  "policy_version": "...", "scheduled_at": "ISO timestamp"}]}
Two supported source paths, both fail-closed:

1. Catalog parity: the live feed row matches the dated, approved Summit
   catalog entry, and provenance uses the catalog source hash.
2. Live feed: a reviewed LIVE Summit feed artifact whose source_identity is
   the exact feed caption hash (not the catalog hash). This is a distinct
   supported path, NOT catalog parity: the feed caption names the official
   event and URL, sits in the Summit window, and binds all three
   of feed ID, feed image URL, and feed caption hash.

The Story artifact must record aspect=9:16 and source_feed_id,
source_feed_image_url, and source_hash as provenance.
"""

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.lasso_daily_summit import _catalog_entry, _source_identity
from agent.portal_calendar_store import SupabaseCalendarStore
from agent.summit_queue import SUMMIT_URL

START = date(2026, 9, 23)
END = date(2026, 11, 8)
LIVE_START = date(2026, 9, 23)
LIVE_END = date(2026, 11, 8)
ARTIFACT_TABLE = "echo_infographic_artifacts"
RPC = "rpc/stage_lasso_third_story"
UUID_NS = uuid.NAMESPACE_URL


def story_id(day):
    return str(uuid.uuid5(UUID_NS, f"lasso|instagram|{day}|summit|story|slot2"))


def _caption_hash(caption):
    """Exact provenance key for a LIVE feed caption: raw sha256 of the text."""
    return hashlib.sha256(str(caption).encode("utf-8")).hexdigest()


def _approved_summit_facts(caption):
    """Require the official event identity and destination in live copy.

    The feed artifact review covers any additional visual claims. A live feed
    can mention the Summit without repeating its date or venue in the caption.
    """
    text = str(caption or "")
    return "LASSO Growth Summit" in text and SUMMIT_URL in text


def _read(store, table, params):
    response = store._client().get(store._rest(table), params=params,
                                   headers=store._headers(), timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"{table} read failed ({response.status_code})")
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"{table} read returned malformed JSON")
    return rows


def _one(store, table, params):
    rows = _read(store, table, dict(params, limit="2"))
    if len(rows) != 1:
        raise ValueError(f"expected one {table} row, found {len(rows)}")
    return rows[0]


def _feed(store, feed_id):
    return _one(store, "content_calendar", {"id": "eq." + feed_id,
        "gym_id": "eq.lasso", "select": "*"})


def _artifact(store, image_url, tenants=("lasso",)):
    rows = []
    for tenant in tenants:
        rows.extend(_read(store, ARTIFACT_TABLE, {"tenant": "eq." + tenant,
            "image_url": "eq." + image_url, "select": "*", "limit": "2"}))
    if len(rows) != 1:
        raise ValueError(f"expected one reviewed artifact, found {len(rows)}")
    return rows[0]


def _story_rows(store, day):
    # Two rows are enough to detect ambiguity. The SQL transaction repeats
    # the ownership check under a table lock, including concurrent inserts.
    return _read(store, "content_calendar", {"gym_id": "eq.lasso",
        "post_date": "eq." + day, "account": "eq.instagram",
        "format": "eq.story", "variant_status": "eq.active",
        "select": "id,status,slot_index,image_url,logical_post_id",
        "limit": "100"})


def plan_one(store, item, catalog_path):
    day = str(item.get("date") or "")
    parsed = date.fromisoformat(day)
    if not START <= parsed <= END:
        raise ValueError("date outside bounded Summit backfill")
    feed_id = str(uuid.UUID(str(item["feed_id"])))
    image_url = str(item.get("story_image_url") or "").strip()
    digest = str(item.get("story_sha256") or "")
    policy = str(item.get("policy_version") or "").strip()
    scheduled = str(item.get("scheduled_at") or "").strip()
    if (not image_url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not policy or not scheduled):
        raise ValueError("Story needs hosted 9:16 review receipt and schedule")
    from datetime import datetime
    instant = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
    if (instant.tzinfo is None or
            instant.astimezone(ZoneInfo("America/New_York")).date().isoformat() != day):
        raise ValueError("scheduled_at must fall on the LASSO local feed day")

    entry = _catalog_entry(day, catalog_path=catalog_path)
    source = None
    if entry is not None:
        source, _ = _source_identity(entry)
    feed = _feed(store, feed_id)
    caption = str(feed.get("caption") or "")
    if (feed.get("gym_id") != "lasso" or str(feed.get("account") or "").lower() != "instagram"
            or str(feed.get("format") or "").lower() != "feed"
            or feed.get("slot_index") != 2
            or str(feed.get("pillar") or "").lower() != "summit"
            or feed.get("variant_status") != "active"
            or feed.get("status") not in {"pending", "approved", "publishing", "published"}
            or not caption
            or not str(feed.get("image_url") or "").startswith("https://")):
        raise ValueError("live source feed is not an active Summit ordinal-2 feed")
    catalog_match = (source is not None and feed.get("post_date") == day
                     and caption == entry.get("caption"))
    caption_hash = None
    if catalog_match:
        expected_hash = source["source_hash"]
        catalog_url = str(entry.get("hosted_image_url") or "").strip()
        if catalog_url and feed["image_url"] != catalog_url:
            raise ValueError("source feed URL differs from dated catalog")
    else:
        # LIVE-feed path: distinct from catalog parity. The feed caption stays
        # live copy, so provenance binds the exact caption hash instead of the
        # catalog hash, and the caption must name the official event and URL.
        parsed_feed_day = date.fromisoformat(str(feed.get("post_date") or ""))
        if feed.get("post_date") != day:
            raise ValueError("live source feed date differs from Story manifest date")
        if not LIVE_START <= parsed_feed_day <= LIVE_END:
            raise ValueError("live source feed outside Summit window")
        if not _approved_summit_facts(caption):
            raise ValueError("live feed caption lacks approved Summit facts")
        caption_hash = _caption_hash(caption)
        expected_hash = caption_hash
    feed_artifact = _artifact(store, feed["image_url"], ("lasso", "lasso_ig"))
    feed_evidence = feed_artifact.get("evidence") or {}
    if (feed_artifact.get("source_identity", {}).get("source_hash") != expected_hash
            or feed_evidence.get("grade_status") != "PASS"
            or feed_evidence.get("image_sha256") != feed_artifact.get("image_sha256")):
        raise ValueError("source feed is not the exact reviewed dated artifact")
    if image_url == feed["image_url"]:
        raise ValueError("Story must have independent 9:16 media")
    artifact = _artifact(store, image_url)
    evidence = artifact.get("evidence") or {}
    identity = artifact.get("source_identity") or {}
    if (artifact.get("image_sha256") != digest or evidence.get("grade_status") != "PASS"
            or evidence.get("image_sha256") != digest
            or evidence.get("policy_version") != policy
            or evidence.get("aspect") != "9:16"
            or identity.get("source_feed_id") != feed_id
            or identity.get("source_feed_image_url") != feed["image_url"]
            or identity.get("source_hash") != expected_hash):
        raise ValueError("reviewed 9:16 artifact does not bind to exact source feed")
    occupied = [row for row in _story_rows(store, day)
                if row.get("status") not in {"denied", "killed", "failed"}
                and row.get("slot_index") in (None, 2)]
    rid = story_id(day)
    if occupied and not (len(occupied) == 1 and occupied[0].get("id") == rid
                     and occupied[0].get("image_url") == image_url
                     and occupied[0].get("logical_post_id") == feed.get("logical_post_id")):
        raise ValueError("active third Story slot already occupied")
    return {"date": day, "feed_id": feed_id, "story_id": rid,
            "feed_caption": feed["caption"], "feed_image_url": feed["image_url"],
            "story_image_url": image_url, "story_source_url": image_url,
            "story_sha256": digest, "source_hash": expected_hash,
            "caption_hash": caption_hash,
            "policy_version": policy, "scheduled_at": scheduled,
            "logical_post_id": feed.get("logical_post_id"),
            "status": "already_present" if occupied else "ready"}


def apply_one(store, action):
    payload = {"p_feed_id": action["feed_id"],
               "p_feed_caption": action["feed_caption"],
               "p_feed_image_url": action["feed_image_url"],
               "p_story_id": action["story_id"],
               "p_story_image_url": action["story_image_url"],
               "p_story_source_url": action["story_source_url"],
               "p_story_sha256": action["story_sha256"],
               "p_source_hash": action["source_hash"],
               "p_caption_hash": action.get("caption_hash"),
               "p_policy_version": action["policy_version"],
               "p_scheduled_at": action["scheduled_at"]}
    response = store._client().post(store._rest(RPC),
        headers=store._headers({"Content-Type": "application/json"}),
        json=payload, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"guarded Story RPC failed ({response.status_code})")
    receipt = response.json()
    if not isinstance(receipt, dict) or receipt.get("result") not in {"inserted", "idempotent", "conflict"}:
        raise RuntimeError("guarded Story RPC returned malformed receipt")
    return receipt


def run(manifest, store, catalog_path, *, apply=False, on_result=None):
    items = manifest.get("stories") if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not items or len(items) > 47:
        raise ValueError("manifest needs 1-47 dated Story items")
    dates = [str(item.get("date") or "") if isinstance(item, dict) else ""
             for item in items]
    if len(set(dates)) != len(dates):
        raise ValueError("duplicate dates in manifest")
    results = []
    for item in sorted(items, key=lambda x: str(x.get("date") or "")
                       if isinstance(x, dict) else ""):
        try:
            action = plan_one(store, item, catalog_path)
            receipt = apply_one(store, action) if apply else {"result": "dry_run"}
            if apply and receipt.get("result") in {"inserted", "idempotent"}:
                found = _story_rows(store, action["date"])
                matches = [row for row in found if row.get("id") == action["story_id"]
                           and row.get("image_url") == action["story_image_url"]
                           and row.get("logical_post_id") == action["logical_post_id"]]
                if len(matches) != 1:
                    raise RuntimeError("guarded Story readback mismatch")
            results.append({"date": action["date"], "story_id": action["story_id"],
                            "feed_id": action["feed_id"], "receipt": receipt})
        except Exception as exc:
            results.append({"date": item.get("date") if isinstance(item, dict) else None, "receipt": {
                "result": "blocked", "reason": type(exc).__name__, "detail": str(exc)[:220]}})
        if on_result is not None:
            on_result(results)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--catalog", type=Path,
                        default=ROOT / "brand_voice" / "lasso_summit_daily.json")
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.receipt.exists():
        raise SystemExit("refusing to overwrite an apply receipt")
    if args.apply:
        from agent import visual_writer_prepare
        if visual_writer_prepare.enabled():
            raise SystemExit("visual writer is armed; this RPC needs reviewed scene preparation before apply")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    def persist(results):
        args.receipt.write_text(json.dumps({"mode": "apply" if args.apply else "dry_run",
            "results": results}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    results = run(manifest, SupabaseCalendarStore(), args.catalog,
                  apply=args.apply, on_result=persist)
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "total": len(results),
                      "blocked": sum(r["receipt"]["result"] in {"blocked", "conflict"} for r in results)}))
    return int(any(r["receipt"]["result"] in {"blocked", "conflict"} for r in results))


if __name__ == "__main__":
    sys.exit(main())
