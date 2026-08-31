"""
onboarding_watch.py — catch a gym that is SET UP WRONG, on day one.

WHY THIS EXISTS AND WHY connection_watch IS NOT ENOUGH: connection_watch sweeps
`client_media_sync._client_bases()`, i.e. the ACCOUNT REGISTRY. Every failure it was
built to catch has actually arrived as a gym MISSING FROM THAT REGISTRY, so the watch
could not see the gym at all. Hill Country's partial connection is the watch's own
founding story, and Hill Country was absent from the registry for weeks. CrossFit
Reverb signed up on 2026-08-30 and was invisible the same way within hours.

So this watch reads the AUTHORITATIVE list instead: echo_intake_tokens, the portal's
own record of every gym it has minted an Echo key for. A gym cannot hide from it by
being missing from Echo's side, because Echo's side is exactly what it audits.

WHAT IT CATCHES (each its own reason code, so an alert names the actual next action):
  not_registered  the portal knows this gym, Echo's registry does not -> it is in
                  NEITHER the build nor the publish lane and will never post
                  (Reverb, Hill Country, The Bolton Club, CrossFit Local)
  key_mismatch    the portal token key and the key its intake forwarded under differ,
                  so its answers land where no reader looks (Reverb:
                  crossfitreverb30b5b2 vs crossfitreverb6cdf33)
  no_sources      registered, but no approved sources -> cannot draft anything
  no_profile      no Zernio profile resolves -> cannot publish anywhere
  not_connected   a profile, but ZERO platforms connected (connection_watch skips this
                  case by design: it only reports PARTIAL connections)
  no_fb_page      Facebook is connected but no page is selected -> every Facebook
                  publish raises "no Facebook page selected" (live on Reverb)

RAILS: read-only everywhere except its own kv dedup stamps. Never registers, connects,
approves or publishes anything: a human reads the alert and acts. ONE alert per gym per
distinct issue-set per day. Behind AGENT_ONBOARDING_WATCH, default OFF.
"""

from datetime import date

from . import config

# Order matters: the FIRST unmet requirement is the one the alert leads with, because
# fixing it is what unblocks the next check.
REASON_NOT_REGISTERED = "not_registered"
REASON_KEY_MISMATCH = "key_mismatch"
REASON_NO_SOURCES = "no_sources"
REASON_NO_PROFILE = "no_profile"
REASON_NOT_CONNECTED = "not_connected"
REASON_NO_FB_PAGE = "no_fb_page"

_FIX = {
    REASON_NOT_REGISTERED:
        "the portal knows this gym but Echo's account registry does not, so it is in "
        "neither the build nor the publish lane. Add it to the dynamic registry "
        "(accounts.register_gym) under this exact key.",
    REASON_KEY_MISMATCH:
        "its intake forwarded under a DIFFERENT account key, so its answers landed "
        "where nothing reads them. Migrate the sources onto the portal key.",
    REASON_NO_SOURCES:
        "no approved sources, so Echo cannot draft anything without inventing facts. "
        "Check the gym completed intake and that it landed on this key.",
    REASON_NO_PROFILE:
        "no Zernio profile resolves for this gym, so nothing can publish. Run "
        "zernio_profile_link, or stamp gyms.zernio_profile_id by hand when the "
        "profile is named something find_profile_id cannot match.",
    REASON_NOT_CONNECTED:
        "a Zernio profile exists but ZERO platforms are connected. Send the gym its "
        "connect link (python -m agent intake-link --account <key>).",
    REASON_NO_FB_PAGE:
        "Facebook is connected but no PAGE is selected, so every Facebook publish "
        "raises 'no Facebook page selected'. Stamp zernio_default_fb_page_id from the "
        "account's metadata.selectedPageId.",
}


# Bases that are NOT client gyms and must never be audited as one. LASSO is excluded
# from client_gym_bases BY DESIGN (it has its own lane) and grounds its copy in
# brand_voice rather than client_sources, so auditing it reports not_registered +
# no_sources every single day forever. blake_personal is a staff account.
_NOT_CLIENTS = ("lasso", "blake_personal")


def enabled():
    return config.onboarding_watch_enabled()


def is_client_gym(base_key):
    """False for LASSO and staff accounts, which are legitimately absent from the
    client registry and legitimately have no client_sources."""
    k = str(base_key or "").strip().lower()
    return bool(k) and k not in _NOT_CLIENTS and not k.startswith("lasso")


