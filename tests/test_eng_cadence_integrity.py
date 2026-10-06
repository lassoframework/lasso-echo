"""Regression coverage for ENG ticket 4c3e16b6 (two posts/day thinning)."""

from datetime import date

from agent import client_media_sync as cms
from agent import client_month_run as cmr
from agent import portal_calendar_store as pcs


def _companion_rows(day, slot, caption, logical_post_id=None):
    image = f"https://cdn.example/{day}-{slot}.jpg"
    common = {
        "gym_id": "eng",
        "post_date": day,
        "slot_index": slot,
        "caption": caption,
        "image_url": image,
        "status": "pending",
        "time_slot": "morning" if slot == 0 else "evening",
    }
    if logical_post_id:
        common["logical_post_id"] = logical_post_id
    return [
        {**common, "account": "instagram", "format": "feed"},
        {**common, "account": "facebook", "format": "feed"},
        {**common, "account": "instagram", "format": "story"},
    ]


def test_stage_belt_never_leaves_orphan_stories_on_eng_short_days(monkeypatch):
    """Oct 19/28/29 lost slot-1 feeds but retained their paired IG Stories."""
    monkeypatch.setattr(pcs.config, "empty_caption_guard_enabled", lambda: True)
    monkeypatch.setattr(pcs.config, "caption_cooldown_enabled", lambda: True)

    from agent import caption_ledger

    monkeypatch.setattr(
        caption_ledger,
        "is_verbatim_blocked",
        lambda gym, caption, planned: caption == "duplicate caption",
    )
    payload = []
    for day in ("2026-10-19", "2026-10-28", "2026-10-29"):
        payload += _companion_rows(day, 0, f"fresh caption {day}")
        payload += _companion_rows(day, 1, "duplicate caption")

    kept = pcs._stage_belts("eng", payload)

    assert len(kept) == 9
    assert all(row["slot_index"] == 0 for row in kept)
    for day in ("2026-10-19", "2026-10-28", "2026-10-29"):
        assert sum(row["format"] == "feed" and row["account"] == "instagram"
                   for row in kept if row["post_date"] == day) == 1
        assert sum(row["format"] == "story"
                   for row in kept if row["post_date"] == day) == 1


def test_stage_belt_drops_paired_story_with_client_edited_caption(monkeypatch):
    """Logical identity, not matching copy, binds an edited Story to its feed."""
    monkeypatch.setattr(pcs.config, "empty_caption_guard_enabled", lambda: True)
    monkeypatch.setattr(pcs.config, "caption_cooldown_enabled", lambda: True)

    from agent import caption_ledger

    monkeypatch.setattr(
        caption_ledger,
        "is_verbatim_blocked",
        lambda gym, caption, planned: caption == "duplicate feed caption",
    )
    logical_post_id = "2eef7b05-c6fa-49bd-a9ec-0ca6a651892e"
    payload = _companion_rows(
        "2026-10-19", 1, "duplicate feed caption", logical_post_id)
    payload[-1]["caption"] = "Client edited Story caption"

    assert pcs._stage_belts("eng", payload) == []


def test_companion_admission_keeps_ig_and_story_when_only_fb_mirror_is_deduped(
        monkeypatch):
    """An independent FB refusal cannot erase the valid IG cadence unit."""
    monkeypatch.setattr(pcs.config, "empty_caption_guard_enabled", lambda: True)
    monkeypatch.setattr(pcs.config, "caption_cooldown_enabled", lambda: True)
    from agent import caption_ledger
    monkeypatch.setattr(
        caption_ledger, "is_verbatim_blocked",
        lambda gym, caption, planned: caption == "raw duplicate caption")

    logical_post_id = "d4e77854-f03c-47dd-a1b0-88617d8bc5e8"
    payload = _companion_rows(
        "2026-10-19", 1, "raw duplicate caption", logical_post_id)
    payload[0]["caption"] = "raw duplicate caption\n\n#eng #fitness"
    filtered = pcs._stage_belts("eng", payload)

    assert [(row["account"], row["format"]) for row in filtered] == [
        ("instagram", "feed"), ("instagram", "story")]
    assert pcs._drop_companions_missing_instagram_feed(payload, filtered) == filtered


