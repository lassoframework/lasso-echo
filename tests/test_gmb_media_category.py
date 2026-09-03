"""Google REQUIRES a photo category on gmb-media, and only one value is safe to default to.

2026-09-03: crossfitnine7f7dadc's daily Google Business photo drop failed with
400 INVALID_ARGUMENT, "Photo must specify either category or price list item id".
The payload was {"mediaFormat": "PHOTO", "sourceUrl": ...} with no category.

The category is not a free choice. Of the ten values Google accepts, COVER, PROFILE and
LOGO REPLACE the gym's existing branding on its own listing, so an automated daily drop
must never select them. The rest assert what is IN the photo, which Echo does not know.
ADDITIONAL is the general gallery bucket and the only defensible default.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import zernio as z  # noqa: E402


class _Recorder:
    """Captures the outbound payload without any network."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})

        class _R:
            status_code = 200
            text = '{"id":"m1"}'

            @staticmethod
            def json():
                return {"id": "m1"}
        return _R()


def _client(rec):
    c = z.ZernioClient(http=rec, api_key="k")
    return c


def test_payload_now_carries_a_category(monkeypatch):
    """The regression: without this key Google returns 400 INVALID_ARGUMENT."""
    rec = _Recorder()
    _client(rec).create_gmb_media("acct1", "https://x/y.jpg")
    payload = rec.calls[0]["json"]
    assert payload["category"] == "ADDITIONAL"
    assert payload["sourceUrl"] == "https://x/y.jpg"
    assert payload["mediaFormat"] == "PHOTO"


def test_default_is_the_additive_gallery_bucket():
    assert z.GBP_GALLERY_CATEGORY == "ADDITIONAL"
    assert z.GBP_GALLERY_CATEGORY not in z._GMB_BRANDING_CATEGORIES


@pytest.mark.parametrize("bad", ["COVER", "PROFILE", "LOGO", "cover", " logo "])
def test_branding_categories_are_refused(bad):
    """A daily automated drop must never overwrite a client's cover photo or logo.
    That is not a recoverable mistake, so it fails before any request is made."""
    with pytest.raises(ValueError, match="branding"):
        z._gmb_category(bad)


def test_a_branding_category_never_reaches_the_network():
    rec = _Recorder()
    with pytest.raises(ValueError):
        _client(rec).create_gmb_media("acct1", "https://x/y.jpg", category="LOGO")
    assert rec.calls == [], "must refuse before issuing the request"


def test_an_unknown_category_fails_loudly_here_not_as_an_opaque_google_400():
    with pytest.raises(ValueError, match="unknown GBP photo category"):
        z._gmb_category("INTERIOR_ISH")


@pytest.mark.parametrize("ok", ["EXTERIOR", "INTERIOR", "PRODUCT", "TEAMS",
                                "FOOD_AND_DRINK", "MENU", "additional"])
def test_the_non_branding_categories_are_accepted_when_named_explicitly(ok):
    """Echo does not choose these itself, but a human/caller may."""
    assert z._gmb_category(ok) == ok.strip().upper()


def test_description_is_only_sent_when_present():
    rec = _Recorder()
    _client(rec).create_gmb_media("acct1", "https://x/y.jpg")
    assert "description" not in rec.calls[0]["json"]

    rec2 = _Recorder()
    _client(rec2).create_gmb_media("acct1", "https://x/y.jpg", description="Front desk")
    assert rec2.calls[0]["json"]["description"] == "Front desk"


def test_every_documented_category_is_covered_by_the_allowlist():
    """Pinned against Zernio's gmb-media docs so a doc change is caught here."""
    documented = {"COVER", "PROFILE", "LOGO", "EXTERIOR", "INTERIOR", "FOOD_AND_DRINK",
                  "MENU", "PRODUCT", "TEAMS", "ADDITIONAL"}
    assert set(z.GMB_PHOTO_CATEGORIES) == documented
