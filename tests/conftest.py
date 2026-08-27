"""Shared test isolation: every test gets its OWN sqlite db file, so the /data
store never leaks state across tests (and never touches a real /data).

ENV ISOLATION (2026-08-27, Blake's done-criterion "suite must pass in the Railway
container"): the production container carries ARMED feature flags (AGENT_SB7_ENABLED,
AGENT_OPS_ALERTS, AGENT_CLIENT_MEDIA_SYNC, ...). Tests are written against DEFAULTS
(every flag off) and arm flags explicitly via monkeypatch.setenv — so ambient
AGENT_*/ECHO_* env must be stripped or the suite only passes on machines whose env
happens to be empty. Stripping also removes the live-Slack hazard: an armed
AGENT_OPS_ALERTS plus a real token in the container env could otherwise let a test
fire a REAL ops alert. AGENT_DB_PATH is re-set to the per-test tmp db after the sweep.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("AGENT_") or key.startswith("ECHO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo_test.db"))
