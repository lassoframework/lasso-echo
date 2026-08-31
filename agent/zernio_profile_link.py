"""
zernio_profile_link.py — backfill gyms.zernio_profile_id (the PUBLISHER's profile id).

Pierce Fitness, 2026-08-24: the gym's Zernio profile existed and was fully connected
(Instagram + Facebook + Google), yet EVERY approved post failed to publish with
"no Zernio profile id stored; the gym must connect first". Root cause: nothing wrote
gyms.zernio_profile_id for a client gym. The publisher's _default_profile_resolver reads
that exact column (via db.gym_get); only the provisioning path ever wrote it, and Pierce
was onboarded without it. gbp_conn_sync writes zernio_profile_id ONLY into the GBP
connections table, which the IG/FB publisher never reads. So a fully-connected gym could
sit forever with an empty profile id and silently never publish.

This closes that gap: for each client base whose gyms.zernio_profile_id is empty, look up
the gym's Zernio profile by name and write the profile id (and the connected Facebook page
id) into the gyms table. Read-only against Zernio; idempotent; NEVER overwrites a non-empty
id; never raises out (one gym's failure never blocks the rest). Behind
AGENT_ZERNIO_PROFILE_LINK (config.zernio_profile_link_enabled), default OFF.

Swift River CrossFit, 2026-08-31: the "no matching Zernio profile" ops alert fired ~37
minutes after intake, on auto-generated fill images, for a gym that had not yet had a
chance to connect its socials — indistinguishable from a genuinely stuck gym (Pierce sat
for weeks). Two real bugs, both fixed here:

  1. NO GRACE PERIOD. The alert fired on first sighting. Every new gym's fill content
     lands within the hour, long before anyone could plausibly have connected Zernio, so
     this alert was firing on essentially every fresh onboarding — noise indistinguishable
     from a real stuck gym. Now mirrors calendar_autopublish.sweep_stuck_publishing's
     three-state kv idiom: first sighting starts a clock (no alert), a later sweep alerts
     only once the gym has sat in this state past ZERNIO_LINK_GRACE_SECONDS, then the kv
     flips to "alerted" so it never re-fires for that gym.

  2. THE DISPLAY-NAME FALLBACK WAS A SILENT NO-OP for exactly the gyms it exists to help.
     A dynamically-registered client gym (accounts.register_gym) has NO row in the `gyms`
     table — db.gym_get(base) returns {} — so `if not pid and row:` was always False and
     the name-candidate loop never ran. The gym's real display name, IG handle, and FB page
     URL live in the account REGISTRY (accounts._load_registry_rows), not the gyms table.
     _candidate_names_for_base now reads both sources and the lookup goes through Zernio's
     find_profile_id_any (one /v1/profiles read for every candidate, the same alias-matching
     helper written for the Zanshin/Pete profile-duplication fix) instead of one HTTP round
     trip per candidate.
"""

from datetime import datetime, timezone

from . import config


# How long a "no Zernio profile, but media exists" sighting must persist before it pages a
# human. Long enough that normal onboarding (intake -> connect socials, typically same day
# to a few days) never trips it; short enough that a genuinely stuck gym (Pierce sat for
# weeks) is still caught promptly. Not env-configurable by design, same as
# calendar_autopublish.STALE_PUBLISHING_SECONDS — this is a judgment call, not a per-gym knob.
ZERNIO_LINK_GRACE_SECONDS = 24 * 3600  # 24h


def _fb_page_id(accounts_json):
    """The connected Facebook page id under a Zernio profile, best effort ('' if none).

    Zernio's facebook account carries the page under metadata: a chosen page id
    (selectedPageId / pageId) wins; otherwise, when exactly one page is available, use it.
    We never guess among several unlabelled pages."""
    accounts = (accounts_json or {}).get("accounts") or []
    for a in accounts:
        if (a.get("platform") or "").lower() != "facebook":
            continue
        md = a.get("metadata") or {}
        for k in ("selectedPageId", "pageId", "page_id"):
            v = md.get(k) or a.get(k)
            if v:
                return str(v)
        pages = md.get("availablePages") or md.get("availablePage") or []
        if isinstance(pages, dict):
            pages = [pages]
        if isinstance(pages, list) and len(pages) == 1:
            pid = (pages[0] or {}).get("id")
            if pid:
                return str(pid)
    return ""


def _name_candidates(row):
    """Display-name variants to try when a UUID-keyed portal gym's base does not match its
    Zernio profile name. Ordered, de-duped, empties dropped. e.g. 'Top Fuel Fitness' ->
    ['Top Fuel Fitness', 'topfuelfitness', 'top_fuel_fitness', 'top fuel fitness']."""
    name = ((row.get("display_name") or row.get("gym_name") or row.get("name") or "")).strip()
    if not name:
        return []
    variants = [name, name.lower().replace(" ", ""),
                name.lower().replace(" ", "_"), name.lower()]
    seen, out = set(), []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _registry_row_for_base(base):
    """The dynamic account-registry row for this base (name / ig_handle / fb_page), or {}.

    THE GAP: db.gym_get(base) reads the `gyms` TABLE, which a dynamically-registered client
    gym (accounts.register_gym) never has a row in — its real display name and handles live
    only in the registry JSON. Reading gym_get alone made the name-candidate fallback a
    silent no-op for every such gym (Swift River, 2026-08-31). Never raises: an import or
    read failure just yields no extra candidates, same as any other best-effort lookup here.
    """
    try:
        from . import accounts as _accounts
        for row in _accounts._load_registry_rows():
            if (row.get("base") or "").strip() == base:
                return row
    except Exception:
        pass
    return {}


