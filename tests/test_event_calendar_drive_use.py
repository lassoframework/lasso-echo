"""Interim armed event Drive hold: no selection or mutation before integration.

These tests do not certify remote-use integration. The event Drive lane remains
held until prewrite identity, staged-batch writes and recovery are implemented.
"""
import pytest

from agent import event_calendar as ec, gym_event as ge
from agent import gbp_drive_use_journal as journal, gym_media_selector as selector
from agent import remote_drive_use
from tests.gym_media_fakes import FakeMediaStore, make_asset, make_source

GYM = "pete"


def event():
    return ge.GymEvent.from_row(dict(
        id="evt1", gym_id=GYM, name="Bring a Friend Week",
        type="bring_a_friend", starts_on="2026-10-01", ends_on="2026-10-07",
        tz="America/New_York", offer_text="Partner trains free",
        link="https://gym.test/baf", brief="Bring a friend", media_ids=("a1",),
        status="scheduled"))


def row(**over):
    return dict(event_id="evt1", post_date="2026-10-03", account="instagram",
                format="feed", status="pending", **over)


class CalendarStore:
    def __init__(self, rows=()):
        self.rows = [dict(r) for r in rows]
        self.inserted = []
        self.patched = []

    def list_month(self, gym_id, month):
        return []

    def list_event_rows(self, gym_id, event_id):
        return [dict(r) for r in self.rows]

    def insert_rows(self, gym_id, rows):
        landed = [dict(r, gym_id=gym_id, id=f"r{i}") for i, r in enumerate(rows)]
        self.inserted.extend(landed)
        return landed

    def patch_media(self, gym_id, rid, image_url, asset_id, source_media_url=None):
        self.patched.append(rid)
        return dict(id=rid, gym_id=gym_id, image_url=image_url,
                    source_media_asset_id=asset_id)


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_REMOTE_DRIVE_USE_CAS_ENABLED", "true")
    calls = []

    def forbid(label):
        def fail(*args, **kwargs):
            calls.append(label)
            raise AssertionError(f"armed hold called {label}")
        return fail

    # Capture calls even where legacy best-effort handlers swallow exceptions.
    monkeypatch.setattr(selector, "pick_media", forbid("default picker"))
    monkeypatch.setattr(selector, "claim_drive_content", forbid("claim"))
    monkeypatch.setattr(selector, "stamp_use", forbid("stamp"))
    monkeypatch.setattr(ec, "_host_asset", forbid("default host"))
    monkeypatch.setattr(journal, "prepare", forbid("journal"))
    monkeypatch.setattr(remote_drive_use, "apply", forbid("remote use"))
    return calls, forbid


@pytest.mark.parametrize("injected", [True, False])
def test_armed_attach_holds_before_picker_claim_or_host(armed, injected):
    calls, forbid = armed
    missing = row()
    existing = row(image_url="https://cdn.test/non-drive.jpg")
    logs = []
    kwargs = dict(picker=forbid("injected picker"), host=forbid("injected host")) \
        if injected else {}
    kept, held = ec._attach_media(GYM, [missing, existing], logs.append, **kwargs)
    assert kept == [existing] and kept[0] is existing
    assert held == [missing] and held[0] is missing
    assert "image_url" not in missing
    assert calls == []
    assert any("before photo selection" in line for line in logs)


def test_armed_stage_never_calls_insert_even_if_it_could_lose_ack(armed):
    calls, forbid = armed
    store = CalendarStore()
    store.insert_rows = forbid("insert with lost ack")
    result = ec.stage_arc(store, event(), [row()],
                          media_picker=forbid("picker claim"),
                          media_host_fn=forbid("host"))
    assert result["staged"] == 0 and result["held_media"] == 1
    assert calls == []


