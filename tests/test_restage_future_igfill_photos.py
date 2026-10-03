"""Offline acceptance for the exact receipt-bound photo replacement operator."""
import copy
import json
import stat

import pytest

from agent.portal_calendar_store import SupabaseCalendarStore, PortalStoreError
from scripts import hold_future_igfill_photos as hold
from scripts import restage_future_igfill_photos as op


def row(id_, *, gym="gym", day="2026-10-15", status="pending", image=None):
    return {"id": id_, "gym_id": gym, "post_date": day, "status": status,
            "variant_status": "active", "account": "instagram", "format": "feed",
            "caption": "keep this exact caption", "image_url": image or f"https://cdn/igfill_{day}_card.png",
            "source_media_url": None, "source_media_asset_id": None,
            "media_not_ready_reason": hold.REASON, "created_at": "2026-10-01T00:00:00Z",
            "published_at": None, "late_post_id": None, "thumbnail_url": None,
            "publish_claim_token": None, "publish_reservation_day": None, "slot_index": 0,
            "scheduled_at": f"{day}T14:00:00Z"}


def asset(id_, *, gym="gym", digest="a" * 64, **overrides):
    return {"id": id_, "gym_id": gym, "kind": "photo", "eligible": True,
            "excluded_by_coach": False, "review_status": "approved", "reviewed_by": "scanner",
            "reviewed_at": "2026-10-02T00:00:00Z", "content_hash": digest,
            "review_content_hash": digest, "moderation_status": "clean", "people_detected": True,
            "moderation_json": {"verdict": "clean", "provider": "scanner", "content_hash": digest,
                                "asset_id": id_, "gym_id": gym, "people_detected": True,
                                "observed_at": "2026-10-02T00:00:00Z"},
            "used_count": 0, "last_used_at": None, **overrides}


class Media:
    def __init__(self, assets):
        self.assets = copy.deepcopy(assets)
    def available(self):
        return True
    def list_assets(self, gym):
        return copy.deepcopy(self.assets)
    def get_asset(self, id_):
        return next((copy.deepcopy(a) for a in self.assets if a["id"] == id_), None)


class Calendar:
    def __init__(self, rows):
        self.rows = {r["id"]: copy.deepcopy(r) for r in rows}
        self.calls = []
        self.conflict_id = None
        self.readback_bad = False
        self.writer_result = None
        self.evidence = {}
    def get_row(self, gym, id_):
        r = copy.deepcopy(self.rows.get(id_))
        if r and r["gym_id"] != gym:
            return None
        if self.readback_bad and self.calls:
            r["caption"] = "raced"
        return r
    def list_photo_restage_book(self, gym):
        return [copy.deepcopy(r) for r in self.rows.values() if r["gym_id"] == gym]
    def replace_future_infographic_media(self, gym, before, *, reason, render_evidence=None, **payload):
        self.calls.append(before["id"])
        assert reason == hold.REASON
        if before["id"] == self.conflict_id or self.rows[before["id"]] != before:
            return None
        self.rows[before["id"]].update(payload, media_not_ready_reason=None)
        self.evidence[before["id"]] = render_evidence
        if self.writer_result:
            self.rows[before["id"]].update(self.writer_result)
        return copy.deepcopy(self.rows[before["id"]])


def receipt(tmp_path, rows):
    before = [{**hold._image(r), "media_not_ready_reason": None} for r in rows]
    path = tmp_path / "original.json"
    hold._receipt(path, {"operation": "hold_future_igfill_photos", "reason": hold.REASON,
                        "state": "readback_verified", "before_image": before,
                        "readback": [hold._image(r) for r in rows],
                        "changed_ids": [r["id"] for r in rows], "target_digest": hold._digest(before),
                        "target_count": len(rows)}, create=True)
    return path


def picker(monkeypatch):
    monkeypatch.setattr(op.selector, "pickable", lambda gym, kind, **kw:
                        [a for a in kw["store"].list_assets(gym) if a["gym_id"] == gym])


