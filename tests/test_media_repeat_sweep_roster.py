from types import SimpleNamespace

from agent import echo_clients
from agent.jobs import media_repeat_sweep as mrs


def _client_set(*, ok=True):
    return SimpleNamespace(
        ok=ok,
        gym_ids=frozenset({"gym-a", "gym-b"}),
        key_to_gym={
            "registered_a": "gym-a",
            "portal_a": "gym-a",
            "token_b": "gym-b",
            "token_b_alt": "gym-b",
            "token_nonclient": "other-gym",
        },
        ambiguous_keys=frozenset(),
        token_keys_by_gym={
            "gym-a": frozenset({"portal_a", "registered_a"}),
            "gym-b": frozenset({"token_b", "token_b_alt"}),
        },
    )


def test_sweep_roster_adds_unregistered_echo_client_and_dedupes_aliases():
    roster = mrs._client_sweep_bases(
        ["registered_a", "portal_a"], _client_set())

    assert roster == ["registered_a", "portal_a", "token_b", "token_b_alt"]
    assert "token_nonclient" not in roster


def test_sweep_roster_skips_ambiguous_token_alias():
    clients = _client_set()
    clients.ambiguous_keys = frozenset(
        {"portal_a", "registered_a", "token_b", "token_b_alt"})

    assert mrs._client_sweep_bases([], clients) == []


def test_sweep_roster_drops_ambiguous_registered_alias():
    clients = _client_set()
    clients.ambiguous_keys = frozenset({"registered_a"})

    assert "registered_a" not in mrs._client_sweep_bases(["registered_a"], clients)


def test_sweep_roster_preserves_registered_roster_if_client_read_fails():
    assert mrs._client_sweep_bases(["registered_a"], _client_set(ok=False)) == [
        "registered_a"
    ]


def test_default_run_uses_expanded_roster_and_keeps_lasso(monkeypatch):
    seen = []
    monkeypatch.setattr(mrs, "SupabaseCalendarStore", lambda: object())
    monkeypatch.setattr(mrs, "_sweep_roster", lambda: ["registered_a", "token_b"])
    monkeypatch.setattr(
        mrs, "sweep_gym",
        lambda base, store, **kwargs: seen.append(base) or {
            "gym": base, "photos_repeated": 0, "dates_fixed": 0,
            "rows_repointed": 0, "stories_reburned": 0,
            "approved_left": 0, "small_library": False,
        })

    mrs.run([], apply=False)

    assert seen == ["registered_a", "token_b", "lasso"]


def test_echo_snapshot_failure_does_not_add_token_only_candidates(monkeypatch):
    monkeypatch.setattr("agent.calendar_autopublish.client_gym_bases",
                        lambda: ["registered_a"])
    monkeypatch.setattr(echo_clients, "snapshot",
                        lambda **kwargs: _client_set(ok=False))

    assert mrs._sweep_roster() == ["registered_a"]
