"""gym_media_drive §4/§5/§7: classify, dedupe on content_hash, eligibility gates,
unprobed video never selectable, HEIC->JPEG + HEVC->H.264 + rendition cache."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_index as idx  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, folder, photo, video, make_asset  # noqa: E402


# ---- classify + skip ---------------------------------------------------------
def test_classify_image_video_other():
    assert idx.classify("a.jpg", "image/jpeg") == "photo"
    assert idx.classify("a.mp4", "video/mp4") == "video"
    assert idx.classify("a.heic", "application/octet-stream") == "photo"  # by ext
    assert idx.classify("notes.pdf", "application/pdf") == "other"
    assert idx.classify("data.zip", "application/zip") == "other"


def test_docs_pdf_zip_logged_and_skipped():
    files = [photo("p1"), video("v1"),
             folder("d1", "sub", "root"),
             _doc("doc1", "plan.pdf", "application/pdf"),
             _doc("z1", "batch.zip", "application/zip")]
    rows, skipped = idx.build_rows(files, "src1", "pierce")
    ids = {r["id"] for r in rows}
    assert ids == {"p1", "v1"}
    assert len(skipped) == 2


def _doc(fid, title, mime):
    from agent.integrations.drive_client import DriveFile
    return DriveFile(id=fid, title=title, mime_type=mime, size_bytes=1000,
                     parent_id="root", modified_time="2026-08-01T00:00:00Z",
                     content_hash=fid + "h")


# ---- dedupe on content_hash --------------------------------------------------
def test_dedupe_same_bytes_reuploaded_indexes_once():
    """The same photo re-uploaded (a coach dragging the folder twice) shares a
    content_hash and indexes ONCE, keeping the earliest-modified copy."""
    a = photo("newer", md5="SAME", modified="2026-08-10T00:00:00Z")
    b = photo("older", md5="SAME", modified="2026-08-01T00:00:00Z")
    rows, _ = idx.build_rows([a, b], "src1", "pierce")
    assert len(rows) == 1
    assert rows[0]["id"] == "older"           # earliest kept


def test_dedupe_across_folders():
    """The same bytes living in two subfolders index once."""
    a = photo("f1copy", parent="folderA", md5="DUP", modified="2026-08-05T00:00:00Z")
    b = photo("f2copy", parent="folderB", md5="DUP", modified="2026-08-02T00:00:00Z")
    rows, _ = idx.build_rows([a, b], "src1", "pierce")
    assert len(rows) == 1
    assert rows[0]["id"] == "f2copy"


def test_no_hash_files_are_all_kept():
    a = photo("n1", md5="")
    b = photo("n2", md5="")
    rows, _ = idx.build_rows([a, b], "src1", "pierce")
    assert {r["id"] for r in rows} == {"n1", "n2"}


# ---- eligibility gates -------------------------------------------------------
def test_photo_short_edge_gate():
    small = photo("s1", w=500, h=700)      # short edge 500 < 640
    big = photo("b1", w=1080, h=1350)      # short edge 1080 >= 640
    rows, _ = idx.build_rows([small, big], "src1", "pierce")
    by_id = {r["id"]: r for r in rows}
    assert by_id["s1"]["eligible"] is False
    assert by_id["s1"]["reject_reason"] == idx.REJECT_PHOTO_SMALL
    assert by_id["b1"]["eligible"] is True


def test_photo_aspect_out_of_band_carries_crop_hint():
    wide = photo("w1", w=3000, h=800)      # ratio 3.75 > 1.91 -> crop toward 1.91:1
    rows, _ = idx.build_rows([wide], "src1", "pierce")
    r = rows[0]
    assert r["eligible"] is True           # aspect never rejects
    assert r["crop_hint"] == "1.91:1"


def test_video_unprobed_is_not_selectable():
    """A video with no probe yet: eligible NULL, never selectable (fail closed)."""
    rows, _ = idx.build_rows([video("v1")], "src1", "pierce")
    assert rows[0]["eligible"] is None


def test_video_over_900mb_rejected_at_index():
    rows, _ = idx.build_rows([video("big", size=950_000_000)], "src1", "pierce")
    assert rows[0]["eligible"] is False
    assert rows[0]["reject_reason"] == idx.REJECT_VIDEO_SIZE


def test_video_eligibility_gate_values():
    assert idx.video_eligibility(1000, 42.0, 1080, 1350)[0] is True
    assert idx.video_eligibility(1000, 2.0)[0] is False       # too short
    assert idx.video_eligibility(1000, 120.0)[0] is False      # too long
    assert idx.video_eligibility(1000, None)[0] is None        # unprobed


# ---- HEIC / HEVC -------------------------------------------------------------
def test_heic_photo_is_eligible_pending_conversion():
    h = photo("h1", title="IMG_0001.HEIC", mime="image/heic", w=0, h=0)
    rows, _ = idx.build_rows([h], "src1", "pierce")
    assert rows[0]["eligible"] is True          # conversion is Echo's job (§5)
    assert rows[0]["reject_reason"] is None


def test_rendition_key_is_gym_and_hash_scoped():
    key = idx.rendition_key("pierce", "abc123", ".jpg")
    assert key == "pierce/abc123.jpg"


def test_ensure_rendition_heic_converts_and_caches(tmp_path):
    """First use converts HEIC->JPEG, uploads, persists the url. Second use is a
    pure cache hit (no re-convert)."""
    store = FakeMediaStore(assets=[make_asset("h1", kind="photo",
                                              title="IMG.HEIC", mime="image/heic")])
    asset = store.get_asset("h1")
    calls = {"heic": 0, "host": 0}

    def fake_heic(src, dest):
        calls["heic"] += 1
        open(dest, "wb").write(b"jpegbytes")
        return dest

    def fake_host(path, gym):
        calls["host"] += 1
        return f"https://cdn.fake/{gym}/rend.jpg"

    src = tmp_path / "IMG.HEIC"
    src.write_bytes(b"heicbytes")
    url, converted = idx.ensure_rendition(
        asset, str(src), store=store, host_fn=fake_host, heic_fn=fake_heic)
    assert converted is True and url and calls["heic"] == 1

    # Second use: the asset now carries rendition_url -> cache hit, no re-convert.
    asset2 = store.get_asset("h1")
    url2, converted2 = idx.ensure_rendition(
        asset2, str(src), store=store, host_fn=fake_host, heic_fn=fake_heic)
    assert converted2 is False and url2 == url
    assert calls["heic"] == 1 and calls["host"] == 1     # no second convert/upload


def test_ensure_rendition_hevc_transcodes(tmp_path):
    store = FakeMediaStore(assets=[make_asset("v1", kind="video", title="clip.mov",
                                              mime="video/quicktime")])
    asset = store.get_asset("v1")
    calls = {"hevc": 0}

    def fake_hevc(src, dest, runner=None):
        calls["hevc"] += 1
        open(dest, "wb").write(b"h264")
        return dest

    def fake_probe(path):
        return {"duration_sec": 20.0, "width": 1080, "height": 1920, "codec": "hevc"}

    src = tmp_path / "clip.mov"
    src.write_bytes(b"hevcbytes")
    url, converted = idx.ensure_rendition(
        asset, str(src), store=store, host_fn=lambda p, g: "https://cdn.fake/x.mp4",
        hevc_fn=fake_hevc, probe_fn=fake_probe)
    assert converted is True and calls["hevc"] == 1


def test_ensure_rendition_degrades_when_converter_unavailable(tmp_path):
    """No pillow-heif -> (None, False), never a crash; caller marks not-eligible."""
    store = FakeMediaStore(assets=[make_asset("h1", kind="photo",
                                              title="IMG.HEIC", mime="image/heic")])
    asset = store.get_asset("h1")

    def boom(src, dest):
        raise idx.ConversionUnavailable("pillow-heif missing")

    src = tmp_path / "IMG.HEIC"
    src.write_bytes(b"x")
    url, converted = idx.ensure_rendition(asset, str(src), store=store,
                                          host_fn=lambda p, g: "u", heic_fn=boom)
    assert url is None and converted is False


def test_ensure_rendition_noop_for_plain_jpeg(tmp_path):
    store = FakeMediaStore(assets=[make_asset("p1", kind="photo", title="team.jpg")])
    asset = store.get_asset("p1")
    src = tmp_path / "team.jpg"
    src.write_bytes(b"jpg")
    url, converted = idx.ensure_rendition(asset, str(src), store=store,
                                          host_fn=lambda p, g: "u")
    assert url is None and converted is False       # plain asset: use the original