def prepare(gym, root, *, siblings, candidates_fn, **kwargs):
    candidate = candidates_fn(gym, root)[0]
    def variant(r):
        return {"ok": True, "kind": "photo", "source": "drive", "image_url": f'https://cdn/{r["id"]}.jpg',
                "source_media_url": "https://cdn/source.jpg", "thumbnail_url": "",
                "source_media_asset_id": candidate["key"]}
    return {**variant(root), "siblings": {r["id"]: variant(r) for r in siblings}}


def invoke(tmp_path, monkeypatch, rows, assets, *, calendar=None, **kwargs):
    picker(monkeypatch)
    calendar = calendar or Calendar(rows)
    path = receipt(tmp_path, rows)
    return calendar, path, dict(store=calendar, media_store=Media(assets), hold_receipt_path=path,
                               today="2026-10-03", **kwargs)


def test_dry_run_is_read_only_groups_same_source_and_leaves_no_photo_gym_held(tmp_path, monkeypatch):
    rows = [row("a"), row("b", status="approved"), row("c", gym="empty"),
            row("d", image="https://cdn/igfill_2026-10-15_second.png")]
    rows[1].update(account="facebook")
    calendar, path, args = invoke(tmp_path, monkeypatch, rows,
                                  [asset("first"), asset("alias"), asset("second", digest="b" * 64)])
    result = op.run(**args)
    assert result["ok"] and result["dry_run"]
    plan = result["preflight"]
    assert plan["replacement_count"] == 3 and plan["held_count"] == 1
    assert [len(g["rows"]) for g in plan["groups"]] == [2, 1]
    assert len({g["asset"]["content_hash"] for g in plan["groups"]}) == 2
    assert not calendar.calls and len(list(tmp_path.iterdir())) == 1
    assert calendar.rows["c"]["media_not_ready_reason"] == hold.REASON


@pytest.mark.parametrize("override", [{"review_status": "pending"}, {"used_count": 1},
                                       {"last_used_at": "2026-01-01"}, {"kind": "video"},
                                       {"gym_id": "foreign"}, {"used_count": None},
                                       {"review_content_hash": "different"}])
def test_only_explicitly_unused_approved_owned_photos_selected(tmp_path, monkeypatch, override):
    _, _, args = invoke(tmp_path, monkeypatch, [row("a")], [asset("bad", **override)])
    result = op.run(**args)
    assert result["preflight"]["replacement_count"] == 0
    assert result["preflight"]["held_count"] == 1


def test_book_history_excludes_hash_alias_even_if_usage_was_not_stamped(tmp_path, monkeypatch):
    rows = [row("target")]
    calendar = Calendar(rows + [{**row("historical", day="2026-01-01"), "source_media_asset_id": "used",
                                 "status": "denied", "variant_status": "archived"}])
    _, _, args = invoke(tmp_path, monkeypatch, rows,
                        [asset("used"), asset("alias"), asset("fresh", digest="b" * 64)], calendar=calendar)
    result = op.run(**args)
    assert result["preflight"]["groups"][0]["asset"]["id"] == "fresh"


def test_unowned_sibling_blocks_group(tmp_path, monkeypatch):
    rows = [row("held")]
    calendar = Calendar(rows + [{**row("other"), "media_not_ready_reason": None}])
    _, _, args = invoke(tmp_path, monkeypatch, rows, [asset("photo")], calendar=calendar)
    assert op.run(**args)["preflight"]["blocked"][0]["reason"] == "same_media_sibling_outside_hold_receipt"


def test_apply_preserves_pending_approved_caption_and_has_private_writeahead(tmp_path, monkeypatch):
    rows = [row("a", status="approved"), row("b"), row("c", gym="empty")]
    calendar, path, args = invoke(tmp_path, monkeypatch, rows, [asset("photo")])
    digest = op.run(**args)["preflight"]["target_digest"]
    output = tmp_path / "restage.json"
    reservations, settled = [], []
    def reserve(gym, root, pick, candidate, media_store):
        saved = json.loads(output.read_text())
        assert saved["state"] == "reservation_intent" and saved["prepared"]
        reservations.append(candidate["content_hash"])
        return True
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RESTAGE_ENABLED", "true")
    result = op.run(**args, apply=True, expected_digest=digest, receipt_path=output,
                    prepare_fn=prepare, reserve_fn=reserve,
                    settle_fn=lambda *a, **kw: settled.append(kw["swapped_ids"]))
    assert result["ok"] and result["changed_ids"] == ["a", "b"]
    assert reservations == ["a" * 64] and settled == [["a", "b"]]
    assert calendar.rows["a"]["status"] == "approved" and calendar.rows["b"]["status"] == "pending"
    assert all(calendar.rows[id_]["caption"] == rows[i]["caption"] for i, id_ in enumerate(("a", "b")))
    assert calendar.rows["c"]["media_not_ready_reason"] == hold.REASON
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    saved = json.loads(output.read_text())
    assert saved["state"] == "readback_verified" and len(saved["readback"]) == 2


