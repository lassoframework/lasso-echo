"""
tests/test_slack_convo_routing.py — product -> agent identity routing, no fallthrough.

Blake's TESTS list (2026-09-03 ruling, item 1): "product to agent routing has no
fallthrough, a ticket never gets a reply from the wrong identity."
"""
import pytest

from agent.slack_convo import identities as ids
from agent.slack_convo import routing


def test_final_map_routes_every_identity_with_no_gaps():
    # Keys are the REAL support_tickets.product values in use today (see D34 in
    # docs/slack_convo/DECISIONS.md for why these are not literally the ruling's
    # "social"/"ads"/"portal"/"engage" business labels), but every one of the five
    # identities is mapped, with no fallthrough.
    assert set(routing.PRODUCT_TO_IDENTITY.values()) == {
        "wrangler", "echo", "lainey", "scout", "ranger"}
    assert routing.PRODUCT_TO_IDENTITY["websites"] == "wrangler"


@pytest.mark.parametrize("product,expected", [
    ("websites", "wrangler"),
    ("echo", "echo"),
    ("lainey", "lainey"),
    ("scout", "scout"),
    ("ranger", "ranger"),
])
def test_route_maps_every_product_to_its_identity(product, expected):
    assert routing.route(product) == expected


def test_route_is_case_insensitive_and_trims_whitespace():
    assert routing.route(" Websites ") == "wrangler"
    assert routing.route("ECHO") == "echo"


@pytest.mark.parametrize("bad", ["", "wrangler", "unknown_product", "social", "ads", "portal",
                                 "engage", None])
def test_unmapped_product_raises_never_falls_through(bad):
    # NB: "wrangler" is an IDENTITY name, not a PRODUCT value -- passing it where a product
    # is expected must raise, not silently resolve. "social"/"ads"/"portal"/"engage" are
    # the ruling's BUSINESS labels (D34), not real product column values, so they must
    # raise too rather than being guessed into a mapping.
    with pytest.raises(routing.UnroutableProduct):
        routing.route(bad)


def test_route_never_returns_an_identity_for_an_unmapped_product():
    """Defends against a future refactor adding an `except: return "echo"` or similar
    silent default -- the exception type is the whole contract."""
    try:
        routing.route("something_nobody_onboarded")
    except routing.UnroutableProduct:
        pass
    else:
        pytest.fail("route() must raise UnroutableProduct, never return silently")


def test_every_mapped_identity_actually_exists_in_the_registry():
    for identity_name in routing.PRODUCT_TO_IDENTITY.values():
        assert identity_name in ids.IDENTITIES


def test_every_mapped_identitys_own_product_field_agrees_with_the_map():
    """Routing-drift guard (D34): identities.py's BotIdentity.product must be the exact
    key that maps to it here, or route() raises rather than silently misrouting."""
    for product, identity_name in routing.PRODUCT_TO_IDENTITY.items():
        assert ids.IDENTITIES[identity_name].product == product


def test_wrangler_identity_product_is_websites_not_wrangler():
    """Item 1's specific instruction: Wrangler's product is retargeted from 'wrangler' to
    'websites' so lassoframework-site / lasso-gym-sites tickets route to Wrangler."""
    assert ids.IDENTITIES["wrangler"].product == "websites"


def test_route_identity_returns_the_botidentity_object():
    ident = routing.route_identity("ranger")
    assert ident.name == "ranger"
    assert ident is ids.IDENTITIES["ranger"]
