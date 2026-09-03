"""
connect_link_notify.py — the moment a new gym is auto-registered, hand its owner a
working connect link, instead of leaving that as an unautomated human step.

WHY THIS EXISTS (2026-09-03): onboarding_watch.autoregister() closes "who puts this
gym in the account registry", but a DIFFERENT gap sat right behind it: nobody ever
automated the FIRST send of a gym's connect link. register_gym's own docstring says
"Tokens are NEVER written here (env, by hand)" -- true of the registry row, and it
turned out to be true of the notification too. Verified live: five gyms (CrossFit
Sunnyside, CrossFit Local, CrossFit Newtown, District H, MFLH) each had a real
Zernio profile and ZERO connection attempts EVER logged in Zernio's 90-day retained
activity log -- not a broken connect flow (it worked for five OTHER gyms that same
week), just nothing that ever prompted the owner to click it.

Rails:
  - OFF by default (config.auto_connect_link_enabled()).
  - Fires ONCE per gym, ever (kv-stamped `connect_link_sent_<base_key>`). A later
    autoregister call for the same gym (idempotent, can run repeatedly) never
    re-sends.
  - Never invents a contact. The owner is resolved from the portal's OWN records
    (gym_assignments joined to app_users, relationship='client_owner'); zero or
    ambiguous matches, or no matching Slack account, ESCALATES via a NEEDS_TRIAGE
    ops alert instead of silently doing nothing -- a silent gap here would just
    recreate the exact bug this module exists to close.
  - Sends as ECHO (the shared Slack bot token AGENT_SLACK_BOT_TOKEN), in a fresh
    group DM with the LASSO approver (config.APPROVER_SLACK_ID) included -- the same
    manual process used for all five gyms on 2026-09-03.
  - The message is a fixed template naming only the gym and its link. No invented
    facts, no client content, nothing that could ever need scrubbing.
"""
import json
import os
import urllib.error as _ue
import urllib.parse as _up
import urllib.request as _ur

from . import config

_CONNECT_MESSAGE = (
    "Hey {name}, this is Echo.\n\n"
    "Your account for {gym} is set up on our side, but Facebook and Instagram have "
    "not been connected yet, so there is nothing for us to post to.\n\n"
    "Here is your connect link. Click it and follow the steps for both:\n\n"
    "{link}\n\n"
    "Make sure you are logged into the Facebook account that manages your gym's "
    "Page, and that Instagram is a Business or Creator account linked to that same "
    "Page.\n\n"
    "Let us know here once it is done, or if anything looks off partway through."
)


class _Http:
    """Minimal stdlib GET/POST adapter, same shape as slack_surface's -- no extra
    dependency, and every call site is injectable for tests."""

    def get(self, url, headers=None, timeout=30):
        req = _ur.Request(url, headers=headers or {})
        try:
            with _ur.urlopen(req, timeout=timeout) as r:
                return _Resp(r.status, r.read())
        except _ue.HTTPError as e:
            return _Resp(e.code, e.read())

    def post(self, url, headers=None, data=None, timeout=30):
        body = data.encode() if isinstance(data, str) else data
        req = _ur.Request(url, data=body, headers=headers or {}, method="POST")
        try:
            with _ur.urlopen(req, timeout=timeout) as r:
                return _Resp(r.status, r.read())
        except _ue.HTTPError as e:
            return _Resp(e.code, e.read())


class _Resp:
    def __init__(self, status_code, body_bytes):
        self.status_code = status_code
        self._body = body_bytes

    def json(self):
        return json.loads(self._body.decode())


def _http(http=None):
    return http or _Http()


