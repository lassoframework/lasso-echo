"""
reply_engine_watch.py — the reply engine is FAILING SILENTLY (AUD-008 / AUD-105).

WHAT IS BROKEN (verified live 2026-09-05 against the shared plane):

  echo_reply_accounts   6 rows, covering eng and topfuel ONLY
  echo_reply_settings   2 rows (eng, topfuel), both with last_sync_at NULL
  echo_reply_queue     10 rows total, EVERY ONE created 2026-08-31T14:27:20
                       -> five days of zero ingest
  gym_social_accounts  42 rows, all carrying a late_account_id; 30 of them are
                       instagram or facebook, and 26 of those (13 gyms) have NO
                       row in echo_reply_accounts

The portal's reply webhook answers HTTP 200 with {ignored: "account not mapped to
a gym"} when echo_reply_accounts has no row for the account, and {ignored: "gym
disabled"} when settings are absent. A 200 is not an error anywhere, so 13 of 15
gyms drop every inbound comment on the floor and NOTHING says a word.

WHY NOBODY NOTICED (AUD-105): the "REPLY NEEDED" cards come from
agent/inbox_alerts.py, which reads the Zernio inbox API directly. Nothing in this
whole package has ever read echo_reply_queue (grep it: zero hits outside this
file). Two systems that disagree, and the one that can actually REPLY is the empty
one. An empty queue produces no signal at all, so the silence looks like calm.

WHAT THIS MODULE DOES: read the four tables, compare them, and say plainly which
gyms are unmapped and whether ingest has stopped. READ ONLY. It never writes a
mapping, never enables a gym, never touches the queue, and never replies to
anyone -- inserting a mapping on a gym's behalf would arm auto-reply for a client
who never asked for it. It reports so a human can decide.

Flag: config.reply_engine_watch_enabled() (AGENT_REPLY_ENGINE_WATCH, default OFF).
"""

from datetime import datetime, timezone

from . import config

# Only the platforms the reply engine can actually answer on. googlebusiness rows
# exist in echo_reply_accounts but reviews are a different lane, so an unmapped GBP
# account is NOT counted as a gap here (naming a gap that is not one is how a
# watchdog earns its way into the noise filter).
_REPLY_PLATFORMS = ("instagram", "facebook")

# How long the queue may go without a new row before ingest is called stale. The
# webhook is event-driven, so a quiet weekend is normal; five days is not.
STALE_INGEST_DAYS = 3


def enabled():
    return config.reply_engine_watch_enabled()


def _norm(value):
    return str(value or "").strip().lower()


