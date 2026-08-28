"""
account_key_doctor.py — the early-warning guard that makes the account_key stranding class
IMPOSSIBLE to ship silently.

THE STRANDING CLASS (topfuel / district_h, live 2026-08-28): a gym buys the social product,
but its account-registry BASE (topfuel) does not resolve to exactly one non-archived gyms
row (gyms.slug is top-fuel), so every base->gym lookup silently no-ops and the owner sits
"not connected" for weeks until they complain. The resolver fix (resolve_gym_uuid) bridges
base != slug, but a NEW base that normalization still can't bridge, or a base that resolves
to TWO rows (ambiguous) or ONLY an archived row, would strand again — invisibly.

THIS DOCTOR surfaces that LOUDLY at onboarding time, not via a client complaint. For every
social-product gym base it asserts:
  * the base resolves via resolve_gym_uuid to EXACTLY ONE non-archived gyms row, and
  * (where a Zernio profile is expected) that gym resolves a stored zernio_profile_id.
Anything that does not cleanly resolve is a STRANDING RISK, classified:
  OK             — resolves to one gym uuid (and a profile if expected).
  NO_PROFILE     — resolves to a gym uuid but no Zernio profile is bound yet (a soft warning:
                   expected right after connect, a risk if it lingers).
  UNRESOLVED     — resolve_gym_uuid returns None: no clean base->gym map (the topfuel bug).
  AMBIGUOUS      — the base normalises onto more than one non-archived gym (refuse to guess).
  ARCHIVED_ONLY  — the only gym the base could match is archived / a -dup ghost.

Two surfaces, both RAILS-SAFE:
  (a) CLI  `python -m agent account-key-doctor`  — READ-ONLY report. Never writes, never
      provisions, never deletes/merges a gym. Reads only.
  (b) ALERT — one throttled ops alert per unresolved social gym (reuses ops_alerts, gated by
      config.account_key_doctor_alerts_enabled(), default OFF; throttled per base via kv so a
      cron re-run never spams). A clean gym never alerts.

Mirrors zernio_reverify.py / account_key_reconcile.py: injectable readers (bases /
resolve_uuid / profile) so the whole doctor runs offline in tests. Never raises out.

    python -m agent account-key-doctor            # report every social-product gym base
    python -m agent account-key-doctor --alert     # report + fire throttled alerts (flag-gated)
    python -m agent account-key-doctor --base topfuel   # one base
"""

from . import config


# Risk verdicts that mean a gym is (or will be) STRANDED and must surface loudly.
STRANDING_VERDICTS = ("UNRESOLVED", "AMBIGUOUS", "ARCHIVED_ONLY")

# Throttle window: re-alert a still-stranded base at most once per this window, so a doctor
# cron (or repeated onboards) never spams the channel.
ALERT_THROTTLE_SECONDS = 24 * 3600


# ---- default injectable readers (live; all read-only) ------------------------------

# Internal / non-client bases that legitimately have no portal gyms row or Zernio profile
# (a personal test account, the LASSO house account which runs its own Meta-direct lane).
# They are NOT client social-product gyms, so a missing gym/profile for them is expected,
# not a stranding risk — excluding them keeps the nightly alert to REAL client stranding.
_INTERNAL_BASES = {"blake_personal", "lasso"}


def _is_internal_base(base):
    b = (str(base) if base is not None else "").strip().lower()
    return b in _INTERNAL_BASES or b.endswith("_personal")


def _default_bases():
    """The social-product CLIENT gym bases from the account registry (internal/personal
    bases excluded — see _is_internal_base). Mirrors zernio_reverify.reverify_bases' source.
    A failure yields [] so the doctor reports 'no data' rather than guessing."""
    try:
        from .calendar_autopublish import client_gym_bases
        return [b for b in (client_gym_bases() or []) if not _is_internal_base(b)]
    except Exception:  # noqa: BLE001
        return []


def _default_store():
    """A read-only shared-plane store, or None when creds are absent. Reused for both
    resolve_gym_uuid and the profile lookup so one creds check covers both."""
    try:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
        return store if store.available() else None
    except Exception:  # noqa: BLE001
        return None


