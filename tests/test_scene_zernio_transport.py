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


def _mk_target(content="hello scene", **kw):
    """A SceneChannelTarget with a VALID payload digest binding `content`.
    The digest is computed over the exact canonical payload, mirroring how a
    real caller freezes an attempt."""
    base = dict(account_id="acct_1", channel="instagram", media_url=MEDIA_URL,
                expected_profile_id="prof_1")
    base.update(kw)
    probe = szt.SceneChannelTarget(payload_sha256="0" * 64, **base)
    digest = szt.canonical_payload_digest(szt.canonical_payload(probe, content))
    return szt.SceneChannelTarget(payload_sha256=digest, **base)


TARGET = _mk_target()


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
        self.create_calls.append({"payload": payload, "draft": draft,
                                  "publish_now": publish_now,
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
              status="published", platform_status="published",
              content_type="feed", page_id=None, content="hello scene",
              media_type="image"):
    psd = {"contentType": content_type} if content_type is not None else None
    if page_id is not None:
        psd = psd or {}
        psd["pageId"] = page_id
    entry = {"accountId": account_id, "platform": channel,
             "status": platform_status}
    if psd is not None:
        entry["platformSpecificData"] = psd
    body = {"_id": post_id, "profileId": profile_id, "status": status,
            "platforms": [entry],
            "mediaItems": [{"type": media_type, "url": media_url}]}
    if content is not None:
        body["content"] = content
    return body


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
    assert szt.readback_matches(ok, TARGET, post_id="zpost_1",
                                expected_content="hello scene") is True
    assert szt.readback_matches(ok, TARGET, post_id="zpost_2",
                                expected_content="hello scene") is False
    # Content binding is mandatory: no expected_content never matches.
    assert szt.readback_matches(ok, TARGET, post_id="zpost_1") is False
    for bad_top in ("failed", "pending", "draft", None):
        assert szt.readback_matches(_readback(status=bad_top), TARGET,
                                    post_id="zpost_1",
                                    expected_content="hello scene") is False
    for bad_plat in ("failed", "pending", "", None):
        assert szt.readback_matches(_readback(platform_status=bad_plat), TARGET,
                                    post_id="zpost_1",
                                    expected_content="hello scene") is False


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


# ---- surface / destination / payload-digest binding ---------------------------

STORY_TARGET = _mk_target("hi", channel="facebook", content_type="story",
                          page_id="page_9")


def test_surface_must_be_explicit_validated():
    with pytest.raises(ValueError):
        szt.SceneChannelTarget(account_id="a", channel="instagram",
                               media_url=MEDIA_URL, expected_profile_id="p",
                               content_type="reel")  # not a declared surface


def test_story_payload_mirrors_zernio_builder():
    """platformSpecificData.contentType='story' + pageId, exactly as
    agent/zernio.py create_post assembles it."""
    payload = szt.canonical_payload(STORY_TARGET, "hi")
    entry = payload["platforms"][0]
    assert entry["platformSpecificData"] == {"contentType": "story",
                                             "pageId": "page_9"}


def test_feed_payload_omits_platform_specific_data():
    payload = szt.canonical_payload(TARGET, "hi")
    assert "platformSpecificData" not in payload["platforms"][0]


def test_story_readback_proven_when_surface_and_page_match():
    client = FakeClient()
    client.posts["zpost_1"] = _readback(channel="facebook", content_type="story",
                                        page_id="page_9", content="hi")
    attempt = szt.ScenePublishAttempt(attempt_id=ATTEMPT_ID,
                                      target=STORY_TARGET, content="hi")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert out.ok and out.readback_verified


def test_readback_missing_content_type_held_even_for_feed(attempt, client):
    """Surface is never inferred: a readback without contentType proves
    nothing — not even a feed."""
    client.posts["zpost_1"] = _readback(content_type=None)
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.status == szt.STATUS_HELD


def test_surface_drift_story_vs_feed_held(attempt, client):
    client.posts["zpost_1"] = _readback(content_type="story")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_page_drift_held():
    client = FakeClient()
    attempt = szt.ScenePublishAttempt(attempt_id=ATTEMPT_ID,
                                      target=STORY_TARGET, content="hi")
    for bad in (None, "page_OTHER", ""):
        client.posts["zpost_1"] = _readback(channel="facebook",
                                            content_type="story", page_id=bad,
                                            content="hi")
        out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
        assert not out.ok, bad


def test_undeclared_readback_page_held_when_target_has_none(attempt, client):
    """Target declared no page; a readback pageId is destination drift."""
    client.posts["zpost_1"] = _readback(page_id="page_9")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_same_account_on_another_profile_held(attempt, client):
    client.posts["zpost_1"] = _readback(profile_id="prof_OTHER")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def _digest_bound_attempt(content="bind me"):
    target = _mk_target(content)
    return szt.ScenePublishAttempt(attempt_id=ATTEMPT_ID, target=target,
                                   content=content)


def test_payload_digest_match_publishes_with_content_proof(client):
    attempt = _digest_bound_attempt()
    client.posts["zpost_1"] = _readback(content="bind me")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert out.ok and out.readback_verified


def test_payload_digest_mismatch_refuses_pre_network(client):
    target = szt.SceneChannelTarget(
        account_id="acct_1", channel="instagram", media_url=MEDIA_URL,
        expected_profile_id="prof_1", payload_sha256="0" * 64)
    attempt = szt.ScenePublishAttempt(attempt_id=ATTEMPT_ID, target=target)
    with pytest.raises(ValueError):
        szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert client.create_calls == [] and client.get_calls == []


def test_payload_digest_bound_readback_without_content_held(client):
    """Digest binding requires the readback to prove the content verbatim."""
    attempt = _digest_bound_attempt()
    client.posts["zpost_1"] = _readback(content=None)  # no content field
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok
    client.posts["zpost_1"] = _readback(content="different words")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


# ---- adversarial: stale idempotency / digest / media-type rails ---------------

def test_stale_409_existing_post_with_different_caption_held(attempt, client):
    """A 409 existingPostId whose readback is a delivered post with a
    DIFFERENT caption is a stale idempotency hit, never PUBLISHED."""
    client.create_error = _conflict("zpost_stale")
    client.posts["zpost_stale"] = _readback(post_id="zpost_stale",
                                            content="a completely different caption")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.status == szt.STATUS_HELD
    assert out.post_id == "zpost_stale"


def test_stale_409_readback_without_content_field_held(attempt, client):
    """If the provider readback does not expose the content field, the
    transport cannot prove content binding and must hold."""
    client.create_error = _conflict("zpost_stale")
    client.posts["zpost_stale"] = _readback(post_id="zpost_stale", content=None)
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_missing_payload_digest_refused_at_construction():
    """payload_sha256 is MANDATORY: no default, no empty-string fallback."""
    with pytest.raises(ValueError):  # omitted -> empty -> refused
        szt.SceneChannelTarget(account_id="a", channel="instagram",
                               media_url=MEDIA_URL, expected_profile_id="p")
    with pytest.raises(ValueError):  # explicit empty string
        szt.SceneChannelTarget(account_id="a", channel="instagram",
                               media_url=MEDIA_URL, expected_profile_id="p",
                               payload_sha256="")


@pytest.mark.parametrize("bad", [
    "0" * 63, "0" * 65, "g" * 64, "0x" + "0" * 62, 42, None,
    " " + "0" * 63 + " Z",
])
def test_invalid_payload_digest_refused_before_network(bad, client):
    with pytest.raises((ValueError, TypeError)):
        szt.SceneChannelTarget(account_id="a", channel="instagram",
                               media_url=MEDIA_URL, expected_profile_id="p",
                               payload_sha256=bad)
    assert client.create_calls == [] and client.get_calls == []


def test_uppercase_hex_digest_normalized_and_accepted():
    digest = szt.canonical_payload_digest(
        szt.canonical_payload(TARGET, "hello scene"))
    target = _mk_target("hello scene",
                        )  # same digest, lowercase
    assert target.payload_sha256 == digest
    upper = szt.SceneChannelTarget(
        account_id="acct_1", channel="instagram", media_url=MEDIA_URL,
        expected_profile_id="prof_1", payload_sha256=digest.upper())
    assert upper.payload_sha256 == digest


def test_readback_media_type_drift_held(attempt, client):
    """Same singleton media URL but type 'video' where the canonical payload
    expects 'image' is drift — held, never published."""
    client.posts["zpost_1"] = _readback(media_type="video")
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok and out.status == szt.STATUS_HELD


def test_readback_media_type_missing_held(attempt, client):
    body = _readback()
    del body["mediaItems"][0]["type"]
    client.posts["zpost_1"] = body
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert not out.ok


def test_publish_sends_explicit_immediate_send_args(attempt, client):
    """publish() must call create_post_raw with draft=False, publish_now=True
    so the sent body's send mode is explicit and cannot silently default."""
    client.posts["zpost_1"] = _readback()
    out = szt.SceneZernioTransport(client=client, enabled=True).publish(attempt)
    assert out.ok
    call = client.create_calls[0]
    assert call["draft"] is False
    assert call["publish_now"] is True
    # The digested canonical payload IS the final provider body: publishNow
    # is already present, so create_post_raw's body copy is byte-identical.
    assert call["payload"]["publishNow"] is True
    assert (szt.canonical_payload_digest(call["payload"])
            == attempt.target.payload_sha256)


@pytest.mark.parametrize("tamper", [
    lambda p: p.pop("publishNow"),                      # send mode removed
    lambda p: p.__setitem__("publishNow", False),       # immediate -> not now
    lambda p: p.__setitem__("isDraft", True),           # draft smuggled in
])
def test_send_mode_tamper_changes_digest_or_refuses_pre_network(tamper, client):
    """Adversarial: any change to the final send-mode field of the provider
    body must change the digest (so a caller freezing a tampered attempt is
    refused BEFORE any network call) — there is no way to digest one mode
    and send another."""
    payload = szt.canonical_payload(TARGET, "hello scene")
    tampered = dict(payload)
    tamper(tampered)
    assert szt.canonical_payload_digest(tampered) != TARGET.payload_sha256
    # Prove the refusal rail end-to-end: a caller whose frozen digest was
    # computed over a body with the send mode REMOVED cannot publish — the
    # mismatch raises before any network call.
    removed = dict(payload)
    removed.pop("publishNow")
    stale_digest = szt.canonical_payload_digest(removed)
    stale_target = szt.SceneChannelTarget(
        account_id="acct_1", channel="instagram", media_url=MEDIA_URL,
        expected_profile_id="prof_1", payload_sha256=stale_digest)
    stale_attempt = szt.ScenePublishAttempt(attempt_id=ATTEMPT_ID,
                                            target=stale_target,
                                            content="hello scene")
    with pytest.raises(ValueError):
        szt.SceneZernioTransport(client=client, enabled=True).publish(stale_attempt)
    assert client.create_calls == [] and client.get_calls == []
