"""
scope_gate.py — the FOUNDATION-TRIGGER CHECK, as real code.

Blake's rule, which this file is the executable form of:

    "Does fixing this client's specific complaint require changing something that
    isn't exclusively theirs? If yes, it's foundational: stop, message Blake, do not
    act."

The updated trigger list (Blake, 2026-09-06 — supersedes the first draft, which also
listed "requires a code deploy"; a code fix scoped to ONE client's own bug is now in
scope, bounded by everything else here):

  * ad budget, ad targeting, campaign launch/pause  -- ALWAYS, unconditionally
  * feature flags or config affecting more than one gym
  * schema / migrations / RLS policy changes
  * secrets, tokens, credentials, auth config
  * another client's data
  * billing (Stripe, invoicing, plan/tier)
  * pixel / CAPI setup

HOW THIS IS ORDERED, AND WHY IT MATTERS.
The blocklist is evaluated BEFORE the allowlist, and the allowlist is a membership
test against frozen constants rather than a pattern. So the default for anything
novel is REFUSE, in both directions:

  * an action KIND this file does not know   -> refuse
  * a TABLE this file does not know          -> refuse
  * a scope COLUMN this file does not know   -> refuse
  * a repo PATH outside the allowed roots    -> refuse
  * zero, or more than one, scope value      -> refuse

That last one is the "another client's data" trigger made mechanical: an action is
allowed to name exactly one gym. Not "no gym in the blocklist" -- exactly one, named.
A missing scope is not a permissive default, it is a refusal, which is the shape D68
says an enumeration must have to be safe: closed input, refuse-by-default.

This file has no knowledge of the client's SENTENCE. It inspects the PROPOSED ACTION.
That is the whole point of D68: the question is not the thing to classify.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# ACTION KINDS. A kind not listed here refuses.
# ---------------------------------------------------------------------------
KIND_PER_GYM_SYNC = "per_gym_sync"          # run an existing sync for ONE gym's own source
KIND_DATA_PATCH = "data_patch"              # patch rows in ONE gym's own content tables
KIND_ASK_CLIENT = "ask_client"              # no write at all; ask for information
KIND_CODE_FIX = "code_fix"                  # a real code change for ONE gym's own bug
KIND_NOOP_WAIT = "noop_wait"                # nothing to do; state is already correct

ALLOWED_ACTION_KINDS = frozenset({
    KIND_PER_GYM_SYNC, KIND_DATA_PATCH, KIND_ASK_CLIENT,
    KIND_CODE_FIX, KIND_NOOP_WAIT,
})

# Kinds that write nothing, so the table/scope rules below do not apply to them.
NON_WRITING_KINDS = frozenset({KIND_ASK_CLIENT, KIND_NOOP_WAIT})

# ---------------------------------------------------------------------------
# THE ALLOWLIST: tables whose rows belong to exactly one gym and which this
# capability may write, and the columns that carry that gym scope.
# ---------------------------------------------------------------------------
ALLOWED_DATA_TABLES = frozenset({
    "media_source",
    "media_asset",
    "content_calendar",
})

ALLOWED_SCOPE_COLUMNS = frozenset({"gym_id", "account_key", "base_key"})

# Repo roots a code fix may touch. Everything else refuses -- including agent/config.py
# (a flag surface), migrations/ (schema), .github/ (CI + secrets) and
# agent/slack_convo/ (the #fixer bus's own gate, explicitly out of scope).
ALLOWED_CODE_FIX_ROOTS = (
    "agent/jobs/",
    "agent/gym_media_",
    "agent/media_",
    "agent/client_dm_support/",
    "tests/",
)

# ---------------------------------------------------------------------------
# THE BLOCKLIST. Checked FIRST. Every entry here maps to one of Blake's named
# foundation triggers, and the trigger name is returned so the escalation card can
# say which line was hit rather than "not allowed".
# ---------------------------------------------------------------------------
BILLING_TABLES = frozenset({
    "gym_billing", "billing", "invoices", "invoice", "subscriptions",
    "subscription", "stripe_customers", "stripe_subscriptions",
    "stripe_events", "payments", "payment_methods", "charges", "plans",
    "prices", "coupons", "refunds", "credits", "customer",
})

AD_TABLES = frozenset({
    "ad_accounts", "ad_account", "campaigns", "campaign", "adsets", "adset",
    "ad_sets", "ads", "ad", "ad_creatives", "adcreatives", "targeting",
    "custom_audiences", "lookalike_audiences", "pixels", "pixel",
    "capi_events", "conversion_events", "gym_campaign_actions",
    "budget_schedules", "meta_campaigns", "meta_adsets", "meta_ads",
})

IDENTITY_TABLES = frozenset({
    "gyms", "app_users", "users", "gym_assignments", "memberships",
    "auth.users", "roles", "permissions", "echo_gym_settings",
    "echo_intake_tokens", "gym_billing_tier",
})

SECRET_TABLES = frozenset({
    "secrets", "vault", "tokens", "api_keys", "credentials",
    "echo_social_connections", "oauth_tokens", "service_accounts",
})

# Operation shapes that are foundational whatever table they name.
SCHEMA_OPS = frozenset({
    "create table", "alter table", "drop table", "create policy",
    "alter policy", "drop policy", "create index", "drop index",
    "grant", "revoke", "migration", "rls", "create function",
})

# Path fragments that are foundational whatever the action kind claims.
BLOCKED_PATH_FRAGMENTS = (
    "migrations/",
    ".env",
    ".github/",
    "agent/config.py",
    "agent/slack_convo/",
    "railway.json",
    "nixpacks.toml",
    "procfile",
    "secrets",
    "credentials",
    "brand_voice/",          # a gym's voice doc holds client-authored copy; Echo may
                             # never write one (CLAUDE.md: no invented facts or offers)
)

TRIGGER_AD_MONEY = "ad_budget_targeting_or_campaign"
TRIGGER_MULTI_GYM_CONFIG = "feature_flag_or_config_affecting_more_than_one_gym"
TRIGGER_SCHEMA = "schema_migration_or_rls"
TRIGGER_SECRETS = "secrets_tokens_credentials_or_auth"
TRIGGER_OTHER_CLIENT = "another_clients_data"
TRIGGER_BILLING = "billing_stripe_invoicing_or_plan_tier"
TRIGGER_PIXEL_CAPI = "pixel_or_capi_setup"
TRIGGER_UNKNOWN_SHAPE = "unrecognised_action_shape_refuse_by_default"
TRIGGER_CLIENT_AUTHORED_CONTENT = "client_authored_content_echo_may_not_write"


@dataclass(frozen=True)
class ProposedAction:
    """What the capability wants to DO. Never the client's sentence."""

    kind: str
    summary: str = ""
    tables: tuple = ()               # tables written
    scope_column: str = ""           # the column carrying the gym scope
    scope_values: tuple = ()         # must be exactly one value
    paths: tuple = ()                # repo files a code fix would write
    sql_ops: tuple = ()              # DDL/DML verbs, when the action names any
    touches_env_or_flag: bool = False
    touches_pixel_or_capi: bool = False
    is_ad_related: bool = False

    def normalised_tables(self):
        return tuple(str(t or "").strip().lower() for t in self.tables)


