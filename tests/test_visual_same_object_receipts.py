"""Same exact URL (source == delivered) owner-attested preparation path.

PR235 Child A: a generated artifact is published at the exact URL it was
rendered to -- it was never transformed, so its byte evidence is ONE immutable
owner-created object-read receipt whose UUID serves as BOTH source-read and
delivered-read evidence, with a NULL render receipt (no invented lineage).
The draft RPC visual_global_prepare_source_rendition then binds the object and
must report usage_claimed=False. Flag OFF and any ambiguity fail closed.
"""
import hashlib
import uuid

import pytest

from agent import visual_owner_receipts as owner
from agent import visual_writer_prepare as prep


TENANT = "11111111-1111-4111-8111-111111111111"
URL = "https://media.example/generated/card.png"
DATA = b"exact generated artifact bytes"
FINGERPRINT = "md5:" + hashlib.md5(DATA).hexdigest()
GROUP = "vg_sameobject1"


@pytest.fixture(autouse=True)
def own_host(monkeypatch):
    monkeypatch.setattr("agent.config.S3_PUBLIC_BASE_URL", "https://media.example")


# ---- owner producer: receipt shape -------------------------------------------

class Cursor:
    def __init__(self):
        self.calls = []
        self._id = uuid.uuid4()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, params):
        self.calls.append((statement, params))

    def fetchone(self):
        return (self._id,)


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


def _evidence(**changes):
    value = {"exact_url": URL, "evidence_ref": "same-object-read-1",
             "observed_by": "visual_writer_prepare"}
    value.update(changes)
    return value


def test_same_object_receipt_is_single_row_null_render_receipt():
    connection = Connection()
    receipts = owner.produce_same_object(
        tenant=TENANT, group_key=GROUP, exact_bytes=DATA,
        evidence=_evidence(), connection_factory=lambda: connection)
    # ONE object-read insert, NEVER a render receipt insert.
    statements = [s for s, _ in connection.cursor_obj.calls]
    assert len(statements) == 1
    assert statements[0].startswith("insert into public.visual_global_object_read_receipt")
    assert not any("render_receipt" in s for s in statements)
    # The same UUID is the evidence for both roles; render receipt is NULL.
    assert receipts["render_receipt"] is None
    assert uuid.UUID(receipts["read_receipt"])
    params = connection.cursor_obj.calls[0][1]
    assert params[0] == TENANT and params[1] == URL
    assert params[2] == FINGERPRINT and params[3] == len(DATA)
    assert params[4] == "verified_object_read" and params[5] is None
    assert connection.committed and not connection.rolled_back and connection.closed


def test_same_object_receipt_rejects_invented_render_evidence():
    for bad in (_evidence(operation="render"), _evidence(rendered_by="someone")):
        with pytest.raises(owner.OwnerReceiptError, match="render operation"):
            owner.produce_same_object(
                tenant=TENANT, group_key=GROUP, exact_bytes=DATA, evidence=bad,
                connection_factory=lambda: pytest.fail("database must stay unopened"))


def test_same_object_receipt_validates_host_bytes_and_evidence():
    with pytest.raises(owner.OwnerReceiptError, match="media host"):
        owner.produce_same_object(
            tenant=TENANT, group_key=GROUP, exact_bytes=DATA,
            evidence=_evidence(exact_url="https://evil.example/x.png"),
            connection_factory=lambda: pytest.fail("database must stay unopened"))
    with pytest.raises(owner.OwnerReceiptError, match="differs from observed"):
        owner.produce_same_object(
            tenant=TENANT, group_key=GROUP, exact_bytes=DATA,
            evidence=_evidence(fingerprint="md5:" + "0" * 32),
            connection_factory=lambda: pytest.fail("database must stay unopened"))
    with pytest.raises(owner.OwnerReceiptError, match="byte observation"):
        owner.produce_same_object(
            tenant=TENANT, group_key=GROUP, exact_bytes=b"", evidence=_evidence(),
            connection_factory=lambda: pytest.fail("database must stay unopened"))