def test_prepared_feed_story_readback_uses_writer_lineage_and_receipt(tmp_path, monkeypatch):
    from agent import visual_writer_prepare
    rows = [row("feed"), {**row("story"), "format": "story"}]
    calendar, _, args = invoke(tmp_path, monkeypatch, rows, [asset("photo")])
    calendar.writer_result = {"visual_group_key": "vg_verified", "byte_hash": "derived:md5:abc"}
    digest = op.run(**args)["preflight"]["target_digest"]

    def prepared(gym, root, *, siblings, candidates_fn, **kwargs):
        candidate = candidates_fn(gym, root)[0]
        def variant(target):
            return {"ok": True, "kind": "photo", "source": "drive",
                    "image_url": f"https://cdn/{target['id']}.jpg",
                    "source_media_url": "https://cdn/source.jpg", "thumbnail_url": "",
                    "source_media_asset_id": candidate["key"],
                    "render_evidence": {"variant": target["format"]}}
        return {**variant(root), "siblings": {target["id"]: variant(target) for target in siblings}}

    monkeypatch.setattr(visual_writer_prepare, "enabled", lambda: True)
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RESTAGE_ENABLED", "true")
    output = tmp_path / "restage.json"
    result = op.run(**args, apply=True, expected_digest=digest, receipt_path=output,
                    prepare_fn=prepared, reserve_fn=lambda *a: True,
                    settle_fn=lambda *a, **kw: None)
    assert result["ok"]
    assert calendar.evidence == {"feed": {"variant": "feed"}, "story": {"variant": "story"}}
    readback = json.loads(output.read_text())["readback"]
    assert all(entry["visual_group_key"] == "vg_verified" and
               entry["byte_hash"] == "derived:md5:abc" for entry in readback)


def test_apply_stops_on_conflict_retaining_remaining_hold_and_claim(tmp_path, monkeypatch):
    rows = [row("a"), row("b"), row("later", day="2026-10-16")]
    calendar, _, args = invoke(tmp_path, monkeypatch, rows,
                               [asset("photo"), asset("next", digest="b" * 64)])
    digest = op.run(**args)["preflight"]["target_digest"]
    calendar.conflict_id = "b"
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RESTAGE_ENABLED", "true")
    output = tmp_path / "restage.json"
    result = op.run(**args, apply=True, expected_digest=digest, receipt_path=output,
                    prepare_fn=prepare, reserve_fn=lambda *a: True, settle_fn=lambda *a, **kw: None)
    assert not result["ok"] and result["changed_ids"] == ["a"]
    assert calendar.calls == ["a", "b"]
    assert calendar.rows["b"]["media_not_ready_reason"] == hold.REASON
    assert calendar.rows["later"]["media_not_ready_reason"] == hold.REASON
    assert json.loads(output.read_text())["state"] == "reconcile_required"


def test_digest_flag_and_receipt_revalidation_precede_any_side_effect(tmp_path, monkeypatch):
    calendar, _, args = invoke(tmp_path, monkeypatch, [row("a")], [asset("photo")])
    digest = op.run(**args)["preflight"]["target_digest"]
    def forbidden(*a, **kw):
        pytest.fail("unexpected preparation")
    output = tmp_path / "out.json"
    assert not op.run(**args, apply=True, expected_digest=digest, receipt_path=output, prepare_fn=forbidden)["ok"]
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RESTAGE_ENABLED", "true")
    assert not op.run(**args, apply=True, expected_digest="wrong", receipt_path=output, prepare_fn=forbidden)["ok"]
    calendar.rows["a"]["caption"] = "changed"
    assert not op.run(**args, apply=True, expected_digest=digest, receipt_path=output, prepare_fn=forbidden)["ok"]
    assert not calendar.calls and not output.exists()


