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
"""

from . import config


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
    name = ((row.get("display_name") or row.get("gym_name") or "")).strip()
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


def link_client_profiles(bases=None, zernio=None, db=None, logger=None):
    """Ensure every client gym's gyms.zernio_profile_id is populated from Zernio.

    bases   explicit list of tenant bases; default = the client _ig account bases.
    zernio  injectable ZernioClient (find_profile_id + list_accounts).
    db      injectable db module (gym_get + gym_upsert), for tests.
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
            # piercefitness), then fall back to the gym's DISPLAY NAME. A portal-onboarded
            # gym is keyed by a UUID, so its base never matches the profile name; its Zernio
            # profile is named after the gym, so the display-name pass links it. Any gym
            # going forward links automatically whichever key its profile is named for.
            pid = zernio.find_profile_id(base)
            if not pid and row:
                for cand in _name_candidates(row):
                    pid = zernio.find_profile_id(cand)
                    if pid:
                        break
            if not pid:
                no_profile += 1
                # ALERT when this is a REAL active gym (it has uploaded media), not an
                # empty onboarding stub (audit 2026-08-25 MAJOR: 'no_profile' was a
                # silent counter — the same symptom class as Pierce, invisible again).
                # Deduped per gym (kv) so the per-loop runner never storms.
                try:
                    import os as _os
                    lib = _os.path.join(config.LIBRARY_PATH, base)
                    has_media = _os.path.isdir(lib) and any(
                        _os.path.splitext(n)[1].lower() in
                        (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov")
                        for n in _os.listdir(lib))
                    if has_media and not db.kv_get(f"zernio_link_alerted_{base}"):
                        db.kv_set(f"zernio_link_alerted_{base}", "1")
                        from . import ops_alerts
                        ops_alerts.alert(
                            f"gym {base} has uploaded media but NO matching Zernio "
                            "profile (by base key or display name) — it can NEVER "
                            "publish until its socials are connected in Zernio or the "
                            "profile is linked by hand.")
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
