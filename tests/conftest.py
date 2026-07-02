"""Shared test isolation: every test gets its OWN sqlite db file, so the /data
store never leaks state across tests (and never touches a real /data)."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo_test.db"))
