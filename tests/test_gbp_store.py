"""
GbpStore (agent/gbp_store.py): the availability guard reflects real creds, the
idempotency reader excludes terminal rows, and onboarding_intake reads through the
store's own _get. Offline via a fake base store capturing PostgREST params.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.gbp_store import GbpStore  # noqa: E402


class _FakeBase:
    """Stands in for SupabaseCalendarStore: carries _url/_key and captures _get calls."""

    def __init__(self, url="", key="", rows=None):
        self._url = url
        self._key = key
        self.captured = []
        self._rows = rows or []

    # GbpStore._get calls self._s._client().get(...); we bypass that by giving GbpStore a
    # base that ALSO exposes _get so we intercept at the higher level in these tests.


class _CapturingStore(GbpStore):
    """GbpStore whose _get is stubbed to capture (table, params) and return canned rows."""

    def __init__(self, base, rows=None):
        super().__init__(base=base)
        self._rows = rows or []

    def _get(self, table, params):
        self._s.captured.append((table, params))
        return self._rows


def test_available_false_without_creds():
    assert GbpStore(base=_FakeBase(url="", key="")).available() is False


def test_available_true_with_creds():
    assert GbpStore(base=_FakeBase(url="https://x.supabase.co", key="svc")).available() is True


def test_available_prefers_base_available_when_present():
    class _WithAvail(_FakeBase):
        def available(self):
            return False
    # base says unavailable even though creds look set -> honored
    s = GbpStore(base=_WithAvail(url="u", key="k"))
    assert s.available() is False


def test_future_gbp_rows_excludes_terminal_statuses():
    base = _FakeBase(url="u", key="k")
    s = _CapturingStore(base)
    s.future_gbp_rows("lasso", "2026-09-01")
    table, params = base.captured[-1]
    assert table == "content_calendar"
    assert params["account"] == "eq.googlebusiness"
    assert params["gym_id"] == "eq.lasso"
    assert params["status"] == "not.in.(failed,denied,deleted)"


def test_onboarding_intake_queries_by_base_name():
    base = _FakeBase(url="u", key="k")
    s = _CapturingStore(base, rows=[{"business_name": "LASSO", "offers": [], "ghl_link": ""}])
    rec = s.onboarding_intake("lasso_ig")
    table, params = base.captured[-1]
    assert table == "onboarding_intake"
    assert params["business_name"] == "ilike.*lasso*"   # base key, suffix stripped
    assert rec["business_name"] == "LASSO"


def test_onboarding_intake_none_when_empty():
    s = _CapturingStore(_FakeBase(url="u", key="k"), rows=[])
    assert s.onboarding_intake("lasso") is None


def test_any_gbp_rows_queries_gym_scoped():
    base = _FakeBase(url="u", key="k")
    s = _CapturingStore(base, rows=[{"id": "x"}])
    assert s.any_gbp_rows("lasso") is True
    table, params = base.captured[-1]
    assert table == "content_calendar"
    assert params["account"] == "eq.googlebusiness" and params["gym_id"] == "eq.lasso"


def test_any_gbp_rows_false_when_empty():
    s = _CapturingStore(_FakeBase(url="u", key="k"), rows=[])
    assert s.any_gbp_rows("lasso") is False


def test_release_coach_review_patches_status():
    # release flips coach_review -> pending via a filtered PATCH
    captured = {}

    class _Resp:
        status_code = 200
        text = "[]"
        def json(self):
            return [{"id": "1", "status": "pending"}, {"id": "2", "status": "pending"}]

    class _Client:
        def patch(self, url, params=None, headers=None, json=None, timeout=None):
            captured["params"] = params
            captured["json"] = json
            return _Resp()

    class _Base(_FakeBase):
        def _client(self):
            return _Client()
        def _rest(self, path):
            return f"https://x/rest/v1/{path}"
        def _headers(self, extra=None):
            return {}

    s = GbpStore(base=_Base(url="u", key="k"))
    released = s.release_coach_review("lasso")
    assert len(released) == 2
    assert captured["params"]["status"] == "eq.coach_review"
    assert captured["params"]["gym_id"] == "eq.lasso"
    assert captured["json"] == {"status": "pending"}


# ---- G3 gym_gbp_metrics: posts_published + top_post_id seed -------------------

def test_bump_posts_published_inserts_new_month():
    base = _FakeBase(url="u", key="k")

    class _Resp:
        status_code = 201
        text = "[]"
        def json(self): return [{"id": "m1", "posts_published": 1}]

    class _Client:
        def __init__(self): self.posts = []
        def post(self, url, params=None, headers=None, json=None, timeout=None):
            self.posts.append(json); return _Resp()

    client = _Client()

    class _Base(_FakeBase):
        def _client(self): return client
        def _rest(self, p): return f"https://x/rest/v1/{p}"
        def _headers(self, extra=None): return {}

    s = _CapturingStore(_Base(url="u", key="k"), rows=[])   # _get returns [] -> insert
    s.bump_posts_published("lasso", "locations/1", "2026-09-01",
                           now_iso="2026-09-01T09:00:00Z", seed_top_post_id="zpA")
    body = client.posts[-1]
    assert body["posts_published"] == 1
    assert body["top_post_id"] == "zpA"        # seeded on first publish of the month
    assert body["month"] == "2026-09-01" and body["gbp_location_id"] == "locations/1"


def test_bump_posts_published_increments_existing_and_keeps_top():
    captured = {}

    class _Resp:
        status_code = 200
        text = "[]"
        def json(self): return [{"id": "m1", "posts_published": 6}]

    class _Client:
        def patch(self, url, params=None, headers=None, json=None, timeout=None):
            captured["json"] = json; return _Resp()

    class _Base(_FakeBase):
        def _client(self): return _Client()
        def _rest(self, p): return f"https://x/rest/v1/{p}"
        def _headers(self, extra=None): return {}

    # existing row already has 5 published + a top_post_id -> increment, don't reseed top
    s = _CapturingStore(_Base(url="u", key="k"),
                        rows=[{"id": "m1", "posts_published": 5, "top_post_id": "zpOld"}])
    s.bump_posts_published("lasso", "locations/1", "2026-09-01",
                           now_iso="2026-09-02T09:00:00Z", seed_top_post_id="zpNew")
    assert captured["json"]["posts_published"] == 6
    assert "top_post_id" not in captured["json"], "an existing top_post_id is never reseeded"


# ---- publish_claim_token claim + CAS transitions (2026-10-02) ---------------------

from agent.portal_calendar_store import PortalStoreError  # noqa: E402


class _Resp:
    def __init__(self, rows, status_code=200):
        self._rows = rows
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._rows


class _Http:
    """Fake httpx client: each queued response is returned in order; calls recorded."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def patch(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append({"params": params or {}, "json": json or {}})
        return self.responses.pop(0)


