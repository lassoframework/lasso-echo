import copy
import json
import stat

from scripts import hold_future_media_repeats as hold


def _row(row_id, gym, day, *, account="instagram", status="pending"):
    return {
        "id": row_id, "gym_id": gym, "post_date": day, "status": status,
        "variant_status": "active", "account": account, "format": "feed",
        "caption": f"Caption for {row_id}, keep as written.",
        "image_url": f"https://cdn.test/{row_id}.jpg", "source_media_url": None,
        "source_media_asset_id": None, "media_not_ready_reason": None,
        "created_at": "2026-09-30T12:00:00Z", "published_at": None,
        "late_post_id": None, "publish_claim_token": None,
        "publish_reservation_day": None,
    }


class MemoryStore:
    def __init__(self, rows):
        self.rows = {row["id"]: copy.deepcopy(row) for row in rows}
        self.http = MemoryHttp(self)

    def list_future_media_maintenance_rows(self, start, end):
        assert start == min("2026-10-03", "2026-10-04")
        assert end == "2027-10-03"
        return [copy.deepcopy(row) for row in self.rows.values()]

    def get_row(self, gym_id, row_id):
        row = self.rows.get(row_id)
        if row is None or row["gym_id"] != gym_id:
            return None
        return copy.deepcopy(row)

    def _client(self):
        return self.http

    def _rest(self, table):
        return f"https://supabase.test/rest/v1/{table}"

    def _headers(self, extra=None):
        return dict(extra or {})


class MemoryHttp:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.after_patch = None

    def patch(self, url, *, params, json, **kwargs):
        self.calls.append((url, copy.deepcopy(params), copy.deepcopy(json)))
        assert set(json) == {"media_not_ready_reason"}
        matches = []
        for row in self.store.rows.values():
            if all((row.get(field) is None if predicate == "is.null"
                    else str(row.get(field)) == predicate[3:])
                   for field, predicate in params.items()):
                matches.append(row)
        if len(matches) == 1:
            matches[0]["media_not_ready_reason"] = json["media_not_ready_reason"]
        if self.after_patch is not None:
            self.after_patch(len(self.calls))
        return Response(copy.deepcopy(matches))


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class MemoryMediaStore:
    def __init__(self, assets):
        self.assets = assets

    def get_asset(self, asset_id):
        return copy.deepcopy(self.assets.get(asset_id))


def fixture():
    specs = copy.deepcopy(hold.TARGETS)
    rows = {}
    assets = {}
    for spec in specs:
        row = _row(spec["id"], spec["gym_id"], spec["post_date"],
                   account=spec["account"], status=spec["status"])
        if spec["source_media_url_md5"] is not None:
            row["source_media_url"] = f"https://source.test/{spec['id']}.jpg"
        if spec["source_media_asset_id"] is not None:
            row["source_media_asset_id"] = spec["source_media_asset_id"]
            assets[spec["source_media_asset_id"]] = {
                "id": spec["source_media_asset_id"], "gym_id": spec["gym_id"],
                "content_hash": spec["content_hash"],
            }
        rows[spec["id"]] = row

    for spec in specs:
        target = rows[spec["id"]]
        for prior_id, prior_day, keys in spec["earlier"]:
            prior = rows.setdefault(prior_id, _row(prior_id, spec["gym_id"], prior_day))
            if "image_url" in keys:
                prior["image_url"] = target["image_url"]
            if "source_media_url" in keys:
                prior["source_media_url"] = target["source_media_url"]
            if "asset_hash" in keys:
                prior["source_media_asset_id"] = spec["source_media_asset_id"]

    for spec in specs:
        target = rows[spec["id"]]
        spec["image_url_md5"] = hold._url_md5(target["image_url"])
        spec["source_media_url_md5"] = hold._url_md5(target["source_media_url"])
    return specs, rows, assets


def configured(monkeypatch):
    specs, rows, assets = fixture()
    monkeypatch.setattr(hold, "TARGETS", tuple(specs))
    return MemoryStore(rows.values()), MemoryMediaStore(assets), rows


def test_dry_run_revalidates_all_13_ids_and_prior_cross_date_identity(monkeypatch):
    store, media, _ = configured(monkeypatch)
    result = hold.run(store=store, media_store=media, today="2026-10-03")
    preflight = result["preflight"]
    assert result["ok"] and result["dry_run"]
    assert preflight["target_count"] == 13
    assert preflight["pending_count"] == 9
    assert preflight["approved_count"] == 4
    assert len(preflight["match_evidence"]) == 13
    assert all(entry["earlier_matches"] for entry in preflight["match_evidence"])
    assert len({p["id"] for p in preflight["target_proofs"]}) == 13
    assert not store.http.calls


