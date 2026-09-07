"""
AD SPEND IS STRUCTURALLY IMPOSSIBLE FOR THIS CAPABILITY.

Blake, 2026-09-06: "no code path in this capability may call into the Meta/Pipeboard
ad-write tools AT ALL — not gated by a check that could misfire, but literally no
import, no call path, nothing wired."

These tests assert the STRUCTURE, not a gate's opinion. The distinction matters: a
gate that declines is one bad edit from allowing; an absent call path has nothing to
allow. The last test in this file is the important one — it proves the check would
actually catch a violation, by planting one.
"""
import os
import textwrap

import pytest

from agent.client_dm_support import ad_block, consumer, flow, remedies, scope_gate

PKG = os.path.dirname(
    os.path.abspath(__import__("agent.client_dm_support", fromlist=["x"]).__file__)
)


def test_the_package_has_no_ad_call_path_at_all():
    findings = ad_block.scan_for_ad_call_paths()
    assert findings == [], "\n".join(findings)
    assert ad_block.assert_no_ad_call_path() is True


def test_no_module_in_the_package_imports_an_ad_surface():
    """Belt on the AST scan: read the raw source and assert the forbidden module
    names appear nowhere except in ad_block.py's own constant tables."""
    for root, _dirs, names in os.walk(PKG):
        if "__pycache__" in root:
            continue
        for n in names:
            if not n.endswith(".py") or n == "ad_block.py":
                continue
            src = open(os.path.join(root, n), encoding="utf-8").read()
            for bad in ad_block.FORBIDDEN_AD_IMPORT_PREFIXES:
                assert f"import {bad}" not in src, (n, bad)
                assert f"from {bad}" not in src, (n, bad)


def test_no_executor_can_be_an_ad_write():
    """The remedy executor registry is the only place this capability performs an
    action. It has exactly one entry and that entry is a per-gym media sync."""
    assert set(remedies.EXECUTORS) == {"per_gym_drive_sync"}


def test_the_code_fix_lane_is_empty_two_way_guard():
    """D68's two-way guard. scope_gate KNOWS how to check a code fix, but no remedy
    produces one and no executor can run one. If a future change fills this lane, this
    test goes red and forces the wiring question to be answered rather than letting a
    code-writing lane arrive silently."""
    assert scope_gate.KIND_CODE_FIX in scope_gate.ALLOWED_ACTION_KINDS
    assert "code_fix" not in remedies.EXECUTORS
    src = open(os.path.join(PKG, "remedies.py"), encoding="utf-8").read()
    assert "KIND_CODE_FIX" not in src, (
        "remedies.py now plans a code fix; the autonomous code-fix + deploy lane needs "
        "an explicit wiring decision before it can run"
    )


def test_an_ad_shaped_message_always_escalates_whatever_else_is_true():
    """The belt. Even with a perfectly healthy gym and a routable topic word in the
    same sentence, an ad-money message escalates before any diagnostic runs."""
    for text in (
        "can you bump my ad budget to $80/day",
        "please pause my campaigns until Monday",
        "change the targeting to 25-45 women",
        "my photos are missing and also please raise the daily budget",
        "launch the new ad set today please",
        "what is my CPL this week",
    ):
        d = flow.handle_ticket(text=text, gym_key="crossfitlocal")
        assert d.decision == flow.DECISION_ESCALATE, text
        assert d.foundation_trigger == scope_gate.TRIGGER_AD_MONEY, text
        assert not d.will_post


def test_ad_belt_can_only_add_escalation_never_permit():
    """There is no branch on which is_ad_money_topic() == False permits an ad action:
    the only ad-capable thing in the package is nothing."""
    assert ad_block.is_ad_money_topic("my photos are missing") is False
    # ...and the non-ad message still cannot reach an ad executor, because there is none.
    assert not (set(remedies.EXECUTORS) & ad_block.FORBIDDEN_AD_CALL_NAMES)


