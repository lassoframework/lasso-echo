"""
account_key_resolve.py — map an account key a gym's LINK carries onto the key that gym's
data actually lives under, by IDENTITY rather than by resemblance.

WHY THIS MODULE REPLACED A NAME-SLUG HEURISTIC (audit, 2026-09-04)

One gym can hold two account keys, because two systems derive keys differently from the
same gym_id: the portal mints `slug + rawUUID[:6]` (social-onboard.ts deriveAccountKey)
while Echo derives `slug + sha256(gym_id)[:6]` (account_key._base_key). A signed link
self-decodes whatever key it was MINTED with, so a gym can arrive under a key nothing else
reads. That is the bug this resolves.

The first attempt matched by NAME-SLUG: take the part before the 6-hex tail and remap when
exactly one known key shares it. An adversarial audit showed why that is unsafe once it is
wired into the token boundary, where it governs WRITES (media bind/disconnect, calendar
approve/deny/kill, uploads):

  * The ambiguity guard was conditioned on the completeness of the very set whose
    incompleteness is the trigger. With gyms `crossfitlocal1a2b3c` and `crossfitlocal9f8e7d`,
    if the second is momentarily absent from the set (cache window after a re-key, a blank
    column, a truncated read, replica lag) then a request carrying ITS OWN LIVE KEY was
    remapped onto the FIRST gym. A live key must never be rewritten, and a write must never
    land on another tenant.
  * The <slug><6hex> shape is a heuristic, not a guarantee: `a`-`f` are letters, so a gym
    legitimately keyed `crossfitdecade` parses as slug `crossfit` + `decade` and became
    eligible for exactly that misroute.

So this module never matches on resemblance. It COMPUTES, for each gym, the Echo-derived
key that gym would have, and maps that exact string onto the portal key that gym's data
lives under. The mapping is anchored on gym_id, so it is injective by construction and
needs no ambiguity guard at all.

FAIL-CLOSED IS THE ONLY SAFE DIRECTION. Every uncertainty -- an unreadable plane, a
truncated page, a gym whose rows disagree, a key that maps nowhere -- returns the key
UNCHANGED, which is exactly the pre-fix behaviour. Remapping is only ever done on an exact
match against a complete, unambiguous reading.
"""
import threading
import time

from . import config

# The plane changes at onboarding speed, not request speed. This sits in the auth path, so
# it is cached; a FAILED read is cached far more briefly so a Supabase brownout cannot turn
# every tokened request into a fresh 8s round trip, while still recovering quickly.
_TTL_OK = 300.0
_TTL_FAIL = 20.0
_PAGE = 1000          # PostgREST caps responses; page explicitly so truncation is visible
_MAX_PAGES = 20       # 20k gyms is far beyond the fleet; hitting it means "unknown"
_READ_TIMEOUT = 8     # bounded: this runs before a request is served
_BUILD_BUDGET = 25.0  # total seconds ONE rebuild may spend across ALL pages of BOTH tables

# state: {"at": float, "ok": bool, "live": frozenset, "map": dict}
_lock = threading.Lock()
_cache = {"at": None, "ok": False, "live": frozenset(), "map": {}, "by_gym": {},
          "last_fail": None}


def _norm(value):
    return str(value or "").strip().lower()


def _get(path, params):
    """One bounded read of the shared plane. Returns (rows, ok). ok=False on any failure or
    absent creds, so the caller can refuse to remap rather than remap on partial data."""
    url, key = config.supabase_url(), config.supabase_service_key()
    if not url or not key:
        return [], False
    try:
        import requests  # lazy, matches the repo pattern
        r = requests.get(f"{url.rstrip('/')}/rest/v1/{path}", params=params,
                         headers={"apikey": key, "Authorization": f"Bearer {key}",
                                  "Accept": "application/json"},
                         timeout=_READ_TIMEOUT)
        if r.status_code >= 400:
            return [], False
        body = r.json()
        return (body if isinstance(body, list) else []), True
    except Exception as e:  # noqa: BLE001 - never let a plane read break a request
        print(f"[key-resolve] plane read failed ({path}): {type(e).__name__}: {e}")
        return [], False


