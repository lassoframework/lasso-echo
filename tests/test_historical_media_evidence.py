import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import historical_media_evidence as hme


def _md5(data: bytes) -> str:
    import hashlib
    return hashlib.md5(data).hexdigest()


@pytest.fixture
def media_dir(tmp_path):
    d = tmp_path / "bytes"
    d.mkdir()
    return d


def _payload(rows, assets):
    return {"gym_id": "gym1", "media_assets": assets, "published_rows": rows}


def test_unique_match_emits_known_used(media_dir):
    data = b"published-bytes"
    (media_dir / "row1").write_bytes(data)
    payload = _payload(
        [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9", "media_url": "https://r2/x"}],
        [{"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(data),
          "used_count": 0, "filename": "x.jpg", "phash": "deadbeef"}],
    )
    out = hme.build_evidence(payload, media_dir)
    assert out["held"] == []
    assert out["known_used"] == [{
        "row_id": "row1", "late_post_id": "post9", "media_asset_id": "a1",
        "content_hash": _md5(data), "published_md5": _md5(data),
    }]


def test_no_match_held(media_dir):
    (media_dir / "row1").write_bytes(b"other-bytes")
    payload = _payload(
        [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"}],
        [{"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(b"nope")}],
    )
    out = hme.build_evidence(payload, media_dir)
    assert out["known_used"] == []
    assert out["held"][0]["reason"] == "no_hash_match"


def test_duplicate_hash_held_ambiguous(media_dir):
    data = b"dup-bytes"
    (media_dir / "row1").write_bytes(data)
    payload = _payload(
        [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"}],
        [{"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(data)},
         {"asset_id": "a2", "gym_id": "gym1", "content_hash": _md5(data)}],
    )
    out = hme.build_evidence(payload, media_dir)
    assert out["known_used"] == []
    assert out["held"][0]["reason"] == "ambiguous_hash_match"
    assert out["held"][0]["candidate_asset_ids"] == ["a1", "a2"]


def test_missing_bytes_and_missing_post_id(media_dir):
    payload = _payload(
        [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"},
         {"gym_id": "gym1", "row_id": "row2", "late_post_id": ""},
         {"gym_id": "gym1", "row_id": "row3"}],
        [],
    )
    out = hme.build_evidence(payload, media_dir)
    reasons = {h["row_id"]: h["reason"] for h in out["held"]}
    assert reasons == {"row1": "missing_bytes", "row2": "missing_post_id",
                       "row3": "missing_post_id"}


def test_cross_gym_isolation(media_dir):
    data = b"gym1-bytes"
    (media_dir / "row1").write_bytes(data)
    payload = _payload(
        [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"}],
        [{"asset_id": "aX", "gym_id": "gym2", "content_hash": _md5(data)}],
    )
    out = hme.build_evidence(payload, media_dir)
    assert out["known_used"] == []
    assert out["held"][0]["reason"] == "no_hash_match"


def test_source_asset_id_and_zero_used_count_not_proof(media_dir):
    # Row points at an asset id directly, but byte hash matches nothing:
    # held, not known_used. used_count=0 / filename / phash / URL are ignored.
    (media_dir / "row1").write_bytes(b"bytes")
    payload = _payload(
        [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9", "source_media_asset_id": "a1",
          "media_url": "https://r2/known"}],
        [{"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(b"different"),
          "used_count": 0, "filename": "known.jpg", "phash": "same"}],
    )
    out = hme.build_evidence(payload, media_dir)
    assert out["known_used"] == []
    assert out["held"][0]["reason"] == "no_hash_match"


def test_cli_malformed_input(media_dir, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert hme.main(["--input", str(bad), "--media-dir", str(media_dir)]) == 2
    assert "malformed input JSON" in capsys.readouterr().err


def test_cli_rejects_non_object_input(media_dir, capsys):
    import io
    old = sys.stdin
    sys.stdin = io.StringIO(json.dumps([1, 2]))
    try:
        rc = hme.main(["--media-dir", str(media_dir)])
    finally:
        sys.stdin = old
    assert rc == 2


def test_path_traversal_row_id_rejected(media_dir):
    data = b"secret"
    (media_dir / "row1").write_bytes(b"ok")
    (media_dir.parent / "secret").write_bytes(data)
    payload = _payload(
        [{"gym_id": "gym1", "row_id": "../secret", "late_post_id": "post9"}],
        [{"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(data)}],
    )
    out = hme.build_evidence(payload, media_dir)
    assert out["known_used"] == []
    assert out["held"][0]["reason"] == "missing_bytes"


def test_cli_end_to_end(tmp_path, capsys):
    d = tmp_path / "bytes"
    d.mkdir()
    (d / "row1").write_bytes(b"cli-bytes")
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps(_payload(
        [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"}],
        [{"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(b"cli-bytes")}])))
    rc = hme.main(["--input", str(inp), "--media-dir", str(d)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["known_used"][0]["media_asset_id"] == "a1"


# --- Adversarial tests (independent Luna review) ---

def test_empty_gym_id_holds_everything(media_dir):
    data = b"published-bytes"
    (media_dir / "row1").write_bytes(data)
    payload = {
        "gym_id": "",
        "media_assets": [{"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(data)}],
        "published_rows": [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"}],
    }
    out = hme.build_evidence(payload, media_dir)
    assert out["known_used"] == []
    assert out["held"][0]["reason"] == "missing_gym_id"


def test_cli_rejects_empty_gym_id(media_dir, tmp_path, capsys):
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps({"gym_id": "", "published_rows": []}))
    assert hme.main(["--input", str(inp), "--media-dir", str(media_dir)]) == 2
    assert "nonempty gym_id" in capsys.readouterr().err


def test_mixed_gym_rows_with_identical_bytes_never_emit_known_used(media_dir):
    # Two gyms publish identical bytes; neither row may be auto-matched.
    data = b"shared-bytes"
    (media_dir / "row1").write_bytes(data)
    (media_dir / "row2").write_bytes(data)
    payload = {
        "gym_id": "gym1",
        "media_assets": [
            {"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(data)},
            {"asset_id": "aX", "gym_id": "gym2", "content_hash": _md5(data)},
        ],
        "published_rows": [
            {"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"},
            {"gym_id": "gym2", "row_id": "row2", "late_post_id": "postA"},
            {"gym_id": "", "row_id": "row1", "late_post_id": "post9"},
        ],
    }
    out = hme.build_evidence(payload, media_dir)
    # gym1's row legitimately matches its own gym's asset; the other-gym row
    # and the empty-gym row are held, never matched against gym2's asset.
    assert [r["row_id"] for r in out["known_used"]] == ["row1"]
    reasons = {(h["row_id"], h["reason"]) for h in out["held"]}
    assert ("row2", "gym_mismatch") in reasons
    assert ("row1", "gym_mismatch") in reasons  # empty-gym copy of row1


def test_malformed_nested_entries_do_not_crash(media_dir):
    (media_dir / "row1").write_bytes(b"ok-bytes")
    payload = {
        "gym_id": "gym1",
        "media_assets": [
            None,
            "not-a-dict",
            {"asset_id": "", "gym_id": "gym1", "content_hash": _md5(b"ok-bytes")},
            {"gym_id": "gym1", "content_hash": _md5(b"ok-bytes")},
            {"asset_id": "a1", "gym_id": "gym1", "content_hash": "not-a-hash"},
            {"asset_id": "a1", "gym_id": "gym1"},
            {"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(b"ok-bytes")},
        ],
        "published_rows": [
            None,
            "not-a-dict",
            {"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"},
        ],
    }
    out = hme.build_evidence(payload, media_dir)
    # Only the one valid asset matches the row; malformed assets fail closed.
    assert out["known_used"] == [{
        "row_id": "row1", "late_post_id": "post9", "media_asset_id": "a1",
        "content_hash": _md5(b"ok-bytes"), "published_md5": _md5(b"ok-bytes"),
    }]
    assert len(out["held"]) == 2
    assert all(h["reason"] == "malformed_row" for h in out["held"])


def test_empty_asset_id_and_bad_hash_fail_closed(media_dir):
    data = b"row-bytes"
    (media_dir / "row1").write_bytes(data)
    digest = _md5(data)
    payload = {
        "gym_id": "gym1",
        "media_assets": [
            {"asset_id": "", "gym_id": "gym1", "content_hash": digest},
            {"media_asset_id": "", "gym_id": "gym1", "content_hash": digest},
            {"asset_id": "a1", "gym_id": "gym1", "content_hash": ""},
            {"asset_id": "a2", "gym_id": "gym1", "content_hash": "zz" * 16},
            {"asset_id": "a3", "gym_id": "gym1", "content_hash": digest.upper() + "0"},
        ],
        "published_rows": [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"}],
    }
    out = hme.build_evidence(payload, media_dir)
    assert out["known_used"] == []
    assert out["held"][0]["reason"] == "no_hash_match"


def test_oversized_file_held(media_dir, monkeypatch):
    monkeypatch.setattr(hme, "MAX_BYTES", 8)
    (media_dir / "row1").write_bytes(b"x" * 100)
    payload = _payload(
        [{"gym_id": "gym1", "row_id": "row1", "late_post_id": "post9"}],
        [{"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(b"x" * 100)}],
    )
    out = hme.build_evidence(payload, media_dir)
    assert out["known_used"] == []
    assert out["held"][0]["reason"] == "oversized_file"


def test_output_sorted_and_identical_under_input_reorder(media_dir):
    d1, d2 = b"aaa-bytes", b"bbb-bytes"
    (media_dir / "rowB").write_bytes(d2)
    (media_dir / "rowA").write_bytes(d1)
    rows = [
        {"gym_id": "gym1", "row_id": "rowB", "late_post_id": "post2"},
        {"gym_id": "gym1", "row_id": "rowA", "late_post_id": "post1"},
        {"gym_id": "gym1", "row_id": "rowC", "late_post_id": "post3"},
    ]
    assets = [
        {"asset_id": "a2", "gym_id": "gym1", "content_hash": _md5(d2)},
        {"asset_id": "a1", "gym_id": "gym1", "content_hash": _md5(d1)},
    ]
    out1 = hme.build_evidence(_payload(rows, assets), media_dir)
    out2 = hme.build_evidence(_payload(list(reversed(rows)), list(reversed(assets))), media_dir)
    assert out1 == out2
    j1 = json.dumps(out1, sort_keys=True)
    j2 = json.dumps(out2, sort_keys=True)
    assert j1 == j2
    assert [r["row_id"] for r in out1["known_used"]] == ["rowA", "rowB"]
    assert [h["row_id"] for h in out1["held"]] == ["rowC"]
