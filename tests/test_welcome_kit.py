"""
Welcome kit tests: renders (HTML + real PDF) for an account; dash-free text
layer; no pricing strings anywhere; the trust rules land in plain language;
unknown account is a clear no-op.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import pdf_report, welcome_kit  # noqa: E402


def test_renders_for_account(monkeypatch, tmp_path):
    out = welcome_kit.run("lasso_ig", out_dir=str(tmp_path))
    assert out is not None
    html_path, pdf_path = out
    html = open(html_path, encoding="utf-8").read()
    assert "Welcome" in html
    for section in ("How posting works", "How to send us creative",
                    "What the monthly report covers", "The trust rules"):
        assert section in html
    assert "approved by a human" in html
    assert "never invent claims" in html
    # PDF: real, dash free, trust language present
    assert os.path.getsize(pdf_path) > 1000
    text = pdf_report.pdf_text(pdf_path)
    assert "approved by a human" in text
    for ch in ("—", "–"):
        assert ch not in text and ch not in html


def test_no_pricing_strings(tmp_path):
    html_path, pdf_path = welcome_kit.run("lasso_ig", out_dir=str(tmp_path))
    html = open(html_path, encoding="utf-8").read()
    text = pdf_report.pdf_text(pdf_path)
    for body in (html, text):
        low = body.lower()
        assert "$" not in body                      # no dollar figures at all
        assert not re.search(r"\bprice|pricing|per month|monthly fee\b", low)
        assert "99" not in body                     # the unconfirmed price never


def test_unknown_account_noop(tmp_path, capsys):
    assert welcome_kit.run("nope_ig", out_dir=str(tmp_path)) is None
    assert "unknown account" in capsys.readouterr().out
    assert os.listdir(tmp_path) == []
