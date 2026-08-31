"""
event_calendar insertion / re-grade / overlap / edit / cancel, all offline.

Covers EVENT_CAMPAIGNS_BUILD.md §5:
  * insertion re-grades through the A-gate; doctrine displaced FIRST, proof/offer never
  * two overlapping events respect the offer category cap (second arc thins)
  * date edit re-times the arc without killing approved unaffected rows
  * cancel -> pending arc rows flip denied with reason; ended likewise
  * every staged arc row lands pending; recap held until media
"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import event_calendar as ec
from agent import gym_event as ge


def _event(**over):
    base = dict(
        id="evt_baf_x", gym_id="pete", name="Bring a Friend Week",
        type="bring_a_friend", starts_on="2026-09-22", ends_on="2026-09-28",
        tz="America/New_York",
        offer_text="Your partner trains free all week",
        link="https://petegym.com/baf", brief="Who are you bringing?",
        media_ids=("m1",), status="scheduled",
    )
    base.update(over)
    return ge.GymEvent.from_row(base)


def _arc_rows(ev, today=date(2026, 9, 1)):
    return ge.draft_arc(ev, ge.plan_arc(ev, today=today))


def _doctrine_row(d, status="pending", account="instagram"):
    return {"id": f"doc_{d}", "gym_id": "pete", "post_date": d, "pillar": "doctrine",
            "format": "feed", "account": account, "caption": f"House pillar {d} teaching about consistency and showing up.", "status": status}


def _offer_row(d, status="approved", event_id="evt_prior"):
    return {"id": f"off_{d}", "gym_id": "pete", "post_date": d, "pillar": "offer",
            "format": "feed", "account": "instagram", "event_id": event_id,
            "caption": f"A real live offer on {d}, first class free this month.", "status": status}


# ---- merge: displace doctrine first, never proof/offer -------------------------

def test_merge_displaces_doctrine_on_arc_date():
    ev = _event()
    arc = _arc_rows(ev)
    announce_date = next(r["post_date"] for r in arc if r["arc_kind"] == ge.ANNOUNCE)
    existing = [_doctrine_row(announce_date)]
    merged = ec.merge_arc(existing, arc)
    # the doctrine row on the announce date is displaced (gone).
    assert not any(r.get("id") == f"doc_{announce_date}" for r in merged)
    # the arc announce row is present.
    assert any(r.get("arc_kind") == ge.ANNOUNCE for r in merged)


def test_merge_never_displaces_offer():
    ev = _event()
    arc = _arc_rows(ev)
    announce_date = next(r["post_date"] for r in arc if r["arc_kind"] == ge.ANNOUNCE)
    existing = [_offer_row(announce_date, status="approved")]
    merged = ec.merge_arc(existing, arc)
    # the protected offer row survives; the arc row is ADDED alongside.
    assert any(r.get("id") == f"off_{announce_date}" for r in merged)


def test_merge_never_displaces_approved_doctrine():
    ev = _event()
    arc = _arc_rows(ev)
    d = next(r["post_date"] for r in arc if r["arc_kind"] == ge.ANNOUNCE)
    existing = [_doctrine_row(d, status="approved")]  # human owns it
    merged = ec.merge_arc(existing, arc)
    assert any(r.get("id") == f"doc_{d}" for r in merged)


# ---- overlap cap: second arc thins ---------------------------------------------

def test_overlap_second_arc_thins_under_ceiling():
    ev = _event()
    arc = _arc_rows(ev)
    # A first GENUINELY OVERLAPPING event already put many OFFER posts on the SAME DAYS
    # this arc wants, already at/over the 25% ceiling. The second arc must thin, not
    # flood. (Built from the arc's own dates: since 2026-08-30 overlap_thin only counts
    # prior offer rows that actually INTERSECT the new arc's window, because counting
    # every offer row in the month bucket thinned two events that never ran together.)
    _arc_days = sorted({str(r["post_date"])[:10] for r in arc})
    existing = [_offer_row(d, status="pending") for d in _arc_days]
    kept = ec.overlap_thin(existing, arc)
    assert len(kept) < len(arc)
    # keeps the spine (announce/final/recap present when heavily thinned).
    kinds = {r["arc_kind"] for r in kept}
    assert ge.ANNOUNCE in kinds or ge.RECAP in kinds


def test_overlap_single_event_empty_calendar_not_thinned():
    # A lone event on an empty calendar is NEVER thinned (no overlap).
    ev = _event()
    arc = _arc_rows(ev)
    kept = ec.overlap_thin([], arc)
    assert len(kept) == len(arc)


def test_overlap_no_thin_when_room():
    ev = _event()
    arc = _arc_rows(ev)
    existing = [_doctrine_row(f"2026-09-{d:02d}") for d in range(1, 25)]
    kept = ec.overlap_thin(existing, arc)
    assert len(kept) == len(arc)   # plenty of room (doctrine, not offer), nothing thinned


# ---- re-grade to A after insertion ---------------------------------------------

def test_insertion_regrades_to_A():
    ev = _event()
    arc = _arc_rows(ev)
    # a healthy, varied, A-grade month: distinct rich captions, each with an ask,
    # audience-framed, no category over 25%, no gaps.
    existing = []
    cats = ["doctrine", "education", "community", "faces", "results", "proof"]
    hooks = ["Consistency beats intensity", "The one myth about weight loss",
             "Our members showed up again", "Meet a real member this week",
             "A real result from a busy mom", "A verified testimonial from a coach"]
    for i in range(30):   # full month span, no artificial gap around the recap (T+2)
        d = f"2026-09-{i+1:02d}"
        c = cats[i % len(cats)]
        # short hook (first line < 125 chars) + rich body + booking ask -> A-grade.
        existing.append({"id": f"r{i}", "gym_id": "pete", "post_date": d,
                         "pillar": c, "format": "feed", "account": "instagram",
                         "media_kind": "photo", "vision_derived": True,
                         "caption": (f"{hooks[i % len(hooks)]}, take {i}.\n\n"
                                     "A genuinely distinct and rich body for busy "
                                     "professionals who want to start training and get "
                                     "real results without the gym intimidation.\n\n"
                                     "Book your free intro today and get started."),
                         "status": "pending"})
    # baseline must already be A before we can assert insertion keeps it A.
    _, base = ec.regrade(list(existing), profile="GYM")
    assert base.total >= 90, ("baseline not A", base.total, base.defects[:4])
    merged = ec.merge_arc(existing, arc)
    _, grade = ec.regrade(merged, profile="GYM")
    assert grade.total >= 90, (grade.total, grade.letter, grade.defects[:4])


# ---- date edit re-times without killing approved -------------------------------

def test_date_edit_retimes_without_killing_approved():
    # lead_days=7 pins the historic arc shape: this test is about RETIMING, not lead
    # time. The default lead is now 21 (Blake, 2026-08-30), which legitimately places
    # the announce before starts_on and would break the date assertions below.
    ev = _event(lead_days=7)
    old_arc = _arc_rows(ev)
    # approve the announce row (human owns it).
    for r in old_arc:
        if r["arc_kind"] == ge.ANNOUNCE:
            r["status"] = "approved"
    # move the event one week later.
    new_ev = _event(starts_on="2026-09-29", ends_on="2026-10-05", lead_days=7)
    restage, keep, remove = ec.retime_arc(old_arc, new_ev, today=date(2026, 9, 1))
    # the approved old announce row is KEPT (never reverted).
    assert any(r["arc_kind"] == ge.ANNOUNCE and r["status"] == "approved" for r in keep)
    # the pending old rows that moved are marked for removal.
    assert remove
    # the new arc is planned for the new dates.
    assert all(r["post_date"] >= "2026-09-22" for r in restage)
    assert any(r["post_date"] > "2026-09-28" for r in restage)


def test_date_edit_unchanged_approved_row_not_restaged():
    ev = _event()
    old_arc = _arc_rows(ev)
    # approve the recap (T+2). Its date does not change if we only nudge starts_on later
    # but keep ends_on... instead test: same dates edit is a no-op for approved rows.
    for r in old_arc:
        if r["arc_kind"] == ge.RECAP:
            r["status"] = "approved"
    new_ev = _event()  # identical dates
    restage, keep, remove = ec.retime_arc(old_arc, new_ev, today=date(2026, 9, 1))
    # recap date unchanged and approved -> kept, not re-staged.
    assert any(r["arc_kind"] == ge.RECAP for r in keep)
    assert not any(r["arc_kind"] == ge.RECAP for r in restage)


# ---- cancel / ended sweep ------------------------------------------------------

class _FakeStore:
    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.denied = []

    def list_event_rows(self, gym_id, event_id):
        return [dict(r) for r in self.rows.values()
                if r.get("gym_id") == gym_id and r.get("event_id") == event_id]

    def deny_with_reason(self, gym_id, row_id, reason):
        r = self.rows.get(row_id)
        if r and r.get("gym_id") == gym_id:
            r["status"] = "denied"
            r["reject_reason"] = reason
            self.denied.append((row_id, reason))
            return r
        return None


def test_cancel_flips_pending_arc_rows_denied():
    ev = _event()
    arc = _arc_rows(ev)
    for i, r in enumerate(arc):
        r["id"] = f"arc{i}"
        r["event_id"] = ev.id
        r["gym_id"] = "pete"
    # approve one row; it must NOT be denied.
    arc[0]["status"] = "approved"
    store = _FakeStore(arc)
    res = ec.cancel_event(store, "pete", ev.id)
    assert res["reason"] == ec.REJECT_CANCELLED
    # every pending row denied; the approved one left.
    for rid, reason in store.denied:
        assert reason == ec.REJECT_CANCELLED
    assert store.rows["arc0"]["status"] == "approved"
    assert all(store.rows[f"arc{i}"]["status"] == "denied"
               for i in range(1, len(arc)))


def test_ended_uses_event_ended_reason():
    ev = _event()
    arc = _arc_rows(ev)
    for i, r in enumerate(arc):
        r["id"] = f"arc{i}"
        r["event_id"] = ev.id
        r["gym_id"] = "pete"
    store = _FakeStore(arc)
    res = ec.cancel_event(store, "pete", ev.id, ended=True)
    assert res["reason"] == ec.REJECT_ENDED


# ---- stage_arc through a fake store: pending + recap held -----------------------

class _StageStore:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.inserted = []

    def list_month(self, gym_id, month):
        return [r for r in self.existing if str(r.get("post_date"))[:7] == month]

    def insert_rows(self, gym_id, rows):
        for r in rows:
            rr = dict(r)
            rr["gym_id"] = gym_id
            self.inserted.append(rr)
        return self.inserted[-len(rows):]


def test_stage_arc_stages_pending_and_holds_blocked_recap():
    ev = _event(media_ids=())   # no media -> recap blocked
    arc = _arc_rows(ev)
    store = _StageStore()
    res = ec.stage_arc(store, ev, arc, profile="GYM")
    assert res["ok"]
    # every inserted row is pending.
    assert all(r["status"] == "pending" for r in store.inserted)
    # the blocked recap was held out of staging.
    assert not any(r.get("arc_kind") == ge.RECAP for r in store.inserted)
    assert res["held_recap"] >= 1
    # transient keys are stripped from the DB payload.
    assert all("recap_blocked" not in r and "arc_kind" not in r for r in store.inserted)


# ---- an image-less arc row must never be staged ----------------------------------
def test_attach_media_gives_rows_a_photo_and_stamps_the_asset():
    """Event rows used to be staged with NO image_url at all — media_ids existed on
    GymEvent but was never consumed anywhere, so every event post for every gym was
    unpublishable. Each row now gets a real photo from the gym's own pool."""
    from agent import event_calendar as ec
    rows = [{"post_date": "2026-10-03", "account": "instagram", "format": "feed"},
            {"post_date": "2026-10-04", "account": "instagram", "format": "feed"}]
    picked = [{"id": "a1", "title": "one.jpg"}, {"id": "a2", "title": "two.jpg"}]
    kept, held = ec._attach_media(
        "zanshinfitness630e22", rows, lambda m: None,
        picker=lambda exclude: next((a for a in picked if a["id"] not in exclude), None),
        host=lambda asset, gym, drive: f"https://cdn.test/{asset['id']}.jpg")
    assert held == []
    assert [r["image_url"] for r in kept] == ["https://cdn.test/a1.jpg",
                                              "https://cdn.test/a2.jpg"]
    assert [r["source_media_asset_id"] for r in kept] == ["a1", "a2"]