def test_row_store_atomic_payload_and_exact_predicates_preserve_approval():
    before = row("approved", status="approved")
    class Response:
        status_code = 200
        def __init__(self, data): self.data = data
        def json(self): return self.data
    class Http:
        calls = []
        def patch(self, url, **kw):
            self.calls.append(kw)
            return Response([{**before, **kw["json"]}])
    http = Http()
    store = SupabaseCalendarStore(url="https://test.supabase.co", service_key="fake", http=http)
    result = store.replace_future_infographic_media("gym", before, image_url="https://cdn/photo.jpg",
        source_media_url=None, source_media_asset_id="approved-photo", reason=hold.REASON)
    assert result["status"] == "approved"
    call = http.calls[0]
    assert set(call["params"]) == set(op._ROW_FIELDS)
    assert call["params"]["caption"] == "eq.keep this exact caption"
    assert call["params"]["status"] == "eq.approved"
    assert call["params"]["media_not_ready_reason"] == f"eq.{hold.REASON}"
    assert call["json"]["media_not_ready_reason"] is None
    assert call["json"]["image_url"] == "https://cdn/photo.jpg"
    assert "status" not in call["json"] and "caption" not in call["json"]


@pytest.mark.parametrize("change", [{"media_not_ready_reason": "other owner hold"},
                                    {"status": "publishing"}, {"publish_claim_token": "claimed"},
                                    {"publish_reservation_day": "2026-10-15"},
                                    {"caption": None, "image_url": "https://cdn/real-photo.jpg"}])
def test_store_rejects_unowned_claimed_or_nonplaceholder_row(change):
    class Http:
        def patch(self, *a, **kw): pytest.fail("unexpected patch")
    store = SupabaseCalendarStore(url="https://test.supabase.co", service_key="fake", http=Http())
    assert store.replace_future_infographic_media("gym", {**row("id"), **change},
        image_url="https://cdn/photo.jpg", source_media_url=None,
        source_media_asset_id="photo", reason=hold.REASON) is None


def test_complete_book_pagination_fails_closed():
    class Response:
        status_code = 200
        def __init__(self, data): self.data = data
        def json(self): return self.data
    class Http:
        calls = []
        def get(self, url, **kw):
            self.calls.append(kw["params"])
            if len(self.calls) == 1:
                return Response([row(f"{i:04}") for i in range(500)])
            return Response([row("0499")])
    http = Http()
    store = SupabaseCalendarStore(url="https://test.supabase.co", service_key="fake", http=http)
    with pytest.raises(PortalStoreError, match="pagination stalled"):
        store.list_photo_restage_book("gym")
    assert http.calls[1]["id"] == "gt.0499"


def test_reserve_rechecks_approved_asset_snapshot_before_claim(monkeypatch):
    a = asset("photo")
    media = Media([{**a, "used_count": 1}])
    monkeypatch.setattr(op.selector, "claim_drive_content", lambda *a: pytest.fail("unexpected claim"))
    assert not op._reserve("gym", row("a"), {}, a, media)


@pytest.mark.parametrize("algorithm", ["md5", "sha256"])
def test_original_download_bytes_are_bound_to_review_before_render(tmp_path, algorithm):
    import hashlib
    class Drive:
        def available(self): return True
        def download(self, id_, path):
            path.write_bytes(b"reviewed original photo")
    digest = getattr(hashlib, algorithm)(b"reviewed original photo").hexdigest()
    path = tmp_path / "photo.jpg"
    wrapper = op._ReviewedDrive(Drive(), asset("photo", digest=digest))
    assert wrapper.available()
    wrapper.download("photo", path)
    with pytest.raises(ValueError, match="approved bytes"):
        op._ReviewedDrive(Drive(), asset("photo", digest="0" * len(digest))).download("photo", path)


