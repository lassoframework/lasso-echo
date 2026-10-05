"""Insert only missing reviewed LASSO Stories for the Oct 2-Nov 8 book.

The manifest names one exact feed UUID and a separately hosted, reviewed 9:16
Story object per entry. Media preparation is an upstream step: render from the
feed's approved source, run the infographic review, verify real 9:16 pixels,
host the result, and register its artifact under tenant ``lasso`` with the
``source_identity(feed)`` returned here, or use variant_regen's account tenant
(``lasso_ig``/``lasso_fb``) and its exact caption source identity. A plain feed crop, date match, or an
unreviewed hosted URL cannot pass. Facebook has its own feed caption and must
have its own source-bound artifact. Dry-run is the default; --apply stages
pending rows only through a transactionally guarded, insert-only RPC.

Manifest format: {"stories": [{"account": "instagram|facebook",
  "date": "2026-10-07", "slot_index": 0, "feed_id": "uuid",
  "story_image_url": "https://...", "story_sha256": "64 lowercase hex",
  "policy_version": "...", "artifact_tenant": "lasso_ig|lasso_fb|lasso"}]}
Maximum 36 entries per invocation; split a larger reviewed manifest into
bounded batches with separate receipts.
"""

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.portal_calendar_store import SupabaseCalendarStore

FIRST, LAST = date(2026, 10, 2), date(2026, 11, 8)
ZONE = ZoneInfo("America/New_York")
RPC = "rpc/stage_lasso_paired_story"


def measured_story_evidence(evidence, digest):
    """Require dimensions measured from the exact reviewed image bytes."""
    measured = evidence.get("verified_dimensions") if isinstance(evidence, dict) else None
    return (isinstance(measured, dict)
            and evidence.get("aspect") == "9:16"
            and evidence.get("pixels") == "1080x1920"
            and type(measured.get("width")) is int and measured["width"] == 1080
            and type(measured.get("height")) is int and measured["height"] == 1920
            and measured.get("image_sha256") == digest)


def deterministic_id(account, feed_id, slot):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"lasso|{account}|{uuid.UUID(str(feed_id))}|story|slot{int(slot)}"))


def source_identity(feed):
    """A source key over this feed's exact account, copy, media, day, slot and ID."""
    source = {
        "source_id": f"content_calendar:{feed['id']}:paired_story",
        "source_feed_id": str(uuid.UUID(str(feed["id"]))),
        "source_account": str(feed["account"]).lower(),
        "source_day": str(feed["post_date"])[:10],
        "source_slot": str(int(feed["slot_index"])),
        "source_feed_caption": str(feed["caption"]),
        "source_feed_image_url": str(feed["image_url"]),
        "source_logical_post_id": str(feed.get("logical_post_id") or ""),
    }
    canonical = json.dumps(source, sort_keys=True, separators=(",", ":"))
    source["source_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return source


def _request_rows(store, table, params):
    response = store._client().get(store._rest(table), params=params,
                                   headers=store._headers(), timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"{table} read failed ({response.status_code})")
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"{table} returned malformed rows")
    return rows


def _one(store, table, params):
    rows = _request_rows(store, table, dict(params, limit="2"))
    if len(rows) != 1:
        raise ValueError(f"expected exactly one {table} row; found {len(rows)}")
    return rows[0]


def _feed(store, feed_id):
    return _one(store, "content_calendar", {
        "gym_id": "eq.lasso", "id": "eq." + feed_id, "select": "*"})


def _artifact(store, url, tenant):
    return _one(store, "echo_infographic_artifacts", {
        "tenant": "eq." + tenant, "image_url": "eq." + url, "select": "*"})


def _active_day(store, day):
    # Include legacy NULL status/variant rows and held rows. The normal
    # complete-range reader has a positive status allowlist and can miss one.
    rows, cursor = [], None
    for _ in range(100):
        params = {"gym_id": "eq.lasso", "post_date": "eq." + day,
                  "order": "id", "limit": "500", "select": "*"}
        if cursor is not None:
            params["id"] = "gt." + cursor
        page = _request_rows(store, "content_calendar", params)
        previous = cursor
        for row in page:
            rid = str(row.get("id") or "") if isinstance(row, dict) else ""
            if (not rid or row.get("gym_id") != "lasso"
                    or str(row.get("post_date") or "")[:10] != day
                    or (previous is not None and rid <= previous)):
                raise RuntimeError("complete day read malformed or out of scope")
            previous = rid
        rows.extend(page)
        if len(page) < 500:
            return rows
        cursor = previous
    raise RuntimeError("complete day read exceeded page bound")