class _Base:
    _url, _key = "u", "k"

    def __init__(self, http):
        self._http = http

    def _client(self):
        return self._http

    def _rest(self, table):
        return f"https://x/{table}"

    def _headers(self, extra=None):
        return dict(extra or {})


def test_claim_publishing_writes_and_returns_the_persisted_token():
    http = _Http([_Resp([{"id": "r1", "status": "publishing",
                          "image_url": "https://r2/x.jpg"}])])
    # the DB echoes back whatever token we wrote
    s = GbpStore(base=_Base(http))
    import agent.gbp_store as gs
    orig = gs.uuid.uuid4
    gs.uuid.uuid4 = lambda: gs.uuid.UUID("aaaaaaaa-1111-4111-8111-111111111111")
    try:
        http.responses[0]._rows[0]["publish_claim_token"] = \
            "aaaaaaaa-1111-4111-8111-111111111111"
        tok = s.claim_publishing("r1")
    finally:
        gs.uuid.uuid4 = orig
    assert tok == "aaaaaaaa-1111-4111-8111-111111111111"
    call = http.calls[0]
    assert call["params"]["status"] == "eq.approved"          # conditional claim
    assert call["json"]["status"] == "publishing"
    assert call["json"]["publish_claim_token"] == tok         # same write, same token


