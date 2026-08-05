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
    "04_funnel": ("bars", [("CLOSE RATE", 70), ("SHOW RATE", 50),
                           ("BOOKING", 50), ("LEAD VOLUME", 40)]),
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
    "04_funnel": ["Close rate 70%+ is the first leg",
                  "Show rate 50%+ and booking 50%+",
                  "Lead volume 40%+ comes last",
                  "Fix the broken leg, then scale"],
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
        # cap the headline (3 lines / smaller) so the dense content band + strip fit
        hf, lines = _fit(d, concept["headline"].upper(), cw, 3, 96)
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
            row = max(46, min(_POINT_ROW, (strip_y - 58 - y) // (len(pts) - 1)))
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
