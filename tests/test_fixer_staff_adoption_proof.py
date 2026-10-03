"""Scout staff proof stays read-only and closed without a real business check."""
import json
from copy import deepcopy

from agent import fixer_ops
from agent import fixer_staff_adoption_proof as proof


def message(number):
    return {"id": f"00000000-0000-4000-8000-{number:012d}",
            "author_id": proof.REQUESTER, "created_at": "2026-10-02T12:00:00Z",
            "body": f"request {number}"}


def payload(messages=None):
    return {"ticket_id": proof.TICKET_ID, "request_version": 0,
            "client_id": None, "slack_channel_id": proof.CHANNEL,
            "slack_thread_ts": proof.THREAD,
            "request_messages": messages if messages is not None else [message(1), message(2)]}


class Reader:
    def __init__(self, messages=None, ticket=None):
        self.messages = messages if messages is not None else [message(1), message(2)]
        self.ticket = ticket if ticket is not None else {
            "id": proof.TICKET_ID, "product": "echo", "source": "slack_conversation",
            "status": "hold", "is_test": False, "client_id": None,
            "identity_kind": "coach", "request_version": 0,
            "slack_user_id": proof.REQUESTER,
            "slack_channel_id": proof.CHANNEL, "slack_thread_ts": proof.THREAD}
        self.calls = []

    def __call__(self, table, params):
        self.calls.append((table, params))
        if table == "support_tickets":
            return [dict(self.ticket)]
        assert table == "support_messages"
        rows = [row for row in self.messages if row["id"] > params.get("id", "gt.")[3:]]
        return [{**row, "ticket_id": proof.TICKET_ID, "direction": "inbound"}
                for row in rows[:int(params["limit"])]]


def post(monkeypatch, body, reader, secret="secret", http_get=None, receipt_read=None):
    monkeypatch.setenv("FIXER_OPS_SECRET", "secret")
    return fixer_ops.handle("POST", proof.PATH,
                            lambda name, default="": secret if name == "X-Fixer-Ops-Secret" else default,
                            json.dumps(body).encode(),
                            deps={"staff_adoption_read": reader, "staff_adoption_http_get": http_get,
                                  "staff_adoption_receipt_read": receipt_read}, log=lambda *_: None)


def test_exact_readback_refuses_when_the_private_archive_receipt_is_unavailable(monkeypatch):
    reader = Reader()
    status, body = post(monkeypatch, payload(), reader)
    assert (status, body) == (503, {"error": "business_check_unavailable"})
    assert [table for table, _ in reader.calls] == ["support_tickets", "support_messages"]
    assert "verified" not in body and "production_sha" not in body


def test_secret_and_method_gate_precede_all_reads(monkeypatch):
    reader = Reader()
    assert post(monkeypatch, payload(), reader, secret="wrong")[0] == 401
    assert fixer_ops.handle("GET", proof.PATH, lambda key, default="": "secret",
                            deps={"staff_adoption_read": reader})[0] == 405
    assert reader.calls == []


def test_client_binding_and_wrong_ticket_refused_before_source_read(monkeypatch):
    reader = Reader()
    for patch in ({"client_id": "22222222-2222-4222-8222-222222222222"},
                  {"ticket_id": "11111111-1111-4111-8111-111111111111"},
                  {"request_version": True}):
        assert post(monkeypatch, {**payload(), **patch}, reader)[0] == 400
    assert reader.calls == []


def test_stale_version_and_incomplete_thread_fail(monkeypatch):
    reader = Reader()
    reader.ticket["request_version"] = 1
    assert post(monkeypatch, payload(), reader) == (409, {"error": "ticket_identity_mismatch"})
    reader.ticket["request_version"] = 0
    assert post(monkeypatch, payload([message(1)]), reader) == (
        409, {"error": "request_messages_mismatch"})


def test_complete_keyset_read_and_source_failure(monkeypatch):
    rows = [message(n) for n in range(1, 202)]
    reader = Reader(messages=rows)
    assert post(monkeypatch, payload(rows), reader)[0] == 503
    assert len([table for table, _ in reader.calls if table == "support_messages"]) == 2
    assert reader.calls[-1][1]["id"] == f"gt.{rows[199]['id']}"

    def broken(*_):
        raise RuntimeError("source down")
    assert post(monkeypatch, payload(), broken) == (
        503, {"error": "authoritative_source_unavailable"})