# The column each table is ordered by. THIS IS NOT COSMETIC (audit, 2026-09-04): the first
# version hardcoded "gym_id.asc" for BOTH reads, but `gyms` keys on `id`, not `gym_id`.
# PostgREST answers an unknown order column with 400 / 42703, _get maps any >=400 to
# ok=False, and the whole resolver therefore returned every key unchanged FOREVER in
# production while thirty tests passed green -- because every one of them injected a fake
# reader that ignored `params`. A paging read must order by a column the table actually
# has, and a test must drive the real parameter construction.
# The order each table is paged by. It must be UNIQUE per row: offset paging over a
# non-unique column has non-deterministic tie order, so past one page a row can be skipped
# entirely -- which would silently drop a live key out of `live` and make it remappable.
# echo_intake_tokens can hold two rows per gym_id (that is the split state this module
# exists for), so it is ordered by the composite.
_ORDER_COLUMN = {"echo_intake_tokens": "gym_id.asc,echo_account_key.asc",
                 "gyms": "id.asc"}


def _read_all(path, select, get=None, now_fn=None, deadline=None):
    """Every row of `path`, paged explicitly. Returns (rows, complete).

    complete=False when a page fails, the page budget is exhausted, or the time budget is
    spent -- a silently truncated read is what made the old ambiguity guard unsound, so
    truncation is surfaced rather than mistaken for "that is all of them"."""
    order = _ORDER_COLUMN.get(path)
    if not order:
        raise KeyError(f"no order column declared for table {path!r}")
    getter = get or _get
    clock = now_fn or time.time
    # The budget is the WHOLE rebuild's, shared across both tables. Computing it per table
    # doubled it silently -- the previous version could spend 2 x _BUILD_BUDGET while
    # holding the rebuild lock, stalling every concurrent tokened request behind it.
    if deadline is None:
        deadline = clock() + _BUILD_BUDGET
    rows, offset = [], 0
    for _ in range(_MAX_PAGES):
        if clock() > deadline:
            return rows, False  # auth path: never spend an unbounded amount of time here
        page, ok = getter(path, {"select": select, "order": order,
                                 "limit": str(_PAGE), "offset": str(offset)})
        if not ok:
            return rows, False
        rows.extend(page or [])
        if len(page or []) < _PAGE:
            return rows, True
        offset += _PAGE
    return rows, False


