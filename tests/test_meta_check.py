"""
Tests for agent/meta_check.py.

All tests use an injectable http mock. No live network calls are made.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Ensure the repo root is importable when running from the worktree directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.accounts import Account, Platform
from agent.meta_check import check_account, check_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_account(key="test_ig", platform=Platform.INSTAGRAM,
                  token_env="TEST_TOKEN", target_id_env="TEST_TARGET_ID"):
    return Account(
        key=key,
        display_name="Test Account",
        platform=platform,
        token_env=token_env,
        target_id_env=target_id_env,
    )


def _json_response(body, status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    return r


def _make_http(debug_body=None, target_body=None, biz_body=None,
               publishable_body=None, debug_status=200,
               target_status=200, biz_status=200, pub_status=200):
    """Build a mock requests-like object where get() routes on the URL."""
    def _get(url, params=None, timeout=30):
        params = params or {}
        if url.endswith("/debug_token"):
            return _json_response(debug_body or {}, debug_status)
        # Route on which fields= the call is asking for.
        # Order matters: check the most specific patterns first to avoid
        # substring collisions (e.g. "is_business_account" contains "business").
        fields = params.get("fields", "")
        field_set = set(f.strip() for f in fields.split(","))
        if field_set == {"id", "name"}:
            return _json_response(target_body or {}, target_status)
        if "is_business_account" in field_set or "can_post" in field_set:
            return _json_response(publishable_body or {}, pub_status)
        if "business" in field_set:
            return _json_response(biz_body or {}, biz_status)
        return _json_response({}, 200)

    http = MagicMock()
    http.get.side_effect = _get
    return http


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReadyWhenAllChecksPass(unittest.TestCase):
    """All checks pass for a well-configured IG account."""

    def setUp(self):
        os.environ["TEST_TOKEN"] = "fake_token_value"
        os.environ["TEST_TARGET_ID"] = "12345"

    def tearDown(self):
        os.environ.pop("TEST_TOKEN", None)
        os.environ.pop("TEST_TARGET_ID", None)

    def test_ready_when_all_checks_pass(self):
        http = _make_http(
            debug_body={"data": {
                "is_valid": True,
                "scopes": ["instagram_basic", "pages_read_engagement",
                           "instagram_content_publish"],
            }},
            target_body={"id": "12345", "name": "Test Page"},
            biz_body={"id": "12345", "business": {"id": "99"}},
            publishable_body={"id": "12345", "is_business_account": True,
                              "username": "testgym"},
        )
        account = _make_account()
        result = check_account(account, http=http)

        self.assertTrue(result["ready"])
        self.assertEqual(result["missing"], [])
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertEqual(by_name["token_set"]["status"], "pass")
        self.assertEqual(by_name["token_valid"]["status"], "pass")
        self.assertEqual(by_name["scopes"]["status"], "pass")
        self.assertEqual(by_name["target_reachable"]["status"], "pass")
        self.assertEqual(by_name["publishable"]["status"], "pass")


class TestNotReadyWhenTokenMissing(unittest.TestCase):
    """Token env unset: first check fails, all remaining are skipped."""

    def test_not_ready_when_token_missing(self):
        # Make sure the env var is absent.
        os.environ.pop("TEST_TOKEN", None)
        os.environ.pop("TEST_TARGET_ID", None)

        account = _make_account()
        result = check_account(account, http=MagicMock())

        self.assertFalse(result["ready"])
        self.assertIn("token_set", result["missing"])
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertEqual(by_name["token_set"]["status"], "fail")
        # All network-dependent checks must be skipped, never fail.
        for name in ("token_valid", "scopes", "target_reachable",
                     "business_connected", "publishable"):
            self.assertEqual(by_name[name]["status"], "skip",
                             f"{name} should be skip, got {by_name[name]['status']}")


class TestNotReadyWhenTokenInvalid(unittest.TestCase):
    """debug_token returns is_valid=False."""

    def setUp(self):
        os.environ["TEST_TOKEN"] = "expired_token"
        os.environ["TEST_TARGET_ID"] = "12345"

    def tearDown(self):
        os.environ.pop("TEST_TOKEN", None)
        os.environ.pop("TEST_TARGET_ID", None)

    def test_not_ready_when_token_invalid(self):
        http = _make_http(
            debug_body={"data": {
                "is_valid": False,
                "error": {"message": "Token has expired"},
            }},
        )
        account = _make_account()
        result = check_account(account, http=http)

        self.assertFalse(result["ready"])
        self.assertIn("token_valid", result["missing"])
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertEqual(by_name["token_valid"]["status"], "fail")
        self.assertIn("expired", by_name["token_valid"]["detail"].lower())


class TestTargetUnreachableFail(unittest.TestCase):
    """target_id endpoint returns 400/403."""

    def setUp(self):
        os.environ["TEST_TOKEN"] = "valid_token"
        os.environ["TEST_TARGET_ID"] = "99999"

    def tearDown(self):
        os.environ.pop("TEST_TOKEN", None)
        os.environ.pop("TEST_TARGET_ID", None)

    def test_target_unreachable_fail(self):
        http = _make_http(
            debug_body={"data": {
                "is_valid": True,
                "scopes": ["instagram_basic", "pages_read_engagement"],
            }},
            target_body={"error": {"message": "Unsupported get request"}},
            target_status=400,
        )
        account = _make_account()
        result = check_account(account, http=http)

        self.assertFalse(result["ready"])
        self.assertIn("target_reachable", result["missing"])
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertEqual(by_name["target_reachable"]["status"], "fail")


class TestMissingScopeWarn(unittest.TestCase):
    """debug_token returns scopes but they are insufficient; result is warn not fail."""

    def setUp(self):
        os.environ["TEST_TOKEN"] = "valid_but_thin_token"
        os.environ["TEST_TARGET_ID"] = "12345"

    def tearDown(self):
        os.environ.pop("TEST_TOKEN", None)
        os.environ.pop("TEST_TARGET_ID", None)

    def test_missing_scope_warn(self):
        # Scopes absent entirely (self-validation path, may omit them).
        http = _make_http(
            debug_body={"data": {
                "is_valid": True,
                # No "scopes" key at all.
            }},
            target_body={"id": "12345", "name": "Test Page"},
            biz_body={"id": "12345"},
            publishable_body={"id": "12345", "is_business_account": True,
                              "username": "testgym"},
        )
        account = _make_account()
        result = check_account(account, http=http)

        by_name = {c["name"]: c for c in result["checks"]}
        # Absent scopes list = warn, not fail.
        self.assertEqual(by_name["scopes"]["status"], "warn")
        # warn does not block ready (only fail does).
        self.assertNotIn("scopes", result["missing"])


class TestCheckAllRunsAllAccounts(unittest.TestCase):
    """check_all returns one result per account."""

    def setUp(self):
        os.environ["TEST_TOKEN_A"] = "tok_a"
        os.environ["TEST_TARGET_A"] = "11111"
        os.environ["TEST_TOKEN_B"] = "tok_b"
        os.environ["TEST_TARGET_B"] = "22222"

    def tearDown(self):
        for k in ("TEST_TOKEN_A", "TEST_TARGET_A", "TEST_TOKEN_B", "TEST_TARGET_B"):
            os.environ.pop(k, None)

    def test_check_all_returns_one_result_per_account(self):
        acct_a = _make_account(key="acct_a", token_env="TEST_TOKEN_A",
                               target_id_env="TEST_TARGET_A")
        acct_b = _make_account(key="acct_b", token_env="TEST_TOKEN_B",
                               target_id_env="TEST_TARGET_B",
                               platform=Platform.FACEBOOK_PAGE)

        http = _make_http(
            debug_body={"data": {"is_valid": True,
                                 "scopes": ["instagram_basic",
                                            "pages_read_engagement",
                                            "pages_manage_posts"]}},
            target_body={"id": "11111", "name": "Gym A"},
            biz_body={"id": "11111", "business": {"id": "1"}},
            publishable_body={"id": "11111", "is_business_account": True,
                              "can_post": True},
        )

        results = check_all(http=http, accounts=[acct_a, acct_b])
        self.assertEqual(len(results), 2)
        keys = {r["account"] for r in results}
        self.assertIn("acct_a", keys)
        self.assertIn("acct_b", keys)


if __name__ == "__main__":
    unittest.main()
