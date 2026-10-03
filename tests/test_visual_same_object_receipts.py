"""The exact same-object path requires one owner read and one guarded RPC."""
import hashlib
import uuid

import pytest

from agent import media_host, visual_owner_receipts as owner
from agent import visual_writer_prepare as prep


TENANT = "11111111-1111-4111-8111-111111111111"
URL = "https://media.example/selected.png"
RECEIPT = str(uuid.uuid4())
DATA = b"reviewed exact PNG bytes"
DIGEST = "md5:" + hashlib.md5(DATA).hexdigest()


class Response:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class HTTP:
    def __init__(self, *, group=None, rpc_result=None, asset=None):
        self.group = group
        self.rpc_result = rpc_result
        self.asset = asset
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        name = url.rsplit("/", 1)[-1]
        self.calls.append(("get", name, params))
        if name == "tenant_alias":
            return Response([{"alias_key": "lasso", "tenant_id": TENANT}])
        if name == "media_asset":
            return Response([self.asset] if self.asset else [])
        if name == "visual_group_alias":
            return Response([{"group_key": self.group}] if self.group else [])
        raise AssertionError(name)

    def post(self, url, *, headers, json, timeout):
        name = url.rsplit("/", 1)[-1]
        self.calls.append(("post", name, json))
        if name == "visual_global_prepare_bundle":
            self.group = "vg_reviewed"
            return Response({"group_key": self.group, "fingerprint": DIGEST})
        if name == "visual_global_prepare_source_rendition":
            return Response(self.rpc_result if self.rpc_result is not None else {
                "group_key": self.group, "source_fingerprint": DIGEST,
                "delivered_fingerprint": DIGEST, "usage_claimed": False})
        raise AssertionError(name)


class Store:
    def __init__(self, http):
        self.http = http

    def _client(self):
        return self.http

    def _rest(self, name):
        return "https://db.example/rest/v1/" + name

    def _headers(self, extra=None):
        return extra or {}


class Cursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params):
        self.connection.statements.append((sql, params))
        if self.connection.fail:
            raise RuntimeError("insert failed")

    def fetchone(self):
        return (RECEIPT,)


class Connection:
    def __init__(self, fail=False):
        self.fail = fail
        self.statements = []
        self.commits = self.rollbacks = self.closes = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


@pytest.fixture(autouse=True)
def host(monkeypatch):
    monkeypatch.setattr(media_host.config, "S3_PUBLIC_BASE_URL", "https://media.example")
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_OWNER_RECEIPTS", "1")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", "test-only")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_ROLE", "receipt_owner")


