"""
Contact sheet CLI (operator hygiene Part B).

    python -m agent contact-sheet --set <brand|service|b2b> | --all [--out PATH]

One self contained HTML grid of the CURRENT library renders for a set, built
straight from library state (each concept's sidecar: live R2 url, key, pillar),
plus a review hint line per card; stat cards (a `cite` on the concept) get the
numeral hint "Read the number character by character." A concept not rendered
yet says so honestly instead of a broken image.

READ ONLY against the library: sidecars are read, nothing in the library moves.
The only writes are the HTML file itself (--out, default under the library)
and its upload to the same R2 bucket at echo/contact_sheets/<set>_<date>.html;
the public URL prints at the end. Hosting dark = the local path still prints
(the sheet is still usable from disk). Visible copy is dash free.
"""

import html as _html
import json
import os
from datetime import date

from . import config, media_host
from .regen_library import CONCEPTS, V2_PREFIX

SHEET_PREFIX = "echo/contact_sheets"
STAT_HINT = "Read the number character by character."
DEFAULT_HINT = "Check the headline wording and the single red accent."


def _sets():
    return sorted({v.get("set", "brand") for v in CONCEPTS.values()})


def _sidecar(library_path, key):
    try:
        with open(os.path.join(library_path, f"{V2_PREFIX}{key}.json"),
                  encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def gather(set_name, library_path=None):
    """[{key, url, pillar, hint, headline}] for one set (or 'all'), one entry
    per concept, library order. Read only."""
    library_path = library_path or config.LIBRARY_PATH
    entries = []
    for key, spec in CONCEPTS.items():
        if set_name != "all" and spec.get("set", "brand") != set_name:
            continue
        side = _sidecar(library_path, key)
        entries.append({
            "key": key,
            "url": side.get("public_url", ""),
            "pillar": side.get("pillar") or spec.get("pillar")
                      or spec.get("set", "brand"),
            "hint": STAT_HINT if spec.get("cite") else DEFAULT_HINT,
            "headline": spec["headline"],
        })
    return entries


def build_html(set_name, entries, day=None):
    """The self contained sheet: a plain table grid, no external assets. The
    visible copy carries no dash characters (the date renders as words; the
    ISO form lives only in the filename and upload key)."""
    day = day or date.today().isoformat()
    day = date.fromisoformat(day).strftime("%d %b %Y")  # dash free on the page
    e = _html.escape
    cards = []
    for item in entries:
        img = (f'<img src="{e(item["url"])}" width="320">' if item["url"]
               else "<p><b>not rendered yet</b> (run regen for this key)</p>")
        cards.append(
            "<td style=\"padding:16px;background:#ffffff;border:1px solid #ddd\">"
            f"{img}"
            f"<p><b>{e(item['key'])}</b></p>"
            f"<p>{e(item['headline'])}</p>"
            f"<p>pillar: {e(item['pillar'])}</p>"
            f"<p><i>review: {e(item['hint'])}</i></p></td>")
    rows, per_row = [], 3
    for i in range(0, len(cards), per_row):
        rows.append("<tr>" + "".join(cards[i:i + per_row]) + "</tr>")
    return (
        "<html><head><title>"
        f"LASSO contact sheet: {e(set_name)} ({e(day)})</title></head>"
        "<body style=\"background:#FAF6F0;color:#121E3C\">"
        f"<h1>LASSO contact sheet: {e(set_name)} set, {e(day)}</h1>"
        f"<p>{len(entries)} concept(s), current library renders. Review each "
        "card against its hint before approving anything into rotation.</p>"
        f"<table>{''.join(rows)}</table></body></html>")


def sheet_key(set_name, day=None):
    return f"{SHEET_PREFIX}/{set_name}_{day or date.today().isoformat()}.html"


def run(set_name, out_path=None, s3_client=None, library_path=None):
    """Build, write, and upload one sheet. Returns {set, path, count, key, url}."""
    valid = _sets() + ["all"]
    if set_name not in valid:
        print(f"unknown set: {set_name} ({', '.join(valid)})")
        return None
    entries = gather(set_name, library_path)
    if not entries:
        print(f"contact-sheet: no concepts in set {set_name}; nothing to build.")
        return None
    day = date.today().isoformat()
    text = build_html(set_name, entries, day)
    out_path = out_path or os.path.join(library_path or config.LIBRARY_PATH,
                                        f"contact_sheet_{set_name}_{day}.html")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    key = sheet_key(set_name, day)
    url = ""
    client = s3_client
    if client is None and config.hosting_enabled():
        client = media_host._default_client()
    if client is not None:
        try:
            client.put(key, out_path)
            url = f"{config.S3_PUBLIC_BASE_URL.rstrip('/')}/{key}"
        except Exception as e:
            print(f"contact-sheet: upload failed ({type(e).__name__}: {e}); "
                  "the local file below still works.")
    else:
        print("contact-sheet: hosting is dark (flag or credentials); local only.")
    print(f"contact-sheet: {len(entries)} concept(s) on {out_path}")
    if url:
        print(f"contact-sheet: {url}")
    return {"set": set_name, "path": out_path, "count": len(entries),
            "key": key, "url": url}


def cli(args):
    """python -m agent contact-sheet --set <name>|--all [--out PATH]."""
    set_name, out_path = None, None
    i = 0
    while i < len(args):
        if args[i] == "--set" and i + 1 < len(args):
            set_name = args[i + 1]; i += 2; continue
        if args[i] == "--all":
            set_name = "all"; i += 1; continue
        if args[i] == "--out" and i + 1 < len(args):
            out_path = args[i + 1]; i += 2; continue
        print(f"unrecognized argument: {args[i]}\n"
              "usage: python -m agent contact-sheet --set <name>|--all [--out PATH]")
        return
    if not set_name:
        print("usage: python -m agent contact-sheet --set <name>|--all [--out PATH]")
        return
    run(set_name, out_path)
