"""
GBP mirror Drive-provenance tests (agent/gbp_mirror.py), fully offline.

The Oct14-Nov3 Swift River audit found 21 pending Google Business derivative rows
with source_media_asset_id NULL even though each mirrored a Drive photo feed row —
weakening future global one-use provenance. This package makes the mirror fail-closed:

  * a GBP row that mirrors a Drive photo carries the feed draft's ACTUAL
    source_media_asset_id and exact RAW source_media_url (never the cropped URL),
  * when the global prepared writer (AGENT_VISUAL_GLOBAL_WRITER_PREP) is armed, the
    row also carries byte-bound source->delivered render evidence into the insert
    boundary via a sink keyed by the cropped image_url,
  * when attestation cannot be built (missing/unverifiable source bytes, no explicit
    raw source, an opaque injected image seam, or no sink to forward proof through)
    the mirror HOLDS the row instead of staging an unprovable one,
  * guard-OFF behavior for a non-Drive photo is the byte-for-byte legacy crop path.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, gbp_mirror as gm, gbp_planner  # noqa: E402

RAW_URL = "https://cdn.test/raw/drive-photo.jpg"
CROPPED_URL = "https://cdn.test/gbp/2026-10-14.jpg"
ASSET_ID = "asset-123"

_EVIDENCE = {
    "operation": "render",
    "source_exact_url": RAW_URL,
    "delivered_exact_url": CROPPED_URL,
    "source_fingerprint": "md5:aaa",
    "delivered_fingerprint": "md5:bbb",
    "source_byte_length": 10,
    "delivered_byte_length": 12,
    "evidence_ref": "gbp_planner:render:aaa:bbb:uuid",
    "observed_by": "gbp_planner",
    "rendered_by": "gbp_planner",
}


def _draft(drive=True, raw_url=RAW_URL):
    d = types.SimpleNamespace(
        caption="Members showed up at 5am and put in the work today.",
        platform="instagram", day_key="2026-10-14", is_story=False,
        draft_type="feed", category="community",
        creative_path="/tmp/x.jpg",
        creative_public_url=RAW_URL,
        scheduled_for="2026-10-14T10:00:00Z")
    if drive:
        d.source_media_asset_id = ASSET_ID
        if raw_url:
            d.source_media_url = raw_url
    return d


def _ctx():
    return {"city": "Cape Coral", "voice": object(), "cta_url": "https://gym.test/join",
            "account_gen_key": "eng_ig"}


def _armed(monkeypatch, writer=False):
    monkeypatch.setenv("AGENT_GBP_MIRROR", "true")
    monkeypatch.setattr(config, "hosting_enabled", lambda: True)
    monkeypatch.setattr("agent.gbp.caption_issues", lambda cap, city=None: [])
    if writer:
        monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    else:
        monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)


def _media_seams(monkeypatch, tmp_path, transform_result, bytes_ok=True):
    """Patch the crop lane: a local still always resolves; the transform (or the
    legacy crop) returns the injected result; the served-source byte verify is
    stubbed offline. Returns a calls recorder."""
    monkeypatch.setattr(config, "data_dir", lambda: str(tmp_path))
    still = tmp_path / "still.jpg"
    still.write_bytes(b"source-bytes")
    calls = {"transform": 0, "legacy": 0, "verify": 0}

    def _verify(url, path):
        calls["verify"] += 1
        return bytes_ok

    monkeypatch.setattr(gbp_planner, "_url_bytes_match", _verify)
    monkeypatch.setattr(gm, "_local_still",
                        lambda draft, lib, log: (str(still), None))

    def _transform(account_key, image, day_key, *, source_url=None):
        calls["transform"] += 1
        assert source_url == RAW_URL
        return transform_result

    monkeypatch.setattr(gbp_planner, "_transformed_gbp_image", _transform)

    def _legacy(account_key, image, day_key):
        calls["legacy"] += 1
        return CROPPED_URL

    monkeypatch.setattr(gbp_planner, "_cropped_image_url", _legacy)
    return calls


_PROV = {"url": CROPPED_URL, "source_media_url": RAW_URL,
         "render_evidence": dict(_EVIDENCE)}


def _rows(monkeypatch, drafts, **kw):
    kw.setdefault("ctx", _ctx())
    kw.setdefault("caption_fn", lambda fact: f"{fact} Visit us in Cape Coral.")
    kw.setdefault("logger", lambda m: None)
    return gm.rows_for("eng", drafts, **kw)


# ---- guard OFF: Drive photo keeps its provenance, writer untouched --------------------

def test_guard_off_drive_mirror_carries_asset_id_and_exact_raw_source(
        monkeypatch, tmp_path):
    _armed(monkeypatch, writer=False)
    calls = _media_seams(monkeypatch, tmp_path, dict(_PROV))
    sink = {}
    rows = _rows(monkeypatch, [_draft()], render_evidence_sink=sink)
    assert calls["transform"] == 1 and calls["legacy"] == 0
    assert len(rows) == 1
    r = rows[0]
    assert r["source_media_asset_id"] == ASSET_ID
    assert r["source_media_url"] == RAW_URL, "exact raw source, never the cropped URL"
    assert r["image_url"] == CROPPED_URL
    assert r["status"] == "pending"           # owner tap untouched
    assert "visual_group_key" not in r        # writer prep never ran
    assert sink == {}, "guard OFF forwards no render evidence"


def test_guard_off_non_drive_photo_is_the_legacy_crop_path(monkeypatch, tmp_path):
    _armed(monkeypatch, writer=False)
    calls = _media_seams(monkeypatch, tmp_path, dict(_PROV))
    rows = _rows(monkeypatch, [_draft(drive=False)],
                 render_evidence_sink={})
    assert calls["legacy"] == 1 and calls["transform"] == 0, \
        "non-Drive guard-off rows keep the byte-for-byte legacy crop"
    assert len(rows) == 1
    r = rows[0]
    assert "source_media_url" not in r and "source_media_asset_id" not in r
    assert set(r) == {"gym_id", "account", "post_date", "pillar", "format",
                      "caption", "image_url", "status", "gbp_topic_type",
                      "gbp_cta_type", "gbp_cta_url"}


def test_missing_source_bytes_hold_the_mirror_and_never_harm_the_feed(
        monkeypatch, tmp_path):
    _armed(monkeypatch, writer=False)
    calls = _media_seams(monkeypatch, tmp_path, None, bytes_ok=False)  # byte verify failed
    draft = _draft()
    rows = _rows(monkeypatch, [draft], render_evidence_sink={})
    assert rows == [], "an unverifiable source is held, never staged unprovable"
    assert draft.source_media_url == RAW_URL and draft.source_media_asset_id == ASSET_ID


def test_drive_draft_without_raw_source_url_is_held_not_relabeled(
        monkeypatch, tmp_path):
    _armed(monkeypatch, writer=False)
    calls = _media_seams(monkeypatch, tmp_path, dict(_PROV))
    rows = _rows(monkeypatch, [_draft(raw_url=None)], render_evidence_sink={})
    assert rows == [], "a Drive rendition is never relabeled as the raw source"
    assert calls["transform"] == 0 and calls["legacy"] == 0


# ---- guard ARMED: render evidence must reach the writer or the row holds -------------

def test_armed_writer_gets_byte_bound_evidence_through_the_sink(
        monkeypatch, tmp_path):
    _armed(monkeypatch, writer=True)
    _media_seams(monkeypatch, tmp_path, dict(_PROV))
    sink = {}
    rows = _rows(monkeypatch, [_draft()], render_evidence_sink=sink)
    assert len(rows) == 1
    r = rows[0]
    assert r["source_media_url"] == RAW_URL
    assert r["source_media_asset_id"] == ASSET_ID
    ev = sink[CROPPED_URL]
    assert ev["source_exact_url"] == RAW_URL
    assert ev["delivered_exact_url"] == CROPPED_URL
    assert "render_evidence" not in r, "evidence is a side channel, never a row column"


def test_armed_writer_holds_when_attestation_cannot_be_built(monkeypatch, tmp_path):
    _armed(monkeypatch, writer=True)
    _media_seams(monkeypatch, tmp_path, None, bytes_ok=False)
    sink = {}
    rows = _rows(monkeypatch, [_draft()], render_evidence_sink=sink)
    assert rows == [] and sink == {}


def test_armed_writer_holds_when_no_sink_can_forward_the_proof(
        monkeypatch, tmp_path):
    _armed(monkeypatch, writer=True)
    _media_seams(monkeypatch, tmp_path, dict(_PROV))
    rows = _rows(monkeypatch, [_draft()])   # no sink: proof could never reach insert
    assert rows == [], "an unforwardable attestation would fail the batch — hold it"


def test_armed_writer_holds_the_opaque_injected_image_seam(monkeypatch):
    _armed(monkeypatch, writer=True)
    rows = gm.rows_for("eng", [_draft()], ctx=_ctx(),
                       image_fn=lambda d, day: "https://cdn.test/injected.jpg",
                       caption_fn=lambda fact: "real copy for the listing",
                       logger=lambda m: None, render_evidence_sink={})
    assert rows == [], "an injected URL has no local still to byte-bind — unprovable"


def test_armed_writer_still_never_raises_into_the_month_build(monkeypatch):
    _armed(monkeypatch, writer=True)
    monkeypatch.setattr(gm, "_local_still",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _rows(monkeypatch, [_draft()], render_evidence_sink={}) == []


# ---- the shared row builder ------------------------------------------------------------

def test_row_stamps_the_asset_id_only_when_given_one():
    row = gbp_planner._row("eng", "eng_ig", "2026-10-14", "copy", CROPPED_URL,
                           topic_type="STANDARD", pillar="community",
                           source_media_url=RAW_URL,
                           source_media_asset_id=ASSET_ID)
    assert row["source_media_asset_id"] == ASSET_ID
    assert row["source_media_url"] == RAW_URL
    plain = gbp_planner._row("eng", "eng_ig", "2026-10-14", "copy", CROPPED_URL,
                             topic_type="STANDARD", pillar="community")
    assert "source_media_asset_id" not in plain and "source_media_url" not in plain


# ---- crop-cache collision: same basename, different bytes (P1 repair) -------------------

def _make_photo(path, rgb):
    from PIL import Image
    Image.new("RGB", (1200, 1000), rgb).save(path, "JPEG", quality=95)


def _provenance_lane(monkeypatch, tmp_path):
    """The REAL provenance lane end to end (real _local_still, real
    _hash_qualified_source, real _cropped_image cache, real
    _transformed_gbp_image) with only the network edges stubbed: the served
    raw source bytes and the R2 upload. Returns (served, delivered, drafts)."""
    import hashlib
    _armed(monkeypatch, writer=False)
    monkeypatch.setattr("agent.rotation._cache_dir", lambda _key: str(tmp_path),
                        raising=False)
    served, delivered = {}, {}

    first = tmp_path / "first" / "same.jpg"
    second = tmp_path / "second" / "same.jpg"
    first.parent.mkdir(exist_ok=True)
    second.parent.mkdir(exist_ok=True)
    _make_photo(first, (200, 30, 30))
    _make_photo(second, (30, 30, 200))
    bytes_a, bytes_b = first.read_bytes(), second.read_bytes()
    url_a, url_b = "https://cdn.test/raw/a/same.jpg", "https://cdn.test/raw/b/same.jpg"
    served[url_a], served[url_b] = bytes_a, bytes_b

    def _verify(url, path):
        return served.get(url) == open(path, "rb").read()

    monkeypatch.setattr(gbp_planner, "_url_bytes_match", _verify)

    def _host(local_path, tenant, client=None):
        data = open(local_path, "rb").read()
        url = f"https://cdn.test/gbp/{hashlib.md5(data).hexdigest()[:12]}.jpg"
        delivered[url] = data
        return url

    monkeypatch.setattr("agent.media_host.host_media", _host)

    def _draft_for(path, url):
        d = _draft()
        d.creative_path = str(path)
        d.creative_public_url = url
        d.source_media_url = url
        return d

    return served, delivered, _draft_for(first, url_a), _draft_for(second, url_b), \
        bytes_a, bytes_b


def test_same_basename_distinct_bytes_never_share_the_planner_crop_cache(
        monkeypatch, tmp_path):
    import hashlib
    served, delivered, draft_a, draft_b, bytes_a, bytes_b = \
        _provenance_lane(monkeypatch, tmp_path)
    ctx, log = _ctx(), lambda m: None

    # Reproduce the original P1 window: photo B's mtime is OLDER than the crop
    # photo A is about to write, so an account+basename+mtime cache key must
    # reuse A's crop for B. The content-hash-qualified basename prevents that.
    old = 1_600_000_000
    os.utime(str(tmp_path / "second" / "same.jpg"), (old, old))

    prov_a = gm.cropped_with_provenance(draft_a, ctx, str(tmp_path), log)
    prov_b = gm.cropped_with_provenance(draft_b, ctx, str(tmp_path), log)
    assert prov_a and prov_b, "both verifiable sources must render"
    assert prov_a["url"] != prov_b["url"], \
        "distinct bytes must deliver distinct hosted crops"
    assert delivered[prov_a["url"]] != delivered[prov_b["url"]]

    # Exact lineage per photo: the source evidence binds THIS photo's bytes,
    # the delivered evidence binds THAT photo's own crop pixels.
    for prov, raw, url in ((prov_a, bytes_a, draft_a.source_media_url),
                           (prov_b, bytes_b, draft_b.source_media_url)):
        assert prov["source_media_url"] == url
        ev = prov["render_evidence"]
        assert ev["source_exact_url"] == url
        assert ev["delivered_exact_url"] == prov["url"]
        assert ev["source_fingerprint"] == "md5:" + hashlib.md5(raw).hexdigest()
        crop = delivered[prov["url"]]
        assert ev["delivered_fingerprint"] == "md5:" + hashlib.md5(crop).hexdigest()
        assert ev["source_byte_length"] == len(raw)
        assert ev["delivered_byte_length"] == len(crop)

    # The planner crop cache now holds one content-unique crop per photo, and
    # the mirror staged exactly one hash-qualified source per content.
    crops = os.listdir(tmp_path / "gbp_crops")
    staged = os.listdir(tmp_path / "gbp_mirror_src")
    assert len(crops) == 2, f"one crop per distinct content, got {crops}"
    assert len(staged) == 2, f"one staged source per distinct content, got {staged}"
    assert not any(n.startswith(".mirror_src_") for n in staged), \
        "atomic write leaves no temp litter"

    # Same-content repeat is stable: a third same.jpg with A's bytes (and an
    # ancient mtime) reuses A's exact crop and hosted URL, adding no new files.
    third = tmp_path / "third" / "same.jpg"
    third.parent.mkdir(exist_ok=True)
    third.write_bytes(bytes_a)
    os.utime(str(third), (old, old))
    draft_c = _draft()
    draft_c.creative_path = str(third)
    draft_c.creative_public_url = draft_a.source_media_url
    draft_c.source_media_url = draft_a.source_media_url
    prov_c = gm.cropped_with_provenance(draft_c, ctx, str(tmp_path), log)
    assert prov_c["url"] == prov_a["url"]
    assert delivered[prov_c["url"]] == delivered[prov_a["url"]]
    assert len(os.listdir(tmp_path / "gbp_crops")) == 2
    assert len(os.listdir(tmp_path / "gbp_mirror_src")) == 2


def test_served_source_mismatch_holds_before_any_crop_is_cached(
        monkeypatch, tmp_path):
    served, delivered, draft_a, draft_b, _ba, _bb = \
        _provenance_lane(monkeypatch, tmp_path)
    ctx, log = _ctx(), lambda m: None
    # The served source no longer matches the local still.
    served[draft_a.source_media_url] = b"tampered-bytes"
    assert gm.cropped_with_provenance(draft_a, ctx, str(tmp_path), log) is None
    assert delivered == {}, "an unverifiable source never reaches the crop lane"
    assert not (tmp_path / "gbp_crops").exists() or \
        os.listdir(tmp_path / "gbp_crops") == []