def test_claim_publishing_lost_claim_returns_none():
    s = GbpStore(base=_Base(_Http([_Resp([])])))
    assert s.claim_publishing("r1") is None


def test_claim_publishing_inconsistent_returned_token_raises():
    http = _Http([_Resp([{"id": "r1", "status": "publishing",
                          "publish_claim_token": "bbbbbbbb-2222-4222-8222-222222222222"}])])
    s = GbpStore(base=_Base(http))
    try:
        s.claim_publishing("r1")
        assert False, "an unverified token must never be used"
    except PortalStoreError:
        pass


def test_mark_published_claimed_is_token_scoped_and_clears_token():
    http = _Http([_Resp([{"id": "r1", "status": "published"}])])
    s = GbpStore(base=_Base(http))
    out = s.mark_published_claimed("r1", "aaaaaaaa-1111-4111-8111-111111111111",
                                   "zpost_1", "2026-10-02T00:00:00Z")
    assert out["status"] == "published"
    call = http.calls[0]
    assert call["params"]["status"] == "eq.publishing"
    assert call["params"]["publish_claim_token"] == \
        "eq.aaaaaaaa-1111-4111-8111-111111111111"
    assert call["json"]["publish_claim_token"] is None        # cleared on terminal
    assert call["json"]["late_post_id"] == "zpost_1"


def test_claimed_transition_with_no_matching_row_raises_visibly():
    for fn in (lambda s: s.mark_published_claimed("r1", "t", "z", "2026-10-02"),
               lambda s: s.mark_failed_claimed("r1", "t", "nope")):
        s = GbpStore(base=_Base(_Http([_Resp([])])))
        try:
            fn(s)
            assert False, "a stale/changed claim must never be overwritten silently"
        except PortalStoreError as e:
            assert e.status == 409


def test_release_publishing_claim_is_token_scoped():
    http = _Http([_Resp([{"id": "r1", "status": "approved"}])])
    s = GbpStore(base=_Base(http))
    out = s.release_publishing_claim("r1", "tok-1", "approved")
    assert out["status"] == "approved"
    call = http.calls[0]
    assert call["params"]["publish_claim_token"] == "eq.tok-1"
    assert call["json"]["publish_claim_token"] is None
    # no match -> None, no exception, nothing overwritten
    s2 = GbpStore(base=_Base(_Http([_Resp([])])))
    assert s2.release_publishing_claim("r1", "tok-1", "approved") is None


# ---- media hold + variant guard (2026-10-02 Sol review release-blocker) -----------

def test_approved_gbp_rows_excludes_holds_variants_claims_and_blank_images():
    base = _FakeBase(url="u", key="k")
    s = _CapturingStore(base, rows=[
        {"id": "ok", "image_url": "https://r2/a.jpg"},
        {"id": "blank", "image_url": "   "},            # slips past not.is.null
        {"id": "hold", "image_url": "https://r2/b.jpg",
         "media_not_ready_reason": "rendering"},        # server filter could miss
    ])
    out = s.approved_gbp_rows("2026-10-02")
    _table, params = base.captured[-1]
    # exact GET predicates: unpublished, unclaimed, no late post, active variant,
    # non-null image, and NO media hold
    assert params["published_at"] == "is.null"
    assert params["late_post_id"] == "is.null"
    assert params["publish_claim_token"] == "is.null"
    assert params["variant_status"] == "eq.active"
    assert params["image_url"] == "not.is.null"
    assert params["media_not_ready_reason"] == "is.null"
    # client-side blank/whitespace trim after the GET (PostgREST cannot trim)
    assert [r["id"] for r in out] == ["ok"]


