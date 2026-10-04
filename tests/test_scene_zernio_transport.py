"""
Tests for agent/scene_zernio_transport — the fail-closed, OFF-by-default
adapter over ZernioClient.create_post_raw / get_post.

Fully OFFLINE: a FakeClient records create_post_raw calls and serves scripted
get_post readbacks; no real network is ever touched.

Covers:
  - happy path: 2xx with a post id + exact readback identity -> published;
  - 409 WITH existingPostId accepted ONLY via a matching readback;
  - 409 WITHOUT existingPostId -> held, no readback trusted;
  - 2xx WITHOUT a post id -> held (never inferred success);
  - readback identity mismatch (account / channel / media / profile drift, and
    an unverifiable readback shape) -> held;
  - disabled-by-default: no flag, no network call, held outcome;
  - immutability: attempt UUID forwarded verbatim and never regenerated;
    frozen inputs reject mutation; invalid attempt ids refuse pre-network.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import scene_zernio_transport as szt  # noqa: E402
from agent import zernio  # noqa: E402

ATTEMPT_ID = "3f6f8f8a-2a1b-4c5d-9e0f-1234567890ab"
MEDIA_URL = "https://r2.example.test/scene/card1.jpg"

TARGET = szt.SceneChannelTarget(
    account_id="acct_1",
    channel="instagram",
    media_url=MEDIA_URL,
    expected_profile_id="prof_1",
)


class FakeClient:
    """Scriptable stand-in for ZernioClient (create_post_raw / get_post only)."""

    def __init__(self):
        self.create_calls = []
        self.get_calls = []
        self.create_result = {"_id": "zpost_1"}
        self.create_error = None
        self.posts = {}          # post_id -> get_post payload
        self.get_error = None

    def create_post_raw(self, payload, *, draft=False, publish_now=True,
                        idempotency_key=None):
        self.create_calls.append({"payload": payload,
                                  "idempotency_key": idempotency_key})
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def get_post(self, post_id):
        self.get_calls.append(str(post_id))
        if self.get_error is not None:
            raise self.get_error
        return self.posts[str(post_id)]


def _readback(post_id="zpost_1", account_id="acct_1", channel="instagram",
              media_url=MEDIA_URL, profile_id="prof_1",
              status="published", platform_status="published"):
    return {"_id": post_id, "profileId": profile_id, "status": status,
            "platforms": [{"accountId": account_id, "platform": channel,
                           "status": platform_status}],
            "mediaItems": [{"type": "image", "url": media_url}]}


@pytest.fixture
def attempt():
    return szt.ScenePublishAttempt(attempt_id=ATTEMPT_ID, target=TARGET,
                                   content="hello scene")


@pytest.fixture
def client():
    c = FakeClient()
    c.posts["zpost_1"] = _readback()
    return c


# ---- happy path ------------------------------------------------------------

def test_happy_path_published_after_exact_readback(attempt, client):
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert out.ok and out.status == szt.STATUS_PUBLISHED
    assert out.post_id == "zpost_1"
    assert out.readback_verified is True
    assert client.create_calls[0]["idempotency_key"] == ATTEMPT_ID
    assert client.get_calls == ["zpost_1"]
    payload = client.create_calls[0]["payload"]
    assert payload["platforms"] == [{"accountId": "acct_1", "platform": "instagram"}]
    assert payload["mediaItems"] == [{"type": "image", "url": MEDIA_URL}]


# ---- 409 handling ------------------------------------------------------------

def _conflict(existing=None):
    detail = ('{"existingPostId": "%s"}' % existing) if existing else \
        '{"error": "duplicate content"}'
    return zernio.ZernioError(409, detail)


def test_409_with_existing_post_id_accepted_via_readback(attempt, client):
    client.create_error = _conflict("zpost_9")
    client.posts["zpost_9"] = _readback(post_id="zpost_9")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert out.ok and out.post_id == "zpost_9" and out.readback_verified
    assert client.get_calls == ["zpost_9"]


def test_409_without_existing_post_id_held(attempt, client):
    client.create_error = _conflict(None)
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.status == szt.STATUS_HELD
    assert "existingPostId" in out.reason
    assert client.get_calls == []  # nothing verifiable -> no readback trusted


def test_409_existing_post_id_readback_mismatch_held(attempt, client):
    client.create_error = _conflict("zpost_9")
    client.posts["zpost_9"] = _readback(post_id="zpost_9", account_id="acct_OTHER")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.post_id == "zpost_9"


# ---- 2xx without a post id ---------------------------------------------------

@pytest.mark.parametrize("body", [{}, {"status": "ok"}, {"post": {}}, {"data": {"x": 1}}])
def test_2xx_without_post_id_held(attempt, client, body):
    client.create_result = body
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.status == szt.STATUS_HELD
    assert "no post id" in out.reason
    assert client.get_calls == []


# ---- readback identity mismatch ----------------------------------------------

@pytest.mark.parametrize("drift", [
    {"account_id": "acct_2"},
    {"channel": "facebook"},
    {"media_url": "https://r2.example.test/scene/OTHER.jpg"},
    {"profile_id": "prof_2"},
])
def test_readback_identity_mismatch_held(attempt, client, drift):
    client.posts["zpost_1"] = _readback(**drift)
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.status == szt.STATUS_HELD
    assert "does not prove" in out.reason


@pytest.mark.parametrize("bad", [
    {"_id": "zpost_1"},                                   # no identity fields
    {"_id": "zpost_1", "platforms": []},                  # empty platforms
    "not-a-dict",                                          # wrong shape entirely
    {"_id": "zpost_1", "profileId": "prof_1",
     "platforms": [{"accountId": "acct_1", "platform": "instagram"}]},
                                                           # no mediaItems
])
def test_unverifiable_readback_shape_held(attempt, client, bad):
    client.posts["zpost_1"] = bad
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


@pytest.mark.parametrize("extra_field, extra_value", [
    ("platforms", {"accountId": "acct_2", "platform": "facebook", "status": "published"}),
    ("mediaItems", {"type": "image", "url": "https://r2.example.test/scene/other.jpg"}),
])
def test_unexpected_extra_destination_or_media_held(attempt, client,
                                                     extra_field, extra_value):
    readback = _readback()
    readback[extra_field].append(extra_value)
    client.posts["zpost_1"] = readback
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.status == szt.STATUS_HELD


def test_readback_failure_held(attempt, client):
    client.get_error = zernio.ZernioError(500, "boom")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and "readback" in out.reason


def test_non_409_provider_error_held(attempt, client):
    client.create_error = zernio.ZernioError(500, "boom")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and "500" in out.reason


def test_transport_exception_held(attempt, client):
    client.create_error = TimeoutError("slow")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and "ambiguous" in out.reason


# ---- disabled by default -------------------------------------------------------

def test_disabled_by_default_no_network(attempt, client, monkeypatch):
    monkeypatch.delenv(szt.ENABLED_ENV, raising=False)
    out = szt.SceneZernioTransport(client=client).publish(attempt)  # enabled=None
    assert not out.ok and "disabled" in out.reason
    assert client.create_calls == [] and client.get_calls == []


def test_explicit_disabled_constructor_no_network(attempt, client):
    out = szt.SceneZernioTransport(client=client, enabled=False).publish(attempt)
    assert not out.ok
    assert client.create_calls == []


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_env_flag_truthy_enables(value, monkeypatch):
    monkeypatch.setenv(szt.ENABLED_ENV, value)
    assert szt.transport_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "enabled", "2"])
def test_env_flag_unrecognised_stays_off(value, monkeypatch):
    monkeypatch.setenv(szt.ENABLED_ENV, value)
    assert szt.transport_enabled() is False


# ---- immutability + attempt-UUID ownership -------------------------------------

def test_attempt_uuid_forwarded_verbatim_never_generated(attempt, client):
    szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert client.create_calls[0]["idempotency_key"] == ATTEMPT_ID
    # The recorded key IS the caller's string, byte for byte.
    assert isinstance(client.create_calls[0]["idempotency_key"], str)
    assert uuid.UUID(client.create_calls[0]["idempotency_key"]) == uuid.UUID(ATTEMPT_ID)


def test_invalid_attempt_id_refused(client):
    for bad in ("", "not-a-uuid", None, 12345):
        with pytest.raises(ValueError):
            szt.ScenePublishAttempt(attempt_id=bad, target=TARGET)
    assert client.create_calls == []  # refused before any send


def test_frozen_inputs_reject_mutation(attempt):
    with pytest.raises(Exception):
        attempt.attempt_id = str(uuid.uuid4())
    with pytest.raises(Exception):
        attempt.target.account_id = "acct_2"
    with pytest.raises(Exception):
        attempt.content = "rewritten"
    out = szt.SceneTransportOutcome(status=szt.STATUS_HELD, attempt_id=ATTEMPT_ID)
    with pytest.raises(Exception):
        out.status = szt.STATUS_PUBLISHED


def test_publish_requires_attempt_object(client):
    with pytest.raises(TypeError):
        szt.SceneZernioTransport(client=client, enabled=True).publish(
            {"attempt_id": ATTEMPT_ID})


def test_replayed_attempt_uses_same_key(attempt, client):
    t = szt.SceneZernioTransport(client=client, enabled=True)
    t.publish(attempt)
    t.publish(attempt)
    keys = [c["idempotency_key"] for c in client.create_calls]
    assert keys == [ATTEMPT_ID, ATTEMPT_ID]


# ---- P1 adversarial: readback must prove the SAME post, delivered -------------

def test_readback_post_id_must_equal_requested(attempt, client):
    """A get_post that answers with a DIFFERENT post's body never verifies the
    one we asked about."""
    client.posts["zpost_1"] = _readback(post_id="zpost_OTHER")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.post_id == "zpost_1"


@pytest.mark.parametrize("status", ["failed", "pending", "scheduled",
                                    "processing", "draft", "queued", "",
                                    None, "publshed", "PUBLISHED-ish"])
def test_non_conclusive_top_level_status_held(attempt, client, status):
    client.posts["zpost_1"] = _readback(status=status)
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


@pytest.mark.parametrize("status", list(szt._DELIVERED_STATES))
def test_every_delivered_state_accepted(attempt, client, status):
    client.posts["zpost_1"] = _readback(status=status, platform_status=status)
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert out.ok, status


@pytest.mark.parametrize("pstatus", ["failed", "pending", "scheduled",
                                     "processing", "draft", "", None])
def test_non_conclusive_platform_status_held(attempt, client, pstatus):
    """A published top-level status does NOT rescue a platform entry that has
    not conclusively delivered (the failed_watch lesson: one platform can fail
    while the top-level status stays published)."""
    client.posts["zpost_1"] = _readback(platform_status=pstatus)
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_platform_entry_missing_status_field_held(attempt, client):
    body = _readback()
    del body["platforms"][0]["status"]
    client.posts["zpost_1"] = body
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_platform_entry_for_wrong_account_held(attempt, client):
    body = _readback()
    body["platforms"][0]["accountId"] = "acct_2"
    client.posts["zpost_1"] = body
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_platform_entry_for_other_channel_only_held(attempt, client):
    """A readback reporting only a facebook delivery never verifies an
    instagram attempt, even if that entry is published."""
    body = _readback()
    body["platforms"] = [{"accountId": "acct_1", "platform": "facebook",
                          "status": "published"}]
    client.posts["zpost_1"] = body
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_multiple_ambiguous_platform_entries_held(attempt, client):
    """Two entries for the target channel (even both 'published') is a shape we
    refuse to judge — held, never a guess."""
    body = _readback()
    body["platforms"].append({"accountId": "acct_1", "platform": "instagram",
                              "status": "published"})
    client.posts["zpost_1"] = body
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_mixed_multi_platform_entries_held(attempt, client):
    body = _readback()
    body["platforms"].append({"accountId": "acct_9", "platform": "instagram",
                              "status": "failed"})
    client.posts["zpost_1"] = body
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_unexpected_other_channel_entry_held(attempt, client):
    """The request named one channel, so an extra destination stays held."""
    body = _readback()
    body["platforms"].append({"accountId": "acct_fb", "platform": "facebook",
                              "status": "published"})
    client.posts["zpost_1"] = body
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.status == szt.STATUS_HELD


def test_wrapped_post_shape_verifies(attempt, client):
    """{post: {...}} / {data: {...}} wrappers verify identically."""
    for wrap in ("post", "data"):
        client.posts["zpost_1"] = {wrap: _readback()}
        out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
        assert out.ok, wrap


def test_readback_status_matrix_pure():
    """The pure gate directly: success requires BOTH levels conclusive."""
    ok = _readback()
    assert szt.readback_matches(ok, TARGET, post_id="zpost_1") is True
    assert szt.readback_matches(ok, TARGET, post_id="zpost_2") is False
    for bad_top in ("failed", "pending", "draft", None):
        assert szt.readback_matches(_readback(status=bad_top), TARGET,
                                    post_id="zpost_1") is False
    for bad_plat in ("failed", "pending", "", None):
        assert szt.readback_matches(_readback(platform_status=bad_plat), TARGET,
                                    post_id="zpost_1") is False


# ---- P2 adversarial: existingPostId extraction must never throw or guess ------

@pytest.mark.parametrize("detail", [
    '[{"existingPostId": "zp_1"}]',       # JSON LIST body
    '["zp_1"]',                            # JSON list of scalars
    '"zp_1"',                              # JSON scalar string body
    '409',                                 # JSON scalar number body
    'true',                                # JSON scalar bool body
    'null',                                # JSON null body
    'duplicate content',                   # not JSON at all
    '',                                    # empty
    None,                                  # absent
    '{"existingPostId": null}',
    '{"existingPostId": ""}',
    '{"existingPostId": "   "}',
    '{"existingPostId": {"id": "zp_1"}}',  # nested dict value
    '{"existingPostId": ["zp_1"]}',        # list value
    '{"existingPostId": true}',            # bool value
    '{"existingPostId": ',                 # malformed JSON
    '{"error": "duplicate"}',              # dict without the field
])
def test_existing_post_id_rejects_garbage(detail):
    assert szt._existing_post_id(detail) == ""


@pytest.mark.parametrize("detail,expected", [
    ('{"existingPostId": "zp_1"}', "zp_1"),
    ('{"error": "duplicate", "existingPostId": "zp_9"}', "zp_9"),
    ({"existingPostId": "zp_2"}, "zp_2"),          # already-parsed dict
    ('{"existingPostId": " zp_3 "}', "zp_3"),      # whitespace trimmed
])
def test_existing_post_id_accepts_only_a_verifiable_id(detail, expected):
    assert szt._existing_post_id(detail) == expected


def test_409_list_body_held_without_throw(attempt, client):
    client.create_error = zernio.ZernioError(409, '[{"existingPostId": "zp_1"}]')
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and client.get_calls == []


def test_409_scalar_body_held_without_throw(attempt, client):
    client.create_error = zernio.ZernioError(409, "duplicate content")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and client.get_calls == []


def test_409_nested_dict_value_held_without_throw(attempt, client):
    client.create_error = zernio.ZernioError(
        409, '{"existingPostId": {"id": "zp_1"}}')
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and client.get_calls == []
