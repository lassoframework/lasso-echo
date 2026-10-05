"""Guard the temporary LASSO claim envelope against a fleet-wide cap change."""

from pathlib import Path

from agent import calendar_autopublish as cap
from agent import cadence


SQL = (Path(__file__).parents[1] / "migrations" /
       "lasso_bounded_catchup_capacity_20261005.sql").read_text().lower()


def test_only_lasso_can_claim_temporary_capacity_five():
    assert "p_capacity > 3 and p_capacity <> 5" in SQL
    assert "p_capacity = 3 and p_gym_id <> 'lasso'" in SQL
    assert "p_capacity = 5 and not (\n      p_gym_id = 'lasso'" in SQL
    assert "p_day between date '2026-10-06' and date '2026-10-11'" in SQL
    assert "p_timezone = 'america/new_york'" in SQL
    assert "v_row.post_date = p_day" in SQL
    assert "v_row.post_date between date '2026-10-02' and date '2026-10-05'" in SQL


def test_three_current_plus_two_backlog_per_account_format_on_actual_day():
    assert "v_used >= p_capacity" in SQL
    assert "v_current_used >= 3" in SQL
    assert "v_backlog_used >= 2" in SQL
    assert "lower(btrim(coalesce(account, '')))" in SQL
    assert "coalesce(nullif(lower(btrim(format)), ''), 'feed')" in SQL
    assert "status = 'publishing' and publish_reservation_day = p_day" in SQL
    assert "(published_at at time zone p_timezone)::date = p_day" in SQL
    assert "pg_advisory_xact_lock(hashtextextended(p_gym_id, 0))" in SQL


def test_existing_claim_and_media_safety_are_preserved():
    for fragment in (
        "id = p_row_id and gym_id = p_gym_id",
        "status in ('pending', 'approved')",
        "published_at is null",
        "late_post_id is null",
        "variant_status = 'active'",
        "nullif(btrim(coalesce(image_url, '')), '') is not null",
        "media_not_ready_reason is null",
        "p_approved_only and v_row.status <> 'approved'",
        "publish_claim_token = v_token",
        "to service_role",
    ):
        assert fragment in SQL


def test_lasso_normal_twelve_and_temporary_twenty_keep_client_cap(monkeypatch):
    monkeypatch.setattr(cap.config, "lasso_three_feed_enabled", lambda: True)
    assert cap._client_publish_limits("lasso", "2026-10-05", 8) == (7, 12)
    assert cap._client_publish_limits("lasso", "2026-10-06", 8) == (7, 20)
    assert cap._client_publish_limits("lasso", "2026-10-11", 8) == (9, 20)
    assert cap._client_publish_limits("lasso", "2026-10-12", 8) == (7, 12)
    assert cap._client_publish_limits("client-gym", "2026-10-06", 8) == (7, 8)


def test_temporary_five_claims_only_exact_lasso_outage_or_current_rows(monkeypatch):
    monkeypatch.setattr(cap.config, "lasso_three_feed_enabled", lambda: True)
    monkeypatch.setattr(cadence, "resolve_posts_per_day",
                        lambda gym, store, day=None: 3)
    row = {"format": "feed", "post_date": "2026-10-02"}
    assert cap._publish_capacity("lasso", row, object(), "2026-10-06") == 5
    assert cap._publish_capacity("lasso", {**row, "format": "story"},
                                 object(), "2026-10-06") == 5
    assert cap._publish_capacity("lasso", {**row, "post_date": "2026-10-06"},
                                 object(), "2026-10-06") == 5
    assert cap._publish_capacity("lasso", {**row, "post_date": "2026-10-01"},
                                 object(), "2026-10-06") == 3
    assert cap._publish_capacity("lasso", row, object(), "2026-10-12") == 3
    assert cap._publish_capacity("client-gym", row, object(), "2026-10-06") == 3
