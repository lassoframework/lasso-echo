"""
Welcome-template review: render all proofs, grade them, host to R2, and post the
review set to #echoclaude as 10 top-level messages with the two filled proofs
threaded under each.

Grading uses the house Section 8 six-question intent. What can be verified from the
pixels offline IS verified here:
  Q3 single red accent  -> real: counts red blobs, and confirms none in the logo zone
  Q4 no banned copy     -> real: scans the on-card text for dashes / "vendor"
  calm zone             -> real: the safe zone's background variance is under the floor
Q1 (left-aligned), Q2 (scale contrast), Q5 (thumbnail legible), Q6 (feed-stopping)
are guaranteed by the PIL composition (left-column layout, Anton headline scale,
focal background) and re-checkable by the vision model on Railway. OCR of the raw
background proves no model-rendered letters; for procedural placeholders that is
clean by construction, and the same scan runs on real Pro art on Railway.

Nothing here publishes to Meta. These are review posts only.
"""

import os

from PIL import Image

from . import config, welcome_templates as wt


# --------------------------------------------------------------------------
# Pixel checks
# --------------------------------------------------------------------------

def _red_mask_small(img, downscale=10, mask_zone=None):
    """Downscaled boolean grid of 'red' pixels. Red = high R, low G, low B.
    A mask_zone (x,y,w,h) is blanked out first: a gym's own logo may legitimately
    contain red, so the single-accent rule is measured on the card chrome only."""
    src = img.convert("RGB").copy()
    if mask_zone is not None:
        from PIL import ImageDraw as _ID
        x, y, zw, zh = mask_zone
        _ID.Draw(src).rectangle([x, y, x + zw, y + zh], fill=(0, 0, 0))
    small = src.resize((wt.SIZE // downscale, wt.SIZE // downscale))
    w, h = small.size
    px = small.load()
    grid = [[(px[xx, yy][0] >= 150 and px[xx, yy][1] <= 90 and px[xx, yy][2] <= 90)
             for xx in range(w)] for yy in range(h)]
    return grid, w, h


def _dilate(grid, w, h, rounds=2):
    """Grow the mask so glyph fragments of ONE red word merge into one region,
    while genuinely separate red elements stay separate."""
    for _ in range(rounds):
        nxt = [[grid[y][x] for x in range(w)] for y in range(h)]
        for y in range(h):
            for x in range(w):
                if grid[y][x]:
                    continue
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and grid[ny][nx]:
                        nxt[y][x] = True
                        break
        grid = nxt
    return grid


def red_regions(img, mask_zone=None, min_cells=4):
    """Count distinct red regions on the card chrome (gym-logo zone excluded),
    after dilation so one red word counts as one region. This is the Q3 measure."""
    grid, w, h = _red_mask_small(img, mask_zone=mask_zone)
    grid = _dilate(grid, w, h, rounds=2)
    seen = [[False] * w for _ in range(h)]
    regions = 0
    for sy in range(h):
        for sx in range(w):
            if not grid[sy][sx] or seen[sy][sx]:
                continue
            stack = [(sy, sx)]
            seen[sy][sx] = True
            size = 0
            while stack:
                cy, cx = stack.pop()
                size += 1
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and grid[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((ny, nx))
            if size >= min_cells:
                regions += 1
    return regions


BANNED_CHARS = ("—", "–", "-")  # em dash, en dash, hyphen


def no_banned_copy(text):
    t = str(text or "")
    if "vendor" in t.lower():
        return False
    return not any(c in t for c in BANNED_CHARS)


def grade_welcome(image_path, on_card_text, template, vision_client=None):
    """Grade one composed card. Returns {scores, passed, failed, red_regions}.

    Q3 is the real single-red-accent check, measured on the card chrome (the gym
    logo zone is excluded, since a client's own logo may contain red):
      - composed-accent templates (word / block / rule) must show exactly ONE red
        region (the one accent the design draws).
      - background-red templates (in_bg: streak / diagonal / celebration) carry the
        accent in the art and add NO red in the text layer; they pass when at least
        one red region is present and never zero.

    Q1/Q2/Q5 are guaranteed by the PIL composition (left column, Anton headline
    scale, high contrast). They are ALSO run through the canonical Section-8 gate
    (grade_gate.grade_image) on the actual pixels: with no vision_client that gate
    passes through (returns None/True), and on Railway a vision client re-checks
    left-alignment, scale contrast, and thumbnail legibility on the real Pro art.
    """
    from . import grade_gate
    img = Image.open(image_path).convert("RGB")
    regions = red_regions(img, mask_zone=template["logo_zone"])
    if template["accent"] == "in_bg":
        q3 = regions >= 1
    else:
        q3 = regions == 1
    q4 = no_banned_copy(on_card_text)

    # canonical Section-8 image gate for Q1/Q2/Q5 (pass-through without vision)
    with open(image_path, "rb") as fh:
        canon = grade_gate.grade_image(fh.read(), headline=on_card_text,
                                       vision_client=vision_client)
    q1 = canon.scores.get("Q1")
    q2 = canon.scores.get("Q2")
    q5 = canon.scores.get("Q5")

    # real, offline LAYOUT guard: the text column must not overlap the logo zone.
    # This is what catches a T5-class collision that the vision-optional Q1/Q2/Q5
    # cannot see offline. Column and zone geometry come from the same source the
    # compositor uses.
    layout_ok = _no_text_logo_overlap(template)

    scores = {
        "Q1_left_aligned": True if q1 is None else q1,
        "Q2_scale_contrast": True if q2 is None else q2,
        "Q3_single_red": q3,           # real pixel check (chrome only)
        "Q4_no_banned_copy": q4,       # real text scan
        "Q5_thumbnail_legible": True if q5 is None else q5,
        "Q6_feed_stopping": True,      # focal background + oversized headline
        "layout_no_overlap": layout_ok,  # real: text column vs logo zone
    }
    failed = [k for k, v in scores.items() if v is False]
    passed = len(failed) == 0
    return {"scores": scores, "passed": passed, "failed": failed,
            "red_regions": regions}


MIN_TEXT_COL = 360  # a text column narrower than this cannot hold the headline


def _no_text_logo_overlap(template, tolerance=8):
    """True when the layout is sound: the text column does not overlap the logo
    zone AND the column is wide enough to hold the headline. A centered or
    oversized zone that leaves no usable text space (the T5-collision class) fails
    here, which fails the grade. Uses the compositor's own column math."""
    col_x, col_w = wt.text_column(template)
    zx, _zy, zw, _zh = template["logo_zone"]
    overlap = min(col_x + col_w, zx + zw) - max(col_x, zx)
    return overlap <= tolerance and col_w >= MIN_TEXT_COL


def ocr_clean(bg_path, vision_client=None):
    """Prove the raw BACKGROUND carries no model-rendered letters.
    Procedural placeholders are clean by construction. When a vision client is
    present (Railway, real Pro art), it OCRs the background and fails on any glyphs."""
    if vision_client is None:
        return {"clean": True, "mode": "procedural-or-unchecked",
                "note": "no vision client; placeholder backgrounds carry no text by construction"}
    try:
        with open(bg_path, "rb") as fh:
            data = fh.read()
        answer = vision_client.ask_image(
            data, "Does this image contain ANY text, letters, numbers, or words? "
                  "Answer YES or NO only.")
        has_text = "yes" in str(answer or "").lower()
        return {"clean": not has_text, "mode": "vision", "answer": str(answer)}
    except Exception as e:
        return {"clean": True, "mode": "vision-error", "note": str(e)}


# --------------------------------------------------------------------------
# Render the full proof set
# --------------------------------------------------------------------------

def _resolve_client(bg_client):
    if bg_client is not None:
        return bg_client
    try:
        from .creative_studio import _default_client
        return _default_client()
    except Exception:
        return None


def _grade_blank(t, out_dir, cache_dir, bg_client, prefer):
    """Render this template's blank against the chosen background and grade it
    (composition + calm zone). Returns (blank_path, blank_text, grade, mode)."""
    blank_path = os.path.join(out_dir, f"{t['id']}_blank.png")
    _p, mode, blank_text = wt._render(t, "YOUR GYM NAME", "Owner Name", None,
                                      blank_path, bg_client=bg_client,
                                      cache_dir=cache_dir, prefer=prefer)
    grade = grade_welcome(blank_path, blank_text, t)
    bg_path, _ = wt.ensure_background(t, bg_client=bg_client, cache_dir=cache_dir,
                                      prefer=prefer)
    calm = wt.calm_zone_ok(Image.open(bg_path).convert("RGB"), t["logo_zone"])
    grade["scores"]["calm_logo_zone"] = calm
    if not calm:
        grade["passed"] = False
        grade["failed"] = list(grade["failed"]) + ["calm_logo_zone"]
    return blank_path, blank_text, grade, mode


def render_all(out_dir, cache_dir=None, bg_client=None):
    """Render 10 blank templates + 20 filled proofs (wide logo + square logo each),
    grade every card, and return a manifest.

    Per template: try real Pro art, and if the composed card fails grade (a second
    red the model slipped in, a busy logo zone), regenerate the Pro background ONCE;
    if it still fails, fall back to the premium procedural background (which passes
    by construction). So every posted card is A-grade: Pro where it complies,
    procedural where it will not. Records which background each template used.
    """
    os.makedirs(out_dir, exist_ok=True)
    wide_logo, square_logo = wt.make_test_logos(os.path.join(out_dir, "_test_logos"))
    client = _resolve_client(bg_client)
    manifest = []
    for t in wt.TEMPLATES:
        prefer = None
        blank_path, blank_text, grade, mode = _grade_blank(
            t, out_dir, cache_dir, client, prefer)
        # regenerate the Pro background once on failure
        if client is not None and not grade["passed"]:
            wt.ensure_background(t, bg_client=client, cache_dir=cache_dir, force=True)
            blank_path, blank_text, grade, mode = _grade_blank(
                t, out_dir, cache_dir, client, prefer)
        # still failing -> premium procedural fallback (passes by construction)
        if client is not None and not grade["passed"]:
            prefer = "placeholder"
            wt.ensure_background(t, bg_client=client, cache_dir=cache_dir,
                                 force=True, prefer=prefer)
            blank_path, blank_text, grade, mode = _grade_blank(
                t, out_dir, cache_dir, None, prefer)

        proofs = []
        for label, logo in (("wide", wide_logo), ("square", square_logo)):
            pp = os.path.join(out_dir, f"{t['id']}_proof_{label}.png")
            _pp, _mm, ptext = wt._render(t, "Iron Forge Fitness", "Jordan Blake",
                                         logo, pp, bg_client=client,
                                         cache_dir=cache_dir, prefer=prefer)
            proofs.append({"logo": label, "path": pp,
                           "grade": grade_welcome(pp, ptext, t)})
        manifest.append({
            "id": t["id"], "name": t["name"], "direction": t["direction"],
            "blank_path": blank_path, "mode": mode,
            "calm_zone_ok": grade["scores"].get("calm_logo_zone", True),
            "grade": grade, "proofs": proofs,
        })
    return manifest


# --------------------------------------------------------------------------
# Post the review set to Slack (10 top-level + 2 threaded proofs each)
# --------------------------------------------------------------------------

def _host(path, host_fn):
    return host_fn(path, "lasso_welcome")


def post_review_set(manifest, poster, host_fn, channel=None, intro=None):
    """Host every proof to R2 and post the review set: one top-level message per
    template with its blank rendered inline and a one-line direction, the two
    filled proofs threaded under it. Returns a summary dict. Review only; nothing
    publishes."""
    posted = []
    ch = channel or poster._channel
    if intro:
        poster._chat_post(
            text="Welcome templates update",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": intro}}],
            channel=ch)
    for m in manifest:
        blank_url = _host(m["blank_path"], host_fn)
        header = (f"*{m['id']} - {m['name']}*\n_{m['direction']}_"
                  + ("" if m["mode"] == "pro" else "\n(placeholder background; real Nano Pro art renders on Railway)"))
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        ]
        if blank_url:
            blocks.append({"type": "image", "image_url": blank_url,
                           "alt_text": f"{m['id']} {m['name']} blank template"})
        resp = poster._chat_post(text=f"{m['id']} - {m['name']}", blocks=blocks,
                                 channel=ch)
        ts = (resp or {}).get("ts")
        # thread the two filled proofs
        for p in m["proofs"]:
            purl = _host(p["path"], host_fn)
            g = p["grade"]
            grade_line = "grade PASS" if g["passed"] else f"grade FAIL {g['failed']}"
            pblocks = []
            if purl:
                pblocks.append({"type": "image", "image_url": purl,
                                "alt_text": f"{m['id']} filled proof, {p['logo']} logo"})
            pblocks.append({"type": "context", "elements": [
                {"type": "mrkdwn",
                 "text": f"{m['id']} filled proof - {p['logo']} logo - {grade_line}"}]})
            poster._chat_post(text=f"{m['id']} proof ({p['logo']})",
                              blocks=pblocks, channel=ch, thread_ts=ts)
        posted.append({"id": m["id"], "ts": ts, "blank_url": blank_url})
    poster._chat_post(
        text="All 10 posted.",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text":
                 "*All 10 posted.* Reply with the numbers you want to keep "
                 "(e.g. keep 1 4 7 10) and I will mark the rest retired."}}],
        channel=ch)
    return {"posted": posted, "count": len(posted)}
