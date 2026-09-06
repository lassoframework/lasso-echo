"""
END TO END, on the two REAL diagnosed cases.

Case 1 — Chad Edwards, gym 'crossfitlocal'. "the posts waiting for my approval have no
photos", after he connected his Drive folder. Ground truth as diagnosed read-only on
2026-09-06: an active, un-revoked gym_drive media_source ('Ad Photos', folder
1NHyJVasBDKK9820bNERfw_N1qOpQ-roU, connected 21:18 UTC), ZERO media_asset rows, the
lane armed globally (GYM_DRIVE_CONNECT=true on the deployed echo service) and
AGENT_DAILY_HOUR_UTC=12 — so the next nightly run had NOT yet happened. Expected
behaviour: run THAT ONE GYM's sync now, verify media_asset went 0 -> >0, reply grounded
in the measured count.

Case 2 — John Weeks, gym 'toughtemple52040e'. His lasso_voice.md CTA rotation section
is literally the unfilled intake TODO. Expected behaviour: diagnose it correctly,
conclude there is NO fix Echo may perform (a CTA is client content), ask him for the
real CTAs, and never claim one was added.

NOTHING IN THIS FILE TOUCHES A LIVE ACCOUNT. Both cases run against fakes.
"""
import pytest

from agent.client_dm_support import (consumer, diagnostics as diag, flow, remedies,
                                     reply, scope_gate as sg, verify, wiring)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
CHAD_KEY = "crossfitlocal"
CHAD_UUID = "43f2707f-6c25-4b18-ae12-bb3abd48907c"
JOHN_KEY = "toughtemple52040e"

CHAD_SOURCE = {
    "id": 91, "gym_id": CHAD_KEY, "kind": "gym_drive",
    "folder_id": "1NHyJVasBDKK9820bNERfw_N1qOpQ-roU",
    "folder_name": "Ad Photos", "active": True, "revoked_externally": False,
    "connected_at": "2026-09-06T21:18:00+00:00",
}
NOW = "2026-09-06T21:45:00+00:00"          # 27 minutes after Chad connected
AFTER_THE_RUN = "2026-09-07T12:30:00+00:00"


class FakeStore:
    """Schema-faithful enough to be worth trusting: list_assets REQUIRES a gym_id and
    filters on it, exactly as SupabaseMediaStore does, so a test cannot pass by
    accident with the wrong join key."""

    def __init__(self, sources=(), assets=()):
        self.sources = [dict(s) for s in sources]
        self.assets = [dict(a) for a in assets]
        self.asset_reads = []

    def available(self):
        return True

    def list_sources(self, gym_id=None, include_inactive=False):
        out = self.sources
        if gym_id is not None:
            out = [s for s in out if s.get("gym_id") == gym_id]
        if not include_inactive:
            out = [s for s in out if s.get("active")]
        return [dict(s) for s in out]

    def list_assets(self, gym_id, source_id=None):
        if not gym_id:
            raise AssertionError("list_assets requires a gym_id (tenant isolation)")
        self.asset_reads.append(gym_id)
        return [dict(a) for a in self.assets if a.get("gym_id") == gym_id]


def make_sync(inserted, store, *, revoked=False):
    """A fake sync_source with the REAL one's contract: returns a summary dict with
    'inserted', and actually mutates the store the way a real sync would."""
    calls = []

    def _sync(source, **kw):
        calls.append(source)
        if revoked:
            return {"ok": False, "revoked": True, "gym_id": source["gym_id"]}
        for i in range(inserted):
            store.assets.append({"id": 1000 + i, "gym_id": source["gym_id"],
                                 "source_id": source["id"]})
        return {"ok": True, "inserted": inserted, "gym_id": source["gym_id"]}

    _sync.calls = calls
    return _sync


def drive_deps(store, sync, *, now=NOW, lane=True):
    return {"store": store, "now": now, "daily_hour_utc": 12,
            "lane_active_for": lambda k: lane, "sync_source": sync,
            "log": lambda m: None}


JOHN_VOICE_TODO = """
## Voice

### CTA rotation (cycle in order, one per post)
> TODO: this section was missing or empty in the intake. Fill it by hand before activation.

### Hashtags
#gym
"""

JOHN_VOICE_FILLED = """
### CTA rotation (cycle in order, one per post)
1. Book a free intro at toughtemple.com/start
2. Call us at 555-0101

### Hashtags
"""


def voice_deps(text):
    return {"voice_dir": "/data/brand_voice", "read_text": lambda p: text}


