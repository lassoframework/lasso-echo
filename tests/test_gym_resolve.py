"""Tests for agent/gym_resolve.py: gym name + owner resolution order and
confidence labeling (Part A of the auto welcome-post spec)."""

import json
import os

import pytest

from agent import gym_resolve as gr


class _Customer:
    def __init__(self, id="cus_1", email="", name="", metadata=None):
        self.id = id
        self.email = email
        self.name = name
        self.metadata = metadata or {}


# ---------------------------------------------------------------------------
# owner name normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("RYAN PARR", "Ryan Parr"),
    ("Just Estes", "Just Estes"),
    ("mary o'brien", "Mary O'Brien"),
    ("jean-luc picard", "Jean-Luc Picard"),
    ("mcdonald", "McDonald"),
    ("MACGREGOR", "MacGregor"),
    ("", ""),
    ("   ", ""),
])
def test_normalize_owner_name(raw, expected):
    assert gr.normalize_owner_name(raw) == expected


# ---------------------------------------------------------------------------
# domain -> gym name inference
# ---------------------------------------------------------------------------

def test_domain_to_gym_name_hyphenated():
    assert gr.domain_to_gym_name("owner@bird-dog-crossfit.com") == "Bird Dog Crossfit"


def test_domain_to_gym_name_smashed_with_known_suffix():
    name = gr.domain_to_gym_name("owner@birddogcrossfit.com")
    assert name.lower().endswith("crossfit")
    assert name.split()[0].lower() == "birddog"


def test_domain_to_gym_name_no_known_suffix_falls_back_to_capitalized_stem():
    assert gr.domain_to_gym_name("owner@acmestudio.com") == "Acmestudio"


def test_domain_to_gym_name_from_url():
    assert gr.domain_to_gym_name("https://www.acmegym.com/about") == "Acme Gym"


def test_domain_to_gym_name_empty_input():
    assert gr.domain_to_gym_name("") == ""
    assert gr.domain_to_gym_name(None) == ""


def test_website_from_email():
    assert gr.website_from_email("owner@acmegym.com") == "https://acmegym.com"
    assert gr.website_from_email("not-an-email") == ""
    assert gr.website_from_email("") == ""


# ---------------------------------------------------------------------------
# portal tenant matching
# ---------------------------------------------------------------------------

def _write_tenant(base_dir, key, name, approver_name):
    tdir = os.path.join(base_dir, key)
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "tenant.json"), "w", encoding="utf-8") as fh:
        json.dump({"key": key, "name": name, "approver_name": approver_name}, fh)


def test_match_portal_tenant_by_metadata_account_key(tmp_path):
    _write_tenant(str(tmp_path), "acme_gym", "Acme Gym", "Jordan Blake")
    cust = _Customer(metadata={"account_key": "acme_gym"})
    key, rec = gr.match_portal_tenant(cust, base_dir=str(tmp_path))
    assert key == "acme_gym"
    assert rec["name"] == "Acme Gym"


def test_match_portal_tenant_by_exact_name(tmp_path):
    _write_tenant(str(tmp_path), "acme_gym", "Acme Gym", "Jordan Blake")
    cust = _Customer(name="acme gym")  # case-insensitive exact match
    key, rec = gr.match_portal_tenant(cust, base_dir=str(tmp_path))
    assert key == "acme_gym"


def test_match_portal_tenant_no_match_returns_none(tmp_path):
    _write_tenant(str(tmp_path), "acme_gym", "Acme Gym", "Jordan Blake")
    cust = _Customer(name="Totally Different Gym")
    key, rec = gr.match_portal_tenant(cust, base_dir=str(tmp_path))
    assert key is None and rec is None


# ---------------------------------------------------------------------------
# resolve_gym: full order + confidence
# ---------------------------------------------------------------------------

def test_resolve_gym_portal_wins_confirmed(tmp_path):
    _write_tenant(str(tmp_path), "acme_gym", "Acme Gym", "RYAN PARR")
    cust = _Customer(email="ryan@acmegym.com", name="Acme Gym LLC",
                     metadata={"account_key": "acme_gym"})
    res = gr.resolve_gym(cust, base_dir=str(tmp_path))
    assert res.confidence == gr.CONFIRMED
    assert res.source == "portal"
    assert res.gym_name == "Acme Gym"
    assert res.owner_name == "Ryan Parr"
    assert res.account_key == "acme_gym"


def test_resolve_gym_stripe_business_name_confirmed(tmp_path):
    cust = _Customer(email="owner@somewhere.com", name="Somewhere Fitness")
    res = gr.resolve_gym(cust, base_dir=str(tmp_path))
    assert res.confidence == gr.CONFIRMED
    assert res.source == "stripe_business_name"
    assert res.gym_name == "Somewhere Fitness"


def test_resolve_gym_domain_inference_is_inferred(tmp_path):
    cust = _Customer(email="owner@acmegym.com", name="")
    res = gr.resolve_gym(cust, base_dir=str(tmp_path))
    assert res.confidence == gr.INFERRED
    assert res.source == "email_domain"
    assert res.gym_name == "Acme Gym"
    assert "confirm" in res.note.lower()


def test_resolve_gym_web_search_fallback_is_inferred(tmp_path):
    cust = _Customer(id="cus_9", email="", name="")

    def fake_search(query):
        return {"name": "Found Gym", "website": "https://foundgym.com"}

    res = gr.resolve_gym(cust, base_dir=str(tmp_path), search_fn=fake_search)
    assert res.confidence == gr.INFERRED
    assert res.source == "web_search"
    assert res.gym_name == "Found Gym"


def test_resolve_gym_unresolved_when_nothing_matches(tmp_path):
    cust = _Customer(id="cus_9", email="", name="")
    res = gr.resolve_gym(cust, base_dir=str(tmp_path), search_fn=lambda q: None)
    assert res.source == "unresolved"
    assert res.gym_name == ""
