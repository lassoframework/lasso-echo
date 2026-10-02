"""
Hard scope guard for lasso_astra_rework.py (agent/lasso_astra_rework.py).

Born from the CrossFit Chateau scope violation: a client gym got swept because
scoping was left to the caller remembering to pass the right gym_id. This
guard makes that class of mistake structurally impossible for THIS mechanism:
_enforce_lasso_only_scope lives INSIDE run() and rework_row() themselves, so
any call against a non-allowlisted (client) account raises ScopeViolation,
logs clearly, and reads/writes nothing — no store.list_month, no Astra call,
no store.create_variant_candidate.

Mutation-tested: with the guard call removed from run()/rework_row(), these
tests fail (verified manually during development); restoring the guard makes
them pass again.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import lasso_astra_rework as lar  # noqa: E402


class _StoreThatMustNeverBeCalled:
    """Any read or write on this store fails the test immediately — proves the
    guard fires BEFORE any interaction with the calendar store."""

    def list_month(self, gym_id, month):
        pytest.fail("list_month must never be called for a non-allowlisted gym_id")

    def list_variant_candidates(self, gym_id, month):
        pytest.fail(
            "list_variant_candidates must never be called for a non-allowlisted gym_id"
        )

    def create_variant_candidate(self, *a, **k):
        pytest.fail(
            "create_variant_candidate must never be called for a non-allowlisted gym_id"
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "true")


@pytest.mark.parametrize("client_gym", [
    "crossfitchateau813e78", "district_h", "crossfitnewtown", "some_other_client",
])
def test_run_refuses_every_non_lasso_gym_write_mode(client_gym):
    store = _StoreThatMustNeverBeCalled()
    logged = []
    with pytest.raises(lar.ScopeViolation):
        lar.run(store, gym_id=client_gym, months=["2026-09"], write=True,
               log=logged.append)
    assert any(client_gym in m for m in logged)
    assert any("SCOPE VIOLATION" in m for m in logged)


def test_run_refuses_a_client_gym_even_in_dry_run():
    """The guard fires before write=False's dry-run branch too — a dry run must
    not even read a client gym's calendar."""
    store = _StoreThatMustNeverBeCalled()
    with pytest.raises(lar.ScopeViolation):
        lar.run(store, gym_id="crossfitchateau813e78", months=["2026-09"],
               write=False)


def test_run_refuses_before_checking_the_feature_flag(monkeypatch):
    """Scope is enforced even if ECHO_VARIANT_PAIRING is off — the guard is not
    downstream of any other gate."""
    monkeypatch.setenv("ECHO_VARIANT_PAIRING", "false")
    store = _StoreThatMustNeverBeCalled()
    with pytest.raises(lar.ScopeViolation):
        lar.run(store, gym_id="crossfitchateau813e78", months=["2026-09"],
               write=True)


def test_rework_row_itself_refuses_a_client_gym_id():
    """Defense in depth: even if a future caller reaches rework_row directly,
    bypassing run() entirely, the guard still fires."""
    store = _StoreThatMustNeverBeCalled()
    row = {"id": "x", "gym_id": "district_h", "caption": "hi"}
    with pytest.raises(lar.ScopeViolation):
        lar.rework_row(store, row, gym_id="district_h")


def test_lasso_itself_is_unaffected_by_the_guard():
    """The guard must never block the one legitimate account."""
    class _Store:
        def list_month(self, gym_id, month):
            return []
    result = lar.run(_Store(), gym_id="lasso", months=["2026-09"], write=False)
    assert result["ok"] is True


def test_allowlist_is_exactly_lasso():
    """Locks the allowlist contents down explicitly so a future edit that
    accidentally widens it (e.g. adds a client gym "temporarily") is caught by
    this test, not discovered in production."""
    assert lar.LASSO_ONLY_ALLOWLIST == frozenset({"lasso"})
