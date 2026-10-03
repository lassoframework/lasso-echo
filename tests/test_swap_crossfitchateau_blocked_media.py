"""Focused tests for the CrossFit Chateau blocked-media repair writer."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO))
SCRIPT = REPO / "scripts" / "swap_crossfitchateau_blocked_media.py"
SPEC = importlib.util.spec_from_file_location("swap_chateau", SCRIPT)
swap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(swap)

ROW = {
    "id": swap._ROW_ID, "gym_id": swap._GYM, "status": "approved",
    "reject_reason": swap._REASON, "image_url": "https://cdn/old.png",
    "source_media_asset_id": "asset-1", "source_media_url": None,
    "format": "feed", "variant_status": "active",
}


class FakeResponse:
    def __init__(self, rows, status_code=200):
        self.status_code = status_code
        self._rows = rows
        self.text = ""

    def json(self):
        return self._rows


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def _client(self):
        return self

    def _rest(self, path):
        return path

    def _headers(self, extra=None):
        return dict(extra or {})

    def _prepare_visual_replacement(self, account_key, current, payload,
                                    render_evidence=None):
        return dict(payload)

    @staticmethod
    def _visual_media_cas(current, params):
        result = dict(params)
        result["image_url"] = 'eq."https://cdn/old.png"'
        return result

    @staticmethod
    def _visual_media_result(rows, account_key, current, payload):
        return rows[0] if rows else None

    def patch(self, url, *, params, headers, json, timeout):
        self.calls.append((params, json))
        return FakeResponse(self.rows)


def _pick():
    return {"image_url": "https://cdn/new.png", "source_media_asset_id": "asset-2",
            "source_media_url": "https://cdn/new-raw.png"}


def test_flag_off_direct_cas_unchanged(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    store = FakeStore([dict(ROW, image_url="https://cdn/new.png")])
    updated = swap._conditional_replace(store, dict(ROW), _pick())
    assert updated is not None
    params, payload = store.calls[0]
    assert params["status"] == "eq.approved"
    assert params["reject_reason"] == f"eq.{swap._REASON}"
    assert params["source_media_asset_id"] == "eq.asset-1"
    assert payload["status"] == "pending" and payload["reject_reason"] == ""
    assert "visual_group_key" not in payload and "byte_hash" not in payload


def test_flag_on_routes_through_prepared_writer(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    store = FakeStore([dict(ROW, image_url="https://cdn/new.png")])
    prepared_payloads = []

    def fake_prepare(account_key, current, payload, render_evidence=None):
        out = dict(payload, visual_group_key="vg-1", byte_hash="bh-1")
        prepared_payloads.append(out)
        return out

    monkeypatch.setattr(FakeStore, "_prepare_visual_replacement",
                        lambda self, a, c, p, render_evidence=None: fake_prepare(a, c, p))
    updated = swap._conditional_replace(store, dict(ROW), _pick())
    assert updated is not None
    params, payload = store.calls[0]
    # Prepared CAS binds every observed column, not just the legacy four.
    assert params["id"].startswith("eq.") and params["gym_id"] == f"eq.{swap._GYM}"
    assert params["image_url"] == 'eq."https://cdn/old.png"'
    assert payload["visual_group_key"] == "vg-1" and payload["byte_hash"] == "bh-1"


def test_flag_on_preparation_refusal_writes_nothing(monkeypatch):
    from agent import visual_writer_prepare
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")

    def refusing_prepare(self, account_key, current, payload, render_evidence=None):
        raise visual_writer_prepare.VisualPreparationError("no lineage")

    monkeypatch.setattr(FakeStore, "_prepare_visual_replacement", refusing_prepare)
    store = FakeStore([])
    try:
        swap._conditional_replace(store, dict(ROW), _pick())
    except SystemExit as exc:
        assert "REFUSED" in str(exc)
    else:
        raise AssertionError("preparation refusal must abort the write")
    assert store.calls == []