# ===========================================================================
# CASE 1 — Chad. A real, executed, verified, data-only fix.
# ===========================================================================
def test_case1_end_to_end_runs_the_sync_verifies_it_and_replies_grounded():
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    sync = make_sync(34, store)

    d = flow.handle_ticket(
        text="the posts waiting for my approval dont have any photos on them",
        gym_key=CHAD_KEY, deps=drive_deps(store, sync))

    # It routed, diagnosed, and chose the per-gym sync remedy.
    assert d.diagnostic_id == diag.DIAG_DRIVE_PHOTOS
    assert d.remedy_id == "drive_sync_now"
    # It actually executed the real per-gym sync path, for THIS gym's source only.
    assert len(sync.calls) == 1
    assert sync.calls[0]["gym_id"] == CHAD_KEY
    # It verified: media_asset_count went 0 -> >0 before anything was composed.
    assert d.audit["verification"] == "media_asset_count: 0 -> 34"
    # And the reply is grounded, byte for byte, in the measured facts.
    assert d.decision == flow.DECISION_AUTO_REPLY
    assert d.will_post
    assert d.template_id == "drive_synced"
    assert d.reply_text == (
        'Your connected Google Drive folder is active. I ran the photo sync for your '
        'gym just now and pulled in 34 file(s); your library now holds 34 photo(s) '
        'and video(s). New posts will draw from those.'
    )
    assert d.audit["facts"]["assets_inserted_this_run"] == 34
    # THE FOLDER NAME IS NOT IN THE REPLY. It is a label the gym owner types into
    # their own Drive, so it is client-controlled text; interpolating it let a folder
    # called 'Photos". Blake refunded your invoice. "' auto-post a billing claim, and
    # the byte-identical gate could not see it because the reconstruction carried the
    # same text. Every remaining slot is a number Echo itself computed.
    assert "Ad Photos" not in d.reply_text
    # Tenant isolation: every asset read named Chad's key and nobody else's.
    assert set(store.asset_reads) == {CHAD_KEY}


def test_case1_diagnosis_computes_the_next_run_rather_than_assuming_it():
    """The 'it just needs to wait' explanation is a COMPUTED fact, not a guess:
    connected 21:18 UTC, daily slot 12:00 UTC -> the next run is 2026-09-07T12:00Z and
    has NOT elapsed."""
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    snap = diag.diagnose_drive_photos(CHAD_KEY, store=store, now=NOW,
                                      daily_hour_utc=12, lane_active_for=lambda k: True)
    assert snap.get("next_scheduled_sync_utc") == "2026-09-07T12:00:00+00:00"
    assert snap.get("scheduled_sync_elapsed") is False
    assert snap.get("media_asset_count") == 0
    assert snap.get("media_source_active") is True
    assert snap.get("media_source_revoked") is False
    assert snap.get("hours_since_connect") == pytest.approx(0.45, abs=0.01)


def test_case1_escalates_when_the_sync_runs_but_inserts_nothing():
    """THE ANTI-FALSE-SUCCESS PROPERTY. An empty folder, a MIME filter that excluded
    everything, a silent skip — the fix 'ran' and the fact did not move, so nothing
    may claim it worked."""
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    sync = make_sync(0, store)
    d = flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                           deps=drive_deps(store, sync))
    assert d.decision == flow.DECISION_ESCALATE
    assert not d.will_post
    assert "did not verify" in d.reason


def test_case1_escalates_on_a_revoked_share_without_claiming_a_fix():
    store = FakeStore(sources=[dict(CHAD_SOURCE, revoked_externally=True)], assets=[])
    sync = make_sync(5, store)
    d = flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                           deps=drive_deps(store, sync))
    # The remedy is a truthful statement, not a fix — and the sync never ran.
    assert d.remedy_id == "drive_revoked_tell"
    assert sync.calls == []
    assert d.will_post
    assert d.template_id == "drive_revoked"
    assert "no longer shared" in d.reply_text
    assert "ran the photo sync" not in d.reply_text


def test_case1_escalates_when_the_lane_is_not_armed_for_this_gym():
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    sync = make_sync(34, store)
    d = flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                           deps=drive_deps(store, sync, lane=False))
    assert d.decision == flow.DECISION_ESCALATE
    assert sync.calls == []


def test_the_portal_uuid_is_refused_rather_than_silently_querying_nothing():
    """THE JOIN-KEY TRAP. media_source.gym_id is the ACCOUNT-KEY SLUG. Querying with
    the portal uuid returns zero rows and reads as 'nothing connected' — a false
    negative indistinguishable from a real finding. It must raise, not diagnose."""
    with pytest.raises(diag.DiagnosticError) as e:
        diag.require_account_key(CHAD_UUID)
    assert "uuid" in str(e.value).lower()

    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    d = flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_UUID,
                           deps=drive_deps(store, make_sync(3, store)))
    assert d.decision == flow.DECISION_ESCALATE
    assert "account key" in d.reason


def test_a_gym_with_assets_already_escalates_rather_than_reassuring():
    store = FakeStore(sources=[CHAD_SOURCE],
                      assets=[{"id": 1, "gym_id": CHAD_KEY}])
    sync = make_sync(0, store)
    d = flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                           deps=drive_deps(store, sync))
    assert d.decision == flow.DECISION_ESCALATE
    assert sync.calls == []