def _scheduled_after_feed(feed, day):
    raw = feed.get("scheduled_at")
    if not raw:
        # A normal calendar row may carry no display schedule. In that case
        # derive BOTH sides from the live publisher's canonical slot mapping;
        # never invent a time from the manifest or a row ID hash.
        from agent.calendar_autopublish import scheduled_iso_for_row
        feed_row = dict(feed, gym_id="lasso", format="feed", post_date=day)
        story_row = dict(feed_row, format="story")
        feed_iso = scheduled_iso_for_row(feed_row, tz_name="America/New_York")
        story_iso = scheduled_iso_for_row(story_row, tz_name="America/New_York")
        if not feed_iso or not story_iso:
            raise ValueError("canonical feed/Story slot schedule unavailable")
        feed_time = datetime.fromisoformat(feed_iso)
        story_time = datetime.fromisoformat(story_iso)
        if (feed_time.date().isoformat() != day
                or story_time.date().isoformat() != day
                or story_time - feed_time != timedelta(minutes=15)):
            raise ValueError("canonical Story must follow its feed by 15 minutes")
        return story_time.isoformat()
    moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("source feed schedule lacks timezone")
    local = moment.astimezone(ZONE)
    if local.date().isoformat() != day:
        raise ValueError("source feed schedule is outside its LASSO day")
    scheduled = local + timedelta(minutes=15)
    if scheduled.date().isoformat() != day:
        raise ValueError("no same-day Story slot after source feed")
    return scheduled.isoformat()


def plan_one(store, item):
    if not isinstance(item, dict):
        raise ValueError("manifest Story item must be an object")
    account = str(item.get("account") or "").strip().lower()
    day = str(item.get("date") or "")
    slot = item.get("slot_index")
    if account not in ("instagram", "facebook") or type(slot) is not int or slot not in (0, 1, 2):
        raise ValueError("Story must name exact IG/FB account and slot 0-2")
    parsed = date.fromisoformat(day)
    if not FIRST <= parsed <= LAST:
        raise ValueError("date outside Oct 2-Nov 8 LASSO book boundary")
    feed_id = str(uuid.UUID(str(item["feed_id"])))
    story_url = str(item.get("story_image_url") or "").strip()
    story_sha = str(item.get("story_sha256") or "")
    policy = str(item.get("policy_version") or "").strip()
    artifact_tenant = str(item.get("artifact_tenant") or "lasso").strip().lower()
    if (not story_url.startswith("https://")
            or not re.fullmatch(r"[0-9a-f]{64}", story_sha) or not policy
            or artifact_tenant not in ("lasso", "lasso_ig", "lasso_fb")
            or (artifact_tenant == "lasso_ig" and account != "instagram")
            or (artifact_tenant == "lasso_fb" and account != "facebook")):
        raise ValueError("Story needs hosted URL, SHA-256 and review policy")

    feed = _feed(store, feed_id)
    if (feed.get("gym_id") != "lasso"
            or str(feed.get("account") or "").strip().lower() != account
            or str(feed.get("format") or "").strip().lower() != "feed"
            or str(feed.get("post_date") or "")[:10] != day
            or feed.get("slot_index") != slot
            or feed.get("variant_status") != "active"
            or feed.get("status") not in {"pending", "approved", "published"}
            or feed.get("media_not_ready_reason") is not None
            or not str(feed.get("caption") or "").strip()
            or not str(feed.get("image_url") or "").startswith("https://")
            or story_url == feed.get("image_url")):
        raise ValueError("exact source feed is missing, changed or media-held")
    if (feed["status"] == "published"
            and (not feed.get("published_at") or feed.get("late_post_id") is None)):
        raise ValueError("source feed has incomplete publish receipt")
    if (feed["status"] in {"pending", "approved"}
            and (feed.get("published_at") or feed.get("late_post_id") is not None)):
        raise ValueError("source feed has inconsistent publish state")
    scheduled = _scheduled_after_feed(feed, day)
    identity = source_identity(feed)
    if artifact_tenant == "lasso":
        expected_source = identity
        source_hash = identity["source_hash"]
    else:
        source_hash = hashlib.sha256(feed["caption"].encode("utf-8")).hexdigest()
        expected_source = {
            "source_id": f"content_calendar:{feed_id}:caption",
            "source_hash": source_hash,
        }
    artifact = _artifact(store, story_url, artifact_tenant)
    evidence = artifact.get("evidence") or {}
    recorded_source = artifact.get("source_identity") or {}
    if (artifact.get("image_sha256") != story_sha
            or evidence.get("grade_status") != "PASS"
            or evidence.get("image_sha256") != story_sha
            or evidence.get("policy_version") != policy
            or not measured_story_evidence(evidence, story_sha)
            or any(recorded_source.get(key) != value for key, value in expected_source.items())):
        raise ValueError("9:16 reviewed artifact is not bound to this exact feed")

    active = _active_day(store, day)
    story_id = deterministic_id(account, feed_id, slot)
    occupied = [row for row in active if isinstance(row, dict)
        and str(row.get("account") or "").strip().lower() == account
        and str(row.get("format") or "").strip().lower() == "story"
        and (row.get("variant_status") or "active") == "active"
        and (row.get("status") or "pending") not in {"denied", "killed", "failed"}
        and row.get("slot_index") in (None, slot)]
    if occupied and not (len(occupied) == 1
            and occupied[0].get("id") == story_id
            and occupied[0].get("image_url") == story_url
            and occupied[0].get("logical_post_id") == feed.get("logical_post_id")
            and (occupied[0].get("status") != "published" or
                 (occupied[0].get("published_at") and
                  occupied[0].get("late_post_id") is not None))):
        blockers = [{"id": str(row.get("id")), "status": str(row.get("status")),
                     "slot_index": row.get("slot_index")} for row in occupied[:5]]
        raise ValueError("active Story already occupies account/day/slot; "
                         "historical published rows need exact source proof: "
                         + json.dumps(blockers, sort_keys=True))
    state = "ready"
    if occupied:
        state = ("satisfied_published" if occupied[0].get("status") == "published"
                 else "already_present")
    return {"account": account, "date": day, "slot_index": slot,
            "feed_id": feed_id, "feed_status": feed["status"],
            "feed_caption": feed["caption"], "feed_image_url": feed["image_url"],
            "feed_pillar": feed["pillar"],
            "feed_scheduled_at": feed["scheduled_at"],
            "feed_logical_post_id": feed.get("logical_post_id"),
            "story_id": story_id, "story_image_url": story_url,
            "story_sha256": story_sha, "artifact_tenant": artifact_tenant,
            "source_hash": source_hash,
            "policy_version": policy, "story_scheduled_at": scheduled,
            "state": state}


