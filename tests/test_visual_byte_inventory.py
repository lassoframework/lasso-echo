import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import visual_byte_inventory as inventory


def test_exact_url_download_is_deduplicated_and_each_row_is_preserved():
    url = "https://media.example/a.jpg?variant=story"
    calls = []

    def reader(value):
        calls.append(value)
        return b"delivered bytes"

    result = inventory.build_manifest({"rows": [
        {"id": "row-1", "gym_id": "gym-a", "status": "published", "post_date": "2026-10-01", "image_url": url},
        {"id": "row-2", "gym_id": "gym-b", "status": "held", "post_date": "2026-10-02", "image_url": url},
    ]}, reader)

    assert calls == [url]
    assert result["unique_exact_urls_read"] == 1
    assert [row["row_ref"] for row in result["rows"]] == ["row-1", "row-2"]
    delivered = result["rows"][0]["delivered"]
    assert delivered == {
        "exact_url": url, "status": "observed",
        "md5": hashlib.md5(b"delivered bytes").hexdigest(),
        "sha256": hashlib.sha256(b"delivered bytes").hexdigest(),
        "byte_length": len(b"delivered bytes"),
    }


def test_source_observation_is_separate_from_delivered_digest():
    source_url = "https://media.example/raw.jpg"
    delivered_url = "https://media.example/burned.jpg"
    payloads = {source_url: b"raw source", delivered_url: b"burned rendition"}
    result = inventory.build_manifest([{
        "id": "story-row", "gym_id": "gym-a", "status": "published",
        "date": "2026-10-03", "image_url": delivered_url,
        "source_media_url": source_url,
    }], lambda url: payloads[url])

    row = result["rows"][0]
    assert row["delivered"]["exact_url"] == delivered_url
    assert row["delivered"]["md5"] == hashlib.md5(payloads[delivered_url]).hexdigest()
    assert row["source_observation"]["exact_url"] == source_url
    assert row["source_observation"]["md5"] == hashlib.md5(payloads[source_url]).hexdigest()
    assert row["source_observation"]["md5"] != row["delivered"]["md5"]


def test_missing_ambiguous_and_unreadable_objects_are_explicit_errors():
    result = inventory.build_manifest([
        {"id": "missing", "gym_id": "g", "status": "active", "date": "d"},
        {"id": "ambiguous", "image_url": "https://a.example/x", "delivered_url": "https://b.example/y"},
        {"id": "unreadable", "image_url": "https://media.example/fail"},
        {"id": "empty", "image_url": "https://media.example/empty"},
    ], lambda url: (_ for _ in ()).throw(OSError("network failure")) if url.endswith("fail") else
       b"" if url.endswith("empty") else b"bytes")

    errors = [row["delivered"].get("error") for row in result["rows"]]
    assert errors == ["missing_delivered_url", "ambiguous_delivered_url",
                      "exact_url_read_failed", "empty_object"]
    assert result["rows"][0]["gym"] == "g"


def test_invalid_redirect_prone_or_credential_urls_fail_before_reader():
    calls = []
    result = inventory.build_manifest([{"id": "bad", "image_url": "https://u:p@host/a"}],
                                      lambda url: calls.append(url) or b"bytes")
    assert result["rows"][0]["delivered"]["error"] == "invalid_or_missing_exact_url"
    assert calls == []


def test_oversized_reader_result_is_rejected(monkeypatch):
    monkeypatch.setattr(inventory, "MAX_BYTES", 3)
    result = inventory.build_manifest([{"image_url": "https://media.example/big"}],
                                      lambda _url: b"four")
    assert result["rows"][0]["delivered"]["error"] == "object_exceeds_byte_limit"


def test_direct_cli_bootstraps_repo_and_loads_default_reader_without_network(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    bootstrap = tmp_path / "sitecustomize.py"
    bootstrap.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(repo)!r})
        import agent.visual_writer_prepare as reader_module
        reader_module._bytes_for_url = lambda url: b"subprocess mock bytes"
    """), encoding="utf-8")
    snapshot = tmp_path / "snapshot.json"
    manifest = tmp_path / "manifest.json"
    snapshot.write_text(json.dumps({"rows": [{
        "id": "cli-row", "gym_id": "gym-cli", "status": "published",
        "post_date": "2026-10-03", "image_url": "https://media.example/mock.jpg",
    }]}), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)

    completed = subprocess.run(
        [sys.executable, str(repo / "scripts" / "visual_byte_inventory.py"),
         str(snapshot), str(manifest)],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    row = json.loads(manifest.read_text(encoding="utf-8"))["rows"][0]
    assert row["delivered"]["status"] == "observed"
    assert row["delivered"]["md5"] == hashlib.md5(b"subprocess mock bytes").hexdigest()


def test_empty_snapshot_direct_cli_bootstraps_without_pythonpath(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    snapshot = tmp_path / "empty.json"
    manifest = tmp_path / "manifest.json"
    snapshot.write_text("[]", encoding="utf-8")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, str(repo / "scripts" / "visual_byte_inventory.py"),
         str(snapshot), str(manifest)],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(manifest.read_text(encoding="utf-8"))["rows"] == []


def test_reader_import_failure_is_a_command_error_not_row_level_missing(tmp_path, monkeypatch, capsys):
    snapshot = tmp_path / "snapshot.json"
    manifest = tmp_path / "manifest.json"
    snapshot.write_text("[]", encoding="utf-8")

    def fail_setup():
        raise ImportError("reader dependency unavailable")

    monkeypatch.setattr(inventory, "_load_default_reader", fail_setup)
    result = inventory.main([str(snapshot), str(manifest)])

    assert result == 2
    assert "reader setup failed" in capsys.readouterr().err
    assert not manifest.exists()


def test_default_reader_none_is_reported_as_exact_url_read_failure(monkeypatch):
    from agent import visual_writer_prepare

    monkeypatch.setattr(visual_writer_prepare, "_bytes_for_url", lambda _url: None)
    result = inventory.build_manifest([{"image_url": "https://media.example/missing"}])

    assert result["rows"][0]["delivered"]["error"] == "exact_url_read_failed"
