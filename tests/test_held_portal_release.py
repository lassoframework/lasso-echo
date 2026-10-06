"""0383 proof-bound explicit portal delivery and lost-response recovery."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.held_portal_release import deliver_held_portal_notice
from agent.slack_convo.bus import BusError

TID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class Store:
    def __init__(self):
        evidence = dict(verified=True, ticket_id=TID, request_version=2,
                        verifier_identity="reviewer", repair_author_identity="builder",
                        repair_revision="frozen-sha", review_receipt="https://example.test/review",
                        behavior_receipt="https://example.test/behavior")
        self.ticket_row = dict(id=TID, request_version=2, product="echo", source="website_tab",
            status="merged", classification="code_fix", client_id="gym", bot_identity="echo",
            slack_user_id=None, slack_channel_id=None, slack_thread_ts=None,
            escalated=True, hold_tier="routine", verification_before={"snapshot": True},
            verification_after={"fixer": {"held_release_review": evidence}})
        self.proof = dict(id=PID, ticket_id=TID, request_version=2, expected_status="merged",
            expected_classification="code_fix", hold_tier="routine", repair_evidence=evidence,
            notice_message_id=MID, notice_body="Your photo repair is ready for review.",
            consumed_at=None, consumed_message_id=None)
        for field in ("product", "client_id", "bot_identity", "slack_user_id", "slack_channel_id", "slack_thread_ts"):
            self.proof[field] = self.ticket_row[field]
        self.row = None
        self.events = []
        self.patch_mode = "ok"
        self.rpc_mode = "ok"

    def ticket(self, tid):
        assert tid == TID
        return deepcopy(self.ticket_row)

    def _get(self, table, params):
        assert table == "fixer_release_proofs" and params["id"] == "eq." + PID
        return [deepcopy(self.proof)]

    def message(self, mid):
        assert mid == MID
        return deepcopy(self.row)

    def _insert(self, table, row):
        assert table == "support_messages" and row["delivery_status"] == "held"
        self.events.append("insert")
        self.row = deepcopy(row)
        return deepcopy(row), False

    def _patch(self, table, match, fields):
        assert table == "support_messages" and fields == {"delivery_status": "posted"}
        assert match["delivery_status"] == "eq.held"
        assert match["attachments"].startswith("eq.") and match["body"].startswith("eq.")
        self.events.append("post")
        if self.patch_mode == "empty":
            return None
        self.row.update(fields)
        if self.patch_mode == "lost":
            raise TimeoutError("posted response lost")
        return deepcopy(self.row)

    def _rest(self, name):
        assert name == "rpc/fixer_release_held_delivery"
        return name

    def _headers(self):
        return {}

    def _client(self):
        return self

    def post(self, _url, *, data, headers, timeout):
        import json
        params = json.loads(data)
        assert params["p_notice_message_id"] == MID and params["p_proof_id"] == PID
        assert params["p_expected_slack_channel_id"] is None
        assert self.row["delivery_status"] == "posted"
        self.events.append("release")
        if self.rpc_mode != "cas_lost":
            self.ticket_row.update(status="resolved", escalated=False, hold_tier=None)
            self.proof.update(consumed_at="now", consumed_message_id=MID)
        if self.rpc_mode == "lost":
            raise TimeoutError("release response lost")
        return SimpleNamespace(status_code=200)


def deliver(store):
    return deliver_held_portal_notice(store, ticket_id=TID, request_version=2, proof_id=PID)


def test_portal_release_posts_exact_notice_before_cas_and_is_idempotent():
    store = Store()
    assert deliver(store) == dict(notice_message_id=MID, delivered=True, resolved=True)
    assert store.events == ["insert", "post", "release"]
    assert store.row["author_type"] == "ranger" and store.row["direction"] == "outbound"
    assert store.row["attachments"]["kind"] == "status"
    frozen = deepcopy(store.row)
    assert deliver(store)["resolved"] is True
    assert store.events == ["insert", "post", "release"] and store.row == frozen


@pytest.mark.parametrize("mode", ["patch", "rpc"])
def test_lost_response_recovers_same_notice_without_reposting(mode):
    store = Store()
    setattr(store, "patch_mode" if mode == "patch" else "rpc_mode", "lost")
    with pytest.raises(TimeoutError):
        deliver(store)
    store.patch_mode = store.rpc_mode = "ok"
    assert deliver(store)["resolved"] is True
    assert store.events.count("insert") == store.events.count("post") == 1


def test_empty_patch_representation_without_readback_never_releases():
    store = Store()
    store.patch_mode = "empty"
    with pytest.raises(BusError, match="posted portal notice readback"):
        deliver(store)
    assert "release" not in store.events and store.ticket_row["escalated"]


def test_cas_loss_reports_delivery_without_false_resolution():
    store = Store()
    store.rpc_mode = "cas_lost"
    assert deliver(store) == dict(notice_message_id=MID, delivered=True, resolved=False)
    assert store.ticket_row["hold_tier"] == "routine"


@pytest.mark.parametrize("mutate", [
    lambda s: s.ticket_row.update(request_version=3),
    lambda s: s.ticket_row.update(client_id="other-gym"),
    lambda s: s.ticket_row.update(slack_channel_id="C_CHANGED"),
    lambda s: s.ticket_row.update(escalated=False),
    lambda s: s.ticket_row.update(hold_tier="review"),
    lambda s: s.proof["repair_evidence"].update(review_receipt="file:///local"),
    lambda s: s.proof["repair_evidence"].update(verifier_identity="builder"),
    lambda s: s.proof.update(notice_body=""),
])
def test_invalid_or_changed_proof_fails_before_delivery(mutate):
    store = Store()
    mutate(store)
    with pytest.raises(BusError):
        deliver(store)
    assert store.events == []


def test_existing_unbound_notice_cannot_be_borrowed():
    store = Store()
    store.row = dict(id=MID, body="different")
    with pytest.raises(BusError, match="notice changed"):
        deliver(store)
    assert store.events == []