def test_flag_off_preserves_exact_row_and_has_no_io(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    row = {"image_url": URL}
    http = HTTP()
    assert prep.prepare_same_object(Store(http), "lasso", row) is row
    assert http.calls == []


def test_default_owner_requires_full_non_service_role_config(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_OWNER_RECEIPTS", "1")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", "test-only")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_ROLE", "service_role")
    assert owner.default_same_object_writer() is None
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_ROLE", "receipt_owner")
    assert owner.default_same_object_writer() is owner.produce_same_object
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.delenv("AGENT_VISUAL_RECEIPT_OWNER_DSN")
    http = HTTP()
    with pytest.raises(prep.VisualPreparationError, match="producer is unavailable"):
        prep.prepare_same_object(Store(http), "lasso", {"image_url": URL})
    assert http.calls == []


def test_production_owner_config_rejects_stale_byte_and_receipt_callbacks(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", "host=production.example dbname=live")
    http = HTTP()
    injected = []

    def fake_reader(url):
        injected.append(("reader", url))
        return DATA

    def fake_writer(**kwargs):
        injected.append(("writer", kwargs))
        return {"read_receipt": RECEIPT, "render_receipt": None}

    for overrides in ({"read_bytes": fake_reader}, {"receipt_writer": fake_writer},
                      {"read_bytes": fake_reader, "receipt_writer": fake_writer,
                       "isolated_test_callbacks": True}):
        with pytest.raises(prep.VisualPreparationError, match="isolated test configuration"):
            prep.prepare_same_object(Store(http), "lasso", {"image_url": URL}, **overrides)
    assert http.calls == [] and injected == []


def test_one_owner_insert_then_same_uuid_twice_with_null_render(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    http = HTTP()
    connection = Connection()
    calls = []

    def produce(**kwargs):
        calls.append(kwargs)
        return owner.produce_same_object(**kwargs, connection_factory=lambda: connection)

    row = {"image_url": URL, "source_media_url": URL, "byte_hash": "derived:" + DIGEST}
    result = prep.prepare_same_object(Store(http), "lasso", row,
                                      read_bytes=lambda url: DATA, receipt_writer=produce,
                                      isolated_test_callbacks=True)
    assert row == {"image_url": URL, "source_media_url": URL, "byte_hash": "derived:" + DIGEST}
    assert result["visual_group_key"] == "vg_reviewed"
    assert result["byte_hash"] == "derived:" + DIGEST
    assert len(connection.statements) == 1
    assert "visual_global_object_read_receipt" in connection.statements[0][0]
    assert connection.statements[0][1][:4] == (TENANT, URL, DIGEST, len(DATA))
    assert (connection.commits, connection.rollbacks, connection.closes) == (1, 0, 1)
    assert calls[0]["evidence"]["fingerprint"] == DIGEST
    posts = [call for call in http.calls if call[0] == "post"]
    assert [call[1] for call in posts] == ["visual_global_prepare_bundle",
                                          "visual_global_prepare_source_rendition"]
    rpc = posts[-1][2]
    assert rpc["p_source_read_receipt"] == rpc["p_delivered_read_receipt"] == RECEIPT
    assert rpc["p_render_receipt"] is None and rpc["p_tenant"] == TENANT


def test_known_group_skips_legacy_bundle_and_rejects_bad_rpc(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    http = HTTP(group="vg_reviewed", rpc_result={"group_key": "vg_reviewed",
        "source_fingerprint": DIGEST, "delivered_fingerprint": DIGEST, "usage_claimed": True})
    with pytest.raises(prep.VisualPreparationError, match="conflicting identity"):
        prep.prepare_same_object(Store(http), "lasso", {"image_url": URL},
            read_bytes=lambda _: DATA,
            receipt_writer=lambda **_: {"read_receipt": RECEIPT, "render_receipt": None},
            isolated_test_callbacks=True)
    assert not any(call[1] == "visual_global_prepare_bundle" for call in http.calls)


def test_owner_rejects_false_bytes_and_render_lineage_and_rolls_back():
    evidence = {"exact_url": URL, "fingerprint": DIGEST, "byte_length": len(DATA),
                "evidence_ref": "test", "observed_by": "test"}
    for changed in ({"fingerprint": "md5:" + "0" * 32}, {"operation": "render"},
                    {"exact_url": "https://foreign.example/selected.png"}):
        with pytest.raises(owner.OwnerReceiptError):
            owner.produce_same_object(tenant=TENANT, group_key="vg_reviewed",
                exact_bytes=DATA, evidence={**evidence, **changed},
                connection_factory=lambda: Connection())
    connection = Connection(fail=True)
    with pytest.raises(owner.OwnerReceiptError, match="transaction failed"):
        owner.produce_same_object(tenant=TENANT, group_key="vg_reviewed",
            exact_bytes=DATA, evidence=evidence, connection_factory=lambda: connection)
    assert (connection.commits, connection.rollbacks, connection.closes) == (0, 1, 1)


def test_drive_and_url_identity_fail_before_owner_receipt(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    called = []
    writer = lambda **kwargs: called.append(kwargs)
    for row in ({"image_url": "https://foreign.example/image.png"},
                {"image_url": URL, "source_media_url": "https://media.example/raw.png"},
                {"image_url": URL, "byte_hash": "derived:md5:" + "0" * 32}):
        with pytest.raises(prep.VisualPreparationError):
            prep.prepare_same_object(Store(HTTP()), "lasso", row,
                                     read_bytes=lambda _: DATA, receipt_writer=writer,
                                     isolated_test_callbacks=True)
    asset = {"id": "asset-1", "gym_id": "lasso", "content_hash": "0" * 32}
    with pytest.raises(prep.VisualPreparationError, match="Drive asset MD5"):
        prep.prepare_same_object(Store(HTTP(asset=asset)), "lasso",
            {"image_url": URL, "source_media_asset_id": "asset-1"},
            read_bytes=lambda _: DATA, receipt_writer=writer,
            isolated_test_callbacks=True)
    assert called == []
