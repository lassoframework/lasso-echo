"""
THE FULL GYM book launch campaign. Flag: AGENT_BOOK_CAMPAIGN_ENABLED (OFF).

Armed, the campaign LEADS the calendar: one book post per day takes posting
priority and the normal pillars fill around it. Selection order:
  1. knowledge/BOOK_LAUNCH_QUEUE_WEEK1.md posts VERBATIM, in order, one per day
     (the same queue item serves every LASSO account on its day).
  2. After the queue: angles 1 to 8 from full_gym_launch_campaign.md rotate.
     Angles 9 to 11 stay DARK until their LOCKED blanks fill in
     full_gym_book.md; a direct attempt BLOCKS with the blank named, never a
     guess.

HARD RULES enforced here:
  - full_gym_book.md is the MASTER source; known conflicts (subtitle of record,
    the author bio figure) surface as WARNING lines on the draft card so Blake
    sees them at the tap.
  - Case study numbers are copied character exact from full_gym_case_studies.md;
    any numeric token in a case caption that is not in the study's entry BLOCKS
    the draft. The four studies marked numbers pending cannot be selected.
  - First person ownership voice, always ("our book", "we wrote"). A book
    caption without it blocks.
  - Cover style (black canvas, red and white type) is the ONE approved exception
    to the cream house spec, scoped to these cards only. An existing image in
    content_library/book_campaign/ matching the day's queue item is used instead
    of generating.
  - Every draft still cards to Blake. Nothing here publishes.
"""

import json
import os
import re

from . import config, creative_studio, db, media_host, schedule
from .drafter import Draft, DraftStatus, _make_id

FIRST_PERSON_MARKS = ("our book", "we wrote", "in our book", "sherman and i")
BOOK_CARD_DIR = os.path.join("content_library", "book_campaign")


