"""Static SQL-shape checks for the DRAFT media group migration plus a
pure-python model of the RPC reservation semantics.

The migration is DRAFT-only (not applied anywhere), so these tests never touch
a database: they assert the hardening invariants textually (RLS, grants,
pinned search_path, terminal-status guards) and exercise the claim/publish/
release state machine through a python model that mirrors the PL/pgSQL logic
in migrations/DRAFT_media_group_usage_20261002.sql line for line.
"""
from pathlib import Path

SQL = (
    Path(__file__).parents[1]
    / "migrations"
    / "DRAFT_media_group_usage_20261002.sql"
).read_text()


def _normalized():
    return " ".join(SQL.lower().split())


# --------------------------------------------------------------------------
# Static SQL-shape checks
# --------------------------------------------------------------------------

def test_draft_file_is_marked_not_applied():
    head = SQL.splitlines()[:3]
    assert any("DRAFT" in line and "NOT APPLIED" in line for line in head)


def test_tables_are_rls_enabled_and_service_role_only():
    normalized = _normalized()
    assert "alter table public.media_group enable row level security" in normalized
    assert "alter table public.media_group_usage enable row level security" in normalized
    assert "revoke all on table public.media_group from public, anon, authenticated" in normalized
    assert "revoke all on table public.media_group_usage from public, anon, authenticated" in normalized
    assert "grant select on table public.media_group to service_role" in normalized
    assert "revoke insert, update, delete on table public.media_group from service_role" in normalized
    # No browser-role write path, no permissive policies.
    assert "create policy" not in normalized
    assert "to anon" not in normalized.replace("from public, anon, authenticated", "")
    assert "grant insert" not in normalized
    assert "grant update" not in normalized


def test_rpcs_are_security_definer_with_pinned_search_path():
    normalized = _normalized()
    for fn in (
        "claim_media_group",
        "publish_media_group_usage",
        "release_media_group_reservation",
        "hold_media_group",
        "media_group_usage_guard",
        "media_group_status_guard",
    ):
        assert f"create or replace function public.{fn}" in normalized
    # Every function body is defined with a pinned search_path.
    assert normalized.count("set search_path = pg_catalog, public") >= 6
    assert "security definer" in normalized
    # Execute is granted only to service_role and revoked from browser roles.
    assert normalized.count("grant execute on function public.") == 4
    assert normalized.count("to service_role") >= 4
    assert "from public, anon, authenticated" in normalized


def test_claim_rpc_serializes_on_group_row_and_refuses_cross_date():
    normalized = _normalized()
    assert "for update of g" in normalized
    assert "u.status = 'reserved' and u.usage_date <> p_usage_date" in normalized
    assert "on conflict on constraint media_group_usage_pkey do nothing" in normalized
    # Published permanence: a published group refuses any new date.
    assert "u.status = 'published'" in normalized
    assert "held_used" in normalized
    assert "held_conflict" in normalized
    assert "held_terminal" in normalized
    assert "missing_group" in normalized


def test_terminal_guards_block_recycling_and_published_deletion():
    normalized = _normalized()
    assert "old.status in ('exhausted', 'unknown', 'retired') and new.status <> old.status" in normalized
    assert "published media group usage is permanent and cannot be removed" in normalized
    assert "published media group usage cannot be reopened" in normalized
    assert "before update or delete on public.media_group_usage" in normalized
    # Foreign key pins usage rows to a group and forbids group deletion.
    assert "references public.media_group (gym_id, group_key)" in normalized
    assert "on delete restrict" in normalized


def test_group_key_and_tenant_are_non_blank():
    normalized = _normalized()
    assert "check (nullif(btrim(gym_id), '') is not null)" in normalized
    assert "check (nullif(btrim(group_key), '') is not null)" in normalized
    assert "p_gym_id <> btrim(p_gym_id)" in normalized
    assert "primary key (gym_id, group_key, usage_date, sibling_id)" in normalized


# --------------------------------------------------------------------------
# Pure-python model of the RPC semantics
# --------------------------------------------------------------------------

class GroupStore:
    """Mirrors media_group + media_group_usage and the RPC state machine."""

    TERMINAL = {"exhausted", "unknown", "retired"}

    def __init__(self):
        # (gym_id, group_key) -> status
        self.groups = {}
        # (gym_id, group_key, usage_date, sibling_id) -> status
        self.usage = {}

    def add_group(self, gym, key, status="active"):
        self.groups[(gym, key)] = status

    def claim(self, gym, key, date, sibling, channel):
        """Model of public.claim_media_group."""
        assert channel in ("ig", "fb", "story", "gbp")
        status = self.groups.get((gym, key))
        if status is None:
            return "missing_group"
        if status != "active":
            return "held_terminal"
        rows = [
            (d, s, st)
            for (g, k, d, s), st in self.usage.items()
            if g == gym and k == key
        ]
        published_exists = any(st == "published" for _, _, st in rows)
        other_date_reserved = any(
            st == "reserved" and d != date for d, _, st in rows
        )
        same_date_exists = any(d == date for d, _, _ in rows)
        if published_exists and not same_date_exists:
            return "held_used"
        if other_date_reserved:
            return "held_conflict"
        self.usage.setdefault((gym, key, date, sibling), "reserved")
        return "shared" if same_date_exists else "claimed"

    def publish(self, gym, key, date, sibling=None):
        """Model of public.publish_media_group_usage; returns rows changed."""
        count = 0
        for (g, k, d, s), st in list(self.usage.items()):
            if (
                g == gym and k == key and d == date and st == "reserved"
                and (sibling is None or s == sibling)
            ):
                self.usage[(g, k, d, s)] = "published"
                count += 1
        return count

    def release(self, gym, key, date, sibling):
        """Model of public.release_media_group_reservation; returns remaining."""
        row = (gym, key, date, sibling)
        if self.usage.get(row) == "reserved":
            del self.usage[row]
        elif self.usage.get(row) == "published":
            raise AssertionError("published rows cannot be released")
        return sum(1 for (g, k, *_rest) in self.usage if g == gym and k == key)


