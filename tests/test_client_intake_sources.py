"""
Intake to source docs (Part 2). A client's submitted intake lands as PENDING
per-account sources, held for human approval; an unapproved source is never in
the approved set the drafting path reads. Fully OFFLINE (tmp sqlite).
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


_BUNDLE = {
    "offer": ["6 week challenge for $199", ("Free intro session", "website /start")],
    "service": ["Small group personal training"],
    "testimonial": [("I lost 30 pounds in 3 months", "member Sarah M")],
}


# ---- 1. submitted intake becomes PENDING per-account sources -------------------
def test_intake_lands_as_pending():
    created = cs.submit_intake("gym_alpha_ig", _BUNDLE)
    assert len(created) == 4
    assert all(s.status == "pending" for s in created)
    assert all(s.account_key == "gym_alpha_ig" for s in created)
    # visible as pending, and scoped to this account
    assert len(cs.pending_sources("gym_alpha_ig")) == 4
    assert cs.pending_sources("gym_beta_ig") == []
    # the explicit citation is kept; the bare string gets the intake default
    offers = cs.pending_sources("gym_alpha_ig", category="offer")
    cites = {s.text: s.citation for s in offers}
    assert cites["Free intro session"] == "website /start"
    assert cites["6 week challenge for $199"] == "intake:gym_alpha_ig"


# ---- 2. an unapproved source is NEVER in the approved set (used by drafts) -----
def test_pending_never_in_approved_set():
    cs.submit_intake("gym_alpha_ig", _BUNDLE)
    # the drafting path only ever reads approved_sources / approved_claims
    assert cs.approved_sources("gym_alpha_ig") == []
    assert cs.approved_claims("gym_alpha_ig") == []
    # a claim-bearing pending fact does not clear the gate for this account
    assert "6 week challenge for $199" not in cs.approved_claims("gym_alpha_ig")


# ---- 3. after human approval, the same sources become usable ------------------
def test_approval_promotes_pending_to_usable():
    cs.submit_intake("gym_alpha_ig", _BUNDLE)
    n = cs.approve_all("gym_alpha_ig")
    assert n == 4
    assert cs.pending_sources("gym_alpha_ig") == []
    assert len(cs.approved_sources("gym_alpha_ig")) == 4
    assert "6 week challenge for $199" in cs.approved_claims("gym_alpha_ig")


# ---- 4. approving one source leaves the rest pending --------------------------
def test_approve_single_source():
    created = cs.submit_intake("gym_alpha_ig", {"offer": ["A", "B"]})
    assert cs.approve_source(created[0].id) is True
    approved = [s.text for s in cs.approved_sources("gym_alpha_ig")]
    pending = [s.text for s in cs.pending_sources("gym_alpha_ig")]
    assert approved == ["A"]
    assert pending == ["B"]


# ---- 5. a bad category stores nothing (all-or-nothing) ------------------------
def test_unknown_category_stores_nothing():
    with pytest.raises(ValueError):
        cs.submit_intake("gym_alpha_ig",
                         {"offer": ["kept?"], "nonsense": ["bad"]})
    assert cs.all_sources("gym_alpha_ig") == []   # nothing landed


# ---- 6. blank items are skipped ----------------------------------------------
def test_blank_items_skipped():
    created = cs.submit_intake("gym_alpha_ig",
                               {"offer": ["real", "  ", "", ("  ", "cite")]})
    assert [s.text for s in created] == ["real"]


# ---- 7. intake can be landed already-approved when a human is the submitter ----
def test_intake_can_land_approved():
    created = cs.submit_intake("gym_alpha_ig", {"about": ["Est. 2015"]},
                               status="approved")
    assert created[0].status == "approved"
    assert len(cs.approved_sources("gym_alpha_ig")) == 1
