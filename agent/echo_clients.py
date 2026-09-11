"""
echo_clients.py — THE Echo client universe. One predicate, used by every lane that
enumerates gyms.

THE INCIDENT THIS CLOSES (2026-09-11, 12:03-12:06 UTC)
------------------------------------------------------
onboarding_watch treated `echo_intake_tokens` as "the AUTHORITATIVE list of Echo
clients". It is not. The portal mints an Echo intake token for EVERY gym it knows
(131 rows that morning, one per portal gym), so the onboarding lane swept the whole
LASSO ads fleet: it auto-registered ~110 non-clients into gym_accounts.json, sent
connect_link_notify's "Hey <name>, this is Echo ... here is your connect link" Slack
group DM to 36 gym owners who never bought Echo (they are Launch / Ascend / Apex ads
clients), ran website-intake against 131 sites, and minted 153 "not set up to post"
/ "no single client_owner email" ops tickets for gyms that were never supposed to
post anything.

THE RULE
--------
    echo_gym_settings IS the Echo client universe.  echo_intake_tokens IS NOT.

Verified against the live portal DB the same day: echo_gym_settings held exactly the
20 Echo clients (Chateau, Local, Newtown, Nine 7, Reverb, Sunnyside, Zanshin,
District H, ENG, GRITX, Hill Country, LASSO, MFLH, Pierce, Swift River, Bolton Club,
Top Fuel, Tough Temple, Train716, ZZ Test Gym); echo_intake_tokens held 131 gyms;
gyms.plan was 'full' for all 158 rows, gym_products carried only the 'ads' product,
and gym_billing.tier is the ADS tier. No other positive Echo marker exists in the
schema, so a row in echo_gym_settings is the strictest predicate that still includes
all 20 and excludes every one of the 36.

WHAT THIS MODULE ANSWERS
------------------------
    is_echo_client(gym_id | base_key | account_key)  -> bool
    echo_client_keys()                               -> frozenset of client base keys
    echo_client_gym_ids()                            -> frozenset of client gym UUIDs
    only_clients(idents) / only_client_bases(bases)  -> the client subset, order kept

A gym is matched by its portal UUID, or by ANY base key that gym has ever legitimately
held: the echo_intake_tokens key (both rows, when a gym is split), the Echo-derived key
(slug + sha256(gym_id)[:6], account_key._base_key), the portal-derived key (slug +
rawUUID[:6], social-onboard.ts), the gyms.slug, and the bare name slug (the legacy
hand keys: crossfitlocal, hillcountry, theboltonclub, districth ...). Every alias is
computed FROM a client gym's own identity, so an alias can only ever widen the match
onto a gym that IS a client — it can never admit a non-client.

FAIL CLOSED. On any read error, missing creds, truncated page, or an EMPTY
echo_gym_settings (an empty client universe is far more likely to be a broken read
than the truth), the answer is "not a client" and every fleet lane does nothing. A
failed read is retried after _TTL_FAIL seconds; a good read is cached _TTL_OK (5 min).
The hardcoded accounts.ACCOUNTS bases are the one exception (see only_client_bases):
a human wrote those into the repo, and LASSO's own daily run must never depend on a
Supabase round trip.

The service key is read lazily, never logged, never returned. `http` is injectable so
every path is unit tested offline.
"""
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field

from . import config

_TTL_OK = 300.0        # a good snapshot is believed for 5 minutes (the spec's ceiling)
_TTL_FAIL = 20.0       # a failed read is retried soon, but not on every call
_READ_TIMEOUT = 15
_PAGE = 1000           # PostgREST's default row cap; page explicitly so truncation shows
_MAX_PAGES = 20

_SUFFIXES = ("_ig", "_fb")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ID_FINGERPRINT_LEN = 6
_NAME_SLUG_MAXLEN = 24   # mirrors account_key._NAME_SLUG_MAXLEN and the portal's cap


@dataclass(frozen=True)
class ClientSet:
    """One reading of the client universe. `ok=False` means UNKNOWN, and every
    membership test on an unknown set answers False (fail closed)."""
    ok: bool
    gym_ids: frozenset = frozenset()
    keys: frozenset = frozenset()          # every base key belonging to a client
    key_to_gym: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)      # gym_id -> portal name
    other_keys: frozenset = frozenset()    # token keys of gyms that are NOT clients
    other_gym_ids: frozenset = frozenset() # portal gym ids that are NOT clients
    error: str = ""
    at: float = 0.0

    def is_client(self, ident):
        if not self.ok:
            return False
        n = normalize_key(ident)
        if not n:
            return False
        return n in self.gym_ids or n in self.keys


def normalize_key(ident):
    """Lowercase, stripped, platform suffix removed: 'CrossFitLocal_ig' -> 'crossfitlocal'.
    A UUID normalises to its lowercase form."""
    n = str(ident or "").strip().lower()
    for suf in _SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)]
            break
    return n


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "", str(name or "").strip().lower())
    return s[:_NAME_SLUG_MAXLEN]


