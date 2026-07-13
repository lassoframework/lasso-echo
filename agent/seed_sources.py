"""
seed-sources: ingest a gym's intake bundle into its client sources in one step.

    python -m agent seed-sources --account <key> [--file <path>] [--review]

This is how the launch gyms get stocked before day one. It reads a simple markdown
bundle and lands each fact as a client source for the account.

Bundle format (one category per section, one fact per line, optional citation):

    # offer
    - 6 week challenge for $199 (website /pricing)
    - Free intro session for new members
    # service
    - Small group personal training (website /services)
    # testimonial
    - Sarah lost 30 pounds in 3 months (member Sarah M)

A trailing "(...)" on a line is read as that fact's source citation; without one
the fact gets the intake:<account_key> default. Section headers must be one of the
six client categories (offer / service / testimonial / faq / about / promo).

By default facts land APPROVED (a human is running this on vetted material). Pass
--review to hold everything PENDING for a second set of eyes before Echo can draft
from it. All-or-nothing: an unknown category aborts with a clear error and stores
nothing.
"""

import os
import re
import sys

from . import client_sources

_HEADER_RE = re.compile(r'^#+\s*([A-Za-z_]+)\s*$')
_BULLET_RE = re.compile(r'^[-*]\s+')
_CITE_RE = re.compile(r'^(.*?)\s*\(([^()]+)\)\s*$')


def _default_path(account_key):
    return os.path.join("brand_voice", "clients", account_key, "sources.md")


def parse_bundle(text):
    """Parse the markdown bundle into {category: [(text, citation)]}. Lines before
    the first header are ignored; blank lines and empty categories are dropped."""
    bundle = {}
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        header = _HEADER_RE.match(line)
        if header:
            current = header.group(1).strip().lower()
            bundle.setdefault(current, [])
            continue
        if current is None:
            continue
        line = _BULLET_RE.sub("", line).strip()
        cite = _CITE_RE.match(line)
        if cite:
            fact, citation = cite.group(1).strip(), cite.group(2).strip()
        else:
            fact, citation = line, ""
        if fact:
            bundle[current].append((fact, citation))
    return {k: v for k, v in bundle.items() if v}


def seed_from_file(account_key, path, review=False):
    """Read the bundle at `path` and land it for the account. Returns
    (created, bundle). Raises ValueError on an unknown category (stores nothing)."""
    with open(path, encoding="utf-8") as fh:
        bundle = parse_bundle(fh.read())
    status = "pending" if review else "approved"
    created = client_sources.submit_intake(account_key, bundle, status=status)
    return created, bundle


_USAGE = ("usage: python -m agent seed-sources --account <key> "
          "[--file <path>] [--review]")


def cli(args):
    account_key = path = None
    review = False
    i = 0
    while i < len(args):
        if args[i] == "--account" and i + 1 < len(args):
            account_key = args[i + 1]; i += 2; continue
        if args[i] == "--file" and i + 1 < len(args):
            path = args[i + 1]; i += 2; continue
        if args[i] == "--review":
            review = True; i += 1; continue
        print(f"unrecognized: {args[i]}\n{_USAGE}")
        sys.exit(2)
    if not account_key:
        print(_USAGE)
        sys.exit(2)
    path = path or _default_path(account_key)
    if not os.path.isfile(path):
        print(f"seed-sources: no intake bundle at {path}. "
              "Pass --file <path> or create it.")
        sys.exit(2)
    try:
        created, _bundle = seed_from_file(account_key, path, review=review)
    except ValueError as e:
        print(f"seed-sources: {e}")
        sys.exit(1)

    status = "held for review (pending)" if review else "approved"
    by_cat = {}
    for s in created:
        by_cat[s.category] = by_cat.get(s.category, 0) + 1
    print(f"seed-sources: {account_key} <- {path}")
    for cat in client_sources.CLIENT_CATEGORIES:
        if cat in by_cat:
            print(f"  {cat:<12} {by_cat[cat]}")
    print(f"  total {len(created)} source(s), {status}")
    if review:
        print("  approve them (approve-all / the review flow) before Echo drafts "
              "from these.")
    sys.exit(0)
