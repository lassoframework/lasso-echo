"""scene_claim_wave: prep emits candidate phash evidence but NEVER marks used.

The claim-wave redesign item (a): preparation may stage candidate scene
(pHash) evidence, but prep is not a usage decision — nothing at prep time may
record a scene as used. These tests pin the CURRENT writer behavior in
agent/visual_writer_prepare.py:

* the scene-fingerprint helper is advisory: never raises, never gates,
  returns a namespaced 'scene:phash64:<16 hex>' or None;
* the prep evidence payloads carry scene_fingerprint as metadata next to the
  md5 byte identity, which stays the only authority;
* prep calls ONLY the byte-identity RPCs (visual_global_prepare_bundle /
  visual_global_prepare_source_rendition) — no scene write RPC, no
  'used'/claim surface, and every result keeps usage_claimed False.

Contract-dependent checks for the wave staging writer skip cleanly when the
target object is absent (see tests/test_scene_claim_wave_sql.py); these tests
never silently pass against missing objects.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import visual_scene  # noqa: E402
from agent import visual_writer_prepare as vwp  # noqa: E402


class _FakePng:
    """Deterministic 'image bytes' the real dct_phash cannot decode — used to
    prove undecodable bytes record null evidence, and (via monkeypatch) to
    prove a decodable scene lands in the evidence payload."""


def test_scene_helper_is_advisory_and_never_raises():
    for data in (None, b"", b"not an image", 123, object()):
        assert vwp._scene_fingerprint(data) is None


def test_scene_helper_returns_namespaced_fingerprint_for_decodable_bytes(monkeypatch):
    monkeypatch.setattr(visual_scene, "scene_fingerprint",
                        lambda data: "scene:phash64:" + "a" * 16)
    fp = vwp._scene_fingerprint(b"png-bytes")
    assert fp == "scene:phash64:" + "a" * 16


def test_scene_helper_swallows_classifier_errors(monkeypatch):
    def _boom(data):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(visual_scene, "scene_fingerprint", _boom)
    assert vwp._scene_fingerprint(b"png-bytes") is None


def test_raw_source_registration_emits_scene_evidence_without_any_scene_write(monkeypatch):
    """Prep evidence carries the advisory scene fingerprint alongside the md5
    identity, and the ONLY RPC is the byte-identity bundle RPC — no scene
    record/write/claim RPC exists or is called at prep time."""
    calls = []

    def fake_rpc(store, name, arguments):
        calls.append((name, arguments))
        assert name == "visual_global_prepare_bundle"
        return {"fingerprint": arguments["p_fingerprint"], "group_key": "vg_test"}

    monkeypatch.setattr(vwp, "_rpc", fake_rpc)
    monkeypatch.setattr(vwp, "_own_media_url", lambda url: True)
    monkeypatch.setattr(visual_scene, "scene_fingerprint",
                        lambda data: "scene:phash64:" + "b" * 16)

    source = b"raw-object-bytes"
    group = vwp._register_raw_source(
        store=None, tenant="11111111-2222-3333-4444-555555555555",
        prepared={}, source_url="https://media.example/x.png",
        source=source, asset=None)

    assert group == "vg_test"
    assert [name for name, _ in calls] == ["visual_global_prepare_bundle"]
    args = calls[0][1]
    evidence = args["p_evidence"]
    # advisory scene evidence is present, byte identity stays the authority
    assert evidence["scene_fingerprint"] == "scene:phash64:" + "b" * 16
    assert evidence["verified_bytes"] == args["p_fingerprint"]
    assert args["p_fingerprint"].startswith("md5:")
    # nothing in the prep payload claims or stages usage
    assert "p_used_date" not in args
    assert "usage" not in evidence
    assert not any("scene" in name for name, _ in calls)


def test_raw_source_registration_records_null_evidence_for_undecodable_bytes(monkeypatch):
    calls = []

    def fake_rpc(store, name, arguments):
        calls.append((name, arguments))
        return {"fingerprint": arguments["p_fingerprint"], "group_key": "vg_test"}

    monkeypatch.setattr(vwp, "_rpc", fake_rpc)
    monkeypatch.setattr(vwp, "_own_media_url", lambda url: True)

    group = vwp._register_raw_source(
        store=None, tenant="11111111-2222-3333-4444-555555555555",
        prepared={}, source_url="https://media.example/x.png",
        source=b"not-decodable", asset=None)

    assert group == "vg_test"
    assert calls[0][1]["p_evidence"]["scene_fingerprint"] is None


def test_prep_module_has_no_scene_write_path():
    """No prep-time scene record RPC is called from Python: the rejected
    record functions are never referenced, and the only scene surface is
    advisory candidate staging evidence."""
    import inspect

    source = inspect.getsource(vwp)
    assert "visual_scene_record_use" not in source
    assert "visual_scene_phash" not in source
    assert "scene_fingerprint" in source  # advisory evidence only


# ---- wave staging writer (landed): candidate evidence, never used -----------

TENANT = "11111111-2222-3333-4444-555555555555"
GOOD_FP = "scene:phash64:" + "c" * 16
OBJ = ("source", "https://media.example/x.png", "md5:" + "1" * 32, 1234, GOOD_FP)


def test_candidate_emission_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_SCENE_CANDIDATE", raising=False)
    assert vwp._scene_candidate(TENANT, "vg_a", [OBJ]) is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_candidate_emission_explicit_off(monkeypatch, value):
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", value)
    assert vwp._scene_candidate(TENANT, "vg_a", [OBJ]) is None


def test_candidate_payload_is_staging_only_and_never_counts_as_use(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", "1")
    payload = vwp._scene_candidate(TENANT, "vg_a", [OBJ])
    assert payload["kind"] == "visual_scene_candidate"
    assert payload["stage"] == "candidate"
    assert payload["usage_claimed"] is False
    assert payload["counts_as_use"] is False
    assert payload["excludes_candidates"] is False
    assert payload["tenant_id"] == TENANT and payload["group_key"] == "vg_a"
    (entry,) = payload["objects"]
    assert entry["phash"] == "c" * 16          # bare 16-hex for staging
    assert entry["fingerprint"].startswith("md5:")
    assert entry["stageable"] is True


def test_candidate_payload_ambiguous_flag_arms_fail_closed(monkeypatch):
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", "sometimes")
    assert vwp._scene_candidate(TENANT, "vg_a", [OBJ]) is not None


def test_candidate_undecodable_object_is_not_stageable_never_distinct(monkeypatch):
    """An object whose bytes did not decode carries phash=None and
    stageable=False: the NOT-NULL staging table can never take it, and null
    pHash evidence is unknown — fail closed, never DISTINCT."""
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", "1")
    bad = ("delivered", "https://media.example/y.png", "md5:" + "2" * 32, 99, None)
    payload = vwp._scene_candidate(TENANT, "vg_a", [OBJ, bad])
    good_entry, bad_entry = payload["objects"]
    assert good_entry["stageable"] is True
    assert bad_entry["phash"] is None
    assert bad_entry["stageable"] is False


@pytest.mark.parametrize("scene_fp", ["garbage", "scene:phash64:xyz",
                                      "scene:phash64:" + "c" * 15, 42])
def test_candidate_malformed_scene_fingerprint_is_not_stageable(monkeypatch, scene_fp):
    monkeypatch.setenv("AGENT_VISUAL_SCENE_CANDIDATE", "1")
    obj = ("source", "https://media.example/x.png", "md5:" + "1" * 32, 1, scene_fp)
    (entry,) = vwp._scene_candidate(TENANT, "vg_a", [obj])["objects"]
    assert entry["stageable"] is False
    assert entry["phash"] is None
