import io
import json
import struct
import zlib

import pytest
from PIL import Image

from agent import historical_published_visual_corpus as corpus


def png(color):
    out = io.BytesIO()
    Image.new("RGB", (24, 18), color).save(out, "PNG")
    return out.getvalue()


def snapshot(rows):
    return {"schema_version": 1, "complete": True, "row_count": len(rows), "rows": rows}


def row(rid, url, *, gym="gym-a", date="2026-09-01", revision="r1"):
    return {"row_id": rid, "revision": revision, "gym_id": gym,
            "published_at": date + "T12:00:00Z", "image_url": url}


class Response:
    status_code = 200
    headers = {}

    def __init__(self, data): self.data = data
    def iter_content(self, _size): yield self.data
    def close(self): pass


class Session:
    def __init__(self, objects): self.objects = objects
    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        return Response(self.objects[url])


def test_requires_explicit_complete_count_and_exact_row_revision_allowlist():
    with pytest.raises(corpus.CorpusError, match="explicitly_complete"):
        corpus.load_snapshot({"schema_version": 1, "row_count": 1, "rows": [row("a", "https://cdn.test/a")]})
    with pytest.raises(corpus.CorpusError, match="row_count_mismatch"):
        corpus.load_snapshot(snapshot([row("a", "https://cdn.test/a")]) | {"row_count": 2})
    with pytest.raises(corpus.CorpusError, match="allowlist_mismatch"):
        corpus.load_snapshot(snapshot([row("a", "https://cdn.test/a")]), [("a", "r2")])


def test_cross_gym_date_duplicate_bytes_and_near_scene_are_signals_not_clearance(tmp_path):
    one, two = png((30, 60, 90)), png((31, 61, 91))
    rows = [row("a", "https://cdn.test/a", gym="gym-a", date="2026-08-01"),
            row("b", "https://cdn.test/b", gym="gym-b", date="2026-09-02"),
            row("c", "https://cdn.test/c", gym="gym-c", date="2026-09-03")]
    result = corpus.collect(snapshot(rows), allowed_hosts={"cdn.test"}, manifest_path=tmp_path / "private.json",
                            session=Session({rows[0]["image_url"]: one, rows[1]["image_url"]: one,
                                             rows[2]["image_url"]: two}))
    assert result["complete"] is True and result["hashed_count"] == 3
    assert (tmp_path / "private.json").stat().st_mode & 0o777 == 0o600
    text = (tmp_path / "private.json").read_text()
    assert "https://" not in text and '"row_id"' not in text and "gym-a" not in text
    signals = corpus.compare_signals(result)
    assert len(signals["exact_byte_groups"]) == 1
    duplicate = signals["exact_byte_groups"][0]
    assert len({x["gym_sha256"] for x in duplicate}) == 2
    assert len({x["published_date_sha256"] for x in duplicate}) == 2
    assert signals["near_scene_pairs_hamming_le_8"]
    assert signals["candidate_absence_means_unused"] is False


def test_unreadable_row_stays_unknown_and_host_is_fail_closed(tmp_path):
    rows = [row("bad", "https://evil.test/x")]
    result = corpus.collect(snapshot(rows), allowed_hosts={"cdn.test"}, manifest_path=tmp_path / "state.json",
                            session=Session({}))
    assert result["unknown_count"] == 1 and result["records"]
    record = next(iter(result["records"].values()))
    assert record["status"] == "unknown"
    assert record["unknown_reason"] == "hosted_url_not_allowlisted"
    assert not any("unused" in str(v).lower() for v in record.values())


def test_resume_rejects_changed_row_revision_snapshot(tmp_path):
    path = tmp_path / "state.json"
    old = snapshot([row("a", "https://cdn.test/a")])
    corpus.collect(old, allowed_hosts={"cdn.test"}, manifest_path=path,
                   session=Session({"https://cdn.test/a": png((1, 2, 3))}))
    changed = snapshot([row("a", "https://cdn.test/a", revision="r2")])
    with pytest.raises(corpus.CorpusError, match="resume_snapshot_changed"):
        corpus.collect(changed, allowed_hosts={"cdn.test"}, manifest_path=path)


def test_null_published_at_is_retained_as_unknown_date_and_image_is_hashed(tmp_path):
    rows = [row("no-date", "https://cdn.test/a")]
    rows[0]["published_at"] = None
    result = corpus.collect(snapshot(rows), allowed_hosts={"cdn.test"}, manifest_path=tmp_path / "unknown-date.json",
                            session=Session({"https://cdn.test/a": png((4, 5, 6))}))
    assert result["row_count"] == 1 and result["hashed_count"] == 1 and result["complete"]
    record = next(iter(result["records"].values()))
    assert record["published_date_known"] is False
    assert record["status"] == "hashed"


def test_manifest_refuses_non_private_permissions(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 1, "snapshot_sha256": "wrong", "records": {}}))
    path.chmod(0o644)
    with pytest.raises(corpus.CorpusError, match="permissions_not_private"):
        corpus.collect(snapshot([row("a", "https://cdn.test/a")]), allowed_hosts={"cdn.test"}, manifest_path=path)


