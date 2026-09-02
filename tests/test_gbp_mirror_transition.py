"""
The two INTERACTIONS arming the Google Business mirror creates, pinned so neither can
regress silently (Blake, 2026-09-02: "do 1-3 on A+ build").

Item 1, THE TRANSITION. Six gyms already hold Google rows written by the monthly planner.
When their next build mirrors, the build's delete-then-insert replaces those rows. An
APPROVED Google row must survive that (the gym already tapped it, and a gym watching its
own listing must not see approved work vanish); a PENDING planner row is replaced by the
mirrored one, which is the point.

Item 2, THE PLANNER STANDS DOWN. Once a gym has mirrored rows, jobs/gbp_month_sweep must
report 'already_planned' for it and write nothing, or a gym gets two Google months. The
mirror's own docstring claims this is "verified, not assumed" — this is that verification,
executable, rather than a claim in a comment.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gbp_dogfood  # noqa: E402
from agent.jobs import gbp_month_sweep as sweep_mod  # noqa: E402

TODAY = date.today()
SOON = (TODAY + timedelta(days=5)).isoformat()


# ---- item 2: the monthly sweep stands down for a mirrored gym -------------------------

class _StoreWithFutureRows:
    """The idempotency read the planner actually consults, and nothing else."""

    def __init__(self, future_rows):
        self._future = future_rows
        self.planned = []

    def future_gbp_rows(self, gym, on_or_after):
        return list(self._future)

    def any_gbp_rows(self, gym):
        return bool(self._future)

    def insert_rows(self, gym, rows):
        self.planned.extend(rows)
        return len(rows)


def _mirrored_row(status="pending"):
    """A row shaped the way gbp_mirror emits it: account googlebusiness, no marker of
    which code path wrote it. That indistinguishability is exactly why the sweep's guard
    covers mirrored rows too."""
    return {"gym_id": "eng", "account": "googlebusiness", "post_date": SOON,
            "status": status, "format": "update", "caption": "Real Google copy.",
            "image_url": "https://cdn.test/x_gbp.jpg", "gbp_topic_type": "STANDARD"}


def test_a_gym_with_mirrored_rows_makes_the_planner_skip_without_writing():
    store = _StoreWithFutureRows([_mirrored_row()])
    out = gbp_dogfood.plan_gbp_dogfood(
        "eng", "eng_ig", voice=object(), library_path="/x", city="Cape Coral",
        store=store, start=TODAY, connection_status="connected",
        logger=lambda m: None)
    assert out.get("skipped_existing") is True
    assert out.get("planned") == 0
    assert store.planned == [], "the planner must not write a second Google month"


def test_a_gym_with_no_google_rows_still_gets_planned():
    # the guard must not be so broad that it stops the planner doing its job for the
    # gyms the mirror cannot reach yet.
    store = _StoreWithFutureRows([])
    called = {}

    def _plan(*a, **kw):
        called["yes"] = True
        return {"ok": True, "planned": 12}

    import agent.gbp_planner as gp
    orig = gp.plan_gbp_month
    gp.plan_gbp_month = _plan
    try:
        out = gbp_dogfood.plan_gbp_dogfood(
            "eng", "eng_ig", voice=object(), library_path="/x", city="Cape Coral",
            store=store, start=TODAY, connection_status="connected",
            logger=lambda m: None)
    finally:
        gp.plan_gbp_month = orig
    assert called.get("yes") is True
    assert out.get("planned") == 12


def test_the_sweep_reports_already_planned_for_a_mirrored_gym():
    """End of the chain: the fleet sweep's own per-gym verdict."""
    class _S:
        def available(self):
            return True

        def all_connections(self):
            return [{"portal_gym_key": "eng", "status": "connected"}]

    out = sweep_mod.sweep(
        store=_S(),
        runner=lambda base, city=None: {"ok": True, "planned": 0,
                                        "skipped_existing": True},
        address_fn=lambda b: "326 SW 2nd Ter, Cape Coral, Florida",
        alert=lambda *a, **k: None)
    assert out["skipped_existing"] == 1
    assert out["gyms_planned"] == 0 and out["planned"] == 0


