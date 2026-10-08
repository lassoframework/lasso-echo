from types import SimpleNamespace
import pytest
from agent.accounts import get_account
from agent.real_month_run import _canonical_owned_source_platforms


@pytest.mark.parametrize("key,source_only,raw,expected", [
    ("lasso_fb", True, "facebook_page", "facebook"),
    ("lasso_fb", True, "facebook", "facebook"),
    ("lasso_fb", False, "facebook_page", "facebook_page"),
    ("client_fb", True, "facebook_page", "facebook_page"),
    ("lasso_fb", True, "fb", "fb"),
    ("lasso_ig", True, "instagram", "instagram"),
])
def test_verified_owned_source_boundary(key, source_only, raw, expected):
    account = get_account("lasso_fb")
    before = account.platform
    fragments = ["Approved source excerpt"]
    draft = SimpleNamespace(platform=raw, source_fragments=fragments)
    result = _canonical_owned_source_platforms([draft], key, account, source_only)
    assert result[0].platform == expected
    assert result[0].source_fragments is fragments
    assert account.platform == before


def test_wrong_source_account_is_not_coerced():
    account = SimpleNamespace(key="client_fb", platform="facebook_page")
    draft = SimpleNamespace(platform="facebook_page")
    assert _canonical_owned_source_platforms([draft], "lasso_fb", account, True)[0].platform == "facebook_page"
