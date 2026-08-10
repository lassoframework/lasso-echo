"""
Summit card compositor — PIL/code text rendering, never model-rendered text.

Fixes the reference-card defects at the source: text is drawn with PIL so it can
never garble; the LASSO wordmark is typeset in the brand font (Anton), so no card
generates a logo; no dates or week numbers are baked into the art; exactly one red
element per card (grade-gate Q3); every zone earns its place (no vacant thirds).

Two treatments per concept:
  A  type-led editorial: oversized Anton headline with ONE red word, over cream
     or navy, a receipted fact row filling the lower third.
  B  data-led: medium ink headline, then a designed data element (fact tiles,
     checklist, numbered steps, funnel bars, session grid, contrast columns) that
     carries the single red accent.

Pure PIL: no API key, no network. Renders locally so every asset is viewable and
grade-checkable before it is ever scheduled. Palette + type: house style Sec 2.
"""

import os
from PIL import Image, ImageDraw, ImageFont

SIZE = 1080
MARGIN = 96

CREAM = (250, 246, 240)
NAVY = (18, 30, 60)
RED = (224, 49, 49)
SKY = (94, 185, 230)
WHITE = (250, 250, 250)
MUTE_CREAM = (122, 122, 128)
MUTE_NAVY = (168, 178, 198)

_FD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
ANTON = os.path.join(_FD, "Anton-Regular.ttf")
OSWALD = os.path.join(_FD, "Oswald-Medium.ttf")
OSWALD_B = os.path.join(_FD, "Oswald-Bold.ttf")
MONT = os.path.join(_FD, "Montserrat-Medium.ttf")
MONT_SB = os.path.join(_FD, "Montserrat-SemiBold.ttf")
MONT_B = os.path.join(_FD, "Montserrat-Bold.ttf")

DEFAULT_FACTS = ["NOV 7 + 8", "VIRGIN HOTEL NASHVILLE", "100 SEATS"]

# ---- BOLD SUMMIT identity (sprint concept cards ONLY) -----------------------
# Blake ruling: the sprint/summit cards must NOT look like the daily house
# infographic (soft cream + navy). This is a deliberately LOUD, high-contrast
# identity: a deep near-black base, ONE electric accent, big color-blocked bands,
# oversized condensed type, an event lockup on every card, and a sponsor strip.
# It is PIL-composited (never Gemini) so the bold palette is fully controlled.
# The DAILY house look (creative_studio + the cream/navy renderers above) is
# UNTOUCHED; only the sprint feed/story path uses these helpers.
BOLD_BG = (14, 18, 32)          # ~#0E1220 deep midnight, nothing like the cream card
BOLD_BG_2 = (20, 26, 46)        # a hair lighter, for the color-blocked lower band
BOLD_ACCENT = (255, 74, 28)     # ~#FF4A1C amplified LASSO red/orange, the ONE accent
BOLD_INK = (247, 244, 238)      # warm white/cream type
BOLD_MUTE = (150, 162, 190)     # muted cool grey for labels

# Dash-free event lockup shown on EVERY bold card.
EVENT_LOCKUP = ("LASSO GROWTH SUMMIT", "NOVEMBER 7 and 8", "VIRGIN HOTEL NASHVILLE")

# Per-concept bold callouts: the data point(s) that must read big, not buried.
# Sourced only from the receipted spine (no fabrication). (value, label) pairs.
BOLD_CALLOUTS = {
    "01_invitation": [("100", "SEATS"), ("10", "LEADERS"), ("2", "DAYS")],
    "02_deliverable": [("2027", "GROWTH PLAN"), ("1", "BROKEN LEG FIXED")],
    "03_agenda": [("10", "SESSIONS"), ("10", "LEADERS"), ("1", "PLAN")],
    "04_funnel": [("40", "LEADS TO BOOK"), ("50", "BOOK TO SHOW"),
                  ("70", "SHOW TO CLOSE")],
    "05_math": [("2027", "TARGET"), ("1", "NUMBER NOT A WISH")],
    "06_room": [("100", "OPERATORS"), ("2", "DAYS"), ("0", "STAGE PITCHES")],
    "07_numbers": [("100", "OWNERS"), ("10", "LEADERS"), ("2", "DAYS"), ("1", "PLAN")],
    "13_audience": [("100", "SERIOUS OPERATORS"), ("2027", "BUILT HERE")],
    "11_stakes": [("70", "PERCENT CLOSE OR BETTER"), ("1", "SYSTEM AWAY")],
    "12_outcome": [("2027", "GROWTH PLAN"), ("90", "DAY ACTION PLAN")],
}


def _f(path, size):
    return ImageFont.truetype(path, size)


def _tw(d, text, font):
    b = d.textbbox((0, 0), text, font=font)
    return b[2] - b[0]


def _th(d, text, font):
    b = d.textbbox((0, 0), text, font=font)
    return b[3] - b[1]


def _tracked(d, xy, text, font, fill, tracking=6, stroke_width=0, stroke_fill=None):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill,
               stroke_width=stroke_width, stroke_fill=stroke_fill)
        x += _tw(d, ch, font) + tracking
    return x


def _tracked_w(d, text, font, tracking=6):
    return sum(_tw(d, ch, font) + tracking for ch in text) - tracking