def apply_one(store, action):
    payload = {
        "p_feed_id": action["feed_id"], "p_account": action["account"],
        "p_day": action["date"], "p_slot": action["slot_index"],
        "p_feed_status": action["feed_status"],
        "p_feed_caption": action["feed_caption"],
        "p_feed_image_url": action["feed_image_url"],
        "p_feed_scheduled_at": action["feed_scheduled_at"],
        "p_feed_logical_post_id": action["feed_logical_post_id"],
        "p_story_id": action["story_id"],
        "p_story_image_url": action["story_image_url"],
        "p_story_sha256": action["story_sha256"],
        "p_artifact_tenant": action["artifact_tenant"],
        "p_source_hash": action["source_hash"],
        "p_policy_version": action["policy_version"],
        "p_story_scheduled_at": action["story_scheduled_at"],
    }
    response = store._client().post(store._rest(RPC),
        headers=store._headers({"Content-Type": "application/json"}),
        json=payload, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"guarded Story RPC failed ({response.status_code})")
    receipt = response.json()
    if not isinstance(receipt, dict) or receipt.get("result") not in {
            "inserted", "idempotent", "conflict"}:
        raise RuntimeError("guarded Story RPC returned malformed receipt")
    if receipt.get("result") in {"inserted", "idempotent"}:
        if receipt.get("id") != action["story_id"]:
            raise RuntimeError("guarded Story RPC returned wrong identity")
    return receipt


def run(manifest, store, *, apply=False, on_result=None):
    items = manifest.get("stories") if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 36:
        raise ValueError("manifest needs 1-36 Story entries")
    keys = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("manifest contains a non-object Story")
        keys.append((item.get("account"), item.get("date"), item.get("slot_index")))
    if len(keys) != len(set(keys)):
        raise ValueError("manifest repeats an account/day/slot")
    results = []
    for item in sorted(items, key=lambda row: (
            str(row.get("date")), str(row.get("account")),
            str(row.get("slot_index")))):
        try:
            action = plan_one(store, item)
            receipt = ({"result": "already_published", "id": action["story_id"]}
                       if apply and action["state"] == "satisfied_published"
                       else apply_one(store, action) if apply else {"result": "dry_run"})
            if apply and receipt["result"] in {"inserted", "idempotent"}:
                rows = _active_day(store, action["date"])
                matches = [row for row in rows if row.get("id") == action["story_id"]
                           and row.get("image_url") == action["story_image_url"]
                           and row.get("account") == action["account"]
                           and row.get("slot_index") == action["slot_index"]
                           and row.get("logical_post_id") == action["feed_logical_post_id"]]
                if len(matches) != 1:
                    raise RuntimeError("Story insert readback mismatch")
            results.append({"account": action["account"], "date": action["date"],
                            "slot_index": action["slot_index"],
                            "feed_id": action["feed_id"],
                            "story_id": action["story_id"], "receipt": receipt})
        except Exception as exc:
            results.append({"account": item.get("account"), "date": item.get("date"),
                            "slot_index": item.get("slot_index"), "receipt": {
                                "result": "blocked", "reason": type(exc).__name__,
                                "detail": str(exc)[:220]}})
        if on_result is not None:
            on_result(results)
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
            raise SystemExit("visual writer is armed; extend the RPC's scene claim before apply")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    def persist(results):
        args.receipt.write_text(json.dumps({"mode": "apply" if args.apply else "dry_run",
            "results": results}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    results = run(manifest, SupabaseCalendarStore(),
                  apply=args.apply, on_result=persist)
    blocked = sum(row["receipt"]["result"] in {"blocked", "conflict"} for row in results)
    print(json.dumps({"mode": "apply" if args.apply else "dry_run",
                      "total": len(results), "blocked": blocked}))
    return int(bool(blocked))


if __name__ == "__main__":
    sys.exit(main())
