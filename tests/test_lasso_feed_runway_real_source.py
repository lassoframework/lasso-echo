"""Regression: real approved-source path for the owned FB page.

Production evidence (2026-10-08 stock-30-day runway run): the planner built a
genuine source-only draft for facebook 2026-10-27 slot 1 with real approved
fragments, but real_month_planner.to_calendar_rows stamped the row with the
provider platform id "facebook_page" while the runway filtered for the
canonical calendar account "facebook" — zero rows matched and the lane refused
with "approved source unavailable". These tests drive the REAL plan_and_build
and the REAL to_calendar_rows offline (approved local source docs only; no
credentials, no network, no mutation) to prove the seam now resolves.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import uuid

import pytest
from agent import build_lock, config, caption_ledger
from agent.jobs import lasso_feed_runway as job

NOW = datetime(2026, 10, 27, 6, tzinfo=ZoneInfo("America/New_York"))
DAY = "2026-10-27"


def feed(account="facebook", slot=0, fmt="feed", **kw):
    return dict(id=str(uuid.uuid4()), gym_id="lasso", account=account, format=fmt,
                post_date=DAY, slot_index=slot, variant_status="active", status="pending",
                logical_post_id=str(uuid.uuid4()), caption="Approved original", **kw)


class Store:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.staged = []
    def gym_autonomy(self, gym):
        assert gym == "lasso"
        return True
    def rows_in_range_complete(self, gym, first, last, **kw):
        assert gym == "lasso" and kw == {"all_statuses": True}
        return deepcopy(self.rows)
    def stage_lasso_runway_feed(self, row, **kw):
        self.staged.append(deepcopy(row))
        self.rows.append(deepcopy(row))
        return {"result": "inserted", "id": row["id"]}
    def get_row(self, gym, row_id):
        return deepcopy(next(r for r in self.rows if r["id"] == row_id))


class Artifacts:
    available = True
    def claim(self, *args):
        return True
    def release(self, *args):
        pass


@pytest.fixture
def armed_real_source(monkeypatch):
    # Same production flags; the source readers stay REAL (offline approved docs).
    for env, val in {
        "AGENT_LASSO_FEED_RUNWAY": "true",
        "AGENT_REAL_MONTH_PLAN": "true",
        "AGENT_LASSO_3X_ENABLED": "true",
        "AGENT_LASSO_EDITORIAL_CALENDAR": "true",
        "AGENT_LOGICAL_POST_ID": "true",
        "AGENT_CONTENT_BRAIN_ENABLED": "true",
        "AGENT_NANO_ENABLED": "true",
        "AGENT_HOSTING_ENABLED": "true",
    }.items():
        monkeypatch.setenv(env, val)
    monkeypatch.setattr(config, "lasso_infographic_quality_enabled", lambda _: True)
    from agent import variant_regen, cadence
    monkeypatch.setattr(variant_regen, "enabled", lambda: True)
    monkeypatch.setattr(cadence, "resolve_posts_per_day_live", lambda _: 3)
    monkeypatch.setattr(build_lock, "acquire", lambda *a, **k: True)
    monkeypatch.setattr(build_lock, "release", lambda *a, **k: None)
    monkeypatch.setattr(build_lock, "start_heartbeat", lambda *a, **k: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(caption_ledger, "is_blocked_strict", lambda *a: False)
    monkeypatch.setattr(caption_ledger, "record_staged_strict", lambda *a: None)
    monkeypatch.setattr(caption_ledger, "is_blocked", lambda *a: False)
    monkeypatch.setattr(job, "_grade", lambda *a: True)
    monkeypatch.setattr(job, "_source_snapshot", lambda: {"approved": "fixed"})
    monkeypatch.setattr(job.repair, "_reviewed_artifact_record", lambda *a:
                        {"image_url": "https://cdn.example/reviewed.png"})


def test_real_facebook_page_source_resolves_canonical_facebook_row(armed_real_source):
    # facebook slot 0 already staged; slot 1 is the first missing, exactly the
    # production target (facebook 2026-10-27 slot 1).
    original = [feed("facebook", 0)]
    store = Store(original)
    out = job.run(account_key="lasso_fb", now=NOW, store=store,
                  artifact_store=Artifacts(), horizon_days=2)
    assert out["ok"] and out["inserted"] == 1, out["reason"]
    assert out["target"] == ["facebook", DAY, 1]
    assert store.rows[:-1] == original and len(store.staged) == 1
    staged = store.staged[0]
    assert staged["account"] == "facebook"  # canonical, never the provider alias
    assert staged["post_date"] == DAY and staged["slot_index"] == 1
    assert staged["format"] == "feed" and staged["caption"].strip()


def test_dark_studio_still_refuses_without_loosening_fragment_requirement(
        armed_real_source, monkeypatch):
    monkeypatch.setenv("AGENT_CONTENT_BRAIN_ENABLED", "false")
    store = Store([feed("facebook", 0)])
    out = job.run(account_key="lasso_fb", now=NOW, store=store,
                  artifact_store=Artifacts(), horizon_days=2)
    assert out["reason"] == "approved source unavailable" and not store.staged


def test_real_artifact_lease_uses_uuid_owner_for_claim_and_release(armed_real_source):
    # The production RPC's p_owner is UUID, while the build lock accepts a
    # labelled string. Enforce the real RPC type boundary at both lease calls.
    calls = []
    class TypedArtifacts(Artifacts):
        def claim(self, tenant, key, owner):
            assert str(uuid.UUID(owner)) == owner
            calls.append(("claim", tenant, key, owner))
            return True
        def release(self, tenant, key, owner):
            assert str(uuid.UUID(owner)) == owner
            calls.append(("release", tenant, key, owner))
    store = Store([feed("facebook", 0)])
    result = job.run(account_key="lasso_fb", now=NOW, store=store,
                     artifact_store=TypedArtifacts(), horizon_days=2)
    assert result["inserted"] == 1
    assert len(calls) == 2
    assert calls[0][0] == "claim" and calls[1][0] == "release"
    assert calls[0][1:] == calls[1][1:]


@pytest.mark.parametrize("shift, accepted", [(0, True), (60, False)])
def test_postgrest_utc_readback_preserves_exact_scheduled_instant(armed_real_source, shift, accepted):
    class PostgrestStore(Store):
        def get_row(self, gym, row_id):
            row = super().get_row(gym, row_id)
            when = datetime.fromisoformat(row["scheduled_at"]).astimezone(timezone.utc)
            row["scheduled_at"] = (when + timedelta(seconds=shift)).isoformat()
            return row
    store = PostgrestStore([feed("facebook", 0)])
    result = job.run(account_key="lasso_fb", now=NOW, store=store,
                     artifact_store=Artifacts(), horizon_days=2)
    assert result["ok"] is accepted
    assert len(store.staged) == 1
    if not accepted:
        assert result["reason"] == "insert readback mismatch"


@pytest.mark.parametrize("saved", [None, "not-a-time", "2026-10-27T18:30:00"])
def test_missing_invalid_or_naive_readback_is_not_certified(saved):
    assert not job._same_scheduled_instant(saved, "2026-10-27T18:30:00-04:00")