def _wrap(d, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if _tw(d, t, font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit(d, text, max_w, max_lines, start, floor=56):
    s = start
    while s >= floor:
        fo = _f(ANTON, s)
        ls = _wrap(d, text, fo, max_w)
        if len(ls) <= max_lines:
            return fo, ls
        s -= 4
    fo = _f(ANTON, floor)
    return fo, _wrap(d, text, fo, max_w)


def _headline(d, x, y, lines, font, red_tokens, ink, gap=6, shadow=False):
    lh = _th(d, "AY", font) + gap + int(font.size * 0.28)
    for i, line in enumerate(lines):
        cx = x
        for word in line.split():
            fill = RED if word.strip(".,").upper() in red_tokens else ink
            if shadow:
                d.text((cx + 3, y + i * lh + 3), word, font=font, fill=(6, 10, 20))
            d.text((cx, y + i * lh), word, font=font, fill=fill)
            cx += _tw(d, word + " ", font)
    return y + len(lines) * lh


def _ghost(d, bg, text):
    gf = _f(ANTON, 600)
    col = tuple(int(b * 0.93 + (255 - b) * 0.035) for b in bg)
    d.text((SIZE - 330, SIZE - 540), text, font=gf, fill=col)


def _footer(d, ink, mute):
    fy = SIZE - MARGIN - 20
    ex = _tracked(d, (MARGIN, fy), "LASSO", _f(ANTON, 34), ink, 8)
    d.text((ex + 14, fy + 8), "GROWTH SUMMIT", font=_f(OSWALD, 22), fill=mute)
    url = "LASSOFRAMEWORK.COM/SUMMIT"
    uf = _f(OSWALD, 24)
    d.text((SIZE - MARGIN - _tw(d, url, uf), fy + 4), url, font=uf, fill=mute)


# ---- data elements (treatment B; each carries the ONE red accent) ----------

def _fact_tiles(d, y, tiles, ink, mute, big=False, accent_first=False):
    n = len(tiles)
    gap = 24
    tw = (SIZE - 2 * MARGIN - gap * (n - 1)) // n
    h = 150 if big else 118
    vf = _f(ANTON, 44 if big else 34)
    for i, t in enumerate(tiles):
        x = MARGIN + i * (tw + gap)
        val = t.split("|")[0].strip()
        # accent_first paints exactly ONE red element: the first tile as a SOLID red
        # chip with white text. A deliberate accent that reads as designed, never an
        # errant outline. Used only on treatment-B tile cards (headline carries no red).
        if accent_first and i == 0:
            d.rectangle([x, y, x + tw, y + h], fill=RED)
            tile_ink = WHITE
        else:
            d.rectangle([x, y, x + tw, y + h], outline=ink, width=2)
            tile_ink = ink
        for j, ln in enumerate(_wrap(d, val, vf, tw - 28)[:2]):
            d.text((x + 16, y + 18 + j * (vf.size + 4)), ln, font=vf, fill=tile_ink)
    return y + h


def _checklist(d, y, items, ink, mute):
    rf = _f(MONT, 34)
    row = 118
    for i, it in enumerate(items):
        cy = y + i * row
        r = 22
        # exactly ONE red element: only the first check ring is red, rest are ink.
        mark = RED if i == 0 else ink
        d.ellipse([MARGIN, cy, MARGIN + 2 * r, cy + 2 * r], outline=mark, width=4)
        d.line([MARGIN + 12, cy + 22, MARGIN + 20, cy + 30], fill=mark, width=4)
        d.line([MARGIN + 20, cy + 30, MARGIN + 34, cy + 12], fill=mark, width=4)
        for j, ln in enumerate(_wrap(d, it, rf, SIZE - 2 * MARGIN - 70)[:2]):
            d.text((MARGIN + 66, cy - 4 + j * 40), ln, font=rf, fill=ink)
    return y + len(items) * row


def _steps(d, y, steps, ink, mute):
    nf = _f(ANTON, 40)
    tf = _f(MONT, 32)
    row = 116
    for i, s in enumerate(steps):
        cy = y + i * row
        box = 60
        # exactly ONE red element: first step box red, the rest navy.
        col = RED if i == 0 else (SKY if ink == WHITE else NAVY)
        d.rectangle([MARGIN, cy, MARGIN + box, cy + box], fill=col)
        num = str(i + 1)
        d.text((MARGIN + (box - _tw(d, num, nf)) // 2, cy + 4), num, font=nf, fill=WHITE)
        for j, ln in enumerate(_wrap(d, s, tf, SIZE - 2 * MARGIN - box - 30)[:2]):
            d.text((MARGIN + box + 24, cy + 2 + j * 38), ln, font=tf, fill=ink)
    return y + len(steps) * row


def _bars(d, y, bars, ink, mute):
    lf = _f(OSWALD, 30)
    row = 120
    full = SIZE - 2 * MARGIN
    for i, (label, pct) in enumerate(bars):
        cy = y + i * row
        d.text((MARGIN, cy), f"{label}  {pct}%+", font=lf, fill=ink)
        track_y = cy + 46
        d.rounded_rectangle([MARGIN, track_y, MARGIN + full, track_y + 34], radius=17,
                            fill=tuple(int(b * 0.82 + 255 * 0.18) for b in NAVY) if ink == WHITE else (222, 216, 208))
        fillw = int(full * pct / 100.0)
        col = RED if i == 0 else (SKY if ink == WHITE else NAVY)
        d.rounded_rectangle([MARGIN, track_y, MARGIN + fillw, track_y + 34], radius=17, fill=col)
    return y + len(bars) * row


def _grid(d, y, items, ink, mute):
    nf = _f(ANTON, 30)
    tf = _f(MONT, 26)
    cols, rows = 2, (len(items) + 1) // 2
    colw = (SIZE - 2 * MARGIN - 40) // 2
    row_h = 92
    for i, it in enumerate(items):
        c, r = i // rows, i % rows
        x = MARGIN + c * (colw + 40)
        cy = y + r * row_h
        box = 50
        d.rectangle([x, cy, x + box, cy + box], fill=RED if i == 0 else NAVY if ink == WHITE else NAVY)
        n = f"{i+1:02d}"
        d.text((x + (box - _tw(d, n, nf)) // 2, cy + 6), n, font=nf, fill=WHITE)
        for j, ln in enumerate(_wrap(d, it, tf, colw - box - 20)[:2]):
            d.text((x + box + 16, cy + 2 + j * 30), ln, font=tf, fill=ink)
    return y + rows * row_h


def _contrast(d, y, rows, ink, mute):
    tf = _f(MONT, 30)
    colw = (SIZE - 2 * MARGIN - 40) // 2
    rx = MARGIN + colw + 40
    row_h = 104
    d.line([MARGIN + colw + 20, y, MARGIN + colw + 20, y + len(rows) * row_h - 24], fill=mute, width=2)
    for i, (bad, good) in enumerate(rows):
        cy = y + i * row_h
        # left X (muted), right check (red = the one accent lives on the winning column's first mark)
        d.line([MARGIN, cy + 6, MARGIN + 26, cy + 32], fill=mute, width=4)
        d.line([MARGIN + 26, cy + 6, MARGIN, cy + 32], fill=mute, width=4)
        for j, ln in enumerate(_wrap(d, bad, tf, colw - 50)[:2]):
            d.text((MARGIN + 44, cy + j * 34), ln, font=tf, fill=mute)
        ck = RED if i == 0 else (SKY if ink == WHITE else NAVY)
        d.line([rx, cy + 20, rx + 12, cy + 32], fill=ck, width=5)
        d.line([rx + 12, cy + 32, rx + 34, cy + 4], fill=ck, width=5)
        for j, ln in enumerate(_wrap(d, good, tf, colw - 50)[:2]):
            d.text((rx + 48, cy + j * 34), ln, font=tf, fill=ink)
    return y + len(rows) * row_h


def _bignums(d, y, pairs, ink, mute):
    for i, (num, label) in enumerate(pairs):
        cy = y + i * 118
        nf = _f(ANTON, 96)
        d.text((MARGIN, cy), num, font=nf, fill=RED if i == len(pairs) - 1 else ink)
        lf = _f(OSWALD, 40)
        d.text((MARGIN + 210, cy + 28), label, font=lf, fill=ink)
    return y + len(pairs) * 118


# per-concept treatment-B data element
_B_ELEMENTS = {
    "01_invitation": ("tiles", DEFAULT_FACTS),
    "02_deliverable": ("check", ["Revenue target and the member math",
                                 "Your one broken funnel leg and the fix",
                                 "Sales, retention, and team plays",
                                 "A 90 day action plan for Monday"]),
    "03_agenda": ("grid", ["Where you are now", "Your 2027 target",
                           "The funnel diagnostic", "Offer and positioning",
                           "Lead generation", "The sales system", "Retention",
                           "Team and leadership", "Client value", "Capacity and pricing"]),
    "04_funnel": ("bars", [("LEADS TO BOOK", 40), ("BOOK TO SHOW", 50),
                           ("SHOW TO CLOSE", 70)]),
    "05_math": ("steps", ["Set your 2027 revenue target",
                          "Subtract today. That gap is the work",
                          "Divide by revenue per member",
                          "Apply your close rate for leads per month"]),
    "06_room": ("tiles", ["100 OPERATORS", "10 LEADERS", "2 DAYS"]),
    "07_numbers": ("bignums", [("100", "OWNERS"), ("10", "LEADERS"),
                               ("2", "DAYS"), ("1", "PLAN")]),
    "13_audience": ("check", ["Serious operators, not hobbyists",
                              "Owners ready to build 2027",
                              "100 in one room, no stage pitches",
                              "Came for a plan, not more notes"]),
    "11_stakes": ("contrast", [("Close under 70 percent", "Close 70 percent or better"),
                               ("Burn ad spend", "A funnel that holds"),
                               ("Do it all yourself", "A business on systems")]),
    "12_outcome": ("check", ["Your revenue target and member math",
                             "Your funnel fix",
                             "Sales, retention, and team playbook",
                             "A 90 day action plan for Monday"]),
}


def _base_with_bg(bg_path):
    """A 1080 canvas from a Higgsfield background, cover-cropped, with a navy
    legibility scrim (darker top and bottom) so composited white text stays
    readable. Returns the RGB image. Text is still drawn in PIL over this, so the
    image never carries model-rendered (garble-prone) text."""
    src = Image.open(bg_path).convert("RGB")
    # cover-crop to square then resize to SIZE
    w, h = src.size
    s = min(w, h)
    src = src.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((SIZE, SIZE))
    # navy scrim: strong at top (headline) and bottom (footer), lighter middle
    scrim = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    h1, h2 = SIZE * 0.60, SIZE * 0.84
    for y in range(SIZE):
        # Hold a strong navy scrim through the entire text zone (headline, deck, and
        # the dense checklist down to ~84%) so white supporting text always reads;
        # the crowd still shows through, and the bottom band carries the fact strip.
        if y <= h1:
            a = int(224 - (224 - 176) * (y / h1))        # 224 -> 176
        elif y <= h2:
            a = int(176 - (176 - 150) * ((y - h1) / (h2 - h1)))  # 176 -> 150
        else:
            a = 150
        bot = int((y - SIZE * 0.74) * 2.6) if y > SIZE * 0.74 else 0
        a = min(240, max(a, bot))
        sd.line([(0, y), (SIZE, y)], fill=(10, 18, 38, a))
    base = Image.alpha_composite(src.convert("RGBA"), scrim).convert("RGB")
    return base


# Dense supporting content per concept — fills the middle so the type-led cards
# carry real substance (the reframe, the diagnostic, the deliverables, the session
# map, the member math), not open space. Sourced from 04_summit_campaign.md and the
# approved captions. No blocked claims, no fabricated numbers.
POINTS = {
    "01_invitation": ["100 seats. When they are gone, they are gone",
                      "10 industry leaders, one room, two days",
                      "You leave with a plan, not a notebook"],
    "02_deliverable": ["Your 2027 revenue target and the member math",
                       "Your one broken funnel leg and the fix",
                       "Your sales, retention, and team plays",
                       "A 90 day action plan you run Monday"],
    "03_agenda": ["Where you are now and your 2027 target",
                  "The funnel diagnostic and the member math",
                  "Offer, positioning, and lead generation",
                  "Sales system, retention, team, and pricing"],
    "04_funnel": ["Leads to book 40%+ is leg one",
                  "Book to show 50%+ is leg two",
                  "Show to close 70%+ is leg three",
                  "Fix the weakest leg, then scale"],
    "05_math": ["Set your 2027 revenue target",
                "Subtract where you are today",
                "Divide by revenue per member",
                "Apply your close rate for leads"],
    "06_room": ["100 serious operators, not hobbyists",
                "No stage pitches, only real strategy",
                "99 peers who get what you carry",
                "The room is the ROI"],
    "07_numbers": ["100 gym owners in one room",
                   "10 industry leaders on stage",
                   "2 days in Nashville",
                   "1 finished 2027 plan"],
    "13_audience": ["Established gym owners building 2027",
                    "Not hobbyists, not tire kickers",
                    "No stage pitches, real strategy",
                    "From operators who have done it"],
    "11_stakes": ["Closing under 70% vs 70% or better",
                  "Burning ad spend vs a funnel that holds",
                  "Doing it all yourself vs systems that run",
                  "Most owners are one system away"],
    "12_outcome": ["Your revenue target and member math",
                   "Your broken funnel leg, fixed",
                   "Your sales, retention, and team playbook",
                   "A 90 day action plan, on paper"],
}


_POINT_ROW = 66


def _overlay_dark(img, box, alpha):
    """Composite a translucent dark rounded plate onto an RGB image; returns a new RGB
    image. Used behind the checklist band so white text stays crisp over any photo."""
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle(box, radius=18, fill=(6, 12, 22, alpha))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def _points_band(d, y, points, ink, row=None, on_photo=False):
    """A dense checklist that fills the middle of a type-led card. Sky/navy checks
    (never a second red — the single red lives in the headline). Crisp white Oswald on
    photo cards. `row` (vertical pitch) is set by the caller so the band always clears
    the bottom fact strip, even under a tall headline."""
    rf = _f(OSWALD_B, 33)  # bold grotesque reads crisp white over a photo
    row = row or _POINT_ROW
    accent = SKY if ink == WHITE else NAVY
    for i, p in enumerate(points):
        cy = y + i * row
        r = 10
        d.ellipse([MARGIN, cy + 9, MARGIN + 2 * r, cy + 9 + 2 * r], outline=accent, width=3)
        d.line([MARGIN + 6, cy + 19, MARGIN + 9, cy + 24], fill=accent, width=3)
        d.line([MARGIN + 9, cy + 24, MARGIN + 16, cy + 13], fill=accent, width=3)
        if on_photo:
            d.text((MARGIN + 46, cy), p, font=rf, fill=(255, 255, 255),
                   stroke_width=1, stroke_fill=(6, 12, 22))
        else:
            d.text((MARGIN + 46, cy), p, font=rf, fill=ink)
    return y + len(points) * row


def _fact_strip(d, y, facts, ink, on_photo=False):
    """One compact line of event facts at the bottom of a type-led card, so the middle
    is free for the dense checklist. Replaces the three large tiles on the _a variant."""
    f = _f(OSWALD_B, 27)
    text = "      ".join(facts)
    if on_photo:
        _tracked(d, (MARGIN, y), text, f, (255, 255, 255), 2,
                 stroke_width=2, stroke_fill=(6, 12, 22))
    else:
        _tracked(d, (MARGIN, y), text, f, ink, 2)


def render_card(concept, treatment, out_path, canvas=None, bg_path=None):
    if bg_path:
        canvas = "navy"  # scrimmed image is always a dark canvas
    canvas = canvas or ("navy" if treatment == "b" else "cream")
    bg = NAVY if canvas == "navy" else CREAM
    ink = WHITE if canvas == "navy" else NAVY
    mute = MUTE_NAVY if canvas == "navy" else MUTE_CREAM

    if bg_path:
        img = _base_with_bg(bg_path)
    else:
        img = Image.new("RGB", (SIZE, SIZE), bg)
    d = ImageDraw.Draw(img)
    cw = SIZE - 2 * MARGIN
    if not bg_path:
        _ghost(d, bg, "".join(c for c in concept["headline"] if c.isdigit())[:3] or "26")

    y = MARGIN
    _tracked(d, (MARGIN, y), concept["eyebrow"].upper(), _f(OSWALD_B, 30), mute, 5)
    y += 62

    if treatment == "a":
        red_tokens = set(w.strip(".,").upper() for w in concept["red_word"].split())
        # headline fills the top (big for short headlines; long ones auto-reduce to 3 lines)
        hf, lines = _fit(d, concept["headline"].upper(), cw, 3, 104)
        y = _headline(d, MARGIN, y + 6, lines, hf, red_tokens, ink, shadow=bool(bg_path))
        y += 14
        df = _f(MONT_SB, 33)
        for ln in _wrap(d, concept["deck"], df, cw):
            if bg_path:
                # crisp white with a dark stroke so the deck reads over ANY photo zone
                d.text((MARGIN, y), ln, font=df, fill=(255, 255, 255),
                       stroke_width=2, stroke_fill=(6, 12, 22))
            else:
                d.text((MARGIN, y), ln, font=df, fill=mute)
            y += 44
        # dense supporting checklist fills the middle so there is no open space.
        # Row pitch adapts to the space left above the fact strip so the last bullet
        # never collides with it, even when a tall 3-line headline pushes it down.
        y += 20
        pts = POINTS.get(concept["id"], [])
        strip_y = SIZE - MARGIN - 60
        row = _POINT_ROW
        if len(pts) > 1:
            # Spread the points to fill without crowding: comfortable pitch (cap 92) so
            # short cards are not airy-empty and dense cards are not crammed; floor keeps
            # the last point clear of the strip under a tall headline.
            row = max(58, min(92, (strip_y - 56 - y) // (len(pts) - 1)))
        _points_band(d, y, pts, ink, row=row, on_photo=bool(bg_path))
        _fact_strip(d, strip_y, DEFAULT_FACTS, ink, on_photo=bool(bg_path))
    else:
        hf, lines = _fit(d, concept["headline"].upper(), cw, 2, 82)
        y = _headline(d, MARGIN, y + 4, lines, hf, set(), ink)  # ink headline; red lives in data
        y += 20  # breathing room so the deck never crowds the headline baseline
        for ln in _wrap(d, concept["deck"], _f(MONT, 30), cw)[:2]:
            d.text((MARGIN, y), ln, font=_f(MONT, 30), fill=mute); y += 40
        y += 20
        kind, data = _B_ELEMENTS[concept["id"]]
        if kind == "tiles":
            # B tile cards carry their one red on the first tile (ink headline).
            ty = _fact_tiles(d, y, data, ink, mute, big=True, accent_first=True)
            # fill the lower third with a receipted supporting line so the card
            # has no vacant field (house style: every zone earns its place).
            sup = concept.get("support")
            if sup:
                sy = ty + 56
                sf = _f(MONT, 34)
                for ln in _wrap(d, sup, sf, cw):
                    d.text((MARGIN, sy), ln, font=sf, fill=ink); sy += 46
        else:
            {"check": _checklist, "steps": _steps, "bars": _bars, "grid": _grid,
             "contrast": _contrast, "bignums": _bignums}[kind](d, y, data, ink, mute)

    _footer(d, ink, mute)
    img.save(out_path)
    return out_path


# Treatment A background per concept (Higgsfield atmospheric/venue, no crowds),
# arc-spaced so no two adjacent posts share a background. Treatment B stays on a
# solid canvas so the data element reads cleanly.
# People-filled event photos on the community/room/who concepts (gym owners in
# the room and meeting); atmospheric/venue on the methodology concepts.
BG_MAP = {
    "01_invitation": "audience", "02_deliverable": "venue", "03_agenda": "networking",
    "04_funnel": "venue", "05_math": "networking", "06_room": "audience",
    "07_numbers": "audience", "13_audience": "audience", "11_stakes": "venue",
    "12_outcome": "networking",
}


def render_all(out_dir, bg_dir=None):
    from .summit_rebuild import SUMMIT_CONCEPTS
    paths = []
    for c in SUMMIT_CONCEPTS:
        for t in ("a", "b"):
            p = os.path.join(out_dir, f"{c['id']}_{t}.png")
            bg = None
            if t == "a" and bg_dir and c["id"] in BG_MAP:
                cand = os.path.join(bg_dir, BG_MAP[c["id"]] + ".png")
                bg = cand if os.path.isfile(cand) else None
            render_card(c, t, p, bg_path=bg)
            paths.append(p)
    return paths


# ---- speaker reveal cards (real headshot, type-led fallback if missing) -----

def _cover(src, w, h):
    src = src.convert("RGB")
    sw, sh = src.size
    scale = max(w / sw, h / sh)
    src = src.resize((max(1, int(sw * scale)), max(1, int(sh * scale))))
    nw, nh = src.size
    return src.crop(((nw - w) // 2, (nh - h) // 2, (nw - w) // 2 + w, (nh - h) // 2 + h))


def _rounded(img, radius):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1],
                                           radius=radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def render_reveal(name, track, company, session, points, out_path,
                  headshot=None, red_name=None):
    """Speaker reveal: real headshot top-right, name + company top-left, the verbatim
    session title and 4+ points full width below, on clean navy. Never a generated face;
    if no headshot resolves it falls back to a wider type-led name block."""
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    PW, PH = 400, 410
    px, py = SIZE - MARGIN - PW, MARGIN + 4
    has_photo = bool(headshot) and os.path.isfile(headshot)
    if has_photo:
        photo = _rounded(_cover(Image.open(headshot), PW, PH), 28)
        img.paste(photo, (px, py), photo)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([px, py, px + PW, py + PH], radius=28, outline=(70, 92, 120), width=2)

    lx = MARGIN
    lw = (px - MARGIN - 44) if has_photo else (SIZE - 2 * MARGIN)
    y = MARGIN + 6
    _tracked(d, (lx, y), track.upper(), _f(OSWALD_B, 28), SKY, 5)
    y += 58
    red_tokens = {(red_name or name.split()[-1]).upper()}
    hf, lines = _fit(d, name.upper(), lw, 3, 88)
    y = _headline(d, lx, y, lines, hf, red_tokens, WHITE)
    y += 12
    cf = _f(MONT_SB, 26)
    for ln in _wrap(d, company, cf, lw):
        d.text((lx, y), ln, font=cf, fill=(212, 220, 232))
        y += 34

    y = max(y, py + PH) + 18
    _tracked(d, (MARGIN, y), "SPEAKING ON", _f(OSWALD_B, 24), SKY, 4)
    y += 44
    sf = _f(OSWALD_B, 36)
    for ln in _wrap(d, session, sf, SIZE - 2 * MARGIN):
        d.text((MARGIN, y), ln, font=sf, fill=WHITE)
        y += 46
    y += 14

    strip_top = SIZE - MARGIN - 88
    # auto-fit: shrink the point font until all points clear the strip, even with wraps
    fs = 28
    while fs >= 22:
        rf = _f(MONT_SB, fs)
        lh = fs + 10
        heights = [max(1, len(_wrap(d, r, rf, SIZE - 2 * MARGIN - 46))) * lh for r in points]
        if y + sum(heights) + 8 * len(points) <= strip_top - 6 or fs == 22:
            break
        fs -= 2
    gap = max(6, min(48, (strip_top - 12 - y - sum(heights)) // max(len(points), 1)))
    for i, r in enumerate(points):
        rl = _wrap(d, r, rf, SIZE - 2 * MARGIN - 46)
        d.ellipse([MARGIN, y + 6, MARGIN + 18, y + 24], outline=SKY, width=3)
        d.line([MARGIN + 4, y + 15, MARGIN + 8, y + 20], fill=SKY, width=3)
        d.line([MARGIN + 8, y + 20, MARGIN + 15, y + 9], fill=SKY, width=3)
        for j, ln in enumerate(rl):
            d.text((MARGIN + 44, y + j * (fs + 10)), ln, font=rf, fill=(226, 232, 242))
        y += heights[i] + gap

    _fact_strip(d, strip_top, DEFAULT_FACTS, WHITE, on_photo=False)
    _footer(d, WHITE, MUTE_NAVY)
    img.save(out_path)
    return out_path


# ---- agenda cards (Day 1 / Day 2) -------------------------------------------
# Sourced ONLY from 02_verified_stats.md "SUMMIT SPEAKERS — receipts": each
# session title is verbatim from that block, attributed to the speaker it belongs
# to. Event runs NOV 7 + 8 at Virgin Hotel Nashville (both verified).
#
# FABRICATION GUARD: the verified source gives session titles and speakers but
# does NOT publish session TIMES or a per-day running order. So these cards carry
# NO times, and the day split is a THEMATIC grouping (growth track / business
# track), never a claim that "session X runs at time Y on day N". The card labels
# the day by its verified DATE only (Day 1 = NOV 7, Day 2 = NOV 8). Whether Blake
# wants specific sessions locked to specific days is a BLOCK item (see the sprint
# report), not something invented here.

# Each session is (speaker, verbatim session title). Titles are verbatim from the
# SUMMIT SPEAKERS receipts block. Grouped by theme, not by a fabricated schedule.
AGENDA_DAY1 = {
    "day": "DAY ONE",
    "date": "NOV 7",
    "theme": "THE GROWTH ENGINE",
    "sessions": [
        ("Andrew Charlesworth", "State of the Industry"),
        ("Blake Ruff", "Meta Ads in 2026: What's Working Now (and What's Dead)"),
        ("Tommy Allen", "The Math Behind Scaling: Know Your Levers"),
        ("Stu Brauer", "Scaling for Success: From Operator to Owner"),
    ],
}

AGENDA_DAY2 = {
    "day": "DAY TWO",
    "date": "NOV 8",
    "theme": "THE BUSINESS ENGINE",
    "sessions": [
        ("Jeff Smith", "Leadership That Scales: From Coach to CEO"),
        ("Scott Rammage", "Hiring That Scales Your Gym"),
        ("Brian Alexander", "Building a Predictable Hiring Machine"),
        ("Nicole Aucoin", "Increasing LTV Through Ancillary Services"),
    ],
}


def render_agenda(spec, out_path):
    """One agenda day card on navy: eyebrow (GROWTH SUMMIT AGENDA), the day label
    with its verified date, a themed track name carrying the single red accent, then
    the verbatim sessions (title + speaker) as numbered rows. No times, ever."""
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    ink, mute = WHITE, MUTE_NAVY

    y = MARGIN
    _tracked(d, (MARGIN, y), "GROWTH SUMMIT AGENDA", _f(OSWALD_B, 30), mute, 5)
    y += 66

    # Day label + verified date on one line; the date is the only day claim.
    df = _f(ANTON, 92)
    d.text((MARGIN, y), spec["day"], font=df, fill=ink)
    dxr = MARGIN + _tw(d, spec["day"], df) + 30
    d.text((dxr, y + 30), spec["date"], font=_f(OSWALD_B, 44), fill=mute)
    y += 116

    # themed track name is the one red element on the card
    tf = _f(ANTON, 58)
    d.text((MARGIN, y), spec["theme"], font=tf, fill=RED)
    y += 104

    # sessions: numbered navy chip (never a second red), verbatim title, speaker
    nf = _f(ANTON, 34)
    stf = _f(OSWALD_B, 34)
    spf = _f(MONT_SB, 26)
    cw = SIZE - 2 * MARGIN
    box = 56
    strip_y = SIZE - MARGIN - 60
    n = len(spec["sessions"])
    row = max(120, (strip_y - 40 - y) // max(n, 1))
    for i, (speaker, title) in enumerate(spec["sessions"]):
        cy = y + i * row
        d.rectangle([MARGIN, cy, MARGIN + box, cy + box], fill=SKY)
        num = f"{i + 1:02d}"
        d.text((MARGIN + (box - _tw(d, num, nf)) // 2, cy + 6), num, font=nf, fill=NAVY)
        tx = MARGIN + box + 26
        ty = cy - 2
        for ln in _wrap(d, title, stf, cw - box - 26)[:2]:
            d.text((tx, ty), ln, font=stf, fill=ink)
            ty += 42
        d.text((tx, ty + 2), speaker, font=spf, fill=mute)

    _fact_strip(d, strip_y, ["NOV 7 + 8", "VIRGIN HOTEL NASHVILLE", "100 SEATS"], ink)
    _footer(d, ink, mute)
    img.save(out_path)
    return out_path


# ---- panel card (Future of Gym Growth) --------------------------------------
# Panelists are Streamfit, HireVP, and Tommy (Blake ruling). PushPress is NOT a
# panelist and must never appear. Only the panelist names + the panel title go on
# the card; no fabricated bios, no invented session time.

PANEL = {
    "eyebrow": "SUMMIT PANEL",
    "title": "THE FUTURE OF GYM GROWTH",
    "red_word": "FUTURE",
    "deck": "Three operators on where boutique fitness goes next.",
    "panelists": ["Streamfit", "HireVP", "Tommy Allen"],
}


def render_panel(spec, out_path):
    """Panel card on navy: eyebrow, oversized headline (one red word), deck, then the
    three panelist names in equal tiles (first tile solid red as the single accent)."""
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    ink, mute = WHITE, MUTE_NAVY
    cw = SIZE - 2 * MARGIN

    y = MARGIN
    _tracked(d, (MARGIN, y), spec["eyebrow"], _f(OSWALD_B, 30), mute, 5)
    y += 66

    # ink headline; the single red accent lives on the first panelist tile below
    # (never two reds on one card).
    hf, lines = _fit(d, spec["title"].upper(), cw, 3, 104)
    y = _headline(d, MARGIN, y + 6, lines, hf, set(), ink)
    y += 18

    df = _f(MONT_SB, 33)
    for ln in _wrap(d, spec["deck"], df, cw):
        d.text((MARGIN, y), ln, font=df, fill=mute)
        y += 46
    y += 30

    _tracked(d, (MARGIN, y), "ON THE PANEL", _f(OSWALD_B, 26), SKY, 4)
    y += 60

    # three panelist tiles stacked; first tile solid red (the one accent), rest outlined
    names = spec["panelists"]
    tf = _f(ANTON, 46)
    th = 118
    gap = 26
    strip_y = SIZE - MARGIN - 60
    avail = strip_y - 40 - y
    th = min(th, (avail - gap * (len(names) - 1)) // len(names))
    for i, name in enumerate(names):
        ty = y + i * (th + gap)
        if i == 0:
            d.rectangle([MARGIN, ty, MARGIN + cw, ty + th], fill=RED)
            tile_ink = WHITE
        else:
            d.rectangle([MARGIN, ty, MARGIN + cw, ty + th], outline=ink, width=2)
            tile_ink = ink
        d.text((MARGIN + 28, ty + (th - tf.size) // 2 - 6), name.upper(),
               font=tf, fill=tile_ink)

    _fact_strip(d, strip_y, ["NOV 7 + 8", "VIRGIN HOTEL NASHVILLE", "100 SEATS"], ink)
    _footer(d, ink, mute)
    img.save(out_path)
    return out_path


# ---- 1080x1920 STORY versions ----------------------------------------------
# Paired vertical (9:16) versions of the feed cards so the sprint can run a story
# alongside a feed post on the same day (up to 3 feed posts/day, stories sit on top).
# Native-tall composition, NOT a crop: the same house vocabulary (Anton headline
# with one red word, Oswald deck, checklist / data element) laid out for the frame,
# with a top+bottom safe band so nothing lands under IG's chrome. Same fabrication
# rules: verified facts only, no dashes, one red accent.

STORY_W, STORY_H = 1080, 1920
STORY_MARGIN = 96
STORY_SAFE_TOP = 250      # clear of the IG top chrome / profile row
STORY_SAFE_BOT = 320      # clear of the caption / reply bar


def _story_footer(d, ink, mute, y):
    ex = _tracked(d, (STORY_MARGIN, y), "LASSO", _f(ANTON, 34), ink, 8)
    d.text((ex + 14, y + 8), "GROWTH SUMMIT", font=_f(OSWALD, 22), fill=mute)
    url = "LASSOFRAMEWORK.COM/SUMMIT"
    d.text((STORY_MARGIN, y + 48), url, font=_f(OSWALD, 24), fill=mute)


def _story_base_with_bg(bg_path):
    """A 1080x1920 canvas from a Higgsfield background, cover-cropped to the tall
    frame (never squashed), with a navy legibility scrim holding through the whole
    text column so white text always reads. Text is still PIL-drawn over this."""
    src = Image.open(bg_path).convert("RGB")
    src = _cover(src, STORY_W, STORY_H)
    scrim = Image.new("RGBA", (STORY_W, STORY_H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    for y in range(STORY_H):
        # strong at top (headline band) easing to a steady mid, then strong again
        # at the bottom fact strip; the athleisure crowd shows through the middle.
        if y <= STORY_H * 0.46:
            a = int(228 - (228 - 168) * (y / (STORY_H * 0.46)))
        elif y <= STORY_H * 0.74:
            a = 168
        else:
            a = min(240, int(168 + (y - STORY_H * 0.74) * 0.7))
        sd.line([(0, y), (STORY_W, y)], fill=(10, 18, 38, a))
    return Image.alpha_composite(src.convert("RGBA"), scrim).convert("RGB")


def _story_data_element(d, y, concept, ink, mute):
    """Draw the treatment-B data element for a concept in the story frame. Reuses the
    feed data-element vocabulary; MARGIN is shared so the elements land full width."""
    kind, data = _B_ELEMENTS[concept["id"]]
    if kind == "tiles":
        return _fact_tiles(d, y, data, ink, mute, big=True, accent_first=True)
    fn = {"check": _checklist, "steps": _steps, "bars": _bars, "grid": _grid,
          "contrast": _contrast, "bignums": _bignums}[kind]
    return fn(d, y, data, ink, mute)


def render_card_story(concept, treatment, out_path, bg_path=None):
    """Story (1080x1920) version of one summit feed card.
      A = type-led: photo (or navy) background, oversized headline with one red word,
          deck, dense checklist, bottom fact strip.
      B = data-led: navy, ink headline (red lives in the data element), the concept's
          data element (bars / checklist / steps / grid / contrast / tiles / bignums)."""
    on_photo = bool(bg_path) and treatment == "a"
    if on_photo:
        img = _story_base_with_bg(bg_path)
    else:
        img = Image.new("RGB", (STORY_W, STORY_H), NAVY)
    d = ImageDraw.Draw(img)
    ink = WHITE
    mute = MUTE_NAVY
    cw = STORY_W - 2 * STORY_MARGIN

    y = STORY_SAFE_TOP
    _tracked(d, (STORY_MARGIN, y), concept["eyebrow"].upper(), _f(OSWALD_B, 32), mute, 5)
    y += 74

    if treatment == "a":
        red_tokens = set(w.strip(".,").upper() for w in concept["red_word"].split())
        # _wrap / _headline take an explicit width, so the wider story column just works.
        s = 128
        while s >= 64:
            fo = _f(ANTON, s)
            ls = _wrap(d, concept["headline"].upper(), fo, cw)
            if len(ls) <= 4:
                break
            s -= 4
        y = _headline(d, STORY_MARGIN, y + 6, ls, fo, red_tokens, ink,
                      shadow=on_photo)
        y += 22
        df = _f(MONT_SB, 36)
        for ln in _wrap(d, concept["deck"], df, cw):
            if on_photo:
                d.text((STORY_MARGIN, y), ln, font=df, fill=(255, 255, 255),
                       stroke_width=2, stroke_fill=(6, 12, 22))
            else:
                d.text((STORY_MARGIN, y), ln, font=df, fill=mute)
            y += 50
        y += 40
        pts = POINTS.get(concept["id"], [])
        strip_y = STORY_H - STORY_SAFE_BOT
        row = _POINT_ROW
        if len(pts) > 1:
            row = max(70, min(140, (strip_y - 90 - y) // (len(pts) - 1)))
        # story checklist: bigger type for the taller frame
        rf = _f(OSWALD_B, 40)
        accent = SKY
        for i, p in enumerate(pts):
            cy = y + i * row
            r = 12
            d.ellipse([STORY_MARGIN, cy + 8, STORY_MARGIN + 2 * r, cy + 8 + 2 * r],
                      outline=accent, width=3)
            d.line([STORY_MARGIN + 7, cy + 20, STORY_MARGIN + 11, cy + 26], fill=accent, width=3)
            d.line([STORY_MARGIN + 11, cy + 26, STORY_MARGIN + 20, cy + 14], fill=accent, width=3)
            if on_photo:
                d.text((STORY_MARGIN + 54, cy), p, font=rf, fill=(255, 255, 255),
                       stroke_width=1, stroke_fill=(6, 12, 22))
            else:
                d.text((STORY_MARGIN + 54, cy), p, font=rf, fill=ink)
        _fact_strip(d, strip_y, DEFAULT_FACTS, ink, on_photo=on_photo)
        _story_footer(d, ink, mute, strip_y + 56)
    else:
        s = 104
        while s >= 60:
            fo = _f(ANTON, s)
            ls = _wrap(d, concept["headline"].upper(), fo, cw)
            if len(ls) <= 3:
                break
            s -= 4
        y = _headline(d, STORY_MARGIN, y + 4, ls, fo, set(), ink)
        y += 28
        for ln in _wrap(d, concept["deck"], _f(MONT, 34), cw)[:3]:
            d.text((STORY_MARGIN, y), ln, font=_f(MONT, 34), fill=mute)
            y += 46
        y += 40
        _story_data_element(d, y, concept, ink, mute)
        strip_y = STORY_H - STORY_SAFE_BOT
        _fact_strip(d, strip_y, DEFAULT_FACTS, ink)
        _story_footer(d, ink, mute, strip_y + 56)

    img.save(out_path)
    return out_path


def render_all_stories(out_dir, bg_dir=None):
    """Render the paired story versions of every feed concept (both treatments)."""
    from .summit_rebuild import SUMMIT_CONCEPTS
    paths = []
    for c in SUMMIT_CONCEPTS:
        for t in ("a", "b"):
            p = os.path.join(out_dir, f"{c['id']}_{t}_story.png")
            bg = None
            if t == "a" and bg_dir and c["id"] in BG_MAP:
                cand = os.path.join(bg_dir, BG_MAP[c["id"]] + ".png")
                bg = cand if os.path.isfile(cand) else None
            render_card_story(c, t, p, bg_path=bg)
            paths.append(p)
    return paths


# ============================================================================
# BOLD SUMMIT CARDS (sprint concept feed + story)
# ============================================================================
# A dedicated, loud, high-contrast look for the summit sprint, VISUALLY DISTINCT
# from the daily cream/navy house card. PIL-composited end to end (never Gemini)
# so the bold palette is fully controlled. Every card carries: the event lockup
# band (LASSO GROWTH SUMMIT / NOVEMBER 7 and 8 / VIRGIN HOTEL NASHVILLE), the
# oversized condensed headline with the ONE accent word, big numeric callouts,
# and a bottom sponsor strip ("PRESENTED WITH"). Sponsors are injectable and
# NEVER fabricated: an empty list renders a safe partner placeholder.


def _bold_accent_lockup(d, x, y):
    """A small accent-block event tag drawn top-left of a bold card: a solid accent
    chip, then the summit name, so the brand reads instantly and loud."""
    chip = 22
    d.rectangle([x, y + 4, x + chip, y + 4 + chip], fill=BOLD_ACCENT)
    ex = _tracked(d, (x + chip + 16, y), EVENT_LOCKUP[0], _f(OSWALD_B, 30), BOLD_INK, 4)
    return ex


def _bold_callouts_row(d, x, y, callouts, w, big=True, max_h=None):
    """Big numeric callouts laid across a row, each value in oversized Anton with a
    small label beneath. The ONE accent lives on the FIRST value (never a second
    accent block). Data is rendered LOUD, never buried. Returns the y just past the
    tallest label so the caller can flow content below without a collision.

    `max_h` (optional) caps the vertical space: the value font auto-shrinks so the
    number + its label fit within it, so a tall 3-line headline never squeezes the
    callouts into the footer."""
    n = max(1, len(callouts))
    gap = 28
    cellw = (w - gap * (n - 1)) // n
    vsize = 112 if big else 88
    lsize = 24 if big else 20
    if max_h:
        # value + ~1.5 label lines must fit max_h; shrink the value font to suit.
        while vsize > 48 and vsize + 10 + int(lsize * 1.6) > max_h:
            vsize -= 6
    vf = _f(ANTON, vsize)
    lf = _f(OSWALD_B, lsize)
    bottom = y
    for i, (val, label) in enumerate(callouts):
        cx = x + i * (cellw + gap)
        # shrink an over-wide value (e.g. "2027") so it never spills the cell
        s = vf.size
        f = vf
        while _tw(d, val, f) > cellw and s > 44:
            s -= 6
            f = _f(ANTON, s)
        col = BOLD_ACCENT if i == 0 else BOLD_INK
        d.text((cx, y), val, font=f, fill=col)
        # place the label below the REAL glyph bottom (textbbox), not the nominal
        # font size, so the label never overlaps the tall Anton numerals.
        gb = d.textbbox((cx, y), val, font=f)
        ly = gb[3] + 10
        wrapped = _wrap(d, label, lf, cellw)[:2]
        for j, ln in enumerate(wrapped):
            d.text((cx, ly + j * (lf.size + 4)), ln, font=lf, fill=BOLD_MUTE)
        bottom = max(bottom, ly + len(wrapped) * (lf.size + 4))
    return bottom


def _bold_event_band(d, x, y, w, ink=None, mute=None):
    """The event lockup band present on EVERY bold card: dates + venue in one loud
    line, dash-free. Kept distinct from the accent so it never competes with it."""
    ink = ink or BOLD_INK
    mute = mute or BOLD_MUTE
    df = _f(OSWALD_B, 32)
    d.text((x, y), EVENT_LOCKUP[1], font=df, fill=ink)          # NOVEMBER 7 and 8
    vy = y + df.size + 8
    d.text((x, vy), EVENT_LOCKUP[2], font=_f(OSWALD, 28), fill=mute)  # VIRGIN HOTEL...
    return vy + 34


def _bold_sponsor_strip(d, x, y, w, h, sponsors):
    """Bottom band labeled PRESENTED WITH with slots for sponsor names/logos.
    `sponsors` is injectable and NEVER fabricated: an empty list renders a subtle
    'Presented with our partners' placeholder, ready for tagging; supplied names are
    laid across the strip. Uppercased for the loud identity; dash-free by contract."""
    d.rectangle([x, y, x + w, y + h], fill=BOLD_BG_2)
    d.rectangle([x, y, x + 10, y + h], fill=BOLD_ACCENT)  # accent tab, the strip is not dead
    lf = _f(OSWALD_B, 24)
    _tracked(d, (x + 30, y + 20), "PRESENTED WITH", lf, BOLD_MUTE, 4)
    names = [str(s).strip().upper() for s in (sponsors or []) if str(s).strip()]
    ny = y + 54
    if not names:
        d.text((x + 30, ny), "Presented with our partners", font=_f(MONT_SB, 28),
               fill=BOLD_INK)
        return
    text = "     ".join(names)
    nf = _f(OSWALD_B, 30)
    # shrink if the joined names overrun the strip width
    s = nf.size
    while _tw(d, text, nf) > w - 60 and s > 18:
        s -= 2
        nf = _f(OSWALD_B, s)
    d.text((x + 30, ny), text, font=nf, fill=BOLD_INK)


# ---- bold PHOTO background + legibility scrim -------------------------------
# Blake ruling: each bold summit card should sit OVER a real event-scene photo
# (gym owners at a summit in athleisure), not the flat dark base. The photo is
# cover-cropped to the exact card size (reusing the house _cover primitive), then
# a two-band BOLD_BG scrim is composited so the oversized ANTON headline, the
# numeric callouts, the URL, the dash-free event lockup, and the sponsor strip all
# stay legible over ANY photo. The whole bold overlay is still PIL-drawn ON TOP,
# so text is never model-rendered (garble-proof) and the accent side rail stays.
# When no background is provided the flat BOLD_BG behavior is UNCHANGED.

# Configurable background library. The operator drops jpg/png into feed/ and
# story/ subdirs; this code only consumes them. Missing/empty -> flat dark.
SUMMIT_BG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "content_library", "summit_bg")
_BG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _bold_base_with_bg(bg_path, w, h):
    """A (w, h) canvas from a real event photo, cover-cropped to the EXACT card
    size (reusing the house _cover primitive so a portrait is never squished), then
    darkened by a vertical BOLD_BG scrim: high opacity in the TOP band (behind the
    headline + callouts) and the BOTTOM band (behind the URL, event lockup, and
    sponsor strip), easing to ~35-45% through the middle so the crowd photo shows
    through. A slight overall darken sits under all of it. Tuned so cream/white text
    and the electric accent stay legible over any photo. Returns an RGB image.

    Never crashes the card: the caller falls back to the flat dark base if opening
    or cropping the photo raises (a bad/corrupt file must not sink the sprint)."""
    src = _cover(Image.open(bg_path), w, h)   # exact size, cover-cropped, no squish
    scrim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    br, bg_, bb = BOLD_BG
    # The top band holds a STRONG near-opaque scrim across the whole headline +
    # callouts zone (down to top_hold), then eases to the mid so the crowd photo
    # shows through the middle; the bottom band ramps strong again behind the URL,
    # the dash-free event lockup, and the sponsor strip. Tuned so cream/white text
    # and the electric accent stay legible over ANY photo.
    top_hold = h * 0.30        # headline zone: hold the strong scrim through here
    top_band = h * 0.46        # ease strong -> mid finishes here
    bot_band = h * 0.66        # URL + event lockup + sponsor strip below here
    a_top, a_mid, a_bot = 236, 108, 238       # ~93% top, ~42% middle, ~93% bottom
    for y in range(h):
        if y <= top_hold:
            a = a_top                                                # strong, flat
        elif y <= top_band:
            a = int(a_top - (a_top - a_mid) * ((y - top_hold) / (top_band - top_hold)))
        elif y <= bot_band:
            a = a_mid                                                # steady mid
        else:
            a = int(a_mid + (a_bot - a_mid) * ((y - bot_band) / (h - bot_band)))
        sd.line([(0, y), (w, y)], fill=(br, bg_, bb, a))
    base = Image.alpha_composite(src.convert("RGBA"), scrim).convert("RGB")
    return base


def _bold_canvas(w, h, bg_path):
    """The base canvas for a bold card: the scrimmed event photo when bg_path is a
    real, openable image; otherwise the flat BOLD_BG field (unchanged behavior).
    NEVER crashes on a missing/corrupt/failed background: any error falls back to
    the flat dark base so the card always renders."""
    if bg_path:
        try:
            return _bold_base_with_bg(bg_path, w, h)
        except Exception as exc:            # corrupt/unreadable photo -> flat dark
            print(f"  bold bg failed ({os.path.basename(str(bg_path))}: {exc}); "
                  "flat dark fallback")
    return Image.new("RGB", (w, h), BOLD_BG)


def _pick_summit_bg(concept_id, background_dir, subdir):
    """Deterministically pick ONE background for a concept from
    `background_dir`/`subdir` (feed/ or story/). Rotates by a STABLE hash of the
    concept id so cards vary across concepts but a given concept always resolves to
    the same photo (stable, reproducible sprints). Returns an absolute path or None.

    None (flat dark fallback) when: no dir given, the subdir is missing, or it holds
    no jpg/png. The images themselves are added by the operator; this only consumes
    a sorted list of them, so the mapping is stable regardless of filesystem order."""
    if not background_dir:
        return None
    sub = os.path.join(background_dir, subdir)
    if not os.path.isdir(sub):
        return None
    files = sorted(f for f in os.listdir(sub)
                   if os.path.splitext(f)[1].lower() in _BG_EXTS)
    if not files:
        return None
    # stable hash (not Python's salted hash()): same id -> same index every run
    import hashlib
    idx = int(hashlib.sha1(str(concept_id).encode("utf-8")).hexdigest(), 16) % len(files)
    return os.path.join(sub, files[idx])


def _summit_bold_feed(concept, out_path, sponsors=(), background_path=None):
    """Render ONE bold summit sprint FEED card (1080x1080), PIL-composited.

    Layout (color-blocked bands, top to bottom):
      - deep midnight base with a big accent side rail (the loud identity)
      - accent event tag + oversized condensed headline (ONE accent word)
      - big numeric callouts for the concept's data point(s)
      - the event lockup band (dates + venue), dash-free, on every card
      - a bottom sponsor strip (PRESENTED WITH), injectable + never fabricated

    Distinct from the daily house card by construction: dark base, electric accent,
    color blocks, oversized type. When `background_path` is a real event photo it is
    cover-cropped + scrimmed underneath and the bold overlay composites on top; when
    None the flat BOLD_BG behavior is unchanged. Returns out_path."""
    img = _bold_canvas(SIZE, SIZE, background_path)
    d = ImageDraw.Draw(img)
    x = MARGIN
    w = SIZE - 2 * MARGIN

    # loud accent side rail down the left edge (color-blocked, not the soft house card)
    d.rectangle([0, 0, 18, SIZE], fill=BOLD_ACCENT)

    y = MARGIN
    _bold_accent_lockup(d, x, y)
    y += 60

    # eyebrow, then the oversized condensed headline with the ONE accent word
    _tracked(d, (x, y), concept["eyebrow"].upper(), _f(OSWALD_B, 28), BOLD_MUTE, 5)
    y += 58
    red_tokens = set(w0.strip(".,").upper() for w0 in concept["red_word"].split())
    # cap the headline so even a 3-line block leaves room for the callouts below
    hf, lines = _fit(d, concept["headline"].upper(), w, 3, 116)
    hy = _headline(d, x, y, lines, hf, red_tokens, BOLD_INK)

    # bottom bands are pinned; the URL, event band, and sponsor strip live here.
    strip_h = 132
    strip_y = SIZE - strip_h
    band_y = strip_y - 96          # NOVEMBER ... / VIRGIN HOTEL ...
    url_y = band_y - 58            # LASSOFRAMEWORK.COM/SUMMIT

    # big numeric callouts (data read loud, not buried) flow in the gap BETWEEN the
    # headline bottom and the URL line, so a tall headline never collides with them.
    callouts = BOLD_CALLOUTS.get(concept["id"], [("100", "SEATS"), ("2", "DAYS")])
    co_y = hy + 40
    _bold_callouts_row(d, x, co_y, callouts, w, big=True, max_h=url_y - 24 - co_y)

    _tracked(d, (x, url_y), "LASSOFRAMEWORK.COM/SUMMIT",
             _f(OSWALD_B, 26), BOLD_ACCENT, 3)
    _bold_event_band(d, x, band_y, w)
    _bold_sponsor_strip(d, 0, strip_y, SIZE, strip_h, sponsors)

    img.save(out_path)
    return out_path


def _summit_bold_story(concept, out_path, sponsors=(), background_path=None):
    """Render ONE bold summit sprint STORY card (1080x1920), PIL-composited.

    Same bold identity as the feed, laid out for the 9:16 frame with top/bottom safe
    bands so nothing lands under IG chrome. Event lockup on the card; sponsor strip at
    the bottom (injectable, never fabricated). When `background_path` is a real event
    photo it is cover-cropped to the tall frame + scrimmed underneath and the bold
    overlay composites on top; when None the flat BOLD_BG behavior is unchanged.
    Returns out_path."""
    img = _bold_canvas(STORY_W, STORY_H, background_path)
    d = ImageDraw.Draw(img)
    x = STORY_MARGIN
    w = STORY_W - 2 * STORY_MARGIN

    d.rectangle([0, 0, 20, STORY_H], fill=BOLD_ACCENT)  # loud accent rail

    y = STORY_SAFE_TOP
    _bold_accent_lockup(d, x, y)
    y += 74

    _tracked(d, (x, y), concept["eyebrow"].upper(), _f(OSWALD_B, 32), BOLD_MUTE, 5)
    y += 74
    red_tokens = set(w0.strip(".,").upper() for w0 in concept["red_word"].split())
    s = 150
    while s >= 72:
        fo = _f(ANTON, s)
        ls = _wrap(d, concept["headline"].upper(), fo, w)
        if len(ls) <= 4:
            break
        s -= 4
    hy = _headline(d, x, y, ls, fo, red_tokens, BOLD_INK)

    # pinned bottom bands (URL, event band, sponsor strip)
    strip_h = 150
    strip_y = STORY_H - STORY_SAFE_BOT
    band_y = strip_y - 110
    url_y = band_y - 66

    # callouts flow in the gap between headline and URL, auto-fit to the space
    callouts = BOLD_CALLOUTS.get(concept["id"], [("100", "SEATS"), ("2", "DAYS")])
    co_y = hy + 56
    _bold_callouts_row(d, x, co_y, callouts, w, big=True, max_h=url_y - 30 - co_y)

    _tracked(d, (x, url_y), "LASSOFRAMEWORK.COM/SUMMIT",
             _f(OSWALD_B, 30), BOLD_ACCENT, 3)
    _bold_event_band(d, x, band_y, w)
    _bold_sponsor_strip(d, 0, strip_y, STORY_W, strip_h, sponsors)

    img.save(out_path)
    return out_path


def render_bold_feed(concept, treatment, out_path, sponsors=(),
                     background_path=None, background_dir=None):
    """Sprint-path entry for a bold FEED card. `treatment` is accepted for a drop-in
    signature match with the studio feed path (both a/b share the concept's bold
    identity; the treatment no longer forks the LOOK, only the caption arc does).

    Background: pass `background_path` for an explicit photo, or `background_dir` to
    have a background deterministically selected from its feed/ subdir by a stable
    hash of the concept id (cards vary; a given concept is stable). An explicit path
    wins. When neither resolves to a photo the flat BOLD_BG behavior is unchanged."""
    bgp = background_path
    if bgp is None and background_dir:
        bgp = _pick_summit_bg(concept["id"], background_dir, "feed")
    return _summit_bold_feed(concept, out_path, sponsors=sponsors, background_path=bgp)


def render_bold_story(concept, treatment, out_path, sponsors=(),
                      background_path=None, background_dir=None, bg_path=None):
    """Sprint-path entry for a bold STORY card. Signature matches render_card_story
    (`bg_path` is the legacy studio-story alias, accepted for compatibility).

    Background: pass `background_path`/`bg_path` for an explicit photo, or
    `background_dir` to have one deterministically selected from its story/ subdir by
    a stable hash of the concept id. An explicit path wins. When neither resolves to a
    photo the flat BOLD_BG behavior is unchanged."""
    bgp = background_path or bg_path
    if bgp is None and background_dir:
        bgp = _pick_summit_bg(concept["id"], background_dir, "story")
    return _summit_bold_story(concept, out_path, sponsors=sponsors, background_path=bgp)
