"""
gbp-check probe tests: each state renders the right honest line (READY, quota
zero pending the Google case, auth failure, wrong project, missing config).
Read only; the token never appears in output.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, gbp_check  # noqa: E402

TOKEN = "gbp-token-secret-1"


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Http:
    def __init__(self, resp):
        self.resp = resp

    def get(self, url, headers=None, timeout=None):
        return self.resp


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_GBP_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(config, "GBP_ACCOUNT_ID", "A1")
    monkeypatch.setattr(config, "GBP_LOCATION_ID", "L1")


def test_ready(monkeypatch, capsys):
    _arm(monkeypatch)
    out = gbp_check.gbp_check(http=_Http(_Resp(200, {"name": "loc"})))
    assert out["ready"] is True
    printed = capsys.readouterr().out
    assert printed.startswith("gbp-check: READY")
    assert TOKEN not in printed


def test_quota_zero_names_the_case(monkeypatch, capsys):
    _arm(monkeypatch)
    out = gbp_check.gbp_check(http=_Http(
        _Resp(429, {"error": {"status": "RESOURCE_EXHAUSTED"}})))
    assert out["ready"] is False
    printed = capsys.readouterr().out
    assert "NOT READY" in printed and "3-8465000040674" in printed


def test_auth_failure(monkeypatch, capsys):
    _arm(monkeypatch)
    out = gbp_check.gbp_check(http=_Http(_Resp(401, {"error": "unauth"})))
    assert out["ready"] is False
    assert "auth failure" in capsys.readouterr().out


def test_wrong_project(monkeypatch, capsys):
    _arm(monkeypatch)
    out = gbp_check.gbp_check(http=_Http(_Resp(
        403, {"error": {"status": "PERMISSION_DENIED",
                        "message": "API not enabled on project 12345"}})))
    assert out["ready"] is False
    assert "wrong project" in capsys.readouterr().out


def test_missing_token_and_config(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_GBP_ACCESS_TOKEN", raising=False)
    out = gbp_check.gbp_check(http=_Http(_Resp(200, {})))
    assert out["ready"] is False
    assert "AGENT_GBP_ACCESS_TOKEN" in capsys.readouterr().out
    monkeypatch.setenv("AGENT_GBP_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(config, "GBP_ACCOUNT_ID", "")
    out2 = gbp_check.gbp_check(http=_Http(_Resp(200, {})))
    assert out2["ready"] is False
    assert "AGENT_GBP_ACCOUNT_ID" in capsys.readouterr().out
