"""
no_ad_rail.py — A TRIPWIRE, and it says so.

Blake's hardest limit: ad budget, targeting and campaign launch/pause always escalate,
structurally, with no import and no call path into any Meta/Pipeboard ad-write tool
anywhere in this capability.

WHAT THE GUARANTEE ACTUALLY RESTS ON, strongest first. This ordering is not decoration:
the previous build spent two audit rounds patching an AST scanner it called a proof,
found fourteen bypasses across those rounds (relative imports, importlib + computed
getattr, module-object aliasing, sys.modules, bytes literals, chr()-built URLs,
subscript-rooted calls, os.execvp, a live requests module reachable through a module
the package legitimately imported), and correctly withdrew the claim in round 3 rather
than patching a third time. That is D68's rule applied: "if you are on round three of
adding the missed case to the list, stop; change what you are gating on."

  1. THIS CAPABILITY PERFORMS EXACTLY ONE ACTION, and it is a per-gym Google Drive
     media sync. conditions.ACTIONS is a closed set of one, pinned literally by a
     two-way test. A closed set of one is a stronger statement than any list of what
     may not be imported, because it enumerates what CAN happen rather than trying to
     enumerate what cannot.
  2. THIS SERVICE HAS NO AD-WRITE RAIL. There is no Marketing API client, no ad-account
     id and no ad system-user token in this repo's Echo worker. Code cannot call an ad
     write that does not exist or authenticate with a credential it does not hold.
  3. EVERY AD-SHAPED MESSAGE ESCALATES UNCONDITIONALLY AND FIRST (lane.ESCALATE_ALWAYS),
     before anything reads a fact. That belt is a keyword list, i.e. an enumeration over
     an open set -- so it is labelled a belt, and nothing rests on it. A phrasing it
     misses costs a human a ticket; it cannot cost a client a wrong action, because of
     1 and 2.
  4. THIS FILE. A static scan of the package's OWN source for an ad import or an ad
     credential. It catches the careless case -- somebody adding `from ..meta_publisher
     import x` in six months -- and turns it into a red test rather than a discovery.
     It is not a proof and this docstring does not claim it is one.
"""
from __future__ import annotations

import ast
import os

# Modules that would give this package a path towards a paid-ads surface. A tripwire
# list: informative, not load-bearing. Named honestly -- several near-neighbours in this
# repo are NOT ad-money surfaces (meta_publisher is organic IG/FB publishing; pixel_gate
# is the creative fabrication gate, a name collision meaning the opposite; `spend` is
# Gemini spend; `runway` is creative runway), and pretending otherwise is how the
# previous build's table came to claim coverage it did not have.
TRIPWIRE_MODULES = frozenset({
    "facebook_business",
    "pipeboard",
})

# Strings that would only appear here if somebody were building an ad call.
#
# NOTE, recorded because it is the whole lesson in miniature: the first draft of this
# list contained the bare token "act_" (as in Meta's /act_<id>/ ad-account path). It
# fired immediately -- on `_extract_ctas`, which contains "act_" as a substring. A
# substring test doing a token's job is the same defect this repo has now hit in an
# allowlist (a vendor path passing a root check), in a denylist, and here. Each entry
# below is anchored so it cannot match ordinary identifiers.
TRIPWIRE_LITERALS = (
    "graph.facebook.com",
    "META_SYSTEM_USER_TOKEN",
    "/act_",
    "/campaigns",
    "/adsets",
)


class AdRailPresent(RuntimeError):
    """The tripwire fired. Never soften this into a warning."""


def _package_files():
    here = os.path.dirname(os.path.abspath(__file__))
    return sorted(os.path.join(here, f) for f in os.listdir(here)
                  if f.endswith(".py"))


def scan_package():
    """Every tripwire hit in this package's own source, as (file, line, what)."""
    hits = []
    for path in _package_files():
        base = os.path.basename(path)
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError as e:  # noqa: PERF203
            raise AdRailPresent(f"{base} does not parse: {e}") from e
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                head = str(n).split(".")[0]
                if head in TRIPWIRE_MODULES:
                    hits.append((base, getattr(node, "lineno", 0), f"import {n}"))
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                text = (node.value.decode("utf-8", "ignore")
                        if isinstance(node.value, bytes) else node.value)
                for lit in TRIPWIRE_LITERALS:
                    # This file itself declares the literals it looks for.
                    if base == "no_ad_rail.py":
                        continue
                    if lit in text:
                        hits.append((base, getattr(node, "lineno", 0),
                                     f"literal {lit!r}"))
    return hits


def assert_no_ad_rail():
    """Called on the real path in lane.run_once(), not only from tests -- a guard that
    only ever runs under pytest is an assertion nobody calls, which is itself an
    instance of the bug it exists to catch (D68)."""
    hits = scan_package()
    if hits:
        detail = "; ".join(f"{f}:{ln} {what}" for f, ln, what in hits)
        raise AdRailPresent(
            "agent/client_dm_support now contains an ad-shaped import or credential, "
            "which this capability must never have: " + detail
        )
    return True
