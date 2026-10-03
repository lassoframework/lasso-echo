"""Store-level contract for a newly hosted raw source and its rendition."""
import hashlib
import uuid

import pytest

from agent import config, portal_calendar_store as pcs
from agent import visual_owner_receipts, visual_writer_prepare as prep


TENANT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
RAW = "https://media.example/new-raw.jpg"
BURN = "https://media.example/new-burn.jpg"
SOURCE = b"fresh raw pixels"
DELIVERED = b"fresh rendered pixels"


def md5(data):
    return "md5:" + hashlib.md5(data).hexdigest()


class Response:
    status_code = 200
    text = ""

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class RPC:
    def __init__(self, asset=None):
        self.asset = asset
        self.aliases = {}
        self.calls = []
        self.row = {"id": "row-1", "gym_id": "gym", "status": "pending",
                    "format": "story", "variant_status": "active",
                    "image_url": "https://media.example/old.jpg",
                    "source_media_url": "https://media.example/old-raw.jpg"}
        self.receipts = None

    def get(self, url, *, params, headers, timeout):
        table = url.rsplit("/", 1)[-1]
        self.calls.append(("get", table))
        if table == "tenant_alias":
            key = params["alias_key"][3:]
            return Response([{"alias_key": key, "tenant_id": OTHER if key == "foreign" else TENANT}])
        if table == "media_asset":
            return Response([self.asset] if self.asset else [])
        if table == "visual_group_alias":
            key = (params["gym_id"][3:], params["alias_kind"][3:],
                   params["alias_value"][3:])
            group = self.aliases.get(key)
            return Response([{"group_key": group}] if group else [])
        if table == "content_calendar":
            return Response([self.row.copy()])
        raise AssertionError(table)

    def post(self, url, *, headers, json, timeout):
        rpc = url.rsplit("/", 1)[-1]
        self.calls.append(("post", rpc, json))
        assert json["p_tenant"] == TENANT
        if rpc == "visual_global_prepare_bundle":
            assert json["p_fingerprint"] == md5(SOURCE)
            assert json["p_evidence"]["verified_bytes"] == md5(SOURCE)
            assert json["p_evidence"]["delivered_url"] == RAW
            aliases = {(item["alias_kind"], item["alias_value"])
                       for item in json["p_aliases"]}
            assert ("canonical_url", RAW) in aliases
            assert ("byte_hash", "derived:" + md5(SOURCE)) in aliases
            if self.asset:
                assert ("source_asset", self.asset["id"]) in aliases
                assert json["p_asset_id"] == self.asset["id"]
            else:
                assert json["p_asset_id"] is None
            self.aliases[(TENANT, "canonical_url", RAW)] = "vg_fresh"
            return Response({"group_key": "vg_fresh", "fingerprint": md5(SOURCE)})
        assert rpc == "visual_global_prepare_source_rendition"
        assert self.aliases[(TENANT, "canonical_url", RAW)] == json["p_group_key"]
        assert json["p_group_key"] == "vg_fresh"
        assert self.receipts is not None
        assert all(json[key] == self.receipts[receipt] for key, receipt in (
            ("p_source_read_receipt", "source_read_receipt"),
            ("p_delivered_read_receipt", "delivered_read_receipt"),
            ("p_render_receipt", "render_receipt")))
        self.aliases[(TENANT, "canonical_url", BURN)] = "vg_fresh"
        return Response({"group_key": "vg_fresh", "source_fingerprint": md5(SOURCE),
                         "delivered_fingerprint": md5(DELIVERED), "usage_claimed": False})

    def patch(self, url, *, params, headers, json, timeout):
        self.calls.append(("patch", url.rsplit("/", 1)[-1], json))
        assert params["id"] == "eq.row-1" and params["gym_id"] == "eq.gym"
        self.row.update(json)
        return Response([self.row.copy()])


