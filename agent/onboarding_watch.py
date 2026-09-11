"""
onboarding_watch.py — catch a gym that is SET UP WRONG, on day one.

WHY THIS EXISTS AND WHY connection_watch IS NOT ENOUGH: connection_watch sweeps
`client_media_sync._client_bases()`, i.e. the ACCOUNT REGISTRY. Every failure it was
built to catch has actually arrived as a gym MISSING FROM THAT REGISTRY, so the watch
could not see the gym at all. Hill Country's partial connection is the watch's own
founding story, and Hill Country was absent from the registry for weeks. CrossFit
Reverb signed up on 2026-08-30 and was invisible the same way within hours.

So this watch reads the PORTAL's roster instead of Echo's registry, so a gym cannot hide
from it by being missing from Echo's side, because Echo's side is exactly what it audits.

THE ROSTER IS *NOT* echo_intake_tokens (2026-09-11 incident). This docstring used to
call echo_intake_tokens "the AUTHORITATIVE list of Echo clients". It is not: the portal
mints an Echo intake token for EVERY gym it knows (131 rows, one per portal gym), so
with autoregister + auto connect-link armed this watch registered ~110 LASSO ads-only
gyms into Echo's registry, DMed 36 of their owners a connect link "from Echo", and
minted 153 "not set up to post" tickets for gyms that never bought Echo. The client
universe is `echo_gym_settings` (agent/echo_clients.py, the ONE predicate); the token
table only supplies each client's minted key. portal_keys() returns token rows for
Echo clients ONLY, and every action in this module re-checks is_echo_client before it
alerts, registers or notifies. echo_clients fails closed: an unreadable client universe
means an empty roster and a silent sweep, never the fleet.

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
  no_intake_token an ECHO CLIENT (echo_gym_settings row) the portal's gyms table knows
                  but echo_intake_tokens has ZERO rows for, so it never even ENTERS
                  the sweep above (Empire Training Academy, 2026-09-09). Read straight
                  from `gyms` rather than through the token-keyed roster, so this is
                  the one check that does not require a base_key to already exist.
                  Gated on is_echo_client like everything else here.

RAILS: read-only everywhere except its own kv dedup stamps. Never registers, connects,
approves or publishes anything: a human reads the alert and acts. ONE alert per gym per
distinct issue-set per day. Behind AGENT_ONBOARDING_WATCH, default OFF.
"""

import os
from datetime import date

from . import config
from . import echo_clients
from . import zernio as _z

# Order matters: the FIRST unmet requirement is the one the alert leads with, because
# fixing it is what unblocks the next check.
REASON_NOT_REGISTERED = "not_registered"
REASON_KEY_MISMATCH = "key_mismatch"
REASON_NO_SOURCES = "no_sources"
REASON_NO_VOICE = "no_voice"
REASON_NO_PROFILE = "no_profile"
REASON_NOT_CONNECTED = "not_connected"
REASON_NO_FB_PAGE = "no_fb_page"
#: Some publishable platforms are connected and some are NOT. Carries the missing
#: ones by name (e.g. "missing_platform:instagram"), because "not set up to post"
#: with no platform named is a support ticket, not an answer.
REASON_MISSING_PLATFORM = "missing_platform"
# THE BLIND SPOT THIS WATCH WAS BUILT TO CATCH, AND COULD NOT (2026-09-10, Empire
# Training Academy incident): every check above is keyed off portal_keys(), i.e.
# echo_intake_tokens rows. A gym with ZERO such rows -- exactly Empire's situation, and
# exactly the failure mode this whole watch exists for -- never enters the roster loop
# in run() at all, so it can never be checked, let alone alerted on. zero_token_gyms()
# reads the portal's OWN `gyms` table directly (the same table the portal's new
# 15-minute retry cron sweeps from the other direction) and reports any real client gym
# absent from echo_intake_tokens entirely. Deliberately kept as a SECOND, independent
# watcher rather than folded away in favor of the portal cron: two watchers catching the
# same gap from different angles is safety margin, not redundancy waste, and this one
# still fires even if the portal cron is ever disabled, misconfigured, or has its own bug.
REASON_NO_INTAKE_TOKEN = "no_intake_token"

# The full set, in check order. Anything summarising this watch (the onboarding-audit
# screen) should iterate THIS rather than its own hand-listed tuple, so a new reason
# code can never be invisible in the summary the way no_voice was invisible for months.
REASONS = (REASON_NOT_REGISTERED, REASON_KEY_MISMATCH, REASON_NO_SOURCES,
           REASON_NO_VOICE, REASON_NO_PROFILE, REASON_NOT_CONNECTED,
           REASON_NO_FB_PAGE, REASON_MISSING_PLATFORM, REASON_NO_INTAKE_TOKEN)