def portal_keys(http=None):
    """Every (gym_id, echo_account_key) the PORTAL has minted, from echo_intake_tokens.
    This is the authoritative roster; Echo's own registry is what we audit against it.
    Returns [] when creds are absent or the read fails (the sweep is then a no-op)."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return []
    if http is None:
        import requests  # lazy
        http = requests
    try:
        r = http.get(f"{url.rstrip('/')}/rest/v1/echo_intake_tokens",
                     params={"select": "gym_id,echo_account_key"},
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=30)
        if r.status_code >= 400:
            return []
        return [(str(t.get("gym_id") or ""), str(t.get("echo_account_key") or ""))
                for t in (r.json() or []) if (t.get("echo_account_key") or "").strip()]
    except Exception:  # noqa: BLE001 - a roster read failure is a silent no-op
        return []


def intake_keys(http=None):
    """{gym_id: echo_account_key} as recorded on the gym's own INTAKE submission. A
    value differing from the portal token key is the key_mismatch that stranded four
    gyms' answers."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return {}
    if http is None:
        import requests  # lazy
        http = requests
    try:
        r = http.get(f"{url.rstrip('/')}/rest/v1/echo_social_intake",
                     params={"select": "client_key,echo_account_key"},
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=30)
        if r.status_code >= 400:
            return {}
        return {str(x.get("client_key") or ""): str(x.get("echo_account_key") or "")
                for x in (r.json() or [])}
    except Exception:  # noqa: BLE001
        return {}


def check_gym(base_key, gym_id="", intake_key="", *, bases=None, deps=None):
    """Every setup problem for ONE gym, most-blocking first. Pure apart from the
    injected `deps` readers, so the whole rule set is offline-testable."""
    d = deps or _live_deps()
    issues = []
    registered = base_key in (bases if bases is not None else d["bases"]())
    if not registered:
        issues.append(REASON_NOT_REGISTERED)
    if intake_key and intake_key != base_key:
        issues.append(REASON_KEY_MISMATCH)
    if not d["approved_sources"](base_key):
        issues.append(REASON_NO_SOURCES)
    profile_id = d["profile_id"](base_key)
    if not profile_id:
        issues.append(REASON_NO_PROFILE)
        return issues
    platforms = d["platforms"](profile_id)
    if not platforms:
        issues.append(REASON_NOT_CONNECTED)
        return issues
    if "facebook" in platforms and not d["fb_page"](base_key):
        issues.append(REASON_NO_FB_PAGE)
    return issues


def run(*, deps=None, alert=None, kv=None, today=None, http=None):
    """Sweep every gym on the portal roster and alert on the ones set up wrong.
    Returns {base_key: [reason, ...]} for the gyms alerted this pass."""
    if not enabled():
        return {}
    d = deps or _live_deps()
    if alert is None:
        from .ops_alerts import alert as _alert
        alert = _alert
    if kv is None:
        from . import db
        kv = type("_KV", (), {"get": staticmethod(db.kv_get),
                              "set": staticmethod(db.kv_set)})()
    day = str(today or date.today())
    roster = d["roster"](http)
    intake = d["intake"](http)
    try:
        bases = set(d["bases"]())
    except Exception:  # noqa: BLE001 - without the registry every gym reads unregistered
        return {}
    out = {}
    for gym_id, base_key in roster:
        if not is_client_gym(base_key):
            continue
        try:
            issues = check_gym(base_key, gym_id, intake.get(gym_id, ""),
                               bases=bases, deps=d)
        except Exception:  # noqa: BLE001 - one gym never blocks the sweep
            continue
        if not issues:
            continue
        stamp = f"onboarding_watch_{base_key}_{'+'.join(issues)}_{day}"
        try:
            if kv.get(stamp, ""):
                continue                      # already said this today
        except Exception:  # noqa: BLE001
            pass
        lead = issues[0]
        alert(f"{base_key}: not set up to post ({', '.join(issues)}). {_FIX[lead]}")
        try:
            kv.set(stamp, "alerted")
        except Exception:  # noqa: BLE001
            pass
        out[base_key] = issues
    return out


def _live_deps():
    """The real readers. Split out so run()/check_gym() are fully injectable."""
    def _bases():
        from .calendar_autopublish import client_gym_bases
        return client_gym_bases()

    def _approved(base):
        from . import client_sources
        return client_sources.approved_sources(f"{base}_ig")

    def _profile(base):
        from . import zernio_publisher
        try:
            return zernio_publisher._default_profile_resolver(f"{base}_ig")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return ""

    def _platforms(profile_id):
        from . import zernio
        try:
            res = zernio.ZernioClient().list_accounts(profile_id) or {}
            return {str(a.get("platform") or "")
                    for a in (res.get("accounts") or res.get("data") or [])}
        except Exception:  # noqa: BLE001
            return set()

    def _fb_page(base):
        from . import zernio_publisher
        try:
            return zernio_publisher._default_page_resolver(f"{base}_fb")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return ""

    return {"roster": portal_keys, "intake": intake_keys, "bases": _bases,
            "approved_sources": _approved, "profile_id": _profile,
            "platforms": _platforms, "fb_page": _fb_page}
