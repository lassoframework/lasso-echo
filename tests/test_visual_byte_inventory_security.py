import json
import stat

from scripts import visual_byte_inventory as inventory


def test_write_manifest_is_atomic_and_owner_only(tmp_path):
    target = tmp_path / "manifest.json"
    inventory.write_manifest(target, {"format": "test", "rows": []})

    assert json.loads(target.read_text()) == {"format": "test", "rows": []}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".manifest.json.*.tmp"))


def test_default_reader_classifies_storage_unavailable(monkeypatch):
    monkeypatch.setattr(inventory, "_load_default_reader", lambda: (
        lambda _url: (_ for _ in ()).throw(
            inventory.ReaderObservationError("storage_unavailable_or_object_unreadable"))))

    result = inventory.build_manifest(
        [{"id": "outside", "image_url": "https://example.invalid/a.jpg"}])

    assert result["rows"][0]["delivered"]["error"] == "storage_unavailable_or_object_unreadable"


def test_default_reader_classifies_unconfigured_setup(monkeypatch):
    monkeypatch.setattr(inventory, "_load_default_reader",
                        lambda: (_ for _ in ()).throw(
                            inventory.ReaderSetupError("reader_unconfigured")))

    # Setup errors are resolved before rows are processed and remain a command error.
    try:
        inventory.build_manifest([{"id": "row", "image_url": "https://x.invalid/a"}])
    except inventory.ReaderSetupError as exc:
        assert str(exc) == "reader_unconfigured"
    else:
        raise AssertionError("expected reader setup failure")
