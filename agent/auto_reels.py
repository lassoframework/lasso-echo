"""Settled gym uploads -> one automatic pending reel, never a publishing path.

Runs in Echo's single volume-owning listener. SQLite transactions and a volume file
lock coordinate processes sharing that volume; this is not a distributed lease for
replicas with different disks. Missing persistent storage fails closed. Each content
version is reserved to a durable batch before rendering. Retries keep the same UUID
and reconcile its deterministic calendar row before doing expensive work again.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import uuid

from . import config, db

_PREFIX = "auto_reels:v1:"
_MAX_ATTEMPTS = 3
_RETRY_SECONDS = 300
_MAX_CLIPS = 10
_MIN_CLIPS = 3
_MIN_SECONDS = 15.0
_MOMENT_SECONDS = 7.0
_LOG_TRANSITIONS = {}
_MIRROR_OUTCOMES = {}


def _consumed_elsewhere(jobs, request_id, job):
    consumed = {entry['key'] for rid, other in jobs.items()
                if rid != request_id and other.get('status') == 'staged'
                for entry in other['entries']
                if not other.get('used') or entry['id'] in other['used']}
    return any(entry['key'] in consumed for entry in job['entries'])
_MAX_OPERATOR_RETRIES = 3
# Reasons shown to operators / logs: fixed phrases only (never raw exception text).
_SAFE_REASON_MAX = 200


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _source_key(asset):
    # Content bytes win over Drive names and resync timestamps. The fallback includes
    # size + modified time so an overwritten Drive ID becomes genuinely new footage.
    return str(asset.get("content_hash") or _digest([
        asset.get("id"), asset.get("size_bytes"), asset.get("drive_modified")]))


def _read(conn, gym):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (_PREFIX + gym,)).fetchone()
    return json.loads(row[0]) if row else {"jobs": {}}


def _write(conn, gym, state):
    conn.execute("INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (_PREFIX + gym, json.dumps(state)))


@contextmanager
def _gym_lock(gym):
    """Held across rendering; process death releases it without a guessed lease TTL."""
    path = db.db_path() + ".auto-reels." + _digest(gym)[:20] + ".lock"
    with open(path, "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _safe_diagnostics(rows):
    # Keep editorial evidence, never local source paths or signed media addresses.
    allowed = ("asset_id", "held", "reason", "hold_reason", "start_ts", "end_ts", "score")
    return [{key: row[key] for key in allowed if key in row}
            for row in (rows or []) if isinstance(row, dict)][: _MAX_CLIPS]


def _pool_hold(reason):
    text = str(reason).lower()
    return ("at least three eligible clips" in text
            or "not enough clear, usable moments" in text
            or "usable segment(s) totaling" in text
            or "automatic captions need verified footage details" in text)


def _recover_used(gym, request_id, job):
    """Prefer durable segment evidence; unknown usage conservatively reserves the batch."""
    allowed = {e["id"] for e in job["entries"]}
    raw = db.kv_get("story_studio_segs:" + request_id, "")
    if raw:
        record = json.loads(raw)
        if record.get("gym_id") == gym and record.get("asset_ids"):
            return [aid for aid in record["asset_ids"] if aid in allowed]
    from .story_studio_store import default_store
    store = default_store()
    if store.available():
        record = store.get_render(request_id, gym_id=gym)
        if record and record.get("gym_id") == gym and record.get("segment_plan"):
            return [seg["asset_id"] for seg in record["segment_plan"]
                    if seg.get("asset_id") in allowed]
    return None



def _sanitize_reason(reason):
    """Fixed-length operator/log text: drop URLs and credential-like tokens."""
    import re as _re
    text = str(reason or "").replace("\n", " ").strip()
    text = _re.sub(r"https?://\S+", "[url]", text, flags=_re.I)
    text = _re.sub(r"Bearer\s+\S+", "[redacted]", text, flags=_re.I)
    text = _re.sub(r"(api[_-]?key|token|secret)[=:]\S+", r"\1=[redacted]", text, flags=_re.I)
    return text[:_SAFE_REASON_MAX]


def _is_exhausted(job):
    if not isinstance(job, dict):
        return False
    if job.get("status") in ("staged", "waiting_pool", "running", "superseded"):
        return False
    return int(job.get("attempts") or 0) >= _MAX_ATTEMPTS


def _job_snapshot(request_id, job):
    return {
        "request_id": request_id,
        "status": "exhausted" if _is_exhausted(job) else job.get("status"),
        "attempts": int(job.get("attempts") or 0),
        "attempts_total": int(job.get("attempts_total") or job.get("attempts") or 0),
        "operator_retries": int(job.get("operator_retries") or 0),
        "reason": _sanitize_reason(job.get("reason")),
        "updated_at": job.get("updated_at"),
        "next_attempt_at": job.get("next_attempt_at"),
        "clip_count": len(job.get("entries") or []),
    }


def _list_exhausted(state):
    return [(rid, job) for rid, job in (state.get("jobs") or {}).items() if _is_exhausted(job)]


def _finish(gym, request_id, status, reason="", now=0, used=None, diagnostics=None, copy_provenance=None):
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read(conn, gym)
        job = state["jobs"][request_id]
        total = int(job.get("attempts_total") or 0)
        if int(job.get("attempts") or 0) > total:
            total = int(job["attempts"])
        job.update(status=status, reason=_sanitize_reason(reason)[:500], updated_at=now,
                   attempts_total=total,
                   next_attempt_at=now + _RETRY_SECONDS * (3 ** max(0, job["attempts"] - 1)))
        if used is not None:
            job["used"] = used
        if diagnostics is not None:
            job["moment_diagnostics"] = _safe_diagnostics(diagnostics)
        if copy_provenance is not None:
            job["copy_provenance"] = copy_provenance
        _write(conn, gym, state)
        conn.commit()


def _claim(gym, candidates, assets, now):
    """Observe/debounce and reserve a new batch OR retry one existing stable batch."""
    entries = []
    for candidate in candidates:
        aid = str(candidate.get("asset_id") or "")
        asset = assets.get(aid) or {}
        if candidate.get("gym_id") != gym or asset.get("gym_id") != gym:
            continue
        if not aid:
            continue
        key = _source_key(asset)
        # Enrichment/brief corrections can reopen an exhausted held batch without
        # allowing previously STAGED footage to become new just because it was tagged.
        context = _digest([asset.get("vision_json"), asset.get("brief")])
        duration = max(0.0, float(candidate.get("end_ts") or 0) - float(candidate.get("start_ts") or 0))
        entries.append({"id": aid, "key": key, "context": context,
                        "seconds": min(_MOMENT_SECONDS, duration),
                        "usage_count": int(asset.get("used_count") or 0)})
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _read(conn, gym)
        jobs = state["jobs"]
        # A resync retains used_count when a Drive ID's bytes change. Allow a new
        # version only when its count is fully explained by our known old render;
        # unknown/extra use by another lane remains excluded.
        def unconsumed(entry):
            if entry["usage_count"] <= 0:
                return True
            prior = [e for j in jobs.values() if j["status"] == "staged"
                     for e in j["entries"] if e["id"] == entry["id"]
                     and e["key"] != entry["key"]
                     and (not j.get("used") or e["id"] in j["used"])]
            return bool(prior) and entry["usage_count"] <= max(e.get("usage_count", 0) + 1 for e in prior)
        entries = [entry for entry in entries if unconsumed(entry)]
        signature = _digest(sorted((e["key"], e["context"], e["seconds"]) for e in entries))
        # Retry the original batch, never rebuild it from whatever happens to be new.
        for request_id, job in jobs.items():
            if job['status'] not in ('staged', 'running', 'uncertain') and _consumed_elsewhere(jobs, request_id, job):
                job.update(status='superseded', reason='Source footage was staged by another job')
            # Repair pre-minimum-check jobs from earlier builds. They could never
            # satisfy the automatic renderer and must not strand drip uploads.
            if job["status"] not in ("staged", "uncertain", "running") and (
                    len(job["entries"]) < _MIN_CLIPS or _pool_hold(job.get("reason"))):
                job["status"] = "waiting_pool"
            if job["status"] in ("staged", "waiting_pool", "superseded") or (job["attempts"] >= _MAX_ATTEMPTS and job["status"] not in ("running", "uncertain")):
                continue
            if now < job.get("next_attempt_at", 0):
                continue
            if job["attempts"] >= _MAX_ATTEMPTS:
                _write(conn, gym, state); conn.commit()
                return request_id, dict(job, reconcile_only=True)
            job.update(status="running", attempts_total=int(job.get('attempts_total', job['attempts']))+1,
                       attempts=job["attempts"] + 1, updated_at=now)
            _write(conn, gym, state); conn.commit()
            return request_id, dict(job)
        if state.get("observed") != signature:
            state.update(observed=signature, settled_since=now)
            _write(conn, gym, state); conn.commit()
            return None
        if now - state.get("settled_since", now) < config.auto_reels_debounce_seconds():
            _write(conn, gym, state); conn.commit()
            return None
        staged = {e["key"] for j in jobs.values() if j["status"] == "staged"
                  for e in j["entries"] if not j.get("used") or e["id"] in j["used"]}
        reserved = set()
        for job in jobs.values():
            rejected = {d.get("asset_id") for d in job.get("moment_diagnostics", []) if d.get("held")}
            for entry in job["entries"]:
                if job["status"] == "waiting_pool" and entry["id"] not in rejected:
                    continue
                if job["status"] == "staged" and job.get("used") and entry["id"] not in job["used"]:
                    continue
                reserved.add((entry["key"], entry["context"]))
        fresh = [e for e in entries if e["key"] not in staged and (e["key"], e["context"]) not in reserved]
        # Duplicate content under two Drive IDs contributes once.
        unique = {e["key"]: e for e in reversed(fresh)}
        fresh = list(reversed(list(unique.values())))[:_MAX_CLIPS]
        if len(fresh) < _MIN_CLIPS or sum(e["seconds"] for e in fresh) < _MIN_SECONDS:
            _write(conn, gym, state); conn.commit()
            return None
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "echo:auto-reels:" + gym + ":" + _digest(fresh)))
        if request_id in jobs:
            # Unchanged insufficient pool: keep waiting, never reset its attempt count.
            _write(conn, gym, state); conn.commit()
            return None
        job = {"status": "running", "attempts": 1, "attempts_total": 1, "entries": fresh,
               "created_at": now, "updated_at": now, "next_attempt_at": 0}
        jobs[request_id] = job
        _write(conn, gym, state); conn.commit()
        return request_id, dict(job)


def run_gym(gym, *, now=None, discover=None, create=None, calendar=None, identity=None):
    """Process at most one <=10-clip batch; injectable I/O is for offline tests only."""
    if not config.auto_reels_enabled() or gym not in config.auto_reels_gyms():
        return {"gym": gym, "status": "off"}
    if not config.story_studio_render_active_for(gym):
        return {"gym": gym, "status": "off", "reason": "Story rendering is not enabled"}
    if not db.kv_is_durable() or db.db_path() == ":memory:":
        return {"gym": gym, "status": "held", "reason": "Persistent Echo worker storage is required"}
    from . import story_candidates, story_studio, gym_identity
    from .portal_calendar_store import SupabaseCalendarStore
    discover = discover or (lambda gym, **kw: story_candidates.discover_candidates(gym, strict=True, **kw))
    create = create or story_studio.create_story
    identity = identity or (lambda key: gym_identity.tokens_for(key, use_cache=False))
    now = now or datetime.now(timezone.utc)
    timestamp = now.timestamp()
    with _gym_lock(gym) as locked:
        if not locked:
            return {"gym": gym, "status": "busy"}
        tokens = identity(gym)
        if not tokens:
            return {"gym": gym, "status": "held", "reason": "Gym identity is missing"}
        candidates, assets = discover(gym, now=now)
        claimed = _claim(gym, candidates, assets, timestamp)
        if not claimed:
            # Do not pretend "waiting" when an exhausted job needs an explicit operator retry.
            try:
                with db.connect() as conn:
                    exhausted = _list_exhausted(_read(conn, gym))
            except Exception:
                exhausted = []
            if exhausted:
                rid, job = exhausted[0]
                snap = _job_snapshot(rid, job)
                return {"gym": gym, "status": "exhausted", "request_id": rid,
                        "reason": snap["reason"] or "Retry limit reached; operator retry required",
                        "attempts": snap["attempts"],
                        "operator_retries": snap["operator_retries"]}
            if not candidates:
                return {"gym": gym, "status": "held", "reason": "No eligible raw footage"}
            return {"gym": gym, "status": "waiting", "reason": "No settled new batch"}
        request_id, job = claimed
        _mirror_status(gym)
        try:
            calendar = calendar or SupabaseCalendarStore()
            # Every retry checks the deterministic row, including ambiguous failures
            # after the database accepted a previous insert but the HTTP reply was lost.
            existing = calendar.get_row(gym, request_id)
            if existing:
                try:
                    used = _recover_used(gym, request_id, job)
                except Exception:
                    used = None
                _finish(gym, request_id, "staged", now=timestamp, used=used)
                return {"gym": gym, "status": "staged", "request_id": request_id, "reconciled": True}
            if job.get("reconcile_only"):
                _finish(gym, request_id, "held", "Retry limit reached; no calendar row found", timestamp)
                return {"gym": gym, "status": "held", "request_id": request_id, "reason": "Retry limit reached"}
            ids = [e["id"] for e in job["entries"]]
            available = {str(c.get("asset_id")) for c in candidates}
            if not all(e["id"] in available and e["id"] in assets
                       and _source_key(assets[e["id"]]) == e["key"]
                       and assets[e["id"]].get("gym_id") == gym for e in job["entries"]):
                _finish(gym, request_id, "held", "Original source is no longer eligible", timestamp)
                return {"gym": gym, "status": "held", "request_id": request_id, "reason": "Original source is no longer eligible"}
            selected = [c for c in candidates if c.get("asset_id") in ids]
            outcome = create({"id": request_id, "gym_id": gym, "account_key": gym,
                              "asset_ids": ids, "auto_reel": True, "identity_tokens": tokens,
                              "requested_by": "echo_auto_reels", "platform": "instagram"},
                             candidates=selected, assets_by_id={aid: assets[aid] for aid in ids},
                             now=now, cal_store=calendar)
            status = "staged" if outcome.get("status") == "staged" else "held"
            if status == "held" and _pool_hold(outcome.get("reason")):
                status = "waiting_pool"
            _finish(gym, request_id, status, outcome.get("reason", ""), timestamp,
                    used=outcome.get("used_clips") if status == "staged" else None,
                    diagnostics=outcome.get("moment_diagnostics"),
                    copy_provenance=outcome.get("copy_provenance"))
            return {"gym": gym, "status": "held" if status == "waiting_pool" else status, "request_id": request_id,
                    "reason": outcome.get("reason", ""), "attempts": job["attempts"]}
        except Exception as exc:
            # Class only: upstream exceptions may contain signed source URLs/tokens.
            _finish(gym, request_id, "uncertain", type(exc).__name__, timestamp)
            return {"gym": gym, "status": "held", "request_id": request_id,
                    "reason": type(exc).__name__, "attempts": job["attempts"]}


def poll(*, now=None):
    if not config.auto_reels_enabled():
        return []
    import threading
    gyms=sorted(config.auto_reels_gyms())
    stopped=threading.Event()
    def heartbeat():
        from .auto_reel_status import publish_many
        while not stopped.wait(60):
            try:
                publish_many({gym:_status_snapshot(gym) for gym in gyms
                              if config.auto_reels_portrait_active_for(gym)})
            except Exception:
                print('[auto-reels] status heartbeat unavailable')
    ticker=threading.Thread(target=heartbeat,name='auto-reels-status',daemon=True)
    ticker.start()
    outcomes=[]
    try:
        for gym in gyms:
            try:
                # Use actual per-job time during long fleet passes.
                outcomes.append(run_gym(gym, now=now if not config._truthy(
                    __import__('os').environ.get('AGENT_AUTO_REELS_ALL_ACCOUNTS','false')) else None))
            except Exception as exc:
                outcomes.append({'gym':gym,'status':'held','reason':type(exc).__name__})
            finally:
                _mirror_status(gym,outcomes[-1] if outcomes else None)
    finally:
        stopped.set()
        ticker.join(timeout=12)
    return outcomes


def _status_snapshot(gym):
    snapshot=gym_status(gym)
    outcome=_MIRROR_OUTCOMES.get(gym) or {}
    if outcome.get('reason') and not outcome.get('request_id'):
        snapshot.update(reason=_sanitize_reason(outcome['reason']),status=outcome.get('status'))
    return snapshot


def _mirror_status(gym, outcome=None):
    if config.auto_reels_portrait_active_for(gym):
        from .auto_reel_status import publish
        _MIRROR_OUTCOMES[gym]=outcome or {}
        snapshot=_status_snapshot(gym)
        if not publish(gym, snapshot):
            print('[auto-reels] portal status mirror unavailable')



def gym_status(gym):
    """Sanitized operational snapshot for one gym (KV jobs + exhausted markers)."""
    if not db.kv_is_durable() or db.db_path() == ":memory:":
        return {"ok": False, "gym": gym, "status": "held",
                "reason": "Persistent Echo worker storage is required", "jobs": []}
    try:
        with db.connect() as conn:
            state = _read(conn, gym)
    except Exception:
        return {"ok": False, "gym": gym, "status": "held",
                "reason": "Job state is unavailable", "jobs": []}
    jobs = [_job_snapshot(rid, job) for rid, job in sorted((state.get("jobs") or {}).items())]
    exhausted = [j for j in jobs if j["status"] == "exhausted"]
    return {
        "ok": True,
        "gym": gym,
        "status": "exhausted" if exhausted else ("idle" if not jobs else "active"),
        "jobs": jobs,
        "exhausted_count": len(exhausted),
    }


def retry_exhausted(gym, request_id=None, *, now=None, note=""):
    """Bounded explicit operator reopen of one exhausted job.

    Keeps the same UUID and reserved entries. Never touches staged footage.
    Resets the automatic attempt counter for another bounded cycle; operator
    retries themselves are capped. No automatic unlimited loop.
    """
    if not db.kv_is_durable() or db.db_path() == ":memory:":
        return {"ok": False, "gym": gym, "status": "held",
                "reason": "Persistent Echo worker storage is required"}
    timestamp = (now.timestamp() if hasattr(now, "timestamp") else float(now or 0))
    if not timestamp:
        timestamp = datetime.now(timezone.utc).timestamp()
    note = _sanitize_reason(note)
    with _gym_lock(gym) as locked:
        if not locked:
            return {"ok": False, "gym": gym, "status": "busy",
                    "reason": "Another worker holds this gym lock"}
        try:
            with db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                state = _read(conn, gym)
                jobs = state.get("jobs") or {}
                if request_id:
                    if request_id not in jobs:
                        conn.rollback()
                        return {"ok": False, "gym": gym, "status": "held",
                                "reason": "No such job for this gym", "request_id": request_id}
                    target_id = request_id
                else:
                    exhausted = _list_exhausted(state)
                    if not exhausted:
                        conn.rollback()
                        return {"ok": False, "gym": gym, "status": "held",
                                "reason": "No exhausted job to retry"}
                    if len(exhausted) > 1:
                        conn.rollback()
                        return {"ok": False, "gym": gym, "status": "held",
                                "reason": "Multiple exhausted jobs; pass request_id"}
                    target_id = exhausted[0][0]
                job = jobs[target_id]
                if _consumed_elsewhere(jobs, target_id, job):
                    conn.rollback()
                    return {"ok": False, "gym": gym, "status": "superseded",
                            "reason": "Source footage was staged by another job", "request_id": target_id}
                if job.get("status") == "staged":
                    conn.rollback()
                    return {"ok": False, "gym": gym, "status": "staged",
                            "reason": "Job already staged; refusing retry",
                            "request_id": target_id}
                if not _is_exhausted(job):
                    conn.rollback()
                    return {"ok": False, "gym": gym, "status": job.get("status"),
                            "reason": "Job is not exhausted; automatic retry still applies",
                            "request_id": target_id}
                op = int(job.get("operator_retries") or 0)
                if op >= _MAX_OPERATOR_RETRIES:
                    conn.rollback()
                    return {"ok": False, "gym": gym, "status": "exhausted",
                            "reason": "Operator retry limit reached",
                            "request_id": target_id,
                            "operator_retries": op}
                prior_attempts = int(job.get("attempts") or 0)
                total = int(job.get("attempts_total") or prior_attempts)
                audit = list(job.get("retry_audit") or [])
                audit.append({
                    "at": timestamp,
                    "note": note,
                    "prior_attempts": prior_attempts,
                    "prior_reason": _sanitize_reason(job.get("reason")),
                    "prior_status": job.get("status"),
                })
                job.update(
                    status="held",
                    attempts=0,
                    attempts_total=total,
                    operator_retries=op + 1,
                    next_attempt_at=0,
                    updated_at=timestamp,
                    reason="Operator retry queued",
                    retry_audit=audit[-20:],
                    reconcile_only=False,
                )
                job.pop("reconcile_only", None)
                _write(conn, gym, state)
                conn.commit()
        except Exception:
            return {"ok": False, "gym": gym, "status": "held",
                    "reason": "Job state is unavailable"}
    return {"ok": True, "gym": gym, "status": "queued_retry",
            "request_id": target_id, "operator_retries": op + 1,
            "attempts_total": total}


def report_outcome(outcome):
    """Sanitized, transition-deduped operational log line. No Slack/client sends."""
    if not isinstance(outcome, dict):
        return False
    status = str(outcome.get("status") or "")
    quiet = status in ("off", "waiting", "busy")
    gym = str(outcome.get("gym") or "unknown")
    request_id = str(outcome.get("request_id") or "preclaim")
    reason = _sanitize_reason(outcome.get("reason"))
    digest = _digest([status, reason, outcome.get("attempts"),
                      outcome.get("operator_retries")])[:16]
    key = _PREFIX + "log:" + gym + ":" + request_id
    def process_transition():
        if _LOG_TRANSITIONS.get(key) == digest:
            return False
        if len(_LOG_TRANSITIONS) >= 1000:
            _LOG_TRANSITIONS.clear()
        _LOG_TRANSITIONS[key] = digest
        return True
    try:
        if not db.kv_is_durable() or db.db_path() == ":memory:":
            changed = process_transition()
        else:
            changed = db.kv_get(key) != digest
            if changed:
                db.kv_set(key, digest)
    except Exception:
        # Storage faults are themselves useful operator outcomes. Fall back to
        # bounded process-local deduplication, never drop them silently.
        changed = process_transition()
    if quiet or not changed:
        return False
    suffix = f" ({request_id})" if request_id != "preclaim" else ""
    detail = f": {reason}" if reason else ""
    print(f"[auto-reels] {status} for {gym}{suffix}{detail}")
    return True


def _cli(argv=None):
    """python -m agent.auto_reels status|retry ... (operator surface; no publish)."""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python -m agent.auto_reels status [gym]")
        print("       python -m agent.auto_reels retry <gym> [request_id] [--note TEXT]")
        return 2
    cmd = argv[0]
    if cmd == "status":
        gyms = [argv[1]] if len(argv) > 1 else sorted(config.auto_reels_gyms() or [])
        if not gyms:
            print("No pilot gyms configured (AGENT_AUTO_REELS_GYMS).")
            return 1
        for gym in gyms:
            snap = gym_status(gym)
            print(json.dumps(snap, sort_keys=True))
        return 0 if all(gym_status(g).get("ok") for g in gyms) else 1
    if cmd == "retry":
        if len(argv) < 2:
            print("retry requires <gym>")
            return 2
        gym = argv[1]
        request_id = None
        note = ""
        rest = argv[2:]
        if rest and rest[0] != "--note":
            request_id = rest[0]
            rest = rest[1:]
        if rest[:1] == ["--note"]:
            note = " ".join(rest[1:])
        result = retry_exhausted(gym, request_id, note=note)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") else 1
    print("unknown command:", cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
