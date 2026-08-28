"""
account_key_reconcile.py — the onboard reconciler for the account_key collision bug.

THE BUG (Bird Dog CrossFit / Bolton Club, live): account_key was set ad hoc with no
canonical derivation, so gyms with near-identical names COLLIDED onto one key, gyms got
DUPLICATE keys, and gyms that bought the social product were STRANDED with no reconciled
key at all. This sweep, mirroring zernio_reverify.py's style, finds those gyms, computes
the CANONICAL key (account_key.canonical_account_key), and reports a PLAN of what it would
change. Dry-run by DEFAULT; only --apply writes.

Hard rails (never crossed, even with --apply):
  * NEVER merges two real gyms' data — a collided key is DISAMBIGUATED (each gym keeps its
    own row under its own new key), never collapsed onto one.
  * NEVER deletes a populated Zernio profile — the reconciler only proposes an account_key
    string; profile binds go through the guarded _persist_profile_id, never a delete.
  * IDEMPOTENT — a gym already on its canonical key is OK/no-op; re-running changes nothing.
  * TENANT-SAFE — every plan row asserts gym_id, and the canonical key folds the STABLE
    gym_id in, so a rename never re-mints and two tenants never share a key.

Behind AGENT_ACCOUNT_KEY_RECONCILE (default OFF): even --apply is a no-op with the flag
dark, so the writer ships safe. All I/O is injectable (reader / writer) so tests run offline.

    python -m agent account-key-reconcile --gym <gym_id>     # dry-run one gym
    python -m agent account-key-reconcile --all              # dry-run the fleet
    python -m agent account-key-reconcile --all --apply      # write (flag must be armed)
"""

from . import config
from .account_key import canonical_account_key


# ---- plan classification ----------------------------------------------------------

# A gym record is a dict: {gym_id, name, account_key (current, may be ''), has_social_product}.
# The reader yields these; the classifier + canonical fn need nothing else.

def _social_gyms(records):
    """Only gyms that actually bought the social product. Never fabricate the flag: a record
    with has_social_product falsy is excluded (we do not reconcile a gym that never bought it)."""
    return [r for r in records if r.get("has_social_product")]


def build_plan(records):
    """Pure planner. `records` is the full gym list (dicts with gym_id, name, account_key,
    has_social_product). Returns a list of per-gym plan rows, each:
        {gym_id, name, current, canonical, status, change}
    status in {OK, MISSING, COLLIDED, MISMATCH, ERROR}; change True iff current != canonical.

    Collision detection is done over the SOCIAL gyms only, on the CURRENT keys, so a key held
    by two different gym_ids marks BOTH as COLLIDED and each is disambiguated independently."""
    social = _social_gyms(records)

    # Which current keys are shared by more than one distinct gym_id -> a real collision.
    key_owners = {}
    for r in social:
        cur = (str(r.get("account_key") or "")).strip()
        gid = (str(r.get("gym_id") or "")).strip()
        if cur and gid:
            key_owners.setdefault(cur, set()).add(gid)
    collided_keys = {k for k, owners in key_owners.items() if len(owners) > 1}

    # The set of keys already TAKEN, for the disambiguator walk. We seed it with every
    # NON-collided current key (those are staying put) so a disambiguation never lands on a
    # key another gym legitimately holds. Collided keys are NOT reserved (they are being
    # rewritten away). Canonical keys are reserved as we assign them, so two gyms colliding
    # after canonicalisation still separate.
    taken = set()
    for r in social:
        cur = (str(r.get("account_key") or "")).strip()
        if cur and cur not in collided_keys:
            taken.add(cur)

    plan = []
    for r in social:
        gid = (str(r.get("gym_id") or "")).strip()
        name = (str(r.get("name") or "")).strip()
        cur = (str(r.get("account_key") or "")).strip()

        # A collided current key must be recomputed even if it "looks issued" — that is the
        # whole point. A non-collided current key is treated as ISSUED (idempotent: keep it,
        # even across a later rename), so we never needlessly churn a live key.
        issued = cur if (cur and cur not in collided_keys) else None

        row = {"gym_id": gid, "name": name, "current": cur or "(none)"}
        try:
            canonical = canonical_account_key(
                gid, name,
                is_taken=lambda k, _self=None: k in taken,
                issued_key=issued,
            )
        except (ValueError, RuntimeError) as exc:
            row.update({"canonical": "(error)", "status": "ERROR", "change": False,
                        "error": f"{type(exc).__name__}: {exc}"})
            plan.append(row)
            continue

        # Reserve the assigned key so the next gym's disambiguation avoids it.
        taken.add(canonical)

        if not cur:
            status = "MISSING"
        elif cur in collided_keys:
            status = "COLLIDED"
        elif cur == canonical:
            status = "OK"
        else:
            status = "MISMATCH"
        row.update({"canonical": canonical, "status": status,
                    "change": canonical != cur})
        plan.append(row)
    return plan