def test_claim_publishing_patch_carries_all_media_hold_predicates():
    http = _Http([_Resp([{"id": "r1", "status": "publishing",
                          "publish_claim_token": "t",
                          "image_url": "https://r2/x.jpg"}])])
    s = GbpStore(base=_Base(http))
    import agent.gbp_store as gs
    orig = gs.uuid.uuid4
    gs.uuid.uuid4 = lambda: gs.uuid.UUID("aaaaaaaa-1111-4111-8111-111111111111")
    try:
        http.responses[0]._rows[0]["publish_claim_token"] = \
            "aaaaaaaa-1111-4111-8111-111111111111"
        tok = s.claim_publishing("r1")
    finally:
        gs.uuid.uuid4 = orig
    assert tok == "aaaaaaaa-1111-4111-8111-111111111111"
    p = http.calls[0]["params"]
    assert p["id"] == "eq.r1"
    assert p["status"] == "eq.approved"
    assert p["account"] == "eq.googlebusiness"
    assert p["variant_status"] == "eq.active"
    assert p["published_at"] == "is.null"
    assert p["late_post_id"] == "is.null"
    assert p["publish_claim_token"] == "is.null"
    assert p["image_url"] == "not.is.null"
    assert p["media_not_ready_reason"] == "is.null"


def test_claim_publishing_returned_row_with_hold_or_blank_image_raises_and_retains():
    # The PATCH won, but the returned row carries a hold/blank image the predicates
    # could not trim: the claim must be RETAINED (no release write, nothing
    # overwritten) and the caller told to send nothing.
    for bad in ({"id": "r1", "status": "publishing", "image_url": "   "},
                {"id": "r1", "status": "publishing", "image_url": "https://r2/x.jpg",
                 "media_not_ready_reason": "rendering"}):
        http = _Http([_Resp([dict(bad, publish_claim_token="t")])])
        s = GbpStore(base=_Base(http))
        try:
            s.claim_publishing("r1")
            assert False, "a held/blank claimed row must never yield a usable claim"
        except PortalStoreError as e:
            assert e.status == 409
        # exactly one HTTP call (the claim PATCH); no release/overwrite followed
        assert len(http.calls) == 1

# Proof-gated GBP claims use the database RPC exclusively.
def _proof_row(**changes):
    row = dict(id="r1", gym_id="gym", account="googlebusiness", status="publishing",
               variant_status="active", image_url="https://cdn/current.jpg",
               publish_claim_token="aaaaaaaa-1111-4111-8111-111111111111")
    row.update(changes)
    return row


class _ProofHttp(_Http):
    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return self.responses.pop(0)


def test_proof_claim_returns_locked_creative_and_never_patches(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    current = _proof_row(caption="current creative")
    http = _ProofHttp([_Resp([current])])
    out = GbpStore(base=_Base(http)).claim_publishing("r1", gym_id="gym")
    assert out == current
    assert http.calls == [{"url": "https://x/rpc/claim_calendar_gbp_publish_owned",
                           "json": {"p_row_id": "r1", "p_gym_id": "gym"}}]


@pytest.mark.parametrize("response", [[], None, [{"id": "r1"}],
    [_proof_row(gym_id="other")], [_proof_row(status="approved")],
    [_proof_row(publish_claim_token="bad")], [_proof_row(image_url=" ")],
    [_proof_row(), _proof_row()]])
def test_proof_claim_denial_or_invalid_response_never_falls_back(response):
    http = _ProofHttp([_Resp(response)])
    store = GbpStore(base=_Base(http))
    if response == []:
        assert store.claim_publishing("r1", gym_id="gym", require_proof=True) is None
    else:
        with pytest.raises(PortalStoreError):
            store.claim_publishing("r1", gym_id="gym", require_proof=True)
    assert len(http.calls) == 1 and "rpc/" in http.calls[0]["url"]


def test_proof_claim_missing_rpc_holds_without_legacy_patch():
    http = _ProofHttp([_Resp(None, status_code=404)])
    with pytest.raises(PortalStoreError):
        GbpStore(base=_Base(http)).claim_publishing("r1", gym_id="gym", require_proof=True)
    assert len(http.calls) == 1
