"""
Per-gym source store (Part 1). A stored item resolves to exactly one account and
one category; nothing leaks across accounts; unknown categories and empty text
are refused; the approved-claims set is per-account and excludes other gyms.
Fully OFFLINE (tmp sqlite via AGENT_DB_PATH).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_sources as cs  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    yield


# ---- 1. an item resolves to one account and one category ----------------------
def test_add_and_read_one_account_one_category():
    s = cs.add_source("gym_alpha_ig", "offer",
                      "6 week transformation challenge for $199",
                      citation="website /pricing")
    assert s.account_key == "gym_alpha_ig"
    assert s.category == "offer"
    assert s.status == "approved"
    assert s.citation == "website /pricing"
    got = cs.approved_sources("gym_alpha_ig", category="offer")
    assert [x.text for x in got] == ["6 week transformation challenge for $199"]
    # a different category for the same account does not return it
    assert cs.approved_sources("gym_alpha_ig", category="faq") == []


# ---- 2. nothing leaks across accounts -----------------------------------------
def test_no_cross_account_leak():
    cs.add_source("gym_alpha_ig", "service", "Small group personal training")
    cs.add_source("gym_beta_ig", "service", "24/7 open gym access")
    alpha = [s.text for s in cs.approved_sources("gym_alpha_ig")]
    beta = [s.text for s in cs.approved_sources("gym_beta_ig")]
    assert alpha == ["Small group personal training"]
    assert beta == ["24/7 open gym access"]
    # approved_claims is per-account too: beta's fact never clears alpha
    assert "24/7 open gym access" not in cs.approved_claims("gym_alpha_ig")
    assert "Small group personal training" not in cs.approved_claims("gym_beta_ig")


# ---- 3. citation defaults to the account when blank ---------------------------
def test_blank_citation_defaults_to_account():
    s = cs.add_source("gym_alpha_ig", "about", "Family owned since 2015")
    assert s.citation == "client:gym_alpha_ig"


# ---- 4. validation: unknown category / empty text refused ---------------------
def test_unknown_category_refused():
    with pytest.raises(ValueError):
        cs.add_source("gym_alpha_ig", "nonsense", "text")


def test_empty_text_refused():
    with pytest.raises(ValueError):
        cs.add_source("gym_alpha_ig", "offer", "   ")


def test_missing_account_refused():
    with pytest.raises(ValueError):
        cs.add_source("", "offer", "text")


# ---- 5. categories_present is per-account, canonical order ---------------------
def test_categories_present_canonical_order():
    cs.add_source("gym_alpha_ig", "promo", "New Year kickoff")
    cs.add_source("gym_alpha_ig", "offer", "Free intro session")
    cs.add_source("gym_alpha_ig", "faq", "Do you offer childcare? Yes.")
    # canonical order is offer, service, testimonial, faq, about, promo
    assert cs.categories_present("gym_alpha_ig") == ["offer", "faq", "promo"]
    assert cs.categories_present("gym_beta_ig") == []


# ---- 6. approved_claims carries the raw fact for the fabrication gate ----------
def test_approved_claims_contains_stored_text():
    cs.add_source("gym_alpha_ig", "offer", "Join for $99 a month")
    claims = cs.approved_claims("gym_alpha_ig")
    assert "Join for $99 a month" in claims


# ---- the variant fallback must apply to EVERY reader, not just approved ----------
def test_pending_and_all_sources_are_variant_aware():
    """LIVE BUG (2026-08-30): the tenant-variant fallback was applied to
    approved_sources ONLY. A gym whose portal intake landed under the BARE base key had
    pending_sources return [] — so the approval tooling could not see the rows a human
    needed to approve, and the gym sat in no_sources forever with its answers sitting
    right there. Zanshin had 45 pending sources that were invisible this way."""
    # the portal form lands sources under the BARE base key, PENDING approval...
    cs.add_source("westside", "offer", "A real offer line", "client social intake",
                  status="pending")
    # ...while every builder reads them under <base>_ig.
    assert [s.text for s in cs.pending_sources("westside_ig")] == ["A real offer line"]
    assert [s.text for s in cs.all_sources("westside_ig")] == ["A real offer line"]
    # and the reverse direction still works
    assert [s.text for s in cs.pending_sources("westside")] == ["A real offer line"]


def test_variant_fallback_never_merges_two_keys():
    """First variant WITH rows wins; rows are never merged, so a gym with rows under
    both keys is not double-counted."""
    cs.add_source("westside_ig", "offer", "Primary key line", "intake",
                  status="pending")
    cs.add_source("westside", "offer", "Base key line", "intake", status="pending")
    got = [s.text for s in cs.pending_sources("westside_ig")]
    assert got == ["Primary key line"], f"variants were merged: {got}"


def test_variants_bridge_punctuation_drift_between_portal_and_registry():
    """LIVE (2026-08-30): echo_intake_tokens carried 'districth' for the gym Echo knows
    as 'district_h'. An intake submitted then would have landed on a key NO reader ever
    searched, and the gym would have sat in no_sources with its answers in the table —
    the exact failure four other gyms had already hit."""
    from agent import client_sources as cs
    v = cs._tenant_variants("district_h_ig")
    assert "districth" in v and "districth_ig" in v
    # the exact key still comes first
    assert v[0] == "district_h_ig"


def test_punctuation_variant_finds_sources_landed_under_the_portal_key(monkeypatch):
    """Reads are exercised against a stubbed row source: this asserts the VARIANT
    SEARCH, not sqlite, and keeps the test free of shared-db side effects."""
    from agent import client_sources as cs
    stored = {"districth": ["Their real offer line"]}
    monkeypatch.setattr(cs, "_rows",
                        lambda key, status=None, category=None: list(stored.get(key, [])))
    assert cs.pending_sources("district_h_ig") == ["Their real offer line"]
    assert cs.all_sources("district_h_ig") == ["Their real offer line"]


def test_exact_key_always_beats_the_normalised_twin(monkeypatch):
    from agent import client_sources as cs
    stored = {"district_h_ig": ["Exact key line"], "districth": ["Normalised key line"]}
    monkeypatch.setattr(cs, "_rows",
                        lambda key, status=None, category=None: list(stored.get(key, [])))
    got = cs.pending_sources("district_h_ig")
    assert got == ["Exact key line"], f"normalised variant won over the exact key: {got}"


# ---- intake auto-approve (Blake 2026-08-30) --------------------------------------
def test_intake_status_is_pending_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_INTAKE_AUTO_APPROVE", raising=False)
    from agent import client_sources as cs
    assert cs.intake_status() == "pending"


def test_intake_status_is_approved_when_armed(monkeypatch):
    """Intake sources landed PENDING and waited on a human step nobody ran, so gyms sat
    in no_sources for weeks with their own answers in the table (Hill Country 22 from
    Aug 23, Zanshin 45 from Aug 27, ENG 61). A gym's own words about its own gym do not
    need vetting before Echo may quote them."""
    monkeypatch.setenv("AGENT_INTAKE_AUTO_APPROVE", "true")
    from agent import client_sources as cs
    assert cs.intake_status() == "approved"


def test_auto_approved_intake_is_immediately_draftable(monkeypatch, tmp_path):
    """The whole point: a gym that submits intake can build a calendar straight away."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_INTAKE_AUTO_APPROVE", "true")
    from agent import client_sources as cs
    cs.submit_intake("newgym_ig", {"offer": ["Their real front door offer"]},
                     status=cs.intake_status())
    assert [s.text for s in cs.approved_sources("newgym_ig")] == \
        ["Their real front door offer"]
    assert cs.pending_sources("newgym_ig") == []


def test_the_post_approval_gate_is_untouched_by_auto_approve(monkeypatch):
    """Auto-approving SOURCES must not be confused with auto-approving POSTS. Every
    drafted post still waits for the client's tap."""
    monkeypatch.setenv("AGENT_INTAKE_AUTO_APPROVE", "true")
    from agent import config
    assert config.intake_auto_approve() is True
    # the publish-side gates are entirely separate switches and stay off
    assert config.publish_enabled() is False
