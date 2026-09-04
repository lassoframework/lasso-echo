"""
routing.py — product -> agent identity, with NO fallthrough.

Blake's ruling (2026-09-03, item 1, verbatim): "Every ticket routes by product to that
agent identity... enforce in config and routing." And in the scope notes to the builder:
"Add an explicit routing table/function... that maps product -> identity name with NO
fallthrough/default branch (an unmapped product must raise or escalate to a human, never
silently pick an identity)."

Final map (Blake's ruling item 1, business description):
    Wrangler: websites (lassoframework-site, lasso-gym-sites)
    Echo:     social
    Lainey:   lead nurture (engage)
    Scout:    portal
    Ranger:   ads

D34 (logged in docs/slack_convo/DECISIONS.md): the ruling's "social" / "engage" / "portal"
/ "ads" are Blake's business description of each agent's domain, not necessarily literal
support_tickets.product column values -- and the scope note to the builder named ONLY
Wrangler's product for retargeting ("Wrangler's entry needs product retargeted from
'wrangler' to 'websites'"). Echo/Ranger/Scout/Lainey's product columns are left at their
existing self-referential values (echo/ranger/scout/lainey) because renaming them to
social/ads/portal/engage would be a much larger, unrequested blast-radius change: the
portal's Ranger cron (`fixer-lane.ts`) polls `product='ranger'` literally, and other
non-Echo-repo consumers may do the same for their own identity name. Only Wrangler's
column changes, because item 1 explicitly asked for it and lassoframework-site /
lasso-gym-sites tickets had no distinct product value to route on at all before this. This
map is therefore keyed on the REAL product values in use today, not the ruling's
descriptive labels; renaming the other four to match the business labels literally is
flagged back to Blake as an open ruling, not silently done.

This is the single source of truth for product -> identity. identities.py carries the
identity -> product value back (BotIdentity.product), so the two must agree; `route()`
double-checks that at call time.
"""
from . import identities as _identities


class UnroutableProduct(Exception):
    """A ticket's product has no agent identity mapping. NEVER caught to silently fall
    back to a default identity -- every caller must treat this as "escalate to a human",
    exactly Blake's instruction. No cross-agent posting is possible if no identity is ever
    guessed."""


# The one map. Keys are lowercase support_tickets.product values as ACTUALLY stamped
# today (see D34 above for why these are not literally "social"/"ads"/"portal"/"engage");
# values are identities.IDENTITIES keys. No default/else branch anywhere near this.
PRODUCT_TO_IDENTITY = {
    "websites": "wrangler",   # lassoframework-site, lasso-gym-sites
    "echo": "echo",           # social
    "lainey": "lainey",       # lead nurture / engage
    "scout": "scout",         # portal
    "ranger": "ranger",       # ads
}


def route(product: str) -> str:
    """product -> identity name. Raises UnroutableProduct for anything not in the map
    above. No default branch: an unmapped product is a bug or a new product that has not
    been onboarded, never a guess at which agent should answer."""
    key = (product or "").strip().lower()
    if key not in PRODUCT_TO_IDENTITY:
        raise UnroutableProduct(f"no agent identity mapped for product {product!r}")
    name = PRODUCT_TO_IDENTITY[key]
    # Defensive: a registry entry that drifts (identities.py's product field edited
    # without this map, or vice versa) surfaces as the same escalate-not-guess failure,
    # not as a silent misroute.
    ident = _identities.get(name)
    if ident.product != key:
        raise UnroutableProduct(
            f"routing drift: product {product!r} maps to identity {name!r}, but "
            f"{name!r}.product is {ident.product!r}")
    return name


def route_identity(product: str):
    """Convenience: product -> the BotIdentity object itself."""
    return _identities.get(route(product))
