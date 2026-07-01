"""
Google Business Profile publisher tests. Same gates as Meta: draft-only makes no
network call, flag-off makes no call, and a real write needs BOTH publish + GBP flags.
No network — a fake HTTP client only; the token is never logged.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, gbp_publisher  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft  # noqa: E402


def _acct():
    return Account(key="lasso_gbp", display_name="LASSO GBP",
                   platform=Platform.GOOGLE_BUSINESS,
                   token_env="AGENT_GBP_ACCESS_TOKEN", target_id_env="X")


def _draft(caption="Local search wins for gyms.", url="https://cdn.echo.test/a.jpg"):
    return Draft(draft_id="d", account_key="lasso_gbp", platform=Platform.GOOGLE_BUSINESS,
                 caption=caption, hashtags=[], creative_path="/a.jpg",
                 creative_public_url=url, scheduled_for="t",
                 cta_type="LEARN_MORE", cta_url="https://lasso.test/book")


class ExplodingHTTP:
    def post(self, *a, **k):
        raise AssertionError("Network call while draft-only / flag-off!")


class CaptureHTTP:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None, **k):
        self.posts.append({"url": url, "json": json, "headers": headers})

        class R:
            status_code = 200
            def json(self_inner):
                return {"name": "accounts/1/locations/2/localPosts/99"}
        return R()


# ---- 1. draft-only (GBP armed, publish OFF) -> no call ----------------------
def test_draft_only_no_call(monkeypatch):
    monkeypatch.setenv("AGENT_GBP_ENABLED", "true")
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)  # publish OFF
    res = gbp_publisher.publish(_draft(), _acct(), http=ExplodingHTTP())
    assert res.ok and res.mode == "would_publish"


# ---- 2. flag-off (publish armed, GBP OFF) -> no call ------------------------
def test_gbp_flag_off_no_call(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.delenv("AGENT_GBP_ENABLED", raising=False)      # GBP OFF
    res = gbp_publisher.publish(_draft(), _acct(), http=ExplodingHTTP())
    assert res.mode == "would_publish"


# ---- 3. post shape ----------------------------------------------------------
def test_post_shape():
    body = gbp_publisher.build_local_post("hello gyms", image_url="https://x/a.jpg",
                                          cta_type="BOOK", cta_url="https://lasso.test/book")
    assert body["summary"] == "hello gyms"
    assert body["topicType"] == "STANDARD"
    assert body["callToAction"] == {"actionType": "BOOK", "url": "https://lasso.test/book"}
    assert body["media"] == [{"mediaFormat": "PHOTO", "sourceUrl": "https://x/a.jpg"}]


# ---- 4. summary trimmed to 1500 ---------------------------------------------
def test_summary_trimmed_to_1500():
    body = gbp_publisher.build_local_post("x" * 2000, image_url="https://x/a.jpg")
    assert len(body["summary"]) == config.GBP_SUMMARY_LIMIT == 1500


# ---- 5. invalid CTA dropped -------------------------------------------------
def test_invalid_cta_dropped():
    body = gbp_publisher.build_local_post("hi", image_url="", cta_type="BOGUS",
                                          cta_url="https://lasso.test/x")
    assert "callToAction" not in body        # bad type -> no button, post still valid


# ---- 6. armed: posts exactly once with Bearer auth --------------------------
def test_armed_posts_once_with_bearer(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_GBP_ENABLED", "true")
    monkeypatch.setenv("AGENT_GBP_ACCESS_TOKEN", "secret-token")
    monkeypatch.setattr(config, "GBP_ACCOUNT_ID", "111")
    monkeypatch.setattr(config, "GBP_LOCATION_ID", "222")
    http = CaptureHTTP()
    res = gbp_publisher.publish(_draft(), _acct(), http=http)
    assert res.mode == "published"
    assert res.post_id.endswith("/99")
    assert len(http.posts) == 1              # exactly once
    sent = http.posts[0]
    assert sent["url"].endswith("/accounts/111/locations/222/localPosts")
    assert sent["headers"]["Authorization"] == "Bearer secret-token"
    assert sent["json"]["summary"] == "Local search wins for gyms."
