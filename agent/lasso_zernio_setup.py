"""
lasso_zernio_setup.py — one-time, idempotent setup for the LASSO-via-Zernio cutover
(AGENT_LASSO_VIA_ZERNIO; run as `python -m agent lasso-zernio-setup [--page <id>]`).

WHY the cutover exists (Blake's ruling 2026-08-27): metrics_sync ingests Zernio
analytics; LASSO's Meta-direct-published posts read there as an external/second
publisher and taint LASSO's own months for the learning loop. One publish path =
one guard set = A-gate parity. Under the flag, LASSO's calendar rows publish
through the SAME zernio lane as the client gyms — which resolves everything from
the 'lasso' gyms row, exactly like a client. This stamps that row.

What it stamps (all idempotent; a re-run reports 'already' and changes nothing):
  1. gyms.zernio_profile_id = LASSO's verified Zernio profile
     (6a74a3b977a9ae3719f5c0c0 — IG 'lassoframework' connected under it, account
     6a69fc9cdf17280d93d0727f; verified live in Zernio 2026-08-27). NEVER
     overwrites a different non-empty id (clear it by hand first).
  2. gyms.zernio_default_fb_page_id — the Facebook page to publish to, picked via
     the existing ZernioClient.list_facebook_pages flow: auto-selected when there
     is exactly ONE page, or exactly ONE whose name contains 'lasso'
     (case-insensitive); otherwise the pages are PRINTED and the run exits asking
     for a hand-pick (`--page <id>`). Never guesses among several unrelated pages.
  3. the per-gym autonomy kv for 'lasso' (db.set_autonomy): LASSO's Meta-direct
     lane published PENDING rows with no approval step (approved_only=False); the
     zernio client lane requires approval UNLESS the gym is autonomous, so lasso
     is stamped autonomous to keep today's approval model identical — only the
     PUBLISHER changes under the flag.

Until 1 + 2 are stamped, an armed AGENT_LASSO_VIA_ZERNIO lane HOLDS with one
deduped alert (calendar_autopublish) — it never drops a post and never falls back
to Meta-direct. Read-only against Zernio; never logs a token or secret.
"""

LASSO_BASE = "lasso"
LASSO_ZERNIO_PROFILE_ID = "6a74a3b977a9ae3719f5c0c0"   # verified in Zernio 2026-08-27


def _pick_page(pages, want="lasso"):
    """The auto-selectable Facebook page id, or None when the choice is ambiguous:
    exactly ONE page total, or exactly ONE whose name contains `want`
    (case-insensitive). We never guess among several unrelated pages."""
    pages = pages or []
    if len(pages) == 1:
        return str(pages[0].get("id") or "") or None
    matches = [p for p in pages
               if want in str(p.get("name") or "").lower() and p.get("id")]
    if len(matches) == 1:
        return str(matches[0]["id"])
    return None


def run(page_id=None, db=None, zclient=None, logger=None):
    """Stamp the 'lasso' gyms row (profile id + FB page) and the lasso autonomy kv.

    page_id  optional explicit Facebook page id (the --page hand-pick).
    db       injectable db module (gym_get/gym_upsert/is_autonomous/set_autonomy).
    zclient  injectable ZernioClient (list_accounts + list_facebook_pages); only
             constructed/called when the FB page still needs picking, so a fully
             stamped re-run needs no network and no ZERNIO_API_KEY.
    Returns a summary dict: {"ok", "profile", "fb_page", "autonomy", "pages"}.
    """
    log = logger or (lambda m: print(f"[lasso-zernio-setup] {m}"))
    if db is None:
        from . import db
    row = db.gym_get(LASSO_BASE) or {}
    out = {"ok": True, "profile": "", "fb_page": "", "autonomy": "", "pages": []}

    # 1) profile id — stamp once; NEVER overwrite a different non-empty id.
    current = str(row.get("zernio_profile_id") or "").strip()
    if current == LASSO_ZERNIO_PROFILE_ID:
        out["profile"] = "already"
    elif current:
        out["ok"] = False
        out["profile"] = "mismatch"
        log(f"gyms.zernio_profile_id is already '{current}' (expected "
            f"{LASSO_ZERNIO_PROFILE_ID}); NOT overwriting — clear it by hand "
            "first if the stored id is wrong.")
        return out
    else:
        db.gym_upsert(LASSO_BASE, zernio_profile_id=LASSO_ZERNIO_PROFILE_ID)
        out["profile"] = "stamped"
        log(f"stamped gyms.zernio_profile_id={LASSO_ZERNIO_PROFILE_ID}")

    # 2) Facebook page — the existing list_facebook_pages flow, like a client gym.
    page_current = str(row.get("zernio_default_fb_page_id") or "").strip()
    if page_current and not page_id:
        out["fb_page"] = "already"
    else:
        if zclient is None:
            from .zernio import ZernioClient
            zclient = ZernioClient()
        from . import zernio as _z
        accounts_json = zclient.list_accounts(LASSO_ZERNIO_PROFILE_ID)
        fb_account = _z.facebook_account_id(accounts_json)
        if not fb_account:
            out["ok"] = False
            out["fb_page"] = "no_facebook"
            log("no Facebook account is connected under LASSO's Zernio profile; "
                "connect Facebook in Zernio, then re-run this setup.")
            return out
        pages = _z.map_pages(
            zclient.list_facebook_pages(fb_account)).get("pages") or []
        out["pages"] = pages
        if page_id:
            wanted = str(page_id).strip()
            if not any(str(p.get("id")) == wanted for p in pages):
                out["ok"] = False
                out["fb_page"] = "bad_page"
                log(f"--page {wanted} is not among the profile's Facebook pages:")
                for p in pages:
                    log(f"  {p.get('id')}  {p.get('name')}")
                return out
            chosen = wanted
        else:
            chosen = _pick_page(pages)
        if not chosen:
            out["ok"] = False
            out["fb_page"] = "ambiguous"
            log("could not auto-select a Facebook page (need exactly one page, "
                "or exactly one whose name contains 'lasso'). Hand-pick with: "
                "python -m agent lasso-zernio-setup --page <id>")
            for p in pages:
                log(f"  {p.get('id')}  {p.get('name')}")
            return out
        if page_current == chosen:
            out["fb_page"] = "already"
        else:
            db.gym_upsert(LASSO_BASE, zernio_default_fb_page_id=str(chosen))
            out["fb_page"] = "stamped"
            log(f"stamped gyms.zernio_default_fb_page_id={chosen}")

    # 3) autonomy — keep LASSO's no-approval publish model under the client lane.
    if db.is_autonomous(LASSO_BASE):
        out["autonomy"] = "already"
    else:
        db.set_autonomy(LASSO_BASE, True)
        out["autonomy"] = "stamped"
        log("stamped lasso autonomy ON (kv): LASSO keeps publishing pending rows "
            "at slot time with no approval step — only the publisher changes "
            "under AGENT_LASSO_VIA_ZERNIO.")

    log(f"setup summary: profile={out['profile']} fb_page={out['fb_page']} "
        f"autonomy={out['autonomy']}")
    if out["ok"]:
        log("setup complete. Arm AGENT_LASSO_VIA_ZERNIO (with "
            "AGENT_CALENDAR_AUTOPUBLISH + AGENT_PUBLISH_ENABLED + "
            "AGENT_ZERNIO_PUBLISH) to route LASSO's calendar rows through Zernio.")
    return out
