"""
DRAFT approval provenance (2026-10-05, repaired): durable exact-content
human-approval proof for client Manual-mode calendar publishes.

Scope of these tests:
  * the AGENT_APPROVAL_PROOF flag defaults OFF (zero live behavior change);
  * the Supabase store stamps/sends provenance parameters correctly
    (approve_ready actor only when a TRUSTED one exists, claim require_proof)
    and keeps the legacy wire shape when the feature is not in play;
  * the autopublish lane, when armed, sends require_proof on EVERY claim and
    fails closed on stores that cannot carry it; the flag-off lane is
    byte-for-byte legacy;
  * an offline model of the SQL gate shows: a stale digest (any edit of a
    publish-relevant field after approval), missing human provenance, an
    Echo-token-only approval (kind NULL), a spoofed/empty actor, or an
    Auto->Manual flip at claim time holds the row instead of publishing it;
  * the digest binds the FINAL image_url: ANY post-approval media change
    (auto-fit reframe, story reburn, swap) invalidates the proof and the row
    fails closed into fresh review (repair-pass-2 defect 2);
  * the publisher-stamped scheduled_at does NOT invalidate the proof
    (display metadata stamped after approval).

No database, no network: the SQL gate itself lives in
migrations/calendar_approval_provenance_20261005.sql and is exercised against a
disposable Postgres by tests/sql/approval_provenance_check.sh.
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_autopublish as cap
from agent import config
from agent import portal_calendar_store as pcs
from agent.meta_publisher import PublishResult

RUN_DATE = "2026-08-10"
LATE_NOW = "2026-08-10T23:59:00-04:00"

# Swift River's content-plane base key (account_key_split_watch) — the Manual
# client gym used for the race/regression tests.
SWIFT = "swiftrivercrossfitd23567"


# ---- Python mirror of the SQL canonical digest contract --------------------
# Must match calendar_approval_digest() in the migration: lower/btrim account,
# lower/btrim format (default 'feed'), ISO post_date, raw caption, the FINAL
# image_url, then byte_hash, source_media_asset_id, source_media_url.
# scheduled_at is deliberately NOT bound. \x1f-joined.

def canonical_digest(row):
    parts = [
        (str(row.get("account") or "").strip().lower()),
        (str(row.get("format") or "").strip().lower()) or "feed",
        str(row.get("post_date") or ""),
        str(row.get("caption") or ""),
        str(row.get("image_url") or "").strip(),
        str(row.get("byte_hash") or "").strip(),
        str(row.get("source_media_asset_id") or "").strip(),
        str(row.get("source_media_url") or "").strip(),
    ]
    return hashlib.md5("\x1f".join(parts).encode()).hexdigest()


def echo_approve(row):
    """What Echo's approve RPC stamps atomically (repair pass 2): the status
    flip + the digest of the exact content served, with provenance UNPROVED.
    An Echo bearer token is not a verified human identity."""
    row["status"] = "approved"
    row["approval_kind"] = None
    row["approved_by"] = None
    row["approved_at"] = None
    row["approval_digest"] = canonical_digest(row)
    return row


def stamp_human(row, actor="clerk-user-1"):
    """The portal's calendar_stamp_verified_approval: the ONLY mint of a
    human proof, gated on a nonempty authenticated Clerk actor."""
    assert str(actor or "").strip(), "no trusted actor, no human proof"
    row["approval_kind"] = "human"
    row["approved_by"] = actor
    row["approved_at"] = "2026-08-09T12:30:00+00:00"
    return row


def human_prove(row, actor="clerk-user-1"):
    """Full proved lane: Echo approve + portal verified stamp."""
    return stamp_human(echo_approve(row), actor)


def _row(row_id, account="instagram", fmt="feed", post_date=RUN_DATE,
         status="approved", caption=None, image_url="https://cdn/x.jpg",
         gym_id="lasso", source_media_asset_id=None, source_media_url=None,
         byte_hash=None):
    caption = caption if caption is not None else f"caption for {row_id}"
    return {
        "id": row_id, "gym_id": gym_id, "post_date": post_date,
        "account": account, "format": fmt, "status": status,
        "caption": caption, "image_url": image_url, "pillar": "education",
        "published_at": None, "late_post_id": None,
        "source_media_url": source_media_url,
        "source_media_asset_id": source_media_asset_id,
        "byte_hash": byte_hash,
        "media_not_ready_reason": None,
        "approval_kind": None, "approved_by": None,
        "approved_at": None, "approval_digest": None,
    }


# ---- fakes -----------------------------------------------------------------

class _ProofStore:
    """Offline model of content_calendar plus the SQL proof-gated claim.

    claim_publish_slot mirrors claim_calendar_publish_slot_owned: with
    require_proof=True the RPC first re-reads the gym's CURRENT autonomy from
    the DB (here, self.autonomy[gym_id]; True = definitively autonomous,
    anything else = Manual/ambiguous -> fail closed). A gym not definitively
    autonomous needs status='approved' plus a human kind, approved_at and a
    digest matching the row's CURRENT fields (any post-approval edit
    invalidates it). A definitively autonomous gym keeps legacy behavior.
    """

    def __init__(self, rows, autonomy=None, on_due_rows=None, before_claim=None):
        self.rows = {r["id"]: dict(r) for r in rows}
        # autonomy map = the authoritative DB state at CLAIM time.
        self.autonomy = dict(autonomy or {})
        self.on_due_rows = on_due_rows  # optional race injector
        self.before_claim = before_claim
        self.published_calls = []
        self.claim_calls = []

    def due_rows(self, gym_id, run_date):
        if self.on_due_rows:
            self.on_due_rows(self)
        return [dict(r) for r in self.rows.values()
                if r.get("gym_id") == gym_id
                and r.get("post_date") == run_date
                and r.get("status") not in ("published", "denied", "killed")
                and not r.get("published_at") and r.get("image_url")]

    def claim_publish_slot(self, row_id, gym_id, day, timezone_name, capacity,
                           approved_only, require_proof=False):
        self.claim_calls.append((row_id, require_proof))
        if self.before_claim:
            self.before_claim(self, row_id)
        row = self.rows[row_id]
        if row.get("status") not in ("pending", "approved"):
            return None
        # ATOMIC MODE CHECK: current DB autonomy, not the caller's snapshot.
        enforce = bool(require_proof) and self.autonomy.get(gym_id) is not True
        if approved_only and row["status"] != "approved":
            return None
        if enforce:
            if row["status"] != "approved":
                return None  # flipped to Manual: pending rows hold
            if (row.get("approval_kind") != "human"
                    or not str(row.get("approved_by") or "").strip()
                    or not row.get("approved_at")
                    or not row.get("approval_digest")
                    or row["approval_digest"] != canonical_digest(row)):
                return None  # held: unproved, actorless, or stale proof
        row["status"] = "publishing"
        row["publish_claim_token"] = "claim-token-1"
        if require_proof:
            return dict(row, autonomous_at_claim=self.autonomy.get(gym_id) is True)
        return "claim-token-1"

    def mark_publishing(self, row_id):
        row = self.rows.get(row_id)
        if not row or row.get("status") not in ("pending", "approved"):
            return False
        row["status"] = "publishing"
        return True

    def patch_caption_autonomous_clean(self, gym_id, row_id, expected_status,
                                       expected_caption, clean_caption):
        row = self.rows[row_id]
        if (self.autonomy.get(gym_id) is not True
                or row["status"] != expected_status
                or row["caption"] != expected_caption):
            return None
        row.update(caption=clean_caption, approval_kind=None, approved_by=None,
                   approved_at=None, approval_digest=None)
        return dict(row)

    def mark_published(self, row_id, media_id, published_at=None, **kw):
        row = self.rows[row_id]
        if row.get("status") != "publishing":
            return None
        self.published_calls.append(row_id)
        row["status"] = "published"
        row["published_at"] = published_at
        return row

    def mark_publish_failed(self, row_id, revert_status="pending", **kw):
        row = self.rows.get(row_id)
        if row:
            row["status"] = revert_status
        return row

    def release_content_ledger_claim(self, *a, **kw):
        return None


class _LegacyStore(_ProofStore):
    """A store with NO proof-capable claim (pre-migration shape)."""
    claim_publish_slot = None


class _FakePublisher:
    def __init__(self):
        self.calls = []

    def __call__(self, draft, account, **kw):
        self.calls.append(draft.draft_id)
        return PublishResult(ok=True, mode="published", media_id="MEDIA_1")


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    # The billing gate fail-closes on gyms outside the account registry (Swift
    # test base included); the proof tests are not about billing.
    from agent import publish_billing_gate
    monkeypatch.setattr(publish_billing_gate, "publishing_blocked",
                        lambda *a, **k: False)
    # Account-registry routing (Swift's test base has no registry entry) is
    # outside the proof tests' scope; route every row to a stub account.
    from types import SimpleNamespace
    monkeypatch.setattr(cap, "_account_for",
                        lambda row, gym_id: SimpleNamespace(key=f"{gym_id}_ig", platform="instagram"))


@pytest.fixture
def proof_armed(armed, monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")


# ---- flag ------------------------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    assert config.approval_proof_enabled() is False


def test_flag_armed(monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PROOF", "true")
    assert config.approval_proof_enabled() is True


# ---- store wire shape --------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = ""

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, post_resp):
        self.calls = []
        self._post_resp = post_resp

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append((url, json))
        return self._post_resp

    def get(self, *a, **kw):
        raise AssertionError("unexpected read")

    def patch(self, *a, **kw):
        raise AssertionError("unexpected patch")


def test_approve_ready_omits_actor_when_none(monkeypatch):
    http = _FakeHTTP(_Resp(200, [{"id": "r1", "gym_id": "gymx"}]))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().approve_ready("gymx", "r1")
    url, payload = http.calls[0]
    assert url.endswith("/rpc/approve_calendar_row_if_media_ready")
    assert payload == {"p_row_id": "r1", "p_gym_id": "gymx"}


def test_approve_ready_has_no_actor_param(monkeypatch):
    """Defect 1 repair: the Echo approve RPC mints NO human proof and takes
    no actor at all -- nothing a caller passes can become approved_by."""
    import inspect
    assert "actor" not in inspect.signature(
        pcs.SupabaseCalendarStore.approve_ready).parameters
    http = _FakeHTTP(_Resp(200, [{"id": "r1", "gym_id": "gymx"}]))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    with pytest.raises(TypeError):
        pcs.SupabaseCalendarStore().approve_ready("gymx", "r1",
                                                  actor="clerk-user-9")


def _approve_row_fixture(status="pending", digest="digest-abc"):
    row = {"id": "r1", "gym_id": "gymx", "status": status,
           "image_url": "https://cdn/x.jpg", "media_not_ready_reason": None}
    if status == "pending":
        row["approval_digest"] = None
    return row


def test_portal_approve_response_contract_new_approval(monkeypatch):
    """Portal contract: a NEW approval returns approval_state='approved',
    idempotent=False and the digest the portal must present to
    calendar_stamp_verified_approval. The spoofable browser-body actor_id is
    never forwarded (approve_ready takes no actor)."""
    import agent.portal_social as ps

    calls = []

    class _SB:
        def approve_ready(self, account_key, draft_id):
            calls.append((account_key, draft_id))
            return {"id": draft_id, "gym_id": account_key,
                    "status": "approved", "approval_digest": "digest-abc"}

    row = _approve_row_fixture()
    monkeypatch.setattr(ps, "_action_gates", lambda *a, **k: None)
    monkeypatch.setattr(ps, "_sb_load_owned_row", lambda *a, **k: (row, None))
    status, resp = ps._handle_approve_supabase(
        "gymx", "r1", "spoofed-browser-actor", None, _SB())
    assert status == 200
    assert calls == [("gymx", "r1")]
    assert resp["approval_state"] == "approved"
    assert resp["idempotent"] is False
    assert resp["approval_digest"] == "digest-abc"


def test_portal_approve_response_contract_already_approved(monkeypatch):
    """A replayed approve is explicitly idempotent and must NOT mint a new
    stamp (the portal returns 409 review_refresh_required on this)."""
    import agent.portal_social as ps

    class _SB:
        def approve_ready(self, *a):  # must never be reached
            raise AssertionError("no new stamp on an idempotent replay")

    row = _approve_row_fixture(status="approved")
    monkeypatch.setattr(ps, "_action_gates", lambda *a, **k: None)
    monkeypatch.setattr(ps, "_sb_load_owned_row", lambda *a, **k: (row, None))
    status, resp = ps._handle_approve_supabase(
        "gymx", "r1", "actor", None, _SB())
    assert status == 200
    assert resp["approval_state"] == "already_approved"
    assert resp["idempotent"] is True
    assert "approval_digest" not in resp


def test_portal_approve_response_contract_published(monkeypatch):
    """A published row keeps the legacy no-op response, distinguishable as
    approval_state='published'; never a new stamp."""
    import agent.portal_social as ps

    class _SB:
        def approve_ready(self, *a):
            raise AssertionError("no stamp for a published row")

    row = _approve_row_fixture(status="published")
    monkeypatch.setattr(ps, "_action_gates", lambda *a, **k: None)
    monkeypatch.setattr(ps, "_sb_load_owned_row", lambda *a, **k: (row, None))
    status, resp = ps._handle_approve_supabase(
        "gymx", "r1", "actor", None, _SB())
    assert status == 200
    assert resp["approval_state"] == "published"
    assert resp["idempotent"] is True


def test_claim_slot_omits_proof_param_by_default(monkeypatch):
    http = _FakeHTTP(_Resp(200, "00000000-0000-0000-0000-000000000001"))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().claim_publish_slot(
        "r1", "gymx", RUN_DATE, "America/New_York", 2, True)
    url, payload = http.calls[0]
    assert url.endswith("/rpc/claim_calendar_publish_slot_owned")
    assert "p_require_approval_proof" not in payload


def test_claim_slot_sends_proof_param_only_when_required(monkeypatch):
    row = _row("r1", gym_id="gymx")
    row.update(status="publishing", publish_claim_token="00000000-0000-0000-0000-000000000001")
    http = _FakeHTTP(_Resp(200, {"row": row, "autonomous_at_claim": False}))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    claimed = pcs.SupabaseCalendarStore().claim_publish_slot(
        "r1", "gymx", RUN_DATE, "America/New_York", 2, True, require_proof=True)
    url, payload = http.calls[0]
    assert url.endswith("/rpc/claim_calendar_publish_slot_proven_owned")
    assert "p_require_approval_proof" not in payload
    assert claimed["caption"] == row["caption"]
    assert claimed["autonomous_at_claim"] is False


# ---- Manual lane, armed -----------------------------------------------------

def test_armed_manual_lane_publishes_only_fresh_human_proved_rows(proof_armed):
    fresh = human_prove(_row("fresh"))
    stale = human_prove(_row("stale"))
    stale["caption"] = "edited AFTER the approval tap"  # digest now stale
    automatic_era = _row("auto-era")  # approved, no provenance at all
    # DEFECT 1: an Echo bearer-token approval ALONE (status + digest, kind
    # NULL) is NOT a human proof, even with a perfectly matching digest.
    echo_only = echo_approve(_row("echo-only",
                                  caption="approved via Echo token only"))
    # DEFECT 1: a 'human' kind with an EMPTY/spoofed actor is not proof --
    # the claim gate requires a nonempty trusted approved_by.
    spoofed = human_prove(_row("spoofed-actor",
                               caption="human kind with spoofed actor"))
    spoofed["approved_by"] = "   "
    store = _ProofStore([fresh, stale, automatic_era, echo_only, spoofed],
                        autonomy={"lasso": False})
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW, approved_only=True)

    assert summary["published"] == ["fresh"]
    assert store.published_calls == ["fresh"]
    # every approved row reached the atomic claim; the gate held the rest
    assert {rid for rid, rp in store.claim_calls} == {
        "fresh", "stale", "auto-era", "echo-only", "spoofed-actor"}
    assert all(rp is True for _, rp in store.claim_calls)
    # held rows keep their approval for the portal; nothing was reverted/failed
    assert store.rows["stale"]["status"] == "approved"
    assert store.rows["auto-era"]["status"] == "approved"
    assert store.rows["echo-only"]["status"] == "approved"
    assert store.rows["spoofed-actor"]["status"] == "approved"


def test_swift_manual_requires_fresh_proof(proof_armed):
    """Swift River (Manual): an approved row with no provenance NEVER
    publishes; the same row with a fresh human proof of its exact final
    media/caption publishes."""
    unproved = _row("swift-unproved", gym_id=SWIFT)
    proved = human_prove(_row("swift-proved", gym_id=SWIFT))
    store = _ProofStore([unproved, proved], autonomy={SWIFT: False})
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id=SWIFT, store=store,
                              publisher=pub, zernio_publish=pub, now=LATE_NOW,
                              approved_only=True)

    assert summary["published"] == ["swift-proved"]
    assert store.rows["swift-unproved"]["status"] == "approved"


def test_auto_to_manual_flip_caught_atomically_at_claim(proof_armed):
    """Race (defect 4): the worker's snapshot said Auto (approved_only=False),
    but the gym flipped to Manual before the claim. The claim re-reads the
    CURRENT DB state and holds every row — no approval, no publish."""
    def flip_to_manual(store):
        store.autonomy[SWIFT] = False  # portal toggle landed after the snapshot

    rows = [_row("swift-pending", gym_id=SWIFT, status="pending"),
            _row("swift-approved-auto-era", gym_id=SWIFT)]  # approved, no proof
    store = _ProofStore(rows, autonomy={SWIFT: True}, on_due_rows=flip_to_manual)
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id=SWIFT, store=store,
                              publisher=pub, zernio_publish=pub, now=LATE_NOW,
                              approved_only=False)

    assert summary["published"] == []
    assert pub.calls == []
    assert store.rows["swift-pending"]["status"] == "pending"
    assert store.rows["swift-approved-auto-era"]["status"] == "approved"
    assert all(rp is True for _, rp in store.claim_calls)


def test_armed_autonomous_gym_unchanged_when_db_says_auto(proof_armed):
    """The autonomous lane is byte-for-byte unchanged while the DB still says
    the gym is autonomous: pending rows publish with NO proof gate."""
    store = _ProofStore([_row("lasso-auto", status="pending")],
                        autonomy={"lasso": True})
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW, approved_only=False)

    assert summary["published"] == ["lasso-auto"]
    assert store.claim_calls == [("lasso-auto", True)]  # armed, DB said Auto


def test_gated_publish_uses_locked_creative_after_prefetch_race(proof_armed):
    def edit_before_claim(store, row_id):
        store.rows[row_id]["caption"] = "caption committed during preflight"
        store.rows[row_id]["image_url"] = "https://cdn/committed.jpg"

    store = _ProofStore([_row("raced", status="pending")],
                        autonomy={"lasso": True}, before_claim=edit_before_claim)
    sent = []

    def publish(draft, account):
        sent.append((draft.caption, draft.creative_public_url))
        return PublishResult(ok=True, mode="published", media_id="MEDIA_1")

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=publish, now=LATE_NOW,
                              approved_only=False)
    assert summary["published"] == ["raced"]
    assert sent == [("caption committed during preflight",
                     "https://cdn/committed.jpg")]


def test_gated_publish_rejects_token_without_locked_creative(proof_armed):
    class TokenOnlyStore(_ProofStore):
        def claim_publish_slot(self, *args, **kwargs):
            super().claim_publish_slot(*args, **kwargs)
            return "claim-token-1"

    store = TokenOnlyStore([_row("token-only", status="pending")],
                           autonomy={"lasso": True})
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW,
                              approved_only=False)
    assert summary["published"] == []
    assert summary["failed"] == ["token-only"]
    assert pub.calls == []


def test_gated_autonomous_meta_cleanup_persists_then_sends_clean_body(proof_armed):
    raw = _row("auto-meta", status="pending",
               caption="Ready to train.\n[why] internal rationale")
    store = _ProofStore([raw], autonomy={"lasso": True})
    sent = []

    def publish(draft, account):
        sent.append(draft.caption)
        return PublishResult(ok=True, mode="published", media_id="MEDIA_1")

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=publish, now=LATE_NOW,
                              approved_only=False)
    assert summary["published"] == ["auto-meta"]
    assert sent == ["Ready to train."]
    assert store.rows["auto-meta"]["caption"] == "Ready to train."


def test_gated_auto_to_manual_before_meta_cleanup_holds(proof_armed):
    row = _row("flipped-meta", status="approved",
               caption="Ready to train.\n[why] internal rationale")
    store = _ProofStore([row], autonomy={"lasso": False})
    pub = _FakePublisher()
    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW,
                              approved_only=False)
    assert summary["published"] == []
    assert pub.calls == []
    assert store.rows["flipped-meta"]["caption"] == row["caption"]


def test_armed_autonomy_unresolvable_fails_closed(proof_armed):
    """Resolver ambiguity (no/duplicate gyms or settings row) is NOT
    autonomous: the proof gate applies even on the 'autonomous' lane."""
    store = _ProofStore([_row("mystery", status="pending")],
                        autonomy={})  # DB lookup unresolved
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW, approved_only=False)

    assert summary["published"] == []
    assert pub.calls == []
    assert store.rows["mystery"]["status"] == "pending"


def test_unarmed_manual_lane_keeps_legacy_behavior(armed, monkeypatch):
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    legacy = _row("legacy")  # approved, zero provenance
    store = _ProofStore([legacy])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW, approved_only=True)

    assert summary["published"] == ["legacy"]
    # the claim was made WITHOUT the proof requirement: live behavior unchanged
    assert store.claim_calls == [("legacy", False)]


def test_unarmed_autonomous_lane_keeps_legacy_behavior(armed, monkeypatch):
    monkeypatch.delenv("AGENT_APPROVAL_PROOF", raising=False)
    store = _ProofStore([_row("auto", status="pending")])
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW, approved_only=False)

    assert summary["published"] == ["auto"]
    assert store.claim_calls == [("auto", False)]


def test_armed_lane_fails_closed_on_store_without_proof_claim(proof_armed):
    store = _LegacyStore([human_prove(_row("fresh"))], autonomy={"lasso": False})
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW, approved_only=True)

    # No proof-capable atomic claim: hold, never fall back to mark_publishing.
    assert summary["published"] == []
    assert pub.calls == []
    assert store.rows["fresh"]["status"] == "approved"


def test_armed_lane_fails_closed_on_legacy_claim_signature(proof_armed):
    class _OldSignatureStore(_ProofStore):
        def claim_publish_slot(self, row_id, gym_id, day, timezone_name, capacity,
                               approved_only):
            raise AssertionError("legacy claim must not be called while proof is armed")

    store = _OldSignatureStore([human_prove(_row("fresh"))],
                               autonomy={"lasso": False})
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW, approved_only=True)

    assert summary["published"] == []
    assert summary["waiting"] == ["fresh"]
    assert summary["failed"] == []
    assert pub.calls == []


def test_armed_claim_internal_typeerror_surfaces_as_failure(
        proof_armed, monkeypatch, capsys):
    class _BrokenProofStore(_ProofStore):
        def claim_publish_slot(self, row_id, gym_id, day, timezone_name, capacity,
                               approved_only, require_proof=False):
            raise TypeError("internal claim defect")

    repeat_failures = []
    monkeypatch.setattr(cap, "_note_repeat_failure",
                        lambda row_id, gym_id, exc:
                        repeat_failures.append((row_id, gym_id, str(exc))))
    store = _BrokenProofStore([human_prove(_row("broken"))],
                              autonomy={"lasso": False})
    pub = _FakePublisher()

    summary = cap.publish_due(RUN_DATE, gym_id="lasso", store=store,
                              publisher=pub, now=LATE_NOW, approved_only=True)

    assert summary["published"] == []
    assert summary["failed"] == ["broken"]
    assert summary["waiting"] == []
    assert pub.calls == []
    assert repeat_failures == [("broken", "lasso", "internal claim defect")]
    assert "TypeError: internal claim defect" in capsys.readouterr().out


# ---- digest contract ---------------------------------------------------------

@pytest.mark.parametrize("field,new_value", [
    ("caption", "edited caption"),
    ("post_date", "2026-08-11"),
    ("account", "facebook"),
    ("format", "story"),
    ("image_url", "https://cdn/changed-final-media.jpg"),
    ("byte_hash", "abc123"),
    ("source_media_asset_id", "drive-file-999"),
    ("source_media_url", "https://drive/new-source.jpg"),
])
def test_any_publish_relevant_edit_invalidates_proof(field, new_value):
    row = human_prove(_row("r"))
    assert row["approval_digest"] == canonical_digest(row)
    row[field] = new_value
    assert canonical_digest(row) != row["approval_digest"]


def test_publisher_scheduled_at_stamp_does_not_invalidate_proof():
    """Defect 2: scheduled_at is stamped by the publisher AFTER approval;
    it is display metadata and must not be bound by the digest."""
    row = human_prove(_row("r"))
    row["scheduled_at"] = "2026-08-10 14:00:00+00"
    assert canonical_digest(row) == row["approval_digest"]


def test_final_media_change_after_approval_invalidates_proof():
    """Defect 2 repair: the digest binds the FINAL image_url. Feed auto-fit
    or a story reburn between approval and claim changes the visible pixels,
    so the proof MUST fail closed into fresh review -- never a silent publish
    of changed media, even when the underlying source asset is unchanged."""
    row = human_prove(_row("r", source_media_url="https://drive/swift-src-1.jpg"))
    row["image_url"] = "https://cdn/reframed-feed-safe.jpg"  # publish-time reframe
    assert canonical_digest(row) != row["approval_digest"]

    bare = human_prove(_row("bare"))  # image_url is the ONLY identity
    bare["image_url"] = "https://cdn/reframed-feed-safe.jpg"
    assert canonical_digest(bare) != bare["approval_digest"]


def test_echo_only_approval_digest_matches_but_is_not_human_proof():
    """Defect 1: Echo's approve stores a CORRECT digest yet leaves kind /
    actor / time UNPROVED -- matching digest alone never satisfies the gate."""
    row = echo_approve(_row("r"))
    assert row["status"] == "approved"
    assert row["approval_digest"] == canonical_digest(row)
    assert row["approval_kind"] is None
    assert row["approved_by"] is None
    assert row["approved_at"] is None


def test_media_swap_after_approval_invalidates_proof():
    """Defect 5: a portal media swap lands a NEW asset/source after approval;
    the old approval must not publish it."""
    row = human_prove(_row("r", source_media_asset_id="drive-file-1",
                           image_url="https://cdn/old.jpg"))
    row["source_media_asset_id"] = "drive-file-2"
    row["image_url"] = "https://cdn/new.jpg"
    assert canonical_digest(row) != row["approval_digest"]