def test_owner_boundary_absent_fails_closed_no_service_role_path(monkeypatch):
    """Without the complete owner DSN/role config there is NO receipt writer at
    all -- receipts never fall back to the service role."""
    for env in ("AGENT_VISUAL_GLOBAL_OWNER_RECEIPTS",
                "AGENT_VISUAL_RECEIPT_OWNER_DSN",
                "AGENT_VISUAL_RECEIPT_OWNER_ROLE"):
        monkeypatch.delenv(env, raising=False)
    assert owner.default_same_object_writer() is None
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_OWNER_RECEIPTS", "true")
    assert owner.default_same_object_writer() is None       # DSN/role still missing
    with pytest.raises(owner.OwnerReceiptError, match="not configured"):
        owner.produce_same_object(
            tenant=TENANT, group_key=GROUP, exact_bytes=DATA, evidence=_evidence())


# ---- writer integration: prepare_same_object ---------------------------------

class Response:
    def __init__(self, value, status_code=200):
        self.value = value
        self.status_code = status_code

    def json(self):
        return self.value


class Store:
    """PostgREST fake: tenant_alias read + both RPC endpoints, calls recorded."""

    def __init__(self, rendition=None, bundle=None):
        self.calls = []
        self.rendition = rendition
        self.bundle = bundle
        self.registered = False

    def _client(self):
        return self

    def _rest(self, path):
        return "https://db.example/rest/v1/" + path

    def _headers(self, extra=None):
        return extra or {}

    def get(self, url, *, params, headers, timeout):
        self.calls.append(("get", url.rsplit("/", 1)[-1], params))
        if url.endswith("tenant_alias"):
            return Response([{"alias_key": "gymx", "tenant_id": TENANT}])
        if url.endswith("visual_group_alias"):
            return Response([{"group_key": GROUP}] if self.registered else [])
        raise AssertionError(url)

    def post(self, url, *, headers, json, timeout):
        self.calls.append(("post", url.rsplit("/", 1)[-1], json))
        if url.endswith("visual_global_prepare_bundle"):
            self.registered = True
            return Response(self.bundle if self.bundle is not None else
                            {"group_key": GROUP, "fingerprint": json["p_fingerprint"]})
        if url.endswith("visual_global_prepare_source_rendition"):
            return Response(self.rendition if self.rendition is not None else
                            {"group_key": json["p_group_key"],
                             "source_fingerprint": FINGERPRINT,
                             "delivered_fingerprint": FINGERPRINT,
                             "usage_claimed": False,
                             "history_refreshed": False,
                             "refreshed_local_groups": 0})
        raise AssertionError(url)


def _armed(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_OWNER_RECEIPTS", "true")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", "test-only")
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_ROLE", "receipt_owner")


def test_prepare_same_object_happy_path_verifies_rpc_result(monkeypatch):
    _armed(monkeypatch)
    receipt_id = str(uuid.uuid4())
    seen = {}

    def writer(**kwargs):
        seen.update(kwargs)
        return {"read_receipt": receipt_id, "render_receipt": None}

    out = prep.prepare_same_object(Store(), "gymx", {"image_url": URL, "source_media_url": URL}, flag_on=True,
                                   read_bytes=lambda url: DATA,
                                   receipt_writer=writer, isolated_test_callbacks=True)
    assert out["image_url"] == URL and out["source_media_url"] == URL
    assert out["visual_group_key"] == GROUP
    assert out["byte_hash"] == "derived:" + FINGERPRINT
    # The owner producer received the EXACT observed bytes and the exact URL.
    assert seen["tenant"] == TENANT and seen["group_key"] == GROUP
    assert seen["exact_bytes"] == DATA
    assert seen["evidence"]["exact_url"] == URL
    assert "operation" not in seen["evidence"] and "rendered_by" not in seen["evidence"]


