"""Repair an existing held LASSO Story against its exact reviewed feed source.

Manifest: {"repairs":[{"story_id":"uuid","feed_id":"uuid",
"story_image_url":"https://...","story_sha256":"64 lowercase hex",
"artifact_tenant":"lasso_ig|lasso_fb|lasso","policy_version":"..."}]}
Dry-run is the default. --apply invokes the exact-CAS RPC and reads back.
"""

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.jobs import lasso_paired_story_backfill as base
from agent.portal_calendar_store import SupabaseCalendarStore

RPC = "rpc/repair_lasso_paired_story"
STORY_SNAPSHOT = ("account", "post_date", "slot_index", "status",
                  "variant_status", "pillar", "caption", "image_url",
                  "source_media_url", "media_not_ready_reason", "scheduled_at",
                  "logical_post_id")
FEED_SNAPSHOT = ("caption", "image_url", "pillar", "status",
                 "scheduled_at", "logical_post_id")


def plan_one(store, item):
    if not isinstance(item, dict):
        raise ValueError("repair entry must be an object")
    story_id = str(uuid.UUID(str(item["story_id"])))
    feed_id = str(uuid.UUID(str(item["feed_id"])))
    story = base._one(store, "content_calendar", {
        "gym_id": "eq.lasso", "id": "eq." + story_id, "select": "*"})
    feed = base._feed(store, feed_id)
    account = str(story.get("account") or "").strip().lower()
    day = str(story.get("post_date") or "")[:10]
    slot = story.get("slot_index")
    if (story.get("gym_id") != "lasso" or account not in ("instagram", "facebook")
            or story.get("format") != "story" or slot not in (0, 1, 2)
            or not base.FIRST <= base.date.fromisoformat(day) <= base.LAST
            or story.get("status") != "pending"
            or story.get("variant_status") != "active"
            or story.get("published_at") or story.get("late_post_id") is not None
            or story.get("publish_claim_token") is not None
            or story.get("media_not_ready_reason") not in (None, "paired_feed_not_ready")
            or str(feed.get("account") or "").strip().lower() != account
            or str(feed.get("post_date") or "")[:10] != day
            or feed.get("slot_index") != slot
            or story.get("logical_post_id") != feed.get("logical_post_id")):
        raise ValueError("Story is not an unclaimed pending exact feed pair")
    # Reuse the insert planner's independent source/artifact checks, while
    # deliberately allowing this one existing Story to occupy the slot.
    active = base._active_day(store, day)
    candidates = [r for r in active if str(r.get("account") or "").lower() == account
                  and r.get("slot_index") == slot
                  and str(r.get("format") or "").lower() == "feed"
                  and (r.get("variant_status") or "active") == "active"]
    if len(candidates) != 1 or candidates[0].get("id") != feed_id:
        raise ValueError("ambiguous active feed in Story slot")
    competing = [r for r in active if r.get("id") != story_id
                 and str(r.get("account") or "").lower() == account
                 and str(r.get("format") or "").lower() == "story"
                 and (r.get("variant_status") or "active") == "active"
                 and (r.get("status") or "pending") in ("pending", "approved", "publishing")
                 and r.get("slot_index") in (None, slot)]
    if competing:
        raise ValueError("another publishable Story occupies this slot")
    historical = [r for r in active if r.get("id") != story_id
                  and str(r.get("account") or "").lower() == account
                  and str(r.get("format") or "").lower() == "story"
                  and (r.get("variant_status") or "active") == "active"
                  and r.get("slot_index") == slot
                  and r.get("status") == "published"
                  and r.get("published_at") and r.get("late_post_id") is not None]
    if historical:
        raise ValueError("historical Story already published in this slot")
    # The planner will see this Story; adapt only its occupancy check by
    # checking the source and artifact directly here.
    url = str(item.get("story_image_url") or "").strip()
    digest = str(item.get("story_sha256") or "")
    policy = str(item.get("policy_version") or "").strip()
    tenant = str(item.get("artifact_tenant") or "lasso").strip().lower()
    if (not url.startswith("https://") or not base.re.fullmatch(r"[0-9a-f]{64}", digest)
            or not policy or tenant not in ("lasso", "lasso_ig", "lasso_fb")
            or tenant == "lasso_ig" and account != "instagram"
            or tenant == "lasso_fb" and account != "facebook"
            or url == feed.get("image_url")
            or feed.get("format") != "feed" or feed.get("variant_status") != "active"
            or feed.get("status") not in ("pending", "approved", "published")
            or feed.get("media_not_ready_reason") is not None
            or not str(feed.get("caption") or "").strip()
            or not str(feed.get("image_url") or "").startswith("https://")):
        raise ValueError("feed or reviewed Story media invalid")
    if feed["status"] == "published" and (not feed.get("published_at") or
                                              feed.get("late_post_id") is None):
        raise ValueError("published feed lacks receipt")
    if feed["status"] != "published" and (feed.get("published_at") or
                                            feed.get("late_post_id") is not None):
        raise ValueError("unpublished feed has receipt")
    identity = base.source_identity(feed)
    source_hash = (identity["source_hash"] if tenant == "lasso" else
                   hashlib.sha256(feed["caption"].encode("utf-8")).hexdigest())
    expected = identity if tenant == "lasso" else {
        "source_id": f"content_calendar:{feed_id}:caption",
        "source_hash": source_hash}
    artifact = base._artifact(store, url, tenant)
    evidence = artifact.get("evidence") or {}
    recorded = artifact.get("source_identity") or {}
    if (artifact.get("image_sha256") != digest
            or evidence.get("grade_status") != "PASS"
            or evidence.get("image_sha256") != digest
            or evidence.get("policy_version") != policy
            or not base.measured_story_evidence(evidence, digest)
            or any(recorded.get(k) != v for k, v in expected.items())):
        raise ValueError("9:16 artifact does not prove exact feed source")
    return {"story_id": story_id, "feed_id": feed_id, "account": account,
            "date": day, "slot_index": slot, "story_image_url": url,
            "story_sha256": digest, "artifact_tenant": tenant,
            "source_hash": source_hash, "policy_version": policy,
            "story_scheduled_at": base._scheduled_after_feed(feed, day),
            "expected_story": {k: story.get(k) for k in STORY_SNAPSHOT},
            "expected_feed": {k: feed.get(k) for k in FEED_SNAPSHOT}}