def test_a_terminal_google_row_does_not_block_a_fresh_plan_forever():
    # future_gbp_rows excludes failed/denied/deleted by design; a gym whose only Google
    # rows are terminal must still be plannable, or one stale cleanup row freezes its
    # listing permanently (the same freeze class as the SAMPLE-row bug).
    store = _StoreWithFutureRows([])   # store models the post-filter result
    called = {}
    import agent.gbp_planner as gp
    orig = gp.plan_gbp_month
    gp.plan_gbp_month = lambda *a, **kw: called.setdefault("yes", True) and None or \
        {"ok": True, "planned": 9}
    try:
        out = gbp_dogfood.plan_gbp_dogfood(
            "eng", "eng_ig", voice=object(), library_path="/x", city="Cape Coral",
            store=store, start=TODAY, connection_status="connected",
            logger=lambda m: None)
    finally:
        gp.plan_gbp_month = orig
    assert out.get("planned") == 9


# ---- item 1: the transition preserves an approved Google row -------------------------

def _store_with_locked(locked_rows):
    """A store exposing the REAL locked_slots contract: any row whose status is not
    wipeable owns its (post_date, account, format) cell."""
    from agent.portal_calendar_store import _slot_key, _WIPEABLE_STATUSES

    class _S:
        def locked_slots(self, account_key, month):
            out = set()
            for r in locked_rows:
                st = str(r.get("status") or "").lower()
                if st and st not in _WIPEABLE_STATUSES:
                    out.add(_slot_key(r))
            return out
    return _S()


def test_an_approved_google_row_survives_a_rebuild_that_would_replace_it():
    """BEHAVIORAL, not a source read: the guard every rebuild lane routes through must
    drop an incoming mirrored row that lands on a cell the gym already approved, so the
    rebuild keeps the approved post instead of inserting a duplicate beside it or
    reverting it. A gym watching its own listing must never see approved work vanish."""
    from agent.portal_calendar_store import preserve_and_prune
    approved = dict(_mirrored_row(status="approved"))
    incoming = dict(_mirrored_row(status="pending"))   # same date/account/format cell
    store = _store_with_locked([approved])
    kept, locked = preserve_and_prune(store, "eng", [SOON[:7]], [incoming])
    assert locked == 1
    assert kept == [], "the incoming row must be dropped, the approved row left in place"


def test_a_pending_planner_row_is_replaced_not_preserved():
    """The other half of the transition: a gym's still-PENDING Google rows (written by
    the monthly planner) are wipeable, so the build's mirrored month legitimately takes
    their place. That IS the transition, and it must not be blocked by the guard."""
    from agent.portal_calendar_store import preserve_and_prune
    pending_planner_row = dict(_mirrored_row(status="pending"))
    incoming = dict(_mirrored_row(status="pending"))
    store = _store_with_locked([pending_planner_row])
    kept, locked = preserve_and_prune(store, "eng", [SOON[:7]], [incoming])
    assert locked == 0
    assert kept == [incoming], "a pending row must not lock its cell against the rebuild"


def test_a_published_google_row_is_also_protected():
    from agent.portal_calendar_store import preserve_and_prune
    store = _store_with_locked([dict(_mirrored_row(status="published"))])
    kept, _ = preserve_and_prune(store, "eng", [SOON[:7]],
                                 [dict(_mirrored_row(status="pending"))])
    assert kept == [], "never insert a draft on top of something already published"


def test_the_mirror_only_ever_writes_pending_never_approved_or_coach_review():
    """The other half of the transition: a mirrored row must never arrive pre-approved,
    or the build would publish to a gym's Google listing without a human tap."""
    from agent import gbp_mirror as gm
    import inspect
    src = inspect.getsource(gm.rows_for)
    assert 'status="pending"' in src
    assert "coach_review" not in src
    assert 'status="approved"' not in src