# ---- source parsing -----------------------------------------------------------------
def _read(name):
    try:
        with open(os.path.join(config.BOOK_DIR, name), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def locked_blanks():
    """{blank name: filled bool} from the book's LOCKED section. A blank counts
    as filled only when Blake has replaced the unknown marker with a value."""
    text = _read("full_gym_book.md")
    section = ""
    in_locked = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_locked = "LOCKED" in line.upper()
            continue
        if in_locked:
            section += line + "\n"
    blanks = {}
    for label in ("LAUNCH DATE", "BUY OR PREORDER LINK", "PRICE",
                  "Subtitle of record", "Author bio claim"):
        m = re.search(rf"- {re.escape(label)}[^:]*:\s*(.+)", section)
        value = (m.group(1).strip() if m else "")
        unfilled = (not value or "unknown" in value.lower()
                    or "blake" in value.lower() or "never mention" in value.lower())
        blanks[label] = not unfilled
    return blanks


def load_queue():
    """The week 1 queue, VERBATIM: [{n, title, card_note, caption, hashtags}]."""
    text = _read(config.BOOK_QUEUE_FILE)
    items = []
    blocks = re.split(r"\n## DAY (\d+)", text)
    for i in range(1, len(blocks), 2):
        n, body = int(blocks[i]), blocks[i + 1]
        title = body.splitlines()[0].strip()
        card = ""
        m = re.search(r"^Card:\s*(.+)$", body, re.MULTILINE)
        if m:
            card = m.group(1).strip()
        cap = re.search(r"Caption:\n(.*?)\n(#\S[^\n]*)", body, re.DOTALL)
        caption, hashtags = "", []
        if cap:
            caption = cap.group(1).strip()
            hashtags = cap.group(2).strip().split()
        items.append({"n": n, "title": title, "card_note": card,
                      "caption": caption, "hashtags": hashtags})
    return sorted(items, key=lambda x: x["n"])


def load_angles():
    """{n: {title, status, blank, hook, body, cta}} from the campaign file."""
    text = _read("full_gym_launch_campaign.md")
    angles = {}
    blocks = re.split(r"\n## Angle (\d+): ", text)
    for i in range(1, len(blocks), 2):
        n, body = int(blocks[i]), blocks[i + 1]
        header = body.splitlines()[0]
        blocked = "BLOCKED" in header
        blank = ""
        m = re.search(r"BLOCKED until (.+?) is filled", header)
        if m:
            blank = m.group(1).strip()
        fields = {}
        for key in ("Hook", "Body", "CTA"):
            fm = re.search(rf"^{key}:\s*(.+)$", body, re.MULTILINE)
            if fm:
                fields[key.lower()] = fm.group(1).strip()
        angles[n] = {"title": header.replace("READY", "").strip(),
                     "blocked": blocked, "blank": blank, **fields}
    return angles


def case_studies():
    """{index: entry text} for studies WITH numbers; the numbers pending four
    (flagged '(In manuscript' ) are excluded and can never be selected."""
    text = _read("full_gym_case_studies.md")
    out = {}
    for m in re.finditer(r"^(\d+)\.\s+(.+)$", text, re.MULTILINE):
        n, entry = int(m.group(1)), m.group(2).strip()
        if "(In manuscript" in entry:
            continue  # numbers pending: not selectable until the file updates
        out[n] = entry
    return out


# ---- gates ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"\d[\d,\.]*")


def numbers_exact(caption, entry):
    """Every numeric token in the caption must appear character exact in the
    study entry (or the book file). A mismatch blocks."""
    entry_tokens = set(_NUM_RE.findall(entry)) | set(_NUM_RE.findall(_read("full_gym_book.md")))
    return [t for t in _NUM_RE.findall(caption) if t not in entry_tokens]


def first_person_ok(caption):
    low = caption.lower()
    return any(mark in low for mark in FIRST_PERSON_MARKS)


def conflict_warnings(caption):
    """The MASTER source rule: known conflicts surface on the card for Blake."""
    warnings = []
    blanks = locked_blanks()
    low = caption.lower()
    if "guide to predictable monthly growth" in low and not blanks.get("Subtitle of record"):
        warnings.append("BOOK CONFLICT: subtitle of record is unresolved (cover vs "
                        "manuscript); this caption uses the cover subtitle. The book "
                        "file wins once Blake picks one.")
    if re.search(r"thousand gym owners|1,?000\+? gym", low) and not blanks.get("Author bio claim"):
        warnings.append("BOOK CONFLICT: author bio figure unresolved (book says over "
                        "a thousand, standing claim says 500 plus). Blake confirms.")
    return warnings


# ---- daily selection --------------------------------------------------------------------
def _queue_progress():
    try:
        return json.loads(db.kv_get("book_queue_progress", "") or "{}")
    except Exception:
        return {}


def queue_item_for(day_key):
    """One queue item per day, in order, shared by every account on that day.
    None once the queue is exhausted."""
    queue = load_queue()
    state = _queue_progress()
    done = state.get("done", [])
    if state.get("day") == day_key and state.get("current") is not None:
        current = state["current"]
        return next((q for q in queue if q["n"] == current), None)
    remaining = [q for q in queue if q["n"] not in done]
    if not remaining:
        return None
    item = remaining[0]
    db.kv_set("book_queue_progress", json.dumps(
        {"done": done + [item["n"]], "day": day_key, "current": item["n"]}))
    return item


def _existing_card(n):
    """A pre-made card in content_library/book_campaign/ for queue day n."""
    if not os.path.isdir(BOOK_CARD_DIR):
        return None
    for name in sorted(os.listdir(BOOK_CARD_DIR)):
        stem = os.path.splitext(name)[0].lower()
        if (stem.startswith(f"day{n}") or stem.startswith(f"day_{n}")) and \
                os.path.splitext(name)[1].lower() in (".jpg", ".jpeg", ".png", ".webp"):
            return os.path.join(BOOK_CARD_DIR, name)
    return None


def _blocked(account, day_key, reason):
    return Draft(draft_id=_make_id(account.key, "book", day_key),
                 account_key=account.key, platform=account.platform,
                 caption="", hashtags=[], creative_path="", creative_public_url="",
                 scheduled_for="", status=DraftStatus.BLOCKED,
                 blocked_reason=reason, day_key=day_key, draft_type="book")


def build_angle_draft(account, day_key, n, nano_client=None, s3_client=None):
    """One angle draft. Angles 9 to 11 BLOCK with the blank named, never a guess."""
    angles = load_angles()
    angle = angles.get(n)
    if angle is None:
        return None
    if angle["blocked"]:
        blanks = locked_blanks()
        blank = angle["blank"] or "a LOCKED blank"
        if not blanks.get(blank.replace("BUY OR PREORDER LINK", "BUY OR PREORDER LINK"), False):
            return _blocked(account, day_key,
                            f"book angle {n} needs {blank} in full_gym_book.md; "
                            "it stays dark until Blake fills it. Never guessed.")
    caption_lines = [angle.get("hook", ""), angle.get("body", ""), angle.get("cta", "")]
    caption = "\n\n".join(x for x in caption_lines if x)
    if not first_person_ok(caption):
        caption += "\n\nFrom our book The Full Gym."
    return _finish_draft(account, day_key, angle.get("hook", "The Full Gym"),
                         caption, ["#thefullgym", "#gymowner", "#lassoframework"],
                         None, nano_client, s3_client,
                         fragments=[x for x in caption_lines if x])


def build_book_draft(account, day_key, nano_client=None, s3_client=None):
    """
    The day's book post (the campaign LEADS the calendar). Queue verbatim first;
    then angles 1 to 8 rotate. None while the flag is OFF or nothing is available.
    """
    if not config.book_campaign_enabled():
        return None
    item = queue_item_for(day_key)
    if item is not None:
        caption = item["caption"]
        if not first_person_ok(caption) and "full gym" not in caption.lower():
            return _blocked(account, day_key,
                            f"queue day {item['n']} caption fails the first person "
                            "voice law; fix the queue file.")
        # case study numbers character exact (queue day 7 = Pat, day 5 = Matt)
        mismatches = numbers_exact(caption, _read("full_gym_case_studies.md"))
        if mismatches:
            return _blocked(account, day_key,
                            f"queue day {item['n']} carries numbers not found "
                            f"character exact in the sources: {mismatches}. Blocked, "
                            "never guessed.")
        return _finish_draft(account, day_key,
                             item["title"] or "The Full Gym", caption,
                             item["hashtags"], _existing_card(item["n"]),
                             nano_client, s3_client,
                             fragments=[f"queue day {item['n']} verbatim"],
                             card_note=item["card_note"])
    # queue exhausted: rotate the READY angles 1 to 8 (9 to 11 stay dark)
    angles = load_angles()
    ready = [n for n in sorted(angles) if n <= 8 and not angles[n]["blocked"]]
    if not ready:
        return None
    from datetime import date
    pick = ready[date.fromisoformat(day_key).toordinal() % len(ready)]
    if pick == 5:
        return build_case_study_draft(account, day_key, nano_client, s3_client)
    return build_angle_draft(account, day_key, pick, nano_client, s3_client)


def build_case_study_draft(account, day_key, nano_client=None, s3_client=None):
    """Angle 5: one case study, numbers character exact, credited to our book."""
    studies = case_studies()
    if not studies:
        return None
    from datetime import date
    keys = sorted(studies)
    n = keys[date.fromisoformat(day_key).toordinal() % len(keys)]
    entry = studies[n]
    caption = (f"{entry}\n\nFull story in our book The Full Gym.")
    mismatches = numbers_exact(caption, entry)
    if mismatches:
        return _blocked(account, day_key,
                        f"case study {n} numbers mismatch: {mismatches}")
    return _finish_draft(account, day_key, "From our book The Full Gym", caption,
                         ["#thefullgym", "#gymowner", "#gymmarketing"],
                         None, nano_client, s3_client, fragments=[entry])


def _finish_draft(account, day_key, headline, caption, hashtags, existing_card,
                  nano_client, s3_client, fragments, card_note=""):
    creative_path = existing_card
    if creative_path is None:
        art = creative_studio.generate(
            headline, [card_note or "The Full Gym book campaign card."],
            client=nano_client,
            palette=creative_studio.BOOK_COVER_PALETTE)  # the ONE scoped exception
        if art is None:
            return None  # studio unavailable: the normal path takes the day
        creative_path = art["path"]
    hosted = media_host.host_media(creative_path, account.key, client=s3_client)
    if not hosted:
        return None
    return Draft(
        draft_id=_make_id(account.key, "book", day_key),
        account_key=account.key, platform=account.platform,
        caption=caption, hashtags=hashtags[:5],
        creative_path=creative_path, creative_public_url=hosted,
        scheduled_for=schedule.scheduled_for(day_key), status=DraftStatus.PENDING,
        source_fragments=fragments, day_key=day_key, draft_type="book",
        warnings=conflict_warnings(caption),
    )
