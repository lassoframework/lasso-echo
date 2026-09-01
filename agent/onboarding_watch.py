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
  no_voice        intake completed but NO brand bible was ever produced (or the
                  add-client scaffold's TODOs were never filled), so voice.load_voice
                  returns nothing, the drafter blocks every card and the gym silently
                  never posts. ENG went this way, and crossfitlocal / hillcountry /
                  theboltonclub went the same way the week of 2026-08-31: sources
                  approved, Zernio connected, everything green, zero posts forever.
  no_profile      no Zernio profile resolves -> cannot publish anywhere
  not_connected   a profile, but ZERO platforms connected (connection_watch skips this
                  case by design: it only reports PARTIAL connections)
  no_fb_page      Facebook is connected but no page is selected -> every Facebook
                  publish raises "no Facebook page selected" (live on Reverb)

RAILS: read-only everywhere except its own kv dedup stamps. Never registers, connects,
approves or publishes anything: a human reads the alert and acts. ONE alert per gym per
distinct issue-set per day. Behind AGENT_ONBOARDING_WATCH, default OFF.
"""

import os
from datetime import date

from . import config

# Order matters: the FIRST unmet requirement is the one the alert leads with, because
# fixing it is what unblocks the next check.
REASON_NOT_REGISTERED = "not_registered"
REASON_KEY_MISMATCH = "key_mismatch"
REASON_NO_SOURCES = "no_sources"
REASON_NO_VOICE = "no_voice"
REASON_NO_PROFILE = "no_profile"
REASON_NOT_CONNECTED = "not_connected"
REASON_NO_FB_PAGE = "no_fb_page"

# The full set, in check order. Anything summarising this watch (the onboarding-audit
# screen) should iterate THIS rather than its own hand-listed tuple, so a new reason
# code can never be invisible in the summary the way no_voice was invisible for months.
REASONS = (REASON_NOT_REGISTERED, REASON_KEY_MISMATCH, REASON_NO_SOURCES,
           REASON_NO_VOICE, REASON_NO_PROFILE, REASON_NOT_CONNECTED,
           REASON_NO_FB_PAGE)

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
    REASON_NO_VOICE:
        "NO brand bible: <DATA_DIR>/brand_voice/<base>/lasso_voice.md is missing, "
        "empty, or still the all-TODO add-client scaffold, so the drafter blocks "
        "every card and this gym never posts, silently and forever. Produce it from "
        "the gym's OWN intake answers (never write one by hand for them): "
        "python -m agent social-intake-sync --base <base>",
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


def _fix_for(reason, base_key):
    """The fix line with THIS gym's key already substituted for the <base> slot, so the
    operator copies a runnable command out of the alert instead of retyping it under
    the wrong key. Only the <base> placeholder is filled: the older fixes say <key>
    (an ACCOUNT key, not always the base) and are left exactly as written."""
    return _FIX[reason].replace("<base>", base_key)


def bible_is_hollow(raw):
    """True when a bible file EXISTS but is only the add-client scaffold: every body
    line is a TODO placeholder.

    WHY THIS COUNTS AS NO BIBLE: onboard.VOICE_TEMPLATE writes a fully-TODO doc, and
    every writer (social_intake_reader._write_doc, website_intake) refuses to clobber a
    file that already exists. So a scaffolded gym that never had its TODOs filled keeps
    that hollow doc FOREVER, and because the file is non-empty, load_voice returns a
    VoiceDoc and preflight passes it. There is no avatar, no pillars, no CTAs and no
    hashtags in it, so it is functionally identical to having no bible at all.

    Deliberately conservative: headings and the scaffold's blockquote are ignored, a
    wrapped TODO paragraph counts as one placeholder (VOICE_TEMPLATE's guardrails TODO
    runs three lines), and ONE real body line anywhere makes the doc real. A
    half-filled bible is a human's work in progress, never a false alarm here."""
    in_todo = False
    saw_body = False
    for line in str(raw or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            in_todo = False          # a blank line or heading ends the TODO paragraph
            continue
        if s.startswith(">"):
            continue                 # the scaffold's own "nothing here is approved" note
        if s.upper().startswith("TODO"):
            in_todo, saw_body = True, True
            continue
        if in_todo:
            saw_body = True          # continuation of the wrapped TODO paragraph
            continue
        return False                 # real content: a human filled something in
    return saw_body


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
    # A mismatch only MATTERS while it is still stranding the answers. Once the sources
    # have been migrated onto the portal key the old echo_social_intake row still records
    # the original key forever, so a bare mismatch check re-reports gyms that were fixed
    # weeks ago. Measured on the first full sweep, 2026-08-30: Pierce, Reverb, Hill
    # Country and The Bolton Club all flagged key_mismatch while each had its sources
    # (17, 6, 22 and 3) present and was publishing normally. With alerts armed that is a
    # daily page about four healthy gyms, and at 100 gyms it is the noise that trains
    # everyone to ignore the watch. So: report it only when the answers really are
    # missing from the key we read.
    has_sources = bool(d["approved_sources"](base_key))
    if intake_key and intake_key != base_key and not has_sources:
        issues.append(REASON_KEY_MISMATCH)
    if not has_sources:
        issues.append(REASON_NO_SOURCES)
    # THE MOST EXPENSIVE SILENT FAILURE IN THE SYSTEM, and until now nothing in this
    # file even looked at it: the intake completes, sources land approved, Zernio
    # connects, every check above and below passes, and NO brand bible was ever
    # produced. voice.load_voice returns None, the drafter blocks every card, and the
    # gym posts nothing forever while reading as perfectly healthy (ENG, then
    # crossfitlocal / hillcountry / theboltonclub in one week). The only existing
    # signal, client_media_sync's _alert_stall(base, "no_voice"), fires once ever,
    # names no next command, and is unreachable for exactly these gyms because
    # scan_and_generate returns early with no sources or no media.
    #
    # POSITION: after no_sources, because a gym with no sources cannot have a bible
    # (the bible is written FROM the intake answers), so no_sources is the step that
    # unblocks this one. BEFORE no_profile, deliberately, for two reasons: (1) the
    # bible is a BUILD-lane requirement and the profile is a PUBLISH-lane one, and a
    # gym with a perfect profile and no bible still posts nothing, so the bible is the
    # more blocking of the two; (2) no_profile RETURNS EARLY, so ordering it first
    # would hide the missing bible entirely, and the day the profile gets linked the
    # gym would go straight back to reading healthy while still never drafting. That
    # is the exact invisibility this check exists to end.
    if has_sources and not d["voice"](base_key):
        issues.append(REASON_NO_VOICE)
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


def autoregister(base_key, gym_id, *, deps=None, alert=None):
    """Register ONE portal-known gym into Echo's dynamic account registry under the
    exact key the portal minted. Returns True when a registration happened.

    Closes the hand step that was paid five times (Hill Country, The Bolton Club,
    CrossFit Local, CrossFit Reverb, CrossFit Newtown): register_gym has ONE
    production caller, the social-intake sweep, so a gym that has not submitted
    intake yet is in NEITHER lane and no automation ever puts it in one.

    Rails: behind AGENT_ONBOARDING_AUTOREGISTER (default OFF); registers only under
    the portal's own key; needs the gym's REAL name from the gyms table and does
    NOTHING without one, because inventing a name is fabrication. Creates an inactive
    Account record only: no tokens, no connection, no approval, no publish. Never
    raises out."""
    if not config.onboarding_autoregister_enabled():
        return False
    if not is_client_gym(base_key):
        return False
    d = deps or _live_deps()
    try:
        name = str(d["gym_name"](gym_id) or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    if not name:
        return False
    try:
        from . import accounts
        # CHECK THE RETURN. register_gym silently no-ops and returns [] (it does NOT
        # raise) when AGENT_DYNAMIC_ACCOUNTS is off. Ignoring that would have this
        # function alert "registered into Echo's account registry", return True, and
        # let run() drop not_registered from the day's alert while NOTHING was written:
        # a fabricated success, which is the exact failure class this whole sweep
        # exists to catch.
        if not accounts.register_gym(base_key, name=name):
            if alert:
                alert(f"{base_key}: auto-register did nothing (the dynamic account "
                      "registry is off, so there is nowhere to write). It stays in "
                      "neither lane. Arm AGENT_DYNAMIC_ACCOUNTS on the worker.")
            return False
    except Exception as exc:  # noqa: BLE001 - one gym never blocks the sweep
        if alert:
            alert(f"{base_key}: auto-register failed ({type(exc).__name__}: {exc}). "
                  "It stays in neither lane until registered by hand.")
        return False
    if alert:
        alert(f"{base_key}: registered into Echo's account registry as '{name}' so it "
              "is in the build lane. It still needs its own intake, connection and "
              "media before anything real can post.")
    return True


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
        # ACT on not_registered when armed, instead of asking a human to do the one
        # mechanical step in this whole list. Re-check after, so the alert reports
        # what is ACTUALLY still wrong rather than a problem we just fixed.
        if REASON_NOT_REGISTERED in issues:
            try:
                if autoregister(base_key, gym_id, deps=d, alert=alert):
                    bases.add(base_key)
                    issues = check_gym(base_key, gym_id, intake.get(gym_id, ""),
                                       bases=bases, deps=d)
            except Exception:  # noqa: BLE001
                pass
        if not issues:
            continue
        stamp = f"onboarding_watch_{base_key}_{'+'.join(issues)}_{day}"
        try:
            if kv.get(stamp, ""):
                continue                      # already said this today
        except Exception:  # noqa: BLE001
            pass
        lead = issues[0]
        alert(f"{base_key}: not set up to post ({', '.join(issues)}). "
              f"{_fix_for(lead, base_key)}")
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

    def _voice(base):
        """True when this gym has a USABLE brand bible, resolved exactly the way the
        live readers do. The path is NOT hardcoded here: client_media_sync's
        _resolve_client_voice_path is the same resolver the drafting lane and preflight
        use, durable <DATA_DIR>/brand_voice/<base>/lasso_voice.md first with the
        account's repo-relative voice_doc as fallback. onboard_verify checks
        'brand_voice/<key>.md', which is not a path anything writes; copying that would
        have made this whole check report the fleet wrong.

        Unreadable -> True (assume fine). A reader that fails open cannot page the
        whole fleet the way an unreadable registry would."""
        from .voice import load_voice
        from .client_media_sync import _resolve_client_voice_path  # noqa: SLF001
        repo_path = os.path.join("brand_voice", base, "lasso_voice.md")
        try:
            from . import accounts
            acct = accounts.get_account(f"{base}_ig") or accounts.get_account(base)
            if acct is not None:
                repo_path = acct.voice_doc_path() or repo_path
        except Exception:  # noqa: BLE001
            pass
        try:
            doc = load_voice(_resolve_client_voice_path(base, repo_path))
        except Exception:  # noqa: BLE001
            return True
        if doc is None:
            return False
        return not bible_is_hollow(doc.raw)

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

    def _gym_name(gym_id):
        """The gym's REAL display name from the shared plane, or "" — never invented.
        autoregister does nothing without one, because a fabricated name would become
        the gym's Zernio profile name and its account label."""
        url = config.supabase_url()
        key = config.supabase_service_key()
        if not url or not key or not gym_id:
            return ""
        try:
            import requests  # lazy
            r = requests.get(f"{url.rstrip('/')}/rest/v1/gyms",
                             params={"id": f"eq.{gym_id}", "select": "name"},
                             headers={"apikey": key, "Authorization": f"Bearer {key}"},
                             timeout=30)
            if r.status_code >= 400:
                return ""
            rows = r.json() or []
            return str((rows[0] or {}).get("name") or "") if rows else ""
        except Exception:  # noqa: BLE001
            return ""

    return {"roster": portal_keys, "intake": intake_keys, "bases": _bases,
            "approved_sources": _approved, "voice": _voice, "profile_id": _profile,
            "platforms": _platforms, "fb_page": _fb_page, "gym_name": _gym_name}
