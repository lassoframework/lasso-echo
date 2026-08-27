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

# CREDENTIAL QUARANTINE (2026-08-27, the gritx storm post-mortem): a suite run
# INSIDE the production container executed tests against LIVE creds — flags were
# armed (fixed by the AGENT_*/ECHO_* sweep below) but raw credentials are not
# AGENT_-prefixed, so handle_cadence's dual-write reached the REAL Supabase
# (gritx posts_per_day flipped to 2 by a test actor) and client-month tests
# fired REAL Slack alerts through the live token. Tests are OFFLINE by
# convention: every known credential/prefix is stripped so no test can ever
# reach a production plane, no matter where the suite runs.
_CRED_PREFIXES = (
    "AGENT_", "ECHO_", "SUPABASE_", "SLACK_", "ANTHROPIC_", "OPENAI_",
    "GEMINI_", "GOOGLE_API", "ZERNIO_", "LATE_", "R2_", "AWS_", "STRIPE_",
    "META_", "OPUS_", "CLOUDFLARE_", "HIGGSFIELD_",
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(_CRED_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo_test.db"))
    # DATA-VOLUME QUARANTINE: in the production container /data EXISTS and holds
    # REAL gym media/state, so every default-/data path (content_library, brain,
    # clipper cache) resolves to live data and tests that expect an empty world
    # fail (14 media-sync failures in the 2026-08-27 container run) or, worse,
    # could write beside real files. Point the data root at an EMPTY per-test dir
    # (deliberately not created — matching a dev machine with no volume).
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "data"))