def evidence():
    return {"source_exact_url": RAW, "delivered_exact_url": BURN,
            "source_fingerprint": md5(SOURCE), "delivered_fingerprint": md5(DELIVERED),
            "source_byte_length": len(SOURCE), "delivered_byte_length": len(DELIVERED),
            "operation": "reburn", "evidence_ref": "test:render-1",
            "observed_by": "test", "rendered_by": "test"}


def setup(monkeypatch, rpc):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://media.example")
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: {RAW: SOURCE, BURN: DELIVERED}[url])

    def receipt_writer(**kwargs):
        assert kwargs == {"tenant": TENANT, "group_key": "vg_fresh",
                          "source_bytes": SOURCE, "delivered_bytes": DELIVERED,
                          "render_evidence": evidence(),
                          "asset_id": rpc.asset["id"] if rpc.asset else None}
        rpc.receipts = {key: str(uuid.uuid4()) for key in (
            "source_read_receipt", "delivered_read_receipt", "render_receipt")}
        return rpc.receipts

    monkeypatch.setattr(visual_owner_receipts, "default_writer", lambda: receipt_writer)
    return pcs.SupabaseCalendarStore(url="https://db.example", service_key="test", http=rpc)


@pytest.mark.parametrize("asset", [None, {"id": "asset-1", "gym_id": "gym",
                                          "content_hash": hashlib.md5(SOURCE).hexdigest()}])
def test_fresh_rendered_swap_registers_raw_before_receipts_and_calendar_patch(monkeypatch, asset):
    rpc = RPC(asset)
    store = setup(monkeypatch, rpc)
    extra = {"source_media_asset_id": "asset-1"} if asset else None
    row = store.swap_media("gym", "row-1", BURN, source_media_url=RAW,
                           extra_fields=extra, render_evidence=evidence())
    assert row["source_media_url"] == RAW
    assert row["image_url"] == BURN
    assert row["visual_group_key"] == "vg_fresh"
    assert row["byte_hash"] == "derived:" + md5(DELIVERED)
    assert [call[1] for call in rpc.calls if call[0] == "post"] == [
        "visual_global_prepare_bundle", "visual_global_prepare_source_rendition"]
    assert rpc.calls[-1][0] == "patch"


@pytest.mark.parametrize("asset", [
    {"id": "asset-1", "gym_id": "gym", "content_hash": hashlib.md5(b"wrong").hexdigest()},
    {"id": "asset-1", "gym_id": "foreign", "content_hash": hashlib.md5(SOURCE).hexdigest()},
])
def test_fresh_swap_rejects_mismatched_asset_bytes_or_tenant_before_registration(monkeypatch, asset):
    rpc = RPC(asset)
    store = setup(monkeypatch, rpc)
    with pytest.raises(prep.VisualPreparationError, match="MD5|tenant"):
        store.swap_media("gym", "row-1", BURN, source_media_url=RAW,
                         extra_fields={"source_media_asset_id": "asset-1"},
                         render_evidence=evidence())
    assert not any(call[0] in ("post", "patch") for call in rpc.calls)


def test_fresh_swap_rejects_render_evidence_for_other_bytes(monkeypatch):
    rpc = RPC()
    store = setup(monkeypatch, rpc)
    wrong = {**evidence(), "source_fingerprint": md5(b"wrong")}
    with pytest.raises(prep.VisualPreparationError, match="rendition lineage"):
        store.swap_media("gym", "row-1", BURN, source_media_url=RAW,
                         render_evidence=wrong)
    assert not any(call[0] in ("post", "patch") for call in rpc.calls)


def test_flag_off_keeps_swap_contract_without_visual_registration(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    rpc = RPC()
    store = pcs.SupabaseCalendarStore(url="https://db.example", service_key="test", http=rpc)
    row = store.swap_media("gym", "row-1", BURN, source_media_url=RAW)
    assert row["image_url"] == BURN and row["source_media_url"] == RAW
    assert [call[0] for call in rpc.calls] == ["patch"]
