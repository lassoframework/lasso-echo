"""Focused gates for source_media_content_hash (ECHO_SOURCE_MEDIA_CONTENT_HASH_ENABLED).

P1: the shared row mapper emits the column ONLY when the feature is ON, and the
direct delete-then-insert callers (client_month_run._apply,
real_month_planner.apply_month_plan) prove schema readiness BEFORE any delete
when the feature is ON. OFF behavior is byte-identical to before.
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_month_run as cmr          # noqa: E402
from agent import real_month_planner as rmp        # noqa: E402
from agent import config                            # noqa: E402
from agent.drafter import Draft, DraftStatus        # noqa: E402

HASH = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _flag(monkeypatch, value):
    monkeypatch.setattr(config, "source_media_content_hash_enabled",
                        lambda: value, raising=False)


class _Calendar:
    """Minimal delete_month/insert_rows recorder. Schema readiness is configurable;
    stores built with ready=None lack the capability entirely (pre-migration fake)."""

    def __init__(self, ready=None):
        self.deleted = []
        self.inserted = []
        self._ready = ready
        if ready is not None:
            self.source_media_content_hash_schema_ready = lambda: ready

    def delete_month(self, base_key, month, **_kw):
        self.deleted.append((base_key, month))
        return 1

    def insert_rows(self, base_key, rows, **_kw):
        self.inserted.extend(dict(r) for r in rows)
        return [dict(r, id=f"new-{i}") for i, r in enumerate(rows)]


def _row(**over):
    row = {"gym_id": "pierce", "account": "instagram", "post_date": "2026-10-10",
           "pillar": "faces", "format": "feed", "caption": "one",
           "image_url": "https://cdn/a.jpg", "status": "pending"}
    row.update(over)
    return row


def _draft(**over):
    fields = dict(draft_id="gymmedia_p1_2026-10-10", account_key="pierce",
                  platform="instagram", caption="one", hashtags=[],
                  creative_path="a.jpg", creative_public_url="https://cdn/a.jpg",
                  scheduled_for="", status=DraftStatus.PENDING,
                  day_key="2026-10-10", draft_type="gym_media", category="faces")
    fields.update(over)
    d = Draft(**{k: v for k, v in fields.items() if k in Draft.__dataclass_fields__})
    for k, v in over.items():
        if k not in Draft.__dataclass_fields__:
            setattr(d, k, v)
    return d


# ---- client_month_run._apply -------------------------------------------------

def test_client_apply_off_inserts_without_hash_and_needs_no_schema_probe(monkeypatch):
    _flag(monkeypatch, False)
    cal = _Calendar(ready=None)  # no readiness capability at all
    res = cmr._apply("pierce", [_row(source_media_content_hash=HASH)],
                     date(2026, 10, 1), 31, cal, lambda *_a: None)
    assert res["ok"] is True
    assert cal.deleted, "OFF path must keep its existing delete-then-insert behavior"
    assert cal.inserted
    assert all("source_media_content_hash" not in r for r in cal.inserted)


def test_client_apply_on_missing_schema_never_deletes(monkeypatch):
    _flag(monkeypatch, True)
    for ready in (None, False):  # incapable store AND a store proving NOT ready
        cal = _Calendar(ready=ready)
        res = cmr._apply("pierce", [_row(source_media_content_hash=HASH)],
                         date(2026, 10, 1), 31, cal, lambda *_a: None)
        assert res["ok"] is False
        assert "not confirmed ready" in res["reason"]
        assert cal.deleted == [] and cal.inserted == []


def test_client_apply_on_ready_schema_deletes_and_inserts(monkeypatch):
    _flag(monkeypatch, True)
    cal = _Calendar(ready=True)
    res = cmr._apply("pierce", [_row(source_media_content_hash=HASH)],
                     date(2026, 10, 1), 31, cal, lambda *_a: None)
    assert res["ok"] is True
    assert cal.deleted and cal.inserted
    assert cal.inserted[0]["source_media_content_hash"] == HASH


# ---- real_month_planner.apply_month_plan -------------------------------------

def test_planner_apply_off_inserts_without_hash_and_needs_no_schema_probe(monkeypatch):
    _flag(monkeypatch, False)
    cal = _Calendar(ready=None)
    res = rmp.apply_month_plan("pierce", [_draft(source_media_content_hash=HASH)], cal)
    assert res["ok"] is True
    assert cal.deleted, "OFF path must keep its existing delete-then-insert behavior"
    assert cal.inserted
    assert all("source_media_content_hash" not in r for r in cal.inserted)


def test_planner_apply_on_missing_schema_never_deletes(monkeypatch):
    _flag(monkeypatch, True)
    for ready in (None, False):
        cal = _Calendar(ready=ready)
        res = rmp.apply_month_plan("pierce", [_draft(source_media_content_hash=HASH)], cal)
        assert res["ok"] is False
        assert "not confirmed ready" in res["reason"]
        assert cal.deleted == [] and cal.inserted == []


def test_planner_apply_on_ready_schema_deletes_and_inserts_with_hash(monkeypatch):
    _flag(monkeypatch, True)
    cal = _Calendar(ready=True)
    res = rmp.apply_month_plan("pierce", [_draft(source_media_content_hash=HASH)], cal)
    assert res["ok"] is True
    assert cal.deleted and cal.inserted
    assert any(r.get("source_media_content_hash") == HASH for r in cal.inserted)


def test_planner_apply_on_schema_probe_exception_never_deletes(monkeypatch):
    _flag(monkeypatch, True)

    class Exploding(_Calendar):
        def __init__(self):
            super().__init__(ready=None)
            def boom():
                raise RuntimeError("postgrest down")
            self.source_media_content_hash_schema_ready = boom

    cal = Exploding()
    res = rmp.apply_month_plan("pierce", [_draft(source_media_content_hash=HASH)], cal)
    assert res["ok"] is False
    assert cal.deleted == [] and cal.inserted == []
