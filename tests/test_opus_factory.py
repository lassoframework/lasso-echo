"""
Opus video factory tests. Fully OFFLINE (fake Opus API, no network, no spend).

Part 1: all-project scan normalizes finished clips across MULTIPLE projects with
no allowlist; unfinished clips (no export url) are excluded; the master flag
gates the whole thing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, opus_factory  # noqa: E402

POD_CLIP = {
    "id": "PODPROJ.C1", "title": "The follow up problem",
    "durationMs": 38000, "uriForExport": "https://cdn.opus/pod1.mp4",
    "score": 92, "transcript": "Most gyms do not have a lead problem.",
}
BIZ_CLIP = {
    "id": "PROJ2.C1", "title": "Booking rate math",
    "durationMs": 45000, "uriForExport": "https://cdn.opus/biz1.mp4",
    "viralityScore": "72", "transcriptText": "We book 71.9 percent of leads.",
}
UNFINISHED = {"id": "PROJ2.C2", "title": "still rendering", "durationMs": 30000}


class FakeOpus:
    """Mirrors the OpusAPI surface the factory uses: list_projects +
    list_exportable_clips(q, project_id)."""

    def __init__(self, projects, clips_by_project):
        self._projects = projects                   # [{"id","title"}]
        self._clips = clips_by_project              # {project_id: [clip,...]}

    def list_projects(self):
        return list(self._projects)

    def list_exportable_clips(self, q, source_id):
        assert q == "findByProjectId"
        return list(self._clips.get(source_id, []))

    def download(self, url):
        return b"VIDEO"


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")


def _fake():
    return FakeOpus(
        projects=[{"id": "PODPROJ", "title": "Gym Marketing Made Simple"},
                  {"id": "PROJ2", "title": "Client Webinars"}],
        clips_by_project={"PODPROJ": [POD_CLIP], "PROJ2": [BIZ_CLIP, UNFINISHED]})


# ---- flag gate -----------------------------------------------------------------------------

def test_scan_flag_off_returns_empty(monkeypatch):
    monkeypatch.delenv("AGENT_OPUS_FACTORY_ENABLED", raising=False)
    assert opus_factory.scan(api=_fake()) == []


# ---- all-project scan, no allowlist ---------------------------------------------------------

def test_scan_returns_clips_across_projects(monkeypatch):
    _arm(monkeypatch)
    records = opus_factory.scan(api=_fake())
    by_id = {r.clip_id: r for r in records}
    assert set(by_id) == {"PODPROJ.C1", "PROJ2.C1"}    # both projects, finished only
    pod = by_id["PODPROJ.C1"]
    assert pod.project_id == "PODPROJ"
    assert pod.source_title == "Gym Marketing Made Simple"
    assert pod.opus_score == 92.0
    assert pod.duration_s == 38.0
    assert pod.transcript == "Most gyms do not have a lead problem."
    biz = by_id["PROJ2.C1"]
    assert biz.source_title == "Client Webinars"
    assert biz.opus_score == 72.0                       # string coerced


def test_scan_excludes_unfinished(monkeypatch):
    _arm(monkeypatch)
    ids = {r.clip_id for r in opus_factory.scan(api=_fake())}
    assert "PROJ2.C2" not in ids                        # render-in-progress excluded


def test_scan_needs_no_allowlist(monkeypatch):
    """No AGENT_OPUS_PROJECT_IDS / collection id set: the scan still finds every
    project's clips via list_projects."""
    _arm(monkeypatch)
    monkeypatch.delenv("AGENT_OPUS_PROJECT_IDS", raising=False)
    monkeypatch.delenv("AGENT_OPUS_COLLECTION_IDS", raising=False)
    assert len(opus_factory.scan(api=_fake())) == 2


def test_scan_dedupes_clip_shared_across_projects(monkeypatch):
    _arm(monkeypatch)
    fake = FakeOpus(projects=[{"id": "A", "title": "A"}, {"id": "B", "title": "B"}],
                    clips_by_project={"A": [POD_CLIP], "B": [POD_CLIP]})
    assert len(opus_factory.scan(api=fake)) == 1


def test_normalize_rejects_unexportable():
    assert opus_factory.normalize_clip({"id": "X"}, "P") is None
    assert opus_factory.normalize_clip({"uriForExport": "http://x/y.mp4"}, "P") is None