def test_readback_failure_stops_and_records_exact_uncertain_row(tmp_path, monkeypatch):
    calendar, _, args = invoke(tmp_path, monkeypatch, [row("a"), row("b")], [asset("photo")])
    digest = op.run(**args)["preflight"]["target_digest"]
    calendar.readback_bad = True
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RESTAGE_ENABLED", "true")
    result = op.run(**args, apply=True, expected_digest=digest, receipt_path=tmp_path / "out.json",
                    prepare_fn=prepare, reserve_fn=lambda *a: True, settle_fn=lambda *a, **kw: None)
    assert not result["ok"] and result["inflight_ids"] == ["a", "b"]
    assert calendar.calls == ["a"] and calendar.rows["b"]["media_not_ready_reason"] == hold.REASON


def test_legacy_url_and_reframe_are_permanent_repeat_evidence(tmp_path, monkeypatch):
    import hashlib
    rows = [row("target")]
    historical = {**row("legacy", day="2026-01-01", image="https://cdn/original.jpg"),
                  "variant_status": "archived", "status": "denied"}
    calendar = Calendar(rows + [historical])
    _, _, args = invoke(tmp_path, monkeypatch, rows, [asset("photo", title="original.jpg")], calendar=calendar)
    assert op.run(**args)["preflight"]["replacement_count"] == 0
    class Drive:
        def download(self, id_, path): path.write_bytes(b"photo bytes")
    content = hashlib.md5(b"photo bytes").hexdigest()
    reframe = f'{hashlib.sha256(b"photo bytes").hexdigest()[:12]}__feed.jpg'
    with pytest.raises(ValueError, match="calendar reframe"):
        op._ReviewedDrive(Drive(), asset("photo", digest=content), [reframe]).download("photo", tmp_path / "photo.jpg")


def test_private_receipt_tamper_and_unavailable_inventory_abort(tmp_path, monkeypatch):
    _, path, args = invoke(tmp_path, monkeypatch, [row("target")], [asset("photo")])
    args["media_store"].available = lambda: False
    with pytest.raises(ValueError, match="inventory unavailable"):
        op.run(**args)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        op.run(**args)


def test_expired_matching_receipt_row_is_digest_bound_blocked_and_future_rows_apply(tmp_path, monkeypatch):
    rows = [row("expired", day="2026-10-02", status="approved"), row("future", day="2026-10-15")]
    calendar, _, args = invoke(tmp_path, monkeypatch, rows, [asset("photo")])
    dry = op.run(**args)
    plan = dry["preflight"]
    assert dry["ok"] and plan["conflict_ids"] == []
    assert plan["blocked"][0]["reason"] == "blocked_expired"
    assert plan["blocked_expired"] == [rows[0]]
    assert plan["target_count"] == 2 and plan["replacement_count"] == 1 and plan["held_count"] == 1
    monkeypatch.setenv("ECHO_FUTURE_IGFILL_RESTAGE_ENABLED", "true")
    result = op.run(**args, apply=True, expected_digest=plan["target_digest"],
                    receipt_path=tmp_path / "out.json", prepare_fn=prepare,
                    reserve_fn=lambda *a: True, settle_fn=lambda *a, **kw: None)
    assert result["ok"] and result["changed_ids"] == ["future"]
    assert calendar.rows["expired"] == rows[0]
    assert result["held"][0]["reason"] == "blocked_expired"


def test_expired_row_identity_change_remains_true_conflict(tmp_path, monkeypatch):
    rows = [row("expired", day="2026-10-02"), row("future")]
    calendar, _, args = invoke(tmp_path, monkeypatch, rows, [asset("photo")])
    calendar.rows["expired"]["caption"] = "concurrent edit"
    result = op.run(**args)
    assert not result["ok"] and result["preflight"]["conflict_ids"] == ["expired"]
    assert result["preflight"]["blocked_expired"] == []


def test_host_verification_rejects_stale_delivered_object_even_at_expected_url(tmp_path, monkeypatch):
    from agent import media_host
    monkeypatch.setattr(media_host.config, "S3_PUBLIC_BASE_URL", "https://cdn.test")
    image = tmp_path / "prepared.jpg"
    image.write_bytes(b"prepared reviewed pixels")
    url = media_host.public_url_for(media_host.key_for(image, "gym"))
    class Response:
        status_code = 200
        closed = False
        def __init__(self, value): self.value = value
        def iter_content(self, chunk_size): return iter([self.value])
        def close(self): self.closed = True
    class Http:
        def __init__(self, response): self.response = response
        def get(self, observed, **kwargs):
            assert observed == url and kwargs["allow_redirects"] is False
            return self.response
    delivered = Response(image.read_bytes())
    assert op._host_verified(image, "gym", host_fn=lambda *a: url, http=Http(delivered)) == url
    assert delivered.closed
    stale = Response(b"previous unreviewed photo")
    with pytest.raises(ValueError, match="differs from prepared bytes|exceeded bounds"):
        op._host_verified(image, "gym", host_fn=lambda *a: url, http=Http(stale))
    assert stale.closed


