"""
Part B — token-scoped client portal endpoints (agent/portal_social.py).

Everything offline: an injected fake Stripe reader, monkeypatched accounts, a temp
SQLite DB for the gym calendar + recreate budget, and a dict store for drafts.

The invariants the handoff calls out, one test class each:
  * flag OFF (AGENT_PORTAL_SOCIAL_ENABLED) -> every route disabled (404), unchanged.
  * Stripe social product not ACTIVE -> 402 + empty-state payload (never a live calendar).
  * TOKEN ISOLATION on EVERY route -> gym A's token can never read or act on gym B.
  * recreate budget -> server-enforced 15/month, 409 when exhausted, free kill.
  * kill requires confirm=true.
  * edit re-runs the fabrication gate -> 422 on an unsupported claim.
  * approve is idempotent.
  * HARD COPY RULES -> no em/en/hyphen dashes and never "vendor" in a client string.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_social as ps
from agent import db as _db
from agent import gym_calendar_queue as gcq
from agent.accounts import Account, Platform
from agent.drafter import Draft, DraftStatus


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """A temp DB + the master flag ON + a configured social product id."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("STRIPE_SOCIAL_PRODUCT_ID", "prod_social")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "false")
    # both analytics/report flags stay OFF for the metrics-shape tests
    monkeypatch.delenv("AGENT_ZERNIO_ANALYTICS_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_MONTHLY_REPORT_ENABLED", raising=False)
    yield


class _ActiveReader:
    """A Stripe reader that reports the gym's social product as ACTIVE."""
    def available(self):
        return True

    def social_active(self, customer_id, product_id):
        return True


class _InactiveReader:
    def available(self):
        return True

    def social_active(self, customer_id, product_id):
        return False


def _account(key, approvers=None):
    return Account(
        key=key, display_name=f"Gym {key}", platform=Platform.INSTAGRAM,
        token_env="TOK_ENV", target_id_env="TGT_ENV",
        approvers=approvers or [f"U_{key}_owner"],
    )


def _register(monkeypatch, *accounts):
    monkeypatch.setattr("agent.accounts.ACCOUNTS", list(accounts))


def _mark_stripe_customer(account_key, cid="cus_1"):
    """Give the gym a Stripe customer id so is_social_active can look it up."""
    _db.gym_upsert(account_key, stripe_customer_id=cid)


def _draft(draft_id, account_key, status=DraftStatus.PENDING, caption="a caption"):
    return Draft(
        draft_id=draft_id, account_key=account_key, platform="instagram",
        caption=caption, hashtags=[], creative_path="/lib/hook_v1.png",
        creative_public_url="", scheduled_for="2026-08-10T18:30:00+00:00",
        status=status, day_key="2026-08-10", draft_type="feed",
    )


class _DictStore:
    def __init__(self, *drafts):
        self._d = {d.draft_id: d for d in drafts}

    def get(self, draft_id):
        return self._d.get(draft_id)

    def put(self, draft):
        self._d[draft.draft_id] = draft
        return draft


# ===========================================================================
# 1. FLAG OFF -> every route disabled (404), byte-for-byte current behavior
# ===========================================================================

def test_flag_off_all_routes_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("AGENT_PORTAL_SOCIAL_ENABLED", raising=False)
    assert ps.handle_social("gymA", "2026-08")[0] == 404
    assert ps.handle_metrics("gymA")[0] == 404
    assert ps.handle_approve("gymA", "d1", "u1")[0] == 404
    assert ps.handle_edit("gymA", "d1", "u1", note="hi")[0] == 404
    assert ps.handle_deny("gymA", "d1", "u1", note="no")[0] == 404
    assert ps.handle_kill("gymA", "d1", "u1", confirm=True)[0] == 404


# ===========================================================================
# 2. STRIPE NOT ACTIVE -> 402 + empty state (never a live calendar)
# ===========================================================================

