"""
DAM v1 tests. Offline, adversarial, zero spend (injected phash + reader).
Asserts: consent exclusion is ABSOLUTE (a consented=false asset is never
selected even as the only asset); unknown fails safe; near-dupes collapse to
one rotation key so the window blocks the group; auto-tag writes sidecars with
review=true on low confidence; everything inert while the flags are OFF.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, dam, rotation  # noqa: E402


def _lib(tmp_path, cards):
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    for name, note, sidecar in cards:
        (lib / name).write_bytes(b"img-" + name.encode())
        (lib / (os.path.splitext(name)[0] + ".txt")).write_text(note, encoding="utf-8")
        if sidecar is not None:
            (lib / (os.path.splitext(name)[0] + ".json")).write_text(
                json.dumps(sidecar), encoding="utf-8")
    return str(lib)


def _arm_rotation(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ROTATION_ENABLED", "true")
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


# ---- consent guard: absolute, fail safe -------------------------------------------
def test_consent_denied_never_selected_even_as_only_asset(monkeypatch, tmp_path):
    _arm_rotation(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_CONSENT_GUARD_ENABLED", "true")
    lib = _lib(tmp_path, [
        ("lasso_p1_member.jpg", "A member story.",
         {"people": True, "consent": "denied"}),
    ])
    kind, payload = rotation.choose("lasso_ig", "2026-07-06", lib)
    assert payload is None                      # the ONLY asset, still never selected
    # across the week too
    for day in ("2026-07-07", "2026-07-08"):
        k, c = rotation.choose("lasso_ig", day, lib)
        assert c is None


def test_unknown_people_or_consent_fails_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONSENT_GUARD_ENABLED", "true")
    lib = _lib(tmp_path, [
        ("a.jpg", "n", None),                                # no sidecar at all
        ("b.jpg", "n", {"people": True}),                    # people, no consent
        ("c.jpg", "n", {"people": True, "consent": "asked"}),
        ("d.jpg", "n", {"people": False}),                   # no faces: fine
        ("e.jpg", "n", {"people": True, "consent": "granted"}),
    ])
    blocked = {n: dam.consent_blocked(os.path.join(lib, n))
               for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg")}
    assert blocked == {"a.jpg": True, "b.jpg": True, "c.jpg": True,
                       "d.jpg": False, "e.jpg": False}


def test_consent_guard_inert_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CONSENT_GUARD_ENABLED", raising=False)
    lib = _lib(tmp_path, [("a.jpg", "n", {"people": True, "consent": "denied"})])
    assert dam.consent_blocked(os.path.join(lib, "a.jpg")) is False


# ---- near-dupe collapse ---------------------------------------------------------------
def test_near_dupes_collapse_rotation_keys(monkeypatch, tmp_path):
    _arm_rotation(monkeypatch, tmp_path)
    lib = _lib(tmp_path, [
        ("lasso_p1_shot_a.jpg", "clean", None),
        ("lasso_p1_shot_b.jpg", "clean", None),   # near-identical to shot_a
        ("lasso_p2_other.jpg", "clean", None),
    ])
    fake_phash = lambda data: ("SAME" if b"shot" in data else "OTHER")
    groups = dam.mark_near_dupes(lib, phash=fake_phash)
    assert list(groups.values()) == [["lasso_p1_shot_a.jpg", "lasso_p1_shot_b.jpg"]]
    # both members now share one rotation key
    key_a = dam.rotation_key(os.path.join(lib, "lasso_p1_shot_a.jpg"))
    key_b = dam.rotation_key(os.path.join(lib, "lasso_p1_shot_b.jpg"))
    assert key_a == key_b == "lasso_p1_shot_a.jpg"
    # serving one blocks the WHOLE group inside the window
    rotation.record_served("lasso_ig", key_a, "p1", "2026-07-06")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert os.path.basename(creative.path) == "lasso_p2_other.jpg"


# ---- auto-tag ----------------------------------------------------------------------------
def _reader(payload):
    return lambda image_bytes: json.dumps(payload)


def test_autotag_writes_sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_AUTOTAG_ENABLED", "true")
    lib = _lib(tmp_path, [("member.jpg", "note text", {"note": "note text"})])
    out = dam.autotag(os.path.join(lib, "member.jpg"),
                      reader=_reader({"tags": ["Gym", "MEMBER"], "people": True,
                                      "description": "A member training.",
                                      "confidence": 0.93}))
    assert out["tags"] == ["gym", "member"]
    assert out["people"] is True and "review" not in out
    assert out["note"] == "note text"          # merge preserved the existing field


def test_autotag_low_confidence_marks_review(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_AUTOTAG_ENABLED", "true")
    lib = _lib(tmp_path, [("blur.jpg", "n", None)])
    out = dam.autotag(os.path.join(lib, "blur.jpg"),
                      reader=_reader({"tags": [], "people": False,
                                      "description": "unclear", "confidence": 0.3}))
    assert out["review"] is True


def test_autotag_inert_when_off_and_counts_spend(monkeypatch, tmp_path):
    lib = _lib(tmp_path, [("x.jpg", "n", None)])
    monkeypatch.delenv("AGENT_AUTOTAG_ENABLED", raising=False)
    exploding = lambda b: (_ for _ in ()).throw(AssertionError("spend while OFF"))
    assert dam.autotag(os.path.join(lib, "x.jpg"), reader=exploding) is None
    # armed + spend cap already reached: no call, no spend
    monkeypatch.setenv("AGENT_AUTOTAG_ENABLED", "true")
    monkeypatch.setenv("AGENT_SPEND_CAP_ENABLED", "true")
    monkeypatch.setenv("AGENT_GEMINI_DAILY_CAP", "0")
    assert dam.autotag(os.path.join(lib, "x.jpg"), reader=exploding,
                       day="2026-07-06") is None