def audit(*, reply_accounts, reply_settings, social_accounts, queue_rows,
          gym_names=None, now=None, stale_days=STALE_INGEST_DAYS):
    """PURE. Compare the four tables and return the findings.

    Returns {mapped, unmapped, unmapped_gyms, disabled_gyms, gyms_with_accounts,
             queue_rows, newest_row, stale_days_actual, ingest_stale}.
    Every input is a plain list of dicts, so this tests without a network call.
    """
    now = now or datetime.now(timezone.utc)
    names = dict(gym_names or {})

    mapped_ids = {_norm(r.get("late_account_id")) for r in (reply_accounts or [])
                  if r.get("late_account_id")}
    settings_gyms = {_norm(r.get("gym_id")) for r in (reply_settings or [])
                     if r.get("enabled")}

    live = [r for r in (social_accounts or [])
            if r.get("late_account_id") and _norm(r.get("platform")) in _REPLY_PLATFORMS]
    unmapped = [r for r in live if _norm(r.get("late_account_id")) not in mapped_ids]

    gyms_with_accounts = {_norm(r.get("gym_id")) for r in live}
    unmapped_gyms = sorted({names.get(_norm(r.get("gym_id")), _norm(r.get("gym_id")))
                            for r in unmapped})

    # TWO KEY SPACES, one column name (found while verifying this watchdog, live
    # 2026-09-05): echo_reply_accounts / echo_reply_settings / echo_reply_queue key
    # gym_id by the ECHO ACCOUNT KEY ('eng', 'topfuel'), while gym_social_accounts
    # keys gym_id by the gyms UUID. Comparing them directly made a HEALTHY gym look
    # disabled, which is exactly the false alarm that gets a watchdog muted.
    #
    # The join is late_account_id, which BOTH sides carry, so no slug guessing and
    # no name munging: an account id that appears in echo_reply_accounts tells us
    # which UUID that account key belongs to.
    social_by_late = {_norm(r.get("late_account_id")): _norm(r.get("gym_id"))
                      for r in live if r.get("late_account_id")}
    key_to_uuid = {}
    for r in (reply_accounts or []):
        uuid = social_by_late.get(_norm(r.get("late_account_id")))
        if uuid:
            key_to_uuid.setdefault(_norm(r.get("gym_id")), uuid)
    enabled_uuids = {key_to_uuid.get(g, g) for g in settings_gyms}

    # A gym can be MAPPED and still dead: settings absent or enabled=false is the
    # webhook's other silent 200 ({ignored: "gym disabled"}).
    disabled_gyms = sorted({names.get(g, g) for g in gyms_with_accounts
                            if g not in enabled_uuids}
                           - set(unmapped_gyms))

    stamps = []
    for r in queue_rows or []:
        raw = str(r.get("created_at") or "")[:19]
        if raw:
            stamps.append(raw)
    newest = max(stamps) if stamps else ""
    stale_actual = None
    if newest:
        try:
            d = datetime.fromisoformat(newest).replace(tzinfo=timezone.utc)
            stale_actual = (now - d).days
        except ValueError:
            stale_actual = None
    ingest_stale = bool(newest) and stale_actual is not None and stale_actual > stale_days
    if not queue_rows:
        ingest_stale = True

    return {
        "mapped": len(mapped_ids),
        "unmapped": len(unmapped),
        "unmapped_gyms": unmapped_gyms,
        "disabled_gyms": disabled_gyms,
        "gyms_with_accounts": len(gyms_with_accounts),
        "queue_rows": len(queue_rows or []),
        "newest_row": newest,
        "stale_days_actual": stale_actual,
        "ingest_stale": ingest_stale,
        # True when the reply tables and gym_social_accounts key gym_id in
        # different spaces (see key_space_note).
        "key_space_split": bool(key_to_uuid) and any(
            k != v for k, v in key_to_uuid.items()),
    }


def report(findings):
    """One plain message, or "" when the reply engine is healthy. PURE."""
    f = findings or {}
    unmapped = int(f.get("unmapped") or 0)
    disabled = list(f.get("disabled_gyms") or [])
    if not unmapped and not disabled and not f.get("ingest_stale"):
        return ""
    lines = ["REPLY ENGINE: comments are being dropped without an error."]
    if unmapped:
        gyms = f.get("unmapped_gyms") or []
        lines.append(
            f"{unmapped} connected Instagram / Facebook account(s) across "
            f"{len(gyms)} gym(s) have no row in echo_reply_accounts, so the reply "
            f"webhook answers 200 'account not mapped to a gym' and throws every "
            f"comment away: {', '.join(gyms[:12])}"
            + (" and more" if len(gyms) > 12 else "") + ".")
    if disabled:
        lines.append(
            f"{len(disabled)} gym(s) are mapped but have no enabled settings row, "
            f"which the webhook answers 200 'gym disabled' for: "
            f"{', '.join(disabled[:12])}.")
    if f.get("ingest_stale"):
        newest = f.get("newest_row") or "never"
        days = f.get("stale_days_actual")
        lines.append(
            f"echo_reply_queue holds {int(f.get('queue_rows') or 0)} row(s) and the "
            f"newest arrived {newest}"
            + (f" ({days} day(s) ago)" if days is not None else "")
            + ". The engine polls, succeeds, and ingests nothing.")
    lines.append(
        "Nothing here is auto repaired: mapping a gym arms replying on that gym's "
        "behalf, and that is a person's decision. Add the echo_reply_accounts rows "
        "and an enabled echo_reply_settings row for each gym that should be live.")
    return " ".join(lines)


def key_space_note(findings):
    """A separate line for the SECOND defect this watchdog turned up: the reply
    tables and gym_social_accounts both call the column gym_id and mean different
    things. Returned separately so it is reported as its own problem and not buried
    inside an outage message. "" when the join resolved cleanly."""
    f = findings or {}
    if not f.get("key_space_split"):
        return ""
    return ("REPLY ENGINE key spaces disagree: echo_reply_accounts, "
            "echo_reply_settings and echo_reply_queue key gym_id by the Echo "
            "ACCOUNT KEY, while gym_social_accounts keys gym_id by the gyms UUID. "
            "Any join written between them without going through late_account_id "
            "will silently match nothing, which reads as a healthy gym being "
            "disabled. Nothing was changed; this is a schema fact to fix once.")


