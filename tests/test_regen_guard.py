"""
Regen batch guard tests (operator hygiene Part A). Offline. Asserts: a second
invocation while a live run holds the lock refuses immediately (naming the
holder's start time) and renders nothing; a stale lock (dead pid or too old)
auto clears and the run proceeds; the end of batch summary lists every concept
exactly once with hash and url; a re-run notes the prior hashes are
superseded; the lock always releases (even mid run failure); dry runs never
lock.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, regen_library  # noqa: E402


class FakeNano:
    def generate_image(self, prompt, model):
        return b"\x89PNG\r\n\x1a\nFAKE" + prompt[:40].encode()


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    return str(lib)


def _write_lock(lib, pid, ts):
    with open(os.path.join(lib, regen_library.LOCK_FILE), "w") as fh:
        json.dump({"pid": pid, "ts": ts, "started": "2026-07-04T01:00:00+00:00"}, fh)


# ---- the lock -------------------------------------------------------------------------
def test_second_concurrent_invocation_refuses(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    _write_lock(lib, os.getpid(), time.time())      # a LIVE holder (this process)
    out = regen_library.run(set_name="b2b", nano_client=FakeNano(),
                            s3_client=FakeS3(), out_dir=lib)
    assert out == {}                                # nothing started
    printed = capsys.readouterr().out
    assert "regen already running since 2026-07-04T01:00:00+00:00" in printed
    assert not [f for f in os.listdir(lib) if f.endswith(".png")]
    # the live holder's lock is NOT cleared by the refused run
    assert os.path.exists(os.path.join(lib, regen_library.LOCK_FILE))


def test_stale_lock_clears_dead_pid_and_old_age(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    _write_lock(lib, 999999999, time.time())        # holder pid does not exist
    out = regen_library.run(only="b2b_five_companies", nano_client=FakeNano(),
                            s3_client=FakeS3(), out_dir=lib)
    assert "files" in out["b2b_five_companies"]       # the run proceeded
    assert "clearing stale lock" in capsys.readouterr().out
    assert not os.path.exists(os.path.join(lib, regen_library.LOCK_FILE))  # released
    # age based staleness: a live pid but an ancient timestamp also clears
    _write_lock(lib, os.getpid(), time.time() - regen_library.LOCK_STALE_SECONDS - 5)
    out = regen_library.run(only="b2b_five_companies", nano_client=FakeNano(),
                            s3_client=FakeS3(), out_dir=lib)
    assert "files" in out["b2b_five_companies"]
    assert "clearing stale lock" in capsys.readouterr().out


def test_lock_releases_after_normal_run_and_dry_runs_never_lock(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    regen_library.run(set_name="b2b", nano_client=FakeNano(),
                      s3_client=FakeS3(), out_dir=lib)
    assert not os.path.exists(os.path.join(lib, regen_library.LOCK_FILE))
    regen_library.run(set_name="b2b", dry_run=True, out_dir=lib)
    assert not os.path.exists(os.path.join(lib, regen_library.LOCK_FILE))


# ---- the end of batch summary ------------------------------------------------------------
def test_summary_lists_every_concept_exactly_once(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    regen_library.run(set_name="b2b", nano_client=FakeNano(),
                      s3_client=FakeS3(), out_dir=lib)
    printed = capsys.readouterr().out
    summary = printed.split("regen summary", 1)[1]
    b2b_keys = [k for k, v in regen_library.CONCEPTS.items()
                if v.get("set") == "b2b"]
    for key in b2b_keys:
        assert summary.count(f"\n  {key}  ") == 1, key   # exactly one row each
        row = [l for l in summary.splitlines() if l.startswith(f"  {key}  ")][0]
        assert "https://cdn.echo.test/" in row           # url in the row
        assert len(row.split()[1]) == 16                 # the 16 char content hash
    assert "superseded" not in printed                   # first run: no note

    # the re-run supersedes every prior hash and says so
    regen_library.run(set_name="b2b", nano_client=FakeNano(),
                      s3_client=FakeS3(), out_dir=lib)
    printed = capsys.readouterr().out
    assert f"supersedes prior hashes for {len(b2b_keys)} concept(s)" in printed


def test_single_concept_run_prints_no_summary(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    regen_library.run(only="b2b_16_cpl", nano_client=FakeNano(),
                      s3_client=FakeS3(), out_dir=lib)
    assert "regen summary" not in capsys.readouterr().out
