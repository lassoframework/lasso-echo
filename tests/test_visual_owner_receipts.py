"""Owner receipt producer contracts; all database handles are local fakes."""
import hashlib
import uuid

import pytest

from agent import visual_owner_receipts as owner
from agent import visual_writer_prepare as prep


TENANT = "11111111-1111-4111-8111-111111111111"
SOURCE_URL = "https://media.example/raw.jpg"
DELIVERED_URL = "https://media.example/burn.jpg"
SOURCE, DELIVERED = b"raw pixels", b"burned pixels"


def evidence(**changes):
    value = {"source_exact_url": SOURCE_URL, "delivered_exact_url": DELIVERED_URL,
             "source_fingerprint": "md5:" + hashlib.md5(SOURCE).hexdigest(),
             "delivered_fingerprint": "md5:" + hashlib.md5(DELIVERED).hexdigest(),
             "source_byte_length": len(SOURCE), "delivered_byte_length": len(DELIVERED),
             "operation": "reburn", "evidence_ref": "story-render-job-42",
             "observed_by": "echo-visual-owner", "rendered_by": "echo-story-renderer"}
    value.update(changes)
    return value


class Cursor:
    def __init__(self):
        self.calls = []
        self.ids = iter([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, params):
        self.calls.append((statement, params))

    def fetchone(self):
        return (next(self.ids),)


class Connection:
    def __init__(self):
        self.cursor_obj = Cursor()
        self.committed = self.rolled_back = self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def own_host(monkeypatch):
    monkeypatch.setattr("agent.config.S3_PUBLIC_BASE_URL", "https://media.example")


def test_owner_producer_inserts_immutable_receipt_triple_in_one_transaction():
    connection = Connection()
    receipts = owner.produce(tenant=TENANT, group_key="vg_scene", source_bytes=SOURCE,
                             delivered_bytes=DELIVERED, render_evidence=evidence(),
                             asset_id="drive-1", connection_factory=lambda: connection)
    assert set(receipts) == {"source_read_receipt", "delivered_read_receipt", "render_receipt"}
    assert connection.committed and not connection.rolled_back and connection.closed
    statements = [statement for statement, _ in connection.cursor_obj.calls]
    assert statements[0].startswith("insert into public.visual_global_object_read_receipt")
    assert statements[1].startswith("insert into public.visual_global_object_read_receipt")
    assert statements[2].startswith("insert into public.visual_global_render_receipt")
    source_params, delivered_params = connection.cursor_obj.calls[0][1], connection.cursor_obj.calls[1][1]
    assert source_params[4:6] == ("drive_asset", "drive-1")
    assert delivered_params[2] == "md5:" + hashlib.md5(DELIVERED).hexdigest()
    assert SOURCE.decode() not in "".join(statements)
    assert DELIVERED.decode() not in "".join(statements)


def test_owner_producer_refuses_external_urls_and_mismatched_evidence():
    with pytest.raises(owner.OwnerReceiptError, match="configured media host"):
        owner.produce(tenant=TENANT, group_key="vg_scene", source_bytes=SOURCE,
                      delivered_bytes=DELIVERED,
                      render_evidence=evidence(source_exact_url="https://evil.example/raw.jpg"),
                      connection_factory=lambda: pytest.fail("database must stay unopened"))
    with pytest.raises(owner.OwnerReceiptError, match="differs from observed"):
        owner.produce(tenant=TENANT, group_key="vg_scene", source_bytes=SOURCE,
                      delivered_bytes=DELIVERED,
                      render_evidence=evidence(delivered_byte_length=1),
                      connection_factory=lambda: pytest.fail("database must stay unopened"))


def test_prepare_uses_configured_owner_writer_only_when_available(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", "test-only")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_ROLE", "receipt_owner")
    seen = {}

    def writer(**kwargs):
        seen.update(kwargs)
        return {"source_read_receipt": str(uuid.uuid4()),
                "delivered_read_receipt": str(uuid.uuid4()),
                "render_receipt": str(uuid.uuid4())}

    class Response:
        status_code = 200
        def json(self):
            return {"group_key": "vg_scene",
                    "source_fingerprint": "md5:" + hashlib.md5(SOURCE).hexdigest(),
                    "delivered_fingerprint": "md5:" + hashlib.md5(DELIVERED).hexdigest(),
                    "usage_claimed": False}

    class Store:
        def _client(self): return self
        def _rest(self, path): return "https://db.example/" + path
        def _headers(self, extra=None): return extra or {}
        def get(self, url, *, params, headers, timeout):
            if url.endswith("tenant_alias"):
                return type("R", (), {"status_code": 200, "json": lambda _: [{"alias_key": "gym", "tenant_id": TENANT}]})()
            if url.endswith("visual_group_alias"):
                return type("R", (), {"status_code": 200, "json": lambda _: [{"group_key": "vg_scene"}]})()
            raise AssertionError(url)
        def post(self, *_, **__): return Response()

    monkeypatch.setattr(owner, "default_writer", lambda: writer)
    prepared = prep.prepare(Store(), "gym", {"image_url": DELIVERED_URL,
                                               "source_media_url": SOURCE_URL},
                            read_bytes=lambda url: {SOURCE_URL: SOURCE, DELIVERED_URL: DELIVERED}[url],
                            render_evidence=evidence(), isolated_test_callbacks=True)
    assert prepared["visual_group_key"] == "vg_scene"
    assert seen["source_bytes"] == SOURCE and seen["delivered_bytes"] == DELIVERED
