"""
connection_watch.py — alert staff when a gym is PARTIALLY connected.

Hill Country Movement, 2026-08-26: the owner completed the Instagram OAuth from the
self-serve connect page. Meta's business login mentions Facebook Pages permissions
during that flow, so she reasonably believed Facebook (and Google Business) were
connected too — but each platform is its own separate OAuth, and nothing on our side
noticed the gym sat Instagram-only for days. The CLIENT had to report it in Slack.

This watch closes that gap: each sweep reads every client gym's Zernio profile and,
when a gym has connected SOME of the three platforms (instagram, facebook,
googlebusiness) but not all, and that exact missing set has persisted past the grace
window, fires ONE deduped ops alert naming the connected + missing platforms and the
command that mints the gym's connect link.

Design:
- Behind AGENT_CONNECTION_WATCH (config.connection_watch_enabled, default OFF).
- READ-ONLY against Zernio (find_profile_id / list_accounts); writes only kv stamps.
- Grace window (AGENT_CONNECTION_WATCH_GRACE_HOURS, default 24h) starts when a given
  missing set is FIRST SEEN, so a gym mid-onboarding is never nagged while the owner
  is still clicking. A missing set that changes (facebook connects, google still
  missing) starts its own grace cycle and can alert again for the new state.
- Deduped per (gym, missing set) via kv — one alert, never a storm. Full connection
  clears the gym's stamps so a later disconnect+partial re-alerts.
- ZERO connected platforms is NOT partial (the gym simply has not started; the
  onboarding flow owns that nudge) — skipped.
- Paced: at most one sweep per AGENT_CONNECTION_WATCH_EVERY_HOURS (default 6) via a
  kv stamp, so the runner can call it every loop.
- Best-effort per gym: one gym's Zernio error never blocks the rest, and never raises.
"""

import os
from datetime import datetime, timedelta, timezone

from . import config

PLATFORMS = ("instagram", "facebook", "googlebusiness")

_SEEN_KEY = "conn_watch_seen_{base}_{suffix}"
_ALERTED_KEY = "conn_watch_alerted_{base}_{suffix}"
_PACE_KEY = "conn_watch_last_sweep"


def _grace_hours():
    try:
        return float(os.environ.get("AGENT_CONNECTION_WATCH_GRACE_HOURS", "24"))
    except ValueError:
        return 24.0


def _sweep_every_hours():
    try:
        return float(os.environ.get("AGENT_CONNECTION_WATCH_EVERY_HOURS", "6"))
    except ValueError:
        return 6.0


def _client_bases(clients=None):
    from .client_media_sync import _client_bases as _cmb
    return _cmb(clients)


