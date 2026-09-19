from agent import echo_clients as ec
from agent import media_bridge_route as route


GYM = "0f0f0f0f-0000-4000-8000-000000000001"
OTHER = "0f0f0f0f-0000-4000-8000-000000000002"


def _clients():
    return ec.build([{"gym_id": GYM}],
                    [{"gym_id": GYM, "echo_account_key": "gymalpha123456"}],
                    [{"id": GYM, "name": "Gym Alpha", "slug": "gym-alpha"}])


def _gym(**changes):
    row = {"id": GYM, "status": "active", "is_demo": False,
           "load_test": False, "is_verification": False}
    row.update(changes)
    return row


def test_accepts_only_one_non_test_channel_bound_to_resolved_tenant():
    result = route.verify_rows(
        "gymalpha123456", _clients(), [_gym()],
        [{"client_id": GYM, "slack_channel_id": "C0CLIENT123", "is_test": False}],
    )
    assert result.ok and result.channel == "C0CLIENT123" and result.gym_id == GYM


def test_rejects_channel_shape_when_ticket_belongs_to_another_tenant():
    result = route.verify_rows(
        "gymalpha123456", _clients(), [_gym()],
        [{"client_id": OTHER, "slack_channel_id": "C0CLIENT123", "is_test": False}],
    )
    assert not result.ok and result.reason == "no tenant-bound client channel"


def test_rejects_shared_ops_and_ambiguous_routes():
    shared = route.verify_rows(
        "gymalpha123456", _clients(), [_gym()],
        [{"client_id": GYM, "slack_channel_id": "G0SHARED123", "is_test": False},
         {"client_id": OTHER, "slack_channel_id": "G0SHARED123", "is_test": False}],
    )
    assert not shared.ok and shared.reason == "no tenant-bound client channel"
    ops = route.verify_rows(
        "gymalpha123456", _clients(), [_gym()],
        [{"client_id": GYM, "slack_channel_id": "C0OPS123", "is_test": False}],
        ops_channel="C0OPS123")
    assert not ops.ok and ops.reason == "ops channel refused"
    ambiguous = route.verify_rows(
        "gymalpha123456", _clients(), [_gym()],
        [{"client_id": GYM, "slack_channel_id": "C0FIRST123", "is_test": False},
         {"client_id": GYM, "slack_channel_id": "G0SECOND123", "is_test": False}],
    )
    assert not ambiguous.ok and ambiguous.reason == "ambiguous client channels"


def test_rejects_revoked_inactive_and_test_only_routes():
    ticket = [{"client_id": GYM, "slack_channel_id": "C0CLIENT123", "is_test": False}]
    assert route.verify_rows("gymalpha123456", _clients(), [_gym()], ticket,
                             revoked=True).reason == "account revoked"
    assert route.verify_rows("gymalpha123456", _clients(), [_gym(status="archived")],
                             ticket).reason == "tenant inactive"
    test_only = route.verify_rows(
        "gymalpha123456", _clients(), [_gym()],
        [{"client_id": GYM, "slack_channel_id": "C0CLIENT123", "is_test": True}],
    )
    assert not test_only.ok and test_only.reason == "no tenant-bound client channel"


class _Response:
    def __init__(self, rows):
        self.status_code = 200
        self._rows = rows

    def json(self):
        return self._rows


class _Http:
    def __init__(self, tables):
        self.tables = tables

    def get(self, url, params, headers, timeout):
        table = url.rsplit("/", 1)[-1]
        offset, limit = int(params["offset"]), int(params["limit"])
        return _Response(self.tables[table][offset:offset + limit])


def test_reader_reads_both_link_tables_and_fails_closed_when_unavailable():
    http = _Http({"gyms": [_gym()],
                  "support_tickets": [{"client_id": GYM,
                                       "slack_channel_id": "C0CLIENT123",
                                       "is_test": False}]})
    reader = route.RouteReader("https://example.test", "not-a-secret", http=http)
    assert reader.resolve("gymalpha123456", clients=_clients()).channel == "C0CLIENT123"
    assert not route.RouteReader().resolve("gymalpha123456", clients=_clients()).ok