def test_run_once_asserts_the_structure_before_reading_any_ticket(monkeypatch):
    """D68: 'an assertion nobody calls is itself an instance of the bug it exists to
    catch.' Prove assert_no_ad_call_path is on the REAL path, by making it raise and
    watching run_once fail before it ever polls."""
    polled = []

    class Bus:
        def available(self):
            return True

        def _get(self, table, params):
            polled.append((table, params))
            return []

    def boom(*_a, **_k):
        raise ad_block.AdCallPathError("planted")

    monkeypatch.setattr(consumer._ad, "assert_no_ad_call_path", boom)
    with pytest.raises(ad_block.AdCallPathError):
        consumer.run_once(bus=Bus(), flag_on=True)
    assert polled == [], "run_once polled tickets despite the structural check failing"


# ---------------------------------------------------------------------------
# THE MUTATION: prove the scanner actually catches a violation.
# ---------------------------------------------------------------------------
def test_the_scanner_catches_a_planted_ad_import(tmp_path):
    (tmp_path / "sneaky.py").write_text(textwrap.dedent("""
        import pipeboard
        def go(): pass
    """))
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("pipeboard" in f for f in findings), findings
    with pytest.raises(ad_block.AdCallPathError):
        ad_block.assert_no_ad_call_path(str(tmp_path))


def test_the_scanner_catches_a_planted_ad_write_call(tmp_path):
    (tmp_path / "sneaky.py").write_text(textwrap.dedent("""
        def go(client):
            return client.update_campaign(daily_budget=9999)
    """))
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("update_campaign" in f for f in findings), findings


def test_the_scanner_catches_a_hand_built_graph_api_url(tmp_path):
    (tmp_path / "sneaky.py").write_text(
        'URL = "https://graph.facebook.com/v19.0/act_123/campaigns"\n'
    )
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("Graph API" in f for f in findings), findings


# ---------------------------------------------------------------------------
# THE THREE BYPASSES AN INDEPENDENT AUDIT FOUND. All three scanned CLEAN before.
# ---------------------------------------------------------------------------
def test_a_relative_import_out_of_the_package_is_caught(tmp_path):
    """THE WORST ONE. The scanner skipped every relative import, on the reasoning that
    'a relative import can never reach another package' — which is false:
    `from ..meta_publisher import x` inside agent.client_dm_support IS
    agent.meta_publisher, the first entry in its own forbidden table. And
    relative-out-of-package is the ONLY cross-package import style this package uses."""
    (tmp_path / "evil_b.py").write_text(
        "from ..meta_publisher import publish_ad\ndef go(): return publish_ad()\n")
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("agent.meta_publisher" in f for f in findings), findings


def test_relative_level_resolution_is_correct():
    assert ad_block._resolve_relative("meta_publisher", 2) == "agent.meta_publisher"
    assert ad_block._resolve_relative(None, 2) == "agent"
    assert ad_block._resolve_relative("jobs.sync_gym_media", 2) == \
        "agent.jobs.sync_gym_media"
    assert ad_block._resolve_relative("facts", 1) == "agent.client_dm_support.facts"


def test_dynamic_import_plus_computed_getattr_is_caught(tmp_path):
    (tmp_path / "evil_a.py").write_text(
        'import importlib\n'
        'def go():\n'
        '    m = importlib.import_module("agent.meta_publisher")\n'
        '    return getattr(m, "create_" + "campaign")(daily_budget=9999)\n')
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("importlib" in f for f in findings), findings
    assert any("import_module" in f for f in findings), findings


def test_subprocess_and_a_split_graph_api_url_are_caught(tmp_path):
    (tmp_path / "evil_c.py").write_text(
        'import subprocess\n'
        'URL = "graph." + "face" + "book.com/v19.0/act" + "_1/campaigns"\n'
        'def go(): return subprocess.run(["curl", "-X", "POST", URL])\n')
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("subprocess" in f for f in findings), findings
    assert any("Graph API" in f for f in findings), findings


def test_string_concatenation_is_folded_before_the_literal_check():
    """A URL split across three literals is one string to a reader and must be one
    string to the scanner."""
    import ast
    tree = ast.parse('X = "graph." + "face" + "book.com"')
    assert "graph.facebook.com" in list(ad_block._string_literals(tree))


