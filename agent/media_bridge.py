"""Persistent media episodes and fail-closed client Slack notices.

Operator recovery for an unresolved send

Run ``python -m agent.media_bridge list GYM`` against the worker's durable DB.
For each episode, inspect the exact channel's Slack history around created_at and
match the exact notice text. If found, record the Slack timestamp with
``mark-delivered GYM EPISODE CHANNEL TS``. If verified absent, use
``mark-absent GYM EPISODE CHANNEL``. Never mark absent from a failed Slack search.
Old episodes remain queryable after media rearms the bridge.
"""
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone

from . import config, db


def enabled():
    return os.environ.get("AGENT_MEDIA_BRIDGE_ALERTS", "false").lower() in ("true", "1", "yes", "on")


def _get(conn, key):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row and row["value"] else None


def _put(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO kv (key,value) VALUES (?,?)",
                 (key, json.dumps(value, sort_keys=True)))


_SHARED_RUNWAY_UNAVAILABLE = object()


def _shared_store():
    """The optional cross-service runway store, or None when it is unavailable.

    The worker's SQLite state remains durable on its own.  This adapter deliberately
    imports lazily so a local-only installation keeps the historical behavior while
    the shared table is not configured.
    """
    try:
        from .shared_media_runway_store import SharedMediaRunwayStore
    except ImportError:
        return None
    store = SharedMediaRunwayStore()
    return store if store.available() else None


def shared_snapshot(base):
    """Read the shared worker snapshot.

    `_SHARED_RUNWAY_UNAVAILABLE` is distinct from an available store that has no
    row: the portal may use its local SQLite fallback only in the former case.
    A reachable store returns its projection or None.  The portal treats that None
    as neutral unknown because the read model deliberately does not distinguish a
    missing row from an unavailable transport.
    """
    store = _shared_store()
    if store is None:
        return _SHARED_RUNWAY_UNAVAILABLE
    return store.read(base)


def _notice_snapshot(base, state):
    """The current episode's transport state, without channel or notice text."""
    episode_id = (state or {}).get("id")
    notice = next((row for row in notice_status(base)
                   if row.get("episode_id") == episode_id), None) if episode_id else None
    status = notice.get("status") if notice else "none"
    if status not in {"none", "unresolved", "ready", "sent"}:
        status = "unresolved"
    return {
        "status": status,
        "episode_id": episode_id,
        "created_at": notice.get("created_at") if notice else None,
        "delivery_confirmed": bool(status == "sent" and notice.get("ts")),
    }


def _fallback_projection(base, state, *, now=None):
    """Project private local dates into the portal-safe shared runway contract."""
    if not state:
        return None
    from .calendar_autopublish import _local_now
    today = _local_now(now, config.posting_timezone_for(base)).date()
    active = today.isoformat() <= state["end"]
    return {
        "active": active,
        "episode_id": state["id"],
        "depleted_on": state["depleted_on"],
        "dates": [state["start"], state["end"]] if active else [],
        "drafts_need_review": active,
        "status": None,
    }


def _mirror_shared_snapshot(base, state, *, now=None):
    """Best-effort mirror after a committed local transition.

    A shared write must never undo or fail the worker's local SQLite transition.
    The compact payload gives the portal the exact state it needs while excluding
    Slack channel ids and client-facing notice text.
    """
    try:
        store = _shared_store()
        if store is None:
            return False
        return bool(store.upsert(
            base,
            fallback_episode=_fallback_projection(base, state, now=now),
            notice_state=_notice_snapshot(base, state),
        ))
    except Exception as exc:  # local SQLite is the fallback when the shared plane fails
        print(f"[media-bridge] shared runway mirror failed for {base}: "
              f"{type(exc).__name__}")
        return False


def episode(base, *, now=None, create=True):
    """Persist two fixed gym-local dates, once for each depletion episode."""
    from .calendar_autopublish import _local_now
    today = _local_now(now, config.posting_timezone_for(base)).date()
    conn = db.connect()
    changed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = _get(conn, "media_bridge_episode_" + base)
        if state is None and create:
            start = today + timedelta(days=1)
            state = {"id": uuid.uuid4().hex, "depleted_on": today.isoformat(), "start": start.isoformat(),
                     "end": (start + timedelta(days=1)).isoformat()}
            _put(conn, "media_bridge_episode_" + base, state)
            changed = True
        conn.commit()
    finally:
        conn.close()
    if changed:
        _mirror_shared_snapshot(base, state, now=now)
    return state


def bridge_days(base, *, now=None, days_ahead=2):
    from .calendar_autopublish import _local_now
    state = episode(base, now=now)
    today = _local_now(now, config.posting_timezone_for(base)).date()
    start, end = date.fromisoformat(state["start"]), date.fromisoformat(state["end"])
    return [(today + timedelta(days=i)).isoformat()
            for i in range(1, min(int(days_ahead), 2) + 1)
            if start <= today + timedelta(days=i) <= end]


