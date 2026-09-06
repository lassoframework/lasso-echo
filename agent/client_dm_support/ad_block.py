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
#
# THIS TABLE IS AN ENUMERATION AND IS NOT WHAT THE GUARANTEE RESTS ON. A hand list of
# names loses to `api.adsets_insert(...)` like every enumeration loses to novel input.
# It is a secondary check. The SOUND argument is the pair above and below it: a module
# that cannot be imported (FORBIDDEN_AD_IMPORT_PREFIXES, now including the package root
# and every aliasing route) and cannot be reached by reflection
# (FORBIDDEN_DYNAMIC_MODULES / _CALLS) cannot have any function called on it, whatever
# that function is named.
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


# DYNAMIC DISPATCH IS FORBIDDEN IN THIS PACKAGE, full stop.
#
# The import and call tables above are only sound if the ONLY way out of this package
# is a static import. `importlib.import_module("agent.meta_publisher")` followed by
# `getattr(m, "create_campaign")()` defeats both tables, and `subprocess.run(["curl",
# ...])` defeats them plus the URL literal check. Rather than adding those spellings to
# a denylist -- an ENUMERATION OF AN OPEN SET, the exact failure D68 Part 1 is about --
# the escape hatches themselves are removed. This package has no legitimate need for
# any of them: it does no reflection, spawns no process, and makes no HTTP call of its
# own (the media store and the bus own all I/O).
FORBIDDEN_DYNAMIC_MODULES = frozenset({
    "importlib", "subprocess", "runpy", "ctypes", "socket",
    "requests", "httpx", "urllib", "urllib.request", "http.client",
    # `sys` gives sys.modules, which is a dynamic import by another name:
    # sys.modules['agent.meta_publisher'] reaches a module this file forbids
    # importing. The package has no need for sys.
    "sys",
})

# Module names refused by EXACT match only (never as a prefix).
#
# MODULE-OBJECT ALIASING: `import agent` then `agent.meta_publisher.publish(...)`
# imports nothing forbidden by name and calls nothing in the call table, yet reaches
# the module. So importing the package ROOT is refused. It must be exact-match: every
# legitimate import in this package resolves to `agent.something`, and forbidding that
# as a prefix would refuse the whole package.
FORBIDDEN_EXACT_MODULES = frozenset({"agent"})

# Names that can ONLY mean an escape hatch. Deliberately tight: `compile` is
# re.compile, `run` is diagnostics.run, and flagging those would make the scanner
# noise that gets switched off -- a control nobody trusts is not a control.
# subprocess/importlib are already unreachable via the import table above, so what
# remains here are the builtins and the os.* spellings that survive it.
FORBIDDEN_DYNAMIC_CALLS = frozenset({
    "import_module", "__import__", "eval", "exec",
    "system", "popen", "Popen", "check_output", "check_call", "spawnv",
})


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


# This package's own dotted name. A relative import is resolved against it, because
# `from ..meta_publisher import publish` inside agent.client_dm_support IS
# agent.meta_publisher -- the first entry in FORBIDDEN_AD_IMPORT_PREFIXES. The first
# version of this scanner skipped relative imports with the comment "a relative import
# can never reach another package", which is simply false, and relative-out-of-package
# is the ONLY cross-package import style this package uses.
PACKAGE_DOTTED = "agent.client_dm_support"


def _resolve_relative(module, level, package=PACKAGE_DOTTED):
    """`from ..x import y` at level 2 inside agent.client_dm_support -> agent.x"""
    parts = package.split(".")
    if level > len(parts):
        return module or ""
    base = parts[: len(parts) - (level - 1)]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _imported_names(tree, package=PACKAGE_DOTTED):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if not node.module:
                    continue
                base = node.module
            else:
                base = _resolve_relative(node.module, node.level, package)
            if not base:
                continue
            # `from .. import config` names no module of its own -- its base is the
            # package root, and yielding that would flag every ordinary relative
            # import as package-root aliasing. Only the CHILDREN it actually binds
            # are imports; a bare `import agent` still yields the root and is refused.
            if node.level == 0 or node.module:
                yield base
            for a in node.names:
                yield f"{base}.{a.name}"


def _dotted(node):
    """The dotted name of an attribute chain, e.g. `agent.meta_publisher.publish` for
    that expression. None when the chain is not rooted in a plain name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _attribute_chains(tree):
    """Every dotted attribute chain in the file. Catches module-object aliasing, where
    a forbidden module is reached through an object rather than by importing its name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            d = _dotted(node)
            if d:
                yield d


def _called_names(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            yield f.attr
        elif isinstance(f, ast.Name):
            yield f.id


def _fold(node):
    """The string a literal expression evaluates to, following + concatenation and
    adjacent-literal joining. `"graph." + "face" + "book.com"` is one string to a
    reader and must be one string to the scanner too."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
        # A URL hidden as a bytes literal reads the same to a human.
        try:
            return node.value.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    if isinstance(node, ast.JoinedStr):
        return "".join(_fold(v) or "" for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _string_literals(tree):
    """Every string a reader would see, with concatenations folded first so that
    splitting a URL across three literals does not hide it."""
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) or isinstance(node, ast.JoinedStr):
            folded = _fold(node)
            if folded:
                yield folded
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            folded = _fold(node)
            if folded:
                yield folded


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

            for bad in FORBIDDEN_DYNAMIC_MODULES:
                if name == bad or name.startswith(bad + "."):
                    findings.append(
                        f"{path}: imports {name!r}. Dynamic dispatch and self-driven "
                        f"I/O are forbidden here -- they would make the ad import/call "
                        f"tables unenforceable."
                    )
            if name in FORBIDDEN_EXACT_MODULES:
                findings.append(
                    f"{path}: imports the package root {name!r}, which reaches every "
                    f"module under it by attribute access without naming any of them"
                )

        for name in _called_names(tree):
            if name in FORBIDDEN_AD_CALL_NAMES:
                findings.append(f"{path}: calls forbidden ad-write function {name!r}")
            if name in FORBIDDEN_DYNAMIC_CALLS:
                findings.append(
                    f"{path}: calls {name!r}, a dynamic-dispatch or subprocess escape "
                    f"hatch; it could reach an ad write the static tables cannot see"
                )

        # getattr/setattr are fine with a LITERAL attribute name (that is just an
        # attribute access with a default). A COMPUTED name is reflection, and
        # `getattr(m, "create_" + "campaign")` is exactly the bypass the call table
        # cannot see, so only that form is refused.
        # Module-object aliasing, e.g. `import agent` + `agent.meta_publisher.x(...)`.
        for chain in _attribute_chains(tree):
            for bad in FORBIDDEN_AD_IMPORT_PREFIXES:
                if chain == bad or chain.startswith(bad + "."):
                    findings.append(
                        f"{path}: reaches forbidden module {bad!r} through the "
                        f"attribute chain {chain!r} without importing it by name"
                    )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if fname not in ("getattr", "setattr"):
                continue
            if len(node.args) < 2 or _fold(node.args[1]) is None:
                findings.append(
                    f"{path}: {fname}() with a computed attribute name is reflection "
                    f"and could reach an ad write the static call table cannot see"
                )

        # Skip this module's own constant tables: they are the definition of what is
        # forbidden, not a use of it.
        if os.path.basename(path) != "ad_block.py":
            for text in _string_literals(tree):
                low = text.lower()
                for frag in FORBIDDEN_AD_LITERAL_FRAGMENTS:
                    if frag in low:
                        findings.append(
                            f"{path}: literal {text[:60]!r} looks like a "
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
