"""
The foundation-trigger check, tested from BOTH sides.

These are written as security properties, not as a description of the code: each one
names the rule Blake stated and asserts the rule, so reverting the implementation a
different wrong way still turns them red. (D68: "a test written alongside a fix tends
to be shaped like the code, not like the rule.")
"""
import pytest

from agent.client_dm_support import scope_gate as sg


# ---------------------------------------------------------------------------
# THE ALLOW SIDE — the two real diagnosed cases, by their literal shapes.
# ---------------------------------------------------------------------------
def test_case1_per_gym_drive_sync_is_allowed():
    """Chad / crossfitlocal: running the existing sync over ONE gym's own
    media_source + media_asset rows is client-scoped and data-only."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_PER_GYM_SYNC,
        summary="run the gym-media Drive sync for one gym",
        tables=("media_source", "media_asset"),
        scope_column="gym_id",
        scope_values=("crossfitlocal",),
    ))
    assert v.allowed, v.reason
    assert v.meta["scope_value"] == "crossfitlocal"


def test_case2_ask_client_is_allowed_and_writes_nothing():
    """John / toughtemple52040e: the correct remedy is a QUESTION. It is allowed
    precisely because it writes nothing."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_ASK_CLIENT,
        summary="ask the gym for their real CTAs",
    ))
    assert v.allowed, v.reason


def test_ask_client_that_secretly_writes_is_refused():
    """A non-writing kind that names a table is contradicting itself; refuse rather
    than trust the label."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_ASK_CLIENT,
        tables=("media_asset",),
        scope_column="gym_id",
        scope_values=("toughtemple52040e",),
    ))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_UNKNOWN_SHAPE


# ---------------------------------------------------------------------------
# THE BLOCK SIDE — one test per foundation trigger Blake named.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", ["gym_billing", "subscriptions", "invoices",
                                   "stripe_customers", "payment_methods", "refunds"])
def test_any_billing_table_escalates(table):
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=(table,),
        scope_column="gym_id", scope_values=("crossfitlocal",),
    ))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_BILLING


@pytest.mark.parametrize("table", ["campaigns", "adsets", "ads", "custom_audiences",
                                   "gym_campaign_actions", "budget_schedules"])
def test_any_ad_table_escalates(table):
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=(table,),
        scope_column="gym_id", scope_values=("crossfitlocal",),
    ))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_AD_MONEY


def test_ad_flag_escalates_even_with_a_perfectly_scoped_allowed_table():
    """is_ad_related is checked FIRST, before the allowlist can rescue it. An action
    that is ad-related cannot be laundered through an allowed table name."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=("content_calendar",),
        scope_column="gym_id", scope_values=("crossfitlocal",),
        is_ad_related=True,
    ))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_AD_MONEY


def test_pixel_and_capi_escalate():
    """Pixel/CAPI tables sit on the ad surface too, and the AD check runs first on
    purpose (Blake: ad money is checked before anything else). So the rule asserted
    here is that they always escalate; either trigger name is a correct report of
    why, and both are foundation triggers."""
    for t in ("pixel", "capi_events", "conversion_events"):
        v = sg.check(sg.ProposedAction(
            kind=sg.KIND_DATA_PATCH, tables=(t,),
            scope_column="gym_id", scope_values=("x",)))
        assert v.escalate, t
        assert v.trigger in (sg.TRIGGER_PIXEL_CAPI, sg.TRIGGER_AD_MONEY), t


def test_pixel_flag_on_an_otherwise_allowed_action_escalates():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=("media_asset",),
        scope_column="gym_id", scope_values=("x",), touches_pixel_or_capi=True))
    assert v.escalate and v.trigger == sg.TRIGGER_PIXEL_CAPI


def test_feature_flag_or_env_escalates():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=("media_asset",),
        scope_column="gym_id", scope_values=("crossfitlocal",),
        touches_env_or_flag=True,
    ))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_MULTI_GYM_CONFIG


@pytest.mark.parametrize("op", ["ALTER TABLE media_asset ADD COLUMN x int",
                                "create policy p on media_asset",
                                "DROP TABLE media_source",
                                "grant select on media_asset to anon",
                                "apply migration 0310"])
def test_schema_migration_and_rls_escalate(op):
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=("media_asset",),
        scope_column="gym_id", scope_values=("crossfitlocal",),
        sql_ops=(op,),
    ))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_SCHEMA


@pytest.mark.parametrize("table", ["gyms", "app_users", "gym_assignments",
                                   "echo_gym_settings", "echo_intake_tokens"])
def test_identity_and_shared_tables_escalate(table):
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=(table,),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_MULTI_GYM_CONFIG