def test_companion_admission_drops_group_when_instagram_feed_is_blocked(monkeypatch):
    monkeypatch.setattr(pcs.config, "empty_caption_guard_enabled", lambda: True)
    monkeypatch.setattr(pcs.config, "caption_cooldown_enabled", lambda: True)
    from agent import caption_ledger
    monkeypatch.setattr(
        caption_ledger, "is_verbatim_blocked",
        lambda gym, caption, planned: caption == "blocked on instagram")

    payload = _companion_rows(
        "2026-10-19", 1, "blocked on instagram",
        "50d256db-8dcf-4dbb-81a3-7e564c0843fd")
    payload[1]["caption"] = "Facebook copy remains independently valid"

    assert pcs._stage_belts("eng", payload) == []


def test_companion_admission_drops_feed_and_mirror_when_required_story_is_missing():
    payload = _companion_rows(
        "2026-10-19", 1, "valid caption",
        "eedaa246-e8f0-4b5b-92c3-424729b43668")
    filtered = [row for row in payload if row["format"] != "story"]

    assert pcs._drop_companions_missing_instagram_feed(payload, filtered) == []


def test_companion_admission_accepts_story_recovered_in_place():
    payload = _companion_rows(
        "2026-10-19", 1, "valid caption",
        "f119b80e-57bf-4907-92b7-f615944d3a2a")
    filtered = [row for row in payload if row["format"] != "story"]
    recovered_story = [row for row in payload if row["format"] == "story"]

    assert pcs._drop_companions_missing_instagram_feed(
        payload, filtered, satisfied=recovered_story) == filtered


def test_human_owned_story_locks_the_day_against_feed_rebuild():
    class _Store:
        def list_month(self, base_key, month):
            return [{
                "post_date": "2026-10-19", "account": "instagram",
                "format": "story", "status": "approved",
                "image_url": "https://cdn.example/approved-story.jpg",
            }]

    locked, _used = cmr._locked_calendar_state(
        "eng", date(2026, 10, 17), 15, _Store(), lambda _m: None)

    assert "2026-10-19" in locked


class _CalendarStore:
    def __init__(self, rows=(), ppd=2):
        self.rows = list(rows)
        self.ppd = ppd
        self.inserted = []
        self.deleted = []

    def list_month(self, base_key, month):
        return [r for r in self.rows if str(r.get("post_date", "")).startswith(month)]

    def gym_posts_per_day(self, base_key):
        return self.ppd

    def delete_month(self, base_key, month, *, preserve_human=True,
                     preserve_dates=()):
        self.deleted.append((base_key, month))
        return 0

    def insert_rows(self, base_key, rows, **kwargs):
        self.inserted.extend(rows)
        self.rows.extend(rows)
        return list(rows)


def test_existing_feed_count_counts_two_slots_not_one_date():
    rows = []
    for day in ("2026-10-19", "2026-10-28", "2026-10-29"):
        rows += _companion_rows(day, 0, f"a {day}")
        rows += _companion_rows(day, 1, f"b {day}")
    store = _CalendarStore(rows)

    count, ok = cms._existing_feed_count(store, "eng", date(2026, 10, 17), 15)

    assert ok is True
    assert count == 6


