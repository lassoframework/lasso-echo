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

import hashlib
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


def test_regen_variant_forwards_poster_evidence_to_candidate_writer(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    original = _row("orig-1", status="approved", image_url="https://cdn/v1.jpg")
    store = _FakeVariantStore(rows=[original])
    seen = {}

    def create_candidate(account_key, anchor_row, image_url, **kwargs):
        seen.update(account_key=account_key, image_url=image_url, kwargs=kwargs)
        return {"id": "cand-new", "image_url": image_url,
                "caption": anchor_row["caption"], "variant_status": "candidate"}

    store.create_variant_candidate = create_candidate
    poster = {"source_exact_url": "https://cdn/clip.mp4",
              "delivered_exact_url": "https://cdn/poster.jpg", "operation": "render"}
    status, _ = ps.handle_regen_variant(
        "eng", "orig-1", "blake", sb_store=store,
        regen_fn=lambda *_: {"ok": True, "image_url": "https://cdn/clip.mp4",
                             "thumbnail_url": "https://cdn/poster.jpg",
                             "source_media_url": "https://cdn/clip.mp4",
                             "poster_render_evidence": poster})

    assert status == 200
    assert seen["kwargs"]["poster_render_evidence"] == poster
    assert seen["kwargs"]["source_media_url"] == "https://cdn/clip.mp4"


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


# ---------------------------------------------------------------------------
# handle_regen_variant_from_brief — the "type what you want" manual button
# ---------------------------------------------------------------------------

def test_regen_variant_from_brief_403_while_flag_off(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "false")
    store = _FakeVariantStore(rows=[_row("orig-1")])
    status, body = ps.handle_regen_variant_from_brief(
        "eng", "orig-1", "blake", "make it about our new 6am class", sb_store=store)
    assert status == 403
    assert store.created == []


def test_regen_variant_from_brief_400_on_empty_brief(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    store = _FakeVariantStore(rows=[_row("orig-1")])
    status, body = ps.handle_regen_variant_from_brief(
        "eng", "orig-1", "blake", "   ", sb_store=store)
    assert status == 400
    assert store.created == []


def test_regen_variant_from_brief_creates_candidate_without_touching_original(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    original = _row("orig-1", status="approved", image_url="https://cdn/v1.jpg")
    store = _FakeVariantStore(rows=[original])
    seen = {}

    def fake_regen(row, account_key, brief):
        seen["brief"] = brief
        assert row["id"] == "orig-1"
        return {"ok": True, "image_url": "https://cdn/v2-brief.jpg"}

    status, body = ps.handle_regen_variant_from_brief(
        "eng", "orig-1", "blake", "highlight our new 6am class",
        sb_store=store, regen_fn=fake_regen)
    assert status == 200
    assert body["candidate"]["image_url"] == "https://cdn/v2-brief.jpg"
    assert seen["brief"] == "highlight our new 6am class"
    assert store.created == [("eng", "orig-1", "https://cdn/v2-brief.jpg")]
    assert store._rows["orig-1"]["image_url"] == "https://cdn/v1.jpg"


def test_regen_variant_from_brief_refuses_on_a_published_row(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    store = _FakeVariantStore(rows=[_row("orig-1", status="published")])
    called = {"n": 0}

    def fake_regen(row, account_key, brief):
        called["n"] += 1
        return {"ok": True, "image_url": "https://cdn/v2.jpg"}

    status, body = ps.handle_regen_variant_from_brief(
        "eng", "orig-1", "blake", "a brief", sb_store=store, regen_fn=fake_regen)
    assert status in (409, 410, 422)
    assert called["n"] == 0
    assert store.created == []


def test_regen_variant_from_brief_409_on_generation_failure_writes_nothing(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    store = _FakeVariantStore(rows=[_row("orig-1")])
    status, body = ps.handle_regen_variant_from_brief(
        "eng", "orig-1", "blake", "a brief", sb_store=store,
        regen_fn=lambda row, account_key, brief: {"ok": False, "reason": "generate_failed"})
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


def test_lasso_variant_uses_exact_scheduled_caption_and_records_its_source(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    monkeypatch.setenv("AGENT_LASSO_INFOGRAPHIC_QUALITY", "true")
    from agent import infographic_artifacts

    caption = "Your team can see the next step.\n\nOne clear follow up keeps work moving."
    row = _row("lasso-post-1", gym_id="lasso", caption=caption, pillar="summit")
    seen = {}

    def fake_generate(headline, facts, **kwargs):
        seen["headline"] = headline
        seen["facts"] = facts
        seen["kwargs"] = kwargs
        return {"path": "/tmp/lasso-v2.png", "prompt": "graded", "model": "astra",
                "route": "astra:test"}

    class FakeArtifactStore:
        def save(self, account_key, url, path, source):
            seen["artifact"] = (account_key, url, path, source)

    monkeypatch.setattr(infographic_artifacts, "ArtifactStore", FakeArtifactStore)
    out = vr.generate_variant_image(row, "lasso_ig", generate_fn=fake_generate,
        host_fn=lambda path, key: "https://cdn.example/lasso-v2.png")

    assert out["ok"] is True
    assert seen["headline"] == "Your team can see the next step."
    assert seen["facts"] == [caption]
    assert seen["kwargs"]["draft_id"] == row["id"]
    assert seen["kwargs"]["cta"] == ""
    assert seen["kwargs"]["footer"] is None
    assert seen["artifact"][3] == {
        "source_id": "content_calendar:lasso-post-1:caption",
        "source_hash": hashlib.sha256(caption.encode("utf-8")).hexdigest(),
    }


def test_lasso_variant_uses_only_cta_present_in_scheduled_caption(monkeypatch):
    from agent import content_planner
    monkeypatch.setattr(content_planner, "load_source_doc", lambda: type(
        "Doc", (), {"ctas": ["Book a growth call", "Save this for later"]})())
    assert vr._caption_approved_cta("One clear step. Book a growth call") == "Book a growth call"
    assert vr._caption_approved_cta("One clear step for your team") == ""


def test_lasso_variant_requires_row_identity_for_artifact_provenance(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    monkeypatch.setenv("AGENT_LASSO_INFOGRAPHIC_QUALITY", "true")
    row = _row("", gym_id="lasso", caption="Approved scheduled copy")
    out = vr.generate_variant_image(row, "lasso_ig",
        generate_fn=lambda *args, **kwargs: pytest.fail("must not generate"))
    assert out == {"ok": False, "reason": "caption_source_unavailable"}


def test_lasso_story_variant_keeps_caption_and_story_canvas(monkeypatch):
    from agent import lasso_current_artifact
    monkeypatch.setattr(lasso_current_artifact,"artifact_current",lambda *a,**k:True)
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    monkeypatch.setenv("AGENT_LASSO_INFOGRAPHIC_QUALITY", "true")
    from agent import infographic_artifacts
    monkeypatch.setattr(infographic_artifacts.ArtifactStore, "save",
        lambda *args, **kwargs: {})
    monkeypatch.setattr(vr, "_attest_reviewed_story_dimensions", lambda path: True)
    seen = {}

    def fake_generate(headline, facts, **kwargs):
        seen.update(headline=headline, facts=facts, kwargs=kwargs)
        return {"path": "/tmp/story-v2.png"}

    row = _row("lasso-story-1", gym_id="lasso", fmt="story",
        caption="A clear next step helps the team.")
    out = vr.generate_variant_image(row, "lasso_ig", generate_fn=fake_generate,
        paired_feed_reference={"feed":dict(row, format="feed"),"artifact":{}},
        host_fn=lambda path, key: "https://cdn.example/story-v2.png")

    assert out["ok"] is True
    assert seen["facts"] == [row["caption"]]
    assert seen["kwargs"]["surface"] == "story"
    assert seen["kwargs"]["aspect"] == "9:16"
    assert seen["kwargs"]["pixels"] == "1080x1920"


# ---------------------------------------------------------------------------
# _owned_story_caption_fields — owned LASSO Story copy de-duplication
#
# Bug being fixed: the owned Story branch rendered headline = first caption
# line, facts = [entire caption], plus the same approved CTA again in a
# separate CTA field, duplicating both in the rendered Story. The audited
# target caption is seven exact paragraphs (split on '\n\n'): H=P0, body
# P1..P5, CTA=P6. The helper must hand the engine exactly those slices and
# reconstruct the original caption byte-for-byte; anything that does not
# match exactly falls back to the original (headline, facts, cta) unchanged.
# ---------------------------------------------------------------------------

_STORY_H = "One clear next step for your team."
_STORY_BODY = [
    "Monday follow up keeps the plan moving. See https://lasso.example/growth for the full plan.",
    "Tuesday the team reviews the numbers together.",
    "Monday follow up keeps the plan moving. See https://lasso.example/growth for the full plan.",  # intentional repeat
    "Thursday is for the wins worth saving #lasso #growthplan.",
    "Friday closes the loop with one short recap.",
]
_STORY_CTA = "Book a growth call"
_STORY_CAPTION = "\n\n".join([_STORY_H] + _STORY_BODY + [_STORY_CTA])


def _story_row(caption=_STORY_CAPTION, **kw):
    kw.setdefault("fmt", "story")
    kw.setdefault("gym_id", "lasso")
    return _row("lasso-story-dedup-1", caption=caption, **kw)


def _run_story_engine(monkeypatch, row, seen):
    """Drive the real generate_variant_image seam for an owned LASSO Story row
    with the artifact/review dependencies faked, recording the exact fields
    handed to the generate engine."""
    from agent import infographic_artifacts, lasso_current_artifact
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    monkeypatch.setenv("AGENT_LASSO_INFOGRAPHIC_QUALITY", "true")
    monkeypatch.setattr(lasso_current_artifact, "artifact_current", lambda *a, **k: True)
    def save_source(self, account_key, url, path, source):
        seen["saved_source"] = dict(source)
        return {}
    monkeypatch.setattr(infographic_artifacts.ArtifactStore, "save", save_source)
    monkeypatch.setattr(vr, "_attest_reviewed_story_dimensions", lambda path: True)
    from agent import content_planner
    monkeypatch.setattr(content_planner, "load_source_doc", lambda: type(
        "Doc", (), {"ctas": [_STORY_CTA]})())

    def fake_generate(headline, facts, **kwargs):
        seen.update(headline=headline, facts=list(facts), kwargs=kwargs)
        return {"path": "/tmp/story-dedup.png", "prompt": "p"}

    paired = {"feed": dict(row, format="feed"), "artifact": {"id": "a1"}}
    out = vr.generate_variant_image(row, "lasso_ig", generate_fn=fake_generate,
                                    paired_feed_reference=paired,
                                    host_fn=lambda path, key: "https://cdn/story-dedup.png")
    return out, paired


def test_owned_story_helper_seven_paragraphs_reconstructs_caption_byte_for_byte():
    """The actual seven-paragraph target: H=P0, body=P1..P5, CTA=P6, and
    join('\\n\\n') of the returned fields must equal the original caption
    byte-for-byte — every word, punctuation mark, ordering, and repeat kept."""
    caption_hash_before = hashlib.sha256(_STORY_CAPTION.encode("utf-8")).hexdigest()
    row = _story_row()
    headline, new_facts, cta = vr._owned_story_caption_fields(
        row, _STORY_H, [_STORY_CAPTION], _STORY_CTA)
    assert headline == _STORY_H
    assert new_facts == _STORY_BODY
    assert cta == _STORY_CTA
    rebuilt = "\n\n".join([headline] + list(new_facts) + [cta])
    assert rebuilt.encode("utf-8") == _STORY_CAPTION.encode("utf-8")
    # source identity untouched: the row dict and its caption hash never change
    assert row["caption"] == _STORY_CAPTION
    assert hashlib.sha256(row["caption"].encode("utf-8")).hexdigest() == caption_hash_before


def test_owned_story_helper_preserves_intentional_body_duplicate_by_index():
    """P1 == P3 on purpose in this caption; the body must keep BOTH, in order."""
    _, new_facts, _ = vr._owned_story_caption_fields(
        _story_row(), _STORY_H, [_STORY_CAPTION], _STORY_CTA)
    assert len(new_facts) == 5
    assert new_facts[0] == new_facts[2]
    assert [i for i, f in enumerate(new_facts) if f == _STORY_BODY[0]] == [0, 2]


def test_owned_story_helper_preserves_headline_and_cta_repeated_in_body():
    caption = "\n\n".join([_STORY_H, _STORY_H, _STORY_CTA, _STORY_CTA])
    fields = vr._owned_story_caption_fields(_story_row(caption), _STORY_H,
                                           [caption], _STORY_CTA)
    assert fields == (_STORY_H, [_STORY_H, _STORY_CTA], _STORY_CTA)
    assert "\n\n".join([fields[0], *fields[1], fields[2]]) == caption


def test_owned_story_engine_falls_back_on_repeated_blank_separator(monkeypatch):
    caption = _STORY_CAPTION.replace("\n\n", "\n\n\n\n", 1)
    seen = {}
    out, _ = _run_story_engine(monkeypatch, _story_row(caption), seen)
    assert out["ok"] is True
    assert seen["facts"] == [caption]


def test_owned_story_helper_keeps_narrative_urls_and_hashtags():
    _, new_facts, _ = vr._owned_story_caption_fields(
        _story_row(), _STORY_H, [_STORY_CAPTION], _STORY_CTA)
    body = "\n\n".join(new_facts)
    assert "https://lasso.example/growth" in body
    assert "#lasso" in body and "#growthplan" in body


@pytest.mark.parametrize("headline,facts,cta,label", [
    # headline is not exactly P0 (drifted wording)
    ("A different headline.", [_STORY_CAPTION], _STORY_CTA, "nonexact_headline"),
    # headline spans more than one sentence of P0
    ("One clear next step for your team. Extra sentence.", [_STORY_CAPTION],
     _STORY_CTA, "multi_sentence_headline"),
    # CTA only partially matches P6
    (_STORY_H, [_STORY_CAPTION], "Book a growth", "partial_cta"),
    # CTA differs only by case from P6
    (_STORY_H, [_STORY_CAPTION], "book a growth call", "case_different_cta"),
])
def test_owned_story_helper_falls_back_to_original_fields(headline, facts, cta, label):
    """Anything that is not the exact audited structure must return the
    ORIGINAL (headline, facts, cta) untouched — never a partial slice."""
    out = vr._owned_story_caption_fields(_story_row(), headline, facts, cta)
    assert out == (headline, facts, cta), label


def test_owned_story_helper_falls_back_when_only_headline_and_cta():
    """Caption of exactly H + CTA leaves an empty residual body — conservative
    fallback, never an empty facts list that would trip the no-fabrication gate."""
    caption = _STORY_H + "\n\n" + _STORY_CTA
    original = (_STORY_H, [caption], _STORY_CTA)
    out = vr._owned_story_caption_fields(_story_row(caption=caption), *original)
    assert out == original


@pytest.mark.parametrize("caption,label", [
    ("  " + _STORY_CAPTION, "leading_raw_whitespace"),
    (_STORY_CAPTION + "\n", "trailing_raw_whitespace"),
    ("\t" + _STORY_CAPTION + "  ", "both_sides_raw_whitespace"),
])
def test_owned_story_helper_falls_back_on_raw_caption_edge_whitespace(caption, label):
    """A caption that is not exactly its own .strip() can never reconstruct
    byte-for-byte from stripped slices — the helper must refuse and return the
    original (headline, facts, cta) untouched."""
    original = (_STORY_H, [caption], _STORY_CTA)
    out = vr._owned_story_caption_fields(_story_row(caption=caption), *original)
    assert out == original, label


@pytest.mark.parametrize("index,padded,label", [
    (0, " " + _STORY_H, "headline_leading_space"),
    (2, _STORY_BODY[1] + "  ", "body_trailing_spaces"),
    (4, " " + _STORY_BODY[3], "second_body_copy_leading_space"),
    (6, _STORY_CTA + "\t", "cta_trailing_tab"),
])
def test_owned_story_helper_falls_back_when_a_paragraph_changes_under_strip(
        index, padded, label):
    """Any canonical paragraph carrying its own leading/trailing whitespace
    breaks exact ordered reconstruction — conservative fallback, originals back."""
    parts = [_STORY_H] + list(_STORY_BODY) + [_STORY_CTA]
    parts[index] = padded
    caption = "\n\n".join(parts)
    original = (_STORY_H, [caption], _STORY_CTA)
    out = vr._owned_story_caption_fields(_story_row(caption=caption), *original)
    assert out == original, label


def test_owned_story_engine_seam_removes_field_duplicates_and_keeps_source(monkeypatch):
    """Through the real generate_variant_image seam, the owned Story branch hands
    the engine headline=P0, facts=P1..P5, cta=P6 — the headline and CTA no
    longer duplicated inside the body — while the row, its caption hash, the
    approved footer path, and the paired feed reference all stay unchanged."""
    seen = {}
    row = _story_row()
    hash_before = hashlib.sha256(row["caption"].encode("utf-8")).hexdigest()
    out, paired = _run_story_engine(monkeypatch, row, seen)

    assert out["ok"] is True
    assert seen["headline"] == _STORY_H
    assert seen["facts"] == _STORY_BODY
    assert seen["kwargs"]["cta"] == _STORY_CTA
    # exact byte reconstruction of what the renderer consumed
    rebuilt = "\n\n".join([seen["headline"]] + seen["facts"] + [seen["kwargs"]["cta"]])
    assert rebuilt.encode("utf-8") == _STORY_CAPTION.encode("utf-8")
    # headline/CTA appear nowhere inside the rendered body facts
    assert all(_STORY_H not in f for f in seen["facts"])
    assert all(_STORY_CTA not in f for f in seen["facts"])
    # source identity, approved footer, and paired feed reference unchanged
    assert row["caption"] == _STORY_CAPTION
    assert hashlib.sha256(row["caption"].encode("utf-8")).hexdigest() == hash_before
    assert seen["kwargs"]["footer"] is None
    assert seen["kwargs"]["paired_feed_reference"] is paired
    assert seen["saved_source"] == {
        "source_id": "content_calendar:" + row["id"] + ":caption",
        "source_hash": hash_before,
    }


def test_lasso_feed_path_retains_complete_original_caption(monkeypatch):
    """The feed path is not the Story de-dup path: the engine still receives the
    ENTIRE original caption as its single fact, headline duplication included."""
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    monkeypatch.setenv("AGENT_LASSO_INFOGRAPHIC_QUALITY", "true")
    from agent import infographic_artifacts
    monkeypatch.setattr(infographic_artifacts.ArtifactStore, "save",
                        lambda *args, **kwargs: {})
    from agent import content_planner
    monkeypatch.setattr(content_planner, "load_source_doc", lambda: type(
        "Doc", (), {"ctas": [_STORY_CTA]})())
    seen = {}

    def fake_generate(headline, facts, **kwargs):
        seen.update(headline=headline, facts=list(facts), kwargs=kwargs)
        return {"path": "/tmp/feed.png", "prompt": "p"}

    row = _row("lasso-feed-1", gym_id="lasso", fmt="feed", caption=_STORY_CAPTION)
    out = vr.generate_variant_image(row, "lasso_ig", generate_fn=fake_generate,
                                    host_fn=lambda path, key: "https://cdn/feed.png")
    assert out["ok"] is True
    assert seen["facts"] == [_STORY_CAPTION]
    assert seen["headline"] == _STORY_H
    assert seen["kwargs"]["cta"] == _STORY_CTA
    assert seen["kwargs"]["footer"] is None
    assert row["caption"] == _STORY_CAPTION


def test_client_story_retains_legacy_inputs_and_never_allocates_owned_copy(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    monkeypatch.setenv("AGENT_LASSO_INFOGRAPHIC_QUALITY", "true")
    def forbidden_allocation(*args):
        raise AssertionError("Client Story entered owned copy allocation")
    monkeypatch.setattr(vr, "_owned_story_caption_fields", forbidden_allocation)
    caption = "Client hook. Client detail. Book a growth call."
    row = _row("client-story", gym_id="eng", fmt="story", caption=caption)
    seen = {}
    def generate(headline, facts, **kwargs):
        seen.update(headline=headline, facts=facts, kwargs=kwargs)
        return {"path": "/tmp/client-story.png", "prompt": "offline"}
    result = vr.generate_variant_image(row, "eng", generate_fn=generate,
                                      host_fn=lambda *args: "https://cdn/client-story.png")
    assert result["ok"] is True
    assert seen["headline"] == row["pillar"]
    assert seen["facts"] == ["Client hook", "Client detail", "Book a growth call."]
    assert "cta" not in seen["kwargs"] and "paired_feed_reference" not in seen["kwargs"]
    assert row["caption"] == caption


# ---------------------------------------------------------------------------
# variant_regen_brief — the human-typed "type what you want" companion
# ---------------------------------------------------------------------------

from agent import variant_regen_brief as vrb  # noqa: E402


def test_variant_regen_brief_disabled_reason_when_flag_off(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "false")
    out = vrb.generate_variant_from_brief(_row("orig-1"), "eng", "a brief")
    assert out == {"ok": False, "reason": vrb.REASON_DISABLED}


def test_variant_regen_brief_empty_brief_guard(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    out = vrb.generate_variant_from_brief(_row("orig-1"), "eng", "   ")
    assert out == {"ok": False, "reason": vrb.REASON_NO_BRIEF}


def test_variant_regen_brief_uses_typed_brief_as_headline_and_grounds_in_sources(monkeypatch):
    """The human's typed brief becomes the rendered headline; the gym's own
    on-file approved material grounds the supporting facts -- nothing invented."""
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    from agent import client_sources
    monkeypatch.setattr(client_sources, "approved_sources",
                        lambda account_key: [type("S", (), {"text": "Free trial week for new members"})()])
    seen = {}

    def fake_generate(headline, facts, client=None, aspect=None, pixels=None,
                      surface=None, account_key=None):
        seen["headline"] = headline
        seen["facts"] = facts
        seen["account_key"] = account_key
        return {"path": "/tmp/v2.png", "prompt": "p", "model": "astra"}

    out = vrb.generate_variant_from_brief(
        _row("orig-1"), "eng", "highlight our new 6am class",
        generate_fn=fake_generate, host_fn=lambda path, key: "https://cdn/v2.png")
    assert out["ok"] is True
    assert seen["headline"] == "highlight our new 6am class"
    assert seen["facts"] == ["Free trial week for new members"]
    assert seen["account_key"] == "eng"


def test_variant_regen_brief_falls_back_to_the_brief_itself_when_no_sources_on_file(monkeypatch):
    """A brand-new gym with nothing approved/pending yet still gets a card from
    its own typed brief -- never a hard failure, and never an invented fact."""
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    from agent import client_sources
    monkeypatch.setattr(client_sources, "approved_sources", lambda account_key: [])
    monkeypatch.setattr(client_sources, "pending_sources", lambda account_key: [])
    seen = {}

    def fake_generate(headline, facts, **kw):
        seen["facts"] = facts
        return {"path": "/tmp/v2.png", "prompt": "p"}

    out = vrb.generate_variant_from_brief(
        _row("orig-1"), "eng", "a brand new ask",
        generate_fn=fake_generate, host_fn=lambda path, key: "https://cdn/v2.png")
    assert out["ok"] is True
    assert seen["facts"] == ["a brand new ask"]


def test_variant_regen_brief_hosting_failure_reason(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    out = vrb.generate_variant_from_brief(
        _row("orig-1"), "eng", "a brief",
        generate_fn=lambda *a, **k: {"path": "/tmp/x.png"},
        host_fn=lambda path, key: None)
    assert out == {"ok": False, "reason": vrb.REASON_HOSTING}


def test_variant_regen_brief_generate_failure_reason(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")
    out = vrb.generate_variant_from_brief(
        _row("orig-1"), "eng", "a brief", generate_fn=lambda *a, **k: None)
    assert out == {"ok": False, "reason": vrb.REASON_GENERATE_FAILED}
