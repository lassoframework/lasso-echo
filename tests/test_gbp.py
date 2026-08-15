"""
GBP pure helpers (agent/gbp.py): caption A+ gate, UTM slugging, Zernio payload rules
(OFFER omits callToAction, CALL exempt from UTM, EVENT/OFFER need a schedule), and the
crop-before-approval image pipeline. Fully offline.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gbp  # noqa: E402


# ---- caption A+ gate (§5.2) ------------------------------------------------

def _good():
    return ("Carmel moms: 6 weeks to your first real strength milestone. Small group "
            "coaching that fits a packed schedule, with a plan you can actually keep.")


def test_good_caption_passes():
    assert gbp.caption_issues(_good(), city="Carmel") == []


def test_hashtag_rejected():
    assert any("hashtag" in i for i in gbp.caption_issues(_good() + " #gym"))


def test_phone_number_rejected():
    assert any("phone" in i for i in gbp.caption_issues(
        "Call us today at (317) 555-0198 to start training in Carmel this week now.",
        city="Carmel"))
    # a bare year or small number is not a phone number
    assert not gbp.has_phone("Coaching since 2015 for 12 week blocks in Carmel today.")


def test_dash_rejected():
    assert any("dash" in i for i in gbp.caption_issues(
        "Carmel strength - built for busy parents who want a plan that actually sticks."))


def test_city_must_be_named():
    assert any("city" in i for i in gbp.caption_issues(_good(), city="Fishers"))
    assert gbp.caption_issues(_good(), city="Carmel") == []


def test_over_cap_and_thin_rejected():
    assert any("cap" in i for i in gbp.caption_issues("x " * 800, city=""))
    assert any("thin" in i for i in gbp.caption_issues("Too short.", city=""))


# ---- UTM (§5.2) ------------------------------------------------------------

def test_pillar_slug():
    assert gbp.pillar_slug("New Year Reset") == "new_year_reset"
    assert gbp.pillar_slug("40+ Reclaim!!") == "40_reclaim"
    assert gbp.pillar_slug("") == "post"


def test_utm_url_appends_and_respects_existing_query():
    u = gbp.utm_url("https://gym.com/start", "Local Update")
    assert u == ("https://gym.com/start?utm_source=google&utm_medium=organic_gbp"
                 "&utm_campaign=echo_local_update")
    u2 = gbp.utm_url("https://gym.com/start?ref=a", "Offer")
    assert "?ref=a&utm_source=google" in u2
    assert gbp.utm_url("", "x") == ""          # CALL has no url


# ---- payload rules (§7.1) --------------------------------------------------

def test_standard_gets_utm_cta():
    pd = gbp.build_platform_data(account_id="acc1", topic_type="STANDARD",
                                 location_id="locations/1", pillar="Local Update",
                                 cta_type="LEARN_MORE", cta_url="https://gym.com/x")
    assert pd["platform"] == "googlebusiness" and pd["accountId"] == "acc1"
    psd = pd["platformSpecificData"]
    assert psd["topicType"] == "STANDARD"
    assert psd["callToAction"]["type"] == "LEARN_MORE"
    assert "utm_campaign=echo_local_update" in psd["callToAction"]["url"]
    assert psd["locationId"] == "locations/1"


def test_call_cta_has_no_url():
    pd = gbp.build_platform_data(account_id="a", topic_type="STANDARD",
                                 location_id="locations/1", pillar="p",
                                 cta_type="CALL")
    assert pd["platformSpecificData"]["callToAction"] == {"type": "CALL"}


def test_offer_omits_call_to_action_and_utms_redeem_url():
    pd = gbp.build_platform_data(
        account_id="a", topic_type="OFFER", location_id="locations/1",
        pillar="New Member Offer",
        offer={"couponCode": "ENG12", "redeemOnlineUrl": "https://gym.com/join",
               "termsConditions": "New members only."},
        event={"schedule": {"startDate": "2026-09-01", "endDate": "2026-09-10"}})
    psd = pd["platformSpecificData"]
    assert psd["topicType"] == "OFFER"
    assert "callToAction" not in psd, "OFFER must NEVER carry a callToAction"
    assert "utm_campaign=echo_new_member_offer" in psd["offer"]["redeemOnlineUrl"]
    assert psd["event"]["schedule"]["endDate"] == "2026-09-10"   # offer window


def test_event_requires_schedule():
    with pytest.raises(gbp.GbpPayloadError):
        gbp.build_platform_data(account_id="a", topic_type="EVENT",
                                location_id="locations/1", pillar="p",
                                cta_url="https://gym.com/x")


def test_non_call_cta_requires_url():
    with pytest.raises(gbp.GbpPayloadError):
        gbp.build_platform_data(account_id="a", topic_type="STANDARD",
                                location_id="locations/1", pillar="p",
                                cta_type="BOOK", cta_url="")


def test_bad_topic_type_raises():
    with pytest.raises(gbp.GbpPayloadError):
        gbp.build_platform_data(account_id="a", topic_type="REEL",
                                location_id="locations/1", pillar="p")


def test_full_post_payload_shape():
    pd = gbp.build_platform_data(account_id="a", topic_type="STANDARD",
                                 location_id="locations/1", pillar="p",
                                 cta_url="https://gym.com/x")
    body = gbp.build_post_payload(caption=_good(), image_url="https://r2/x.jpg",
                                  platform_data=pd)
    assert body["content"] == _good()
    assert body["mediaItems"] == [{"type": "image", "url": "https://r2/x.jpg"}]
    assert body["platforms"] == [pd]


def test_post_payload_requires_image():
    pd = gbp.build_platform_data(account_id="a", topic_type="STANDARD",
                                 location_id="locations/1", pillar="p",
                                 cta_url="https://gym.com/x")
    with pytest.raises(gbp.GbpPayloadError):
        gbp.build_post_payload(caption=_good(), image_url="", platform_data=pd)


# ---- image crop (§5.3) -----------------------------------------------------

def test_crop_to_1200x900(tmp_path):
    from PIL import Image
    src = tmp_path / "wide.jpg"
    Image.new("RGB", (1920, 1080), (60, 80, 100)).save(src)
    out = gbp.crop_4x3(str(src), str(tmp_path / "out.jpg"))
    assert Image.open(out).size == (1200, 900)


def test_crop_rejects_too_small(tmp_path):
    from PIL import Image
    src = tmp_path / "tiny.jpg"
    Image.new("RGB", (300, 200), (0, 0, 0)).save(src)
    with pytest.raises(gbp.GbpPayloadError):
        gbp.crop_4x3(str(src), str(tmp_path / "out.jpg"))
