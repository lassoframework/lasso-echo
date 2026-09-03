"""A nested provider error must survive to the row, the alert and the logs.

2026-09-03: crossfitnine7f7dadc's GBP photo drop failed and every surface said the same
useless thing, "photo upload: ZernioError". Two independent truncations caused it:

  1. gbp_worker recorded only type(e).__name__ and discarded the message entirely.
  2. the Zernio client capped error bodies at 200 chars -- SHORTER than the boilerplate of
     a nested error. Zernio wraps Google Business Profile's own JSON, so the first ~200
     characters are the two "error" envelopes and Google's generic
     "Request contains an invalid argument", and the details array naming the real reason
     was cut off. Production truncated mid-token at '\\"d' of '\\"details\\"'.

Nothing else logged the exception, so the cause was unrecoverable after the fact.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gbp_worker  # noqa: E402
from agent.zernio import ZernioError, _ERR_BODY_CHARS  # noqa: E402


# The real body shape observed in production, with a details array past the old cut.
_GOOGLE_BODY = (
    '{"error":"Invalid request to Google Business Profile: {\n  "error": {\n'
    '    "code": 400,\n    "message": "Request contains an invalid argument.",\n'
    '    "status": "INVALID_ARGUMENT",\n    "details": '
    '[{"reason":"PHOTO_URL_INACCESSIBLE","field":"sourceUrl"}]}}"}'
)


class _Boom:
    def __init__(self, detail):
        self.detail = detail

    def create_gmb_media(self, *a, **k):
        raise ZernioError(400, self.detail)


def _row():
    return {"id": "r1", "gym_id": "crossfitnine7f7dadc",
            "image_url": "https://pub-x.r2.dev/echo/a/b/c.jpg"}


def test_the_old_cap_was_shorter_than_the_boilerplate():
    """Pins WHY 200 was the wrong number: the reason sits past it."""
    assert "PHOTO_URL_INACCESSIBLE" not in _GOOGLE_BODY[:200]
    assert "PHOTO_URL_INACCESSIBLE" in _GOOGLE_BODY[:_ERR_BODY_CHARS]


def test_reject_reason_carries_googles_actual_reason():
    seen = []
    res = gbp_worker.publish_photo_drop(
        _row(), {"zernio_account_id": "a1"},
        client=_Boom(_GOOGLE_BODY[:_ERR_BODY_CHARS]), draft=False, alert=seen.append)
    assert res["status"] == "failed"
    assert res["ok"] is False
    assert "PHOTO_URL_INACCESSIBLE" in res["reject_reason"], \
        "the row must say WHY, not just name the exception class"


def test_the_alert_carries_googles_actual_reason():
    seen = []
    gbp_worker.publish_photo_drop(
        _row(), {"zernio_account_id": "a1"},
        client=_Boom(_GOOGLE_BODY[:_ERR_BODY_CHARS]), draft=False, alert=seen.append)
    assert len(seen) == 1
    assert "PHOTO_URL_INACCESSIBLE" in seen[0]
    assert "crossfitnine7f7dadc" in seen[0] and "r1" in seen[0]


def test_a_credential_quoted_back_by_the_provider_is_scrubbed():
    """The reason detail is now long enough to carry a secret the provider echoed, so
    the scrub() pass is what makes surfacing it safe.

    Use the env-var scrub path: ops_alerts.scrub() redacts the VALUE of any env var
    whose name contains TOKEN/SECRET/KEY. Inject a clearly synthetic value for the
    duration of this test so no real-looking credential pattern appears in the file.
    """
    import os
    _env_key = "ECHO_TEST_SCRUB_TOKEN"
    _env_val = "testcredvalue9876543210abcde"
    leaky = (f'{{"error":"rejected {_env_val} for account",'
             '"details":[{"reason":"PHOTO_URL_INACCESSIBLE"}]}')
    os.environ[_env_key] = _env_val
    try:
        seen = []
        res = gbp_worker.publish_photo_drop(
            _row(), {"zernio_account_id": "a1"},
            client=_Boom(leaky), draft=False, alert=seen.append)
    finally:
        del os.environ[_env_key]
    assert _env_val not in res["reject_reason"]
    assert _env_val not in seen[0]
    assert "REDACTED" in res["reject_reason"]
    # still diagnosable after scrubbing
    assert "PHOTO_URL_INACCESSIBLE" in res["reject_reason"]


def test_reject_reason_stays_bounded():
    """Diagnosable, not unbounded: the column must not take an arbitrary body."""
    res = gbp_worker.publish_photo_drop(
        _row(), {"zernio_account_id": "a1"},
        client=_Boom("x" * 5000), draft=False, alert=lambda m: None)
    assert len(res["reject_reason"]) <= 400


def test_a_missing_image_is_still_reported_without_calling_zernio():
    class _NeverCalled:
        def create_gmb_media(self, *a, **k):
            raise AssertionError("must not upload without an image")

    res = gbp_worker.publish_photo_drop(
        {"id": "r2", "gym_id": "g", "image_url": ""}, {"zernio_account_id": "a1"},
        client=_NeverCalled(), draft=False, alert=lambda m: None)
    assert res["status"] == "failed"
    assert res["reject_reason"] == "photo drop has no image"


def test_draft_mode_never_uploads():
    class _NeverCalled:
        def create_gmb_media(self, *a, **k):
            raise AssertionError("draft mode must not touch Google")

    res = gbp_worker.publish_photo_drop(
        _row(), {"zernio_account_id": "a1"}, client=_NeverCalled(), draft=True)
    assert res["ok"] is True and res["mode"] == "draft"
