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

THE MARKERS (audit ruling, 2026-09-11 round 2). A gym is an Echo client when its
gym_id carries ANY of these four positive markers. Each is something only an Echo
purchase / an Echo owner action creates:

  1. echo_gym_settings row      -- written by the /my cadence + autonomy toggles
                                   (portal echo-cadence.ts / echo-autonomy.ts) and by
                                   Echo's set_gym_zernio_profile_id at CONNECT time
                                   (portal_calendar_store <- zernio_routes), and, once
                                   the parallel portal PR lands, by the per-gym Echo
                                   onboard button. Live: 20 gyms.
  2. echo_social_intake row     -- the owner's OWN Echo (DFY social) intake submission,
                                   keyed by gym_id / client_key. THE DAY-ONE MARKER:
                                   without it a new client is not a client until the
                                   owner connects, connect_link_notify can never fire
                                   for the gym it exists for (bootstrap deadlock), and
                                   onboarding_watch is blind to never-connected clients.
                                   Live: 16 gyms, all of them already in (1); 0
                                   non-clients.
  3. gym_products               -- product in ('social', 'echo_social'), status active:
                                   the standalone Stripe lane's product marker. Live: 0
                                   rows (today the table carries only 'ads').
  4. gyms.plan = 'echo_standalone' -- the standalone lane's plan marker. Live: 0 rows
                                   (every row is 'full').

NOT MARKERS, and why:
  * echo_intake_tokens         -- the portal mints one for EVERY gym it knows (131 rows
                                  for 158 gyms). It is a capability token, not a purchase.
                                  Sweeping it is exactly the incident. It is read here
                                  ONLY to learn a client's minted key (an alias).
  * gym_billing.tier           -- LAUNCH / ASCEND / APEX is the ADS tier.
  * gyms.plan = 'full'         -- every gym has it; it says nothing about Echo.

Verified live: the four markers together yield the same 20 gyms as (1) alone today,
exclude all 112 non-client token keys, all 36 DMed gyms, and Empire Training Academy.

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
    markers: dict = field(default_factory=dict)    # gym_id -> frozenset of marker names
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


MARKER_SETTINGS = "echo_gym_settings"
MARKER_INTAKE = "echo_social_intake"
MARKER_PRODUCT = "gym_products:social"
MARKER_PLAN = "gyms.plan=echo_standalone"
MARKERS = (MARKER_SETTINGS, MARKER_INTAKE, MARKER_PRODUCT, MARKER_PLAN)
SOCIAL_PRODUCTS = ("social", "echo_social")
STANDALONE_PLAN = "echo_standalone"


