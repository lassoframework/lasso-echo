"""Tests for agent/scene_original_bytes — pure byte-evidence for proving
original-source use of a delivered render. Offline by convention (conftest
strips creds/flags); test images are generated in-memory via Pillow, matching
tests/test_vision.py's fixture style.

Core contract under test: byte identity is the ONLY provable outcome. Any
byte-differing delivery is HELD_UNPROVEN — even with a perfectly-formed,
digest-matching RenderEvidence, because caller-created evidence is forgeable
and pHash similarity is not causal proof of rendering."""

import hashlib
import io

import pytest

from agent import scene_original_bytes as sob


def _img_bytes(pixel_fn, *, size=(64, 64), fmt="PNG"):
    """A deterministic non-flat image (a flat image hits dct_phash's
    low-variance guard, which is fine evidence-wise but a boring test)."""
    from PIL import Image
    img = Image.new("L", size)
    img.putdata([pixel_fn(x, y) for y in range(size[1]) for x in range(size[0])])
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _original():
    # deterministic texture strictly inside 40..210 (no mod-wrap discontinuity,
    # which rings differently through the DCT — see test_vision.py's notes)
    return _img_bytes(lambda x, y: 40 + (x * 7 + y * 13) % 170)


def _render_of_original():
    """A byte-differing but visually-near render: same texture, +8 brightness,
    no clipping (mirrors test_vision.py's stable near-dupe fixture)."""
    return _img_bytes(lambda x, y: 40 + (x * 7 + y * 13) % 170 + 8)