def test_attach_media_holds_a_row_it_cannot_give_a_photo():
    """An Instagram feed post with no image cannot publish, so a row we cannot give a
    photo is HELD OUT of staging rather than staged as a promise that must fail."""
    from agent import event_calendar as ec
    rows = [{"post_date": "2026-10-03", "account": "instagram", "format": "feed"}]
    kept, held = ec._attach_media("zanshinfitness630e22", rows, lambda m: None,
                                  picker=lambda exclude: None,
                                  host=lambda *a: "")
    assert kept == [] and len(held) == 1


def test_attach_media_leaves_rows_that_already_have_an_image():
    from agent import event_calendar as ec
    rows = [{"post_date": "2026-10-03", "image_url": "https://cdn.test/keep.jpg"}]
    kept, held = ec._attach_media("g", rows, lambda m: None,
                                  picker=lambda exclude: None, host=lambda *a: "")
    assert held == [] and kept[0]["image_url"] == "https://cdn.test/keep.jpg"


def test_attach_media_holds_when_hosting_fails():
    from agent import event_calendar as ec
    rows = [{"post_date": "2026-10-03", "account": "instagram", "format": "feed"}]
    kept, held = ec._attach_media("g", rows, lambda m: None,
                                  picker=lambda exclude: {"id": "a1", "title": "x.jpg"},
                                  host=lambda *a: "")
    assert kept == [] and len(held) == 1


