import json

import pytest

from agent import outbound_publish_receipt as receipts


def _row(**extra):
    row = {"id": "row-1", "gym_id": "gym-a", "account": "instagram",
           "status": "approved", "image_url": "https://private.example/x.jpg"}
    row.update(extra)
    return row


def test_disabled_receipt_is_inert(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_OUTBOUND_PUBLISH_RECEIPT", raising=False)
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT_PATH", str(tmp_path / "receipt.jsonl"))
    assert receipts.record(lane="calendar", row=_row(), decision="held") is None
    assert not (tmp_path / "receipt.jsonl").exists()


def test_append_only_redacted_receipt(monkeypatch, tmp_path):
    path = tmp_path / "receipt.jsonl"
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT", "true")
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT_PATH", str(path))
    first = receipts.record(lane="calendar", row=_row(source_media_asset_id="asset-1"),
                            decision="held_unapproved", reason="not approved")
    second = receipts.record(lane="gbp", row=_row(account="googlebusiness"),
                             decision="provider_result", attempted=True, outcome="published")
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["lane"] for line in lines] == ["calendar", "gbp"]
    assert first["approved"] is True and first["source_media_asset_id_present"] is True
    assert second["attempted"] is True and second["outcome"] == "published"
    assert "https://private.example/x.jpg" not in path.read_text()


def test_missing_asset_id_is_observed_not_held(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT", "true")
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT_PATH", str(tmp_path / "receipt.jsonl"))
    receipt = receipts.record(lane="calendar", row=_row(source_media_asset_id=""),
                              decision="preflight_passed")
    assert receipt["source_media_asset_id_present"] is False
    assert receipt["decision"] == "preflight_passed"


def test_armed_write_failure_raises_safe_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT", "true")
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT_PATH", str(tmp_path / "receipt.jsonl"))
    monkeypatch.setattr(receipts.os, "write", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    try:
        receipts.record(lane="calendar", row=_row(), decision="preflight_passed")
    except receipts.ReceiptWriteError as exc:
        assert str(exc) == "outbound publish receipt could not be persisted"
    else:
        raise AssertionError("armed persistence failure must fail closed")


def test_partial_writes_complete_before_sync(monkeypatch, tmp_path):
    path = tmp_path / "receipt.jsonl"
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT", "true")
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT_PATH", str(path))
    original_write = receipts.os.write
    writes = []
    synced = []

    def partial_write(fd, payload):
        writes.append(len(payload))
        return original_write(fd, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(receipts.os, "write", partial_write)
    monkeypatch.setattr(receipts.os, "fsync", lambda fd: synced.append(fd))
    receipts.record(lane="calendar", row=_row(), decision="preflight_passed")
    assert len(writes) > 1
    assert len(synced) == 1
    assert len(path.read_text().splitlines()) == 1


def test_armed_sync_failure_raises_safe_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT", "true")
    monkeypatch.setenv("AGENT_OUTBOUND_PUBLISH_RECEIPT_PATH", str(tmp_path / "receipt.jsonl"))
    monkeypatch.setattr(receipts.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("sync")))
    with pytest.raises(receipts.ReceiptWriteError,
                       match="outbound publish receipt could not be persisted"):
        receipts.record(lane="calendar", row=_row(), decision="preflight_passed")
