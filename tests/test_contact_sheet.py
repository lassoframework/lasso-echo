"""
Contact sheet tests (operator hygiene Part B). Offline. Asserts: every concept
in the requested set appears exactly once (and --all covers the whole
library); stat concepts carry the numeral hint; the sheet's visible copy is
dash free (tags stripped; em and en dashes absent everywhere); the upload key
shape is echo/contact_sheets/<set>_<date>.html; the run is read only against
the library (sidecars byte untouched).
"""

import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, contact_sheet, regen_library  # noqa: E402

_DASH_RE = re.compile(r"[‐‑‒–—―−-]")
_TAG_RE = re.compile(r"<[^>]+>")


class FakeS3:
    def __init__(self):
        self.puts = []

    def exists(self, key):
        return False

    def put(self, key, local_path):
        self.puts.append((key, local_path))


def _seed_library(tmp_path, keys):
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    for key in keys:
        side = {"concept": key, "public_url": f"https://cdn.echo.test/{key}.png"}
        spec = regen_library.CONCEPTS[key]
        if spec.get("pillar"):
            side["pillar"] = spec["pillar"]
        (lib / f"lasso_v2_{key}.json").write_text(json.dumps(side))
    return str(lib)


def _b2b_keys():
    return [k for k, v in regen_library.CONCEPTS.items() if v.get("set") == "b2b"]


def test_every_concept_in_set_exactly_once(monkeypatch, tmp_path):
    lib = _seed_library(tmp_path, _b2b_keys())
    out = contact_sheet.run("b2b", s3_client=FakeS3(), library_path=lib)
    text = open(out["path"], encoding="utf-8").read()
    assert out["count"] == 21
    for key in _b2b_keys():
        assert text.count(f"<b>{key}</b>") == 1, key
    # --all covers the whole library, one entry per concept
    entries = contact_sheet.gather("all", lib)
    assert len(entries) == len(regen_library.CONCEPTS)
    assert len({e["key"] for e in entries}) == len(entries)


def test_stat_concepts_carry_numeral_hint(monkeypatch, tmp_path):
    lib = _seed_library(tmp_path, _b2b_keys())
    entries = {e["key"]: e for e in contact_sheet.gather("b2b", lib)}
    cited = {k for k, v in regen_library.CONCEPTS.items() if v.get("cite")}
    for key, e in entries.items():
        if key in cited:
            assert e["hint"] == contact_sheet.STAT_HINT, key
            assert "character by character" in e["hint"]
        else:
            assert e["hint"] == contact_sheet.DEFAULT_HINT, key


def test_visible_copy_dash_free(monkeypatch, tmp_path):
    lib = _seed_library(tmp_path, _b2b_keys())
    out = contact_sheet.run("all", s3_client=FakeS3(), library_path=lib)
    text = open(out["path"], encoding="utf-8").read()
    assert "—" not in text and "–" not in text        # no em/en dash anywhere
    visible = _TAG_RE.sub(" ", text)                  # the copy a reviewer reads
    assert not _DASH_RE.search(visible), _DASH_RE.search(visible)


def test_upload_key_shape_and_read_only(monkeypatch, tmp_path):
    lib = _seed_library(tmp_path, _b2b_keys())
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    before = {f: (tmp_path / "library" / f).read_bytes()
              for f in os.listdir(lib) if f.endswith(".json")}
    s3 = FakeS3()
    out = contact_sheet.run("b2b", s3_client=s3, library_path=lib)
    key = f"echo/contact_sheets/b2b_{date.today().isoformat()}.html"
    assert out["key"] == key
    assert s3.puts and s3.puts[0][0] == key           # uploaded to the right path
    assert out["url"] == f"https://cdn.echo.test/{key}"
    for f, blob in before.items():                    # library sidecars untouched
        assert (tmp_path / "library" / f).read_bytes() == blob


def test_unrendered_concept_says_so(monkeypatch, tmp_path):
    lib = _seed_library(tmp_path, _b2b_keys()[:1])    # only one sidecar exists
    out = contact_sheet.run("b2b", s3_client=FakeS3(), library_path=lib)
    text = open(out["path"], encoding="utf-8").read()
    assert out["count"] == 21
    assert text.count("not rendered yet") == 20       # honest, never a broken image


def test_unknown_set_refuses(monkeypatch, tmp_path, capsys):
    assert contact_sheet.run("nope", s3_client=FakeS3(),
                             library_path=str(tmp_path)) is None
    assert "unknown set" in capsys.readouterr().out
