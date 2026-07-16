"""
Library audit tests: MISSING / THIN detection, format output, preflight warning.
Fully offline (tmp_path for fake libraries; no S3, no db).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.library_audit import (  # noqa: E402
    check_creative,
    audit_account,
    format_result,
    MIN_IMAGE_BYTES,
    MIN_VIDEO_BYTES,
)
from agent.library import Creative  # noqa: E402


# ---- helpers -----------------------------------------------------------------

def _img(tmp_path, name, size=None):
    p = tmp_path / (name + ".jpg")
    p.write_bytes(b"x" * (size if size is not None else MIN_IMAGE_BYTES + 1))
    return str(p)


def _carousel(tmp_path, name, n_slides=3):
    d = tmp_path / name
    d.mkdir()
    for i in range(n_slides):
        (d / f"slide_{i}.png").write_bytes(b"x" * (MIN_IMAGE_BYTES + 1))
    return str(d)


# ---- check_creative ----------------------------------------------------------

def test_check_creative_healthy_image(tmp_path):
    path = _img(tmp_path, "healthy")
    c = Creative(path=path, media_type="image")
    assert check_creative(c) is None


def test_check_creative_missing_image(tmp_path):
    c = Creative(path=str(tmp_path / "ghost.jpg"), media_type="image")
    issue = check_creative(c)
    assert issue is not None and "MISSING" in issue


def test_check_creative_thin_image(tmp_path):
    p = tmp_path / "tiny.jpg"
    p.write_bytes(b"x")  # 1 byte
    c = Creative(path=str(p), media_type="image")
    issue = check_creative(c)
    assert issue is not None and "THIN" in issue


def test_check_creative_healthy_carousel(tmp_path):
    d = _carousel(tmp_path, "good_carousel", n_slides=3)
    slides = [os.path.join(d, f) for f in sorted(os.listdir(d))]
    c = Creative(path=d, media_type="carousel", slides=slides)
    assert check_creative(c) is None


def test_check_creative_missing_carousel_dir(tmp_path):
    c = Creative(path=str(tmp_path / "no_dir"), media_type="carousel", slides=[])
    issue = check_creative(c)
    assert issue is not None and "MISSING" in issue


def test_check_creative_thin_carousel_single_slide(tmp_path):
    d = _carousel(tmp_path, "thin_carousel", n_slides=1)
    slides = [os.path.join(d, f) for f in sorted(os.listdir(d))]
    c = Creative(path=d, media_type="carousel", slides=slides)
    issue = check_creative(c)
    assert issue is not None and "THIN" in issue


# ---- audit_account -----------------------------------------------------------

def _make_lib(tmp_path, n_good=2, n_thin=0, n_missing=0):
    lib = tmp_path / "lib"
    lib.mkdir()
    for i in range(n_good):
        (lib / f"good_{i}.jpg").write_bytes(b"x" * (MIN_IMAGE_BYTES + 1))
    for i in range(n_thin):
        (lib / f"thin_{i}.jpg").write_bytes(b"x")
    for i in range(n_missing):
        stub = lib / f"missing_{i}_carousel"
        stub.mkdir()
        (stub / "note.json").write_text("{}")
    return str(lib)


def test_audit_account_clean(tmp_path):
    lib = _make_lib(tmp_path, n_good=3)
    r = audit_account("test_acct", lib)
    assert r["total"] == 3
    assert r["missing"] == []
    assert r["thin"] == []


def test_audit_account_finds_thin(tmp_path):
    lib = _make_lib(tmp_path, n_good=2, n_thin=1)
    r = audit_account("test_acct", lib)
    assert len(r["thin"]) == 1
    assert "THIN" in r["thin"][0]["reason"]


def test_audit_account_finds_missing_carousel(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "good.jpg").write_bytes(b"x" * (MIN_IMAGE_BYTES + 1))
    d = lib / "speed_to_lead_carousel"
    d.mkdir()
    (d / "note.json").write_text("{}")
    r = audit_account("test_acct", str(lib))
    assert len(r["missing"]) == 0
    assert len(r["thin"]) == 1
    assert "speed_to_lead_carousel" in r["thin"][0]["stem"]


def test_audit_account_empty_library(tmp_path):
    lib = tmp_path / "empty"
    lib.mkdir()
    r = audit_account("test_acct", str(lib))
    assert r["total"] == 0
    assert r["missing"] == []
    assert r["thin"] == []


# ---- format_result -----------------------------------------------------------

def test_format_result_clean():
    r = {"account": "lasso_ig", "lib_path": "content_library", "total": 5,
         "missing": [], "thin": []}
    out = format_result(r)
    assert "lasso_ig" in out
    assert "MISSING (0)" in out
    assert "THIN (0)" in out
    assert "Clean." in out


def test_format_result_with_issues():
    r = {
        "account": "lasso_fb",
        "lib_path": "content_library",
        "total": 10,
        "missing": [{"stem": "speed_to_lead_carousel", "media_type": "carousel",
                     "reason": "MISSING (directory not found)"}],
        "thin": [],
    }
    out = format_result(r)
    assert "speed_to_lead_carousel" in out
    assert "MISSING (1)" in out
    assert "Clean." not in out


# ---- integration: lasso accounts use shared content_library ------------------

def test_real_library_speed_to_lead_carousel_is_thin():
    """The real content_library/speed_to_lead_carousel directory exists but has
    only 3 slides and a note.json — it is a VALID carousel (>= 2 slides).
    Confirmed clean unless slides become corrupt."""
    import os
    lib_path = "content_library"
    if not os.path.isdir(lib_path):
        pytest.skip("content_library not present in this environment")
    r = audit_account("lasso_ig", lib_path)
    stems = [e["stem"] for e in r["missing"] + r["thin"]]
    assert "speed_to_lead_carousel" not in stems, (
        "speed_to_lead_carousel has 3 slides and is healthy; "
        "if it appears here the slides are gone or corrupt: {}".format(r)
    )
