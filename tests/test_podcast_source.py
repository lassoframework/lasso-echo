"""Headless Drive pull fails LOUD (never silent no-op) when unconfigured."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import podcast_source, config  # noqa: E402


def test_no_folder_id_raises(monkeypatch):
    monkeypatch.setattr(config, "podcast_drive_folder_id", lambda: "")
    with pytest.raises(podcast_source.PodcastSourceError) as e:
        podcast_source.newest_episode("/tmp/x")
    assert "AGENT_PODCAST_DRIVE_FOLDER_ID" in str(e.value)


def test_no_service_account_raises(monkeypatch):
    monkeypatch.setattr(config, "podcast_drive_folder_id", lambda: "folder123")
    monkeypatch.setattr(config, "gdrive_service_account_json", lambda: "")
    with pytest.raises(podcast_source.PodcastSourceError) as e:
        podcast_source.newest_episode("/tmp/x")
    assert "AGENT_GDRIVE_SA_JSON" in str(e.value)
