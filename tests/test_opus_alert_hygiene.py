"""
Opus alert hygiene tests (micro patch). Fully OFFLINE. Asserts:
  - a placeholder AGENT_OPUS_PROJECT_IDS value (P<digits> pattern or under 6
    chars) gets ONE warning naming it and NEVER reaches the API (adversarial
    API that fails the test on any placeholder call); real-looking ids pass
    through untouched;
  - the listener warns at startup too;
  - ingest failure alerts debounce to ONE Slack alert per source per day: the
    second failure the same day is silent in Slack but present in the audit
    log with the same honest message; the dead-letter escalation (once per
    clip ever) still posts.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, ops_alerts, opus_ingest  # noqa: E402
from tests.test_opus_ingest import (CLIP_A, FakeOpus, FakeS3,  # noqa: E402
                                    RecordingPoster, _arm)


REAL_ID = "proj_8f3k2m9q"


# ---- placeholder validation ---------------------------------------------------------
def test_validator_splits_placeholders_from_real_ids():
    real, bad = opus_ingest.split_placeholder_project_ids(
        ["P1", "p2", "abc", "", " ", REAL_ID, "1234567", "P1234567"])
    assert real == [REAL_ID, "1234567"]          # real-looking ids untouched
    # P<digits> is a placeholder at ANY length; short strings too
    assert bad == ["P1", "p2", "abc", "P1234567"]


def test_placeholder_never_reaches_api(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS", ["P1", "p2", "abc", REAL_ID])
    monkeypatch.setattr(config, "OPUS_COLLECTION_IDS", [])
    scanned = []

    class AdversarialApi(FakeOpus):
        def list_collections(self):
            raise AssertionError("collections listed despite a real pinned id")

        def list_exportable_clips(self, q, source_id):
            assert source_id == REAL_ID, \
                f"placeholder reached the API: {source_id!r}"
            scanned.append(source_id)
            return []

    opus_ingest.pull(api=AdversarialApi(), s3_client=FakeS3(), out_dir=lib)
    out = capsys.readouterr().out
    assert scanned == [REAL_ID]                  # the real id still scanned
    assert "WARNING" in out
    for bad in ("P1", "p2", "abc"):              # every bad value named
        assert bad in out
    assert out.count("SKIPPED") == 1             # ONE warning line, not per value


def test_all_placeholders_behave_like_no_pin_list(monkeypatch, tmp_path, capsys):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS", ["P1", "P2"])
    monkeypatch.setattr(config, "OPUS_COLLECTION_IDS", [])
    api = FakeOpus()                             # one collection, two clips
    out = opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)
    assert out["pulled"] == 2                    # fell back to collection discovery
    assert "WARNING" in capsys.readouterr().out


def test_listener_startup_warns_on_placeholder(monkeypatch, capsys):
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS", ["P1"])
    monkeypatch.delenv("AGENT_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_CONNECT_ENABLED", raising=False)
    from agent import listener
    listener.run_listener()                      # returns early: no Slack tokens
    out = capsys.readouterr().out
    assert "placeholder" in out and "P1" in out


# ---- alert debounce: one Slack alert per source per day -----------------------------
def _arm_alerts(monkeypatch, tmp_path):
    lib = _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    return lib, rec


def test_second_failure_same_day_slack_silent_audit_present(monkeypatch, tmp_path):
    lib, rec = _arm_alerts(monkeypatch, tmp_path)
    api = FakeOpus(clips=[CLIP_A], fail_downloads=True)

    opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)   # attempt 1
    assert len(rec.notices) == 1                 # first failure posts to Slack
    assert "attempt 1" in rec.notices[0]

    opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)   # attempt 2, same day
    assert len(rec.notices) == 1                 # SILENT in Slack
    debounced = [r for r in db.audit_rows() if r["subject"] == "debounced"]
    assert debounced                             # ...but present in the audit log
    assert "attempt 2" in debounced[0]["reason"]  # same honest message

    # dead-letter escalation (attempt 3) is once-per-clip-ever and still posts
    opus_ingest.pull(api=api, s3_client=FakeS3(), out_dir=lib)
    assert len(rec.notices) == 2
    assert "dead-lettered" in rec.notices[1]


def test_list_failure_debounces_per_project(monkeypatch, tmp_path):
    lib, rec = _arm_alerts(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "OPUS_PROJECT_IDS",
                        ["proj_8f3k2m9q", "proj_7walnut2"])
    monkeypatch.setattr(config, "OPUS_COLLECTION_IDS", [])

    class BoomApi(FakeOpus):
        def list_exportable_clips(self, q, source_id):
            raise RuntimeError("Opus API 500 on /api/exportable-clips")

    opus_ingest.pull(api=BoomApi(), s3_client=FakeS3(), out_dir=lib)
    assert len(rec.notices) == 2                 # one per project, first run
    opus_ingest.pull(api=BoomApi(), s3_client=FakeS3(), out_dir=lib)
    assert len(rec.notices) == 2                 # hourly repeat says nothing new
    assert len([r for r in db.audit_rows() if r["subject"] == "debounced"]) == 2