def _recompressed_same_image():
    """Adversarial: the SAME image re-saved as JPEG (quality 85) — bytes differ,
    pHash distance 0, yet not byte-identical to the original and not provably a
    render of it by any causal account this module can verify."""
    from PIL import Image
    img = Image.open(io.BytesIO(_original()))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _different_image():
    from PIL import Image
    import random
    rng = random.Random(7)
    img = Image.new("L", (64, 64))
    img.putdata([rng.randint(40, 210) for _ in range(64 * 64)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _good_render_evidence(orig, rend):
    return sob.RenderEvidence(
        renderer="pillow-brightness-shift",
        original_sha256=hashlib.sha256(orig).hexdigest(),
        delivered_sha256=hashlib.sha256(rend).hexdigest(),
        attestation="same texture, +8 brightness, PNG re-save")


# ---- primitives --------------------------------------------------------------
def test_sha256_hex_of_bytes():
    data = _original()
    assert sob.sha256_hex(data) == hashlib.sha256(data).hexdigest()
    assert sob.sha256_hex(bytearray(data)) == hashlib.sha256(data).hexdigest()


def test_phash_hex_matches_repo_convention():
    from agent import vision
    data = _original()
    h = sob.phash_hex(data)
    assert h == vision.dct_phash(data)
    assert isinstance(h, str) and len(h) == 16
    int(h, 16)  # 64-bit hex


# ---- rejection: missing / empty / unreadable / wrong type ---------------------
@pytest.mark.parametrize("bad", [None, b"", bytearray(), "not-bytes", 42])
def test_byte_rejection_shapes(bad):
    with pytest.raises(sob.ByteEvidenceError):
        sob.sha256_hex(bad)
    with pytest.raises(sob.ByteEvidenceError):
        sob.build_byte_evidence(bad)


def test_unreadable_image_bytes_rejected():
    garbage = b"\x89PNG-not-actually-a-png" * 8
    with pytest.raises(sob.ByteEvidenceError, match="readable image"):
        sob.phash_hex(garbage)
    with pytest.raises(sob.ByteEvidenceError):
        sob.build_byte_evidence(garbage)
    with pytest.raises(sob.ByteEvidenceError):
        sob.prove_original_use(garbage, garbage)


# ---- declared digest: fail closed --------------------------------------------
def test_declared_digest_match_ok():
    data = _original()
    ev = sob.build_byte_evidence(data, declared_sha256=hashlib.sha256(data).hexdigest())
    assert ev.sha256 == hashlib.sha256(data).hexdigest()
    assert ev.byte_len == len(data)
    assert len(ev.phash) == 16


def test_declared_digest_mismatch_rejected_fail_closed():
    data = _original()
    wrong = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(sob.ByteEvidenceError, match="does not match"):
        sob.build_byte_evidence(data, declared_sha256=wrong)
    # ... including via the prove entry point, on EITHER side
    with pytest.raises(sob.ByteEvidenceError):
        sob.prove_original_use(data, data, declared_original_sha256=wrong)
    with pytest.raises(sob.ByteEvidenceError):
        sob.prove_original_use(data, data, declared_delivered_sha256=wrong)


# ---- identical bytes: the ONLY provable outcome ------------------------------
def test_identical_bytes_prove_without_render_evidence():
    data = _original()
    res = sob.prove_original_use(data, data)
    assert res["status"] == sob.PROVEN_IDENTICAL
    assert res["proven"] is True and res["identical"] is True
    assert res["render_evidence"] is None
    assert res["original"].sha256 == res["delivered"].sha256


# ---- differing bytes: ALWAYS held, render evidence can never upgrade ---------
def test_differing_bytes_without_render_evidence_held():
    res = sob.prove_original_use(_original(), _render_of_original())
    assert res["status"] == sob.HELD_UNPROVEN
    assert res["proven"] is False and res["identical"] is False
    assert res["render_evidence"] is None
    assert "no trusted renderer" in res["reason"]


def test_differing_bytes_with_valid_render_evidence_STILL_HELD():
    """A perfectly-formed, digest-matching RenderEvidence CANNOT prove a
    differing-byte delivery — it is structure-checked, attached for the record,
    and the outcome stays HELD_UNPROVEN."""
    orig, rend = _original(), _render_of_original()
    re_ev = _good_render_evidence(orig, rend)
    res = sob.prove_original_use(orig, rend, render_evidence=re_ev)
    assert res["status"] == sob.HELD_UNPROVEN
    assert res["proven"] is False
    assert res["render_evidence"] is re_ev  # recorded, not believed


def test_differing_bytes_with_structurally_invalid_evidence_raises():
    """Malformed evidence fails closed with an exception rather than silently
    recording garbage."""
    orig, rend = _original(), _render_of_original()
    bad = hashlib.sha256(b"lie").hexdigest()
    good_d = hashlib.sha256(rend).hexdigest()
    good_o = hashlib.sha256(orig).hexdigest()
    base = dict(renderer="r", attestation="a")
    for re_ev in (sob.RenderEvidence(original_sha256=bad, delivered_sha256=good_d, **base),
                  sob.RenderEvidence(original_sha256=good_o, delivered_sha256=bad, **base),
                  sob.RenderEvidence(original_sha256=good_o, delivered_sha256=good_d,
                                     renderer="", attestation="a"),
                  sob.RenderEvidence(original_sha256=good_o, delivered_sha256=good_d,
                                     renderer="r", attestation="  "),
                  "not-a-render-evidence-object"):
        with pytest.raises(sob.ByteEvidenceError):
            sob.prove_original_use(orig, rend, render_evidence=re_ev)


# ---- adversarial: pHash-similar impostors never prove ------------------------
def test_recompressed_same_image_never_proves():
    """Same image recompressed: pHash distance 0, bytes differ.
    Even WITH matching-structure render evidence, never proven."""
    from agent import vision
    orig, shifted = _original(), _recompressed_same_image()
    assert vision.hamming(sob.phash_hex(orig), sob.phash_hex(shifted)) <= 6  # similar!
    res = sob.prove_original_use(orig, shifted)
    assert res["proven"] is False and res["status"] == sob.HELD_UNPROVEN
    re_ev = _good_render_evidence(orig, shifted)
    res2 = sob.prove_original_use(orig, shifted, render_evidence=re_ev)
    assert res2["proven"] is False and res2["status"] == sob.HELD_UNPROVEN


def test_brightness_edited_same_scene_never_proves():
    """A real edit of the same scene (the +8 render) is exactly the case a
    trusted renderer would one day cover — but no trusted renderer exists, so
    it stays held even with forged-but-perfect evidence."""
    orig, rend = _original(), _render_of_original()
    assert orig != rend
    res = sob.prove_original_use(orig, rend,
                                 render_evidence=_good_render_evidence(orig, rend))
    assert res["proven"] is False


def test_different_photo_never_proves():
    orig, other = _original(), _different_image()
    res = sob.prove_original_use(orig, other,
                                 render_evidence=_good_render_evidence(orig, other))
    assert res["proven"] is False and res["status"] == sob.HELD_UNPROVEN


def test_forgery_attempt_cannot_self_certify():
    """An attacker who controls BOTH byte strings and the evidence still gets
    nothing: forged evidence over attacker-chosen bytes is held, and only true
    byte identity proves."""
    fake_orig = _different_image()
    fake_rend = _recompressed_same_image()
    forged = sob.RenderEvidence(
        renderer="totally-real-renderer",
        original_sha256=hashlib.sha256(fake_orig).hexdigest(),
        delivered_sha256=hashlib.sha256(fake_rend).hexdigest(),
        attestation="trust me")
    res = sob.prove_original_use(fake_orig, fake_rend, render_evidence=forged)
    assert res["proven"] is False


# ---- validate_render_evidence: structure-only, proves nothing -----------------
def test_validate_render_evidence_structure_only():
    orig, rend = _original(), _render_of_original()
    o_ev = sob.build_byte_evidence(orig)
    d_ev = sob.build_byte_evidence(rend)
    re_ev = _good_render_evidence(orig, rend)
    assert sob.validate_render_evidence(re_ev, original=o_ev, delivered=d_ev) is re_ev
    # structural validity notwithstanding, the prove outcome is still held
    assert sob.prove_original_use(orig, rend, render_evidence=re_ev)["proven"] is False


# ---- purity --------------------------------------------------------------------
def test_no_network_or_io_imports():
    import inspect
    src = inspect.getsource(sob)
    for forbidden in ("requests", "urllib", "http", "socket", "open(", "urlopen"):
        assert forbidden not in src