def test_exact_receipt_calendar_and_photo_readbacks_return_only_current_worker_sha(monkeypatch, tmp_path):
    historical = []
    for n in range(25):
        day = f"2026-09-{2 + (n % 17):02d}"
        historical.append({"id": f"hist-{n}", "gym_id": proof._SWIFT, "post_date": day,
            "status": "pending", "variant_status": "archived", "image_url": f"https://x/igfill_{n}.jpg",
            "source_media_url": None, "source_media_asset_id": None, "caption": "c",
            "media_not_ready_reason": "held", "published_at": None, "late_post_id": None,
            "scheduled_at": None, "slot_index": None, "account": "instagram", "format": "feed",
            "variant_of": None, "created_at": "2026-09-01T00:00:00Z"})
    future, assets = [], []
    for day in range(3, 12):
        asset_id = f"photo-{day}"
        future.append({"id": f"future-{day}", "gym_id": proof._SWIFT, "post_date": f"2026-10-{day:02d}",
            "status": "pending", "variant_status": "active", "image_url": f"https://media.example/{asset_id}.jpg",
            "source_media_url": None, "source_media_asset_id": asset_id,
            "account": "instagram", "format": "feed"})
        assets.append({"id": asset_id, "gym_id": proof._SWIFT, "kind": "photo", "eligible": True,
            "excluded_by_coach": False, "review_status": "approved", "reviewed_by": "coach",
            "reviewed_at": "2026-10-03T00:00:00Z", "content_hash": asset_id,
            "review_content_hash": asset_id, "moderation_status": "clean", "moderation_json": {"verdict": "clean", "provider": "scan",
                "content_hash": asset_id, "asset_id": asset_id, "gym_id": proof._SWIFT,
                "people_detected": None, "observed_at": "2026-10-03T00:00:00Z"}, "people_detected": None})
    receipt = tmp_path / "archive.json"
    receipt.write_text(json.dumps({"operation": "swift_river_historical_igfill_archive", "client": proof._SWIFT,
        "target_rows": 25, "target_dates": 17, "state": "readback_verified", "before_image": historical,
        "readback": historical, "changed_ids": [r["id"] for r in historical]}))
    receipt.chmod(0o600)
    monkeypatch.setattr(proof, "_ARCHIVE", receipt)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
    base = Reader()
    def read(table, params):
        if table in ("support_tickets", "support_messages"):
            return base(table, params)
        if table == "content_calendar":
            start, end = params["post_date"]
            rows = historical + future
            return [deepcopy(r) for r in rows if start[4:] <= r["post_date"] <= end[4:]]
        assert table == "media_asset"
        return [deepcopy(r) for r in assets if r["id"] == params["id"][3:]]
    monkeypatch.setattr("agent.config.S3_PUBLIC_BASE_URL", "https://media.example")
    class Response:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}
        def __init__(self, url): self.url = url
        def iter_content(self, _size): yield self.url.encode()
    status, body = post(monkeypatch, payload(), read, http_get=lambda url, **_: Response(url))
    assert (status, body) == (503, {"error": "business_check_unavailable"})


def test_media_digest_refuses_an_unallowlisted_url(monkeypatch):
    monkeypatch.setattr("agent.config.S3_PUBLIC_BASE_URL", "https://media.example")
    assert proof._media_digest("https://evil.example/image.jpg", lambda *_a, **_k: None) is None


def test_ticket_scoped_receipt_success_and_stale_or_cross_tenant_refusal(monkeypatch):
    import hashlib
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "b" * 40)
    row_id, asset_id, key = "swift-row-01", "drive-photo-1", "staff-swap-001"
    old, new, caption = "https://old/image.jpg", "https://new/image.jpg", "Keep copy"
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    reader = Reader()
    reader.ticket["verification_before"] = {"fixer": {"staff_swap": {
        "gym_key": proof._SWIFT, "row_id": row_id, "reservation_key": key,
        "before_image_sha256": digest(old), "caption_sha256": digest(caption)}}}
    def read(table, params):
        if table in ("support_tickets", "support_messages"):
            return reader(table, params)
        if table == "content_calendar":
            return [{"id": row_id, "gym_id": proof._SWIFT, "status": "pending", "caption": caption,
                     "image_url": new, "source_media_asset_id": asset_id}]
        return [{"id": asset_id, "gym_id": proof._SWIFT, "kind": "photo", "eligible": True,
                 "excluded_by_coach": False, "review_status": "approved", "reviewed_by": "coach",
                 "reviewed_at": "2026-10-03T00:00:00Z", "content_hash": "h", "review_content_hash": "h",
                 "moderation_status": "clean", "moderation_json": {"verdict": "clean", "provider": "scan", "content_hash": "h", "asset_id": asset_id, "gym_id": proof._SWIFT, "people_detected": None, "observed_at": "2026-10-03T00:00:00Z"}, "people_detected": None}]
    current_key = fixer_ops._business_request_key(read, reader.ticket)
    assert current_key is not None
    receipt = {"action": "swap_media", "status": "done", "ticket_id": proof.TICKET_ID, "gym_key": proof._SWIFT,
               "request_key": current_key, "finished_at": "2026-10-03T13:00:00Z",
               "result": {"postcondition_verified": True, "swap_proof": {"row_id": row_id,
                 "before_image_sha256": digest(old), "after_image_sha256": digest(new),
                 "caption_sha256": digest(caption), "after_asset_id": asset_id}}}
    status, body = post(monkeypatch, payload(), read, receipt_read=lambda *_: dict(receipt))
    assert status == 200 and body["check_id"] == "staff_ticket_keyed_media_swap"
    stale = {**receipt, "finished_at": "2026-10-01T00:00:00Z"}
    assert post(monkeypatch, payload(), read, receipt_read=lambda *_: stale) == (409, {"error": "swap_receipt_stale"})
    foreign = {**receipt, "gym_key": "othergym"}
    assert post(monkeypatch, payload(), read, receipt_read=lambda *_: foreign) == (409, {"error": "swap_receipt_mismatch"})