def test_prepare_same_object_reuses_one_receipt_uuid_for_both_roles(monkeypatch):
    _armed(monkeypatch)
    receipt_id = str(uuid.uuid4())
    store = Store()
    prep.prepare_same_object(store, "gymx", {"image_url": URL, "source_media_url": URL}, flag_on=True,
                             read_bytes=lambda url: DATA,
                             receipt_writer=lambda **k: {"read_receipt": receipt_id,
                                                         "render_receipt": None},
                             isolated_test_callbacks=True)
    rendition = [call[2] for call in store.calls
                 if call[1] == "visual_global_prepare_source_rendition"]
    assert len(rendition) == 1
    args = rendition[0]
    assert args["p_source_read_receipt"] == receipt_id
    assert args["p_delivered_read_receipt"] == receipt_id
    assert args["p_render_receipt"] is None
    assert args["p_tenant"] == TENANT and args["p_group_key"] == GROUP


def test_prepare_same_object_bundle_bootstraps_group_first(monkeypatch):
    _armed(monkeypatch)
    store = Store()
    prep.prepare_same_object(store, "gymx", {"image_url": URL, "source_media_url": URL}, flag_on=True,
                             read_bytes=lambda url: DATA,
                             receipt_writer=lambda **k: {"read_receipt": str(uuid.uuid4()),
                                                         "render_receipt": None},
                             isolated_test_callbacks=True)
    posts = [call[1] for call in store.calls if call[0] == "post"]
    assert posts == ["visual_global_prepare_bundle",
                     "visual_global_prepare_source_rendition"]
    bundle = [call[2] for call in store.calls
              if call[1] == "visual_global_prepare_bundle"][0]
    assert [(a["alias_kind"], a["alias_value"]) for a in bundle["p_aliases"]] == [
        ("byte_hash", "derived:" + FINGERPRINT), ("canonical_url", URL)]
    # No usage is ever claimed by preparation.
    assert not any("usage" in call[1] or "claim" in call[1] for call in store.calls)


def test_prepare_same_object_rejects_conflicting_rpc_result(monkeypatch):
    _armed(monkeypatch)
    writer = lambda **k: {"read_receipt": str(uuid.uuid4()), "render_receipt": None}
    for rendition in ({"group_key": "vg_other", "source_fingerprint": FINGERPRINT,
                       "delivered_fingerprint": FINGERPRINT, "usage_claimed": False},
                      {"group_key": GROUP, "source_fingerprint": "md5:" + "0" * 32,
                       "delivered_fingerprint": FINGERPRINT, "usage_claimed": False},
                      {"group_key": GROUP, "source_fingerprint": FINGERPRINT,
                       "delivered_fingerprint": FINGERPRINT, "usage_claimed": True}):
        with pytest.raises(prep.VisualPreparationError, match="conflicting identity"):
            prep.prepare_same_object(Store(rendition=rendition), "gymx", {"image_url": URL, "source_media_url": URL},
                                     flag_on=True, read_bytes=lambda url: DATA,
                                     receipt_writer=writer, isolated_test_callbacks=True)


def test_prepare_same_object_rejects_render_receipt_from_producer(monkeypatch):
    _armed(monkeypatch)
    bad = lambda **k: {"read_receipt": str(uuid.uuid4()),
                       "render_receipt": str(uuid.uuid4())}
    with pytest.raises(prep.VisualPreparationError, match="invalid receipt"):
        prep.prepare_same_object(Store(), "gymx", {"image_url": URL, "source_media_url": URL}, flag_on=True,
                                 read_bytes=lambda url: DATA, receipt_writer=bad, isolated_test_callbacks=True)


def test_prepare_same_object_owner_writer_absent_fails_closed(monkeypatch):
    _armed(monkeypatch)
    for env in ("AGENT_VISUAL_GLOBAL_OWNER_RECEIPTS",
                "AGENT_VISUAL_RECEIPT_OWNER_DSN",
                "AGENT_VISUAL_RECEIPT_OWNER_ROLE"):
        monkeypatch.delenv(env, raising=False)
    store = Store()
    with pytest.raises(prep.VisualPreparationError, match="owner receipt producer"):
        prep.prepare_same_object(store, "gymx", {"image_url": URL, "source_media_url": URL}, flag_on=True)
    # No scene or receipt RPC is reached without a configured owner writer.
    assert not any(call[1] == "visual_global_prepare_source_rendition"
                   for call in store.calls)


