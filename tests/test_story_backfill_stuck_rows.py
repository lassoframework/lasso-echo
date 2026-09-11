"""
story_backfill_stuck_rows: synthetic-fixture coverage for the one-time backfill
that corrects story_request/story_render pairs left stuck by the pre-fix _held()
bug (PR #86). Everything here runs against an in-memory fake store + an
injectable calendar_row_lookup -- no live Supabase, no live KV.

Three shapes matter and must be told apart:
  1. STUCK (KV empty): request pending or held, render pending, no real calendar
     row ever recorded -> must be corrected to held/denied.
  2. HEALTHY-PENDING (KV has a real row, the "ENG" shape found live on
     production 2026-09-08): request pending, render pending, but a REAL
     calendar card exists and is simply awaiting a coach -> must NEVER be
     touched, even though it looks identical to (1) by status alone.
  3. ALREADY-RESOLVED (denied/denied, or already held/denied): not a candidate
     at all, or a no-op re-run.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.jobs import story_backfill_stuck_rows as job  # noqa: E402
from agent.story_studio import STATUS_DENIED, STATUS_HELD, STATUS_PENDING  # noqa: E402


class _FakeStore:
    """In-memory story_request/story_render pair store, shaped like
    SupabaseStoryStudioStore's public surface (list_requests, render_for_request,
    update_request, update_render) but with zero network calls."""

    def __init__(self):
        self.requests = {}   # (gym, id) -> dict
        self.renders = {}    # (gym, id) -> dict  (render id == request id, per prod)
        self.request_patches = []
        self.render_patches = []

    def seed(self, gym, req_id, req_status, render_status, *, hold_reason=None):
        self.requests[(gym, req_id)] = {
            "id": req_id, "gym_id": gym, "status": req_status,
            "hold_reason": hold_reason, "created_at": "2026-09-01T00:00:00Z"}
        self.renders[(gym, req_id)] = {
            "id": req_id, "request_id": req_id, "gym_id": gym,
            "status": render_status,
            "calendar_row_id": f"story_{req_id}"}

    def list_requests(self, gym_id, status=None):
        return [r for (g, _rid), r in self.requests.items()
                if g == gym_id and (status is None or r.get("status") == status)]

    def render_for_request(self, request_id, gym_id):
        return self.renders.get((gym_id, request_id))

    def update_request(self, request_id, fields, gym_id=None):
        self.request_patches.append((gym_id, request_id, dict(fields)))
        row = self.requests[(gym_id, request_id)]
        row.update(fields)
        return True

    def update_render(self, request_id, fields, gym_id=None):
        self.render_patches.append((gym_id, request_id, dict(fields)))
        row = self.renders[(gym_id, request_id)]
        row.update(fields)
        return True


def _lookup_factory(real_rows):
    """real_rows: {(gym, request_id): "real-calendar-id"} -- anything absent
    returns "" (no real row), matching _stored_calendar_row_id's contract."""
    def _lookup(gym_id, request_id):
        return real_rows.get((gym_id, request_id), "")
    return _lookup


# ---- finds the stuck shape, ignores the healthy one -------------------------
def test_finds_stuck_pending_pending_pair():
    store = _FakeStore()
    store.seed("crossfitreverb30b5b2", "f75eb466", STATUS_PENDING, STATUS_PENDING)
    pairs = job.find_stuck_pairs(store, ["crossfitreverb30b5b2"],
                                 calendar_row_lookup=_lookup_factory({}))
    assert len(pairs) == 1
    assert pairs[0]["request"]["id"] == "f75eb466"


def test_finds_stuck_held_pending_pair_pete_shape():
    """Pete/CrossFit Zanshin: story_request already hand-corrected to 'held', its
    paired story_render was missed and still reads 'pending'."""
    store = _FakeStore()
    store.seed("zanshinfitness630e22", "8e1b4bdf", STATUS_HELD, STATUS_PENDING,
               hold_reason="already corrected by hand")
    pairs = job.find_stuck_pairs(store, ["zanshinfitness630e22"],
                                 calendar_row_lookup=_lookup_factory({}))
    assert len(pairs) == 1


def test_ignores_healthy_pending_pair_with_a_real_calendar_row_eng_shape():
    """ENG: pending/pending by status alone -- IDENTICAL surface shape to the
    stuck rows -- but a real calendar row exists (KV has it). This is a
    legitimate story awaiting a coach's review and must never be touched."""
    store = _FakeStore()
    store.seed("eng", "143e10da", STATUS_PENDING, STATUS_PENDING)
    real = {("eng", "143e10da"): "3b38495c-d5ca-40e1-9204-eedffd5cf937"}
    pairs = job.find_stuck_pairs(store, ["eng"], calendar_row_lookup=_lookup_factory(real))
    assert pairs == []


def test_ignores_already_resolved_denied_pair():
    store = _FakeStore()
    store.seed("piercefitness", "1fc92fcc", STATUS_DENIED, STATUS_DENIED)
    pairs = job.find_stuck_pairs(store, ["piercefitness"],
                                 calendar_row_lookup=_lookup_factory({}))
    assert pairs == []


def test_ignores_gym_with_no_rows_at_all():
    store = _FakeStore()
    pairs = job.find_stuck_pairs(store, ["emptygym"], calendar_row_lookup=_lookup_factory({}))
    assert pairs == []