# gyms.status values that are NOT a real onboarded client yet (mirrors portal_gyms.py's
# _EXCLUDED_STATUSES ruling, 2026-08-27: an 'onboarding' row is a lead who started the
# funnel, not a client yet, and 'inactive'/'archived' are dead rows). A NULL, empty, or
# unrecognized future status is NOT excluded here: fail open, never drop a possibly-real
# gym on a status this watch has not seen.
_EXCLUDED_GYM_STATUSES = {"onboarding", "inactive", "archived"}

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
    REASON_MISSING_PLATFORM:
        "some publishable platforms are connected and some are not, so this gym posts "
        "on a subset of the lanes it is paying for. The reason names the missing ones. "
        "Send the gym its connect link (python -m agent intake-link --account <key>) "
        "and have the owner finish the platform named.",
    REASON_NO_INTAKE_TOKEN:
        "this gym exists in the portal's gyms table but has ZERO echo_intake_tokens "
        "rows, so it never enters this watch's own per-key sweep either (the exact "
        "blind spot that let Empire Training Academy go unseen). The portal's 15 "
        "minute retry cron should mint one automatically; if this alert repeats past "
        "an hour, mint by hand: python -m agent onboard --account <key> --name "
        "'<name>' (any lowercase slug; onboard.run derives the canonical key itself).",
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
    # A reason may carry a payload after a colon ("missing_platform:instagram"), which
    # is the part that makes the alert actionable. The FIX text is keyed on the bare
    # reason, and an unknown reason must not raise out of an alert path.
    bare = str(reason).split(":", 1)[0]
    text = _FIX.get(bare)
    if text is None:
        return ""
    return text.replace("<base>", base_key)


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