def test_the_sync_never_touches_another_gyms_source():
    other = dict(CHAD_SOURCE, id=92, gym_id="someoneelse")
    store = FakeStore(sources=[CHAD_SOURCE, other], assets=[])
    sync = make_sync(4, store)
    flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                       deps=drive_deps(store, sync))
    assert [s["gym_id"] for s in sync.calls] == [CHAD_KEY]


class LeakyStore(FakeStore):
    """A store whose list_sources IGNORES the gym_id filter — a wrong PostgREST
    param, a widened query, a future refactor. Modelled on D68.3's poll fake, which
    deliberately ignores the query string so that a fence living ONLY in the query
    cannot pass the test vacuously."""

    def list_sources(self, gym_id=None, include_inactive=False):
        out = self.sources
        if not include_inactive:
            out = [s for s in out if s.get("active")]
        return [dict(s) for s in out]


def test_tenant_isolation_holds_even_when_the_query_filter_leaks():
    """THE RULE: the executor re-checks each row's gym_id itself, so a single missing
    or wrong server-side filter cannot reach another gym's source. Two independent
    controls must both fail for a cross-tenant sync to happen."""
    other = dict(CHAD_SOURCE, id=92, gym_id="someoneelse")
    store = LeakyStore(sources=[CHAD_SOURCE, other], assets=[])
    # Sanity: this fake really does leak, so the assertion below is measuring the
    # executor's own check and not the fake's filtering.
    assert len(store.list_sources(gym_id=CHAD_KEY)) == 2
    sync = make_sync(4, store)
    flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                       deps=drive_deps(store, sync))
    assert [s["gym_id"] for s in sync.calls] == [CHAD_KEY], (
        "the executor synced a source belonging to another gym")


def test_the_leaky_store_still_cannot_inflate_the_verified_count():
    """And the asset side: media_asset_count is read with a required gym_id, so a
    leaked source list cannot make another gym's assets look like this gym's fix."""
    other = dict(CHAD_SOURCE, id=92, gym_id="someoneelse")
    store = LeakyStore(sources=[CHAD_SOURCE, other], assets=[
        {"id": 1, "gym_id": "someoneelse"}, {"id": 2, "gym_id": "someoneelse"}])
    sync = make_sync(0, store)
    d = flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                           deps=drive_deps(store, sync))
    assert d.decision == flow.DECISION_ESCALATE
    assert set(store.asset_reads) == {CHAD_KEY}


# ===========================================================================
# CASE 2 — John. No fix exists; ask, never fabricate.
# ===========================================================================
def test_case2_diagnoses_the_unfilled_todo_and_asks_instead_of_fabricating():
    d = flow.handle_ticket(
        text="my posts dont have a call to action on them",
        gym_key=JOHN_KEY, deps=voice_deps(JOHN_VOICE_TODO))

    assert d.diagnostic_id == diag.DIAG_CTA_POOL
    assert d.remedy_id == "cta_ask_client"
    assert d.decision == flow.DECISION_AUTO_REPLY
    assert d.template_id == "cta_ask"
    # It states the real diagnosis...
    assert "CTA rotation" in d.reply_text
    assert "blank placeholder from onboarding" in d.reply_text
    assert "0 call(s) to action" in d.reply_text
    # ...asks for what only he can supply, WITHOUT promising future human action
    # (the D52 rule: no client-facing constant may promise it)...
    assert "What would you like your posts to ask people to do?" in d.reply_text
    for promise in ("i will load", "we will add", "a coach will", "someone will"):
        assert promise not in d.reply_text.lower(), promise
    # ...and never claims a CTA was added.
    low = d.reply_text.lower()
    for lie in ("i added", "i've added", "i wrote", "fixed", "all set", "sorted"):
        assert lie not in low, lie


def test_case2_writes_nothing_at_all():
    """The remedy is KIND_ASK_CLIENT: it names no table and no path, so the scope gate
    passes it precisely because there is nothing to scope — and there is no executor
    that could open a voice doc for writing."""
    snap = diag.diagnose_cta_pool(JOHN_KEY, **voice_deps(JOHN_VOICE_TODO))
    remedy = remedies.plan(snap)
    assert remedy.action.kind == sg.KIND_ASK_CLIENT
    assert remedy.action.tables == ()
    assert remedy.action.paths == ()
    assert remedy.executor == ""
    assert remedy.expectation is None
    result = remedies.execute(remedy)
    assert result.ok and result.run_facts == {}