def _classify_base(base, store, *, expect_profile=True):
    """Read-only verdict for ONE base. Returns a row dict:
        {base, gym_uuid, profile_id, verdict}
    verdict in {OK, NO_PROFILE, UNRESOLVED, AMBIGUOUS, ARCHIVED_ONLY}.

    Uses resolve_gym_uuid (the ONE canonical base->uuid resolver, which already refuses an
    ambiguous match and skips archived / -dup rows by returning None). To DISTINGUISH a true
    no-match (UNRESOLVED) from an ambiguous-or-archived-only miss (so the operator knows which
    kind of stranding it is), we do a second read-only probe of the raw gyms list. That probe
    NEVER binds or writes; it only classifies the None."""
    row = {"base": base, "gym_uuid": None, "profile_id": None, "verdict": "UNRESOLVED"}
    if store is None:
        return row

    uuid = None
    try:
        uuid = store.resolve_gym_uuid(base)
    except Exception:  # noqa: BLE001 - an honest miss, not a crash
        uuid = None

    if uuid:
        row["gym_uuid"] = str(uuid)
        pid = None
        if expect_profile:
            try:
                pid = store.gym_zernio_profile_id(base)
            except Exception:  # noqa: BLE001
                pid = None
        row["profile_id"] = str(pid) if pid else None
        row["verdict"] = "OK" if (pid or not expect_profile) else "NO_PROFILE"
        return row

    # uuid is None -> classify WHY (read-only probe of the gyms list, same normalisation as
    # resolve_gym_uuid, so the operator sees AMBIGUOUS vs ARCHIVED_ONLY vs true UNRESOLVED).
    row["verdict"] = _classify_unresolved(base, store)
    return row


def _classify_unresolved(base, store):
    """Read-only refinement of an UNRESOLVED base: AMBIGUOUS (normalises onto >1 live gym),
    ARCHIVED_ONLY (the only candidates are archived / -dup), else UNRESOLVED. Best effort; any
    failure stays UNRESOLVED (the honest, loud default)."""
    def _norm(s):
        return "".join(c for c in (s or "").lower() if c.isalnum())

    def _is_archived(r):
        slug = (r.get("slug") or "").lower()
        name = (r.get("name") or "").lower()
        return ("archived" in slug or "-dup" in slug or "archived" in name
                or "do not use" in name)

    try:
        rows = store.list_gyms_min()
    except Exception:  # noqa: BLE001
        return "UNRESOLVED"
    if not rows:
        return "UNRESOLVED"

    target = _norm(base)
    if not target:
        return "UNRESOLVED"

    def _matches(r):
        ns = _norm(r.get("slug"))
        return ns == target or ns.startswith(target) or (ns and target.startswith(ns))

    candidates = [r for r in rows if _matches(r)]
    if not candidates:
        return "UNRESOLVED"
    live = [r for r in candidates if not _is_archived(r)]
    if not live:
        return "ARCHIVED_ONLY"
    if len(live) > 1:
        return "AMBIGUOUS"
    # exactly one live candidate matched but resolve_gym_uuid still returned None: treat as
    # UNRESOLVED (resolver is the source of truth; we never override it upward to OK).
    return "UNRESOLVED"


# ---- the sweep --------------------------------------------------------------------

def diagnose(base=None, *, bases=None, store=None, expect_profile=True):
    """Read-only diagnosis of the social-product gym bases (or ONE base). Returns a summary:
        {ok, count, rows:[...], stranded:[...]}
    rows is every base's classification; stranded is the subset whose verdict is a
    STRANDING_VERDICT. Never writes, never alerts (that is fire_alerts' job). Never raises."""
    if store is None:
        store = _default_store()
    if base:
        base_list = [base]
    else:
        base_list = list(bases) if bases is not None else _default_bases()

    rows = []
    for b in base_list:
        b = (str(b) if b is not None else "").strip()
        if not b:
            continue
        rows.append(_classify_base(b, store, expect_profile=expect_profile))
    stranded = [r for r in rows if r["verdict"] in STRANDING_VERDICTS]
    # store_available distinguishes "diagnosed and found stranded" from "couldn't diagnose
    # (no creds)". The CLI report still honestly shows UNRESOLVED with no store, but the ALERT
    # never fires on a creds-less host — a missing store is not a stranding SIGNAL.
    return {"ok": True, "count": len(rows), "rows": rows, "stranded": stranded,
            "store_available": store is not None}