def _build(get=None, now_fn=None):
    """(live_keys, stale->live map, complete). Pure apart from the injected reader.

    live_keys: every account key the portal considers current, normalised.
    map:       Echo's DERIVED key for a gym -> that gym's live portal key. Computed from
               gym_id, so one entry can only ever belong to one gym.
    """
    clock = now_fn or time.time
    deadline = clock() + _BUILD_BUDGET
    tokens, t_ok = _read_all("echo_intake_tokens", "gym_id,echo_account_key",
                             get=get, now_fn=now_fn, deadline=deadline)
    gyms, g_ok = _read_all("gyms", "id,name", get=get, now_fn=now_fn, deadline=deadline)
    if not (t_ok and g_ok):
        return frozenset(), {}, {}, False

    # A gym_id with more than one token row cannot be resolved: we cannot tell which key is
    # current, and picking wrong sends a write to the wrong place. Drop it from the map (its
    # keys still count as LIVE, so they are returned unchanged).
    # EVERY key any token row carries is LIVE, not just the last one seen. The previous
    # version overwrote by_gym[gid] unconditionally, so a gym with two rows kept only one
    # of its keys in `live` -- the comment claimed both were kept, and that was false. A
    # key that a gym genuinely holds must never be eligible as a remap target.
    by_gym, dupes, all_keys = {}, set(), set()
    for row in tokens:
        gid, key = _norm(row.get("gym_id")), _norm(row.get("echo_account_key"))
        if not gid or not key:
            continue
        all_keys.add(key)
        if gid in by_gym and by_gym[gid] != key:
            dupes.add(gid)
        by_gym[gid] = key

    live = frozenset(all_keys)
    names = {_norm(g.get("id")): g.get("name") for g in gyms}
    # STRIPPED, NOT LOWERCASED. The portal trims the id before hashing
    # (social-onboard.ts: `const id = (gymId ?? "").trim()`), and so does
    # canonical_account_key -- but _base_key, which this module calls DIRECTLY, does not.
    # A padded id in the column would therefore hash differently here than at either mint
    # site: found by the execution pin's padded-id fixture, not by reading. Case is
    # preserved, because both mint sites hash the id's real case.
    raw_ids = {_norm(g.get("id")): str(g.get("id") or "").strip() for g in gyms}

    from .account_key import _base_key, _slugify_name  # noqa: PLC0415 - same package
    mapping, collided = {}, set()
    for gid, live_key in by_gym.items():
        if gid in dupes:
            continue
        name = names.get(gid)
        if not name or not _slugify_name(name):
            # No usable name means no honest derivation. canonical_account_key REJECTS such
            # a gym outright, so a key derived from one could never be a real stale key --
            # deriving anyway invented a bare 6-hex remap source ("67ef44") that nothing
            # could legitimately carry.
            continue
        try:
            derived = _norm(_base_key(raw_ids.get(gid, gid), name))
        except Exception:  # noqa: BLE001 - a underivable gym simply gets no entry
            continue
        if not derived or derived == live_key or derived in live:
            continue  # nothing to map, or the derived key is itself somebody's live key
        if derived in mapping and mapping[derived] != live_key:
            collided.add(derived)  # two gyms deriving one key: refuse both, never guess
        mapping[derived] = live_key
    for key in collided:
        mapping.pop(key, None)
    # A TARGET held by MORE THAN ONE GYM is unsafe, even though its SOURCE is unique.
    # Two gyms genuinely sharing one current key is a real production state (the Bird Dog /
    # Bolton collision this repo's reconciler exists for). That yields {dA: K, dB: K}: two
    # distinct derived sources pointing at one shared key. The moment either gym is re-keyed
    # -- a portal re-onboard, a reconciler run -- its OWN new live key is briefly absent
    # from this cached view, and the old mapping would rewrite it onto the OTHER tenant's
    # key. That is verbatim the failure this module's header uses to disqualify the
    # rejected name-slug design, reintroduced through the target side, and the brownout
    # backoff makes the window unbounded rather than one cache TTL.
    #
    # There is no safe remap onto a key whose owner is ambiguous, so drop those entries.
    # COUNT HOLDERS FROM THE TOKEN ROWS, NOT FROM by_gym. by_gym keeps ONE key per gym
    # (last row wins), so a SPLIT gym that holds the shared key as a non-final row is
    # invisible to the count and its target survives the drop. Executed repro: tenant X
    # holds {sharedkey, boltonclub} and tenant Y holds {sharedkey}; by_gym sees each key
    # once, the mapping keeps -> sharedkey, and after Y is re-keyed, Y's old link resolves
    # onto a key now held by X ALONE. Split gyms are not hypothetical -- Sunnyside, Nine 7
    # and Chateau were all split on prod on 2026-09-04, and this governs writes.
    holders = {}
    for row in tokens:
        gid, key = _norm(row.get("gym_id")), _norm(row.get("echo_account_key"))
        if gid and key:
            holders.setdefault(key, set()).add(gid)
    for src in [k for k, target in mapping.items()
                if len(holders.get(target, ())) > 1]:
        mapping.pop(src, None)
    resolvable = {gid: k for gid, k in by_gym.items() if gid not in dupes}
    return live, mapping, resolvable, True


def _state(now_fn=None, get=None, fresh=False):
    """The cached view of the plane, rebuilding when stale (or when `fresh`).

    Serialised: N concurrent cold requests used to each run a full rebuild. The lock is
    held across the rebuild and re-checks the cache on entry, so the losers of the race
    take the winner's result instead of issuing their own reads.

    A FAILED rebuild never demotes a healthy cache, AND it always records a backoff stamp.
    Retaining the data without the stamp was worse than clobbering it: once the success TTL
    expired during a brownout, every single request re-entered the rebuild behind the lock
    and blocked on two timed-out reads, so requests queued for minutes. Serving the last
    known-good mapping while backing off is better on both axes.

    `fresh` (mint paths only) always attempts a rebuild and reports the REAL outcome, so a
    one-shot permanent decision is never made from stale data."""
    now = (now_fn or time.time)()

    def _cached():
        return _cache["live"], _cache["map"], _cache["by_gym"], _cache["ok"]

    def _within_ttl():
        if _cache["at"] is None:
            return False
        return now - _cache["at"] < (_TTL_OK if _cache["ok"] else _TTL_FAIL)

    def _backing_off():
        return (_cache["last_fail"] is not None
                and now - _cache["last_fail"] < _TTL_FAIL)

    if not fresh and (_within_ttl() or (_backing_off() and _cache["at"] is not None)):
        return _cached()
    with _lock:
        if not fresh and (_within_ttl() or (_backing_off() and _cache["at"] is not None)):
            return _cached()
        try:
            live, mapping, by_gym, ok = _build(get=get, now_fn=now_fn)
        except Exception as e:  # noqa: BLE001
            # A RAISE must back off exactly like a returned failure. Only the `ok=False`
            # path stamped last_fail, so any exception inside _build (a null row in the
            # JSON, a non-str gym name reaching _slugify_name) left no stamp and no cache
            # write -- and every subsequent auth-path request rebuilt again. Measured at
            # 100 requests -> 200 plane reads. That is the wave-4 blocker, reopened
            # through the exception branch.
            print(f"[key-resolve] rebuild raised: {type(e).__name__}: {e}")
            _cache["last_fail"] = now
            if _cache["ok"]:
                return (_cache["live"], _cache["map"], _cache["by_gym"],
                        False if fresh else True)
            _cache.update({"at": now, "ok": False})
            return _cache["live"], _cache["map"], _cache["by_gym"], False
        if not ok:
            _cache["last_fail"] = now
            if _cache["ok"]:
                # Keep serving the last known-good view; the caller that asked for `fresh`
                # still gets an honest False for its own decision.
                return (_cache["live"], _cache["map"], _cache["by_gym"],
                        False if fresh else True)
        else:
            _cache["last_fail"] = None
        _cache.update({"at": now, "ok": ok, "live": live, "map": mapping,
                       "by_gym": by_gym})
        return live, mapping, by_gym, ok