# ---- backfill_missing_media: stale image-less rows get a real photo, in place ----
class _BackfillStore:
    """Mimics SupabaseCalendarStore's list_event_rows + patch_media (id+gym_id
    scoped, prefetch-confirms no image before writing) for backfill_missing_media."""
    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.patched = []

    def list_event_rows(self, gym_id, event_id):
        return [dict(r) for r in self.rows.values()
                if r.get("gym_id") == gym_id and r.get("event_id") == event_id]

    def get_row(self, gym_id, row_id):
        r = self.rows.get(row_id)
        if r and r.get("gym_id") == gym_id:
            return dict(r)
        return None

    def patch_media(self, gym_id, row_id, image_url, source_media_asset_id=""):
        current = self.get_row(gym_id, row_id)
        if current is None or (current.get("image_url") or "").strip():
            return None
        r = self.rows[row_id]
        r["image_url"] = image_url
        if source_media_asset_id:
            r["source_media_asset_id"] = source_media_asset_id
        self.patched.append(row_id)
        return dict(r)


def _stale_row(rid, post_date, status, event_id="evt_x", image_url=""):
    return {"id": rid, "gym_id": "zanshinfitness630e22", "event_id": event_id,
            "post_date": post_date, "status": status, "image_url": image_url,
            "caption": "unchanged"}