@dataclass(frozen=True)
class ScopeVerdict:
    allowed: bool
    reason: str
    trigger: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def escalate(self):
        return not self.allowed


def _deny(reason, trigger):
    return ScopeVerdict(allowed=False, reason=reason, trigger=trigger)


class _PathRefused(Exception):
    pass


def _normalise_path(raw):
    """A repo-relative, traversal-free, lowercase path -- or a refusal.

    Refuses an absolute path, a path that escapes the repo root, and any path still
    holding a '..' segment after normalisation. `posixpath.normpath` also collapses
    './' and duplicate slashes, so the substring tests below run over one canonical
    spelling instead of the many that name the same file.
    """
    p = str(raw or "").strip().replace("\\", "/")
    if not p:
        raise _PathRefused("a code fix named an empty path")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise _PathRefused(
            f"{raw!r} is an absolute path; a code fix may only name repo-relative "
            f"files inside the allowed roots"
        )
    norm = posixpath.normpath(p).lower()
    if norm == ".." or norm.startswith("../") or "/../" in norm:
        raise _PathRefused(
            f"{raw!r} escapes the repository root after normalisation ({norm!r})"
        )
    if norm.startswith("./"):
        norm = norm[2:]
    return norm


def check(action: ProposedAction) -> ScopeVerdict:
    """allowed, or escalate — with the foundation trigger named.

    Blocklist first, allowlist second, refuse-by-default third.
    """
    if not isinstance(action, ProposedAction):
        return _deny(
            f"check() takes a ProposedAction, got {type(action).__name__}",
            TRIGGER_UNKNOWN_SHAPE,
        )

    kind = str(action.kind or "").strip()
    tables = action.normalised_tables()
    sql_ops = tuple(str(o or "").strip().lower() for o in action.sql_ops)

    # PATHS ARE NORMALISED BEFORE ANY CHECK, and a path that will not normalise is
    # refused outright. Without this, `agent/jobs/../slack_convo/adapter.py` satisfied
    # the allowed-roots prefix test and walked straight out of it -- reaching the
    # #fixer gate, agent/config.py and anything else on the disk, because both the
    # allowlist and the blocklist were plain substring tests over an unnormalised
    # string. Traversal is not a fragment to add to a denylist (that would be an
    # ENUMERATION OF AN OPEN SET); it is removed by normalising, and anything still
    # containing '..' or anchored outside the repo is refused.
    try:
        paths = tuple(_normalise_path(p) for p in action.paths)
    except _PathRefused as e:
        return _deny(str(e), TRIGGER_UNKNOWN_SHAPE)

    # ---- BLOCKLIST -------------------------------------------------------
    # 1. Ad money. Unconditional, and checked before anything else so that no
    #    later branch can be reached with an ad-shaped action in hand.
    if action.is_ad_related or (set(tables) & AD_TABLES):
        return _deny(
            "ad budget, ad targeting and campaign launch/pause are never "
            "autonomous for this capability; this lane has no ad-write call path "
            "at all and a human handles every one of them",
            TRIGGER_AD_MONEY,
        )

    # 2. Pixel / CAPI.
    if action.touches_pixel_or_capi or any(
        t in ("pixel", "pixels", "capi_events", "conversion_events") for t in tables
    ):
        return _deny("pixel/CAPI setup requires explicit approval", TRIGGER_PIXEL_CAPI)

    # 3. Billing.
    if set(tables) & BILLING_TABLES:
        return _deny(
            "billing is never touched by this capability — no Stripe, no invoicing, "
            "no plan/tier, no payment methods, for any client, ever",
            TRIGGER_BILLING,
        )

    # 4. Secrets / tokens / auth.
    if set(tables) & SECRET_TABLES:
        return _deny("secrets, tokens and auth config are foundational", TRIGGER_SECRETS)

    # 5. Identity / shared system tables.
    if set(tables) & IDENTITY_TABLES:
        return _deny(
            "gyms/app_users/gym_assignments and the shared settings tables are not "
            "one client's own data",
            TRIGGER_MULTI_GYM_CONFIG,
        )

    # 6. Schema / migrations / RLS, however the action spells them.
    for op in sql_ops:
        for bad in SCHEMA_OPS:
            if bad in op:
                return _deny(f"schema/RLS operation {op!r} is foundational", TRIGGER_SCHEMA)

    # 7. Feature flags / env.
    if action.touches_env_or_flag:
        return _deny(
            "a feature flag or env var is shared config; changing one affects more "
            "than this gym",
            TRIGGER_MULTI_GYM_CONFIG,
        )

    # 8. Blocked repo paths, whatever the kind claims to be.
    for p in paths:
        for frag in BLOCKED_PATH_FRAGMENTS:
            if frag in p:
                # Name the RIGHT line. A gym's own voice doc is not "config affecting
                # more than one gym" -- it is that client's own authored copy, which
                # Echo may never write (CLAUDE.md: no invented facts, offers or stats).
                # The card should say which rule was hit.
                trigger = (
                    TRIGGER_SCHEMA if "migrations/" in frag
                    else TRIGGER_SECRETS if frag in (".env", "secrets", "credentials")
                    else TRIGGER_CLIENT_AUTHORED_CONTENT if frag == "brand_voice/"
                    else TRIGGER_MULTI_GYM_CONFIG
                )
                return _deny(
                    f"{p!r} is not one client's own data ({frag!r} is shared "
                    f"foundation)",
                    trigger,
                )

    # ---- ALLOWLIST -------------------------------------------------------
    if kind not in ALLOWED_ACTION_KINDS:
        return _deny(
            f"action kind {kind!r} is not one of the enumerated client-scoped "
            f"kinds {sorted(ALLOWED_ACTION_KINDS)}",
            TRIGGER_UNKNOWN_SHAPE,
        )

    if kind in NON_WRITING_KINDS:
        # Nothing is written, so there is nothing to scope. Still refuse if the
        # caller contradicted itself by naming tables or paths.
        if tables or paths:
            return _deny(
                f"{kind!r} must write nothing, but names "
                f"tables={tables} paths={paths}",
                TRIGGER_UNKNOWN_SHAPE,
            )
        return ScopeVerdict(True, f"{kind}: no write, nothing to scope")

    if kind == KIND_CODE_FIX:
        if not paths:
            return _deny(
                "a code fix must name the files it would change; an unnamed change "
                "cannot be scope-checked",
                TRIGGER_UNKNOWN_SHAPE,
            )
        for p in paths:
            # PREFIX ONLY. `... or f"/{root}" in p` made this a substring test, so
            # any path merely CONTAINING an allowed root passed: vendor/tests/evil.py,
            # scripts/tests/deploy_key.py, docs/agent/media_x.py. An allowlist that
            # matches mid-path is not an allowlist. Paths are already normalised, so a
            # prefix test is exact.
            if not any(p.startswith(root) for root in ALLOWED_CODE_FIX_ROOTS):
                return _deny(
                    f"code fix would touch {p!r}, which is outside the allowed "
                    f"roots {ALLOWED_CODE_FIX_ROOTS}",
                    TRIGGER_MULTI_GYM_CONFIG,
                )
        # A code fix still has to say which single gym's bug it is, so the reply and
        # the verification are scoped the same way every other remedy is.
        return _scope_check(action, kind, extra="code fix within allowed roots")

    # per_gym_sync / data_patch: must name known tables and exactly one gym.
    if not tables:
        return _deny(
            f"{kind!r} writes, but names no table; an unnamed write cannot be "
            f"scope-checked",
            TRIGGER_UNKNOWN_SHAPE,
        )
    unknown = [t for t in tables if t not in ALLOWED_DATA_TABLES]
    if unknown:
        return _deny(
            f"table(s) {unknown} are not in the client-scoped allowlist "
            f"{sorted(ALLOWED_DATA_TABLES)}",
            TRIGGER_UNKNOWN_SHAPE,
        )
    return _scope_check(action, kind)


def _scope_check(action, kind, extra=""):
    """Exactly one named gym, on a known scope column. This is the
    'another client's data' trigger, mechanically."""
    col = str(action.scope_column or "").strip().lower()
    if col not in ALLOWED_SCOPE_COLUMNS:
        return _deny(
            f"{kind!r} has scope column {col!r}; a write with no recognised "
            f"gym-scope column could reach any gym's rows",
            TRIGGER_OTHER_CLIENT,
        )
    values = tuple(v for v in (str(x or "").strip() for x in action.scope_values) if v)
    if len(values) != 1:
        return _deny(
            f"{kind!r} names {len(values)} gym scope value(s); an action may affect "
            f"exactly one gym",
            TRIGGER_OTHER_CLIENT,
        )
    tail = f" ({extra})" if extra else ""
    return ScopeVerdict(
        True,
        f"{kind}: client-scoped to {col}={values[0]!r}{tail}",
        meta={"scope_column": col, "scope_value": values[0]},
    )