# ---- live reader (injectable; default Supabase REST) ------------------------------

def _default_reader():
    """Live: the fleet of gyms that bought the social product, from the portal planes.

    Reads the portal `gyms` table (id, slug, name) and left-joins each gym's current
    echo_account_key from `echo_intake_tokens` (gym_id -> echo_account_key), and its social
    entitlement. has_social_product is derived from the token row existing for a social
    product (a gym in echo_intake_tokens bought social); a gym with no token row is not
    reconciled here. Reads creds lazily; NEVER logs the key. No creds -> []. Best effort:
    a read failure returns [] so the sweep reports 'no data' rather than guessing."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return []
    import requests  # lazy, matches the repo pattern
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}

    def _get(path, params):
        try:
            r = requests.get(f"{url}/rest/v1/{path}", params=params, headers=headers, timeout=30)
            if r.status_code >= 400:
                return []
            return r.json() or []
        except Exception:  # noqa: BLE001 - a live-read failure is 'no data', never a crash
            return []

    # token rows carry the social entitlement + the current echo_account_key, keyed by gym_id.
    tokens = _get("echo_intake_tokens", {"select": "gym_id,echo_account_key"})
    token_by_gym = {}
    for t in tokens:
        gid = (str(t.get("gym_id") or "")).strip()
        if gid and gid not in token_by_gym:
            token_by_gym[gid] = (str(t.get("echo_account_key") or "")).strip()

    gyms = _get("gyms", {"select": "id,slug,name"})
    out = []
    for g in gyms:
        gid = (str(g.get("id") or "")).strip()
        if not gid:
            continue
        has_social = gid in token_by_gym
        out.append({
            "gym_id": gid,
            "name": (str(g.get("name") or "")).strip() or (str(g.get("slug") or "")).strip(),
            "account_key": token_by_gym.get(gid, ""),
            "has_social_product": has_social,
        })
    return out


def _default_writer(plan_row):
    """Live writer for --apply. Behind AGENT_ACCOUNT_KEY_RECONCILE (default OFF): a no-op
    return when the flag is dark, so --apply is safe even if run by accident. When armed,
    it UPDATES echo_intake_tokens.echo_account_key for this gym_id to the canonical key.
    It NEVER deletes anything and NEVER touches another gym's row (filtered by gym_id).
    Returns (ok, detail). Reads creds lazily; NEVER logs the key."""
    if not config.account_key_reconcile_enabled():
        return False, "reconcile writer disabled (AGENT_ACCOUNT_KEY_RECONCILE off)"
    gid = (str(plan_row.get("gym_id") or "")).strip()
    new_key = (str(plan_row.get("canonical") or "")).strip()
    if not gid or not new_key or new_key in ("(error)", "(none)"):
        return False, "nothing to write"
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return False, "supabase creds absent"
    import requests  # lazy
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    try:
        r = requests.patch(f"{url}/rest/v1/echo_intake_tokens",
                           params={"gym_id": f"eq.{gid}"},
                           headers=headers, json={"echo_account_key": new_key}, timeout=30)
        if r.status_code >= 400:
            return False, f"write failed: {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"write failed: {type(exc).__name__}"
    return True, "updated"


# ---- sweep ------------------------------------------------------------------------

def reconcile(gym_id=None, apply=False, *, reader=None, writer=None, logger=None):
    """Reconcile ONE gym (gym_id set) or the whole fleet (gym_id None). Dry-run unless
    apply=True. Returns a summary {ok, apply, plan:[...], applied:[...]}. Never raises out.

    apply=True writes ONLY the rows whose change is True and status is not ERROR, each via the
    writer (itself flag-gated + gym_id-scoped). Idempotent: an already-canonical fleet plans
    all-OK and writes nothing."""
    log = logger or (lambda m: print(f"[account-key-reconcile] {m}"))
    reader = reader or _default_reader
    records = reader() or []
    if gym_id:
        gid = str(gym_id).strip()
        records = [r for r in records if (str(r.get("gym_id") or "")).strip() == gid]
        if not records:
            log(f"no social-product gym found for gym_id={gid}")
            return {"ok": True, "apply": bool(apply), "plan": [], "applied": []}

    plan = build_plan(records)
    applied = []
    if apply:
        writer = writer or _default_writer
        for row in plan:
            if row.get("change") and row.get("status") != "ERROR":
                ok, detail = writer(row)
                applied.append({"gym_id": row["gym_id"], "canonical": row["canonical"],
                                "ok": ok, "detail": detail})
                log(f"apply {row['gym_id']} -> {row['canonical']}: ok={ok} ({detail})")
    return {"ok": True, "apply": bool(apply), "plan": plan, "applied": applied}


def print_plan(summary, printer=print):
    """Print the per-gym plan table (mirrors zernio_reverify's readable output)."""
    plan = summary.get("plan") or []
    if not plan:
        printer("account-key-reconcile: no social-product gyms to reconcile (or no data).")
        return
    mode = "APPLY" if summary.get("apply") else "DRY-RUN"
    printer(f"account-key-reconcile [{mode}] — {len(plan)} social gym(s):")
    printer(f"  {'STATUS':9} {'GYM_ID':16} {'CURRENT':22} {'->':2} {'CANONICAL':22}  NAME")
    changed = 0
    for row in plan:
        arrow = "->" if row.get("change") else "=="
        if row.get("change"):
            changed += 1
        printer(f"  {row['status']:9} {row['gym_id'][:16]:16} {str(row['current'])[:22]:22} "
                f"{arrow:2} {str(row['canonical'])[:22]:22}  {row['name']}")
        if row.get("error"):
            printer(f"    ! {row['error']}")
    printer(f"  ({changed} would change, {len(plan) - changed} already canonical)")
    if summary.get("apply"):
        applied = summary.get("applied") or []
        oks = sum(1 for a in applied if a.get("ok"))
        printer(f"  applied: {oks}/{len(applied)} write(s) ok")


def cli(argv):
    """CLI entry: --gym <id> | --all [--apply]. Dry-run unless --apply."""
    gym_id = None
    do_all = False
    apply = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--gym", "--gym-id", "--id") and i + 1 < len(argv):
            gym_id = argv[i + 1]; i += 2; continue
        if a == "--all":
            do_all = True; i += 1; continue
        if a == "--apply":
            apply = True; i += 1; continue
        i += 1
    if not gym_id and not do_all:
        print("usage: python -m agent account-key-reconcile (--gym <gym_id> | --all) [--apply]")
        return
    if apply and not config.account_key_reconcile_enabled():
        print("account-key-reconcile: --apply requested but AGENT_ACCOUNT_KEY_RECONCILE is OFF; "
              "running DRY-RUN. Arm the flag by hand to write.")
        apply = False
    summary = reconcile(gym_id=gym_id, apply=apply)
    print_plan(summary)
