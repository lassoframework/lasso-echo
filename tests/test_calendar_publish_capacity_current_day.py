from pathlib import Path


SQL = (Path(__file__).parents[1] / "migrations" /
       "publish_capacity_current_day_20260930.sql").read_text().lower()


def test_only_current_day_inflight_claims_consume_capacity():
    assert "status = 'publishing' and publish_reservation_day = p_day" in SQL
    assert "and (status = 'publishing'\n" not in SQL


def test_capacity_repair_is_fleet_wide_but_three_remains_lasso_only():
    assert "p_gym_id <> 'lasso'" in SQL
    assert "p_capacity = 3" in SQL
    # The day scoping is not nested under a LASSO predicate: it is the single
    # publishing-count expression shared by 1x, 2x, and LASSO 3x tenants.
    day_scope = SQL.index("status = 'publishing' and publish_reservation_day = p_day")
    count_query = SQL.index("select count(*) into v_used")
    claim_update = SQL.index("v_token := gen_random_uuid()")
    assert count_query < day_scope < claim_update


def test_owned_claim_and_tenant_guards_survive_the_capacity_repair():
    for fragment in (
        "pg_advisory_xact_lock(hashtextextended(p_gym_id, 0))",
        "id = p_row_id and gym_id = p_gym_id",
        "status in ('pending', 'approved')",
        "published_at is null",
        "late_post_id is null",
        "variant_status = 'active'",
        "publish_claim_token = v_token",
        "security definer set search_path = public",
        "to service_role",
    ):
        assert fragment in SQL