def _rest_get(path, params, *, url, key, http=None):
    """One-off Supabase PostgREST GET, mirroring portal_calendar_store's own pattern.
    Returns None on any failure -- a read problem here must ESCALATE, never guess or
    raise into the caller."""
    q = _up.urlencode(params)
    try:
        resp = _http(http).get(
            f"{url}/rest/v1/{path}?{q}",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                    "Accept": "application/json"})
    except Exception:
        return None
    if getattr(resp, "status_code", 599) >= 400:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def resolve_owner_email(gym_id, *, http=None):
    """The gym's client_owner email, resolved from the portal's OWN records
    (gym_assignments joined to app_users) -- never a guess. Returns None when the
    read fails, or when there is zero or more than one client_owner on file: an
    ambiguous match is exactly as unusable as no match, and picking one would be
    the same kind of fabrication this module refuses everywhere else."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key or not gym_id:
        return None
    assignments = _rest_get(
        "gym_assignments",
        {"gym_id": f"eq.{gym_id}", "relationship": "eq.client_owner",
         "select": "app_user_id"},
        url=url, key=key, http=http)
    if not assignments:
        return None
    user_ids = sorted({a.get("app_user_id") for a in assignments if a.get("app_user_id")})
    if len(user_ids) != 1:
        return None
    users = _rest_get("app_users", {"id": f"eq.{user_ids[0]}", "select": "email"},
                      url=url, key=key, http=http)
    if not users:
        return None
    email = (users[0].get("email") or "").strip()
    return email or None


def _slack_lookup_email(email, *, token, http=None):
    try:
        resp = _http(http).get(
            "https://slack.com/api/users.lookupByEmail?" +
            _up.urlencode({"email": email}),
            headers={"Authorization": f"Bearer {token}"})
        body = resp.json()
    except Exception:
        return None
    if not body.get("ok"):
        return None
    return (body.get("user") or {}).get("id") or None


def _slack_open_dm(user_ids, *, token, http=None):
    try:
        resp = _http(http).post(
            "https://slack.com/api/conversations.open",
            headers={"Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"},
            data=json.dumps({"users": ",".join(user_ids)}))
        body = resp.json()
    except Exception:
        return None
    if not body.get("ok"):
        return None
    return (body.get("channel") or {}).get("id") or None


def _slack_send(channel, text, *, token, http=None):
    try:
        resp = _http(http).post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"},
            data=json.dumps({"channel": channel, "text": text}))
        body = resp.json()
    except Exception:
        return False
    return bool(body.get("ok"))


def notify_new_gym(base_key, gym_id, gym_name, *, db=None, http=None, alert=None):
    """Send a newly-registered gym's owner its connect link, once. Returns True only
    when a message was actually sent this call. OFF unless
    config.auto_connect_link_enabled(). Never raises; every failure path ESCALATES
    via `alert` (NEEDS_TRIAGE by ops_triage's fail-safe default) rather than doing
    nothing silently -- a silent gap here is precisely the bug this module exists to
    close."""
    if not config.auto_connect_link_enabled():
        return False
    base_key = str(base_key or "").strip()
    gym_name = str(gym_name or "").strip()
    if not base_key or not gym_name:
        return False
    if db is None:
        from . import db as db
    if alert is None:
        from .ops_alerts import alert as alert

    dedupe_key = f"connect_link_sent_{base_key}"
    try:
        if db.kv_get(dedupe_key):
            return False
    except Exception:
        pass  # a dedupe READ failure must not block a first-ever send

    token = os.environ.get(config.SLACK_BOT_TOKEN_ENV, "")
    if not token:
        alert(f"auto connect-link for {base_key}: no Slack bot token configured; "
              "cannot send. Send the connect link by hand "
              f"(python -m agent intake-link --account {base_key}).")
        return False

    try:
        email = resolve_owner_email(gym_id, http=http)
    except Exception as e:  # noqa: BLE001 - a lookup bug must escalate, not crash the caller
        alert(f"auto connect-link for {base_key}: owner lookup failed "
              f"({type(e).__name__}). Send the connect link by hand "
              f"(python -m agent intake-link --account {base_key}).")
        return False
    if not email:
        alert(f"auto connect-link for {base_key}: no single client_owner email found "
              "in the portal's own records (zero or more than one on file). Send the "
              f"connect link by hand (python -m agent intake-link --account "
              f"{base_key}) and add the owner's contact so this sends on its own "
              "next time.")
        return False

    owner_id = _slack_lookup_email(email, token=token, http=http)
    if not owner_id:
        alert(f"auto connect-link for {base_key}: owner email {email} has no "
              "matching Slack account. Send the connect link by hand "
              f"(python -m agent intake-link --account {base_key}).")
        return False

    try:
        from .intake_web import link_for
        link = link_for(base_key, kind="connect")
    except Exception as e:  # noqa: BLE001
        link = ""
    if not link:
        alert(f"auto connect-link for {base_key}: could not mint a connect link "
              "(AGENT_INTAKE_SIGNING_SECRET may be unset on this service). Send it "
              "by hand once the secret is set.")
        return False

    approver = config.APPROVER_SLACK_ID
    channel = _slack_open_dm([approver, owner_id], token=token, http=http)
    if not channel:
        alert(f"auto connect-link for {base_key}: could not open a Slack DM with "
              f"{email}. Send the connect link by hand: {link}")
        return False

    text = _CONNECT_MESSAGE.format(name=email.split("@")[0], gym=gym_name, link=link)
    if not _slack_send(channel, text, token=token, http=http):
        alert(f"auto connect-link for {base_key}: Slack DM to {email} failed to "
              f"send. Send the connect link by hand: {link}")
        return False

    try:
        db.kv_set(dedupe_key, "1")
    except Exception:
        pass  # the message is already sent; a stamp failure risks one resend, not silence
    return True
