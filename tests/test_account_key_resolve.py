"""Resolving a stale account key by IDENTITY, and the audit that forced the rewrite.

CrossFit Reverb, 2026-09-04. One gym, two keys: the portal mints
`slug + rawUUID[:6]` -> crossfitreverb30b5b2 (where all the data lives) while Echo derives
`slug + sha256(gym_id)[:6]` -> crossfitreverb6cdf33 (what the gym's link carries). Dean saw
"93 posts drafted" over an empty approve list, and a Drive folder that was both
"not connected" and "already connected".

The first fix matched by NAME-SLUG. An adversarial audit killed that design: the ambiguity
guard was conditioned on the completeness of the very set whose incompleteness is the
trigger, so a gym momentarily missing from the known-key set had ITS OWN LIVE KEY rewritten
onto a different gym sharing its slug -- and because resolution now runs at the token
boundary, that governs WRITES (media bind, calendar approve/deny/kill, uploads).

So matching is now by identity: each gym's Echo-derived key is COMPUTED from its gym_id and
mapped onto that gym's live portal key. Injective by construction. Every uncertainty
returns the key unchanged, which is exactly the pre-fix behaviour.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import account_key_resolve as akr  # noqa: E402

# Real production values.
REVERB_ID = "30b5b234-0dac-4048-87d8-5330e6fbfa9d"
REVERB_NAME = "CrossFit Reverb"
REVERB_LIVE = "crossfitreverb30b5b2"     # portal derivation, where the data is
REVERB_STALE = "crossfitreverb6cdf33"    # Echo derivation, what the link carries

# Two gyms that share a name-slug -- the cross-tenant scenario the audit reproduced.
LOCAL_A_ID = "1a2b3c4d-0000-4000-8000-000000000001"
LOCAL_B_ID = "9f8e7d6c-0000-4000-8000-000000000002"


# SCHEMA-FAITHFUL by default. The tables really do key differently -- echo_intake_tokens
# on gym_id, gyms on id -- and PostgREST answers an unknown order column with 400/42703,
# which _get turns into ok=False. A fake that ignores `params` cannot see that, and that
# blind spot shipped a resolver that was a permanent no-op in production while thirty
# tests passed green. So this fake validates the order column the way the database does.
_REAL_COLUMNS = {"echo_intake_tokens": {"gym_id", "echo_account_key"},
                 "gyms": {"id", "name", "slug"}}


def _plane(tokens, gyms, fail=False):
    """A fake reader with _get's contract: (path, params) -> (rows, ok), which REJECTS a
    request the real database would reject."""
    def get(path, params):
        if fail:
            return [], False
        order_col = str(params.get("order", "")).split(".")[0]
        if order_col and order_col not in _REAL_COLUMNS.get(path, set()):
            return [], False   # PostgREST 400: column <path>.<order_col> does not exist
        rows = tokens if path == "echo_intake_tokens" else gyms
        off = int(params.get("offset", 0))
        lim = int(params.get("limit", 1000))
        return rows[off:off + lim], True
    return get


def _derived(gym_id, name):
    from agent.account_key import _base_key
    return _base_key(gym_id, name)


def setup_function(_):
    akr.reset_cache()


def teardown_function(_):
    akr.reset_cache()


# ---- the fix itself --------------------------------------------------------------------

def test_dean_the_stale_key_resolves_to_the_live_one():
    assert _derived(REVERB_ID, REVERB_NAME) == REVERB_STALE, "fixture must match production"
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=get) == REVERB_LIVE


def test_a_live_key_is_returned_untouched():
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.resolve(REVERB_LIVE, now_fn=lambda: 1000.0, get=get) == REVERB_LIVE


def test_an_unknown_key_is_returned_untouched():
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.resolve("somethingelse123456", now_fn=lambda: 1000.0, get=get) == "somethingelse123456"


# ---- the CRITICAL finding: a live key must never be rewritten onto another gym ----------

def test_a_gyms_own_live_key_is_never_remapped_onto_a_slug_twin():
    """THE regression test for the audit's CRITICAL. Two gyms share the name-slug
    'crossfitlocal'. Under the old slug matcher, if gym B was missing from the known-key
    set its own live key resolved to gym A -- a cross-tenant write. Identity matching makes
    that unrepresentable: B's key is only ever mapped from B's own gym_id."""
    a_live = "crossfitlocal1a2b3c"
    b_live = "crossfitlocal9f8e7d"
    # Gym B is deliberately ABSENT from the plane -- the exact trigger condition.
    get = _plane([{"gym_id": LOCAL_A_ID, "echo_account_key": a_live}],
                 [{"id": LOCAL_A_ID, "name": "CrossFit Local"}])
    assert akr.resolve(b_live, now_fn=lambda: 1000.0, get=get) == b_live, \
        "a gym's own live key must never be rewritten onto a slug twin"


def test_a_legitimate_key_that_merely_looks_fingerprinted_is_untouched():
    """'crossfitdecade' parses as slug 'crossfit' + hex 'decade' under the old shape rule,
    which made it eligible for the misroute. Identity matching does not care about shape."""
    get = _plane([{"gym_id": LOCAL_A_ID, "echo_account_key": "crossfit1a2b3c"}],
                 [{"id": LOCAL_A_ID, "name": "CrossFit"}])
    assert akr.resolve("crossfitdecade", now_fn=lambda: 1000.0, get=get) == "crossfitdecade"


# ---- fail-closed on every uncertainty ---------------------------------------------------

def test_an_unreadable_plane_never_remaps():
    get = _plane([], [], fail=True)
    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=get) == REVERB_STALE


def test_a_truncated_read_never_remaps():
    """A silently truncated page is what made the old guard unsound: gyms past the cap
    vanish from the set. A page budget that runs out must read as 'unknown', not 'all'."""
    rows = [{"gym_id": f"{i:08d}-0000-4000-8000-00000000000{i%10}",
             "echo_account_key": f"gym{i:06d}"} for i in range(akr._PAGE * akr._MAX_PAGES + 5)]

    def get(path, params):
        off = int(params.get("offset", 0)); lim = int(params.get("limit", 1000))
        return (rows if path == "echo_intake_tokens" else [])[off:off + lim], True

    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=get) == REVERB_STALE


def test_two_token_rows_for_one_gym_never_remap():
    """If a re-key INSERTED instead of updating, we cannot tell which key is current, and
    guessing sends a write to the wrong place."""
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE},
                  {"gym_id": REVERB_ID, "echo_account_key": "crossfitreverbold99"}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=get) == REVERB_STALE


def test_two_gyms_deriving_the_same_key_refuses_both():
    """A slug+hash collision must never be resolved by picking one."""
    same_name = "Twin"
    d_a = _derived(LOCAL_A_ID, same_name)
    get = _plane([{"gym_id": LOCAL_A_ID, "echo_account_key": "twinaaa111"},
                  {"gym_id": LOCAL_B_ID, "echo_account_key": "twinbbb222"}],
                 [{"id": LOCAL_A_ID, "name": same_name},
                  {"id": LOCAL_B_ID, "name": same_name}])
    # Drive the REAL _build so the collision branch is actually exercised. Both gyms are
    # named "Twin", so both derive <slug><hash-of-their-own-id> -- distinct. To force one
    # derived string to belong to two gyms we stub _base_key, which is what the collision
    # branch defends against.
    import agent.account_key as ak
    real_base = ak._base_key
    ak._base_key = lambda gid, name: "twincollision"
    try:
        akr.reset_cache()
        live, mapping, by_gym, ok = akr._build(get=get, now_fn=lambda: 1000.0)
        assert ok is True
        assert "twincollision" not in mapping, \
            "two gyms deriving one key must map NEITHER -- never pick one"
        assert akr.resolve("twincollision", now_fn=lambda: 1000.0, get=get) == "twincollision"
    finally:
        ak._base_key = real_base
        akr.reset_cache()


def test_a_gym_with_no_name_is_never_derived_for():
    """Never derive from an id alone -- that is the bug the canonical module exists to kill."""
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}],
                 [{"id": REVERB_ID, "name": ""}])
    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=get) == REVERB_STALE


def test_blank_and_null_rows_are_skipped_not_crashed_on():
    get = _plane([{"gym_id": None, "echo_account_key": None},
                  {"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=get) == REVERB_LIVE


def test_plane_values_are_case_and_whitespace_normalised():
    """echo_account_key has no character-class validation on write. A polluted row must not
    produce a remap target that matches no row anywhere."""
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": "  CrossFitReverb30B5B2 "}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=get) == REVERB_LIVE
    assert akr.resolve(" CROSSFITREVERB30B5B2 ", now_fn=lambda: 1000.0, get=get) \
        == " CROSSFITREVERB30B5B2 ", "a live key, however written, is returned untouched"


# ---- caching / availability -------------------------------------------------------------

def test_a_successful_read_is_cached_for_the_full_ttl():
    calls = []

    def counting(path, params):
        calls.append(path)
        rows = ([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}]
                if path == "echo_intake_tokens" else [{"id": REVERB_ID, "name": REVERB_NAME}])
        return rows, True

    akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=counting)
    n = len(calls)
    akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0 + akr._TTL_OK - 1, get=counting)
    assert len(calls) == n, "a per-request resolution must not be a per-request round trip"


def test_a_failed_read_is_retried_soon_not_cached_for_five_minutes():
    """A brownout must not pin the resolver dark for the full TTL, and must not make every
    request pay a fresh timeout either."""
    state = {"fail": True}

    def flaky(path, params):
        if state["fail"]:
            return [], False
        rows = ([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}]
                if path == "echo_intake_tokens" else [{"id": REVERB_ID, "name": REVERB_NAME}])
        return rows, True

    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=flaky) == REVERB_STALE
    state["fail"] = False
    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0 + akr._TTL_FAIL - 1,
                       get=flaky) == REVERB_STALE, "still inside the short failure window"
    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0 + akr._TTL_FAIL + 1,
                       get=flaky) == REVERB_LIVE, "recovers quickly once the plane is back"


def test_the_reader_asks_for_bounded_ordered_pages():
    seen = []

    def spy(path, params):
        seen.append(dict(params))
        return [], True

    akr.resolve(REVERB_STALE, now_fn=lambda: 1000.0, get=spy)
    assert seen, "it must actually read"
    for params in seen:
        assert "limit" in params and "offset" in params and "order" in params, params


# ---- the wave-2 audit's own findings, each with a guard ---------------------------------

def test_every_table_is_ordered_by_a_column_it_actually_has():
    """NEW-1, the bug that made the whole resolver a production no-op: the reader ordered
    BOTH tables by gym_id, but `gyms` keys on `id`. PostgREST answers an unknown order
    column with 400/42703 -> ok=False -> every key returned unchanged, forever, while the
    suite passed green. This asserts the declared order column against each table's real
    columns rather than merely asserting that SOME order was requested."""
    for table, column in akr._ORDER_COLUMN.items():
        assert column in _REAL_COLUMNS[table], \
            f"{table} ordered by {column!r}, which that table does not have"


def test_a_mint_path_forces_a_fresh_read_past_a_warm_cache():
    """NEW-2: minting is a one-shot, permanent decision, and it shares the 300s cache with
    the read path. A cache warmed seconds BEFORE a gym was created answers "" for it, the
    caller falls back to deriving, and the split is recreated -- the Chateau case exactly."""
    NEW_ID = "cccccccc-0000-4000-8000-00000000000c"
    NEW_KEY = "crossfitchateauaaaabb"
    world = {"tokens": [], "gyms": []}

    def get(path, params):
        rows = world["tokens"] if path == "echo_intake_tokens" else world["gyms"]
        off = int(params.get("offset", 0)); lim = int(params.get("limit", 1000))
        return rows[off:off + lim], True

    # t+0: warm the cache while the gym does not exist yet.
    assert akr.portal_key_for_gym(NEW_ID, now_fn=lambda: 0.0, get=get) == ""
    # t+10: the portal creates it.
    world["tokens"] = [{"gym_id": NEW_ID, "echo_account_key": NEW_KEY}]
    world["gyms"] = [{"id": NEW_ID, "name": "CrossFit Chateau"}]
    # t+30, still inside the 300s success cache: a cached miss would recreate the split.
    assert akr.portal_key_for_gym(NEW_ID, now_fn=lambda: 30.0, get=get) == "", \
        "the plain read is cache-served, which is exactly why a mint must not use it"
    assert akr.portal_key_for_gym(NEW_ID, now_fn=lambda: 30.0, get=get, fresh=True) == NEW_KEY


def test_a_suffixed_stale_key_resolves_and_keeps_its_suffix():
    """NEW-6: account_key_mint deliberately preserves _ig/_fb, so suffixed stale keys
    exist. They were previously left unresolved."""
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.resolve(REVERB_STALE + "_ig", now_fn=lambda: 1.0, get=get) \
        == REVERB_LIVE + "_ig"
    assert akr.resolve(REVERB_STALE + "_fb", now_fn=lambda: 1.0, get=get) \
        == REVERB_LIVE + "_fb"
    assert akr.resolve(REVERB_LIVE + "_ig", now_fn=lambda: 1.0, get=get) \
        == REVERB_LIVE + "_ig", "a live suffixed key is untouched"


def test_every_key_a_gym_holds_counts_as_live_not_just_the_last_row():
    """Audit #6: by_gym[gid] was overwritten unconditionally, so a gym with two token rows
    kept only ONE of its keys in `live` -- while the comment claimed both were kept. A key
    a gym genuinely holds must never be eligible as a remap target."""
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE},
                  {"gym_id": REVERB_ID, "echo_account_key": "crossfitreverbold99"}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    live, mapping, by_gym, ok = akr._build(get=get, now_fn=lambda: 1.0)
    assert ok is True
    assert REVERB_LIVE in live and "crossfitreverbold99" in live, \
        "both of the gym's real keys must be live"
    assert REVERB_ID not in by_gym, "a gym with disagreeing rows must not be resolvable"


def test_a_rebuild_that_runs_out_of_time_reports_incomplete():
    """Audit #4: two paged reads on the auth path need a total budget, not just a per-read
    timeout."""
    clock = {"t": 0.0}

    def slow(path, params):
        clock["t"] += akr._BUILD_BUDGET  # one page burns the whole budget
        return [{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}] * akr._PAGE, True

    live, mapping, by_gym, ok = akr._build(get=slow, now_fn=lambda: clock["t"])
    assert ok is False, "an over-budget rebuild must read as unknown, never as complete"


# ---- wave-3 audit findings -------------------------------------------------------------

def test_a_live_key_stored_with_a_suffix_is_never_rewritten():
    """NEW-A, MAJOR, with the ADVERSARIAL fixture actually built.

    The first version of this test used a live key whose base was in neither `live` nor
    `mapping`, so it passed for the wrong reason and still passed with the guard deleted.
    This builds the real hazard: gym A's LIVE key is exactly gym B's DERIVED key + "_ig".
    With the whole-key check removed, resolve() strips the suffix, finds B's derived key in
    the mapping, and rewrites gym A's own live key onto gym B -- a cross-tenant write."""
    b_derived = _derived(REVERB_ID, REVERB_NAME)          # crossfitreverb6cdf33
    a_live = b_derived + "_ig"                            # gym A genuinely holds this
    get = _plane([{"gym_id": LOCAL_A_ID, "echo_account_key": a_live},
                  {"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}],
                 [{"id": LOCAL_A_ID, "name": "Attacker Gym"},
                  {"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.resolve(a_live, now_fn=lambda: 1.0, get=get) == a_live, (
        "gym A's own live key was rewritten onto gym B -- the whole-key live check is gone")
    # the unsuffixed stale key still resolves normally, so the guard is not over-broad
    assert akr.resolve(b_derived, now_fn=lambda: 1.0, get=get) == REVERB_LIVE


def test_a_suffixed_stale_key_that_is_not_live_still_resolves():
    """The guard must not disable suffix resolution for keys nobody holds."""
    get = _plane([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}],
                 [{"id": REVERB_ID, "name": REVERB_NAME}])
    assert akr.resolve(REVERB_STALE + "_ig", now_fn=lambda: 1.0, get=get) \
        == REVERB_LIVE + "_ig"


def test_the_build_budget_is_total_across_both_tables():
    """NEW-C: the deadline was computed per table, so a rebuild could spend 2x the budget
    while holding the rebuild lock."""
    clock = {"t": 0.0}

    def slow(path, params):
        clock["t"] += akr._BUILD_BUDGET * 0.6  # each page burns 60% of the whole budget
        return [{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}] * akr._PAGE, True

    start = clock["t"]
    _live, _map, _by_gym, ok = akr._build(get=slow, now_fn=lambda: clock["t"])
    assert ok is False
    assert clock["t"] - start <= akr._BUILD_BUDGET * 2, \
        "one rebuild must not be able to spend a per-table budget twice"


def test_a_failed_fresh_read_does_not_clobber_a_healthy_cache():
    """NEW-D: a mint path forces fresh=True. One such read during a brownout used to
    overwrite a good cache, so the READ path then returned every key unchanged for the
    next _TTL_FAIL seconds even though the plane had already recovered."""
    world = {"fail": False}

    def flaky(path, params):
        if world["fail"]:
            return [], False
        rows = ([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}]
                if path == "echo_intake_tokens" else [{"id": REVERB_ID, "name": REVERB_NAME}])
        return rows, True

    assert akr.resolve(REVERB_STALE, now_fn=lambda: 1.0, get=flaky) == REVERB_LIVE
    world["fail"] = True
    assert akr.portal_key_for_gym(REVERB_ID, now_fn=lambda: 2.0, get=flaky, fresh=True) == "", \
        "the mint path still gets an honest 'unknown' for its own decision"
    world["fail"] = False
    assert akr.resolve(REVERB_STALE, now_fn=lambda: 3.0, get=flaky) == REVERB_LIVE, \
        "everyone else keeps being served the healthy answer"


def test_an_undeclared_table_raises_rather_than_guessing_an_order_column():
    """_ORDER_COLUMN is the single place a table's ordering is declared. A new table with
    no entry must fail loudly here, not silently order by something that does not exist --
    which is exactly how the production no-op shipped."""
    import pytest
    with pytest.raises(KeyError):
        akr._read_all("some_new_table", "id", get=lambda p, q: ([], True))


def test_a_brownout_backs_off_instead_of_rebuilding_on_every_request():
    """The wave-4 blocker. Retaining a healthy cache on a failed rebuild WITHOUT recording
    a backoff stamp was worse than clobbering it: once the success TTL expired during a
    Supabase brownout, every request re-entered the rebuild behind the global lock and
    blocked on two timed-out reads, so requests queued for minutes."""
    world = {"fail": False, "reads": 0}

    def flaky(path, params):
        world["reads"] += 1
        if world["fail"]:
            return [], False
        rows = ([{"gym_id": REVERB_ID, "echo_account_key": REVERB_LIVE}]
                if path == "echo_intake_tokens" else [{"id": REVERB_ID, "name": REVERB_NAME}])
        return rows, True

    assert akr.resolve(REVERB_STALE, now_fn=lambda: 0.0, get=flaky) == REVERB_LIVE
    world["fail"] = True
    world["reads"] = 0
    # Well past the success TTL, ten requests during the brownout.
    for i in range(10):
        t = akr._TTL_OK + 1 + i * 0.1
        assert akr.resolve(REVERB_STALE, now_fn=lambda t=t: t, get=flaky) == REVERB_LIVE, \
            "the last known-good mapping should still be served"
    assert world["reads"] <= 4, (
        f"{world['reads']} plane reads for 10 requests -- the backoff stamp is missing "
        f"and every request is rebuilding")


def test_a_name_with_no_alphanumerics_never_becomes_a_remap_source():
    """canonical_account_key REJECTS such a gym, so a key derived from one can never be a
    real stale key. Deriving anyway invented a bare 6-hex remap source that nothing could
    legitimately carry."""
    PUNCT_ID = "67ef4400-0000-4000-8000-0000000000ff"
    get = _plane([{"gym_id": PUNCT_ID, "echo_account_key": "punctgym99"}],
                 [{"id": PUNCT_ID, "name": "!!!---***"}])
    _live, mapping, _by_gym, ok = akr._build(get=get, now_fn=lambda: 1.0)
    assert ok is True
    assert all(len(k) > 6 for k in mapping), f"bare-hex remap source invented: {mapping}"


def test_the_media_resolver_defaults_to_the_real_resolver(monkeypatch):
    """Every other test injects `resolve=`; nothing asserted the production default is
    wired to account_key_resolve.resolve at all."""
    from agent import gym_media_routes as gmr
    called = []
    monkeypatch.setattr("agent.account_key_resolve.resolve",
                        lambda key, **kw: called.append(key) or key)
    gmr._resolve_stale_fingerprint("crossfitreverb6cdf33")
    assert called == ["crossfitreverb6cdf33"], "the default path is not wired"


def test_a_target_held_by_two_gyms_is_never_a_remap_destination():
    """MAJOR from the wave-5 audit. The collision guard refused a duplicate SOURCE but
    never checked the TARGET. Two gyms genuinely sharing one current key -- the Bird Dog /
    Bolton collision this repo's reconciler exists for -- yields {dA: K, dB: K}: two
    distinct derived sources pointing at one shared key. The moment either gym is re-keyed,
    its OWN new live key is briefly absent from this cached view and the old mapping
    rewrites it onto the OTHER tenant. That is the exact failure this module's header uses
    to disqualify the name-slug design, coming back through the target side."""
    SHARED = "crossfitanywhere"
    A_ID = "9d9b8a00-0000-4000-8000-00000000000a"
    B_ID = "924e8900-0000-4000-8000-00000000000b"
    get = _plane([{"gym_id": A_ID, "echo_account_key": SHARED},
                  {"gym_id": B_ID, "echo_account_key": SHARED}],
                 [{"id": A_ID, "name": "CrossFit Anywhere"},
                  {"id": B_ID, "name": "CrossFit Anywhere"}])
    _live, mapping, _by_gym, ok = akr._build(get=get, now_fn=lambda: 1.0)
    assert ok is True
    assert mapping == {}, f"a shared key must never be a remap target: {mapping}"
    b_derived = _derived(B_ID, "CrossFit Anywhere")
    assert akr.resolve(b_derived, now_fn=lambda: 1.0, get=get) == b_derived
    assert akr.resolve(b_derived + "_ig", now_fn=lambda: 1.0, get=get) == b_derived + "_ig"


def test_a_rebuild_that_raises_still_backs_off():
    """MAJOR from the wave-5 audit. Only the `ok=False` return path stamped last_fail, so
    any EXCEPTION inside _build (a null row in the JSON, a non-str gym name) left no stamp
    and no cache write, and every subsequent auth-path request rebuilt. Measured at 100
    requests -> 200 plane reads: the wave-4 blocker reopened through the raise branch."""
    reads = {"n": 0}

    def poison(path, params):
        reads["n"] += 1
        return ([None] if path == "echo_intake_tokens" else []), True  # null row -> raises

    for i in range(20):
        assert akr.resolve(REVERB_STALE, now_fn=lambda i=i: float(i), get=poison) \
            == REVERB_STALE
    assert reads["n"] <= 6, (
        f"{reads['n']} plane reads for 20 requests -- a raising rebuild is not backing off")


def test_the_raw_gym_id_is_hashed_not_a_lowercased_copy():
    """The portal and the mint path hash the id EXACTLY as stored. Lowercasing it first
    inside the resolver would derive a different key for any mixed-case id, silently
    reintroducing a divergence."""
    MIXED_ID = "AABBCCDD-0000-4000-8000-00000000000E"
    LIVE = "mixedcasegym01"
    get = _plane([{"gym_id": MIXED_ID, "echo_account_key": LIVE}],
                 [{"id": MIXED_ID, "name": "Mixed Case Gym"}])
    from agent.account_key import _base_key
    assert akr.resolve(_base_key(MIXED_ID, "Mixed Case Gym"),
                       now_fn=lambda: 1.0, get=get) == LIVE, \
        "the resolver must hash the id exactly as the mint path does"
