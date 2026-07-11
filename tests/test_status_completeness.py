"""
`status` must show every capability flag that exists.

AGENT_CATEGORY_ROTATION was invisible in status for weeks; the audit then
found 11 more flags in the same state. Rather than pin a list that rots,
this test derives the flag set from config.py itself: every *_enabled()
function that reads an AGENT_* env var must have that env var named in the
status output. A new flag added to config without a status line fails here.
"""

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config
import agent.__main__ as mm


def test_status_covers_every_config_flag(capsys):
    mm._status()
    out = capsys.readouterr().out

    missing = []
    for name, fn in inspect.getmembers(config, inspect.isfunction):
        if not name.endswith("_enabled"):
            continue
        src = inspect.getsource(fn)
        envs = re.findall(r'"(AGENT_[A-Z0-9_]+)"', src)
        if not envs:
            continue
        if not any(e in out for e in envs):
            missing.append(f"{name} ({', '.join(envs)})")

    assert not missing, (
        "status omits these flags — add a line to _status() for each: "
        + "; ".join(missing))


def test_status_shows_source_paths(capsys):
    mm._status()
    out = capsys.readouterr().out
    for env in ("AGENT_SOURCE_DOC_PATH", "AGENT_KNOWLEDGE_DIR",
                "AGENT_BOOK_DIR", "AGENT_SLACK_CHANNEL_ID"):
        assert env in out, f"status must show {env}"
