"""
lasso_astra_rework (agent/lasso_astra_rework.py), fully offline.

Blake 2026-09-11 (+ same-day correction): rework LASSO's whole existing
calendar by regenerating the IMAGE on non-video, non-published slots as
linked Astra v2 candidates — SAME dates/times/count, nothing publishes.
Asserts: gym scoping, video-row exclusion, published/denied exclusion,
already-candidate-status is untouched, dry-run calls nothing, --limit caps a
real run, one bad row never sinks the sweep, and it reuses
variant_regen.generate_variant_image + create_variant_candidate (never a
bespoke path).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent import lasso_astra_rework as lar  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")


def _row(row_id, gym_id="lasso", status="pending", variant_status="active",
        caption="Real caption", image_url="https://cdn/x.jpg", thumbnail_url=None,
        pillar="doctrine"):
    return {"id": row_id, "gym_id": gym_id, "status": status,
            "variant_status": variant_status, "caption": caption,
            "image_url": image_url, "thumbnail_url": thumbnail_url,
            "pillar": pillar, "post_date": "2026-09-15", "format": "feed"}


class _Store:
    def __init__(self, rows_by_month):
        self._rows_by_month = rows_by_month
        self.created = []

    def list_month(self, gym_id, month):
        return [dict(r) for r in self._rows_by_month.get(month, [])
                if r.get("gym_id") == gym_id]

    def create_variant_candidate(self, account_key, anchor_row, image_url, **kw):
        cand = {"id": f"cand-{anchor_row['id']}", "image_url": image_url,
               "variant_status": "candidate"}
        self.created.append((account_key, anchor_row["id"], image_url))
        return cand


def test_find_candidates_excludes_video_rows():
    rows = {"2026-09": [
        _row("a", thumbnail_url="https://cdn/thumb.jpg"),   # video (has thumbnail)
        _row("b", image_url="https://cdn/clip.mp4"),         # video (extension)
        _row("c", image_url="https://cdn/photo.jpg"),        # eligible
    ]}
    store = _Store(rows)
    found = lar.find_candidates(store, gym_id="lasso", months=["2026-09"])
    assert [r["id"] for r in found] == ["c"]


def test_find_candidates_excludes_published_denied_killed():
    rows = {"2026-09": [
        _row("a", status="published"),
        _row("b", status="denied"),
        _row("c", status="killed"),
        _row("d", status="pending"),
    ]}
    store = _Store(rows)
    found = lar.find_candidates(store, gym_id="lasso", months=["2026-09"])
    assert [r["id"] for r in found] == ["d"]


def test_find_candidates_excludes_non_active_variant_status():
    """A slot that already has a pending candidate from an earlier pass (this
    row IS the candidate, variant_status='candidate') is never re-worked."""
    rows = {"2026-09": [_row("a", variant_status="candidate")]}
    store = _Store(rows)
    found = lar.find_candidates(store, gym_id="lasso", months=["2026-09"])
    assert found == []


def test_find_candidates_excludes_other_gyms():
    rows = {"2026-09": [_row("a", gym_id="some_client")]}
    store = _Store(rows)
    found = lar.find_candidates(store, gym_id="lasso", months=["2026-09"])
    assert found == []


def test_find_candidates_excludes_empty_caption():
    rows = {"2026-09": [_row("a", caption="")]}
    store = _Store(rows)
    found = lar.find_candidates(store, gym_id="lasso", months=["2026-09"])
    assert found == []


def test_dry_run_calls_nothing(monkeypatch):
    rows = {"2026-09": [_row("a"), _row("b")]}
    store = _Store(rows)
    from agent import variant_regen as vr
    monkeypatch.setattr(vr, "generate_variant_image",
                        lambda *a, **k: pytest.fail("must not generate on a dry run"))
    result = lar.run(store, gym_id="lasso", months=["2026-09"], write=False)
    assert result["found"] == 2
    assert result["done"] == 0
    assert store.created == []


def test_write_generates_and_lands_candidates_never_touching_the_original(monkeypatch):
    rows = {"2026-09": [_row("a"), _row("b")]}
    store = _Store(rows)

    def fake_regen(row, gym_id):
        return {"ok": True, "image_url": f"https://cdn/v2-{row['id']}.jpg"}
    monkeypatch.setattr("agent.variant_regen.generate_variant_image", fake_regen)

    result = lar.run(store, gym_id="lasso", months=["2026-09"], write=True)
    assert result["done"] == 2
    assert set(store.created) == {
        ("lasso", "a", "https://cdn/v2-a.jpg"),
        ("lasso", "b", "https://cdn/v2-b.jpg"),
    }
    # original rows in the fixture are untouched (the store never patches them)
    assert rows["2026-09"][0]["image_url"] == "https://cdn/x.jpg"


def test_limit_caps_a_real_run(monkeypatch):
    rows = {"2026-09": [_row("a"), _row("b"), _row("c")]}
    store = _Store(rows)
    monkeypatch.setattr("agent.variant_regen.generate_variant_image",
                        lambda row, gym_id: {"ok": True,
                                            "image_url": f"https://cdn/{row['id']}.jpg"})
    result = lar.run(store, gym_id="lasso", months=["2026-09"], limit=1, write=True)
    assert result["done"] == 1
    assert len(store.created) == 1


def test_one_bad_row_never_sinks_the_sweep(monkeypatch):
    rows = {"2026-09": [_row("a"), _row("b")]}
    store = _Store(rows)

    def flaky_regen(row, gym_id):
        if row["id"] == "a":
            raise RuntimeError("astra timeout")
        return {"ok": True, "image_url": "https://cdn/v2-b.jpg"}
    monkeypatch.setattr("agent.variant_regen.generate_variant_image", flaky_regen)

    result = lar.run(store, gym_id="lasso", months=["2026-09"], write=True)
    assert result["done"] == 1
    assert len(result["failed"]) == 1
    assert result["failed"][0]["id"] == "a"
    assert store.created == [("lasso", "b", "https://cdn/v2-b.jpg")]


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "false")
    rows = {"2026-09": [_row("a")]}
    store = _Store(rows)
    result = lar.run(store, gym_id="lasso", months=["2026-09"], write=True)
    assert result["ok"] is False
    assert store.created == []
