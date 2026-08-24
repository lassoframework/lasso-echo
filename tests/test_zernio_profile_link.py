"""
zernio_profile_link tests (agent/zernio_profile_link.py), fully offline.

Pierce 2026-08-24: a fully-connected Zernio profile with an empty gyms.zernio_profile_id
silently never published. This module backfills that column by matching the Zernio profile
name to the gym base. Covers: flag gate, links only empty gyms, never overwrites a set id,
handles no-profile, extracts the FB page id, and one gym's error never blocks the rest.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, zernio_profile_link as zpl


class _FakeDb:
    def __init__(self, rows):
        self.rows = {k: dict(v) for k, v in rows.items()}
        self.upserts = []

    def gym_get(self, key):
        return self.rows.get(key)

    def gym_upsert(self, account_key, **fields):
        self.upserts.append((account_key, fields))
        self.rows.setdefault(account_key, {}).update(fields)


class _FakeZernio:
    def __init__(self, profiles, accounts=None, raise_for=None):
        self._profiles = profiles           # {name: profile_id}
        self._accounts = accounts or {}     # {profile_id: accounts_json}
        self._raise_for = raise_for or set()

    def find_profile_id(self, name):
        if name in self._raise_for:
            raise RuntimeError("boom")
        return self._profiles.get(name)

    def list_accounts(self, profile_id):
        return self._accounts.get(profile_id, {"accounts": []})


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_ZERNIO_PROFILE_LINK", "true")


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.delenv("AGENT_ZERNIO_PROFILE_LINK", raising=False)
    out = zpl.link_client_profiles(bases=["pierce"], zernio=_FakeZernio({}), db=_FakeDb({}))
    assert out["ok"] is False


def test_links_empty_gym_with_fb_page(armed):
    db = _FakeDb({"piercefitness": {"zernio_profile_id": ""}})
    z = _FakeZernio(
        {"piercefitness": "PID1"},
        accounts={"PID1": {"accounts": [
            {"platform": "facebook",
             "metadata": {"availablePages": [{"id": "661535357048979"}]}},
            {"platform": "instagram"},
        ]}},
    )
    out = zpl.link_client_profiles(bases=["piercefitness"], zernio=z, db=db)
    assert out["linked"] == 1
    assert db.rows["piercefitness"]["zernio_profile_id"] == "PID1"
    assert db.rows["piercefitness"]["zernio_default_fb_page_id"] == "661535357048979"


def test_never_overwrites_a_set_id(armed):
    db = _FakeDb({"eng": {"zernio_profile_id": "ALREADY"}})
    z = _FakeZernio({"eng": "SHOULD_NOT_WIN"})
    out = zpl.link_client_profiles(bases=["eng"], zernio=z, db=db)
    assert out["already"] == 1 and out["linked"] == 0
    assert db.rows["eng"]["zernio_profile_id"] == "ALREADY"
    assert db.upserts == []


def test_no_profile_is_skipped_not_errored(armed):
    db = _FakeDb({"ghost": {"zernio_profile_id": ""}})
    out = zpl.link_client_profiles(bases=["ghost"], zernio=_FakeZernio({}), db=db)
    assert out["no_profile"] == 1 and out["linked"] == 0
    assert db.upserts == []


def test_one_gym_error_never_blocks_the_rest(armed):
    db = _FakeDb({"bad": {"zernio_profile_id": ""}, "good": {"zernio_profile_id": ""}})
    z = _FakeZernio({"good": "PIDG"}, raise_for={"bad"})
    out = zpl.link_client_profiles(bases=["bad", "good"], zernio=z, db=db)
    assert out["errors"] == 1 and out["linked"] == 1
    assert db.rows["good"]["zernio_profile_id"] == "PIDG"


def test_fb_page_id_picks_single_available_page():
    assert zpl._fb_page_id({"accounts": [
        {"platform": "facebook", "metadata": {"availablePages": [{"id": "P1"}]}}]}) == "P1"
    # several unlabelled pages -> we do NOT guess
    assert zpl._fb_page_id({"accounts": [
        {"platform": "facebook",
         "metadata": {"availablePages": [{"id": "P1"}, {"id": "P2"}]}}]}) == ""
    # an explicit selected page wins
    assert zpl._fb_page_id({"accounts": [
        {"platform": "facebook", "metadata": {"selectedPageId": "SEL",
         "availablePages": [{"id": "P1"}, {"id": "P2"}]}}]}) == "SEL"
