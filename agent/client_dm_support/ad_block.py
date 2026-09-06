"""
ad_block.py — ad spend, ad targeting and campaign launch/pause are a PERMANENT,
ABSOLUTE never-autonomous zone for this capability.

Blake, 2026-09-06: "no code path in this capability may call into the Meta/Pipeboard
ad-write tools AT ALL — not gated by a check that could misfire, but literally no
import, no call path, nothing wired."

So there are two things in this file and only ONE of them is the control.

  THE CONTROL is structural absence. agent/client_dm_support/ imports no ad module
  and calls no ad-write function, anywhere, on any branch. assert_no_ad_call_path()
  proves that by parsing every source file in the package with `ast` and failing if
  an import or a call name in the tables below appears. It is asserted statically by
  tests/test_client_dm_ad_block.py AND called at runtime from consumer.run_once()
  before any ticket is read -- because D68 records that "an assertion nobody calls is
  itself an instance of the bug it exists to catch."

  THE BELT is is_ad_money_topic(), a keyword list that makes an ad-shaped message
  escalate unconditionally and early, before any diagnostic runs. It is deliberately
  NOT the control, and must never be treated as one: a keyword list over natural
  language is an ENUMERATION OF AN OPEN SET, the exact failure mode D68 Part 1 is
  about, and it will miss phrasings. It exists so the common case escalates with a
  useful reason rather than falling through to "no diagnostic matched". Safety comes
  from the fact that even a message this list misses cannot reach an ad-write call,
  because no such call exists to reach.

Note the asymmetry that makes this safe: the belt can only ever cause MORE
escalation, never less. There is no branch on which is_ad_money_topic() returning
False permits an ad action -- the only ad action available to this package is none.
"""
from __future__ import annotations

import ast
import os

# ---------------------------------------------------------------------------
# THE FORBIDDEN SURFACE. Any module whose dotted name starts with one of these,
# imported anywhere in this package, is a build error.
#
# These cover the three ways an ad write could reach Meta from this repo:
# Echo's own Meta modules, the Pipeboard/Meta MCP + SDK client surfaces, and the
# ads-triage lane that already holds live campaign write rails.
# ---------------------------------------------------------------------------
FORBIDDEN_AD_IMPORT_PREFIXES = frozenset({
    "agent.meta_publisher",
    "agent.meta_check",
    "agent.pixel_gate",
    "agent.spend",
    "agent.runway",
    "facebook_business",
    "pipeboard",
})

# Attribute/function names that mean "write to an ad account". A call to any of
# these -- however the callee was obtained -- is a build error in this package.
FORBIDDEN_AD_CALL_NAMES = frozenset({
    "create_campaign", "update_campaign", "bulk_update_campaigns",
    "duplicate_campaign", "create_adset", "update_adset",
    "bulk_update_adsets", "duplicate_adset", "create_ad", "update_ad",
    "bulk_update_ads", "duplicate_ad", "create_ad_creative",
    "create_budget_schedule", "update_ad_url_tags", "ads_update_entity",
    "ads_activate_entity", "ads_create_campaign", "ads_create_ad_set",
    "ads_create_ad", "update_custom_audience", "create_custom_audience",
    "upload_conversion_events", "ads_pixel_event_create",
    "ads_pixel_event_update", "set_budget", "set_daily_budget",
    "pause_campaign", "resume_campaign", "launch_campaign",
})

# Strings that, appearing as a literal anywhere in the package, would mean somebody
# is building a Graph API URL by hand to get around the tables above.
FORBIDDEN_AD_LITERAL_FRAGMENTS = (
    "graph.facebook.com",
    "/act_",
    "adsets?",
    "adcreatives?",
)


class AdCallPathError(AssertionError):
    """The package acquired a path to an ad write. This is never recoverable at
    runtime -- it means the structural guarantee is gone and the code must not run."""