# ---- correct_pair: the actual write shape ------------------------------------
def test_correct_pair_dry_run_makes_no_writes():
    store = _FakeStore()
    store.seed("crossfitreverb30b5b2", "f75eb466", STATUS_PENDING, STATUS_PENDING)
    pair = {"gym_id": "crossfitreverb30b5b2",
            "request": store.requests[("crossfitreverb30b5b2", "f75eb466")],
            "render": store.renders[("crossfitreverb30b5b2", "f75eb466")]}
    result = job.correct_pair(store, pair, apply=False)
    assert result["changed"] is True
    assert result["applied"] is False
    assert store.request_patches == []
    assert store.render_patches == []
    # the live rows are untouched
    assert store.requests[("crossfitreverb30b5b2", "f75eb466")]["status"] == STATUS_PENDING
    assert store.renders[("crossfitreverb30b5b2", "f75eb466")]["status"] == STATUS_PENDING


def test_correct_pair_apply_writes_held_and_denied():
    store = _FakeStore()
    store.seed("crossfitreverb30b5b2", "f75eb466", STATUS_PENDING, STATUS_PENDING)
    pair = {"gym_id": "crossfitreverb30b5b2",
            "request": store.requests[("crossfitreverb30b5b2", "f75eb466")],
            "render": store.renders[("crossfitreverb30b5b2", "f75eb466")]}
    result = job.correct_pair(store, pair, apply=True, now="2026-09-08T00:00:00Z")
    assert result["applied"] is True
    assert store.requests[("crossfitreverb30b5b2", "f75eb466")]["status"] == STATUS_HELD
    assert store.requests[("crossfitreverb30b5b2", "f75eb466")]["hold_reason"]
    assert store.renders[("crossfitreverb30b5b2", "f75eb466")]["status"] == STATUS_DENIED
    # calendar_row_id is left UNTOUCHED -- matches a normal post-fix _held() call
    assert store.renders[("crossfitreverb30b5b2", "f75eb466")]["calendar_row_id"] == \
        "story_f75eb466"


def test_correct_pair_pete_shape_only_patches_the_render_never_the_request():
    """Pete's story_request was already correctly held with its own hold_reason.
    The backfill must not clobber that reason by re-writing the request row."""
    store = _FakeStore()
    store.seed("zanshinfitness630e22", "8e1b4bdf", STATUS_HELD, STATUS_PENDING,
               hold_reason="the exact reason written 2026-09-08")
    pair = {"gym_id": "zanshinfitness630e22",
            "request": store.requests[("zanshinfitness630e22", "8e1b4bdf")],
            "render": store.renders[("zanshinfitness630e22", "8e1b4bdf")]}
    result = job.correct_pair(store, pair, apply=True)
    assert result["applied"] is True
    assert store.request_patches == []          # request already correct: no write
    assert len(store.render_patches) == 1        # only the render needed fixing
    assert store.requests[("zanshinfitness630e22", "8e1b4bdf")]["hold_reason"] == \
        "the exact reason written 2026-09-08"
    assert store.renders[("zanshinfitness630e22", "8e1b4bdf")]["status"] == STATUS_DENIED


# ---- idempotency: run() twice is a no-op the second time --------------------
def test_run_twice_is_idempotent():
    store = _FakeStore()
    store.seed("crossfitreverb30b5b2", "f75eb466", STATUS_PENDING, STATUS_PENDING)
    lookup = _lookup_factory({})

    first = job.run(gyms=["crossfitreverb30b5b2"], apply=True, store=store,
                     calendar_row_lookup=lookup, now="2026-09-08T00:00:00Z")
    assert len(first) == 1 and first[0]["changed"] is True

    second = job.run(gyms=["crossfitreverb30b5b2"], apply=True, store=store,
                      calendar_row_lookup=lookup, now="2026-09-08T00:00:00Z")
    # after correction the render reads 'denied' -- find_stuck_pairs filters it
    # out entirely (render.status != pending), so nothing is even a candidate.
    assert second == []
    # no additional writes happened on the second pass
    assert len(store.request_patches) == 1
    assert len(store.render_patches) == 1


def test_run_scans_multiple_gyms_and_reports_a_full_before_after_ledger():
    store = _FakeStore()
    store.seed("crossfitreverb30b5b2", "f75eb466", STATUS_PENDING, STATUS_PENDING)
    store.seed("zanshinfitness630e22", "8e1b4bdf", STATUS_HELD, STATUS_PENDING,
               hold_reason="already corrected by hand")
    store.seed("eng", "143e10da", STATUS_PENDING, STATUS_PENDING)  # healthy, must survive
    real = {("eng", "143e10da"): "3b38495c-d5ca-40e1-9204-eedffd5cf937"}

    results = job.run(gyms=["crossfitreverb30b5b2", "zanshinfitness630e22", "eng"],
                       apply=True, store=store, calendar_row_lookup=_lookup_factory(real),
                       now="2026-09-08T00:00:00Z")
    touched = {r["request_id"] for r in results}
    assert touched == {"f75eb466", "8e1b4bdf"}
    assert "143e10da" not in touched
    # ENG's rows are byte-for-byte untouched
    assert store.requests[("eng", "143e10da")]["status"] == STATUS_PENDING
    assert store.renders[("eng", "143e10da")]["status"] == STATUS_PENDING