@pytest.mark.parametrize("table", ["secrets", "tokens", "api_keys",
                                   "echo_social_connections", "oauth_tokens"])
def test_secret_tables_escalate(table):
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=(table,),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_SECRETS


# ---------------------------------------------------------------------------
# "ANOTHER CLIENT'S DATA", made mechanical.
# ---------------------------------------------------------------------------
def test_a_write_with_no_scope_column_escalates():
    """THE RULE: a write with no gym-scope column could reach any gym's rows. A
    missing scope must be a REFUSAL, not a permissive default."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=("media_asset",),
        scope_column="", scope_values=("crossfitlocal",)))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_OTHER_CLIENT


def test_a_write_naming_two_gyms_escalates():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_PER_GYM_SYNC, tables=("media_asset",),
        scope_column="gym_id", scope_values=("crossfitlocal", "toughtemple52040e")))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_OTHER_CLIENT


def test_a_write_naming_zero_gyms_escalates():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_PER_GYM_SYNC, tables=("media_asset",),
        scope_column="gym_id", scope_values=()))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_OTHER_CLIENT


def test_an_unknown_scope_column_escalates():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=("media_asset",),
        scope_column="tenant", scope_values=("crossfitlocal",)))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_OTHER_CLIENT


# ---------------------------------------------------------------------------
# REFUSE BY DEFAULT — the D68 inversion. Novel input has nowhere to match.
# ---------------------------------------------------------------------------
def test_an_unknown_action_kind_escalates():
    v = sg.check(sg.ProposedAction(
        kind="just_do_it", tables=("media_asset",),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_UNKNOWN_SHAPE


def test_an_unknown_table_escalates_even_when_perfectly_scoped():
    """A table nobody thought about is refused, not allowed. This is the property
    that makes the gate safe against tables that do not exist yet."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=("some_new_table_2027",),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_UNKNOWN_SHAPE


def test_a_write_naming_no_table_escalates():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=(),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate


def test_non_action_input_escalates():
    assert sg.check(None).escalate
    assert sg.check({"kind": "per_gym_sync"}).escalate


# ---------------------------------------------------------------------------
# ADVERSARIAL CASES — real client sentences, turned into the action they would
# require. Each must escalate.
# ---------------------------------------------------------------------------
def test_adversarial_just_refund_me():
    """"hey can you just refund me for this month" -> a Stripe refund."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, summary="issue a refund",
        tables=("refunds", "charges"),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate and v.trigger == sg.TRIGGER_BILLING


def test_adversarial_turn_off_my_ad_budget():
    """"turn off my ad budget for the rest of the month" -> a campaign pause."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, summary="pause campaigns / zero the budget",
        tables=("campaigns",), is_ad_related=True,
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate and v.trigger == sg.TRIGGER_AD_MONEY


def test_adversarial_move_me_to_a_different_stripe_plan():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, summary="change plan/tier",
        tables=("subscriptions", "plans"),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate and v.trigger == sg.TRIGGER_BILLING


def test_adversarial_copy_the_other_gyms_photos_over():
    """"can you just copy the photos from my other location's account" — an allowed
    table, an allowed kind, and still refused because it names two gyms."""
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=("media_asset",),
        scope_column="gym_id", scope_values=("crossfitlocal", "otherbox")))
    assert v.escalate and v.trigger == sg.TRIGGER_OTHER_CLIENT


def test_adversarial_turn_the_drive_feature_on_for_everyone():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_DATA_PATCH, tables=("media_source",),
        scope_column="gym_id", scope_values=("crossfitlocal",),
        touches_env_or_flag=True))
    assert v.escalate and v.trigger == sg.TRIGGER_MULTI_GYM_CONFIG


# ---------------------------------------------------------------------------
# THE CODE-FIX LANE'S GATE (the lane itself is empty; see flow.py).
# ---------------------------------------------------------------------------
def test_code_fix_within_allowed_roots_and_one_gym_is_allowed():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX,
        paths=("agent/jobs/sync_gym_media.py", "tests/test_gym_media_sync.py"),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.allowed, v.reason


@pytest.mark.parametrize("path", [
    "migrations/0310_add_thing.sql",
    ".env",
    ".github/workflows/ci.yml",
    "agent/config.py",
    "agent/slack_convo/adapter.py",
    "railway.json",
    "brand_voice/toughtemple52040e/lasso_voice.md",
    "/data/brand_voice/toughtemple52040e/lasso_voice.md",
])
def test_code_fix_touching_foundation_paths_escalates(path):
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX, paths=(path,),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate, path