def test_apply_requires_flag_digest_write_ahead_receipt_and_preserves_everything_else(
        tmp_path, monkeypatch):
    store, media, before = configured(monkeypatch)
    dry = hold.run(store=store, media_store=media, today="2026-10-03")
    receipt = tmp_path / "hold.json"
    monkeypatch.setenv("ECHO_CROSS_DATE_MEDIA_REPEAT_HOLD_ENABLED", "true")
    applied = hold.run(store=store, media_store=media, today="2026-10-03", apply=True,
                       expected_digest=dry["preflight"]["target_digest"],
                       receipt_path=receipt)
    assert applied["ok"] is True
    assert len(store.http.calls) == 13
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    saved = json.loads(receipt.read_text())
    assert saved["state"] == "readback_verified"
    assert len(saved["changed_ids"]) == 13
    target_ids = {spec["id"] for spec in hold.TARGETS}
    assert all(store.rows[row_id]["media_not_ready_reason"] == hold.REASON
               for row_id in target_ids)
    for row_id, old in before.items():
        after = store.rows[row_id]
        for field in hold._ROW_FIELDS:
            if field != "media_not_ready_reason":
                assert after[field] == old[field]
    assert all(set(payload) == {"media_not_ready_reason"}
               for _, _, payload in store.http.calls)
    # Private proof stores fingerprints and exact identities, not captions or URLs.
    serialized = receipt.read_text()
    assert "Caption for" not in serialized
    assert "https://" not in serialized


def test_apply_digest_stales_on_any_row_edit_before_write(tmp_path, monkeypatch):
    store, media, _ = configured(monkeypatch)
    dry = hold.run(store=store, media_store=media, today="2026-10-03")
    first = hold.TARGETS[0]["id"]
    store.rows[first]["caption"] = "Concurrent human edit"
    monkeypatch.setenv("ECHO_CROSS_DATE_MEDIA_REPEAT_HOLD_ENABLED", "true")
    receipt = tmp_path / "hold.json"
    result = hold.run(store=store, media_store=media, today="2026-10-03", apply=True,
                      expected_digest=dry["preflight"]["target_digest"],
                      receipt_path=receipt)
    assert result["ok"] is False
    assert result["reason"] == "exact dry-run digest and new private receipt required"
    assert store.http.calls == []
    assert not receipt.exists()


def test_preflight_fails_closed_on_changed_repeat_source_or_claim(tmp_path, monkeypatch):
    store, media, _ = configured(monkeypatch)
    cross_url = hold.TARGETS[0]
    prior_id = cross_url["earlier"][0][0]
    store.rows[prior_id]["source_media_url"] = "https://source.test/changed.jpg"
    result = hold.run(store=store, media_store=media, today="2026-10-03")
    assert result["ok"] is False
    assert result["reason"].startswith("preflight failed:")
    assert store.http.calls == []

    store, media, _ = configured(monkeypatch)
    target_id = hold.TARGETS[0]["id"]
    store.rows[target_id]["publish_claim_token"] = "claimed"
    result = hold.run(store=store, media_store=media, today="2026-10-03")
    assert result["ok"] is False
    assert store.http.calls == []


def test_cas_filters_exact_claim_and_snapshot_fields_and_changes_one_column(monkeypatch):
    store, _, rows = configured(monkeypatch)
    before = rows[hold.TARGETS[0]["id"]]
    result = hold._hold_exact(store, before)
    assert result["status"] == before["status"]
    assert result["caption"] == before["caption"]
    assert result["post_date"] == before["post_date"]
    assert store.http.calls[0][1]["publish_claim_token"] == "is.null"
    assert store.http.calls[0][1]["publish_reservation_day"] == "is.null"
    assert store.http.calls[0][1]["caption"] == f"eq.{before['caption']}"
    assert store.http.calls[0][2] == {"media_not_ready_reason": hold.REASON}


def test_each_patch_rechecks_earlier_media_evidence(tmp_path, monkeypatch):
    store, media, _ = configured(monkeypatch)
    dry = hold.run(store=store, media_store=media, today="2026-10-03")
    ordered = sorted(hold.TARGETS, key=lambda spec: (spec["post_date"], spec["gym_id"], spec["id"]))
    first, second = ordered[:2]
    asset_id = second["source_media_asset_id"]

    def change_prior_after_first_patch(call_count):
        if call_count == 1:
            media.assets[asset_id]["content_hash"] = "concurrently-restaged-hash"

    store.http.after_patch = change_prior_after_first_patch
    monkeypatch.setenv("ECHO_CROSS_DATE_MEDIA_REPEAT_HOLD_ENABLED", "true")
    receipt = tmp_path / "hold.json"
    result = hold.run(store=store, media_store=media, today="2026-10-03", apply=True,
                      expected_digest=dry["preflight"]["target_digest"],
                      receipt_path=receipt)
    assert result["ok"] is False
    assert first["id"] in result["changed_ids"]
    assert second["id"] in result["conflict_ids"]
    assert store.rows[second["id"]]["media_not_ready_reason"] is None