def test_apply_fails_closed_when_october_target_lands_27_of_30(monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    rows = []
    for day_num in range(17, 32):
        day = f"2026-10-{day_num:02d}"
        for slot in (0, 1):
            rows.append(_companion_rows(day, slot, f"caption {day} {slot}")[0])

    class _DropEngSecondSlots(_CalendarStore):
        @staticmethod
        def _filtered(incoming):
            short_days = {"2026-10-19", "2026-10-28", "2026-10-29"}
            return [row for row in incoming
                    if not (row["post_date"] in short_days
                            and row.get("slot_index") == 1)]

        def preflight_cadence_rows(self, base_key, incoming):
            return self._filtered(incoming)

        def insert_rows(self, base_key, incoming, **kwargs):
            kept = self._filtered(incoming)
            self.inserted.extend(kept)
            self.rows.extend(kept)
            return kept

    store = _DropEngSecondSlots()
    result = cmr._apply(
        "eng", rows, date(2026, 10, 17), 15, store, lambda _msg: None,
        allow_reshape=True,
    )

    assert result["ok"] is False
    assert result["reason"] == "incomplete cadence preflight"
    assert result["expected_feed_slots"] == 30
    assert result["admitted_feed_slots"] == 27
    assert result["inserted_feed_slots"] == 0
    assert result["incomplete_cadence"] is True
    assert store.deleted == []
    assert store.inserted == []


def test_apply_one_x_tolerates_ordinary_stage_drop(monkeypatch):
    """A 1x gym stays tolerant even while the global 2x rollout is armed."""
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")

    class _OneXDropStore(_CalendarStore):
        def __init__(self):
            super().__init__(ppd=1)
            self.required = "unset"
            self.prevalidated = "unset"

        def preflight_cadence_rows(self, base_key, incoming, *, replace_dates=()):
            raise AssertionError("1x builds must not enter strict cadence preflight")

        def insert_rows(self, base_key, incoming, *, required_feed_slots=None,
                        prevalidated_cadence=False):
            self.required = required_feed_slots
            self.prevalidated = prevalidated_cadence
            kept = list(incoming[:1])
            self.inserted.extend(kept)
            return kept

    rows = [
        _companion_rows("2026-10-19", 0, "first caption")[0],
        _companion_rows("2026-10-20", 0, "second caption")[0],
    ]
    store = _OneXDropStore()

    result = cmr._apply(
        "eng", rows, date(2026, 10, 19), 2, store, lambda _msg: None,
        allow_reshape=True)

    assert result["ok"] is True
    assert result["inserted"] == 1
    assert store.required is None
    assert store.prevalidated is False


def test_apply_two_x_keeps_required_slot_check_strict(monkeypatch):
    monkeypatch.setenv("ECHO_CADENCE_2X_ENABLED", "true")
    from agent import cadence
    monkeypatch.setattr(cadence, "resolve_posts_per_day", lambda *a, **kw: 2)

    class _TwoXDropStore(_CalendarStore):
        def __init__(self):
            super().__init__()
            self.required = None

        def preflight_cadence_rows(self, base_key, incoming, *, replace_dates=()):
            return list(incoming)

        def insert_rows(self, base_key, incoming, *, required_feed_slots=None,
                        prevalidated_cadence=False):
            self.required = set(required_feed_slots or ())
            return [row for row in incoming if row.get("slot_index") == 0]

    rows = (_companion_rows("2026-10-19", 0, "morning")
            + _companion_rows("2026-10-19", 1, "evening"))
    store = _TwoXDropStore()

    result = cmr._apply(
        "eng", rows, date(2026, 10, 19), 1, store, lambda _msg: None,
        allow_reshape=True)

    assert store.required == {("2026-10-19", 0), ("2026-10-19", 1)}
    assert result["ok"] is False
    assert result["reason"] == "incomplete cadence staging"


def test_apply_two_x_fails_closed_when_locked_slot_read_fails(monkeypatch):
    from agent import cadence
    monkeypatch.setattr(cadence, "resolve_posts_per_day", lambda *a, **kw: 2)

    class _UnreadableLockedStore(_CalendarStore):
        def list_month(self, base_key, month):
            raise RuntimeError("locked rows unavailable")

    store = _UnreadableLockedStore()
    rows = _companion_rows("2026-10-20", 0, "new caption")

    result = cmr._apply(
        "eng", rows, date(2026, 10, 19), 2, store, lambda _msg: None,
        locked_days={"2026-10-19"}, allow_reshape=True)

    assert result["ok"] is False
    assert result["reason"] == "locked feed slot count unavailable"
    assert store.deleted == []
    assert store.inserted == []


def test_apply_restores_deleted_book_when_live_cadence_gate_changes(monkeypatch):
    from agent.portal_calendar_store import CadencePreconditionError

    old = {
        **_companion_rows("2026-10-19", 0, "old caption")[0],
        "id": "bcad0782-ce47-48e4-9e56-2881341a0e86",
        "variant_status": "active",
        "media_not_ready_reason": None,
    }

    class _RaceStore(_CalendarStore):
        def __init__(self):
            super().__init__([old])
            self.restored = []

        def preflight_cadence_rows(self, base_key, incoming, *, replace_dates=()):
            return list(incoming)

        def delete_month(self, base_key, month, *, preserve_human=True,
                         preserve_dates=(), return_rows=False):
            self.deleted.append((base_key, month))
            self.rows = []
            return [dict(old)] if return_rows else 1

        def insert_rows(self, base_key, incoming, **kwargs):
            raise CadencePreconditionError(409, "changed after preflight")

        def restore_deleted_rows(self, base_key, rows):
            self.restored = list(rows)
            self.rows = list(rows)
            return list(rows)

    store = _RaceStore()
    new = _companion_rows("2026-10-19", 0, "new caption")[0]
    new["_served_reservation_id"] = 42
    result = cmr._apply(
        "eng", [new], date(2026, 10, 19), 1, store, lambda _msg: None,
        allow_reshape=True)

    assert result["ok"] is False
    assert result["rollback_restored"] is True
    assert result["insert_outcome_unknown"] is False
    assert result["retained_reservation_ids"] == []
    assert store.rows == [old]


def test_apply_restores_first_month_when_second_month_delete_fails():
    old = {
        **_companion_rows("2026-10-31", 0, "old caption")[0],
        "id": "24860419-e96e-40bd-9c83-2b4c8bfd032e",
        "variant_status": "active",
        "media_not_ready_reason": None,
    }

    class _TwoMonthStore(_CalendarStore):
        def __init__(self):
            super().__init__([old])
            self.restored = []

        def delete_month(self, base_key, month, *, preserve_human=True,
                         preserve_dates=(), return_rows=False):
            if month == "2026-11":
                raise RuntimeError("november unavailable")
            self.rows = []
            return [dict(old)] if return_rows else 1

        def restore_deleted_rows(self, base_key, rows):
            self.restored = list(rows)
            self.rows = list(rows)
            return list(rows)

    store = _TwoMonthStore()
    proposals = [
        _companion_rows("2026-10-31", 0, "new oct")[0],
        _companion_rows("2026-11-01", 0, "new nov")[0],
    ]
    result = cmr._apply(
        "eng", proposals, date(2026, 10, 31), 2, store, lambda _msg: None,
        allow_reshape=True)

    assert result["ok"] is False
    assert result["rollback_restored"] is True
    assert result["rollback_failed"] is False
    assert store.rows == [old]


def test_apply_reports_failed_restore_after_partial_delete():
    old = {
        **_companion_rows("2026-10-31", 0, "old caption")[0],
        "id": "d65b31b0-846c-4eee-b889-994437171dfa",
    }

    class _BrokenRestore(_CalendarStore):
        def delete_month(self, base_key, month, *, preserve_human=True,
                         preserve_dates=(), return_rows=False):
            if month == "2026-11":
                raise RuntimeError("november unavailable")
            return [dict(old)] if return_rows else 1

        def restore_deleted_rows(self, base_key, rows):
            raise RuntimeError("restore unavailable")

    store = _BrokenRestore([old])
    proposals = [
        _companion_rows("2026-10-31", 0, "new oct")[0],
        _companion_rows("2026-11-01", 0, "new nov")[0],
    ]
    result = cmr._apply(
        "eng", proposals, date(2026, 10, 31), 2, store, lambda _msg: None,
        allow_reshape=True)

    assert result["ok"] is False
    assert result["rollback_restored"] is False
    assert result["rollback_failed"] is True
