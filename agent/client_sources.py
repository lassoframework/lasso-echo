"""
Per-gym source docs: a client account's own approved material, scoped to that
account, exactly like LASSO's doctrine sources but per-tenant.

Each gym holds its website copy, offers, services, testimonials, FAQ, about, and
promos as CATEGORIZED, CITED content. Every stored item carries account_key,
category, and a source citation (where the fact came from). The six categories:

  offer        a specific offer, package, or price the gym runs
  service      a service or program the gym provides
  testimonial  a member result or quote (client-provided, permission assumed at intake)
  faq          a common question + answer
  about        who the gym is, its story, its coaches
  promo        a time-boxed promotion or event

STATUS is the hard gate. A source is 'pending' until a human approves it; the
drafting path (client_content) reads ONLY approved sources, so client-submitted
material is NEVER auto-trusted as fact. The fabrication gate stays the sole
authority on claims: a caption may only assert what an APPROVED source of THAT
account already says.

Backed by the client_sources table in agent/db.py (WAL on /data). No flag gates
storage itself; the AGENT_CLIENT_SOURCES flag gates whether client accounts DRAFT
from these (see config.client_sources_enabled and client_content).
"""

from dataclasses import dataclass

from . import db

# The client content set. Kept small and concrete on purpose: every category maps
# to something a gym owner can actually hand us on day one.
CLIENT_CATEGORIES = ("offer", "service", "testimonial", "faq", "about", "promo")

_STATUSES = ("approved", "pending")


@dataclass
class ClientSource:
    id: int
    account_key: str
    category: str
    text: str
    citation: str
    status: str
    created_at: str = ""


def _norm(account_key, category, text, citation, status):
    account_key = (account_key or "").strip()
    category = (category or "").strip().lower()
    text = (text or "").strip()
    citation = (citation or "").strip()
    status = (status or "").strip().lower()
    if not account_key:
        raise ValueError("account_key is required")
    if category not in CLIENT_CATEGORIES:
        raise ValueError(
            f"unknown category {category!r}; one of {CLIENT_CATEGORIES}")
    if not text:
        raise ValueError("source text is required (never store an empty fact)")
    if status not in _STATUSES:
        raise ValueError(f"status must be one of {_STATUSES}, got {status!r}")
    if not citation:
        # provenance must never be blank: default to the account itself so every
        # row is traceable even when the caller forgot to name a source.
        citation = f"client:{account_key}"
    return account_key, category, text, citation, status


def add_source(account_key, category, text, citation="", status="approved"):
    """Store one source item for one account in one category. Returns the
    ClientSource. Validates category and non-empty text; a blank citation
    defaults to client:<account_key> so provenance is never lost."""
    account_key, category, text, citation, status = _norm(
        account_key, category, text, citation, status)
    with db._lock, db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO client_sources (account_key, category, text, citation, "
            "status) VALUES (?,?,?,?,?)",
            (account_key, category, text, citation, status))
        conn.commit()
        source_id = cur.lastrowid
    db.audit("client_source_add", account_key,
             f"{category} ({status}) cite={citation}", account_key)
    return ClientSource(id=source_id, account_key=account_key, category=category,
                        text=text, citation=citation, status=status)


def submit_intake(account_key, bundle, status="pending", default_citation=""):
    """
    Land a client's intake bundle (the intake form, or uploaded material) as
    source docs for THAT account, PENDING by default — held for human approval
    before Echo can draft from it. Client input is NEVER auto-trusted as fact.

    bundle: {category: [item, ...]} where each item is either the fact string or
    a (text, citation) pair. Blank items are skipped. Returns the list of
    ClientSource created. Validates every category up front, so an unknown
    category raises and NOTHING is stored (all-or-nothing).
    """
    account_key = (account_key or "").strip()
    if not account_key:
        raise ValueError("account_key is required")
    if status not in _STATUSES:
        raise ValueError(f"status must be one of {_STATUSES}, got {status!r}")
    # Validate + normalize BEFORE any insert so a malformed bundle stores nothing.
    normalized = []
    for category, items in (bundle or {}).items():
        cat = (category or "").strip().lower()
        if cat not in CLIENT_CATEGORIES:
            raise ValueError(
                f"unknown category {category!r}; one of {CLIENT_CATEGORIES}")
        for item in (items or []):
            if isinstance(item, (list, tuple)):
                text = item[0] if item else ""
                citation = item[1] if len(item) > 1 else ""
            else:
                text, citation = item, ""
            if not (text or "").strip():
                continue  # skip blank lines quietly
            citation = (citation or "").strip() or default_citation \
                or f"intake:{account_key}"
            normalized.append((cat, text, citation))
    created = [add_source(account_key, cat, text, citation, status=status)
              for cat, text, citation in normalized]
    db.audit("client_intake", account_key,
             f"landed {len(created)} {status} source(s)", account_key)
    return created


def _rows(account_key, status=None, category=None):
    q = ("SELECT id, account_key, category, text, citation, status, created_at "
         "FROM client_sources WHERE account_key=?")
    args = [(account_key or "").strip()]
    if status is not None:
        q += " AND status=?"
        args.append(status)
    if category is not None:
        q += " AND category=?"
        args.append((category or "").strip().lower())
    q += " ORDER BY id"
    with db.connect() as conn:
        return [ClientSource(id=r["id"], account_key=r["account_key"],
                             category=r["category"], text=r["text"],
                             citation=r["citation"], status=r["status"],
                             created_at=r["created_at"] or "")
                for r in conn.execute(q, args)]


def approved_sources(account_key, category=None):
    """The account's APPROVED sources (optionally one category). The ONLY set the
    drafting path may read: pending material is never returned here."""
    return _rows(account_key, status="approved", category=category)


def pending_sources(account_key, category=None):
    """The account's PENDING sources awaiting human approval."""
    return _rows(account_key, status="pending", category=category)


def all_sources(account_key, category=None):
    """Every source for the account, any status (for review/reporting)."""
    return _rows(account_key, status=None, category=category)


def approve_source(source_id):
    """Flip one pending source to approved. Returns True if a row changed."""
    with db._lock, db.connect() as conn:
        cur = conn.execute(
            "UPDATE client_sources SET status='approved' WHERE id=?", (source_id,))
        conn.commit()
        return cur.rowcount > 0


def approve_all(account_key):
    """Approve every pending source for one account. Returns the count changed."""
    account_key = (account_key or "").strip()
    with db._lock, db.connect() as conn:
        cur = conn.execute(
            "UPDATE client_sources SET status='approved' "
            "WHERE account_key=? AND status='pending'", (account_key,))
        conn.commit()
        n = cur.rowcount
    if n:
        db.audit("client_source_approve", account_key,
                 f"approved {n} pending source(s)", account_key)
    return n


def categories_present(account_key, status="approved"):
    """The categories this account has content in, in canonical order. Drives the
    per-client category spread in client_content."""
    have = {s.category for s in _rows(account_key, status=status)}
    return [c for c in CLIENT_CATEGORIES if c in have]


def approved_claims(account_key):
    """
    Claim sentences that clear the fabrication gate for this account: the text of
    every APPROVED source, plus its dash/vendor-filtered form (so a caption that
    has been cleaned for the copy law still matches its own source). ONLY approved
    sources; a pending source never clears a claim. Never LASSO's global stats.
    """
    from .content_categories import filter_platform_copy
    claims = []
    for s in approved_sources(account_key):
        claims.append(s.text)
        cleaned = filter_platform_copy(s.text)
        if cleaned and cleaned != s.text:
            claims.append(cleaned)
    return claims