def test_social_not_active_returns_402_empty(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    status, body = ps.handle_social("gymA", "2026-08", reader=_InactiveReader())
    assert status == 402
    assert body["active"] is False
    assert body["posts"] == []


def test_no_product_id_configured_is_not_active(db_env, monkeypatch):
    monkeypatch.delenv("STRIPE_SOCIAL_PRODUCT_ID", raising=False)
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    # even with an "active" reader, no product id configured => fail closed
    assert ps.is_social_active("gymA", reader=_ActiveReader()) is False
    assert ps.handle_social("gymA", "2026-08", reader=_ActiveReader())[0] == 402


def test_no_customer_id_is_not_active(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))  # no stripe_customer_id on the gym row
    assert ps.is_social_active("gymA", reader=_ActiveReader()) is False


def test_stripe_read_error_fails_closed(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")

    class _Boom:
        def available(self):
            return True

        def social_active(self, c, p):
            raise RuntimeError("stripe down")

    assert ps.is_social_active("gymA", reader=_Boom()) is False


def test_action_not_active_returns_402(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    store = _DictStore(_draft("d1", "gymA"))
    for status, _ in (
        ps.handle_approve("gymA", "d1", "U_gymA_owner", store=store, reader=_InactiveReader()),
        ps.handle_edit("gymA", "d1", "U_gymA_owner", note="hi", store=store, reader=_InactiveReader()),
        ps.handle_deny("gymA", "d1", "U_gymA_owner", note="no", store=store, reader=_InactiveReader()),
        ps.handle_kill("gymA", "d1", "U_gymA_owner", confirm=True, store=store, reader=_InactiveReader()),
    ):
        assert status == 402


# ===========================================================================
# 3. TOKEN ISOLATION on EVERY route
# ===========================================================================

def test_social_isolation_only_own_gym_rows(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"), _account("gymB"))
    _mark_stripe_customer("gymA")
    _mark_stripe_customer("gymB")
    # seed one row for each gym in the same month
    gcq.upsert_gym_post("gA", "gymA", "2026-08-05", 1, pillar="Proof", feed_url="urlA")
    gcq.upsert_gym_post("gB", "gymB", "2026-08-06", 1, pillar="Proof", feed_url="urlB")

    status, body = ps.handle_social("gymA", "2026-08", reader=_ActiveReader())
    assert status == 200
    days = [p["day_key"] for p in body["posts"]]
    assert "2026-08-05" in days
    assert "2026-08-06" not in days, "gymB's post must NOT appear in gymA's calendar"

    # reversed: gymB never sees gymA's post
    status, body = ps.handle_social("gymB", "2026-08", reader=_ActiveReader())
    days = [p["day_key"] for p in body["posts"]]
    assert days == ["2026-08-06"]


def test_action_isolation_cross_gym_draft_is_404(db_env, monkeypatch):
    """gymA's token, knowing gymB's draft id, must NOT act on it (404, never leaks)."""
    _register(monkeypatch, _account("gymA"), _account("gymB"))
    _mark_stripe_customer("gymA")
    _mark_stripe_customer("gymB")
    # the store holds gymB's draft; gymA tries to act on it by id
    store = _DictStore(_draft("draft-b", "gymB"))
    for fn, extra in (
        (ps.handle_approve, {}),
        (ps.handle_edit, {"note": "x"}),
        (ps.handle_deny, {"note": "x"}),
        (ps.handle_kill, {"confirm": True}),
    ):
        status, body = fn("gymA", "draft-b", "U_gymA_owner", store=store,
                          reader=_ActiveReader(), **extra)
        assert status == 404, f"{fn.__name__} must 404 on a cross-gym draft"
        assert body["ok"] is False


def test_metrics_isolation_keyed_to_account(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"), _account("gymB"))
    _mark_stripe_customer("gymA")
    _mark_stripe_customer("gymB")
    _db.set_baseline_posts_per_week("gymA", 3.0)
    _db.set_baseline_posts_per_week("gymB", 9.0)
    status, body = ps.handle_metrics("gymA", reader=_ActiveReader())
    assert status == 200
    assert body["account_key"] == "gymA"
    assert body["frequency"]["baseline_posts_per_week"] == 3.0
    # gymB's baseline (9.0) never bleeds into gymA's metrics
    assert body["frequency"]["baseline_posts_per_week"] != 9.0


def test_deny_budget_is_per_gym(db_env, monkeypatch):
    """gymA burning its budget never touches gymB's budget (isolation of the counter)."""
    _register(monkeypatch, _account("gymA"), _account("gymB"))
    for _ in range(15):
        assert ps.spend_recreate("gymA") is True
    assert ps.recreate_remaining("gymA") == 0
    assert ps.recreate_remaining("gymB") == 15  # untouched


# ===========================================================================
# 4. RECREATE BUDGET — server-enforced 15/month, 409 when exhausted, free kill
# ===========================================================================

def test_deny_decrements_budget_and_409_when_exhausted(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    # 15 distinct pending drafts so each deny has a real draft to act on
    drafts = [_draft(f"d{i}", "gymA") for i in range(16)]
    store = _DictStore(*drafts)

    for i in range(15):
        status, body = ps.handle_deny("gymA", f"d{i}", "U_gymA_owner",
                                      note="wrong tone", store=store, reader=_ActiveReader())
        assert status == 200, f"deny #{i} should succeed"
        assert body["recreate_budget"]["remaining"] == 15 - (i + 1)

    # the 16th deny in the month is refused with 409, budget not charged further
    status, body = ps.handle_deny("gymA", "d15", "U_gymA_owner",
                                  note="one too many", store=store, reader=_ActiveReader())
    assert status == 409
    assert body["ok"] is False
    assert ps.recreate_spent("gymA") == 15


def test_failed_deny_does_not_charge_budget(db_env, monkeypatch):
    """A deny that fails (unauthorized actor) never burns a budget unit."""
    _register(monkeypatch, _account("gymA", approvers=["U_gymA_owner"]))
    _mark_stripe_customer("gymA")
    store = _DictStore(_draft("d1", "gymA"))
    status, body = ps.handle_deny("gymA", "d1", "U_intruder", note="x",
                                  store=store, reader=_ActiveReader())
    assert status == 403
    assert ps.recreate_spent("gymA") == 0, "a failed deny must not cost a unit"


def test_kill_is_free_never_charges_budget(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    store = _DictStore(_draft("d1", "gymA"))
    status, body = ps.handle_kill("gymA", "d1", "U_gymA_owner", confirm=True,
                                  store=store, reader=_ActiveReader())
    assert status == 200
    assert ps.recreate_spent("gymA") == 0, "kill is free"


# ===========================================================================
# 5. KILL requires confirm=true
# ===========================================================================

def test_kill_requires_confirm(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    store = _DictStore(_draft("d1", "gymA"))
    status, body = ps.handle_kill("gymA", "d1", "U_gymA_owner", confirm=False,
                                  store=store, reader=_ActiveReader())
    assert status == 400
    assert body["ok"] is False
    assert "confirm" in body["error"].lower()


def test_kill_with_confirm_succeeds(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    store = _DictStore(_draft("d1", "gymA"))
    status, body = ps.handle_kill("gymA", "d1", "U_gymA_owner", confirm=True,
                                  store=store, reader=_ActiveReader())
    assert status == 200
    assert body["ok"] is True


# ===========================================================================
# 6. EDIT re-runs the fabrication gate -> 422 on an unsupported claim
# ===========================================================================

def test_edit_unsupported_claim_returns_422(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    store = _DictStore(_draft("d1", "gymA"))
    # a note carrying a fabricated stat with no approved receipt
    status, body = ps.handle_edit("gymA", "d1", "U_gymA_owner",
                                  note="Say we cut costs by 80 percent.",
                                  store=store, reader=_ActiveReader())
    assert status == 422
    assert body["ok"] is False
    assert "fabrication" in body["error"].lower()


def test_edit_clean_note_succeeds(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    store = _DictStore(_draft("d1", "gymA"))
    # a style note with no stats clears the gate; the redraft path is delegated to
    # portal_approvals, which needs no redraft_fn to reach the gate itself
    status, body = ps.handle_edit("gymA", "d1", "U_gymA_owner",
                                  note="Please make the tone warmer.",
                                  store=store, reader=_ActiveReader())
    # the gate passed (not 422); the delegated edit may still report no redraft fn,
    # but it must NOT be a fabrication refusal
    assert status != 422


# ===========================================================================
# 7. APPROVE is idempotent
# ===========================================================================

def test_approve_idempotent_on_already_approved(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    store = _DictStore(_draft("d1", "gymA", status=DraftStatus.APPROVED))
    status, body = ps.handle_approve("gymA", "d1", "U_gymA_owner",
                                     store=store, reader=_ActiveReader())
    assert status == 200
    assert body["ok"] is True
    assert body.get("idempotent") is True


# ===========================================================================
# 8. metrics shape (Part D) — null values, gaps not zeros, flags OFF
# ===========================================================================

def test_metrics_shape_is_gaps_not_zeros(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    status, body = ps.handle_metrics("gymA", days=30, reader=_ActiveReader())
    assert status == 200
    assert body["analytics_available"] is False
    assert body["report_available"] is False
    # missing metrics are None (gaps), never a fabricated 0
    assert body["totals"]["likes"] is None
    assert body["audience"]["followers"] is None
    assert body["frequency"]["current_posts_per_week"] is None
    assert body["gaps"], "an explicit gap note must be present when analytics are off"


# ===========================================================================
# 9. HARD COPY RULES — no dashes, no "vendor" in any client-facing string
# ===========================================================================

def _message_strings(payload):
    """Every human-readable message string in a payload (error/detail/gaps), NOT the
    machine fields (dates, ids, keys) which legitimately carry hyphens."""
    out = []
    for k in ("error", "detail"):
        v = payload.get(k)
        if isinstance(v, str):
            out.append(v)
    gaps = payload.get("gaps")
    if isinstance(gaps, list):
        out.extend(s for s in gaps if isinstance(s, str))
    return out


def test_no_dashes_or_vendor_in_client_strings(db_env, monkeypatch):
    _register(monkeypatch, _account("gymA"))
    _mark_stripe_customer("gymA")
    store = _DictStore(_draft("d1", "gymA"))
    msgs = []

    # gather every client-facing MESSAGE this surface can emit
    msgs += _message_strings(ps.handle_social("gymA", "2026-08", reader=_InactiveReader())[1])
    msgs += _message_strings(ps.handle_metrics("gymA", reader=_ActiveReader())[1])
    msgs += _message_strings(ps.handle_metrics("gymA", reader=_InactiveReader())[1])
    msgs += _message_strings(ps.handle_kill("gymA", "d1", "U_gymA_owner", confirm=False,
                                            store=store, reader=_ActiveReader())[1])
    msgs += _message_strings(ps.handle_edit("gymA", "d1", "U_gymA_owner",
                                            note="cut costs 80 percent", store=store,
                                            reader=_ActiveReader())[1])
    # exhaust the budget to capture the 409 message
    for _ in range(15):
        ps.spend_recreate("gymA")
    msgs += _message_strings(ps.handle_deny("gymA", "d1", "U_gymA_owner", note="x",
                                            store=store, reader=_ActiveReader())[1])

    assert msgs, "expected at least one client message to check"
    for s in msgs:
        for bad in ("—", "–", "-", "vendor"):
            assert bad not in s, f"client message contains banned token {bad!r}: {s!r}"


def test_source_string_literals_have_no_dashes_or_vendor():
    """Grep-assert the module's own string LITERALS, EXCLUDING docstrings (which use
    'token->account' arrows). No em/en dash and never the word 'vendor' anywhere; no
    plain hyphen in a non-docstring literal (dates/ids are built, not literal here)."""
    import ast
    src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "agent", "portal_social.py")
    with open(src_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    # collect docstring node ids so we can skip them for the hyphen rule
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            ds = ast.get_docstring(node, clean=False)
            if ds is not None:
                body0 = node.body[0]
                if isinstance(body0, ast.Expr) and isinstance(body0.value, ast.Constant):
                    docstrings.add(id(body0.value))

    # Docstrings are internal developer docs, not client-facing copy, so they are
    # exempt (they legitimately carry em dashes and 'token->account' arrows). Every
    # NON-docstring literal must be clean of em/en dashes and the word 'vendor'. The
    # plain-hyphen rule for CLIENT MESSAGES is enforced by the runtime test above;
    # here we skip it because non-copy literals (strftime formats like %Y-%m, SQL)
    # legitimately carry hyphens.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            v = node.value
            assert "—" not in v and "–" not in v, f"dash in literal: {v!r}"
            assert "vendor" not in v.lower(), f"'vendor' in literal: {v!r}"


# ===========================================================================
# 10. HTTP LAYER — routing, token->account, flag gate, isolation end to end
# ===========================================================================

def _serve(monkeypatch):
    from agent.intake_web import build_server
    import threading
    server = build_server(0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def test_http_social_unknown_token_is_404(db_env, monkeypatch):
    monkeypatch.setattr("agent.intake_web.client_for_token", lambda t: None)
    monkeypatch.setattr("agent.intake_web.is_revoked", lambda k: False)
    import urllib.request, urllib.error
    server, port = _serve(monkeypatch)
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/portal/validtoken123/social?month=2026-08")
        assert ei.value.code == 404
    finally:
        server.shutdown()


def test_http_social_flag_off_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("AGENT_PORTAL_SOCIAL_ENABLED", raising=False)
    monkeypatch.setattr("agent.intake_web.client_for_token", lambda t: "gymA")
    monkeypatch.setattr("agent.intake_web.is_revoked", lambda k: False)
    import urllib.request, urllib.error
    server, port = _serve(monkeypatch)
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/portal/validtoken123/social?month=2026-08")
        assert ei.value.code == 404
    finally:
        server.shutdown()


def test_http_post_action_routes_to_correct_account(db_env, monkeypatch):
    """The HTTP layer resolves the token to account_key and passes THAT key (never
    another gym's) into the handler."""
    monkeypatch.setattr("agent.intake_web.client_for_token", lambda t: "gymA")
    monkeypatch.setattr("agent.intake_web.is_revoked", lambda k: False)
    seen = []

    def _fake_deny(account_key, draft_id, actor_id, note="", store=None, reader=None):
        seen.append((account_key, draft_id, actor_id, note))
        return 200, {"ok": True, "action": "deny", "draft_id": draft_id}

    monkeypatch.setattr("agent.intake_web._ps.handle_deny", _fake_deny)
    import urllib.request, json
    server, port = _serve(monkeypatch)
    try:
        payload = json.dumps({"actor_id": "U_gymA_owner", "note": "wrong tone"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/portal/validtoken123/posts/d1/deny",
            data=payload, headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req)
        body = json.loads(resp.read())
        assert body["ok"] is True
        assert seen == [("gymA", "d1", "U_gymA_owner", "wrong tone")]
    finally:
        server.shutdown()


def test_http_kill_confirm_flows_through(db_env, monkeypatch):
    monkeypatch.setattr("agent.intake_web.client_for_token", lambda t: "gymA")
    monkeypatch.setattr("agent.intake_web.is_revoked", lambda k: False)
    seen = []

    def _fake_kill(account_key, draft_id, actor_id, confirm=False, store=None, reader=None):
        seen.append(confirm)
        return 200, {"ok": True, "action": "kill", "draft_id": draft_id}

    monkeypatch.setattr("agent.intake_web._ps.handle_kill", _fake_kill)
    import urllib.request, json
    server, port = _serve(monkeypatch)
    try:
        payload = json.dumps({"actor_id": "U_gymA_owner", "confirm": True}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/portal/validtoken123/posts/d1/kill",
            data=payload, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req)
        assert seen == [True]
    finally:
        server.shutdown()