def build(settings_rows, token_rows, gym_rows, *, intake_rows=(), product_rows=(),
          standalone_rows=(), now=None):
    """PURE: assemble a ClientSet from the table readings. Exposed so a test (or an
    operator with SQL access and no service key) can evaluate the exact predicate over
    real rows.

    settings_rows   echo_gym_settings(gym_id)                       -> marker 1
    intake_rows     echo_social_intake(gym_id, client_key,
                    echo_account_key)                               -> marker 2
    product_rows    gym_products(gym_id, product, status)           -> marker 3
    standalone_rows gyms(id, name, slug) WHERE plan=echo_standalone -> marker 4
    token_rows      echo_intake_tokens(gym_id, echo_account_key)    -> aliases ONLY
    gym_rows        gyms(id, name, slug) for the client ids         -> aliases ONLY
    """
    gym_ids, markers = set(), {}

    def _mark(gid, marker):
        gid = normalize_key(gid)
        if not gid:
            return
        gym_ids.add(gid)
        markers.setdefault(gid, set()).add(marker)

    for r in settings_rows or []:
        _mark(r.get("gym_id"), MARKER_SETTINGS)
    intake_aliases = {}
    for r in intake_rows or []:
        gid = normalize_key(r.get("gym_id"))
        ck = normalize_key(r.get("client_key"))
        if not gid and _UUID_RE.match(ck or ""):
            gid = ck                       # older rows carry the UUID as client_key
        if not gid:
            continue
        _mark(gid, MARKER_INTAKE)
        for alias in (ck, normalize_key(r.get("echo_account_key"))):
            if alias and not _UUID_RE.match(alias):
                intake_aliases.setdefault(gid, set()).add(alias)
    for r in product_rows or []:
        product = str(r.get("product") or "").strip().lower()
        status = str(r.get("status") or "").strip().lower()
        if product in SOCIAL_PRODUCTS and status == "active":
            _mark(r.get("gym_id"), MARKER_PRODUCT)
    for r in standalone_rows or []:
        if str(r.get("plan") or STANDALONE_PLAN).strip().lower() == STANDALONE_PLAN:
            _mark(r.get("id"), MARKER_PLAN)
    if not gym_ids:
        return ClientSet(ok=False, error="every Echo marker table returned zero rows; "
                         "refusing to believe an empty client universe", at=now or time.time())

    names, slugs, raw_ids = {}, {}, {}
    for g in list(gym_rows or []) + list(standalone_rows or []):
        gid = normalize_key(g.get("id"))
        if gid in gym_ids:
            names[gid] = str(g.get("name") or "").strip()
            slugs[gid] = str(g.get("slug") or "").strip().lower()
            raw_ids[gid] = str(g.get("id") or "").strip()

    keys, key_to_gym, other, other_ids = set(), {}, set(), set()
    for gid, aliases in intake_aliases.items():
        for a in aliases:
            keys.add(a)
            key_to_gym.setdefault(a, gid)
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
    other_ids -= gym_ids
    return ClientSet(ok=True, gym_ids=frozenset(gym_ids), keys=frozenset(keys),
                     key_to_gym=key_to_gym, names=names, other_keys=frozenset(other),
                     other_gym_ids=frozenset(other_ids),
                     markers={g: frozenset(m) for g, m in markers.items()},
                     at=now or time.time())


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
    # The four MARKER reads. Any one failing = the universe is unknown = fail closed.
    settings, ok = _read_all(http, url, key, "echo_gym_settings", "gym_id")
    if not ok:
        return ClientSet(ok=False, error="echo_gym_settings unreadable", at=stamp)
    intake, ok = _read_all(http, url, key, "echo_social_intake",
                           "gym_id,client_key,echo_account_key")
    if not ok:
        return ClientSet(ok=False, error="echo_social_intake unreadable", at=stamp)
    products, ok = _read_all(http, url, key, "gym_products", "gym_id,product,status",
                             {"product": f"in.({','.join(SOCIAL_PRODUCTS)})"})
    if not ok:
        return ClientSet(ok=False, error="gym_products unreadable", at=stamp)
    standalone, ok = _read_all(http, url, key, "gyms", "id,name,slug,plan",
                               {"plan": f"eq.{STANDALONE_PLAN}"})
    if not ok:
        return ClientSet(ok=False, error="gyms (plan) unreadable", at=stamp)
    # ALIAS reads: the token table is NOT a marker (see the module docstring); it is
    # read only so a client's minted key(s) resolve to the client.
    tokens, ok = _read_all(http, url, key, "echo_intake_tokens", "gym_id,echo_account_key")
    if not ok:
        return ClientSet(ok=False, error="echo_intake_tokens unreadable", at=stamp)
    probe = build(settings, tokens, [], intake_rows=intake, product_rows=products,
                  standalone_rows=standalone, now=stamp)
    if not probe.ok:
        return probe                                    # empty universe -> refused
    # Only the CLIENT gyms' rows are needed for aliases; the non-client id set is
    # complete from the token rows (every registry / echo_gyms entry the incident
    # created came off that roster).
    ids = sorted(probe.gym_ids)
    gyms, ok = _read_all(http, url, key, "gyms", "id,name,slug",
                         {"id": f"in.({','.join(ids)})"})
    if not ok:
        return ClientSet(ok=False, error="gyms unreadable", at=stamp)
    return build(settings, tokens, gyms, intake_rows=intake, product_rows=products,
                 standalone_rows=standalone, now=stamp)


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
