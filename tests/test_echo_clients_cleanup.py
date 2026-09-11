"""
agent/echo_clients_cleanup.py -- archive what the 2026-09-11 fleet sweep created for
non-clients, and ONLY that. Dry run by default; refuses on an unreadable universe;
never deletes a client, a hardcoded base, or anything the shared plane does not know.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import echo_clients as ec  # noqa: E402
from agent import echo_clients_cleanup as ecc  # noqa: E402

TOUGH = "0f0f0f0f-0000-4000-8000-000000000001"
LOCAL = "0f0f0f0f-0000-4000-8000-000000000002"
BOOM = "0f0f0f0f-0000-4000-8000-000000000003"
LEAD = "0f0f0f0f-0000-4000-8000-000000000004"

UNIVERSE = ec.build(
    [{"gym_id": TOUGH}, {"gym_id": LOCAL}],
    [{"gym_id": TOUGH, "echo_account_key": "toughtemple52040e"},
     {"gym_id": LOCAL, "echo_account_key": "crossfitlocal"},
     {"gym_id": BOOM, "echo_account_key": "boomfitbcs3b4da7"},
     {"gym_id": LEAD, "echo_account_key": "deanholcomb9ebee0"}],
    [{"id": TOUGH, "name": "Tough Temple", "slug": "tough-temple"},
     {"id": LOCAL, "name": "CrossFit Local", "slug": "crossfit-local"},
     {"id": BOOM, "name": "BoomFit BCS", "slug": "boomfit-bcs"},
     {"id": LEAD, "name": "Dean Holcomb", "slug": "dean-holcomb"}])

REGISTRY = [
    {"base": "toughtemple086f51", "name": "Tough Temple", "gym_id": TOUGH},   # split key, id vouches
    {"base": "boomfitbcs3b4da7", "name": "BoomFit BCS", "gym_id": BOOM},      # the incident
    {"base": "deanholcomb9ebee0", "name": "Dean Holcomb", "gym_id": LEAD},    # a person, not a gym
    {"base": "crossfitlocal", "name": "CrossFit Local"},                      # key vouches
    {"base": "mysterygym", "name": "Mystery"},                                # plane never saw it
]
SHARED = [{"account_key": "boomfitbcs3b4da7", "display_name": "BoomFit BCS",
           "created_at": "2026-09-10T19:13:15Z", "updated_at": "2026-09-10T19:13:15Z"},
          {"account_key": "crossfitlocal", "display_name": None},
          {"account_key": "districth", "display_name": None},       # legacy client alias
          {"account_key": "eng", "display_name": "ENG"}]            # hardcoded
LOCAL_ROWS = [{"account_key": "deanholcomb9ebee0", "display_name": "Dean Holcomb",
               "created_at": "2026-09-11 12:05:00"},
              {"account_key": "toughtemple52040e", "display_name": "Tough Temple"}]


@pytest.fixture
def world(tmp_path):
    voice = tmp_path / "brand_voice"
    lib = tmp_path / "content_library"
    for d in ("toughtemple52040e", "boomfitbcs3b4da7", "deanholcomb9ebee0",
              "toughtemple0f0f0f", "gym_alpha", ".hidden", "_trash"):
        (voice / d).mkdir(parents=True)
        (voice / d / "lasso_voice.md").write_text("# bible")
    (voice / "lasso_voice.md").write_text("LASSO's own bible file, not a dir")
    for d in ("crossfitreverb30b5b2", "boomfitbcs3b4da7", "book_campaign", "eng"):
        (lib / d).mkdir(parents=True)
        (lib / d / "photo.jpg").write_bytes(b"x")
    (lib / "lasso_card_1_final.png").write_bytes(b"x")
    reg = tmp_path / "gym_accounts.json"
    reg.write_text(json.dumps(REGISTRY, indent=2))
    return {"voice": str(voice), "lib": str(lib), "reg": str(reg), "root": tmp_path}


_REAL_PLAN = ecc.plan    # the main() tests monkeypatch ecc.plan; never recurse into the fake


def _plan(world, **kw):
    return _REAL_PLAN(universe=UNIVERSE, registry_rows=REGISTRY, registry_path=world["reg"],
                      voice_root=world["voice"], library_root=world["lib"],
                      shared_rows=SHARED, local_rows=LOCAL_ROWS, **kw)


def _verdicts(result, kind):
    return {it["key"]: it["verdict"] for it in result["items"] if it["kind"] == kind}


def test_registry_rows_are_judged_by_gym_id_then_key(world):
    v = _verdicts(_plan(world), "registry_row")
    assert v == {"toughtemple086f51": "keep",          # gym_id is a client
                 "boomfitbcs3b4da7": "remove",         # a known non-client gym
                 "deanholcomb9ebee0": "remove",
                 "crossfitlocal": "keep",              # key is a client alias
                 "mysterygym": "unknown"}              # the plane never carried it


def test_directories_only_remove_known_non_client_keys(world):
    r = _plan(world)
    assert _verdicts(r, "brand_voice") == {
        "toughtemple52040e": "keep", "boomfitbcs3b4da7": "remove",
        "deanholcomb9ebee0": "remove", "toughtemple0f0f0f": "keep",   # portal derivation
        "gym_alpha": "unknown"}                                          # never touched
    assert _verdicts(r, "content_lib") == {
        "crossfitreverb30b5b2": "unknown",   # a real client key the FIXTURE plane lacks
        "boomfitbcs3b4da7": "remove", "book_campaign": "unknown", "eng": "keep"}
    # hidden / underscore dirs and loose files at the root are not even listed
    keys = {it["key"] for it in r["items"] if it["kind"] in ("brand_voice", "content_lib")}
    assert not ({".hidden", "_trash", "lasso_voice.md", "lasso_card_1_final.png"} & keys)


def test_shared_and_local_rows(world):
    r = _plan(world)
    assert _verdicts(r, "echo_gyms") == {"boomfitbcs3b4da7": "remove", "crossfitlocal": "keep",
                                         "districth": "unknown", "eng": "keep"}
    assert _verdicts(r, "local_gyms") == {"deanholcomb9ebee0": "remove",
                                          "toughtemple52040e": "keep"}


def test_keep_flag_protects_a_base(world):
    v = _verdicts(_plan(world, keep=("boomfitbcs3b4da7",)), "registry_row")
    assert v["boomfitbcs3b4da7"] == "keep"


def test_hardcoded_bases_are_never_removable():
    hard = ec.hardcoded_bases()
    for base in ("lasso", "district_h", "eng", "gritx", "topfuel", "blake_personal"):
        assert ecc.decide(base, "", UNIVERSE, hard=hard, keep=set())[0] == "keep"


def test_refuses_on_an_unreadable_universe(world):
    bad = ec.ClientSet(ok=False, error="down")
    r = ecc.plan(universe=bad, registry_rows=REGISTRY, voice_root=world["voice"],
                 library_root=world["lib"], shared_rows=SHARED, local_rows=LOCAL_ROWS)
    assert r["ok"] is False and r["items"] == []
    assert "REFUSED" in ecc.format_table(r)
    with pytest.raises(RuntimeError):
        ecc.apply(r, trash_root=str(world["root"] / "t"))


def test_dry_run_table_shape(world):
    text = ecc.format_table(_plan(world))
    head = text.splitlines()
    assert head[0].startswith("Echo client universe: 2 gyms")
    assert "kind" in head[2] and "verdict" in head[2] and "reason" in head[2]
    assert "|" not in text                         # plain aligned text, no markdown table
    assert "remove 7 (brand_voice 2, content_lib 1, echo_gyms 1, local_gyms 1, registry_row 2)" in text
    assert "unknown/left alone 5" in text


class _Store:
    def __init__(self):
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)
        return True


def test_apply_archives_then_removes_only_the_remove_rows(world, monkeypatch):
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", world["reg"])
    r = _plan(world)
    store = _Store()
    local_deleted = []
    trash = str(world["root"] / "_trash" / "2026-09-11")
    out = ecc.apply(r, trash_root=trash, store=store, delete_local=local_deleted.append)
    assert out["errors"] == []
    # directories MOVED, not deleted
    assert not os.path.exists(os.path.join(world["voice"], "boomfitbcs3b4da7"))
    assert os.path.exists(os.path.join(trash, "brand_voice", "boomfitbcs3b4da7", "lasso_voice.md"))
    assert os.path.exists(os.path.join(trash, "brand_voice", "deanholcomb9ebee0"))
    assert os.path.exists(os.path.join(trash, "content_lib", "boomfitbcs3b4da7", "photo.jpg"))
    # kept + unknown untouched
    for d in ("toughtemple52040e", "toughtemple0f0f0f", "gym_alpha"):
        assert os.path.isdir(os.path.join(world["voice"], d))
    for d in ("crossfitreverb30b5b2", "book_campaign", "eng"):
        assert os.path.isdir(os.path.join(world["lib"], d))
    # registry rewritten with only the kept/unknown rows, archive + .bak beside it
    left = [row["base"] for row in json.load(open(world["reg"]))]
    assert left == ["toughtemple086f51", "crossfitlocal", "mysterygym"]
    assert sorted(out["registry_removed"]) == ["boomfitbcs3b4da7", "deanholcomb9ebee0"]
    removed = json.load(open(os.path.join(trash, "gym_accounts.removed.json")))
    assert sorted(row["base"] for row in removed) == ["boomfitbcs3b4da7", "deanholcomb9ebee0"]
    assert any(f.startswith("gym_accounts.json.bak-") for f in os.listdir(trash))
    # shared + local rows archived as JSON before the delete
    assert store.deleted == ["boomfitbcs3b4da7"]
    assert os.path.exists(os.path.join(trash, "echo_gyms", "boomfitbcs3b4da7.json"))
    assert local_deleted == ["deanholcomb9ebee0"]
    assert os.path.exists(os.path.join(trash, "gyms_local", "deanholcomb9ebee0.json"))


def test_apply_one_failure_never_stops_the_rest(world, monkeypatch):
    monkeypatch.setenv("AGENT_GYM_REGISTRY_PATH", world["reg"])

    class _Boom(_Store):
        def delete(self, key):
            raise RuntimeError("supabase down")
    r = _plan(world)
    out = ecc.apply(r, trash_root=str(world["root"] / "t"), store=_Boom(),
                    delete_local=lambda k: None)
    assert any("echo_gyms boomfitbcs3b4da7" in e for e in out["errors"])
    assert sorted(out["registry_removed"]) == ["boomfitbcs3b4da7", "deanholcomb9ebee0"]
    assert out["local_deleted"] == ["deanholcomb9ebee0"]


def test_main_dry_run_is_the_default(world, monkeypatch, capsys):
    def _fake_plan(**kw):
        return _plan(world, **kw)
    monkeypatch.setattr(ecc, "plan", _fake_plan)
    moved = []
    monkeypatch.setattr(ecc, "apply", lambda *a, **k: moved.append(1))
    assert ecc.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "--apply" in out
    assert moved == []


def test_main_refuses_with_exit_2_when_the_universe_is_unreadable(real_echo_clients, monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert ecc.main(["--apply"]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_main_apply_calls_apply_with_keep(world, monkeypatch, capsys):
    seen = {}

    def _fake_plan(**kw):
        seen["kw"] = kw
        return _plan(world, **kw)
    monkeypatch.setattr(ecc, "plan", _fake_plan)
    monkeypatch.setattr(ecc, "apply", lambda r, **k: {"trash": "/t", "moved": [], "registry_removed": [],
                                                      "echo_gyms_deleted": [], "local_deleted": [],
                                                      "errors": []})
    assert ecc.main(["--apply", "--keep", "a,b"]) == 0
    assert seen["kw"]["keep"] == ("a", "b")
    assert "APPLIED" in capsys.readouterr().out


def test_cli_dispatch_exists():
    import inspect
    import agent.__main__ as mm
    src = inspect.getsource(mm.main)
    assert 'cmd == "echo-clients-cleanup"' in src and 'cmd == "echo-clients"' in src
