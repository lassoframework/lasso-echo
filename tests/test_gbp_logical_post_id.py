import uuid

import pytest

from agent import gbp_planner as gp


def _row(day="2026-10-04", image="https://example.test/shared.jpg"):
    return gp._row("portal-gym", "gym_ig", day, "caption", image,
                   topic_type="STANDARD", pillar="community")


def test_each_gbp_singleton_gets_a_distinct_uuid_even_with_same_day_and_photo():
    first = _row()
    second = _row()

    first_id = uuid.UUID(first["logical_post_id"])
    second_id = uuid.UUID(second["logical_post_id"])
    assert first_id != second_id
    assert first["post_date"] == second["post_date"]
    assert first["image_url"] == second["image_url"]


def test_retrying_same_row_object_preserves_its_valid_uuid():
    row = _row()
    original = row["logical_post_id"]

    assert gp._ensure_logical_post_id(row) == original
    assert row["logical_post_id"] == original


def test_invalid_existing_identity_fails_closed():
    row = {"logical_post_id": "not-a-uuid"}

    with pytest.raises(ValueError, match="invalid logical_post_id"):
        gp._ensure_logical_post_id(row)


# ---- rollout flag (ECHO_LOGICAL_POST_ID_ENABLED, default OFF) --------------


@pytest.fixture(autouse=True)
def _logical_post_id_flag_on(monkeypatch):
    """Existing tests in this file exercise the ON behavior."""
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")


def test_flag_defaults_off_and_row_carries_no_key(monkeypatch):
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    from agent import config
    assert config.logical_post_id_enabled() is False
    row = _row()
    assert "logical_post_id" not in row