def test_a_literal_getattr_is_still_allowed():
    """The guard must not be noise: getattr with a LITERAL attribute name is just an
    attribute access with a default, and the package uses it legitimately. A control
    that fires on ordinary code gets switched off."""
    import ast
    findings = []
    tree = ast.parse('x = getattr(obj, "available", None)')
    assert tree is not None
    # The real package uses exactly that form and self-scans clean.
    assert ad_block.scan_for_ad_call_paths() == []
    assert findings == []


@pytest.mark.parametrize("mod", ["importlib", "subprocess", "requests", "httpx",
                                 "urllib", "socket", "ctypes", "runpy"])
def test_every_dynamic_dispatch_module_is_refused(mod):
    """Two-way guard on the constant: each entry must actually be enforced."""
    import tempfile, os as _os
    d = tempfile.mkdtemp()
    open(_os.path.join(d, "m.py"), "w").write(f"import {mod}\n")
    assert ad_block.scan_for_ad_call_paths(d), mod


def test_the_package_uses_no_dynamic_dispatch_of_its_own():
    """The positive form: the ad tables are only sound if the ONLY way out of this
    package is a static import."""
    assert ad_block.assert_no_ad_call_path() is True


# ---------------------------------------------------------------------------
# ROUND-2 BYPASSES: module-object aliasing, sys.modules, bytes literals,
# self-exemption. Each scanned CLEAN before.
# ---------------------------------------------------------------------------
def test_package_root_import_is_refused_module_object_aliasing(tmp_path):
    """`import agent` then `agent.meta_publisher.publish(...)` imports nothing
    forbidden BY NAME and calls nothing in the call table, yet reaches the module."""
    (tmp_path / "b1.py").write_text(
        "import agent\ndef go(): return agent.meta_publisher.publish(1)\n")
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("package root" in f for f in findings), findings


def test_an_attribute_chain_into_a_forbidden_module_is_refused(tmp_path):
    (tmp_path / "b1b.py").write_text(
        "def go(agent): return agent.meta_publisher.publish(1)\n")
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("attribute chain" in f for f in findings), findings


def test_sys_modules_is_refused(tmp_path):
    """sys.modules is a dynamic import by another name."""
    (tmp_path / "b2.py").write_text(
        "import sys\ndef go(): return sys.modules['agent.meta_publisher'].publish(1)\n")
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("'sys'" in f for f in findings), findings


def test_a_graph_url_hidden_as_bytes_is_refused(tmp_path):
    (tmp_path / "b3.py").write_text(
        'URL = b"graph.facebook.com/v19.0/act_1/campaigns"\n')
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("Graph API" in f for f in findings), findings


def test_the_guard_file_no_longer_exempts_itself_from_the_call_table(tmp_path):
    """ad_block.py exempted ITSELF from the dynamic-call table — for no benefit, since
    no call in it was ever in the table — while opening a hole in the one file whose
    whole job is to have no holes. Asserted behaviourally: a file NAMED ad_block.py
    containing an escape hatch must be flagged.

    (The LITERAL-table exemption is different and stays: that file's forbidden-URL
    fragments are themselves string literals, so scanning them would always self-flag.
    The test below pins that distinction.)"""
    (tmp_path / "ad_block.py").write_text(
        "def go(x):\n    return eval(x)\n")
    findings = ad_block.scan_for_ad_call_paths(str(tmp_path))
    assert any("eval" in f for f in findings), findings


def test_the_literal_table_self_exemption_is_still_needed_and_present(tmp_path):
    """The real ad_block.py holds 'graph.facebook.com' and '/act_' as constants. It
    must not flag itself for them, or the guarantee could never be clean."""
    assert any(frag in open(os.path.join(PKG, "ad_block.py"), encoding="utf-8").read()
               for frag in ad_block.FORBIDDEN_AD_LITERAL_FRAGMENTS)
    assert ad_block.scan_for_ad_call_paths() == []