def _echo_key(gym_id, name):
    """Echo's derivation (account_key._base_key): slug + sha256(id)[:6]."""
    s = _slug(name)
    if not s:
        return ""
    return s + hashlib.sha256(str(gym_id or "").strip().encode("utf-8")).hexdigest()[:_ID_FINGERPRINT_LEN]


def _portal_key(gym_id, name):
    """The portal's derivation (social-onboard.ts deriveAccountKey): slug + rawUUID[:6]."""
    s = _slug(name)
    raw = str(gym_id or "").strip().lower().replace("-", "")
    if not s or len(raw) < _ID_FINGERPRINT_LEN:
        return ""
    return s + raw[:_ID_FINGERPRINT_LEN]


def build(settings_rows, token_rows, gym_rows, *, now=None):
    """PURE: assemble a ClientSet from the three table readings. Exposed so a test (or
    an operator with SQL access and no service key) can evaluate the exact predicate
    over real rows."""
    gym_ids = set()
    for r in settings_rows or []:
        gid = normalize_key(r.get("gym_id"))
        if gid:
            gym_ids.add(gid)
    if not gym_ids:
        return ClientSet(ok=False, error="echo_gym_settings returned zero rows; refusing "
                         "to believe an empty client universe", at=now or time.time())

    names, slugs, raw_ids = {}, {}, {}
    for g in gym_rows or []:
        gid = normalize_key(g.get("id"))
        if gid in gym_ids:
            names[gid] = str(g.get("name") or "").strip()
            slugs[gid] = str(g.get("slug") or "").strip().lower()
            raw_ids[gid] = str(g.get("id") or "").strip()

    keys, key_to_gym, other, other_ids = set(), {}, set(), set()
    for t in token_rows or []:
        gid = normalize_key(t.get("gym_id"))
        key = normalize_key(t.get("echo_account_key"))
        if gid and gid not in gym_ids:
            other_ids.add(gid)
        if not key:
            continue
        if gid in gym_ids:
            keys.add(key)
            key_to_gym[key] = gid
        else:
            other.add(key)
    for g in gym_rows or []:
        gid = normalize_key(g.get("id"))
        if gid and gid not in gym_ids:
            other_ids.add(gid)

    for gid in gym_ids:
        name = names.get(gid, "")
        aliases = {
            _echo_key(raw_ids.get(gid, gid), name),
            _portal_key(raw_ids.get(gid, gid), name),
            slugs.get(gid, ""),
            _slug(name),
        }
        for a in aliases:
            a = normalize_key(a)
            if a:
                keys.add(a)
                key_to_gym.setdefault(a, gid)
    # A key both a client and a non-client hold cannot be a non-client marker.
    other -= keys
    return ClientSet(ok=True, gym_ids=frozenset(gym_ids), keys=frozenset(keys),
                     key_to_gym=key_to_gym, names=names, other_keys=frozenset(other),
                     other_gym_ids=frozenset(other_ids), at=now or time.time())


# ---- live reads ------------------------------------------------------------------

def _rest_get(http, url, key, path, params):
    """One PostgREST GET. Returns (rows, ok). Never raises."""
    try:
        r = http.get(f"{url}/rest/v1/{path}", params=params,
                     headers={"apikey": key, "Authorization": f"Bearer {key}",
                              "Accept": "application/json"},
                     timeout=_READ_TIMEOUT)
    except Exception:  # noqa: BLE001 - a transport failure is an unknown universe
        return [], False
    if getattr(r, "status_code", 599) >= 400:
        return [], False
    try:
        rows = r.json()
    except Exception:  # noqa: BLE001
        return [], False
    if not isinstance(rows, list):
        return [], False
    return rows, True


def _read_all(http, url, key, path, select, extra=None):
    """Every row of `path`, paged. (rows, complete). A page that fails, or a table that
    keeps going past _MAX_PAGES, is reported INCOMPLETE and the caller fails closed."""
    out = []
    for page in range(_MAX_PAGES):
        params = {"select": select, "limit": str(_PAGE), "offset": str(page * _PAGE)}
        if extra:
            params.update(extra)
        rows, ok = _rest_get(http, url, key, path, params)
        if not ok:
            return out, False
        out.extend(rows)
        if len(rows) < _PAGE:
            return out, True
    return out, False