_SUFFIXES = ("_ig", "_fb")


def resolve(account_key, now_fn=None, get=None):
    """`account_key` mapped onto the key its gym's data lives under, or unchanged.

    Unchanged is returned whenever anything is uncertain: an unreadable or truncated plane,
    a key that is already live, a key that maps nowhere, or a derived key two gyms share.

    A platform-suffixed key resolves through its BASE and keeps its suffix
    (crossfitreverb6cdf33_ig -> crossfitreverb30b5b2_ig). account_key_mint deliberately
    preserves those suffixes, so suffixed stale keys genuinely exist and were previously
    left unresolved."""
    whole = _norm(account_key)
    if not whole:
        return account_key
    try:
        live, mapping, _by_gym, ok = _state(now_fn=now_fn, get=get)
    except Exception as e:  # noqa: BLE001 - resolution is a repair, never a gate
        print(f"[key-resolve] unavailable: {type(e).__name__}: {e}")
        return account_key
    if not ok:
        return account_key
    # THE WHOLE KEY IS CHECKED AGAINST live FIRST, BEFORE ANY SUFFIX IS STRIPPED. The
    # suffix feature originally stripped first, which meant a live key STORED WITH a
    # suffix was never protected by "a live key is never rewritten" -- resolve() would
    # strip '_ig', resolve the base, and hand back either a doubled suffix or, in the
    # adversarial case, another tenant's key. account_key_mint re-applies _ig/_fb to the
    # portal key and the portal stores exactly what Echo reports, so such rows are a real
    # write path, not a hypothetical.
    if whole in live:
        return account_key
    key, suffix = whole, ""
    for suf in _SUFFIXES:
        if key.endswith(suf):
            key, suffix = key[: -len(suf)], suf
            break
    if key in live:
        return account_key
    remapped = mapping.get(key)
    return f"{remapped}{suffix}" if remapped else account_key


def portal_key_for_gym(gym_id, now_fn=None, get=None, fresh=False):
    """The account key the PORTAL has already issued to this gym, or "".

    This is the anti-divergence primitive. The portal is where a gym is created and where
    its key is first minted (social-onboard.ts deriveAccountKey); Echo deriving its OWN key
    from the same gym_id is what produced two live keys per gym in the first place. Any
    Echo code that is about to mint or derive a key MUST ask this first and use the answer
    when there is one.

    "" on any uncertainty (unreadable or truncated plane, unknown gym, a gym whose token
    rows disagree), so a caller falls back to its existing behaviour rather than acting on
    a half-read.

    PASS fresh=True FROM A MINT PATH. Minting is a one-shot, permanent decision, and the
    300s success cache is shared with the read path: a cache warmed seconds BEFORE a brand
    new gym was created answers "" for it, the caller falls back to deriving, and the split
    this function exists to prevent is recreated -- exactly the Chateau case. A cache miss
    on a write decision must be re-read, not believed."""
    gid = _norm(gym_id)
    if not gid:
        return ""
    try:
        _live, _map, by_gym, ok = _state(now_fn=now_fn, get=get, fresh=fresh)
    except Exception as e:  # noqa: BLE001
        print(f"[key-resolve] portal key lookup unavailable: {type(e).__name__}: {e}")
        return ""
    return by_gym.get(gid, "") if ok else ""


def reset_cache():
    """Tests only."""
    _cache.update({"at": None, "ok": False, "live": frozenset(), "map": {},
                   "by_gym": {}, "last_fail": None})