def fire_alerts(summary, *, alert=None, kv=None, now=None, force=False):
    """Fire ONE throttled ops alert per STRANDED social gym. Gated by
    config.account_key_doctor_alerts_enabled() (default OFF) unless force=True. Throttled per
    base via kv (ALERT_THROTTLE_SECONDS) so a cron re-run never spams. A clean summary fires
    nothing. Returns the list of bases alerted this pass. Never raises out.

    All I/O injectable: `alert(message)` (default ops_alerts.alert), `kv` (default the db kv),
    `now` (default time.time)."""
    summary = summary or {}
    stranded = summary.get("stranded") or []
    if not stranded:
        return []
    # A creds-less host reports everything UNRESOLVED — that is "couldn't diagnose", not a
    # stranding signal, so never alert on it. (Tests that inject a live store leave
    # store_available unset -> treated as available; production passes the real flag.)
    if summary.get("store_available") is False:
        return []
    if not force and not config.account_key_doctor_alerts_enabled():
        return []

    import time as _time
    now = now if now is not None else _time.time()

    if alert is None:
        from . import ops_alerts
        # force=True so the doctor's own flag governs firing, not the general ops-alerts flag
        # (mirrors the token watchdog, which carries its own gate).
        alert = lambda m: ops_alerts.alert(m, force=True)  # noqa: E731
    if kv is None:
        from . import db as _db
        class _KV:
            def get(self, k, default=""):
                return _db.kv_get(k, default)
            def set(self, k, v):
                _db.kv_set(k, v)
        kv = _KV()

    alerted = []
    for r in stranded:
        base = r["base"]
        verdict = r["verdict"]
        throttle_key = f"acctkey_doctor_alert_{base}"
        try:
            last = float(kv.get(throttle_key, "") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last and (now - last) < ALERT_THROTTLE_SECONDS:
            continue  # throttled: already alerted this base recently
        msg = (f"account-key doctor: social-product gym base {base!r} is a STRANDING RISK "
               f"({verdict}) — its base does not cleanly resolve to one live gym. Fix before "
               f"the owner reports 'not connected'. Run: python -m agent account-key-doctor "
               f"--base {base}")
        try:
            alert(msg)
        except Exception:  # noqa: BLE001 - an alert must never take the sweep down
            pass
        try:
            kv.set(throttle_key, str(int(now)))
        except Exception:  # noqa: BLE001
            pass
        alerted.append(base)
    return alerted


# ---- printable report -------------------------------------------------------------

def print_report(summary, printer=print):
    """Print the per-base report table (mirrors the reconcile / reverify readable output)."""
    rows = summary.get("rows") or []
    if not rows:
        printer("account-key-doctor: no social-product gym bases to check (or no data).")
        return
    printer(f"account-key-doctor — {len(rows)} social-product gym base(s):")
    printer(f"  {'VERDICT':13} {'BASE':20} {'GYM_UUID':38} PROFILE")
    for r in rows:
        printer(f"  {r['verdict']:13} {str(r['base'])[:20]:20} "
                f"{str(r['gym_uuid'] or '(none)')[:38]:38} {r['profile_id'] or '(none)'}")
    n_str = len(summary.get("stranded") or [])
    if n_str:
        printer(f"  ! {n_str} STRANDING RISK(s): "
                + ", ".join(f"{r['base']}({r['verdict']})" for r in summary["stranded"]))
    else:
        printer("  all social-product gym bases resolve cleanly.")


# ---- CLI --------------------------------------------------------------------------

def cli(argv):
    """CLI entry: [--base <base>] [--alert]. Read-only report always; --alert also fires
    throttled ops alerts (flag-gated)."""
    base = None
    do_alert = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--base", "--account", "--gym") and i + 1 < len(argv):
            base = argv[i + 1]; i += 2; continue
        if a == "--alert":
            do_alert = True; i += 1; continue
        i += 1
    summary = diagnose(base=base)
    print_report(summary)
    if do_alert:
        if not config.account_key_doctor_alerts_enabled():
            print("account-key-doctor: --alert requested but AGENT_ACCOUNT_KEY_DOCTOR_ALERTS "
                  "is OFF; no alert fired (report above stands). Arm the flag by hand to alert.")
        else:
            fired = fire_alerts(summary)
            print(f"account-key-doctor: fired {len(fired)} alert(s)"
                  + (f" for {', '.join(fired)}" if fired else " (all clean / throttled)"))
