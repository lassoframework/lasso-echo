import io
import json

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


def test_manifest_refuses_non_private_permissions(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 1, "snapshot_sha256": "wrong", "records": {}}))
    path.chmod(0o644)
    with pytest.raises(corpus.CorpusError, match="permissions_not_private"):
        corpus.collect(snapshot([row("a", "https://cdn.test/a")]), allowed_hosts={"cdn.test"}, manifest_path=path)