def apply_one(store, action):
    payload = {"p_story_id": action["story_id"], "p_feed_id": action["feed_id"],
               "p_expected_story": action["expected_story"],
               "p_expected_feed": action["expected_feed"],
               "p_story_image_url": action["story_image_url"],
               "p_story_sha256": action["story_sha256"],
               "p_artifact_tenant": action["artifact_tenant"],
               "p_source_hash": action["source_hash"],
               "p_policy_version": action["policy_version"],
               "p_story_scheduled_at": action["story_scheduled_at"]}
    response = store._client().post(store._rest(RPC),
        headers=store._headers({"Content-Type": "application/json"}),
        json=payload, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"guarded Story repair failed ({response.status_code})")
    receipt = response.json()
    if not isinstance(receipt, dict) or receipt.get("result") not in ("repaired", "conflict"):
        raise RuntimeError("malformed Story repair receipt")
    if receipt["result"] == "repaired" and receipt.get("id") != action["story_id"]:
        raise RuntimeError("repair returned wrong Story ID")
    return receipt


def run(manifest, store, *, apply=False):
    items = manifest.get("repairs") if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 36:
        raise ValueError("manifest needs 1-36 repairs")
    ids = [str(item.get("story_id")) for item in items if isinstance(item, dict)]
    if len(ids) != len(items) or len(ids) != len(set(ids)):
        raise ValueError("manifest repeats or omits a Story ID")
    results = []
    for item in items:
        try:
            action = plan_one(store, item)
            receipt = apply_one(store, action) if apply else {"result": "dry_run"}
            if apply and receipt["result"] == "repaired":
                after = base._one(store, "content_calendar", {
                    "gym_id": "eq.lasso", "id": "eq." + action["story_id"], "select": "*"})
                expected_hold = (None if receipt.get("hold_reason") is None else
                                 "paired_feed_not_ready")
                def same_instant(left, right):
                    return base.datetime.fromisoformat(str(left).replace("Z", "+00:00")) == \
                        base.datetime.fromisoformat(str(right).replace("Z", "+00:00"))
                if (after.get("image_url") != action["story_image_url"]
                        or after.get("source_media_url") != action["story_image_url"]
                        or after.get("pillar") != action["expected_feed"]["pillar"]
                        or not same_instant(after.get("scheduled_at"), action["story_scheduled_at"])
                        or after.get("media_not_ready_reason") != expected_hold):
                    raise RuntimeError("Story repair readback mismatch")
            results.append({"story_id": action["story_id"], "feed_id": action["feed_id"],
                            "receipt": receipt})
        except Exception as exc:
            results.append({"story_id": item.get("story_id"), "receipt": {
                "result": "blocked", "reason": type(exc).__name__,
                "detail": str(exc)[:220]}})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.receipt.exists():
        raise SystemExit("refusing to overwrite an apply receipt")
    if args.apply:
        from agent import visual_writer_prepare
        if visual_writer_prepare.enabled():
            raise SystemExit("visual writer is armed; extend scene claim before apply")
    store = SupabaseCalendarStore()
    result = run(json.loads(args.manifest.read_text(encoding="utf-8")),
                 store, apply=args.apply)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({"mode": "apply" if args.apply else "dry_run",
                                        "results": result}, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    print(json.dumps({"receipt": str(args.receipt), "results": result}, sort_keys=True))
    return 0 if all(r["receipt"]["result"] in ("dry_run", "repaired") for r in result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