def run(*, reader=None, alert_fn=None, db=None, now=None, gym_names=None):
    """The daily watchdog. READ ONLY, kv-deduped per day, durable-or-silent.

    Returns {"ok": bool, "findings": {...}, "alerted": bool}. Flag off -> a no-op
    that reads nothing. Never raises: a watchdog fault may not break a run."""
    if not enabled():
        return {"ok": False, "reason": "AGENT_REPLY_ENGINE_WATCH is OFF (default)",
                "alerted": False}
    now = now or datetime.now(timezone.utc)
    read = reader or _live_reader
    try:
        tables = read()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"read failed: {type(exc).__name__}",
                "alerted": False}

    findings = audit(reply_accounts=tables.get("reply_accounts") or [],
                     reply_settings=tables.get("reply_settings") or [],
                     social_accounts=tables.get("social_accounts") or [],
                     queue_rows=tables.get("queue_rows") or [],
                     gym_names=gym_names or tables.get("gym_names") or {},
                     now=now)
    text = report(findings)
    note = key_space_note(findings)
    if note:
        text = (text + " " + note) if text else note
    if not text:
        return {"ok": True, "findings": findings, "alerted": False}

    try:
        _db = db
        if _db is None:
            from . import db as _dbmod
            _db = _dbmod
        if hasattr(_db, "kv_is_durable") and not _db.kv_is_durable():
            return {"ok": True, "findings": findings, "alerted": False,
                    "reason": "ephemeral kv cannot dedupe; stayed silent"}
        # Re-fires when the shape of the gap CHANGES (a gym mapped, a new one
        # connected), and otherwise once a day. A fleet-wide outage that is still
        # a fleet-wide outage tomorrow should say so again tomorrow.
        key = "reply_engine_watch_state"
        stamp = f"{now.date().isoformat()}|{findings['unmapped']}|" \
                f"{len(findings['disabled_gyms'])}|" \
                f"{int(bool(findings['ingest_stale']))}|" \
                f"{int(bool(findings.get('key_space_split')))}"
        if _db.kv_get(key, "") == stamp:
            return {"ok": True, "findings": findings, "alerted": False}
        _db.kv_set(key, stamp)
    except Exception:  # noqa: BLE001
        return {"ok": True, "findings": findings, "alerted": False}

    try:
        if alert_fn is not None:
            alert_fn(text)
        else:
            from . import ops_alerts
            ops_alerts.alert(text)
    except Exception:  # noqa: BLE001
        return {"ok": True, "findings": findings, "alerted": False}
    return {"ok": True, "findings": findings, "alerted": True, "message": text}


def _live_reader():
    """The four reads against the shared plane. READ ONLY (GET only). Returns
    empty lists on any failure, which the audit treats as "cannot tell", never as
    a false all-clear -- an unreadable plane produces no unmapped count."""
    from .account_key_split_watch import _supabase_get as get

    def _page(table, select):
        out, off = [], 0
        while off < 20000:
            rows = get(table, {"select": select, "limit": "1000",
                               "offset": str(off)})
            if not rows:
                break
            out += rows
            off += len(rows)
            if len(rows) < 1000:
                break
        return out

    gyms = _page("gyms", "id,name,slug")
    return {
        "reply_accounts": _page("echo_reply_accounts",
                                "gym_id,platform,handle,late_account_id,active"),
        "reply_settings": _page("echo_reply_settings", "gym_id,enabled,last_sync_at"),
        "social_accounts": _page("gym_social_accounts",
                                 "gym_id,platform,handle,late_account_id,status"),
        "queue_rows": _page("echo_reply_queue", "gym_id,status,created_at"),
        "gym_names": {str(g.get("id")): (g.get("slug") or g.get("name") or "")
                      for g in gyms},
    }


__all__ = ["enabled", "audit", "report", "key_space_note", "run",
           "STALE_INGEST_DAYS"]