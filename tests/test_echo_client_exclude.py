from agent import echo_clients as ec
from agent import calendar_autopublish as ca


def _set():
    return ec.ClientSet(ok=True, gym_ids=frozenset({"g1", "g2"}),
                        keys=frozenset({"gritx", "eng"}),
                        key_to_gym={"gritx": "g1", "eng": "g2"})


def test_exclusion_drops_gym_by_key_and_uuid(monkeypatch):
    monkeypatch.setenv(ec.EXCLUDE_ENV, "gritx")
    s = ec.apply_exclusions(_set())
    assert not s.is_client("gritx") and not s.is_client("gritx_ig") and not s.is_client("g1")
    assert s.is_client("eng") and s.is_client("g2")


def test_no_env_is_noop(monkeypatch):
    monkeypatch.delenv(ec.EXCLUDE_ENV, raising=False)
    assert ec.apply_exclusions(_set()) == _set()


def test_hardcoded_gritx_excluded_from_client_bases(monkeypatch):
    monkeypatch.setenv(ec.EXCLUDE_ENV, "gritx")
    monkeypatch.setattr(ec, "is_echo_client", lambda *a, **k: True)
    assert "gritx" not in ec.hardcoded_bases()
    assert not ec.is_client_base("gritx")
    assert not ec.is_client_base("gritx_ig")
    assert "gritx" not in ca.client_gym_bases()
    assert "eng" in ca.client_gym_bases()