def test_backfill_missing_media_patches_pending_and_approved_rows():
    """The exact live shape (Pete/Zanshin, 2026-08-31): pending + approved rows
    staged with no image before the media-attach guard existed. Each gets a real
    photo, caption/status/date untouched."""
    from agent import event_calendar as ec
    rows = [
        _stale_row("r1", "2026-10-02", "pending"),
        _stale_row("r2", "2026-09-29", "approved"),
        _stale_row("r3", "2026-09-26", "denied"),   # denied: never touched
    ]
    store = _BackfillStore(rows)
    picked = [{"id": "a1", "title": "one.jpg"}, {"id": "a2", "title": "two.jpg"}]
    res = ec.backfill_missing_media(
        store, "zanshinfitness630e22", "evt_x",
        picker=lambda exclude: next((a for a in picked if a["id"] not in exclude), None),
        host=lambda asset, gym, drive: f"https://cdn.test/{asset['id']}.jpg")
    assert sorted(res["backfilled"]) == ["r1", "r2"]
    assert res["held"] == 0
    assert store.rows["r1"]["image_url"] == "https://cdn.test/a1.jpg"
    assert store.rows["r1"]["status"] == "pending"        # status untouched
    assert store.rows["r1"]["caption"] == "unchanged"      # caption untouched
    assert store.rows["r2"]["image_url"] == "https://cdn.test/a2.jpg"
    assert store.rows["r2"]["status"] == "approved"
    # denied row never entered the candidate set at all.
    assert store.rows["r3"]["image_url"] == ""
    assert "r3" not in store.patched


def test_backfill_missing_media_never_touches_a_row_that_already_has_an_image():
    from agent import event_calendar as ec
    rows = [_stale_row("r1", "2026-10-02", "pending", image_url="https://cdn.test/keep.jpg")]
    store = _BackfillStore(rows)
    res = ec.backfill_missing_media(
        store, "zanshinfitness630e22", "evt_x",
        picker=lambda exclude: {"id": "a1"}, host=lambda *a: "https://cdn.test/a1.jpg")
    assert res["backfilled"] == []
    assert store.rows["r1"]["image_url"] == "https://cdn.test/keep.jpg"


def test_backfill_missing_media_holds_when_pool_empty():
    from agent import event_calendar as ec
    rows = [_stale_row("r1", "2026-10-02", "pending")]
    store = _BackfillStore(rows)
    res = ec.backfill_missing_media(
        store, "zanshinfitness630e22", "evt_x",
        picker=lambda exclude: None, host=lambda *a: "")
    assert res["backfilled"] == []
    assert res["held"] == 1
    assert store.rows["r1"]["image_url"] == ""