def _load(http=None, now=None):
    """Read the plane and build the set. Never raises; failure = ClientSet(ok=False)."""
    url = config.supabase_url()
    key = config.supabase_service_key()
    stamp = now or time.time()
    if not url or not key:
        return ClientSet(ok=False, error="no Supabase creds", at=stamp)
    if http is None:
        import requests  # lazy, matches the rest of the repo
        http = requests
    settings, ok = _read_all(http, url, key, "echo_gym_settings", "gym_id")
    if not ok:
        return ClientSet(ok=False, error="echo_gym_settings unreadable", at=stamp)
    tokens, ok = _read_all(http, url, key, "echo_intake_tokens", "gym_id,echo_account_key")
    if not ok:
        return ClientSet(ok=False, error="echo_intake_tokens unreadable", at=stamp)
    ids = sorted({str(r.get("gym_id") or "").strip() for r in settings
                  if str(r.get("gym_id") or "").strip()})
    if not ids:
        return build(settings, tokens, [], now=stamp)   # -> ok=False, empty universe
    # Only the CLIENT gyms' rows are needed for aliases; the non-client id set is
    # complete from the token rows (every registry / echo_gyms entry the incident
    # created came off that roster).
    gyms, ok = _read_all(http, url, key, "gyms", "id,name,slug",
                         {"id": f"in.({','.join(ids)})"})
    if not ok:
        return ClientSet(ok=False, error="gyms unreadable", at=stamp)
    return build(settings, tokens, gyms, now=stamp)


# ---- cache -----------------------------------------------------------------------

_lock = threading.Lock()
_cache = {"set": None}
_override = None   # tests only: callable(ident) -> bool, installed by set_test_override


def snapshot(http=None, fresh=False, now_fn=None):
    """The cached ClientSet, re-read when stale. Thread-safe; losers of a cold race take
    the winner's reading. A FAILED reading is cached only _TTL_FAIL seconds."""
    clock = now_fn or time.time
    with _lock:
        cur = _cache["set"]
        if cur is not None and not fresh:
            ttl = _TTL_OK if cur.ok else _TTL_FAIL
            if clock() - cur.at < ttl:
                return cur
        new = _load(http=http, now=clock())
        _cache["set"] = new
        return new


def is_echo_client(ident, *, http=None):
    """True iff `ident` (a portal gym UUID, a base key, or an _ig/_fb account key) is one
    of Echo's clients. False on ANY uncertainty."""
    if _override is not None:
        return bool(_override(ident))
    try:
        return snapshot(http=http).is_client(ident)
    except Exception:  # noqa: BLE001 - never raise out of a gate; unknown = not a client
        return False


def echo_client_gym_ids(http=None):
    try:
        s = snapshot(http=http)
    except Exception:  # noqa: BLE001
        return frozenset()
    return s.gym_ids if s.ok else frozenset()


def echo_client_keys(http=None):
    try:
        s = snapshot(http=http)
    except Exception:  # noqa: BLE001
        return frozenset()
    return s.keys if s.ok else frozenset()


def only_clients(idents, *, http=None):
    """The subset of `idents` that are Echo clients, order preserved."""
    return [i for i in (idents or []) if is_echo_client(i, http=http)]


def hardcoded_bases():
    """Base keys of accounts.ACCOUNTS: written into the repo by a human, trusted
    without a plane read (LASSO's own run never waits on Supabase)."""
    from .accounts import ACCOUNTS
    return {normalize_key(a.key) for a in ACCOUNTS if a.key}


def registry_gym_ids():
    """{base: gym_id} from the dynamic registry rows that carry a gym_id stamp."""
    try:
        from .accounts import _load_registry_rows
        rows = _load_registry_rows()
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for r in rows or []:
        base = normalize_key(r.get("base"))
        gid = str(r.get("gym_id") or "").strip()
        if base and gid:
            out[base] = gid
    return out


def is_client_base(base, *, hard=None, gym_ids=None, http=None):
    """The ACCOUNT-REGISTRY form of the predicate: a hardcoded base is always a client;
    a dynamic-registry base is a client when its stamped gym_id or its key is one."""
    n = normalize_key(base)
    if not n:
        return False
    if n in (hard if hard is not None else hardcoded_bases()):
        return True
    gid = (gym_ids if gym_ids is not None else registry_gym_ids()).get(n, "")
    if gid and is_echo_client(gid, http=http):
        return True
    return is_echo_client(n, http=http)


def only_client_bases(bases, *, http=None):
    """Filter a list of registry bases to Echo clients, order preserved. This is the
    gate every fleet enumerator over the account registry goes through
    (client_media_sync._client_bases, calendar_autopublish.client_gym_bases)."""
    hard = hardcoded_bases()
    gids = registry_gym_ids()
    return [b for b in (bases or []) if is_client_base(b, hard=hard, gym_ids=gids, http=http)]


def reset_cache():
    """Tests only."""
    with _lock:
        _cache["set"] = None


def set_test_override(fn):
    """Tests only: install callable(ident)->bool as the predicate (None restores the
    live reader). The suite runs offline with no creds, where the honest answer is
    'unknown = not a client'; the conftest installs an allow-all override so lanes
    that were never about this gate keep testing what they were written to test,
    and the gate's own tests switch it off."""
    global _override
    _override = fn
