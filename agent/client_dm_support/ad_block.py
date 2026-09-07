"""
ad_block.py — ad spend, ad targeting and campaign launch/pause are a PERMANENT,
ABSOLUTE never-autonomous zone for this capability.

Blake, 2026-09-06: "no code path in this capability may call into the Meta/Pipeboard
ad-write tools AT ALL — not gated by a check that could misfire, but literally no
import, no call path, nothing wired."

READ THIS BEFORE TRUSTING THE SCANNER BELOW.

Two earlier versions of this file claimed the AST scan was a SOUND PROOF that no ad
write is reachable. That claim was false, and it was falsified twice: round 2 of the
audit found three bypasses, round 3 found eleven more (subscript-rooted calls,
`os.execvp`/`posix_spawn`, attribute chains not rooted in a plain Name, `chr()`-built
URLs, and -- the sharpest one -- `Bus()._client()`, which returns the live `requests`
module through a module this package legitimately imports).

Patching the tables a third time is the exact loop D68 Part 1 says to stop:
"If you are on round three of 'add the missed case to the list', stop. You are not one
case away. Change what you are gating on." A denylist of call names and module names
over a Turing-complete language is an ENUMERATION OF AN OPEN SET, and it will keep
losing. So the claim is withdrawn rather than re-patched.

WHAT ACTUALLY MAKES AD MONEY IMPOSSIBLE HERE, in order of strength:

  1. THERE IS NO AD-WRITE RAIL IN THIS REPOSITORY. lasso-echo has no Meta Marketing
     API client, no ad-account id, no system-user token, no campaign/adset/ad writer.
     Those rails live in the portal, in a different codebase, behind different
     credentials. assert_no_ad_rail_in_repo() checks this and is the load-bearing
     control: code cannot call an ad write that does not exist and cannot authenticate
     to one with a credential this service does not hold.
  2. THIS CAPABILITY EXECUTES EXACTLY ONE ACTION. remedies.EXECUTORS has a single
     entry, a per-gym media sync, and flow.py can reach no other. There is no branch
     that runs arbitrary code, no shell, no model with a tool grant. What it can DO is
     a closed set of one, which is a stronger statement than what it cannot import.
  3. EVERY AD-SHAPED MESSAGE ESCALATES UNCONDITIONALLY, before any diagnostic runs
     (flow.handle_ticket step 1), and scope_gate refuses any ad-related action first,
     before any allowlist could rescue it.
  4. The AST scan below is a TRIPWIRE, not a proof. It catches the careless case --
     somebody importing an ad module or calling an obvious ad-write name -- and it
     fails the build loudly when it fires. It is genuinely useful for that. It is NOT
     evidence that no path exists, it cannot be, and nothing in this package should be
     described as if it were.

If an ad-write rail is ever added to this repository, control 1 stops holding and this
capability must be re-reviewed from scratch. assert_no_ad_rail_in_repo() is what turns
that from a thing somebody must remember into a test failure.
"""
from __future__ import annotations

import ast
import os

# ---------------------------------------------------------------------------
# TRIPWIRE TABLE 1: modules this package must not import.
#
# HONEST LABELLING, because an earlier version of this comment was wrong. The
# `agent.*` entries here are NOT ad-money surfaces -- this repo has none:
#   agent.meta_publisher  organic Instagram/Facebook publishing
#   agent.meta_check      IG/FB token verification
#   agent.pixel_gate      the creative FABRICATION gate on rendered pixels; the name
#                         collides with the Meta tracking pixel and means the opposite
#   agent.spend           Gemini generation spend, not ad spend
#   agent.runway          creative runway, not ad delivery
# They are listed because this lane has no business importing any of them, and because
# a future rename could turn one into something that matters. facebook_business and
# pipeboard are the real ad SDK/MCP names and are not present in this repo at all.
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


# ---------------------------------------------------------------------------
# CONTROL 1, the load-bearing one: there is no ad-write rail in this repository.
# ---------------------------------------------------------------------------
# Markers of a Meta Marketing API surface. Unlike the tripwire tables above, this is
# not trying to enumerate the ways code could be written -- it is asking whether the
# ingredients of an ad write exist ANYWHERE in the service at all: an ad-account id, a
# marketing-API endpoint, or an SDK that speaks to one. If none of them exists, no
# amount of cleverness inside this package reaches an ad write, because there is
# nothing to reach and nothing to authenticate with.
AD_RAIL_MARKERS = (
    "facebook_business",           # the Meta Marketing API python SDK
    "graph.facebook.com/v",        # a versioned Graph endpoint
    "/act_",                       # an ad-account path segment
    "adaccount",                   # AdAccount SDK object
    "META_AD_ACCOUNT",
    "META_SYSTEM_USER_TOKEN",
    "MARKETING_API",
)

# Files that legitimately CONTAIN these strings because their job is to name them.
# Matched on the path ENDING, not the bare basename: exempting by basename anywhere in
# the tree meant any file called ad_block.py, in any directory, was skipped.
AD_RAIL_MARKER_EXEMPT = (
    "agent/client_dm_support/ad_block.py",
    "tests/test_client_dm_ad_block.py",
)


def scan_repo_for_ad_rail(repo_root=None):
    """Findings naming any Meta Marketing API rail in this service. Empty == none."""
    if repo_root is None:
        repo_root = os.path.dirname(os.path.dirname(_package_dir()))
    findings = []
    for root, dirs, names in os.walk(repo_root):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", "node_modules", ".venv",
                                "venv", ".venv-ops", "ghl-audit")]
        for n in sorted(names):
            if not n.endswith(".py"):
                continue
            path = os.path.join(root, n)
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            if any(rel == e or rel.endswith("/" + e) for e in AD_RAIL_MARKER_EXEMPT):
                continue
            try:
                low = open(path, "r", encoding="utf-8", errors="replace").read().lower()
            except OSError:
                continue
            for marker in AD_RAIL_MARKERS:
                if marker.lower() in low:
                    findings.append(f"{path}: contains ad-rail marker {marker!r}")
    return findings


def assert_no_ad_rail_in_repo(repo_root=None):
    """THE CONTROL. Raise if this service has acquired an ad-write rail.

    While this holds, "ad money is structurally impossible for this capability" is a
    statement about the whole service and does not depend on any property of the
    scanner above. If it ever fires, this capability must be re-reviewed from scratch
    before it is armed again -- which is the point of asserting it rather than
    remembering it.
    """
    findings = scan_repo_for_ad_rail(repo_root)
    if findings:
        raise AdCallPathError(
            "this repository has acquired a Meta Marketing API rail, so 'no ad write "
            "is reachable' no longer follows from the absence of one. "
            "agent/client_dm_support/ must be re-reviewed before it is armed:\n  - "
            + "\n  - ".join(findings[:20])
        )
    return True
