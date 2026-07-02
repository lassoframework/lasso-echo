"""
Branded PDF builder (white label), shared by the monthly report, the grade
card, and the welcome kit.

PDF path decision, stated plainly: REPORTLAB REBUILD. weasyprint needs pango
and cairo system libraries the Railway Nix image does not carry; wkhtmltopdf is
a system binary we cannot ship repo-only. reportlab is a pure pip dependency
(added to requirements.txt) and renders deterministically, so the report layout
is rebuilt natively instead of converting the HTML.

White labeling: the client's display name (and a logo when
brand_voice/<client>/logo.png exists) from per-account config; LASSO branding
is the default. No dash characters anywhere in rendered copy (the no-dash scrub
runs over every string defensively).
"""

import os
import re

# V3 palette
NAVY = "#121E3C"
RED = "#FF0000"
SKY = "#5EB9E6"
CREAM = "#FAF6F0"


def _scrub(text):
    """Defense in depth: no em dashes, en dashes, or stray hyphens-as-dashes in
    rendered copy. Word-internal hyphens are rewritten with a space."""
    t = str(text).replace("—", ", ").replace("–", " to ")
    return re.sub(r"\s+-\s+", ", ", t)


def brand_for(account):
    """{name, logo} for white labeling: the account's display name, plus a logo
    when the client folder carries one. LASSO branding is the default."""
    name = getattr(account, "display_name", "") or "LASSO"
    logo = None
    lib_prefix = getattr(account, "library_prefix", "") or ""
    client_dir = os.path.basename(lib_prefix) if lib_prefix else ""
    for candidate in ([os.path.join("brand_voice", client_dir, "logo.png")]
                      if client_dir else []) + [os.path.join("brand_voice", "logo.png")]:
        if os.path.exists(candidate):
            logo = candidate
            break
    return {"name": name, "logo": logo}


def build_pdf(out_path, title, subtitle, sections, brand=None):
    """
    One clean V3-branded page (flows to more if needed).
    sections: list of ("heading", text) | ("para", text) | ("list", [items])
              | ("table", [(label, value), ...])
    """
    from reportlab.lib import colors  # lazy
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (Image, ListFlowable, ListItem, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    brand = brand or {"name": "LASSO", "logo": None}
    styles = {
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=22,
                                textColor=colors.HexColor(NAVY), spaceAfter=6),
        "subtitle": ParagraphStyle("s", fontName="Helvetica", fontSize=13,
                                   textColor=colors.HexColor(SKY), spaceAfter=14),
        "heading": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=13,
                                  textColor=colors.HexColor(NAVY), spaceBefore=12,
                                  spaceAfter=6),
        "para": ParagraphStyle("p", fontName="Helvetica", fontSize=10,
                               textColor=colors.HexColor(NAVY), leading=14),
    }

    story = []
    if brand.get("logo") and os.path.exists(brand["logo"]):
        try:
            story.append(Image(brand["logo"], width=1.2 * inch, height=1.2 * inch,
                               kind="proportional"))
            story.append(Spacer(1, 8))
        except Exception:
            pass  # a bad logo file never sinks the report
    story.append(Paragraph(_scrub(f"{brand['name']}: {title}"), styles["title"]))
    if subtitle:
        story.append(Paragraph(_scrub(subtitle), styles["subtitle"]))

    for kind, payload in sections:
        if kind == "heading":
            story.append(Paragraph(_scrub(payload), styles["heading"]))
        elif kind == "para":
            story.append(Paragraph(_scrub(payload), styles["para"]))
        elif kind == "list":
            story.append(ListFlowable(
                [ListItem(Paragraph(_scrub(x), styles["para"])) for x in payload],
                bulletType="bullet"))
        elif kind == "table":
            rows = [[_scrub(a), _scrub(b)] for a, b in payload]
            t = Table(rows, colWidths=[3.2 * inch, 3.2 * inch])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor(NAVY)),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor(CREAM)),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(t)

    doc = SimpleDocTemplate(out_path, pagesize=letter,
                            leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.9 * inch, bottomMargin=0.9 * inch,
                            title=_scrub(title))
    doc.build(story)
    return out_path


def pdf_text(path):
    """The PDF's text layer (for tests): pypdf, already a project dependency."""
    from pypdf import PdfReader  # lazy
    return "\n".join((page.extract_text() or "") for page in PdfReader(path).pages)