def _profile_id(base, zernio, db_mod):
    """The gym's Zernio profile id: the stored gyms.zernio_profile_id when present
    (zernio_profile_link populates it, incl. display-name matches for UUID-keyed
    gyms), else a live name lookup. '' when neither resolves."""
    try:
        row = db_mod.gym_get(base) or {}
        pid = (row.get("zernio_profile_id") or "").strip()
        if pid:
            return pid
    except Exception:  # noqa: BLE001 - fall through to the live lookup
        pass
    try:
        return (zernio.find_profile_id(base) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _connected_platforms(accounts):
    """The set of our three platforms that have ANY account row under the profile.
    A present row counts as connected (the 94f29e7 anti-flap ruling); expiry is the
    reconnect lane's concern, not this watch's."""
    out = set()
    for a in accounts or []:
        p = str(a.get("platform") or "").strip().lower()
        if p in PLATFORMS:
            out.add(p)
    return out


def watch_connections(zernio=None, db_mod=None, clients=None, now=None,
                      alert=None, logger=None, force=False):
    """One sweep. Returns {ok, checked, partial, alerted, skipped, results}.

    zernio  injectable client (find_profile_id + list_accounts). Default: live.
    db_mod  injectable db module (kv_get / kv_set / gym_get). Default: agent.db.
    now     injectable aware datetime (default: utcnow).
    force   True skips the pace stamp (CLI / tests / a deliberate immediate sweep).
    """
    log = logger or (lambda m: print(f"[connection-watch] {m}"))
    if not config.connection_watch_enabled():
        return {"ok": False, "reason": "AGENT_CONNECTION_WATCH off", "alerted": 0}

    if db_mod is None:
        from . import db as db_mod  # noqa: PLW0127 - injectable default
    if alert is None:
        from .ops_alerts import alert as _alert
        alert = _alert
    now = now or datetime.now(timezone.utc)

    # Pace: one sweep per window; the runner calls every loop.
    if not force:
        try:
            last = db_mod.kv_get(_PACE_KEY)
            if last:
                last_dt = datetime.fromisoformat(last)
                if now - last_dt < timedelta(hours=_sweep_every_hours()):
                    return {"ok": True, "reason": "paced", "alerted": 0,
                            "checked": 0, "partial": 0, "skipped": 0, "results": []}
        except Exception:  # noqa: BLE001 - an unreadable stamp never blocks the sweep
            pass
    try:
        db_mod.kv_set(_PACE_KEY, now.isoformat())
    except Exception:  # noqa: BLE001
        pass

    if zernio is None:
        from .zernio import ZernioClient
        zernio = ZernioClient()

    grace = timedelta(hours=_grace_hours())
    checked = partial = alerted = skipped = 0
    results = []

    for base in _client_bases(clients):
        try:
            pid = _profile_id(base, zernio, db_mod)
            if not pid:
                skipped += 1
                results.append({"gym": base, "status": "no_profile"})
                continue
            data = zernio.list_accounts(pid)
            accounts = data.get("accounts") if isinstance(data, dict) else data
            present = _connected_platforms(accounts)
            checked += 1

            if not present:
                # Not started: onboarding owns that nudge, not this watch.
                results.append({"gym": base, "status": "none_connected"})
                continue

            missing = sorted(set(PLATFORMS) - present)
            if not missing:
                # Fully connected: clear stamps so a later partial re-alerts.
                for p in _all_suffixes():
                    try:
                        db_mod.kv_set(_SEEN_KEY.format(base=base, suffix=p), "")
                        db_mod.kv_set(_ALERTED_KEY.format(base=base, suffix=p), "")
                    except Exception:  # noqa: BLE001
                        pass
                results.append({"gym": base, "status": "fully_connected"})
                continue

            partial += 1
            suffix = "-".join(missing)
            seen_key = _SEEN_KEY.format(base=base, suffix=suffix)
            alerted_key = _ALERTED_KEY.format(base=base, suffix=suffix)

            first_seen = None
            try:
                raw = db_mod.kv_get(seen_key)
                first_seen = datetime.fromisoformat(raw) if raw else None
            except Exception:  # noqa: BLE001
                first_seen = None

            if first_seen is None:
                db_mod.kv_set(seen_key, now.isoformat())
                results.append({"gym": base, "status": "partial_grace",
                                "missing": missing})
                continue

            if now - first_seen < grace:
                results.append({"gym": base, "status": "partial_grace",
                                "missing": missing})
                continue

            if db_mod.kv_get(alerted_key):
                results.append({"gym": base, "status": "partial_already_alerted",
                                "missing": missing})
                continue

            db_mod.kv_set(alerted_key, "1")
            alerted += 1
            connected_list = ", ".join(sorted(present))
            missing_list = ", ".join(missing)
            hours = int((now - first_seen).total_seconds() // 3600)
            alert(
                f"gym {base} is PARTIALLY CONNECTED: {connected_list} linked, but "
                f"{missing_list} never completed OAuth ({hours}h and counting). The "
                "owner likely believes they finished (Meta's Instagram login mentions "
                "Facebook Pages, so one approval FEELS like all of them). Each platform "
                "is its own approval. Send them their connect page: "
                f"python3 -m agent intake-link --account {base}  (the connect line)."
            )
            results.append({"gym": base, "status": "partial_alerted",
                            "missing": missing})
        except Exception as exc:  # noqa: BLE001 - one gym never blocks the sweep
            skipped += 1
            log(f"{base}: watch failed: {type(exc).__name__}")
            results.append({"gym": base, "status": "error"})

    return {"ok": True, "checked": checked, "partial": partial,
            "alerted": alerted, "skipped": skipped, "results": results}


def _all_suffixes():
    """Every possible missing-set suffix (7 non-empty subsets of the 3 platforms),
    for clearing a fully-connected gym's stamps."""
    from itertools import combinations
    out = []
    for n in (1, 2, 3):
        for combo in combinations(sorted(PLATFORMS), n):
            out.append("-".join(combo))
    return out