def test_staff_coach_inbound_messages_change_the_current_request_key():
    ticket = {"id": proof.TICKET_ID, "created_at": "2026-10-02T00:00:00Z",
              "raw_text": "calendar not rebuilt from Drive"}
    messages = [{"id": "message-1", "ticket_id": proof.TICKET_ID,
                 "created_at": "2026-10-02T12:00:00Z", "body": "swap it",
                 "author_id": proof.REQUESTER, "author_type": "coach",
                 "direction": "inbound", "attachments": {}}]
    selects = []

    def read(table, params):
        assert table == "support_messages"
        selects.append(params["select"])
        return deepcopy(messages)

    before = fixer_ops._business_request_key(read, ticket)
    messages.append({"id": "message-2", "ticket_id": proof.TICKET_ID,
                     "created_at": "2026-10-03T14:00:00Z", "body": "still unchanged",
                     "author_id": proof.REQUESTER, "author_type": "coach",
                     "direction": "inbound", "attachments": {}})
    after = fixer_ops._business_request_key(read, ticket)
    assert before != after
    assert all("author_id" in select.split(",") for select in selects)


def test_staff_ticket_tenant_capability_is_swap_and_pointer_row_only():
    row_id = "swift-row-01"
    pointer = {"gym_key": proof._SWIFT, "row_id": row_id,
               "reservation_key": "staff-swap-001",
               "before_image_sha256": "a" * 64,
               "caption_sha256": "b" * 64}
    ticket = {"id": proof.TICKET_ID, "product": "echo", "source": "slack_conversation",
              "status": "hold", "identity_kind": "coach", "slack_user_id": proof.REQUESTER,
              "slack_channel_id": proof.CHANNEL, "slack_thread_ts": proof.THREAD,
              "client_id": None, "raw_text": "", "verification_before": {"fixer": {"staff_swap": pointer}}}

    class Bus:
        def _get(self, table, params):
            if table == "support_tickets":
                return [deepcopy(ticket)]
            assert table == "echo_intake_tokens"
            return [{"gym_id": "11111111-1111-4111-8111-111111111111",
                     "echo_account_key": proof._SWIFT}]

    deps = {"bus": Bus()}
    assert fixer_ops._ticket_tenant(proof._SWIFT, proof.TICKET_ID, deps,
                                    action="swap_media", args={"row_id": row_id},
                                    reservation_key=pointer["reservation_key"]) is None
    assert fixer_ops._ticket_tenant(proof._SWIFT, proof.TICKET_ID, deps,
                                    action="swap_media", args={"row_id": row_id},
                                    reservation_key="staff-swap-002") == (
        409, {"error": "ticket_tenant_unconfirmed"})
    assert fixer_ops._ticket_tenant(proof._SWIFT, proof.TICKET_ID, deps,
                                    action="swap_media", args={"row_id": "another-row"},
                                    reservation_key=pointer["reservation_key"]) == (
        409, {"error": "ticket_tenant_unconfirmed"})
    assert fixer_ops._ticket_tenant(proof._SWIFT, proof.TICKET_ID, deps,
                                    action="rebuild_calendar", args={},
                                    reservation_key=pointer["reservation_key"]) == (
        409, {"error": "ticket_tenant_unconfirmed"})
    assert fixer_ops._ticket_tenant("othergym", proof.TICKET_ID, deps,
                                    action="swap_media", args={"row_id": row_id},
                                    reservation_key=pointer["reservation_key"]) == (
        409, {"error": "ticket_tenant_unconfirmed"})
