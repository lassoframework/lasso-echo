"""
Variant pairing (migration 0318 / ECHO_VARIANT_PAIRING): Astra can regenerate an
existing scheduled post's creative as an alternate "v2" CANDIDATE, linked to the
original via variant_of, reviewed side by side, and PICKED atomically via
content_calendar_swap_variant (Postgres RPC). Offline throughout: PostgREST is a
fake http client that records every call.

Covers:
  * SupabaseCalendarStore: get_variant_group / create_variant_candidate / swap_variant
    build the right PostgREST calls (filters, payload, rpc/ path).
  * Backward compat: list_month and due_rows both filter variant_status=eq.active,
    so a candidate row sitting beside an active one is never double-counted or
    published.
  * portal_social handlers (handle_list_variants / handle_regen_variant /
    handle_pick_variant): flag gating, gym-scoped ownership, published-row refusal,
    and that regen never touches the original row.
  * variant_regen.generate_variant_image: disabled flag, no-caption guard,
    generate/host failure reasons, and that it calls creative_studio.generate (the
    SAME pipeline daily_studio uses) rather than a bespoke path.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, portal_calendar_store as pcs, portal_social as ps
from agent import variant_regen as vr


# ---------------------------------------------------------------------------
# store-level: fake PostgREST http client
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, get_resp=None, post_resp=None):
        self.calls = []
        self._get_resp = get_resp if get_resp is not None else _Resp(200, [])
        self._post_resp = post_resp if post_resp is not None else _Resp(200, [])

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", url, params or {}, headers or {}))
        return self._get_resp

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("post", url, params or {}, headers or {}, json))
        return self._post_resp


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    yield


def _row(row_id="orig-1", gym_id="eng", post_date="2026-09-20", account="instagram",
         fmt="feed", status="pending", variant_of=None, variant_status="active",
         caption="c", image_url="https://cdn/x.jpg", pillar="education"):
    return {"id": row_id, "gym_id": gym_id, "post_date": post_date,
            "account": account, "format": fmt, "status": status,
            "variant_of": variant_of, "variant_status": variant_status,
            "caption": caption, "image_url": image_url, "pillar": pillar}


# ---- SupabaseCalendarStore: variant methods --------------------------------

def test_get_variant_group_resolves_anchor_and_filters_active_or_candidate(monkeypatch):
    http = _FakeHTTP(get_resp=_Resp(200, [_row("orig-1")]))
    store = pcs.SupabaseCalendarStore()
    monkeypatch.setattr(store, "_client", lambda: http)
    store.get_variant_group("eng", "orig-1")
    # first call: get_row (seed); second: the group listing.
    assert len(http.calls) == 2
    method, url, params, headers = http.calls[1]
    assert params["or"] == "(id.eq.orig-1,variant_of.eq.orig-1)"
    assert params["variant_status"] == "in.(active,candidate)"
    assert params["gym_id"] == "eq.eng"


def test_get_variant_group_uses_candidates_own_variant_of_as_anchor(monkeypatch):
    seed = _row("cand-9", variant_of="orig-1", variant_status="candidate")
    http = _FakeHTTP(get_resp=_Resp(200, [seed]))
    store = pcs.SupabaseCalendarStore()
    monkeypatch.setattr(store, "_client", lambda: http)
    store.get_variant_group("eng", "cand-9")
    _, _, params, _ = http.calls[1]
    assert "id.eq.orig-1,variant_of.eq.orig-1" in params["or"]


def test_get_variant_group_empty_when_row_missing_or_cross_gym(monkeypatch):
    http = _FakeHTTP(get_resp=_Resp(200, []))
    store = pcs.SupabaseCalendarStore()
    monkeypatch.setattr(store, "_client", lambda: http)
    assert store.get_variant_group("eng", "nope") == []
    assert len(http.calls) == 1  # never issues the group read with no seed


def test_create_variant_candidate_copies_slot_identity_never_status(monkeypatch):
    anchor = _row("orig-1", status="approved", caption="original caption")
    http = _FakeHTTP(post_resp=_Resp(200, [dict(anchor, id="cand-new",
                                                variant_status="candidate",
                                                status="pending")]))
    store = pcs.SupabaseCalendarStore()
    monkeypatch.setattr(store, "_client", lambda: http)
    out = store.create_variant_candidate("eng", anchor, "https://cdn/v2.jpg")
    _, _, _, _, body = http.calls[0]
    payload = body[0]
    assert payload["gym_id"] == "eng"
    assert payload["post_date"] == anchor["post_date"]
    assert payload["account"] == anchor["account"]
    assert payload["format"] == anchor["format"]
    assert payload["variant_of"] == "orig-1"
    assert payload["variant_status"] == "candidate"
    # a candidate is NEVER pre-approved, regardless of the anchor's own status
    assert payload["status"] == "pending"
    assert payload["image_url"] == "https://cdn/v2.jpg"
    assert out["id"] == "cand-new"


def test_create_variant_candidate_gym_id_never_trusted_from_anchor(monkeypatch):
    """A caller-supplied anchor_row must never be able to smuggle a different
    gym_id into the insert -- account_key (the caller's proven scope) always wins."""
    anchor = _row("orig-1", gym_id="attacker-gym")
    http = _FakeHTTP(post_resp=_Resp(200, [dict(anchor, id="cand-x")]))
    store = pcs.SupabaseCalendarStore()
    monkeypatch.setattr(store, "_client", lambda: http)
    store.create_variant_candidate("eng", anchor, "https://cdn/v2.jpg")
    payload = http.calls[0][4][0]
    assert payload["gym_id"] == "eng"


def test_create_variant_candidate_points_at_a_candidates_own_anchor_not_itself(monkeypatch):
    """Regenerating FROM an already-candidate row must not start a second group --
    every candidate created for one logical post shares the SAME anchor id."""
    anchor = _row("cand-mid", variant_of="orig-1", variant_status="candidate")
    http = _FakeHTTP(post_resp=_Resp(200, [dict(anchor, id="cand-new")]))
    store = pcs.SupabaseCalendarStore()
    monkeypatch.setattr(store, "_client", lambda: http)
    store.create_variant_candidate("eng", anchor, "https://cdn/v3.jpg")
    payload = http.calls[0][4][0]
    assert payload["variant_of"] == "orig-1"


def test_swap_variant_calls_the_rpc_with_gym_scoped_candidate(monkeypatch):
    http = _FakeHTTP(post_resp=_Resp(200, {"ok": True, "active_id": "cand-1"}))
    store = pcs.SupabaseCalendarStore()
    monkeypatch.setattr(store, "_client", lambda: http)
    out = store.swap_variant("eng", "cand-1", actor="blake")
    method, url, params, headers, body = http.calls[0]
    assert url.endswith("/rpc/content_calendar_swap_variant")
    assert body == {"p_gym_id": "eng", "p_candidate_id": "cand-1", "p_actor": "blake"}
    assert out["ok"] is True


def test_swap_variant_empty_actor_sent_as_null_not_empty_string(monkeypatch):
    http = _FakeHTTP(post_resp=_Resp(200, {"ok": True}))
    store = pcs.SupabaseCalendarStore()
    monkeypatch.setattr(store, "_client", lambda: http)
    store.swap_variant("eng", "cand-1", actor="")
    assert http.calls[0][4]["p_actor"] is None


def test_swap_variant_raises_on_http_error(monkeypatch):
    http = _FakeHTTP(post_resp=_Resp(500, {}, text="boom"))
    store = pcs.SupabaseCalendarStore()
    monkeypatch.setattr(store, "_client", lambda: http)
    with pytest.raises(pcs.PortalStoreError):
        store.swap_variant("eng", "cand-1")


# ---- backward compat: one-row-per-post reads must exclude non-active ------

def test_list_month_filters_variant_status_active():
    http = _FakeHTTP(get_resp=_Resp(200, []))
    store = pcs.SupabaseCalendarStore()
    store._client = lambda: http
    store.list_month("eng", "2026-09")
    params = http.calls[0][2]
    assert params["variant_status"] == "eq.active"


def test_due_rows_filters_variant_status_active_so_a_candidate_can_never_publish():
    http = _FakeHTTP(get_resp=_Resp(200, []))
    store = pcs.SupabaseCalendarStore()
    store._client = lambda: http
    store.due_rows("gym-uuid", "2026-09-20")
    params = http.calls[0][2]
    assert params["variant_status"] == "eq.active"


# ---------------------------------------------------------------------------
# portal_social handlers
# ---------------------------------------------------------------------------

class _FakeVariantStore:
    """Minimal fake covering get_row + the three variant methods, gym-scoped like
    the real PostgREST filters."""

    def __init__(self, rows=None, group=None, swap_result=None):
        self._rows = {r["id"]: dict(r) for r in (rows or [])}
        self._group = group if group is not None else []
        self._swap_result = swap_result if swap_result is not None else {"ok": True}
        self.created = []
        self.swaps = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        return dict(r)

    def get_variant_group(self, account_key, row_id):
        row = self.get_row(account_key, row_id)
        if row is None:
            return []
        return self._group

    def create_variant_candidate(self, account_key, anchor_row, image_url,
                                 caption=None, **_kw):
        self.created.append((account_key, anchor_row.get("id"), image_url))
        return {"id": "cand-new", "image_url": image_url,
                "caption": caption or anchor_row.get("caption"),
                "variant_status": "candidate"}

    def swap_variant(self, account_key, candidate_id, actor=""):
        self.swaps.append((account_key, candidate_id, actor))
        return self._swap_result


@pytest.fixture(autouse=True)
def _social_env(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setattr(ps, "is_social_active", lambda account_key, reader=None: True)
    monkeypatch.setattr(config, "portal_calendar_supabase_enabled", lambda: True)
    yield


def test_list_variants_not_gated_by_variant_pairing_flag(monkeypatch):
    """Reading is safe even with ECHO_VARIANT_PAIRING off -- there is nothing to
    hide (no candidate exists unless the flag was already on when it was made)."""
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "false")
    store = _FakeVariantStore(rows=[_row("orig-1")], group=[_row("orig-1")])
    status, body = ps.handle_list_variants("eng", "orig-1", "blake", sb_store=store)
    assert status == 200
    assert body["ok"] is True
    assert body["variants"][0]["id"] == "orig-1"


def test_list_variants_404_on_cross_gym(monkeypatch):
    store = _FakeVariantStore(rows=[_row("orig-1", gym_id="other")])
    status, body = ps.handle_list_variants("eng", "orig-1", "blake", sb_store=store)
    assert status == 404


def test_regen_variant_403_while_flag_off(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "false")
    store = _FakeVariantStore(rows=[_row("orig-1")])
    status, body = ps.handle_regen_variant("eng", "orig-1", "blake", sb_store=store)
    assert status == 403
    assert store.created == []  # never even read the row


def test_regen_variant_creates_candidate_without_touching_original(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    original = _row("orig-1", status="approved", image_url="https://cdn/v1.jpg")
    store = _FakeVariantStore(rows=[original])

    def fake_regen(row, account_key):
        assert row["id"] == "orig-1"
        return {"ok": True, "image_url": "https://cdn/v2.jpg"}

    status, body = ps.handle_regen_variant("eng", "orig-1", "blake",
                                           sb_store=store, regen_fn=fake_regen)
    assert status == 200
    assert body["candidate"]["image_url"] == "https://cdn/v2.jpg"
    assert store.created == [("eng", "orig-1", "https://cdn/v2.jpg")]
    # the original row itself was never mutated
    assert store._rows["orig-1"]["image_url"] == "https://cdn/v1.jpg"
    assert store._rows["orig-1"]["status"] == "approved"


def test_regen_variant_refuses_on_a_published_row(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    store = _FakeVariantStore(rows=[_row("orig-1", status="published")])
    called = {"n": 0}

    def fake_regen(row, account_key):
        called["n"] += 1
        return {"ok": True, "image_url": "https://cdn/v2.jpg"}

    status, body = ps.handle_regen_variant("eng", "orig-1", "blake",
                                           sb_store=store, regen_fn=fake_regen)
    assert status in (409, 410, 422)
    assert called["n"] == 0  # never even generates for a final row
    assert store.created == []


def test_regen_variant_409_on_generation_failure_writes_nothing(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    store = _FakeVariantStore(rows=[_row("orig-1")])
    status, body = ps.handle_regen_variant(
        "eng", "orig-1", "blake", sb_store=store,
        regen_fn=lambda row, account_key: {"ok": False, "reason": "no_caption"})
    assert status == 409
    assert store.created == []


def test_pick_variant_403_while_flag_off(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "false")
    store = _FakeVariantStore(rows=[_row("cand-1", variant_status="candidate")])
    status, body = ps.handle_pick_variant("eng", "cand-1", "blake", sb_store=store)
    assert status == 403
    assert store.swaps == []


def test_pick_variant_calls_swap_and_returns_active_id(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    store = _FakeVariantStore(
        rows=[_row("cand-1", variant_status="candidate")],
        swap_result={"ok": True, "active_id": "cand-1",
                    "archived_previous_active": "orig-1"})
    status, body = ps.handle_pick_variant("eng", "cand-1", "blake", sb_store=store)
    assert status == 200
    assert body["active_id"] == "cand-1"
    assert body["archived_previous_active"] == "orig-1"
    assert store.swaps == [("eng", "cand-1", "blake")]


def test_pick_variant_404_on_cross_gym_before_ever_calling_the_rpc(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    store = _FakeVariantStore(rows=[_row("cand-1", gym_id="other")])
    status, body = ps.handle_pick_variant("eng", "cand-1", "blake", sb_store=store)
    assert status == 404
    assert store.swaps == []  # ownership refused BEFORE the RPC is ever called


def test_pick_variant_surfaces_rpc_refusal_as_409_not_500(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    store = _FakeVariantStore(
        rows=[_row("cand-1", variant_status="active")],  # already swapped
        swap_result={"ok": False, "error": "not_a_candidate"})
    status, body = ps.handle_pick_variant("eng", "cand-1", "blake", sb_store=store)
    assert status == 409
    assert body["error"] == "not_a_candidate"


def test_pick_variant_not_found_from_rpc_maps_to_404():
    store = _FakeVariantStore(rows=[_row("cand-1")],
                              swap_result={"ok": False, "error": "not_found"})
    os.environ["ECHO_VARIANT_PAIRING"] = "true"
    try:
        status, body = ps.handle_pick_variant("eng", "cand-1", "blake", sb_store=store)
    finally:
        os.environ.pop("ECHO_VARIANT_PAIRING", None)
    assert status == 404


# ---------------------------------------------------------------------------
# variant_regen.generate_variant_image
# ---------------------------------------------------------------------------

def test_variant_regen_disabled_by_default():
    assert config.variant_pairing_enabled() is False


def test_generate_variant_image_returns_disabled_reason_when_flag_off(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "false")
    out = vr.generate_variant_image(_row("orig-1"), "eng")
    assert out == {"ok": False, "reason": vr.REASON_DISABLED}


def test_generate_variant_image_no_caption_guard(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    out = vr.generate_variant_image(_row("orig-1", caption=""), "eng")
    assert out == {"ok": False, "reason": vr.REASON_NO_CAPTION}


def test_generate_variant_image_calls_creative_studio_generate_not_a_bespoke_path(monkeypatch):
    """Regenerating a scheduled post's photo must go through the SAME Astra-first,
    house-style-grade pipeline as any normal build (daily_studio uses
    creative_studio.generate) -- never a separate, ungated image call."""
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    seen = {}

    def fake_generate(headline, facts, client=None, aspect=None, pixels=None,
                      surface=None, account_key=None):
        seen["headline"] = headline
        seen["facts"] = facts
        seen["account_key"] = account_key
        return {"path": "/tmp/v2.png", "prompt": "p", "model": "astra"}

    out = vr.generate_variant_image(
        _row("orig-1", caption="Great class today. Loved it.", pillar="community"),
        "eng", generate_fn=fake_generate, host_fn=lambda path, key: "https://cdn/v2.png")
    assert out["ok"] is True
    assert out["image_url"] == "https://cdn/v2.png"
    assert seen["headline"] == "community"
    assert seen["account_key"] == "eng"
    assert seen["facts"]  # never empty -- the no-fabrication gate upstream relies on this


def test_generate_variant_image_hosting_failure_reason(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    out = vr.generate_variant_image(
        _row("orig-1", caption="fact one"), "eng",
        generate_fn=lambda *a, **k: {"path": "/tmp/x.png"},
        host_fn=lambda path, key: None)
    assert out == {"ok": False, "reason": vr.REASON_HOSTING}


def test_generate_variant_image_generate_failure_reason(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    out = vr.generate_variant_image(
        _row("orig-1", caption="fact one"), "eng",
        generate_fn=lambda *a, **k: None)
    assert out == {"ok": False, "reason": vr.REASON_GENERATE_FAILED}
