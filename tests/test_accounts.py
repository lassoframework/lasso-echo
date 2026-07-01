"""
Account registry: blake_personal is an INACTIVE record (Meta ended personal-profile
publishing in 2018). It must not be drafted for, but stays discoverable for history.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import accounts  # noqa: E402


def test_active_accounts_excludes_blake_personal():
    keys = [a.key for a in accounts.active_accounts()]
    assert keys == ["lasso_ig", "lasso_fb"]
    assert "blake_personal" not in keys


def test_blake_personal_kept_as_inactive_record():
    a = accounts.get_account("blake_personal")     # still discoverable (history kept)
    assert a is not None
    assert a.active is False


def test_lasso_accounts_untouched_and_active():
    for key in ("lasso_ig", "lasso_fb"):
        a = accounts.get_account(key)
        assert a is not None and a.active is True