GYM = "swift_river"
GROUP = "mg_deadbeef01"
D1, D2 = "2026-10-07", "2026-10-08"


def test_same_date_siblings_share_one_group():
    store = GroupStore()
    store.add_group(GYM, GROUP)
    assert store.claim(GYM, GROUP, D1, "row-ig", "ig") == "claimed"
    assert store.claim(GYM, GROUP, D1, "row-fb", "fb") == "shared"
    assert store.claim(GYM, GROUP, D1, "row-story", "story") == "shared"
    assert store.claim(GYM, GROUP, D1, "row-gbp", "gbp") == "shared"
    # Idempotent re-claim by the same sibling.
    assert store.claim(GYM, GROUP, D1, "row-ig", "ig") == "shared"
    assert len(store.usage) == 4


def test_cross_date_claim_is_blocked_while_reserved():
    store = GroupStore()
    store.add_group(GYM, GROUP)
    assert store.claim(GYM, GROUP, D1, "row-ig", "ig") == "claimed"
    assert store.claim(GYM, GROUP, D2, "row-ig-2", "ig") == "held_conflict"
    assert len(store.usage) == 1


def test_publish_makes_group_permanently_used_on_other_dates():
    store = GroupStore()
    store.add_group(GYM, GROUP)
    store.claim(GYM, GROUP, D1, "row-ig", "ig")
    store.claim(GYM, GROUP, D1, "row-fb", "fb")
    assert store.publish(GYM, GROUP, D1) == 2
    # Any other date refuses forever, even after sibling rows exist only there.
    assert store.claim(GYM, GROUP, D2, "row-ig-2", "ig") == "held_used"
    assert store.claim(GYM, GROUP, "2026-12-31", "row-x", "ig") == "held_used"


def test_same_date_sibling_may_still_join_after_publish():
    store = GroupStore()
    store.add_group(GYM, GROUP)
    store.claim(GYM, GROUP, D1, "row-ig", "ig")
    store.publish(GYM, GROUP, D1, "row-ig")
    # Late same-date sibling of an already-published set shares the group.
    assert store.claim(GYM, GROUP, D1, "row-story", "story") == "shared"


def test_release_only_after_last_active_sibling_removed():
    store = GroupStore()
    store.add_group(GYM, GROUP)
    store.claim(GYM, GROUP, D1, "row-ig", "ig")
    store.claim(GYM, GROUP, D1, "row-fb", "fb")
    # Releasing one sibling leaves the group still bound to D1.
    assert store.release(GYM, GROUP, D1, "row-ig") == 1
    assert store.claim(GYM, GROUP, D2, "row-2", "ig") == "held_conflict"
    # Releasing the last active sibling frees the group for another date.
    assert store.release(GYM, GROUP, D1, "row-fb") == 0
    assert store.claim(GYM, GROUP, D2, "row-2", "ig") == "claimed"


def test_published_reservation_cannot_be_released():
    store = GroupStore()
    store.add_group(GYM, GROUP)
    store.claim(GYM, GROUP, D1, "row-ig", "ig")
    store.publish(GYM, GROUP, D1, "row-ig")
    try:
        store.release(GYM, GROUP, D1, "row-ig")
    except AssertionError:
        pass
    else:
        raise AssertionError("published row must not be releasable")


def test_exhausted_and_unknown_hold_visibly_and_never_recycle():
    store = GroupStore()
    store.add_group(GYM, "mg_exhausted", status="exhausted")
    store.add_group(GYM, "mg_unknown", status="unknown")
    assert store.claim(GYM, "mg_exhausted", D1, "r1", "ig") == "held_terminal"
    assert store.claim(GYM, "mg_unknown", D1, "r2", "ig") == "held_terminal"
    # Unknown identity with no group row at all also fails closed.
    assert store.claim(GYM, "mg_never_seen", D1, "r3", "ig") == "missing_group"
    # No rows leaked into the ledger.
    assert store.usage == {}


def test_tenant_isolation_between_gyms():
    store = GroupStore()
    store.add_group("gym_a", GROUP)
    store.claim("gym_a", GROUP, D1, "a-ig", "ig")
    # A different tenant has no such group; it never sees gym_a's state.
    assert store.claim("gym_b", GROUP, D1, "b-ig", "ig") == "missing_group"
    store.add_group("gym_b", GROUP)
    # Same group_key under a different tenant is an independent group.
    assert store.claim("gym_b", GROUP, D2, "b-ig", "ig") == "claimed"