def test_backfill_missing_media_scoped_to_the_named_gym_only():
    """A row belonging to another gym is never patched even if it slipped into the
    listing (defense in depth: list_event_rows is already gym-scoped upstream)."""
    from agent import event_calendar as ec
    rows = [_stale_row("r1", "2026-10-02", "pending")]
    rows[0]["gym_id"] = "some_other_gym"
    store = _BackfillStore(rows)
    res = ec.backfill_missing_media(
        store, "zanshinfitness630e22", "evt_x",
        picker=lambda exclude: {"id": "a1"}, host=lambda *a: "https://cdn.test/a1.jpg")
    assert res == {"backfilled": [], "held": 0}   # list_event_rows found nothing for THIS gym


# ---- overlap_thin must only fire on events that ACTUALLY overlap -----------------
def _arc_offer_row(date, event_id, pillar=None):
    from agent import gym_event as ge
    return {"post_date": date, "account": "instagram", "format": "feed",
            "pillar": pillar or ge.ARC_CATEGORY, "event_id": event_id,
            "status": "pending"}


def test_a_non_overlapping_prior_event_never_thins_the_new_arc():
    """LIVE (Pete/Zanshin 2026-08-30): Back to School (Sep 1 to 15) was cut from 10 arc
    posts to 4 because Bring a Friend Week (Oct 3 to 10) had rows in the same MONTH
    read. The two runs never coincide. He saw 'three posts this week then nothing until
    the last day' — 6 DURING posts silently dropped."""
    from agent import event_calendar as ec
    # the prior event's rows sit AFTER the new arc's whole span
    existing = [_arc_offer_row("2026-09-26", "evt_bring_a_friend"),
                _arc_offer_row("2026-09-29", "evt_bring_a_friend")]
    arc = [_arc_offer_row(f"2026-09-{d:02d}", "evt_back_to_school")
           for d in (1, 2, 4, 6, 8, 10, 12, 14, 15)]
    kept = ec.overlap_thin(existing, arc)
    assert len(kept) == len(arc), (
        f"a non-overlapping event thinned the arc: {len(kept)} of {len(arc)} kept")


def test_a_genuinely_overlapping_event_still_thins():
    """The ceiling must still protect the calendar when two events DO run together."""
    from agent import event_calendar as ec
    existing = [_arc_offer_row(f"2026-09-{d:02d}", "evt_other") for d in range(1, 13)]
    arc = [_arc_offer_row(f"2026-09-{d:02d}", "evt_new") for d in range(1, 13)]
    kept = ec.overlap_thin(existing, arc)
    assert len(kept) < len(arc), "a truly overlapping event was not thinned"


# ---- re-staging an arc must top it up, never duplicate it -----------------------
class _ReStageStore:
    def __init__(self, existing):
        self._existing = list(existing)
        self.inserted = []

    def list_month(self, gym_id, month):
        return [r for r in self._existing
                if str(r.get("post_date", ""))[:7] == month]

    def insert_rows(self, gym_id, rows):
        self.inserted.extend(rows)
        return rows


def test_restaging_an_arc_tops_up_instead_of_duplicating(monkeypatch):
    """LIVE (Pete/Zanshin 2026-08-30): re-staging Back to School after the overlap fix
    brought four dates back a SECOND time, three of them where the client had already
    APPROVED the original. stage_arc only ever ADDS, and preserve_and_prune did not
    catch it, so the client would have seen doubled posts."""
    from agent import event_calendar as ec
    ev = _event()
    arc = _arc_rows(ev)
    # half the arc is already on the calendar for THIS event
    half = arc[: len(arc) // 2]
    existing = [dict(r, event_id=ev.id, status="approved") for r in half]
    store = _ReStageStore(existing)
    monkeypatch.setattr(ec, "_attach_media",
                        lambda gym_id, rows, log, picker=None, host=None: (rows, []))
    res = ec.stage_arc(store, ev, arc, profile="GYM")
    staged_days = {(str(r["post_date"])[:10], r.get("account"), r.get("format"))
                   for r in store.inserted}
    already = {(str(r["post_date"])[:10], r.get("account"), r.get("format"))
               for r in existing}
    assert not (staged_days & already), "a day already on the calendar was staged twice"
    assert res["staged"] == len(store.inserted)
    assert store.inserted, "the genuinely new days should still be topped up"