def _package_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _source_files(pkg_dir=None):
    pkg_dir = pkg_dir or _package_dir()
    out = []
    for root, _dirs, names in os.walk(pkg_dir):
        if "__pycache__" in root:
            continue
        for n in sorted(names):
            if n.endswith(".py"):
                out.append(os.path.join(root, n))
    return out


def _imported_names(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has module None; a relative import can never reach
            # another package, so only absolute ones are interesting here.
            if node.level == 0 and node.module:
                for a in node.names:
                    yield f"{node.module}.{a.name}"
                yield node.module


def _called_names(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            yield f.attr
        elif isinstance(f, ast.Name):
            yield f.id


def scan_for_ad_call_paths(pkg_dir=None):
    """Return a list of human-readable findings. Empty list == the guarantee holds.

    This file is itself scanned, so the tables above must not look like calls or
    imports: they are string constants, which is why they are written as sets of
    strings and never as symbols.
    """
    findings = []
    for path in _source_files(pkg_dir):
        try:
            src = open(path, "r", encoding="utf-8").read()
        except OSError as e:  # noqa: BLE001
            findings.append(f"{path}: unreadable ({type(e).__name__})")
            continue
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError as e:
            findings.append(f"{path}: unparseable ({e})")
            continue

        for name in _imported_names(tree):
            for bad in FORBIDDEN_AD_IMPORT_PREFIXES:
                if name == bad or name.startswith(bad + "."):
                    findings.append(f"{path}: imports forbidden ad module {name!r}")

        for name in _called_names(tree):
            if name in FORBIDDEN_AD_CALL_NAMES:
                findings.append(f"{path}: calls forbidden ad-write function {name!r}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Skip this module's own constant tables: they are the definition of
                # what is forbidden, not a use of it.
                if os.path.basename(path) == "ad_block.py":
                    continue
                low = node.value.lower()
                for frag in FORBIDDEN_AD_LITERAL_FRAGMENTS:
                    if frag in low:
                        findings.append(
                            f"{path}: literal {node.value[:60]!r} looks like a "
                            f"hand-built Graph API ad URL"
                        )
    return findings


def assert_no_ad_call_path(pkg_dir=None):
    """Raise AdCallPathError if any ad write became reachable from this package.

    Called by consumer.run_once() before a single ticket is read, and asserted
    independently by tests/test_client_dm_ad_block.py.
    """
    findings = scan_for_ad_call_paths(pkg_dir)
    if findings:
        raise AdCallPathError(
            "agent/client_dm_support/ must have NO path to an ad write "
            "(Blake, 2026-09-06: structurally incapable, not gated). Found:\n  - "
            + "\n  - ".join(findings)
        )
    return True


# ---------------------------------------------------------------------------
# THE BELT (not the control -- see the module docstring).
# ---------------------------------------------------------------------------
AD_MONEY_TERMS = (
    "ad spend", "adspend", "ad budget", "budget", "cpl", "cost per lead",
    "cpm", "cpc", "roas", "campaign", "campaigns", "ad set", "adset",
    "ad sets", "adsets", "targeting", "audience", "lookalike", "retarget",
    "boost", "boosted", "meta ads", "facebook ads", "fb ads", "instagram ads",
    "ads manager", "pixel", "capi", "conversions api", "daily budget",
    "scale up", "scale my ads", "pause my ads", "turn off my ads",
    "turn my ads", "launch the ad", "launch my ad", "run ads", "running ads",
)


def is_ad_money_topic(text) -> bool:
    """True when a message plainly concerns ad budget, targeting or campaign
    launch/pause. Used ONLY to escalate earlier and with a better reason. Never used
    to permit anything."""
    low = (text or "").lower()
    return any(term in low for term in AD_MONEY_TERMS)


AD_ESCALATION_REASON = (
    "ad_money_always_escalates: ad budget, ad targeting and campaign launch/pause "
    "are never autonomous for this capability (Blake, 2026-09-06). This lane has no "
    "ad-write call path at all; a human handles this."
)
