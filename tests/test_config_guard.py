"""
Track 4: config-check CLI + ENV.md guard tests.

Read-only and informational. Zero behavior changes to the agent.
"""
import pathlib
import re
import subprocess
import sys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_AGENT_DIR = _REPO_ROOT / "agent"
_ENV_MD = _REPO_ROOT / "docs" / "ENV.md"


def _env_md_text() -> str:
    return _ENV_MD.read_text(errors="replace")


def _env_md_vars() -> set:
    """Return the set of ALL_CAPS identifiers mentioned in docs/ENV.md."""
    text = _env_md_text()
    return set(re.findall(r'\b([A-Z][A-Z0-9_]{3,})\b', text))


# ---------------------------------------------------------------------------
# test_config_check_exits_zero
# ---------------------------------------------------------------------------

def test_config_check_exits_zero():
    """config-check must complete without error and exit with code 0."""
    result = subprocess.run(
        [sys.executable, "-m", "agent", "config-check"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"config-check exited {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "=== config-check ===" in result.stdout
    assert "=== done ===" in result.stdout


# ---------------------------------------------------------------------------
# test_env_md_has_key_vars
# ---------------------------------------------------------------------------

def test_env_md_has_key_vars():
    """docs/ENV.md must document the core gate variables."""
    required = {
        "AGENT_ENABLED",
        "AGENT_PUBLISH_ENABLED",
        "AGENT_INTAKE_ENABLED",
        "AGENT_SLACK_BOT_TOKEN",
    }
    documented = _env_md_vars()
    missing = required - documented
    assert not missing, (
        f"These required vars are absent from docs/ENV.md: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# test_code_scan_finds_vars
# ---------------------------------------------------------------------------

def test_code_scan_finds_vars():
    """Scanning agent/config.py for os.environ.get patterns must find at least 15 vars."""
    config_py = _AGENT_DIR / "config.py"
    text = config_py.read_text(errors="replace")
    pattern = re.compile(r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]+)["\']')
    found = set(pattern.findall(text))
    assert len(found) >= 15, (
        f"Expected at least 15 env vars in agent/config.py, found {len(found)}: {sorted(found)}"
    )