def test_explicit_tranche_remains_partial_and_can_resume_against_full_snapshot(tmp_path):
    rows = [row("a", "https://cdn.test/a"), row("b", "https://cdn.test/b")]
    snap = snapshot(rows)
    path = tmp_path / "tranche.json"
    session = Session({r["image_url"]: png((1, 2, 3)) for r in rows})
    first = corpus.collect(snap, allowed_hosts={"cdn.test"}, manifest_path=path,
                           target_row_refs=[("a", "r1")], session=session)
    assert first["row_count"] == 2 and len(first["records"]) == 1
    assert first["complete"] is False
    second = corpus.collect(snap, allowed_hosts={"cdn.test"}, manifest_path=path,
                            target_row_refs=[("b", "r1")], session=session)
    assert len(second["records"]) == 2 and second["complete"] is True


def test_tranche_rejects_refs_outside_snapshot_and_oversized_batches(tmp_path):
    snap = snapshot([row("a", "https://cdn.test/a")])
    with pytest.raises(corpus.CorpusError, match="outside_snapshot"):
        corpus.collect(snap, allowed_hosts={"cdn.test"}, manifest_path=tmp_path / "missing.json",
                       target_row_refs=[("b", "r1")])
    too_many = [(str(i), "r1") for i in range(corpus.MAX_TRANCHE_ROWS + 1)]
    with pytest.raises(corpus.CorpusError, match="tranche_row_refs_invalid"):
        corpus.collect(snap, allowed_hosts={"cdn.test"}, manifest_path=tmp_path / "large.json",
                       target_row_refs=too_many)


def test_candidate_lookup_returns_hash_only_signals_and_never_clearance(tmp_path):
    candidate = png((20, 30, 40))
    other = png((21, 31, 41))
    rows = [row("a", "https://cdn.test/a"), row("b", "https://cdn.test/b"),
            row("c", "https://evil.test/c")]
    snap = snapshot(rows)
    manifest = corpus.collect(snap, allowed_hosts={"cdn.test"}, manifest_path=tmp_path / "manifest.json",
                              session=Session({rows[0]["image_url"]: candidate,
                                               rows[1]["image_url"]: other}))
    result = corpus.lookup_candidate(candidate, manifest, snap)
    assert [m["row_ref_sha256"] for m in result["exact_byte_matches"]] == [
        corpus._sha(corpus._canonical(["a", "r1"]))]
    assert result["near_scene_matches"]
    assert result["corpus_hashed_count"] == 2 and result["corpus_unknown_count"] == 1
    assert result["candidate_absence_means_unused"] is False
    assert "https://" not in json.dumps(result) and '"row_id"' not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["incomplete", "row_count", "snapshot_digest", "raw_url"])
def test_candidate_lookup_rejects_unbound_or_malformed_manifest(tmp_path, mutation):
    image = png((2, 3, 4))
    rows = [row("a", "https://cdn.test/a")]
    snap = snapshot(rows)
    manifest = corpus.collect(snap, allowed_hosts={"cdn.test"}, manifest_path=tmp_path / "lookup.json",
                              session=Session({rows[0]["image_url"]: image}))
    if mutation == "incomplete": manifest["complete"] = False
    elif mutation == "row_count": manifest["row_count"] = 0
    elif mutation == "snapshot_digest": manifest["snapshot_sha256"] = "0" * 64
    elif mutation == "raw_url": next(iter(manifest["records"].values()))["image_url"] = rows[0]["image_url"]
    with pytest.raises(corpus.CorpusError):
        corpus.lookup_candidate(image, manifest, snap)


def test_candidate_lookup_rejects_candidate_bytes_above_bound(monkeypatch):
    monkeypatch.setattr(corpus, "MAX_IMAGE_BYTES", 4)
    with pytest.raises(corpus.CorpusError, match="candidate_bytes_invalid"):
        corpus.lookup_candidate(b"12345", {}, {})


@pytest.mark.parametrize("corruption", ["missing_dhash", "metadata_mismatch", "raw_url"])
def test_resume_rejects_incomplete_mismatched_or_non_hash_only_record(tmp_path, corruption):
    path = tmp_path / "state.json"
    item = row("a", "https://cdn.test/a")
    snap = snapshot([item])
    corpus.collect(snap, allowed_hosts={"cdn.test"}, manifest_path=path,
                   session=Session({item["image_url"]: png((7, 8, 9))}))
    saved = json.loads(path.read_text())
    key, record = next(iter(saved["records"].items()))
    if corruption == "missing_dhash":
        del record["dhash64"]
    elif corruption == "metadata_mismatch":
        record["gym_sha256"] = "0" * 64
    else:
        record["image_url"] = item["image_url"]
    path.write_text(json.dumps(saved))
    path.chmod(0o600)
    with pytest.raises(corpus.CorpusError, match="resume_manifest_invalid"):
        corpus.collect(snap, allowed_hosts={"cdn.test"}, manifest_path=path)


def test_oversized_pixel_dimensions_rejected_before_image_transform(monkeypatch):
    width, height = 4001, 10000
    def png_chunk(kind, payload):
        chunk = kind + payload
        return struct.pack(">I", len(payload)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xffffffff)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    oversized_header = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
                        + png_chunk(b"IDAT", zlib.compress(b"\x00")) + png_chunk(b"IEND", b""))
    monkeypatch.setattr(corpus.ImageOps, "exif_transpose",
                        lambda *_: pytest.fail("transform ran before pixel bound check"))
    with pytest.raises(ValueError, match="image_exceeds_pixel_bound"):
        corpus._visual_fingerprint(oversized_header)