@pytest.mark.parametrize("logical_id", [None, "existing-logical-id"])
def test_armed_backfill_preserves_legacy_and_current_identity(armed, logical_id):
    calls, forbid = armed
    missing = row(id="r1", gym_id=GYM, image_url="",
                  logical_post_id=logical_id)
    store = CalendarStore([missing])
    store.patch_media = forbid("patch with lost ack")
    result = ec.backfill_missing_media(store, GYM, "evt1",
                                       picker=forbid("picker claim"),
                                       host=forbid("host"))
    assert result == {"backfilled": [], "held": 1}
    assert store.rows == [missing]
    assert calls == []


def test_armed_stage_existing_non_drive_image_keeps_legacy_write(armed):
    calls, forbid = armed
    store = CalendarStore()
    result = ec.stage_arc(store, event(),
                          [row(image_url="https://cdn.test/non-drive.jpg")],
                          media_picker=forbid("picker"), media_host_fn=forbid("host"))
    assert result["staged"] == 1 and result["held_media"] == 0
    assert store.inserted[0]["image_url"] == "https://cdn.test/non-drive.jpg"
    assert calls == []


@pytest.mark.parametrize("operation", ["stage", "backfill"])
def test_flag_off_legacy_selection_host_and_stamp(monkeypatch, operation):
    monkeypatch.setenv("AGENT_REMOTE_DRIVE_USE_CAS_ENABLED", "false")
    source = make_source("src1", gym_id=GYM)
    asset = make_asset("a1", gym_id=GYM, source_id="src1")
    media = FakeMediaStore(sources=[source], assets=[asset])
    monkeypatch.setattr(selector._idx, "default_store", lambda: media)
    calls = []
    monkeypatch.setattr(selector, "stamp_use",
                        lambda *a, **kw: calls.append("legacy stamp"))

    def pick(exclude):
        calls.append("picker")
        return dict(asset)

    def host(*args):
        calls.append("host")
        return "https://cdn.test/a1.jpg"

    store = CalendarStore([row(id="r1", gym_id=GYM, image_url="")])
    if operation == "stage":
        result = ec.stage_arc(store, event(), [row()],
                              media_picker=pick, media_host_fn=host)
        assert result["staged"] == 1 and result["held_media"] == 0
    else:
        result = ec.backfill_missing_media(store, GYM, "evt1", picker=pick, host=host)
        assert result == {"backfilled": ["r1"], "held": 0}
    assert calls == ["picker", "host", "legacy stamp"]


def test_armed_flag_read_error_still_holds(monkeypatch):
    monkeypatch.setenv("AGENT_REMOTE_DRIVE_USE_CAS_ENABLED", "true")
    monkeypatch.setattr(remote_drive_use, "enabled",
                        lambda: (_ for _ in ()).throw(RuntimeError("module unreadable")))
    calls = []
    kept, held = ec._attach_media(
        GYM, [row()], lambda m: None,
        picker=lambda *a: calls.append("picker"), host=lambda *a: calls.append("host"))
    assert kept == [] and len(held) == 1 and calls == []


def test_armed_mixed_stage_writes_only_preexisting_media(armed):
    calls, forbid = armed
    store = CalendarStore()
    missing = row()
    existing = dict(row(image_url="https://cdn.test/existing.jpg"),
                    post_date="2026-10-04")
    result = ec.stage_arc(store, event(), [missing, existing],
                          media_picker=forbid("picker claim"),
                          media_host_fn=forbid("host"))
    assert result["staged"] == 1 and result["held_media"] == 1
    assert [r["post_date"] for r in store.inserted] == ["2026-10-04"]
    assert "image_url" not in missing
    assert calls == []


def test_armed_backfill_existing_media_is_untouched(armed):
    calls, forbid = armed
    existing = row(id="r1", gym_id=GYM, image_url="https://cdn.test/existing.jpg")
    store = CalendarStore([existing])
    store.patch_media = forbid("patch")
    result = ec.backfill_missing_media(store, GYM, "evt1",
                                       picker=forbid("picker"), host=forbid("host"))
    assert result == {"backfilled": [], "held": 0}
    assert store.rows == [existing] and calls == []