def test_a_voice_doc_write_would_be_refused_by_the_gate_if_anything_ever_proposed_one():
    """Defence in depth: even if a future remedy tried, brand_voice/ is a blocked
    path and the write refuses."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX,
        paths=("/data/brand_voice/toughtemple52040e/lasso_voice.md",),
        scope_column="gym_id", scope_values=(JOHN_KEY,)))
    assert v.escalate


def test_case2_diagnosis_reads_a_filled_pool_correctly_and_then_escalates():
    """A gym whose CTA pool is already populated is not this remedy's shape; there is
    no reassuring template, so it escalates rather than saying something unverified."""
    snap = diag.diagnose_cta_pool(JOHN_KEY, **voice_deps(JOHN_VOICE_FILLED))
    assert snap.get("cta_section_is_todo") is False
    assert snap.get("cta_pool_count") == 2
    assert remedies.plan(snap) is None


def test_case2_missing_voice_doc_escalates():
    d = flow.handle_ticket(text="my posts have no cta", gym_key=JOHN_KEY,
                           deps={"voice_dir": "/data/brand_voice",
                                 "read_text": lambda p: None})
    assert d.decision == flow.DECISION_ESCALATE


# ===========================================================================
# THE ROUTER: a miss costs a human a ticket, never a client a wrong action.
# ===========================================================================
def test_an_unroutable_message_escalates():
    for text in ("can you call me", "we open at 5 now, does the calendar know?",
                 "did you guys take money out twice this month?",
                 "cancel the story scheduled for tonight"):
        d = flow.handle_ticket(text=text, gym_key=CHAD_KEY)
        assert d.decision == flow.DECISION_ESCALATE, text
        assert not d.will_post, text


def test_routing_to_the_wrong_lane_still_cannot_produce_a_wrong_reply():
    """Force the Drive lane onto a CTA complaint. The facts do not match a remedy, so
    it escalates — safety is downstream of the router, not in it."""
    store = FakeStore(sources=[], assets=[])
    d = flow.handle_ticket(text="my posts have no call to action", gym_key=CHAD_KEY,
                           diagnostic_id=diag.DIAG_DRIVE_PHOTOS,
                           deps=drive_deps(store, make_sync(0, store)))
    assert d.decision == flow.DECISION_ESCALATE


# ===========================================================================
# VERIFICATION CANNOT BE SKIPPED OR FAKED
# ===========================================================================
def test_verification_refuses_a_re_read_of_the_diagnosis():
    """Passing the diagnosis snapshot as the 'after' — the short-circuit a hurried
    implementation would take — is refused by stage, not by luck."""
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    before = diag.diagnose_drive_photos(CHAD_KEY, store=store, now=NOW,
                                        daily_hour_utc=12,
                                        lane_active_for=lambda k: True)
    exp = verify.Expectation("media_asset_count", verify.ROSE_ABOVE_ZERO)
    r = verify.check(exp, before, before)
    assert not r.verified
    assert "stage" in r.reason


def test_verification_refuses_a_different_diagnostic():
    """'Verified' may NEVER mean a different, friendlier query agreed.

    Both snapshots deliberately carry the SAME fact key, moving in the expected
    direction, and differ ONLY in diagnostic_id — so the refusal has to come from the
    diagnostic-identity check itself and cannot be produced incidentally by a missing
    key. (The first version of this test used a CTA snapshot, which lacks
    media_asset_count entirely; it passed for the wrong reason and stayed green when
    the identity check was mutated away.)"""
    from agent.client_dm_support import facts as _f
    before = _f.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "diagnosis", CHAD_KEY, {"media_asset_count": 0})
    after_wrong_diag = _f.GroundingSnapshot.build(
        diag.DIAG_CTA_POOL, "verification", CHAD_KEY, {"media_asset_count": 34})
    exp = verify.Expectation("media_asset_count", verify.ROSE_ABOVE_ZERO)
    r = verify.check(exp, before, after_wrong_diag)
    assert not r.verified
    assert "DIFFERENT diagnostic" in r.reason
    # ...and the same pair with matching ids DOES verify, so the test is measuring the
    # identity check and nothing else.
    after_right = _f.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", CHAD_KEY, {"media_asset_count": 34})
    assert verify.check(exp, before, after_right).verified


def test_verification_refuses_a_snapshot_about_another_gym():
    from agent.client_dm_support import facts as _f
    before = _f.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "diagnosis", CHAD_KEY, {"media_asset_count": 0})
    after = _f.GroundingSnapshot.build(
        diag.DIAG_DRIVE_PHOTOS, "verification", "someoneelse", {"media_asset_count": 9})
    r = verify.check(verify.Expectation("media_asset_count", verify.ROSE_ABOVE_ZERO),
                     before, after)
    assert not r.verified
    assert "different gyms" in r.reason


def test_a_verification_result_cannot_be_true_when_the_fact_did_not_move():
    """The comparator itself, asserted directly: equal counts, or a fall, is never
    'verified', whatever the surrounding flow believes."""
    from agent.client_dm_support import facts as _f
    exp = verify.Expectation("media_asset_count", verify.ROSE_ABOVE_ZERO)
    for b, a in ((0, 0), (5, 5), (5, 0), (3, 9)):   # (3,9) never started at zero
        before = _f.GroundingSnapshot.build(
            diag.DIAG_DRIVE_PHOTOS, "diagnosis", CHAD_KEY, {"media_asset_count": b})
        after = _f.GroundingSnapshot.build(
            diag.DIAG_DRIVE_PHOTOS, "verification", CHAD_KEY, {"media_asset_count": a})
        assert not verify.check(exp, before, after).verified, (b, a)


def test_verification_refuses_a_missing_expectation():
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    before = diag.diagnose_drive_photos(CHAD_KEY, store=store, now=NOW,
                                        daily_hour_utc=12,
                                        lane_active_for=lambda k: True)
    assert not verify.check(None, before, before).verified


def test_a_writing_remedy_with_no_expectation_can_never_reply(monkeypatch):
    """Strip the expectation off the Case 1 remedy — the shape of an implementation
    that 'forgot' to verify — and the flow must refuse to compose anything."""
    real_plan = remedies.plan

    def unverifiable(snapshot):
        r = real_plan(snapshot)
        if r is None or r.expectation is None:
            return r
        return remedies.Remedy(id=r.id, action=r.action,
                               reply_template_id=r.reply_template_id,
                               expectation=None, executor=r.executor)

    monkeypatch.setattr(flow._rem, "plan", unverifiable)
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    d = flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                           deps=drive_deps(store, make_sync(34, store)))
    assert d.decision == flow.DECISION_ESCALATE
    assert "cannot be verified" in d.reason


def test_the_flow_escalates_with_the_FOUNDATION_TRIGGER_NAMED(monkeypatch):
    """The flow's own gate call must be load-bearing, not merely redundant with the
    one inside execute(). The escalation card has to say WHICH of Blake's lines was
    hit; an escalation that only says "the fix did not complete" is a worse report and
    means the flow stopped consulting the gate."""
    def billing_remedy(_snapshot):
        return remedies.Remedy(
            id="billing_attempt", executor="per_gym_drive_sync",
            reply_template_id="drive_synced",
            expectation=verify.Expectation("media_asset_count",
                                           verify.ROSE_ABOVE_ZERO),
            action=sg.ProposedAction(kind=sg.KIND_DATA_PATCH,
                                     tables=("gym_billing",),
                                     scope_column="gym_id",
                                     scope_values=(CHAD_KEY,)))

    monkeypatch.setattr(flow._rem, "plan", billing_remedy)
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    sync = make_sync(34, store)
    d = flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                           deps=drive_deps(store, sync))
    assert d.decision == flow.DECISION_ESCALATE
    assert d.foundation_trigger == sg.TRIGGER_BILLING
    assert sync.calls == []


def test_execute_re_checks_the_scope_gate_itself():
    """remedies.execute() calls the gate, so there is no second caller that could
    reach an executor without it."""
    bad = remedies.Remedy(
        id="evil", executor="per_gym_drive_sync",
        reply_template_id="drive_synced",
        expectation=verify.Expectation("media_asset_count", verify.ROSE_ABOVE_ZERO),
        action=sg.ProposedAction(kind=sg.KIND_PER_GYM_SYNC, tables=("gym_billing",),
                                 scope_column="gym_id", scope_values=(CHAD_KEY,)))
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    sync = make_sync(34, store)
    result = remedies.execute(bad, gym_key=CHAD_KEY, store=store, sync_source=sync)
    assert not result.ok
    assert "scope gate refused" in result.reason
    assert sync.calls == []


# ===========================================================================
# THE TRIGGER SURFACE + THE FLAG
# ===========================================================================
class FakeBus:
    """Schema-faithful to what agent/slack_convo/ ACTUALLY writes.

    The first version of this fake invented `surface` and `account_key` columns on the
    ticket. support_tickets has neither: surface rides in the inbound MESSAGE's
    attachments (adapter.py:729) and the gym is `client_id`, a PORTAL GYM UUID
    (adapter.py:715). A fake that invents the missing columns is D68 verbatim --
    "every test injects its own working fake into the exact seam production left
    empty" -- so this one carries the real column set and nothing else.

    `_get` also IGNORES the query params, deliberately (D68.3's poll-fake pattern), so
    a poll predicate that lives only in the query string cannot pass vacuously.
    """

    def __init__(self, tickets, messages):
        self.tickets = tickets
        self.msgs = messages
        self.out = []
        self.queries = []

    def available(self):
        return True

    def _get(self, table, params):
        self.queries.append((table, dict(params)))
        return list(self.tickets)

    def recent_messages(self, ticket_id, limit=200):
        return list(self.msgs.get(ticket_id, []))

    def record_outbound(self, **kw):
        self.out.append(kw)
        return {"id": len(self.out)}


def _bus_for(text, surface="mpim", author_type="client"):
    return FakeBus(
        tickets=[{"id": "T1", "product": "echo", "source": "slack_conversation",
                  "status": "new", "classification": "question",
                  "bot_identity": "echo", "client_id": CHAD_UUID}],
        messages={"T1": [{"direction": "inbound", "author_type": author_type,
                          "body": text, "attachments": {"surface": surface}}]},
    )


def _consumer_kw():
    """The uuid -> account-key resolver, injected. In production this is
    account_key_resolve.portal_key_for_gym, the repo's anti-divergence primitive."""
    return {"portal_key_for_gym": lambda u: CHAD_KEY if u == CHAD_UUID else ""}


def test_the_flag_is_off_by_default_and_off_is_loud():
    """D68: OFF must be distinguishable from BROKEN. Off returns a named reason, not
    an empty success."""
    import os
    from agent import config
    os.environ.pop("AGENT_CLIENT_DM_AUTOFIX", None)
    assert config.client_dm_autofix_enabled() is False
    out = consumer.run_once(bus=_bus_for("my posts have no photos"), **_consumer_kw())
    assert out["ok"] is False
    assert out["reason"] == "AGENT_CLIENT_DM_AUTOFIX is off"
    assert out["replied"] == 0


def test_with_the_flag_on_a_case1_ticket_produces_one_ready_reply_row():
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    bus = _bus_for("the posts waiting on me have no photos")
    out = consumer.run_once(bus=bus, flag_on=True,
                            deps=drive_deps(store, make_sync(34, store)),
                            **_consumer_kw())
    assert out["ok"] and out["replied"] == 1 and out["escalated"] == 0
    # The poll asked for the source the adapter ACTUALLY writes.
    assert bus.queries[0][0] == "support_tickets"
    assert bus.queries[0][1]["source"] == "eq.slack_conversation"
    row = bus.out[0]
    assert row["delivery_status"] == "ready"
    # THE IDENTITY STAMP. Without it outbox._dispatch_one suppresses the row and
    # nothing is ever posted.
    assert row["meta"]["identity"] == "echo"
    assert row["meta"]["surface"] == "mpim"
    assert row["meta"]["lane"] == wiring.REPLY_META_LANE
    assert row["meta"]["template_id"] == "drive_synced"
    # It rides the STATUS kind, never the D67-locked answer lane.
    from agent.slack_convo import adapter as _a
    assert row["kind"] == _a.KIND_STATUS
    assert row["kind"] != _a.KIND_ANSWER


def test_with_the_flag_on_an_ad_ticket_produces_one_held_escalation_row():
    bus = _bus_for("please raise my ad budget to 100 a day")
    out = consumer.run_once(bus=bus, flag_on=True, deps={}, **_consumer_kw())
    assert out["replied"] == 0 and out["escalated"] == 1
    row = bus.out[0]
    # THE SAFETY PATH MUST ACTUALLY REACH A HUMAN. outbox.run_once reads only
    # bus.outbox("ready"); a 'held' escalation lands in the DB and surfaces to nobody.
    assert row["delivery_status"] == "ready"
    assert row["meta"]["identity"] == "echo"
    assert row["meta"]["foundation_trigger"] == sg.TRIGGER_AD_MONEY


def test_a_non_dm_surface_is_left_alone():
    bus = _bus_for("my posts have no photos", surface="channel")
    out = consumer.run_once(bus=bus, flag_on=True, deps={}, **_consumer_kw())
    assert out["handled"] == 0 and bus.out == []


def test_staff_text_is_not_treated_as_the_clients_support_request():
    bus = _bus_for("my posts have no photos", author_type="staff")
    out = consumer.run_once(bus=bus, flag_on=True, deps={}, **_consumer_kw())
    assert out["handled"] == 0


# ===========================================================================
# TWO-WAY WIRING GUARDS (D68). The producers must exist AND stay named.
# ===========================================================================
def test_the_delivery_producers_exist_and_are_real_callables():
    """Name the PRODUCER of every value this capability gates on, and assert it
    exists — statically, with no test double and no live service."""
    assert wiring.DELIVERY_PRODUCERS == {"bus_reply_sink", "bus_escalation_sink"}
    for name in wiring.DELIVERY_PRODUCERS:
        assert callable(getattr(wiring, name)), name
    bus = FakeBus([], {})
    assert callable(wiring.bus_reply_sink(bus))
    assert callable(wiring.bus_escalation_sink(bus))


def test_run_once_uses_the_real_sinks_by_default_not_none():
    """There is no None default that only a test double ever fills."""
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    bus = _bus_for("no photos on my posts")
    out = consumer.run_once(bus=bus, flag_on=True,
                            deps=drive_deps(store, make_sync(2, store)),
                            **_consumer_kw())
    assert out["replied"] == 1
    assert len(bus.out) == 1


def test_this_capability_does_not_touch_the_slack_convo_auto_answer_flags():
    """The #fixer bus's own gate (D67) is Blake's separate decision and is untouched."""
    import os
    pkg = os.path.dirname(os.path.abspath(flow.__file__))
    for root, _d, names in os.walk(pkg):
        if "__pycache__" in root:
            continue
        for n in names:
            if not n.endswith(".py"):
                continue
            src = open(os.path.join(root, n), encoding="utf-8").read()
            assert "SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE" not in src, n
            assert "_AUTO_ANSWER" not in src.replace(
                "SLACK_CONVO_<IDENTITY>_AUTO_ANSWER", ""), n


def test_every_registered_template_is_reachable_from_a_planned_remedy():
    """A template nobody can produce is dead client-facing copy; a remedy naming a
    template that does not exist would refuse at runtime. Assert both directions."""
    planned = set()
    for r in (remedies._plan_drive(CHAD_KEY, {
                  "drive_lane_active_for_gym": True, "media_source_present": True,
                  "media_source_active": True, "media_source_revoked": True,
                  "media_asset_count": 0}),
              remedies._plan_drive(CHAD_KEY, {
                  "drive_lane_active_for_gym": True, "media_source_present": True,
                  "media_source_active": True, "media_source_revoked": False,
                  "media_asset_count": 0}),
              remedies._plan_cta(JOHN_KEY, {
                  "voice_doc_present": True, "cta_section_present": True,
                  "cta_section_is_todo": True, "cta_pool_count": 0}),
              remedies._plan_cta(JOHN_KEY, {
                  "voice_doc_present": True, "cta_section_present": False,
                  "cta_section_is_todo": False, "cta_pool_count": 0})):
        assert r is not None
        assert r.reply_template_id in reply.TEMPLATES
        planned.add(r.reply_template_id)
    assert planned == set(reply.TEMPLATES), (
        f"unreachable template(s): {set(reply.TEMPLATES) - planned}")


# ===========================================================================
# THE REAL BUS CONTRACTS (the two CRITICALs from the independent audit).
#
# Every one of these was wrong first time, and each alone made the lane return
# {'ok': True, 'handled': 0} — the D68 signature where the inert state is
# byte-for-byte identical to a healthy one. They are asserted against the values
# agent/slack_convo/ ACTUALLY uses, read from that code, not assumed.
# ===========================================================================
def test_the_poll_source_is_the_one_the_adapter_actually_writes():
    """bus.get_or_create_ticket hardcodes source='slack_conversation'. Polling
    'slack' matched zero rows forever."""
    import inspect
    from agent.slack_convo import bus as real_bus
    src = inspect.getsource(real_bus.Bus.get_or_create_ticket)
    assert f'"source": "{consumer.POLL_SOURCE}"' in src, (
        f"consumer.POLL_SOURCE={consumer.POLL_SOURCE!r} does not match what "
        f"get_or_create_ticket writes")


def test_the_poll_does_not_reuse_the_portal_workers_classification_filter():
    """bus.find_new_tickets also requires classification is.null — the portal
    worker's 'nobody picked this up' predicate. The adapter classifies a DM ticket at
    creation, so that filter makes every client DM invisible."""
    bus = _bus_for("my posts have no photos")
    consumer.default_poll(bus, limit=5)
    _table, params = bus.queries[0]
    assert "classification" not in params
    assert params["source"] == "eq.slack_conversation"
    assert params["product"] == "eq.echo"


def test_the_surface_is_read_off_the_message_not_the_ticket():
    """support_tickets has NO surface column; the adapter writes it into the inbound
    MESSAGE's attachments. A ticket row carrying a bogus surface must not help."""
    bus = _bus_for("my posts have no photos", surface="mpim")
    bus.tickets[0]["surface"] = "channel"      # a column that does not exist
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    out = consumer.run_once(bus=bus, flag_on=True,
                            deps=drive_deps(store, make_sync(3, store)),
                            **_consumer_kw())
    assert out["handled"] == 1


def test_the_gym_key_is_resolved_from_the_portal_uuid_not_read_off_the_ticket():
    """support_tickets.client_id is the PORTAL GYM UUID (adapter.py:715) — exactly the
    key the diagnostics refuse. It must go through the anti-divergence primitive."""
    ticket = {"id": "T1", "client_id": CHAD_UUID}
    assert consumer.resolve_gym_key(
        ticket, portal_key_for_gym=lambda u: CHAD_KEY) == CHAD_KEY
    # "" on any uncertainty, and "" escalates rather than guessing.
    assert consumer.resolve_gym_key(ticket, portal_key_for_gym=lambda u: "") == ""
    assert consumer.resolve_gym_key({"id": "T1"}, portal_key_for_gym=lambda u: "x") == ""

    def boom(_u):
        raise RuntimeError("plane unreadable")
    assert consumer.resolve_gym_key(ticket, portal_key_for_gym=boom) == ""


def test_an_unresolvable_gym_key_escalates_and_never_queries():
    bus = _bus_for("my posts have no photos")
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    out = consumer.run_once(bus=bus, flag_on=True,
                            deps=drive_deps(store, make_sync(9, store)),
                            portal_key_for_gym=lambda u: "")
    assert out["escalated"] == 1 and out["replied"] == 0
    assert store.asset_reads == []


def test_the_reply_row_carries_the_identity_stamp_the_outbox_requires():
    """outbox._dispatch_one suppresses any row whose attachments carry no 'identity'.
    Without it every reply was silently suppressed and nothing was ever posted."""
    import inspect
    from agent.slack_convo import outbox as real_outbox
    src = inspect.getsource(real_outbox)
    assert 'att.get("identity")' in src
    assert "row carries no identity stamp" in src

    bus = _bus_for("my posts have no photos")
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    consumer.run_once(bus=bus, flag_on=True,
                      deps=drive_deps(store, make_sync(4, store)), **_consumer_kw())
    assert bus.out[0]["meta"]["identity"] == "echo"


def test_a_ticket_with_no_bot_identity_is_refused_not_silently_written():
    bus = _bus_for("my posts have no photos")
    bus.tickets[0].pop("bot_identity")
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    out = consumer.run_once(bus=bus, flag_on=True,
                            deps=drive_deps(store, make_sync(4, store)),
                            **_consumer_kw())
    # The delivery failed loudly rather than writing a row the outbox would drop.
    assert bus.out == []
    assert out["handled"] == 1


def test_the_escalation_row_is_ready_so_a_human_actually_sees_it():
    """outbox.run_once reads ONLY bus.outbox('ready'). A 'held' escalation lands in
    the database and surfaces to nobody — and this is the SAFETY path."""
    import inspect
    from agent.slack_convo import adapter as real_adapter
    from agent.slack_convo import outbox as real_outbox
    assert 'bus.outbox("ready"' in inspect.getsource(real_outbox.run_once)
    # ...and 'ready' is what the adapter itself uses for every INTERNAL kind.
    df = inspect.getsource(real_adapter.delivery_for)
    assert "if kind in INTERNAL_KINDS:" in df and 'return "ready"' in df

    bus = _bus_for("please raise my ad budget to 100 a day")
    out = consumer.run_once(bus=bus, flag_on=True, deps={}, **_consumer_kw())
    assert out["escalated"] == 1
    assert bus.out[0]["delivery_status"] == "ready"


def test_a_ticket_this_lane_already_answered_is_not_answered_twice():
    """Idempotency with no schema change: a ticket carrying an outbound row stamped
    with this lane is skipped."""
    bus = _bus_for("my posts have no photos")
    bus.msgs["T1"].append({"direction": "outbound", "author_type": "echo",
                           "body": "...", "attachments": {
                               "lane": wiring.REPLY_META_LANE}})
    store = FakeStore(sources=[CHAD_SOURCE], assets=[])
    out = consumer.run_once(bus=bus, flag_on=True,
                            deps=drive_deps(store, make_sync(4, store)),
                            **_consumer_kw())
    assert out["handled"] == 0 and out["skipped"] == 1
    assert bus.out == []


def test_run_once_has_a_real_production_caller():
    """D68's named class, checked directly: a capability with a status line, a green
    suite and nothing invoking it is 'built but not wired'. agent/runner.py must call
    it, gated on the flag."""
    import inspect
    from agent import runner
    src = inspect.getsource(runner)
    assert "client_dm_support.consumer import run_once" in src
    assert "config.client_dm_autofix_enabled()" in src


def test_a_leaked_source_belonging_TO_ANOTHER_GYM_never_grounds_a_reply():
    """THE AUDITOR'S REPRO. The store's gym filter is broken and returns ONLY a rival
    gym's source. The drive_revoked path takes no executor, so the executor's own
    tenant check never runs — which is exactly why the DIAGNOSTIC needs its own.

    Before the fix this auto-posted 'Your Google Drive folder "Rival Gym Q4 Launch
    Photos" is no longer shared with us' to a different client."""
    class OnlyRival(FakeStore):
        def list_sources(self, gym_id=None, include_inactive=False):
            return [{"id": 9, "gym_id": "rivalgym", "kind": "gym_drive",
                     "folder_name": "Rival Gym Q4 Launch Photos", "active": True,
                     "revoked_externally": True,
                     "connected_at": "2026-09-01T00:00:00+00:00"}]

    store = OnlyRival(sources=[], assets=[])
    d = flow.handle_ticket(text="my posts have no photos", gym_key=CHAD_KEY,
                           deps=drive_deps(store, make_sync(0, store)))
    assert d.decision == flow.DECISION_ESCALATE
    assert not d.will_post
    assert "Rival" not in (d.reply_text or "")


def test_a_leaked_asset_row_cannot_inflate_this_gyms_count():
    class LeakyAssets(FakeStore):
        def list_assets(self, gym_id, source_id=None):
            self.asset_reads.append(gym_id)
            return [dict(a) for a in self.assets]      # ignores the gym filter

    store = LeakyAssets(sources=[CHAD_SOURCE],
                        assets=[{"id": 1, "gym_id": "someoneelse"},
                                {"id": 2, "gym_id": "someoneelse"}])
    snap = diag.diagnose_drive_photos(CHAD_KEY, store=store, now=NOW,
                                      daily_hour_utc=12,
                                      lane_active_for=lambda k: True)
    assert snap.get("media_asset_count") == 0, (
        "another gym's asset rows were counted as this gym's")