def reset_notice(base):
    """Close this episode only after a new usable client upload."""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM kv WHERE key=?", ("media_bridge_episode_" + base,))
        conn.commit()
    finally:
        conn.close()
    _mirror_shared_snapshot(base, None)


def rearm_for_new_upload(base, asset_identity, *, usable):
    """Close an episode once for a newly approved, currently usable local asset.

    The identity marker is durable and written in the same transaction as the
    episode deletion.  Consequently, an idempotent approval retry cannot erase
    an episode created after the first approval, while a retry after a failed
    transaction can still complete the one missing rearm.
    """
    identity = _local_asset_identity(asset_identity)
    if not usable or not identity:
        return False
    conn = db.connect()
    rearmed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        seen, _ = _local_inventory_history(conn, base)
        if identity in seen:
            conn.commit()
            return False
        seen.add(identity)
        _put(conn, _local_inventory_key(base), sorted(seen))
        conn.execute("DELETE FROM kv WHERE key=?", ("media_bridge_episode_" + base,))
        conn.commit()
        rearmed = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if rearmed:
        _mirror_shared_snapshot(base, None)
    return rearmed


def _observe_inventory(base, lane, asset_ids):
    """Persist usable asset identities and atomically close an old episode once."""
    names = sorted({str(value) for value in asset_ids if value})
    key = "media_bridge_" + lane + "_inventory_" + base
    conn = db.connect()
    rearmed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = _get(conn, key)
        seen = set(previous or [])
        if previous is not None and any(name not in seen for name in names):
            conn.execute("DELETE FROM kv WHERE key=?", ("media_bridge_episode_" + base,))
            rearmed = True
        _put(conn, key, sorted(seen.union(names)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if rearmed:
        _mirror_shared_snapshot(base, None)


def _local_inventory_key(base):
    """The sole durable identity ledger for reviewed local client media."""
    return "media_bridge_local_inventory_" + base


def _local_asset_identity(value):
    """Match approval basenames with planner paths within one tenant ledger."""
    text = str(value or "").strip()
    return os.path.basename(text) if text else ""


def _local_inventory_history(conn, base):
    """Read the unified ledger and both short-lived Round 4 predecessor keys.

    Older workers persisted full paths for observer entries and basenames for
    approvals.  Folding both into normalized basenames before the next write
    prevents an upgrade from treating an already-consumed asset as fresh.
    """
    keys = (
        _local_inventory_key(base),
        "media_bridge_local_approved_inventory_" + base,
    )
    values = [_get(conn, key) for key in keys]
    seen = {
        identity for value in values for identity in
        (_local_asset_identity(item) for item in (value or [])) if identity
    }
    return seen, any(value is not None for value in values)


def observe_drive_inventory(base, asset_ids):
    """Observe the planner's pickable Drive pool after a successful read."""
    _observe_inventory(base, "drive", asset_ids)


def observe_local_inventory(base, asset_paths):
    """Observe approved, clean, unused local media after a successful read."""
    names = sorted({identity for path in asset_paths
                    if (identity := _local_asset_identity(path))})
    conn = db.connect()
    rearmed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        seen, established = _local_inventory_history(conn, base)
        fresh = set(names).difference(seen)
        if established and fresh:
            conn.execute("DELETE FROM kv WHERE key=?", ("media_bridge_episode_" + base,))
            rearmed = True
        _put(conn, _local_inventory_key(base), sorted(seen.union(names)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if rearmed:
        _mirror_shared_snapshot(base, None)


def notice_text():
    return (
        "Echo cannot currently find a usable photo or video for your next post. "
        "If your gym has suitable source information, Echo may prepare up to two days of infographic drafts. "
        "These drafts need review before they can publish. "
        "Please add fresh photos and videos through Upload media in your Echo portal."
    )


def _outbox_key(base, state):
    return "media_bridge_outbox_" + base + "_" + state["id"]


def notice_status(base):
    """Audit view of every intent across old and current episodes for a tenant."""
    conn = db.connect()
    try:
        prefix = "media_bridge_outbox_" + base + "_"
        rows = conn.execute("SELECT key,value FROM kv WHERE substr(key,1,?)=?",
                            (len(prefix), prefix)).fetchall()
        return [{"episode_id": r["key"].rsplit("_", 1)[-1],
                 "status": data.get("status"), "channel": data.get("channel"),
                 "created_at": data.get("created_at"), "ts": data.get("ts"),
                 "text": data.get("text")}
                for r in rows if (data := json.loads(r["value"]))]
    finally:
        conn.close()


def unresolved_notices(base):
    return [row for row in notice_status(base) if row["status"] == "unresolved"]


def reconcile_notice(base, *, delivered=False, channel="", ts="", episode_id=""):
    """Record a manual Slack-history finding for an uncertain send."""
    state = episode(base, create=False)
    if not state and not episode_id:
        return {"ok": False, "reason": "no episode"}
    key = "media_bridge_outbox_" + base + "_" + episode_id if episode_id else _outbox_key(base, state)
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, key)
        if not row or row["status"] != "unresolved":
            conn.rollback()
            return {"ok": False, "reason": "no unresolved send"}
        if channel and row["channel"] != channel:
            conn.rollback()
            return {"ok": False, "reason": "channel mismatch"}
        if delivered and (not channel or not ts):
            conn.rollback()
            return {"ok": False, "reason": "channel and timestamp required"}
        row["status"] = "sent" if delivered else "ready"
        if delivered:
            row["ts"] = str(ts)
        _put(conn, key, row)
        conn.commit()
    finally:
        conn.close()
    _mirror_shared_snapshot(base, state)
    return {"ok": True, "status": row["status"]}


def notify_bridge(base, account, *, logger=None, now=None, poster=None):
    """Commit send intent before transport. Never automatically retry uncertainty."""
    log = logger or (lambda *_: None)
    if not enabled():
        return {"ok": False, "reason": "flag off"}
    try:
        if not config.slack_convo_client_reply_armed("echo"):
            return {"ok": False, "reason": "client replies disabled"}
        from .media_bridge_route import resolve_client_route
        route = resolve_client_route(base)
        if not route.ok:
            return {"ok": False, "reason": route.reason}
        if poster is None:
            from .slack_surface import SlackPoster
            poster = SlackPoster(channel=route.channel)
        if hasattr(poster, "_send"):
            who = poster._send("https://slack.com/api/auth.test", {})
            if not who.get("ok") or who.get("user_id") != "U0BE39F02KV":
                return {"ok": False, "reason": "Echo sender not verified"}
            info = poster._send("https://slack.com/api/conversations.info",
                                {"channel": route.channel})
            channel_info = info.get("channel") or {}
            if (not info.get("ok") or channel_info.get("id") != route.channel
                    or channel_info.get("is_archived") or not channel_info.get("is_member")):
                return {"ok": False, "reason": "Echo channel membership not verified"}
        state = episode(base, now=now)
        key = _outbox_key(base, state)
        conn = db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _get(conn, key)
            if row and row["status"] == "sent":
                conn.commit()
                return {"ok": True, "deduped": True}
            if row and row["status"] == "unresolved":
                conn.commit()
                return {"ok": False, "reason": "unresolved send", "reconcile": True}
            if row and row["channel"] != route.channel:
                conn.commit()
                return {"ok": False, "reason": "route changed; reconcile required"}
            row = {"status": "unresolved", "channel": route.channel,
                   "text": notice_text(), "created_at": datetime.now(timezone.utc).isoformat()}
            _put(conn, key, row)
            conn.commit()
        finally:
            conn.close()
        _mirror_shared_snapshot(base, state, now=now)
        result = poster._chat_post(text=row["text"], blocks=None, channel=route.channel)
        if not result or not result.get("ok") or not result.get("ts"):
            return {"ok": False, "reason": "unresolved send", "reconcile": True}
        conn = db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row["status"], row["ts"] = "sent", str(result["ts"])
            _put(conn, key, row)
            conn.commit()
        finally:
            conn.close()
        _mirror_shared_snapshot(base, state, now=now)
        return {"ok": True, "sent": True}
    except Exception as exc:
        log(f"{base} media bridge notice failed {type(exc).__name__}")
        return {"ok": False, "reason": type(exc).__name__, "reconcile": True}


def retry_existing_notice(base, account, store, *, now=None, logger=None):
    if enabled() and episode(base, now=now, create=False):
        return notify_bridge(base, account, now=now, logger=logger)


def _main():
    import argparse
    parser = argparse.ArgumentParser(description="Inspect and reconcile media bridge notices")
    parser.add_argument("action", choices=("list", "status", "mark-delivered", "mark-absent"))
    parser.add_argument("gym")
    parser.add_argument("episode_id", nargs="?")
    parser.add_argument("channel", nargs="?")
    parser.add_argument("ts", nargs="?")
    args = parser.parse_args()
    if args.action == "list":
        result = unresolved_notices(args.gym)
    elif args.action == "status":
        result = notice_status(args.gym)
    else:
        if not args.episode_id:
            parser.error("episode_id required")
        if not args.channel:
            parser.error("verified intent channel required")
        if args.action == "mark-delivered" and (not args.channel or not args.ts):
            parser.error("channel and Slack timestamp required")
        result = reconcile_notice(
            args.gym, delivered=args.action == "mark-delivered",
            channel=args.channel or "", ts=args.ts or "", episode_id=args.episode_id)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    _main()