def _candidate_names_for_base(base, gym_row):
    """Every name-ish string worth trying against Zernio for this base: the gyms-table
    row's display name (_name_candidates) UNIONED with the registry row's name and its raw
    ig_handle (Zernio profile names are sometimes set to the handle, not the brand name).
    Ordered, de-duped, empties dropped."""
    reg = _registry_row_for_base(base)
    names = _name_candidates(gym_row or {}) + _name_candidates(reg)
    handle = (reg.get("ig_handle") or "").strip()
    seen, out = set(), []
    for v in names + ([handle] if handle else []):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def link_client_profiles(bases=None, zernio=None, db=None, logger=None):
    """Ensure every client gym's gyms.zernio_profile_id is populated from Zernio.

    bases   explicit list of tenant bases; default = the client _ig account bases.
    zernio  injectable ZernioClient (find_profile_id_any + list_accounts).
    db      injectable db module (gym_get + gym_upsert + kv_get + kv_set), for tests.
    Returns a summary dict. Never raises."""
    log = logger or (lambda m: print(f"[zernio-profile-link] {m}"))
    if not config.zernio_profile_link_enabled():
        return {"ok": False, "reason": "flag off"}

    if db is None:
        from . import db as db
    if zernio is None:
        from .zernio import ZernioClient
        zernio = ZernioClient()
    if bases is None:
        from .calendar_autopublish import client_gym_bases
        bases = client_gym_bases()

    linked = already = no_profile = errors = 0
    results = []
    for base in bases:
        try:
            row = db.gym_get(base)
            if row and (row.get("zernio_profile_id") or "").strip():
                already += 1
                continue                                  # never overwrite a set id
            # Match the Zernio profile by the base key FIRST (named gyms: eng, topfuel,
            # piercefitness), then every name candidate from BOTH the gyms-table row and the
            # dynamic account registry (display name, ig_handle, fb_page — see
            # _candidate_names_for_base). One /v1/profiles read covers every candidate.
            candidates = _candidate_names_for_base(base, row)
            pid = zernio.find_profile_id_any(base, *candidates)
            if not pid:
                no_profile += 1
                # ALERT when this is a REAL active gym (it has uploaded media), not an
                # empty onboarding stub (audit 2026-08-25 MAJOR: 'no_profile' was a
                # silent counter — the same symptom class as Pierce, invisible again) —
                # but only once it has SAT in this state past ZERNIO_LINK_GRACE_SECONDS
                # (Swift River, 2026-08-31: fill media lands within the hour of intake,
                # long before a gym could plausibly have connected Zernio; alerting on
                # first sighting was noise, not signal). Three-state kv clock, same idiom
                # as calendar_autopublish.sweep_stuck_publishing: unset -> start the clock;
                # a timestamp -> alert once past the grace window; "alerted" -> never again.
                try:
                    import os as _os
                    lib = _os.path.join(config.LIBRARY_PATH, base)
                    has_media = _os.path.isdir(lib) and any(
                        _os.path.splitext(n)[1].lower() in
                        (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov")
                        for n in _os.listdir(lib))
                    if has_media:
                        key = f"zernio_link_alerted_{base}"
                        seen = db.kv_get(key) or ""
                        if seen == "alerted":
                            pass                            # already notified once
                        elif not seen:
                            db.kv_set(key, datetime.now(timezone.utc).isoformat())
                        else:
                            due = True
                            try:
                                first = datetime.fromisoformat(seen)
                                due = (datetime.now(timezone.utc) - first).total_seconds() \
                                    >= ZERNIO_LINK_GRACE_SECONDS
                            except ValueError:
                                pass                        # unparseable stamp: alert now
                            if due:
                                from . import ops_alerts
                                ops_alerts.alert(
                                    f"gym {base} has uploaded media but NO matching Zernio "
                                    "profile (by base key, display name, or handle) for "
                                    f"over {ZERNIO_LINK_GRACE_SECONDS // 3600}h — it can "
                                    "NEVER publish until its socials are connected in "
                                    "Zernio or the profile is linked by hand.")
                                db.kv_set(key, "alerted")
                except Exception:  # noqa: BLE001 - the alert never blocks the link pass
                    pass
                results.append({"gym": base, "status": "no_profile"})
                continue
            page_id = ""
            try:
                page_id = _fb_page_id(zernio.list_accounts(pid))
            except Exception:
                page_id = ""
            fields = {"zernio_profile_id": str(pid)}
            if page_id:
                fields["zernio_default_fb_page_id"] = page_id
            db.gym_upsert(base, **fields)
            linked += 1
            results.append({"gym": base, "status": "linked", "profile_id": str(pid),
                            "fb_page_id": page_id})
            log(f"{base}: linked zernio_profile_id={pid} fb_page={page_id or '(none)'}")
        except Exception as e:  # noqa: BLE001 - one gym never blocks the rest
            errors += 1
            results.append({"gym": base, "status": f"error: {type(e).__name__}"})
            log(f"{base}: link failed: {type(e).__name__}: {e}")

    return {"ok": True, "linked": linked, "already": already,
            "no_profile": no_profile, "errors": errors, "results": results}