def test_prepare_same_object_flag_off_raises_and_does_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    store = Store()
    with pytest.raises(prep.VisualPreparationError, match="flag is off"):
        prep.prepare_same_object(store, "gymx", URL, read_bytes=lambda url: DATA,
                                 isolated_test_callbacks=True)
    assert store.calls == []
    # A caller pinning flag_on=False against an actually-off flag is consistent,
    # still off; pinning True against an off flag is ambiguous. Both fail closed.
    with pytest.raises(prep.VisualPreparationError):
        prep.prepare_same_object(Store(), "gymx", URL, flag_on=False,
                                 read_bytes=lambda url: DATA,
                                 isolated_test_callbacks=True)
    with pytest.raises(prep.VisualPreparationError, match="ambiguous"):
        prep.prepare_same_object(Store(), "gymx", {"image_url": URL, "source_media_url": URL}, flag_on=True,
                                 read_bytes=lambda url: DATA,
                                 isolated_test_callbacks=True)


def test_prepare_same_object_flag_state_mismatch_is_ambiguous(monkeypatch):
    _armed(monkeypatch)
    with pytest.raises(prep.VisualPreparationError, match="ambiguous"):
        prep.prepare_same_object(Store(), "gymx", {"image_url": URL, "source_media_url": URL}, flag_on=False,
                                 read_bytes=lambda url: DATA,
                                 isolated_test_callbacks=True)


def test_prepare_same_object_rejects_foreign_url_and_unreadable_bytes(monkeypatch):
    _armed(monkeypatch)
    with pytest.raises(prep.VisualPreparationError, match="media host"):
        prep.prepare_same_object(Store(), "gymx", {"image_url": "https://evil.example/x.png", "source_media_url": "https://evil.example/x.png"},
                                 flag_on=True, read_bytes=lambda url: DATA,
                                 isolated_test_callbacks=True)
    with pytest.raises(prep.VisualPreparationError, match="could not be (read|verified)"):
        prep.prepare_same_object(Store(), "gymx", {"image_url": URL, "source_media_url": URL}, flag_on=True,
                                 read_bytes=lambda url: None,
                                 isolated_test_callbacks=True)


def test_prepare_same_object_bare_url_cannot_imply_source_identity(monkeypatch):
    _armed(monkeypatch)
    with pytest.raises(prep.VisualPreparationError, match="source and delivered URL must match"):
        prep.prepare_same_object(Store(), "gymx", URL, flag_on=True,
                                 read_bytes=lambda url: DATA,
                                 isolated_test_callbacks=True)


def test_production_config_rejects_injected_callbacks_for_url_and_row(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setenv("AGENT_VISUAL_RECEIPT_OWNER_DSN", "host=production.example dbname=live")
    calls = []
    reader = lambda url: calls.append("reader") or DATA
    writer = lambda **kw: calls.append("writer") or {"read_receipt": str(uuid.uuid4())}
    for item in ({"image_url": URL, "source_media_url": URL},):
        for kwargs in ({"read_bytes": reader}, {"receipt_writer": writer},
                       {"read_bytes": reader, "receipt_writer": writer,
                        "isolated_test_callbacks": True}):
            store = Store()
            with pytest.raises(prep.VisualPreparationError, match="isolated test configuration"):
                prep.prepare_same_object(store, "gymx", item, **kwargs)
            assert store.calls == []
    assert calls == []


def test_row_form_preserves_row_contract_and_owner_receipt(monkeypatch):
    _armed(monkeypatch)
    row = {"image_url": URL, "source_media_url": URL,
           "byte_hash": "derived:" + FINGERPRINT}
    store = Store()
    result = prep.prepare_same_object(
        store, "gymx", row, read_bytes=lambda _: DATA,
        receipt_writer=lambda **kw: {"read_receipt": str(uuid.uuid4()),
                                     "render_receipt": None},
        isolated_test_callbacks=True)
    assert result is not row and row.get("visual_group_key") is None
    assert result["visual_group_key"] == GROUP
    assert result["byte_hash"] == "derived:" + FINGERPRINT
    assert result["source_media_url"] == URL