def test_code_fix_outside_allowed_roots_escalates():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX, paths=("agent/zernio_publisher.py",),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate
    assert v.trigger == sg.TRIGGER_MULTI_GYM_CONFIG


def test_code_fix_with_no_named_paths_escalates():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX, paths=(),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate


def test_code_fix_naming_two_gyms_escalates():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX, paths=("agent/jobs/sync_gym_media.py",),
        scope_column="gym_id", scope_values=("a", "b")))
    assert v.escalate and v.trigger == sg.TRIGGER_OTHER_CLIENT


# ---------------------------------------------------------------------------
# TWO-WAY GUARDS on the constants themselves. D68: assert the allow-list still
# CONTAINS what it must, so a future edit cannot quietly widen or narrow it.
# ---------------------------------------------------------------------------
def test_allowed_data_tables_is_exactly_the_three_client_scoped_tables():
    assert sg.ALLOWED_DATA_TABLES == frozenset(
        {"media_source", "media_asset", "content_calendar"})


def test_billing_and_ad_tables_are_disjoint_from_the_allowlist():
    assert not (sg.ALLOWED_DATA_TABLES & sg.BILLING_TABLES)
    assert not (sg.ALLOWED_DATA_TABLES & sg.AD_TABLES)
    assert not (sg.ALLOWED_DATA_TABLES & sg.IDENTITY_TABLES)
    assert not (sg.ALLOWED_DATA_TABLES & sg.SECRET_TABLES)


def test_no_code_fix_root_reaches_a_blocked_path():
    for root in sg.ALLOWED_CODE_FIX_ROOTS:
        for frag in sg.BLOCKED_PATH_FRAGMENTS:
            assert frag not in root, (root, frag)


# ---------------------------------------------------------------------------
# THE BLOCKED-PATH LIST, MADE LOAD-BEARING.
#
# An independent mutation disabled the ENTIRE BLOCKED_PATH_FRAGMENTS loop and the
# suite stayed green: every path the old tests used was also outside the allowed
# roots, so the roots check caught it and the blocklist was asserted by nothing.
# These paths sit INSIDE an allowed root, so only the blocklist can refuse them.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "tests/fixtures/.env",
    "tests/fixtures/secrets/key.json",
    "tests/fixtures/credentials.json",
    "tests/brand_voice/toughtemple52040e/lasso_voice.md",
    "agent/client_dm_support/migrations/0311.sql",
    "tests/.github/workflows/ci.yml",
])
def test_a_blocked_fragment_inside_an_allowed_root_still_escalates(path):
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX, paths=(path,),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate, path


def test_every_blocked_fragment_is_refused_even_under_an_allowed_root():
    """The two-way form: iterate the constant itself, so adding a fragment without
    enforcing it, or dropping one, is caught."""
    for frag in sg.BLOCKED_PATH_FRAGMENTS:
        probe = f"tests/{frag.strip('/')}/x.py" if frag.endswith("/") \
            else f"tests/{frag.lstrip('.')}x/{frag}"
        v = sg.check(sg.ProposedAction(
            kind=sg.KIND_CODE_FIX, paths=(probe,),
            scope_column="gym_id", scope_values=("crossfitlocal",)))
        assert v.escalate, (frag, probe)


# ---------------------------------------------------------------------------
# PATH TRAVERSAL. Every one of these was ALLOWED before normalisation.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,why", [
    ("agent/jobs/../slack_convo/adapter.py", "the #fixer gate, explicitly out of scope"),
    ("agent/jobs/../config.py", "the flag surface"),
    ("agent/jobs/../meta_publisher.py", "outside the allowed roots"),
    ("agent/jobs/../../../../etc/passwd", "outside the repo entirely"),
    ("/Users/blakeruff/.ssh/tests/id_rsa", "absolute path containing '/tests/'"),
    ("agent/media_../../config.py", "prefix trick"),
    ("agent/jobs/./../../secrets.env", "dot segments"),
])
def test_path_traversal_cannot_walk_out_of_an_allowed_root(path, why):
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX, paths=(path,),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.escalate, f"{path} ({why})"


def test_an_absolute_path_is_refused_outright():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX, paths=("/etc/passwd",),
        scope_column="gym_id", scope_values=("x",)))
    assert v.escalate
    assert "absolute" in v.reason


def test_normalisation_does_not_break_ordinary_allowed_paths():
    v = sg.check(sg.ProposedAction(
        kind=sg.KIND_CODE_FIX,
        paths=("./agent/jobs/sync_gym_media.py", "tests//test_gym_media_sync.py"),
        scope_column="gym_id", scope_values=("crossfitlocal",)))
    assert v.allowed, v.reason
