"""
DRAFT durable swap action receipts v3 (2026-10-04): an explicit opaque
action_id binds one portal photo swap to one tenant-scoped receipt via the
three SECURITY DEFINER RPCs (begin / claim_selection / apply). The handler
runs begin BEFORE any row read, derives the swap group ONLY from the claim's
frozen member manifest (gym, logical_post_id, active), writes the group with
exactly ONE apply RPC (never per-row swap_media), and reports success ONLY
from the persisted terminal receipt.

Everything offline: a fake store simulates the SQL contract in memory, the
picker is injected and counted, ledgers are stubbed. No network. Row ids are
real UUIDs (the SQL contract is uuid-typed). Flag: ECHO_SWAP_ACTION_RECEIPT
(default OFF; armed per test).
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import media_swap as msw          # noqa: E402
from agent import portal_calendar_store as pcs  # noqa: E402
from agent import portal_social as ps        # noqa: E402

GYM = "zanshin"
ROW1 = "11111111-1111-4111-8111-111111111111"   # primary feed
ROW2 = "22222222-2222-4222-8222-222222222222"   # same logical post (story)
ROW3 = "33333333-3333-4333-8333-333333333333"   # same date+media, OTHER post
ROW4 = "44444444-4444-4444-8444-444444444444"   # candidate variant of the post
LOGICAL1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LOGICAL2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT", "true")
    # default test env: this gym IS on the tenant allowlist (empty config
    # would fail closed for every gym -- see the allowlist tests below)
    monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT_GYMS", "zanshin")
    yield


def _row(row_id=ROW1, gym_id=GYM, status="pending", fmt="feed",
         logical=LOGICAL1, variant_status="active",
         image_url="https://cdn/old.jpg", post_date="2026-10-20"):
    return {"id": row_id, "gym_id": gym_id, "post_date": post_date,
            "account": "instagram", "status": status, "format": fmt,
            "caption": "a caption the gym is happy with",
            "image_url": image_url,
            "source_media_url": "old.jpg",
            "thumbnail_url": None, "source_media_asset_id": None,
            "logical_post_id": logical, "variant_status": variant_status,
            "pillar": "community", "scheduled_at": None}


class _ReceiptStore:
    """In-memory simulation of the v3 SQL contract: the same binding rules,
    the same frozen manifest semantics, the same all-or-nothing apply. The
    per-row swap_media write is a hard error here -- the receipt path must
    never call it."""

    def __init__(self, rows=None):
        self._rows = {r["id"]: dict(r) for r in (rows or [])}
        self.receipts = {}
        self.calls = []            # ordered event log
        self.applies = []          # prepared payloads seen by apply
        self.apply_failures = []   # exceptions raised by apply, replayed in order
        self.claim_winner = None   # a concurrent winner's frozen selection
        self.get_row_may_fail = False

    # -- calendar reads --
    def get_row(self, account_key, row_id):
        self.calls.append(("get_row", row_id))
        if self.get_row_may_fail:
            raise RuntimeError("row read exploded")
        r = self._rows.get(row_id)
        return dict(r) if r and str(r.get("gym_id")) == str(account_key) else None

    def list_active_logical_post_rows(self, account_key, logical_post_id):
        self.calls.append(("list_members", logical_post_id))
        return sorted(
            (dict(r) for r in self._rows.values()
             if str(r.get("gym_id")) == str(account_key)
             and str(r.get("logical_post_id")) == str(logical_post_id)
             and r.get("variant_status") == "active"),
            key=lambda r: r["id"])

    def list_month(self, account_key, month):
        return [dict(r) for r in self._rows.values()]

    def swap_media(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("per-row swap_media write on the receipt path")

    # -- receipt RPCs (SQL contract) --
    def _receipt(self, account_key, action_id):
        return self.receipts.get((account_key, action_id))

    def action_receipt_begin(self, account_key, action_id, action, row_id,
                             actor_id, fingerprint):
        self.calls.append(("begin", action_id))
        existing = self._receipt(account_key, action_id)
        if existing is not None:
            if (existing["action"] != action or existing["row_id"] != row_id
                    or existing["actor_id"] != (actor_id or "")
                    or existing["request_fingerprint"] != fingerprint):
                raise pcs.ReceiptConflictError(
                    409, "conflicting reuse of action_id with a different request binding")
            return copy.deepcopy(existing)
        row = self._rows.get(row_id)
        if row is None or str(row.get("gym_id")) != str(account_key) \
                or not row.get("logical_post_id"):
            raise pcs.ReceiptHoldError(
                409, "primary row has no logical_post_id; conflicting request "
                     "held for manual review")
        before = {k: row.get(k) for k in
                  ("id", "status", "caption", "post_date", "format", "image_url",
                   "thumbnail_url", "source_media_url", "source_media_asset_id")}
        receipt = {"id": len(self.receipts) + 1, "gym_id": account_key,
                   "action_id": action_id, "action": action, "row_id": row_id,
                   "actor_id": actor_id or "", "request_fingerprint": fingerprint,
                   "status": "started", "selected_asset": None,
                   "planned_siblings": None, "member_manifest": None,
                   "before_state": before, "after_state": None,
                   "sibling_outcomes": None, "error": None,
                   "response_status": None}
        self.receipts[(account_key, action_id)] = receipt
        return copy.deepcopy(receipt)

    def _freeze_manifest(self, account_key, receipt):
        primary = self._rows.get(receipt["row_id"])
        members = [r for r in self._rows.values()
                   if str(r.get("gym_id")) == str(account_key)
                   and str(r.get("logical_post_id")) == str(primary.get("logical_post_id"))
                   and r.get("variant_status") == "active"]
        members.sort(key=lambda r: r["id"])
        return {"logical_post_id": primary.get("logical_post_id"),
                "members": [{k: m.get(k) for k in
                             ("format", "account", "post_date", "status", "caption",
                              "image_url", "thumbnail_url", "source_media_url",
                              "source_media_asset_id")}
                            | {"calendar_row_id": m["id"]} for m in members]}

    def action_receipt_claim(self, account_key, action_id, fingerprint,
                             selected_asset, planned_siblings):
        self.calls.append(("claim", action_id))
        receipt = self._receipt(account_key, action_id)
        assert receipt is not None
        if receipt["request_fingerprint"] != fingerprint:
            raise pcs.ReceiptConflictError(409, "fingerprint mismatch")
        if receipt["status"] in ("succeeded", "failed") \
                or receipt["selected_asset"] is not None:
            return copy.deepcopy(receipt)   # loser/replayer gets the winner
        if self.claim_winner is not None:
            receipt["selected_asset"] = copy.deepcopy(
                self.claim_winner["selected_asset"])
            receipt["planned_siblings"] = copy.deepcopy(
                self.claim_winner.get("planned_siblings") or {})
        else:
            receipt["selected_asset"] = copy.deepcopy(selected_asset)
            receipt["planned_siblings"] = copy.deepcopy(planned_siblings or {})
        receipt["member_manifest"] = self._freeze_manifest(account_key, receipt)
        receipt["status"] = "selected"
        return copy.deepcopy(receipt)

    def action_receipt_apply(self, account_key, action_id, fingerprint, prepared):
        self.calls.append(("apply", action_id))
        self.applies.append(copy.deepcopy(prepared))
        if self.apply_failures:
            raise self.apply_failures.pop(0)
        receipt = self._receipt(account_key, action_id)
        assert receipt is not None
        if receipt["request_fingerprint"] != fingerprint:
            raise pcs.ReceiptConflictError(409, "fingerprint mismatch")
        if receipt["status"] in ("succeeded", "failed"):
            return copy.deepcopy(receipt)
        manifest = receipt.get("member_manifest") or {}
        members = manifest.get("members") or []
        manifest_ids = sorted(m["calendar_row_id"] for m in members)
        prepared_ids = sorted(r["calendar_row_id"] for r in prepared.get("rows", []))
        if prepared_ids != manifest_ids:
            raise pcs.ReceiptConflictError(409, "prepared rows must match the "
                                                "frozen member manifest exactly")
        current_ids = sorted(r["id"] for r in self._rows.values()
                             if str(r.get("gym_id")) == str(account_key)
                             and str(r.get("logical_post_id")) == str(manifest.get("logical_post_id"))
                             and r.get("variant_status") == "active")
        if current_ids != manifest_ids:
            raise pcs.ReceiptConflictError(409, "active logical-post membership "
                                                "changed since selection")
        media_by_id = {r["calendar_row_id"]: r["media"] for r in prepared["rows"]}
        outcomes = []
        for member in members:
            rid = member["calendar_row_id"]
            row = self._rows[rid]
            if row.get("status") not in ("pending", "coach_review") \
                    or str(row.get("image_url") or "") != str(member.get("image_url") or ""):
                raise pcs.ReceiptConflictError(409, f"stale eligible sibling or "
                                                    f"media hold on row {rid}")
        for member in members:
            rid = member["calendar_row_id"]
            row = self._rows[rid]
            media = media_by_id[rid]
            row["image_url"] = media.get("image_url")
            row["source_media_url"] = media.get("source_media_url")
            row["thumbnail_url"] = media.get("thumbnail_url")
            row["source_media_asset_id"] = media.get("source_media_asset_id")
            outcomes.append({"id": rid, "swapped": True,
                             "image_url": media.get("image_url"),
                             "source_media_asset_id": media.get("source_media_asset_id")})
        primary = self._rows[receipt["row_id"]]
        receipt["status"] = "succeeded"
        receipt["after_state"] = {k: primary.get(k) for k in
                                  ("id", "status", "caption", "post_date", "format",
                                   "image_url", "thumbnail_url", "source_media_url",
                                   "source_media_asset_id")}
        receipt["sibling_outcomes"] = outcomes
        receipt["response_status"] = 200
        return copy.deepcopy(receipt)

    def receipt(self, action_id, gym=GYM):
        return copy.deepcopy(self.receipts.get((gym, action_id)))


def _picker(calls, url="https://cdn/new.jpg", asset_id="asset-1"):
    def _pick(_gym, _row, siblings=(), **_kw):
        calls.append(sorted(str(s["id"]) for s in siblings))
        base = {"ok": True, "image_url": url, "source_media_url": None,
                "key": "new.jpg", "kind": "photo", "source": "local",
                "thumbnail_url": "", "source_media_asset_id": asset_id,
                "path": ""}
        return dict(base, siblings={str(s["id"]): dict(base) for s in siblings})
    return _pick


def _wire(monkeypatch):
    ledger = {"reserve": [], "release": [], "settle": []}
    monkeypatch.setattr(msw, "after_swap",
                        lambda *a, **k: ledger["settle"].append(a))
    monkeypatch.setattr(msw, "reserve_local_pick",
                        lambda *a, **k: ledger["reserve"].append(a[2]) or True)
    monkeypatch.setattr(msw, "release_local_pick",
                        lambda pick, *a, **k: ledger["release"].append(pick) or True)
    monkeypatch.setattr(msw, "sibling_rows", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("date/media sibling inference on the receipt path")))
    return ledger


# ---- action_id shape / flag gating ------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", 123, None, {"x": 1},
                                 ["a"], "has space", "x" * 129, "bad?chars"])
def test_malformed_action_id_is_400_and_never_touches_the_store(monkeypatch, bad):
    store = _ReceiptStore([_row()])
    _wire(monkeypatch)
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id=bad,
                                        sb_store=store, picker=_picker([]))
    assert status == 400 and body["reason"] == "invalid_action_id"
    assert store.calls == [] and store.receipts == {}


def test_action_id_with_flag_off_is_503_and_no_store_call(monkeypatch):
    monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT", "false")
    store = _ReceiptStore([_row()])
    _wire(monkeypatch)
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_picker([]))
    assert status == 503 and body["reason"] == "receipts_disabled"
    assert store.calls == [] and store.receipts == {}


# ---- fresh action ------------------------------------------------------------

def test_begin_runs_before_any_row_read(monkeypatch):
    store = _ReceiptStore([_row()])
    _wire(monkeypatch)
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_picker([]))
    assert status == 200
    names = [c[0] for c in store.calls]
    assert names[0] == "begin"
    assert "get_row" not in names[:names.index("begin") + 1]


def test_fresh_action_succeeds_with_one_atomic_apply(monkeypatch):
    store = _ReceiptStore([_row(), _row(row_id=ROW2, fmt="story",
                                        image_url="https://cdn/old-story.jpg"),
                           _row(row_id=ROW3, logical=LOGICAL2),
                           _row(row_id=ROW4, variant_status="candidate")])
    picks = []
    _wire(monkeypatch)
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_picker(picks))
    assert status == 200 and body["ok"] is True and body["action_id"] == "act-1"
    rc = store.receipt("act-1")
    assert rc["status"] == "succeeded"
    assert rc["selected_asset"]["asset_id"] == "asset-1"
    # the picker saw ONLY the OTHER active logical-post members as siblings
    # (the story sibling is in; the same-date unrelated post, the candidate
    # variant AND the clicked primary itself are out -- the primary is rendered
    # once as the primary, never twice as its own sibling), while the frozen
    # manifest below still covers the whole group for apply
    assert picks == [[ROW2]]
    manifest_ids = sorted(m["calendar_row_id"]
                          for m in rc["member_manifest"]["members"])
    assert manifest_ids == sorted([ROW1, ROW2])
    # ONE atomic apply covered exactly the manifest; no per-row writes exist
    assert [c[0] for c in store.calls].count("apply") == 1
    assert sorted(r["calendar_row_id"] for r in store.applies[0]["rows"]) \
        == sorted([ROW1, ROW2])
    # the unrelated same-date row kept its media; the group moved together
    assert store._rows[ROW3]["image_url"] == "https://cdn/old.jpg"
    assert store._rows[ROW1]["image_url"] == "https://cdn/new.jpg"
    assert store._rows[ROW2]["image_url"] == "https://cdn/new.jpg"
    # the caption survived untouched
    assert store._rows[ROW1]["caption"] == "a caption the gym is happy with"
    assert body["caption"] == "a caption the gym is happy with"
    assert "svc-key-secret" not in repr(rc)


def test_exact_terminal_replay_never_reads_the_row(monkeypatch):
    store = _ReceiptStore([_row()])
    picks = []
    _wire(monkeypatch)
    s1, _ = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                 sb_store=store, picker=_picker(picks))
    assert s1 == 200
    # The row is now unreadable AND gone: the replay resolves from the receipt.
    store.get_row_may_fail = True
    store._rows.clear()
    s2, b2 = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                  sb_store=store,
                                  picker=_picker(picks, url="https://cdn/other.jpg",
                                                 asset_id="asset-2"))
    assert (s2, b2["idempotent"]) == (200, True)
    assert b2["image_public_url"] == "https://cdn/new.jpg"  # ORIGINAL asset
    assert len(picks) == 1                                   # picker ran once
    names = [c[0] for c in store.calls]
    assert names.count("apply") == 1 and names.count("claim") == 1


@pytest.mark.parametrize("other_actor,other_row",
                         [("actor-2", ROW1), ("actor-1", ROW2)])
def test_conflicting_binding_replay_is_409(monkeypatch, other_actor, other_row):
    store = _ReceiptStore([_row(), _row(row_id=ROW2, fmt="story")])
    _wire(monkeypatch)
    s1, _ = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                 sb_store=store, picker=_picker([]))
    assert s1 == 200
    status, body = ps.handle_swap_media(GYM, other_row, other_actor,
                                        action_id="act-1", sb_store=store,
                                        picker=_picker([]))
    assert status == 409 and body["reason"] == "action_id_conflict"


def test_historical_null_logical_id_is_manual_review_hold(monkeypatch):
    store = _ReceiptStore([_row(logical=None)])
    _wire(monkeypatch)
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_picker([]))
    assert status == 409 and body["reason"] == "swap_manual_review"
    assert store.receipts == {} and store.applies == []


def test_concurrent_claim_loser_applies_the_winners_frozen_selection(monkeypatch):
    store = _ReceiptStore([_row()])
    ledger = _wire(monkeypatch)
    store.claim_winner = {
        "selected_asset": {"asset_id": "asset-won",
                           "image_url": "https://cdn/won.jpg",
                           "kind": "photo"},
        "planned_siblings": {}}
    status, body = ps.handle_swap_media(
        GYM, ROW1, "actor-1", action_id="act-1", sb_store=store,
        picker=_picker([], url="https://cdn/lost.jpg", asset_id="asset-lost"))
    assert status == 200
    # the row carries the WINNER's media; our candidate was never written
    assert store._rows[ROW1]["image_url"] == "https://cdn/won.jpg"
    assert store.receipt("act-1")["selected_asset"]["asset_id"] == "asset-won"
    # our reservation was exactly released (it can never be written)
    assert ledger["release"] and ledger["release"][0]["source_media_asset_id"] == "asset-lost"


def test_stale_membership_aborts_the_whole_group_with_no_writes(monkeypatch):
    store = _ReceiptStore([_row(), _row(row_id=ROW2, fmt="story",
                                        image_url="https://cdn/old-story.jpg")])
    ledger = _wire(monkeypatch)
    original_claim = store.action_receipt_claim

    def _claim_then_stale(*a, **k):
        won = original_claim(*a, **k)
        store._rows[ROW2]["status"] = "approved"   # sibling moves after claim
        return won

    store.action_receipt_claim = _claim_then_stale
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_picker([]))
    assert status == 409 and body["reason"] == "swap_group_stale"
    # all-or-nothing: NOT ONE row moved
    assert store._rows[ROW1]["image_url"] == "https://cdn/old.jpg"
    assert store._rows[ROW2]["image_url"] == "https://cdn/old-story.jpg"
    # the apply failure was definite (SQLSTATE raise = rolled back), so our
    # reservation was released exactly
    assert ledger["release"]


def test_lost_apply_response_replays_the_same_apply(monkeypatch):
    store = _ReceiptStore([_row()])
    picks = []
    _wire(monkeypatch)
    store.apply_failures = [RuntimeError("timeout: response lost")]
    s1, b1 = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                  sb_store=store, picker=_picker(picks))
    assert (s1, b1["reason"]) == (503, "swap_outcome_unknown")
    assert store.receipt("act-1")["status"] == "selected"
    s2, b2 = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                  sb_store=store,
                                  picker=_picker(picks, url="https://cdn/x.jpg",
                                                 asset_id="asset-2"))
    assert (s2, b2["ok"]) == (200, True)
    assert len(picks) == 1                       # never a repick on replay
    assert len(store.applies) == 2
    assert store.applies[0] == store.applies[1]  # the SAME apply, replayed
    assert store._rows[ROW1]["image_url"] == "https://cdn/new.jpg"


def test_uncertain_apply_never_releases_the_reservation(monkeypatch):
    store = _ReceiptStore([_row()])
    ledger = _wire(monkeypatch)
    store.apply_failures = [RuntimeError("timeout")]
    s1, _ = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                 sb_store=store, picker=_picker([]))
    assert s1 == 503
    assert ledger["reserve"] and not ledger["release"]


def test_receipt_store_down_refuses_closed(monkeypatch):
    store = _ReceiptStore([_row()])
    _wire(monkeypatch)

    def _down(*a, **k):
        raise pcs.ReceiptStoreError(0, "transport")

    store.action_receipt_begin = _down
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_picker([]))
    assert status == 503 and body["reason"] == "receipt_store_unavailable"
    assert store.applies == []


def test_published_gate_survives_on_the_receipt_path(monkeypatch):
    store = _ReceiptStore([_row(status="published")])
    _wire(monkeypatch)
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_picker([]))
    assert status == 409 and store.applies == []
    assert store._rows[ROW1]["image_url"] == "https://cdn/old.jpg"


def test_tenant_isolation_of_receipts(monkeypatch):
    # both tenants under test must be on the receipt allowlist
    monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT_GYMS", "zanshin, othergym")
    other = _row(row_id=ROW3, gym_id="othergym", logical=LOGICAL2)
    store = _ReceiptStore([_row(), other])
    _wire(monkeypatch)
    s1, _ = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                 sb_store=store, picker=_picker([]))
    s2, _ = ps.handle_swap_media("othergym", ROW3, "actor-2", action_id="act-1",
                                 sb_store=store, picker=_picker([]))
    assert (s1, s2) == (200, 200)
    assert (GYM, "act-1") in store.receipts
    assert ("othergym", "act-1") in store.receipts
    assert store.receipt("act-1", gym="othergym")["row_id"] == ROW3


# ---- legacy no-ID parity ------------------------------------------------------

class _LegacyStore:
    """The minimal legacy surface: calendar rows only, no receipt methods. Any
    receipt access is a hard failure -- the no-ID path must never touch it."""

    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.swaps = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        return dict(r) if r and str(r.get("gym_id")) == str(account_key) else None

    def list_month(self, account_key, month):
        return [dict(r) for r in self._rows.values()]

    def swap_media(self, account_key, row_id, image_url, source_media_url=None,
                   extra_fields=None, **kw):
        self.swaps.append((row_id, image_url))
        r = self._rows.get(row_id)
        if not r or str(r.get("gym_id")) != str(account_key):
            return None
        if r.get("status") not in ("pending", "coach_review"):
            return None
        r["image_url"] = image_url
        return dict(r)


def test_no_action_id_keeps_legacy_behavior(monkeypatch):
    store = _LegacyStore([_row()])
    monkeypatch.setattr(msw, "after_swap", lambda *a, **k: None)
    monkeypatch.setattr(msw, "reserve_local_pick", lambda *a, **k: True)
    monkeypatch.setattr(msw, "release_local_pick", lambda *a, **k: None)
    monkeypatch.setattr(msw, "sibling_rows", lambda *a, **k: [])
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", sb_store=store,
                                        picker=_picker([]))
    assert status == 200 and body["ok"] is True
    assert "action_id" not in body and "idempotent" not in body
    assert store.swaps == [(ROW1, "https://cdn/new.jpg")]


def test_no_action_id_with_flag_off_still_uses_legacy_path(monkeypatch):
    monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT", "false")
    store = _LegacyStore([_row()])
    monkeypatch.setattr(msw, "after_swap", lambda *a, **k: None)
    monkeypatch.setattr(msw, "reserve_local_pick", lambda *a, **k: True)
    monkeypatch.setattr(msw, "release_local_pick", lambda *a, **k: None)
    monkeypatch.setattr(msw, "sibling_rows", lambda *a, **k: [])
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", sb_store=store,
                                        picker=_picker([]))
    assert status == 200 and store.swaps == [(ROW1, "https://cdn/new.jpg")]


# ---- repair wave 2026-10-04 (Sol review gaps 1-3, 6) -------------------------

@pytest.mark.parametrize("bad", [" act-1", "act-1 ", "\tact-1", "act-1\n"])
def test_action_id_with_surrounding_whitespace_is_rejected_never_stripped(monkeypatch, bad):
    """Gap 6: whitespace around an action_id is REJECTED as-sent; stripping
    would fingerprint a request tuple the client never sent."""
    store = _ReceiptStore([_row()])
    _wire(monkeypatch)
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id=bad,
                                        sb_store=store, picker=_picker([]))
    assert status == 400 and body["reason"] == "invalid_action_id"
    assert store.calls == [] and store.receipts == {}
    # and the unstripped value never reached a fingerprint
    fp = ps._receipt_fingerprint(GYM, ROW1, "actor-1", bad)
    assert fp != ps._receipt_fingerprint(GYM, ROW1, "actor-1", bad.strip())


def test_local_library_pick_gets_durable_provenance(monkeypatch):
    """Gap 1: a legitimate tenant-owned local pick (media_swap._finish leaves
    source_media_asset_id empty) claims with local:<library key> provenance
    and succeeds; the frozen identity and every applied row carry it."""
    store = _ReceiptStore([_row(), _row(row_id=ROW2, fmt="story",
                                        image_url="https://cdn/old-story.jpg")])
    _wire(monkeypatch)

    def _local_pick(_gym, _row, siblings=(), **_kw):
        base = {"ok": True, "image_url": "https://cdn/echo/zanshin_ig/h/new.jpg",
                "source_media_url": None, "key": "fresh.jpg", "kind": "photo",
                "source": "local", "thumbnail_url": "",
                "source_media_asset_id": "", "path": "/lib/fresh.jpg"}
        return dict(base, siblings={str(s["id"]): dict(base) for s in siblings})

    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_local_pick)
    assert status == 200 and body["ok"] is True
    rc = store.receipt("act-1")
    assert rc["selected_asset"]["asset_id"] == "local:fresh.jpg"
    assert store._rows[ROW1]["source_media_asset_id"] == "local:fresh.jpg"
    assert store._rows[ROW2]["source_media_asset_id"] == "local:fresh.jpg"


def test_local_pick_without_a_safe_key_is_unproven_media_hold(monkeypatch):
    """Gap 1 (fail closed): no Drive asset id AND no safe local key means no
    trustworthy source identity -- hold; nothing is claimed or written."""
    store = _ReceiptStore([_row()])
    ledger = _wire(monkeypatch)

    def _shady_pick(_gym, _row, siblings=(), **_kw):
        return {"ok": True, "image_url": "https://cdn/echo/zanshin_ig/h/x.jpg",
                "source_media_url": None, "key": "../escape.jpg",
                "kind": "photo", "source": "local", "thumbnail_url": "",
                "source_media_asset_id": "", "path": "/lib/x.jpg",
                "siblings": {}}

    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_shady_pick)
    assert status == 409 and body["reason"] == "media_evidence_unavailable"
    assert store.applies == [] and not ledger["reserve"]
    assert store._rows[ROW1]["image_url"] == "https://cdn/old.jpg"


def test_same_url_different_asset_is_a_losing_reservation(monkeypatch):
    """Gap 3: the claim loser compares the ENTIRE frozen identity -- an equal
    hosted URL under a DIFFERENT asset id is still a loser: released before
    apply, never settled."""
    store = _ReceiptStore([_row()])
    ledger = _wire(monkeypatch)
    store.claim_winner = {
        "selected_asset": {"asset_id": "asset-won",
                           "image_url": "https://cdn/same.jpg",
                           "kind": "photo"},
        "planned_siblings": {}}
    status, body = ps.handle_swap_media(
        GYM, ROW1, "actor-1", action_id="act-1", sb_store=store,
        picker=_picker([], url="https://cdn/same.jpg", asset_id="asset-lost"))
    assert status == 200
    assert store._rows[ROW1]["source_media_asset_id"] == "asset-won"
    assert ledger["release"] and \
        ledger["release"][0]["source_media_asset_id"] == "asset-lost"
    # settlement was bound to the FROZEN winner, never to our lost reservation
    assert not any(getattr(p, "get", lambda *a: None)("source_media_asset_id")
                   == "asset-lost" for p in ledger["settle"]
                   if isinstance(p, tuple))


def test_terminal_replay_settles_the_frozen_local_asset(monkeypatch, tmp_path):
    """Gap 2: a success response lost AFTER apply committed settles on replay:
    the frozen local: asset is re-served through the once-only ledger with the
    library path reconstructed from the provenance key."""
    store = _ReceiptStore([_row()])
    ledger = _wire(monkeypatch)
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(msw, "library_path_for", lambda _base: str(lib))

    def _local_pick(_gym, _row, siblings=(), **_kw):
        return {"ok": True, "image_url": "https://cdn/echo/zanshin_ig/h/new.jpg",
                "source_media_url": None, "key": "fresh.jpg", "kind": "photo",
                "source": "local", "thumbnail_url": "",
                "source_media_asset_id": "", "path": str(lib / "fresh.jpg"),
                "siblings": {}}

    s1, _ = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                 sb_store=store, picker=_local_pick)
    assert s1 == 200
    assert len(ledger["settle"]) == 1   # fresh success settled via own_pick
    # replay: no repick, no reservation of our own -- the settlement pick is
    # rebuilt from the FROZEN receipt (local provenance -> library path)
    s2, b2 = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                  sb_store=store, picker=_picker([]))
    assert (s2, b2["idempotent"]) == (200, True)
    assert len(ledger["settle"]) == 2
    before, pick = ledger["settle"][1][1], ledger["settle"][1][2]
    assert pick["source"] == "local"
    assert pick["source_media_asset_id"] == "local:fresh.jpg"
    assert pick["path"] == str(lib / "fresh.jpg")
    assert before["image_url"] == "https://cdn/old.jpg"


def test_terminal_replay_settles_drive_via_deterministic_claim_key(monkeypatch):
    """Gap 2: a Drive winner's reservation is settled on replay by re-deriving
    the byte-bound claim key from the frozen asset -- idempotent claim_done,
    and NEVER a second usage stamp (_drive_stamped: reserve stamped pre-claim
    and stamp_use is not idempotent)."""
    store = _ReceiptStore([_row()])
    ledger = _wire(monkeypatch)
    asset_row = {"id": "asset-1", "gym_id": GYM, "content_hash": "a" * 32}

    from agent import gym_media_index as gmi, gym_media_selector as gms
    monkeypatch.setattr(gmi, "default_store",
                        lambda: type("S", (), {"get_asset": lambda self, i: asset_row})())
    seen_claims = []
    monkeypatch.setattr(gms, "drive_content_claim_id",
                        lambda base, asset: seen_claims.append((base, asset["id"]))
                        or f"gbp_media:{base}:hash:{asset['content_hash']}")

    s1, _ = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                 sb_store=store, picker=_picker([]))
    assert s1 == 200
    s2, b2 = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                  sb_store=store, picker=_picker([]))
    assert (s2, b2["idempotent"]) == (200, True)
    assert len(ledger["settle"]) == 2
    # first settle: our own reservation (no synthetic claim key needed)
    assert ledger["settle"][0][2]["source_media_asset_id"] == "asset-1"
    # replay settle: rebuilt from the frozen receipt with the deterministic
    # claim key, marked already-stamped so stamp_use never re-fires
    pick = ledger["settle"][1][2]
    assert pick["_drive_claim_id"] == f"gbp_media:{GYM}:hash:" + "a" * 32
    assert pick["_drive_claim_account"] == f"{GYM}_gbp"
    assert pick["_drive_stamped"] is True
    assert seen_claims == [(GYM, "asset-1")]


# ---- tenant allowlist (ECHO_SWAP_ACTION_RECEIPT_GYMS) ------------------------
# Comma-separated exact account keys; default EMPTY means nobody -- an
# explicit action_id from a gym outside the list is a fail-closed 503 BEFORE
# any store call, never a silent fallback to the legacy non-idempotent path.

def test_allowlisted_gym_runs_the_receipt_path(monkeypatch):
    # exact match, whitespace around config entries trimmed
    monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT_GYMS", " other-gym , zanshin ,")
    store = _ReceiptStore([_row()])
    _wire(monkeypatch)
    status, body = ps.handle_swap_media("zanshin", ROW1, "actor-1",
                                        action_id="act-1", sb_store=store,
                                        picker=_picker([]))
    assert status == 200 and body["ok"] is True
    assert [c[0] for c in store.calls][0] == "begin"


def test_noncanonical_request_gym_cannot_alias_allowlisted_tenant(monkeypatch):
    monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT_GYMS", "zanshin")
    store = _ReceiptStore([_row()])
    _wire(monkeypatch)
    for gym in ("ZANSHIN", " zanshin "):
        status, body = ps.handle_swap_media(gym, ROW1, "actor-1",
                                            action_id="act-1", sb_store=store,
                                            picker=_picker([]))
        assert status == 503 and body["reason"] == "receipt_gym_not_allowed"
    assert store.calls == [] and store.receipts == {}


def test_gym_outside_allowlist_is_503_before_any_store_call(monkeypatch):
    monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT_GYMS", "other-gym")
    store = _ReceiptStore([_row()])
    _wire(monkeypatch)
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", action_id="act-1",
                                        sb_store=store, picker=_picker([]))
    assert status == 503 and body["ok"] is False
    assert body["reason"] == "receipt_gym_not_allowed"
    assert body["account_key"] == GYM
    assert store.calls == [] and store.receipts == {}


def test_empty_allowlist_enables_no_gym(monkeypatch):
    store = _ReceiptStore([_row()])
    _wire(monkeypatch)
    for raw in ("", "   ", " , ,"):
        monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT_GYMS", raw)
        status, body = ps.handle_swap_media(GYM, ROW1, "actor-1",
                                            action_id="act-1", sb_store=store,
                                            picker=_picker([]))
        assert status == 503 and body["reason"] == "receipt_gym_not_allowed"
    assert store.calls == [] and store.receipts == {}


def test_no_action_id_keeps_legacy_path_regardless_of_allowlist(monkeypatch):
    monkeypatch.setenv("ECHO_SWAP_ACTION_RECEIPT_GYMS", "other-gym")
    store = _LegacyStore([_row()])
    monkeypatch.setattr(msw, "after_swap", lambda *a, **k: None)
    monkeypatch.setattr(msw, "reserve_local_pick", lambda *a, **k: True)
    monkeypatch.setattr(msw, "release_local_pick", lambda *a, **k: None)
    monkeypatch.setattr(msw, "sibling_rows", lambda *a, **k: [])
    status, body = ps.handle_swap_media(GYM, ROW1, "actor-1", sb_store=store,
                                        picker=_picker([]))
    assert status == 200 and body["ok"] is True
    # the legacy per-row write ran; the allowlist gate never engaged
    assert store.swaps == [(ROW1, "https://cdn/new.jpg")]