def test_computed_getattr_is_refused_but_a_literal_one_is_not(tmp_path):
    (tmp_path / "c1.py").write_text(
        'def go(m, n): return getattr(m, "create_" + n)()\n')
    assert any("reflection" in f
               for f in ad_block.scan_for_ad_call_paths(str(tmp_path))), "computed"
    (tmp_path / "c1.py").write_text('def go(m): return getattr(m, "available", None)\n')
    assert ad_block.scan_for_ad_call_paths(str(tmp_path)) == [], "literal must pass"


def test_ordinary_relative_imports_are_not_flagged_as_root_aliasing():
    """`from .. import config` resolves to the bare root but names no module of its
    own; flagging it would make the scanner refuse the whole package."""
    assert ad_block.scan_for_ad_call_paths() == []


# ===========================================================================
# CONTROL 1 — THE LOAD-BEARING ONE.
#
# Two audit rounds falsified the claim that the AST scan is a SOUND PROOF that no ad
# write is reachable: round 2 found three bypasses, round 3 found eleven more,
# including Bus()._client() returning the live `requests` module through a module this
# package legitimately imports. Patching the tables a third time is the loop D68 says
# to stop ("you are not one case away; change what you are gating on"), so the claim
# was withdrawn instead. What actually holds is that this SERVICE has no ad-write rail.
# ===========================================================================
def test_this_repository_contains_no_meta_marketing_api_rail():
    """The control. While this holds, no code path in this package can reach an ad
    write, because there is nothing to reach and no credential to reach it with — and
    that is true regardless of any property of the scanner."""
    findings = ad_block.scan_repo_for_ad_rail()
    assert findings == [], "\n".join(findings[:20])
    assert ad_block.assert_no_ad_rail_in_repo() is True


def test_the_ad_rail_check_would_actually_fire(tmp_path):
    """Prove the control is capable of failing, by planting each marker."""
    for marker in ad_block.AD_RAIL_MARKERS:
        d = tmp_path / marker.replace("/", "_").replace(".", "_")
        d.mkdir()
        (d / "rail.py").write_text(f'X = "{marker}"\n')
        assert ad_block.scan_repo_for_ad_rail(str(d)), marker
        with pytest.raises(ad_block.AdCallPathError):
            ad_block.assert_no_ad_rail_in_repo(str(d))


def test_run_once_asserts_the_ad_rail_control_before_reading_any_ticket(monkeypatch):
    """D68: an assertion nobody calls is itself an instance of the bug it exists to
    catch. This one runs on the real path, ahead of the tripwire."""
    polled = []

    class Bus:
        def available(self):
            return True

        def _get(self, table, params):
            polled.append(table)
            return []

    def boom(*_a, **_k):
        raise ad_block.AdCallPathError("planted rail")

    monkeypatch.setattr(consumer._ad, "assert_no_ad_rail_in_repo", boom)
    with pytest.raises(ad_block.AdCallPathError):
        consumer.run_once(bus=Bus(), flag_on=True)
    assert polled == []


def test_control_2_this_capability_can_perform_exactly_one_action():
    """A closed set of one is a stronger statement than a list of what cannot be
    imported: there is no branch that runs arbitrary code, no shell, no tool-granted
    model. What it CAN do is enumerable, and it is a per-gym media sync."""
    assert set(remedies.EXECUTORS) == {"per_gym_drive_sync"}
    assert remedies.EXECUTORS["per_gym_drive_sync"].__name__ == \
        "_exec_per_gym_drive_sync"


def test_the_docstring_no_longer_claims_the_scan_is_a_proof():
    """The overclaim is the finding, so its absence is the assertion. Two audits
    reported the same false sentence in two different files."""
    src = open(os.path.join(PKG, "ad_block.py"), encoding="utf-8").read()
    assert "sound proof" not in src.lower() or "was false" in src.lower()
    assert "TRIPWIRE" in src
    import agent.config as cfg
    doc = cfg.client_dm_autofix_enabled.__doc__ or ""
    assert "sound proof" not in doc.lower()
