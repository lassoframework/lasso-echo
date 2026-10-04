"""
Tests for agent/scene_attester_evidence — pure normalization of provider and
absence evidence for a future scene attester. Fully offline: every input is a
hand-built dict/list; the module under test makes no provider or DB calls.

Core contract: DELIVERED_PROVEN only on the full readback gate WITH mandatory
payload-digest content binding; ABSENT_PROVEN is NEVER returned (a pure
helper cannot authenticate provider pagination, so caller-attested
range_complete=True is forgeable and no-send is disabled by construction);
every missing/drifted/malformed/ambiguous input is HELD_UNPROVEN or
AMBIGUOUS.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import scene_attester_evidence as sae  # noqa: E402
from agent import scene_zernio_transport as szt  # noqa: E402

MEDIA_URL = "https://r2.example.test/scene/card1.jpg"


def _mk_target(content, **kw):
    base = dict(account_id="acct_1", channel="instagram", media_url=MEDIA_URL,
                expected_profile_id="prof_1")
    base.update(kw)
    probe = szt.SceneChannelTarget(payload_sha256="0" * 64, **base)
    digest = szt.canonical_payload_digest(szt.canonical_payload(probe, content))
    return szt.SceneChannelTarget(payload_sha256=digest, **base)


CONTENT = "hello scene"
STORY_CONTENT = "hi"

TARGET = _mk_target(CONTENT)
STORY_TARGET = _mk_target(STORY_CONTENT, channel="facebook",
                          content_type="story", page_id="page_9")


def _post(post_id="zpost_1", account_id="acct_1", channel="instagram",
          media_url=MEDIA_URL, profile_id="prof_1", status="published",
          platform_status="published", content_type="feed", page_id=None,
          content=CONTENT, media_type="image"):
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


# ---- delivered evidence --------------------------------------------------------

def test_delivered_proven_on_full_readback():
    v = sae.delivered_evidence(_post(), TARGET, post_id="zpost_1",
                               expected_content=CONTENT)
    assert v["status"] == sae.DELIVERED_PROVEN and v["proven"] is True


def test_delivered_proven_story_with_page():
    v = sae.delivered_evidence(
        _post(channel="facebook", content_type="story", page_id="page_9",
              content=STORY_CONTENT),
        STORY_TARGET, post_id="zpost_1", expected_content=STORY_CONTENT)
    assert v["status"] == sae.DELIVERED_PROVEN


def test_delivered_requires_mandatory_content_binding():
    """No expected_content -> HELD_UNPROVEN even on a perfect readback; an
    expected_content the target digest does not bind -> HELD_UNPROVEN."""
    v = sae.delivered_evidence(_post(), TARGET, post_id="zpost_1")
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("content binding is mandatory" in r for r in v["reasons"])
    v = sae.delivered_evidence(_post(content="unbound words"), TARGET,
                               post_id="zpost_1",
                               expected_content="unbound words")
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("not bound by the target's payload_sha256" in r
               for r in v["reasons"])


@pytest.mark.parametrize("body", ["not-a-dict", 42, None, ["x"]])
def test_malformed_body_ambiguous(body):
    v = sae.delivered_evidence(body, TARGET, post_id="zpost_1",
                               expected_content=CONTENT)
    assert v["status"] == sae.AMBIGUOUS and v["proven"] is False


def test_post_id_drift_held():
    v = sae.delivered_evidence(_post(post_id="zpost_2"), TARGET,
                               post_id="zpost_1", expected_content=CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("post id drift" in r for r in v["reasons"])


@pytest.mark.parametrize("status", ["failed", "pending", "draft", "scheduled",
                                    "", None])
def test_non_conclusive_status_held(status):
    v = sae.delivered_evidence(_post(status=status), TARGET, post_id="zpost_1",
                               expected_content=CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN
    v = sae.delivered_evidence(_post(platform_status=status), TARGET,
                               post_id="zpost_1", expected_content=CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN


def test_duplicate_destination_held():
    body = _post()
    body["platforms"].append({"accountId": "acct_2", "platform": "facebook",
                              "status": "published"})
    v = sae.delivered_evidence(body, TARGET, post_id="zpost_1",
                               expected_content=CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("singleton destination" in r for r in v["reasons"])


def test_duplicate_media_held():
    body = _post()
    body["mediaItems"].append({"type": "image", "url": MEDIA_URL + ".2"})
    v = sae.delivered_evidence(body, TARGET, post_id="zpost_1",
                               expected_content=CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("singleton exact media" in r for r in v["reasons"])


def test_media_type_drift_held():
    """Same singleton URL but video-vs-image type drift is held."""
    v = sae.delivered_evidence(_post(media_type="video"), TARGET,
                               post_id="zpost_1", expected_content=CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("media type drift" in r for r in v["reasons"])


def test_missing_surface_never_inferred_held():
    v = sae.delivered_evidence(_post(content_type=None), TARGET,
                               post_id="zpost_1", expected_content=CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("surface not explicit" in r for r in v["reasons"])


def test_surface_drift_held():
    v = sae.delivered_evidence(_post(content_type="story"), TARGET,
                               post_id="zpost_1", expected_content=CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("surface drift" in r for r in v["reasons"])


@pytest.mark.parametrize("page_id", [None, "page_OTHER"])
def test_page_drift_held(page_id):
    v = sae.delivered_evidence(
        _post(channel="facebook", content_type="story", page_id=page_id,
              content=STORY_CONTENT),
        STORY_TARGET, post_id="zpost_1", expected_content=STORY_CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("page drift" in r or "surface not explicit" in r
               for r in v["reasons"])


def test_same_account_on_another_profile_held():
    v = sae.delivered_evidence(_post(profile_id="prof_OTHER"), TARGET,
                               post_id="zpost_1", expected_content=CONTENT)
    assert v["status"] == sae.HELD_UNPROVEN
    assert any("profile drift" in r for r in v["reasons"])


def test_content_binding_required_when_expected_content_given():
    bound = _mk_target("bind me")
    assert sae.delivered_evidence(_post(content="bind me"), bound,
                                  post_id="zpost_1",
                                  expected_content="bind me")["status"] == \
        sae.DELIVERED_PROVEN
    for bad in (_post(content=None), _post(content="other words")):
        v = sae.delivered_evidence(bad, bound, post_id="zpost_1",
                                   expected_content="bind me")
        assert v["status"] == sae.HELD_UNPROVEN


# ---- absence evidence: ABSENT_PROVEN is disabled by construction --------------

def test_absent_proven_constant_is_reserved_never_returned():
    """The vocabulary constant survives for historic-verdict parsing only."""
    assert sae.ABSENT_PROVEN == "absent_proven"


def test_complete_range_without_match_is_ambiguous_never_absent_proven():
    others = [_post(post_id="zp_a", account_id="acct_2"),
              _post(post_id="zp_b", channel="facebook", content_type="story",
                    page_id="page_1")]
    v = sae.absence_evidence(others, range_complete=True, target=TARGET)
    assert v["status"] == sae.AMBIGUOUS and v["proven"] is False


def test_caller_forged_complete_empty_list_stays_ambiguous():
    """An adversarial caller attesting range_complete=True over an EMPTY list
    must never mint ABSENT_PROVEN — pagination cannot be authenticated by a
    pure helper, so no-send is disabled by construction."""
    v = sae.absence_evidence([], range_complete=True, target=TARGET)
    assert v["status"] == sae.AMBIGUOUS and v["proven"] is False
    assert v["status"] != sae.ABSENT_PROVEN


@pytest.mark.parametrize("flag", [False, None, "yes", 1])
def test_incomplete_range_never_proves_absence(flag):
    v = sae.absence_evidence([], range_complete=flag, target=TARGET)
    assert v["status"] == sae.AMBIGUOUS and v["proven"] is False


def test_non_list_range_ambiguous():
    v = sae.absence_evidence({"posts": []}, range_complete=True, target=TARGET)
    assert v["status"] == sae.AMBIGUOUS


def test_matching_post_in_range_disproves_absence():
    v = sae.absence_evidence([_post()], range_complete=True, target=TARGET)
    assert v["status"] == sae.HELD_UNPROVEN and v["proven"] is False
    assert v["post_id"] == "zpost_1"


def test_matching_story_post_disproves_story_absence():
    v = sae.absence_evidence(
        [_post(channel="facebook", content_type="story", page_id="page_9")],
        range_complete=True, target=STORY_TARGET)
    assert v["status"] == sae.HELD_UNPROVEN


def test_surface_drifted_post_does_not_match_feed_absence():
    """A story post is NOT the feed attempt — but with absence proof disabled
    the no-match outcome is AMBIGUOUS, not ABSENT_PROVEN."""
    v = sae.absence_evidence([_post(content_type="story")],
                             range_complete=True, target=TARGET)
    assert v["status"] == sae.AMBIGUOUS


@pytest.mark.parametrize("bad", [
    "garbage",
    {"_id": "zp_x"},                                  # no identity at all
    {"_id": "zp_x", "profileId": "prof_1",
     "platforms": [{"accountId": "acct_1", "platform": "instagram"}]},
                                                       # no explicit surface
])
def test_unparseable_range_post_ambiguous(bad):
    v = sae.absence_evidence([_post(post_id="zp_ok", account_id="acct_2"), bad],
                             range_complete=True, target=TARGET)
    assert v["status"] == sae.AMBIGUOUS


def test_no_provider_or_db_calls_in_module():
    import inspect
    src = inspect.getsource(sae)
    for forbidden in ("requests", "urllib", "socket", "urlopen", "sqlite",
                      "psycopg", ".execute(", "ZernioClient("):
        assert forbidden not in src