def portal_keys(http=None, is_client=None):
    """(gym_id, echo_account_key) for every ECHO CLIENT the portal has minted a key
    for: echo_intake_tokens rows whose gym_id has an echo_gym_settings row.

    NOT the whole token table (2026-09-11): the portal mints a token for every gym it
    knows, clients or not, so the unfiltered table is the LASSO ads fleet. The client
    filter is echo_clients.is_echo_client, which fails closed -- an unreadable client
    universe makes this an empty roster and the sweep a no-op, never the fleet.
    Returns [] when creds are absent or the read fails."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return []
    is_client = is_client or echo_clients.is_echo_client
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
        rows = [(str(t.get("gym_id") or ""), str(t.get("echo_account_key") or ""))
                for t in (r.json() or []) if (t.get("echo_account_key") or "").strip()]
    except Exception:  # noqa: BLE001 - a roster read failure is a silent no-op
        return []
    return [(gid, k) for gid, k in rows if is_client(gid)]


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


def zero_token_gyms(known_gym_ids, http=None, is_client=None):
    """ECHO CLIENT gyms in the portal's `gyms` table with ZERO echo_intake_tokens rows:
    the blind spot portal_keys() (and therefore every check_gym() sweep) can never see,
    because a gym needs a token row just to enter that roster in the first place.

    Gated on echo_clients.is_echo_client (2026-09-11): the gyms table is the whole
    LASSO fleet, and "a gym with no Echo token" is the NORMAL state of an ads-only
    gym, not a fault. Only a gym with an echo_gym_settings row and no token is one.

    Reads `gyms` directly (id, name, slug, status, is_demo, load_test, is_verification)
    rather than joining in SQL, so a read failure here fails the SAME way every other
    reader in this module does: an empty list, never a crash, never a guess. Verified
    2026-09-10 against the live table (project ooqcvmcjspeltuuhcvlh): EVERY status='active'
    row with zero token rows today is portal fixture data (Demo Fitness, ZZ Test Gym,
    SAMPLE Green Healthy, etc.), each carrying is_demo=true -- so `select=*` and reading
    fields with .get() (never a hardcoded column list) means a portal environment missing
    one of these columns degrades to "field absent -> excluded flag reads False" instead
    of a hard 400 that would blind this whole check. known_gym_ids is the set of gym_id
    already seen in portal_keys() -- anything NOT in it is a gym the token-keyed sweep
    will never reach. Excludes the same not-a-real-client-yet statuses portal_gyms.py
    already established (an 'onboarding' row is a lead, not a client) PLUS any row
    flagged is_demo / load_test / is_verification; an unrecognized future status is NOT
    excluded (fail open).

    Returns [(gym_id, name, slug), ...]."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return []
    if http is None:
        import requests  # lazy
        http = requests
    try:
        r = http.get(f"{url.rstrip('/')}/rest/v1/gyms",
                     params={"select": "*"},
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=30)
        if r.status_code >= 400:
            return []
        rows = r.json() or []
    except Exception:  # noqa: BLE001 - a roster read failure is a silent no-op
        return []
    is_client = is_client or echo_clients.is_echo_client
    known = set(known_gym_ids or ())
    out = []
    for g in rows:
        gid = str(g.get("id") or "")
        if not gid or gid in known:
            continue
        if not is_client(gid):
            continue                      # an ads-only gym: no Echo token is correct
        status = str(g.get("status") or "").strip().lower()
        if status in _EXCLUDED_GYM_STATUSES:
            continue
        if g.get("is_demo") or g.get("load_test") or g.get("is_verification"):
            continue
        out.append((gid, str(g.get("name") or ""), str(g.get("slug") or "")))
    return out


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
    # PUBLISHABLE platforms only. _platforms returns EVERY platform string Zernio holds,
    # including 'openaiads', which nothing in Echo can publish to. District H's Zernio
    # profile carries an openaiads account and NOTHING else, so the truthiness check
    # below used to pass and District H read completely healthy while having zero lanes
    # it could actually post on. A platform Echo cannot publish to is not a connection.
    publishable = {p for p in (platforms or set()) if p in _z.PLATFORMS}
    if not publishable:
        issues.append(REASON_NOT_CONNECTED)
        return issues
    # NAME THE MISSING LANE. A gym connected on some publishable platforms but not all
    # used to report NOTHING at all: MFLH has Facebook and no Instagram and read healthy
    # while the main lane was dark. "not set up to post" with no platform named is a
    # support ticket; this says which one, so the fix is one message to the owner.
    missing = [p for p in _z.PLATFORMS if p not in publishable]
    if missing:
        issues.append(f"{REASON_MISSING_PLATFORM}:{','.join(missing)}")
    if "facebook" in publishable and not d["fb_page"](base_key):
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
    # ECHO CLIENTS ONLY (2026-09-11): this is the exact call that put ~110 ads-only
    # gyms into gym_accounts.json and, through notify_new_gym below, DMed 36 of their
    # owners. A gym that is not in echo_gym_settings is never registered, never
    # notified, and never alerted about. Silent by design: a non-client producing an
    # alert is the same noise the incident produced.
    is_client = d.get("is_client") or echo_clients.is_echo_client
    if not (is_client(gym_id) or is_client(base_key)):
        return False
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
        if not accounts.register_gym(base_key, name=name, gym_id=gym_id,
                                     door="onboarding_watch.autoregister"):
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
    # THE NEXT GAP (2026-09-03): registration alone leaves a gym with ZERO platforms
    # connected, and sending that first connect link was ALWAYS a separate manual
    # step -- register_gym's own docstring says tokens are "never written here, by
    # hand", and it turned out the notification was never automated either. Five
    # gyms sat with a real Zernio profile and no connection ever attempted because
    # of exactly this gap. OFF unless config.auto_connect_link_enabled(); never lets
    # a notify failure affect the registration this function just made real.
    try:
        from . import connect_link_notify
        connect_link_notify.notify_new_gym(base_key, gym_id, name, alert=alert)
    except Exception as exc:  # noqa: BLE001 - the gym is registered either way
        if alert:
            alert(f"{base_key}: auto connect-link notify crashed "
                  f"({type(exc).__name__}: {exc}). Send the connect link by hand.")
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
    # THE GATE (2026-09-11). Both rosters below are re-checked against the Echo client
    # universe here, in the loop, even though the live readers already filter: a fake
    # or future reader that forgets to must still be unable to page the fleet.
    is_client = d.get("is_client") or echo_clients.is_echo_client
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
        if not (is_client(gym_id) or is_client(base_key)):
            continue                      # not an Echo client: no check, no alert
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

    # THE BLIND SPOT: everything above iterates `roster`, which is entirely
    # echo_intake_tokens rows. A gym with none never appears in that loop no matter what
    # is wrong with it. Sweep the portal's own gyms table for exactly that gap, keyed by
    # gym_id (there is no minted account key yet to check_gym() against). Best-effort:
    # a reader failure here never blocks the per-key alerts above, and a bad gym row
    # never blocks another gym's alert.
    zero_token_reader = d.get("zero_token", zero_token_gyms)
    try:
        known_ids = {gym_id for gym_id, _ in roster}
        missing = zero_token_reader(known_ids, http)
    except Exception:  # noqa: BLE001
        missing = []
    for gym_id, name, slug in missing:
        label = slug or name or gym_id
        if not is_client_gym(label):
            continue
        if not is_client(gym_id):
            continue                      # an ads-only gym with no Echo token is normal
        stamp = f"onboarding_watch_notoken_{gym_id}_{day}"
        try:
            if kv.get(stamp, ""):
                continue                      # already said this today
        except Exception:  # noqa: BLE001
            pass
        alert(f"{name or label} ({gym_id}): not set up to post "
              f"({REASON_NO_INTAKE_TOKEN}). {_fix_for(REASON_NO_INTAKE_TOKEN, label)}")
        try:
            kv.set(stamp, "alerted")
        except Exception:  # noqa: BLE001
            pass
        out[label] = [REASON_NO_INTAKE_TOKEN]
    return out


def _live_deps():
    """The real readers. Split out so run()/check_gym() are fully injectable.
    `is_client` is the Echo client universe predicate (echo_clients.is_echo_client);
    every roster row and every action goes through it."""
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
            "platforms": _platforms, "fb_page": _fb_page, "gym_name": _gym_name,
            "zero_token": zero_token_gyms,
            "is_client": echo_clients.is_echo_client}
