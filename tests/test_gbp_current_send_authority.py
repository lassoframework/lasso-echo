"""GBP creates recheck current authority, with no provider or production access."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent import config, forward_media_guard as guard, forward_media_publish as bridge
from agent import gbp_worker as gw
from tests.test_gbp_worker import _row, _conn, _c, _TokenStore


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("AGENT_FORWARD_MEDIA_GUARD", "true")
    monkeypatch.setattr("agent.publish_billing_gate.publishing_blocked", lambda _: False)
    monkeypatch.setattr(gw, "_media_reuse_hold", lambda *a, **kw: None)
    monkeypatch.setattr(config, "posting_timezone_for", lambda _: "UTC")


class Provider:
    def __init__(self, events, exc=None, response=None):
        self.events, self.calls, self.exc = events, [], exc
        self.response = response if response is not None else {"_id": "created"}

    def create_post_raw(self, payload, **kwargs):
        return self._create((deepcopy(payload), kwargs))

    def create_gmb_media(self, account_id, url):
        return self._create((account_id, url))

    def _create(self, args):
        self.events.append("create")
        self.calls.append(args)
        if self.exc:
            raise self.exc
        return self.response


def invoke(photo, row, conn, client, store=None, token=None):
    fn = gw.publish_photo_drop if photo else gw.publish_gbp_row
    return fn(row, conn, client=client, draft=False,
              authority_store=store, idempotency_key=token)


def authority(monkeypatch, photo, drift=None):
    """Real bridge with an isolated REST double; simulate changes after first claim."""
    row = _row(id=str(uuid4()), gym_id="gym", account="googlebusiness",
               format="photo" if photo else "feed", post_date="2026-10-07",
               gbp_location_id="locations/123", visual_group_key="visual-group",
               source_media_url="https://r2/original.jpg", thumbnail_url=None)
    token, evidence_id = str(uuid4()), str(uuid4())
    persisted = dict(deepcopy(row), status="publishing", publish_claim_token=token,
                     publish_reservation_day=datetime.now(timezone.utc).date().isoformat())
    state = {"revision": "v1"}
    events = []

    def response(data):
        return SimpleNamespace(status_code=200, json=lambda: deepcopy(data))

    def post(*a, **kw):
        if a[0] == "rpc/fixer_authorize_gbp_forward_send_20261008":
            events.append("atomic")
            args = kw["json"]
            expected = {key: persisted.get(key) for key in gw._GBP_SEND_CREATIVE}
            if (args["p_expected_creative"] != expected
                    or args["p_claim_token"] != persisted["publish_claim_token"]
                    or (args['p_send_kind'] == 'gallery') != (persisted['format'] == 'photo')):
                return response(False)
            try:
                connections = store.connections_for(row["gym_id"])
            except Exception:
                return response(False)
            matching = [c for c in connections if c.get("gbp_location_id") == persisted["gbp_location_id"]]
            return response(len(matching) == 1 and matching[0].get("status") == "connected"
                            and matching[0].get("portal_gym_key") == persisted["gym_id"]
                            and matching[0].get("zernio_account_id") == args["p_native_account_id"])
        return response(dict(calendar_row_id=persisted["id"], gym_id=persisted["gym_id"],
                             account=persisted["account"], format=persisted["format"],
                             gbp_location_id=persisted["gbp_location_id"],
                             post_date=persisted["post_date"], group_key=persisted["visual_group_key"],
                             source_url=persisted["source_media_url"], image_url=persisted["image_url"],
                             thumbnail_url=None, revision=state["revision"]))

    def get(*a, **kw):
        return response([{"evidence_id": evidence_id}]
                        if kw["params"]["row_revision"] == "eq.v1" else [])

    def connections_for(gym):
        assert gym == row["gym_id"]
        events.append("destination")
        return [dict(_conn(), portal_gym_key=gym, status="connected")]

    store = SimpleNamespace(get_row=lambda *a: deepcopy(persisted),
                            _client=lambda: SimpleNamespace(post=post, get=get),
                            _rest=lambda p: p, _headers=lambda *a: {},
                            connections_for=connections_for)

    def claim(actual_store, row_id, actual_token, actual_evidence, revision):
        assert actual_store is store and row_id == row["id"] and actual_token == token
        assert actual_evidence == evidence_id and revision == "v1"
        events.append("claim")
        if len(events) == 1 and drift:
            if drift == "revision":
                state["revision"] = "v2"
            elif drift == "token":
                persisted["publish_claim_token"] = str(uuid4())
            else:
                persisted[drift] = "changed"
        return True

    monkeypatch.setattr(guard, "claim", claim)
    return row, token, store, events


@pytest.mark.parametrize("photo", [False, True])
def test_fresh_claim_replay_immediately_precedes_exact_create(monkeypatch, photo):
    row, token, store, events = authority(monkeypatch, photo)
    client = Provider(events)
    out = invoke(photo, row, _conn(), client, store, token)
    assert out["ok"] and events == ["claim", "destination", "claim", "atomic", "destination", "create"]
    if photo:
        assert client.calls == [(_conn()["zernio_account_id"], row["image_url"])]
    else:
        payload, kwargs = client.calls[0]
        assert payload == gw.build_gbp_payload_for_row(row, _conn())
        assert kwargs == {"draft": False, "idempotency_key": token}


@pytest.mark.parametrize("photo", [False, True])
def test_exact_authority_cannot_be_used_for_the_other_provider_mutation(monkeypatch, photo):
    row, token, store, events = authority(monkeypatch, not photo)
    client = Provider(events)
    out = invoke(photo, row, _conn(), client, store, token)
    assert out['held'] == 'forward_media_verification' and client.calls == []
    assert events[-1] == 'atomic'


@pytest.mark.parametrize("photo", [False, True])
@pytest.mark.parametrize("drift", ["token", "revision", "image_url", "source_media_url",
                                    "gym_id", "post_date", "visual_group_key", "gbp_location_id"])
def test_persisted_drift_after_preflight_holds_before_create(monkeypatch, photo, drift):
    row, token, store, events = authority(monkeypatch, photo, drift)
    client = Provider(events)
    out = invoke(photo, row, _conn(), client, store, token)
    assert out["status"] == "approved" and out["held"] == "forward_media_verification"
    assert events == ["claim", "destination"] and client.calls == []


@pytest.mark.parametrize("photo", [False, True])
@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("drift", ["row", "nested_row", "account", "location"])
def test_local_arguments_cannot_change_during_either_authority_check(monkeypatch, photo, phase, drift):
    row = _row(gym_id="gym", account="googlebusiness", gbp_location_id="locations/123",
               gbp_event={"schedule": {"endDate": "2026-10-08"}})
    conn, events = _conn(), []
    store = SimpleNamespace(connections_for=lambda _: [dict(_conn(), portal_gym_key="gym", status="connected")])
    def authorize(*args):
        events.append("authorize")
        if len(events) == phase:
            if drift == "row":
                row["image_url"] = "https://changed/image"
            elif drift == "nested_row":
                row["gbp_event"]["schedule"]["endDate"] = "2026-12-12"
            else:
                conn["zernio_account_id" if drift == "account" else "gbp_location_id"] = "changed"
        return True
    monkeypatch.setattr(bridge, "authorize", authorize)
    client = Provider(events)
    out = invoke(photo, row, conn, client, store)
    assert out["status"] == "approved" and out["held"] == "forward_media_verification"
    assert client.calls == [] and events == ["authorize"] * phase


@pytest.mark.parametrize("photo", [False, True])
@pytest.mark.parametrize("drift", ["account", "location", "gym", "status", "duplicate", "missing", "read"])
def test_current_native_destination_reread_is_required(monkeypatch, photo, drift):
    row, token, store, events = authority(monkeypatch, photo)
    connection = dict(_conn(), portal_gym_key=row["gym_id"], status="connected")
    def connections_for(gym):
        assert events == ["claim"]  # the reread is after preflight, before final claim
        events.append("destination")
        if drift == "read":
            raise TimeoutError("connection read unavailable")
        if drift == "missing":
            return []
        if drift == "duplicate":
            return [connection, deepcopy(connection)]
        key = {"account": "zernio_account_id", "location": "gbp_location_id",
               "gym": "portal_gym_key", "status": "status"}[drift]
        connection[key] = "changed"
        return [connection]
    store.connections_for = connections_for
    client = Provider(events)
    out = invoke(photo, row, _conn(), client, store, token)
    assert out["status"] == "approved" and out["held"] == "forward_media_verification"
    assert events == ["claim", "destination"] and client.calls == []


@pytest.mark.parametrize("photo", [False, True])
def test_caller_cannot_reroute_valid_claim_to_different_native_account(monkeypatch, photo):
    row, token, store, events = authority(monkeypatch, photo)
    client = Provider(events)
    out = invoke(photo, row, dict(_conn(), zernio_account_id="other-account"), client, store, token)
    assert out["held"] == "forward_media_verification" and client.calls == []
    assert events == ["claim", "destination"]


@pytest.mark.parametrize("photo", [False, True])
@pytest.mark.parametrize("drift", ["account", "location", "gym", "status", "duplicate", "missing", "read"])
def test_persisted_destination_drift_during_final_authority_holds(monkeypatch, photo, drift):
    row, token, store, events = authority(monkeypatch, photo)
    persisted = dict(_conn(), portal_gym_key=row["gym_id"], status="connected")
    state = {"changed": False}
    original_claim = guard.claim
    def claim(*args):
        result = original_claim(*args)
        if events.count("claim") == 2:
            state["changed"] = True
            if drift in {"account", "location", "gym", "status"}:
                key = {"account": "zernio_account_id", "location": "gbp_location_id",
                       "gym": "portal_gym_key", "status": "status"}[drift]
                persisted[key] = "changed"
        return result
    monkeypatch.setattr(guard, "claim", claim)
    def connections_for(gym):
        assert gym == row["gym_id"]
        events.append("destination")
        if state["changed"]:
            if drift == "read":
                raise TimeoutError("persisted destination unavailable")
            if drift == "missing":
                return []
            if drift == "duplicate":
                return [deepcopy(persisted), deepcopy(persisted)]
        return [deepcopy(persisted)]
    store.connections_for = connections_for
    client = Provider(events)
    out = invoke(photo, row, _conn(), client, store, token)
    assert out["status"] == "approved" and out["held"] == "forward_media_verification"
    assert events == ["claim", "destination", "claim", "atomic", "destination"]
    assert client.calls == []


@pytest.mark.parametrize("photo", [False, True])
def test_mid_authority_destination_change_releases_unsent_owned_lease(monkeypatch, photo):
    row = _row(id="r1", gym_id="lasso", account="googlebusiness",
               gbp_location_id="locations/1", format="photo" if photo else "feed")
    store = _TokenStore([row], {"lasso": [dict(_c(), portal_gym_key="lasso")]})
    events = []
    def authorize(*args):
        events.append("authorize")
        if len(events) == 2:
            store._conns["lasso"][0]["status"] = "needs_reconnect"
        return True
    monkeypatch.setattr(bridge, "authorize", authorize)
    monkeypatch.setattr(gw, "_atomic_gbp_send_hold", lambda *a: {
        "ok": False, "status": "approved", "late_post_id": "", "reject_reason": "destination changed",
        "held": "forward_media_verification", "mode": ""})
    client = Provider(events)
    out = gw.publish_due_gbp(store, client, run_date="2026-09-01", draft=False)
    assert out["held"] == 1 and client.calls == []
    assert events == ["authorize", "authorize"]
    assert store.released == [("r1", "approved")] and store.tokens == {}


@pytest.mark.parametrize("photo", [False, True])
def test_missing_row_location_holds_even_with_singleton_connection(monkeypatch, photo):
    monkeypatch.setattr(bridge, "authorize", lambda *a: True)
    row = _row(gym_id="gym", account="googlebusiness")
    store = SimpleNamespace(connections_for=lambda _: pytest.fail("unbound location reached lookup"))
    client = Provider([])
    out = invoke(photo, row, _conn(), client, store)
    assert out["held"] == "forward_media_verification" and client.calls == []


@pytest.mark.parametrize("photo", [False, True])
def test_facade_connection_reread_uses_same_authenticated_calendar_store(monkeypatch, photo):
    row, token, base, events = authority(monkeypatch, photo)
    original_get = base._client().get
    def get(url, **kwargs):
        if url == "gym_gbp_connections":
            assert kwargs["params"]["portal_gym_key"] == "eq." + row["gym_id"]
            connections = base.connections_for(row["gym_id"])
            return SimpleNamespace(status_code=200, json=lambda: connections)
        return original_get(url, **kwargs)
    http = base._client()
    http.get = get
    base._client = lambda: http
    client = Provider(events)
    out = invoke(photo, row, _conn(), client, SimpleNamespace(_s=base), token)
    assert out["ok"] and events == ["claim", "destination", "claim", "atomic", "destination", "create"]


@pytest.mark.parametrize("field", ["content", "mediaItems", "platforms"])
def test_local_post_payload_drift_holds(monkeypatch, field):
    row, conn, captured = _row(), _conn(), {}
    original = gw.build_gbp_payload_for_row
    def build(*args):
        captured["payload"] = original(*args)
        return captured["payload"]
    monkeypatch.setattr(gw, "build_gbp_payload_for_row", build)
    def authorize(*args):
        captured["payload"][field] = "changed"
        return True
    monkeypatch.setattr(bridge, "authorize", authorize)
    client = Provider([])
    out = invoke(False, row, conn, client)
    assert out["held"] == "forward_media_verification" and client.calls == []


@pytest.mark.parametrize("photo", [False, True])
@pytest.mark.parametrize("kind", ["verification", "duplicate"])
def test_final_hold_releases_owned_unsent_scheduler_lease(monkeypatch, photo, kind):
    events = []
    def authorize(*args):
        events.append("authorize")
        if len(events) == 2:
            cls = guard.ForwardMediaDuplicateHold if kind == "duplicate" else guard.ForwardMediaVerificationHold
            raise cls("current claim refused")
        return True
    monkeypatch.setattr(bridge, "authorize", authorize)
    row = _row(id="r1", gym_id="lasso", account="googlebusiness",
               gbp_location_id="locations/1", format="photo" if photo else "feed")
    store = _TokenStore([row], {"lasso": [dict(_c(), portal_gym_key="lasso")]})
    client = Provider(events)
    out = gw.publish_due_gbp(store, client, run_date="2026-09-01", draft=False)
    assert out["held"] == 1 and client.calls == []
    assert store.released == [("r1", "approved")] and store.tokens == {}


@pytest.mark.parametrize("photo", [False, True])
@pytest.mark.parametrize("outcome", ["timeout", "missing_id"])
def test_after_create_ambiguity_retains_owned_lease_without_retry(monkeypatch, photo, outcome):
    events = []
    def authorize(*a):
        events.append("authorize")
        return True
    monkeypatch.setattr(bridge, "authorize", authorize)
    monkeypatch.setattr(gw, "_atomic_gbp_send_hold", lambda *a: None)
    row = _row(id="r1", gym_id="lasso", account="googlebusiness",
               gbp_location_id="locations/1", format="photo" if photo else "feed")
    store = _TokenStore([row], {"lasso": [dict(_c(), portal_gym_key="lasso")]})
    client = Provider(events, exc=TimeoutError("lost") if outcome == "timeout" else None,
                      response={"ok": True})
    out = gw.publish_due_gbp(store, client, run_date="2026-09-01", draft=False)
    assert out["held"] == 1 and events == ["authorize", "authorize", "create"]
    assert len(client.calls) == 1 and store.tokens and store.released == []


@pytest.mark.parametrize("photo", [False, True])
def test_off_preserves_call_arguments_and_has_no_new_authority_read(monkeypatch, photo):
    monkeypatch.delenv("AGENT_FORWARD_MEDIA_GUARD", raising=False)
    original, seen = bridge.authorize, []
    def authorize(*a):
        seen.append(a)
        return original(*a)
    monkeypatch.setattr(bridge, "authorize", authorize)
    client, row, conn = Provider([]), _row(), _conn()
    assert invoke(photo, row, conn, client)["ok"]
    assert len(seen) == 1  # the existing no-op preflight only
    if photo:
        assert client.calls == [(conn["zernio_account_id"], row["image_url"])]
    else:
        assert client.calls == [(gw.build_gbp_payload_for_row(row, conn), {"draft": False})]


@pytest.mark.parametrize('result', [None, False, 1, 'true', {}, []])
def test_atomic_rpc_must_return_literal_true(result):
    seen = []
    def post(endpoint, **kwargs):
        seen.append((endpoint, kwargs['json']))
        return SimpleNamespace(status_code=200, json=lambda: result)
    store = SimpleNamespace(_client=lambda: SimpleNamespace(post=post),
                            _rest=lambda p: p, _headers=lambda *a: {})
    row = _row(id=str(uuid4()), gym_id='gym')
    out = gw._atomic_gbp_send_hold(store, row, _conn(), str(uuid4()), 'post')
    assert out['held'] == 'forward_media_verification'
    assert seen[0][0] == 'rpc/fixer_authorize_gbp_forward_send_20261008'
    assert set(seen[0][1]['p_expected_creative']) == set(gw._GBP_SEND_CREATIVE)