@pytest.mark.parametrize("heic", [False, True])
def test_prepare_ignores_stale_rendition_and_renders_all_variants_from_reviewed_original(tmp_path, monkeypatch, heic):
    from io import BytesIO
    from PIL import Image
    from agent import config, feed_image, gym_media_index, story_image
    from agent.integrations import drive_client
    picture = BytesIO()
    Image.new("RGB", (300, 200), "red").save(picture, format="JPEG")
    original_bytes = picture.getvalue()
    import hashlib
    photo = asset("photo", digest=hashlib.md5(original_bytes).hexdigest(),
                  title="original.heic" if heic else "original.jpg",
                  mime_type="image/heic" if heic else "image/jpeg",
                  rendition_url="https://cdn/stale-blue-photo.jpg", rendition_key="old-key")
    feed_row, story_row = row("feed"), {**row("story"), "format": "story", "caption": "preserve story caption"}
    calendar = Calendar([feed_row, story_row])
    class Drive:
        def available(self): return True
        def download(self, id_, path):
            assert id_ == "photo"
            with open(path, "wb") as output: output.write(original_bytes)
    monkeypatch.setattr(drive_client, "DriveClient", Drive)
    monkeypatch.setattr(config, "story_format_enabled", lambda: True)
    monkeypatch.setattr(config, "story_source_media_enabled", lambda: True)
    monkeypatch.setattr(gym_media_index, "ensure_rendition", lambda *a, **kw: pytest.fail("old rendition route"))
    conversions, renders, hosted = [], [], {}
    def convert(source, output):
        assert Path(source).read_bytes() == original_bytes
        conversions.append(source)
        Image.open(source).save(output, format="JPEG")
    from pathlib import Path
    monkeypatch.setattr(gym_media_index, "heic_to_jpeg", convert)
    def render_feed(source, directory):
        assert Image.open(source).getpixel((0, 0))[0] > 200
        output = Path(directory) / "fresh-feed.jpg"
        Image.open(source).resize((1080, 1350)).save(output)
        renders.append("feed")
        return str(output)
    def render_story(source, caption, gym, directory):
        assert caption == "preserve story caption"
        assert Image.open(source).getpixel((0, 0))[0] > 200
        output = Path(directory) / "fresh-story.jpg"
        Image.open(source).resize((1080, 1920)).save(output)
        renders.append("story")
        return str(output)
    def host(path, gym):
        content = Path(path).read_bytes()
        url = f'https://cdn/{hashlib.sha256(content).hexdigest()}.jpg'
        hosted[url] = content
        return url
    monkeypatch.setattr(feed_image, "get_or_make_feed_image", render_feed)
    monkeypatch.setattr(story_image, "get_or_make_story_image", render_story)
    monkeypatch.setattr(op, "_host_verified", host)
    pick = op._prepare("gym", feed_row, store=calendar, media_store=Media([photo]),
                       siblings=[story_row], candidates_fn=lambda *a: [op._candidate(photo)])
    assert pick["ok"] and renders == ["feed", "story"]
    assert len(conversions) == int(heic)
    assert pick["image_url"] != photo["rendition_url"]
    story = pick["siblings"]["story"]
    assert story["image_url"] != photo["rendition_url"] and story["source_media_url"] != photo["rendition_url"]
    assert Image.open(BytesIO(hosted[pick["image_url"]])).size == (1080, 1350)
    assert Image.open(BytesIO(hosted[story["image_url"]])).size == (1080, 1920)
    assert all(Image.open(BytesIO(content)).getpixel((0, 0))[0] > 200 for content in hosted.values())
    assert photo["rendition_url"] == "https://cdn/stale-blue-photo.jpg"
