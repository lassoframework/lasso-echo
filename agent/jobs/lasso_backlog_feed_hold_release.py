"""Release exact October LASSO backlog feed holds after reviewed Story staging.

This job only calls the service-role CAS RPC. It never publishes, creates art,
changes copy, or lifts a hold when the managed Story source proof is absent.
Run after the paired Story stage/repair job and before the publisher.
"""

from datetime import date

from agent.portal_calendar_store import SupabaseCalendarStore

FIRST = "2026-10-02"
LAST = "2026-10-05"
HOLD = "prepared_backlog_waiting_for_story_and_capacity"
ACCOUNTS = ("instagram", "facebook")
MAX_PER_ACCOUNT = 3
FEED_FIELDS = ("id", "gym_id", "account", "post_date", "slot_index",
               "format", "status", "variant_status", "caption", "image_url",
               "pillar", "scheduled_at", "logical_post_id",
               "media_not_ready_reason", "published_at", "late_post_id",
               "publish_claim_token", "publish_reservation_day")
STORY_FIELDS = ("id", "gym_id", "account", "post_date", "slot_index",
                "format", "status", "variant_status", "caption", "image_url",
                "source_media_url", "pillar", "scheduled_at", "logical_post_id",
                "media_not_ready_reason", "published_at", "late_post_id",
                "publish_claim_token", "publish_reservation_day")


def _rows(store, table, params):
    response = store._client().get(store._rest(table), params=params,
                                   headers=store._headers(), timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"LASSO backlog {table} read failed ({response.status_code})")
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError("LASSO backlog read malformed")
    return rows


def _one(store, table, params):
    rows = _rows(store, table, dict(params, limit="2"))
    if len(rows) != 1:
        raise RuntimeError(f"LASSO backlog expected one {table} row")
    return rows[0]


def _exact_snapshot(row, fields):
    if not isinstance(row, dict) or any(field not in row for field in fields):
        raise RuntimeError("LASSO backlog exact snapshot incomplete")
    return {field: row[field] for field in fields}


def _pending(row, fmt, account):
    return (row.get("gym_id") == "lasso"
            and row.get("account") == account
            and row.get("format") == fmt
            and FIRST <= str(row.get("post_date") or "") <= LAST
            and row.get("slot_index") in (0, 1, 2)
            and row.get("status") == "pending"
            and row.get("variant_status") == "active"
            and row.get("published_at") is None
            and row.get("late_post_id") is None
            and row.get("publish_claim_token") is None
            and row.get("publish_reservation_day") is None)


def run(*, account, store=None, max_per_run=MAX_PER_ACCOUNT,
        today=None):
    """Try at most three exact held feed/Story pairs for one LASSO account.

    Caller wires this after staging. SQL re-verifies every live condition under
    locks; these reads merely bound and select candidates. A failed/malformed
    read fails closed before any RPC call.
    """
    if account not in ACCOUNTS or type(max_per_run) is not int \
            or not 1 <= max_per_run <= MAX_PER_ACCOUNT:
        raise ValueError("invalid LASSO backlog account or cap")
    run_day = date.fromisoformat(today) if isinstance(today, str) else (today or date.today())
    if not date(2026, 10, 5) <= run_day <= date(2026, 10, 11):
        return {"account": account, "attempted": 0, "released": 0,
                "idempotent": 0, "blocked": 0, "reason": "outside_catchup_window"}
    store = store or SupabaseCalendarStore()
    rows = store.rows_in_range_complete("lasso", FIRST, LAST,
                                        all_statuses=True)
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or row.get("gym_id") != "lasso"
        or not FIRST <= str(row.get("post_date") or "")[:10] <= LAST
        for row in rows):
        raise RuntimeError("LASSO backlog complete read failed")
    held = [r for r in rows if _pending(r, "feed", account)
            and r.get("media_not_ready_reason") == HOLD]
    held.sort(key=lambda r: (r["post_date"], r["slot_index"], r["id"]))
    result = {"account": account, "attempted": 0, "released": 0,
              "idempotent": 0, "blocked": 0}
    for feed in held:
        if result["attempted"] >= max_per_run:
            break
        paired = [r for r in rows if _pending(r, "story", account)
                  and r.get("post_date") == feed["post_date"]
                  and r.get("slot_index") == feed["slot_index"]]
        if len(paired) != 1:
            result["blocked"] += 1
            continue
        story = paired[0]
        if (story.get("media_not_ready_reason") not in
                (None, "paired_feed_not_ready")
                or story.get("image_url") == feed.get("image_url")
                or story.get("source_media_url") != story.get("image_url")):
            result["blocked"] += 1
            continue
        # The registry is read before the RPC for a clear operator receipt.
        # The RPC independently validates it inside the locked transaction.
        try:
            link = _one(store, "lasso_managed_paired_stories", {
                "story_id": "eq." + story["id"], "select": "story_id,feed_id"})
            if link != {"story_id": story["id"], "feed_id": feed["id"]}:
                result["blocked"] += 1
                continue
            expected_feed = _exact_snapshot(feed, FEED_FIELDS)
            expected_story = _exact_snapshot(story, STORY_FIELDS)
        except (RuntimeError, KeyError, TypeError):
            result["blocked"] += 1
            continue
        result["attempted"] += 1
        response = store._client().post(
            store._rest("rpc/release_lasso_backlog_feed_hold"),
            headers=store._headers({"Content-Type": "application/json"}),
            json={"p_feed_id": feed["id"], "p_story_id": story["id"],
                  "p_expected_feed": expected_feed,
                  "p_expected_story": expected_story}, timeout=30)
        if response.status_code >= 400:
            result["blocked"] += 1
            continue
        receipt = response.json()
        if not isinstance(receipt, dict) or receipt.get("result") not in (
                "released", "idempotent") or receipt.get("feed_id") != feed["id"] \
                or receipt.get("story_id") != story["id"]:
            result["blocked"] += 1
            continue
        try:
            fresh = _one(store, "content_calendar", {
                "gym_id": "eq.lasso", "id": "eq." + feed["id"], "select": "*"})
        except RuntimeError:
            # The RPC may have committed; report an uncertain receipt rather
            # than treating a failed readback as a verified release.
            result["blocked"] += 1
            continue
        if (fresh.get("media_not_ready_reason") is not None
                or any(fresh.get(key) != feed.get(key) for key in FEED_FIELDS
                       if key != "media_not_ready_reason")):
            result["blocked"] += 1
            continue
        result[receipt["result"]] += 1
    return result
