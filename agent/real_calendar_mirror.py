"""
real_calendar_mirror.py: fold a gym's REAL Echo drafts into the SHARED
content_calendar so the client portal (/social + /calendar) serves the gym's ACTUAL
plan instead of demo content.

Echo's real drafts live in the draft store (agent/store.py PendingStore -> the drafts
table): the daily category rotation, Summit sprint slots, book slots, welcome posts.
The portal reads content_calendar (agent/portal_calendar_store.py). Before this module,
a real gym's content_calendar could hold DEMO content (seeded from demo_calendar_queue
DEMO_POSTS). This mirror maps a real gym's real drafts into content_calendar rows and
CLEARS any demo-manifest rows off the real gym.

DESIGN (three layers, first two are PURE, no I/O):
  1. collect_real_drafts(account_key, store)  -> the gym's REAL drafts as
     content_calendar row dicts. Demo drafts (demof_/demos_ ids) are EXCLUDED. A feed
     draft and its paired story are SEPARATE rows with the correct format.
  2. mirror_plan(account_key, store, existing_rows) -> a deterministic diff: which
     rows to UPSERT (the real drafts) and which existing DEMO rows to DELETE for this
     gym. No I/O, no ordering surprises.
  3. mirror_to_supabase(account_key, store, sb_store) -> applies the plan through the
     injectable SupabaseCalendarStore (upsert real rows, delete demo rows), GYM SCOPED.

HARD GUARDS:
  * Demo content is valid ONLY for the demo gym id (config.demo_calendar_gym_id(),
    behind AGENT_DEMO_CALENDAR_ENABLED). A REAL gym id must never keep demo-manifest
    draft ids after a mirror: mirror_plan lists every demo row on the real gym for
    DELETE, and refuses to upsert any demo-id draft.
  * A draft with no real hosted creative URL (creative_public_url) is skipped: the
    portal never shows an empty card, and the demo/real split stays honest.
  * Nothing here publishes. It writes calendar rows only.
"""

from . import config
from . import demo_calendar_queue as _demo


# The portal-facing statuses Echo's draft states map to on the content_calendar row.
# A content_calendar.status is the portal's own vocabulary; we translate the Draft
# status into it so an approved draft reads as approved in the portal, a killed draft
# as killed, and everything else as pending (awaiting the portal tap).
_STATUS_MAP = {
    "pending": "pending",
    "approved": "approved",
    "skipped": "denied",
    "superseded": "denied",
    "expired": "denied",
    "blocked": "pending",
}


def _draft_status(draft):
    raw = getattr(draft, "status", None)
    val = getattr(raw, "value", raw)
    return _STATUS_MAP.get(str(val or "").lower(), "pending")


def _draft_format(draft):
    """'story' for a 9:16 story draft, else 'feed'. is_story wins; draft_type is the
    belt-and-braces fallback so a story is never mislabeled a feed."""
    if getattr(draft, "is_story", False):
        return "story"
    if str(getattr(draft, "draft_type", "") or "").lower() == "story":
        return "story"
    return "feed"


def _post_date(draft):
    """The row's post_date (YYYY-MM-DD): the draft's day_key, else the date part of
    scheduled_for. Empty when neither is present (such a draft is skipped upstream)."""
    day_key = (getattr(draft, "day_key", "") or "").strip()
    if day_key:
        return day_key[:10]
    sched = (getattr(draft, "scheduled_for", "") or "").strip()
    return sched[:10] if sched else ""


def _pillar(draft):
    """The row's pillar: the draft's category (rotation pillar) if set, else empty.
    Never invented."""
    return (getattr(draft, "category", "") or "").strip()


def _real_row(account_key, draft):
    """One real draft folded into the content_calendar row shape. gym_id == account_key;
    account == the draft's platform; format from is_story/draft_type. No field invented:
    an empty caption stays empty."""
    return {
        "gym_id": account_key,
        "account": getattr(draft, "platform", "") or "",
        "post_date": _post_date(draft),
        "pillar": _pillar(draft),
        "format": _draft_format(draft),
        "caption": getattr(draft, "caption", "") or "",
        "image_url": getattr(draft, "creative_public_url", "") or "",
        "status": _draft_status(draft),
        # The draft id travels as the row id so an action on the mirrored row round
        # trips to the same draft, and re-mirroring the same draft is an UPSERT (not a
        # duplicate). The portal PATCHes /posts/<id>/... with exactly this id.
        "id": getattr(draft, "draft_id", "") or "",
    }


def collect_real_drafts(account_key, store):
    """The gym's REAL drafts as content_calendar row dicts.

    Included: a draft for THIS account that carries a real hosted creative URL and is
    NOT a demo-manifest draft (demof_/demos_ ids are excluded). PENDING / APPROVED /
    PUBLISHED-equivalent states all map through _draft_status. A feed draft and its
    paired story draft are SEPARATE rows, each with the correct format.

    Excluded: demo drafts (id namespace), drafts with no creative_public_url, and drafts
    with no resolvable post_date (nothing to place on a calendar day).

    PURE: reads only store.list_for_account(account_key); no writes, no network.
    """
    if not account_key or store is None:
        return []
    lister = getattr(store, "list_for_account", None)
    if lister is None:
        return []
    rows = []
    for draft in lister(account_key) or []:
        draft_id = getattr(draft, "draft_id", "") or ""
        if _demo.is_demo_draft_id(draft_id):
            continue  # demo content never enters a real gym's calendar
        if not (getattr(draft, "creative_public_url", "") or "").strip():
            continue  # no hosted image: not a portal-ready card
        row = _real_row(account_key, draft)
        if not row["post_date"]:
            continue  # cannot place on a calendar day
        rows.append(row)
    return rows


def _existing_demo_ids(account_key, existing_rows):
    """Ids of DEMO rows currently on THIS gym in content_calendar. A row is demo iff its
    id is a demo-manifest id AND its gym_id matches the gym we are mirroring (never
    another gym's row)."""
    ids = []
    for r in existing_rows or []:
        if str(r.get("gym_id", "")) != str(account_key):
            continue
        rid = r.get("id")
        if _demo.is_demo_draft_id(rid):
            ids.append(rid)
    return ids


def mirror_plan(account_key, store, existing_rows):
    """A deterministic, I/O-free diff for THIS gym.

    Returns {"upsert": [row, ...], "delete_ids": [id, ...]}:
      * upsert: the gym's REAL drafts as content_calendar rows (collect_real_drafts),
        with any demo-id draft defensively dropped (a real gym must never carry a demo
        id after a mirror).
      * delete_ids: the ids of DEMO rows currently on this gym in content_calendar, so
        the mirror clears demo content off the real gym.

    Gym-scoped: every upsert row carries gym_id == account_key, and delete_ids are drawn
    ONLY from existing_rows whose gym_id == account_key. Another gym's rows are never
    touched.
    """
    upsert = [row for row in collect_real_drafts(account_key, store)
              if not _demo.is_demo_draft_id(row.get("id"))]
    delete_ids = _existing_demo_ids(account_key, existing_rows)
    return {"upsert": upsert, "delete_ids": delete_ids}


def mirror_to_supabase(account_key, store, sb_store):
    """Apply the mirror plan through the injectable SupabaseCalendarStore.

    Reads the gym's current content_calendar rows (sb_store.list_month is month scoped,
    so we sweep the months the real drafts land in PLUS whatever months already hold demo
    rows), computes the plan, upserts the real rows, and deletes the demo rows. Writes
    calendar rows only: NOTHING here publishes.

    GUARD: refuses to run for the demo gym id (that gym is the ONE place demo content is
    valid, behind AGENT_DEMO_CALENDAR_ENABLED). Returns a summary dict; never raises out
    (a store error is reported, never a partial silent failure).
    """
    if not account_key or sb_store is None or store is None:
        return {"ok": False, "reason": "missing account_key or store", "upserted": 0,
                "deleted": 0}
    if account_key == config.demo_calendar_gym_id():
        # The demo gym is the sole valid home of demo content; never mirror over it.
        return {"ok": False, "reason": "refusing to mirror the demo gym id",
                "upserted": 0, "deleted": 0}

    real_rows = collect_real_drafts(account_key, store)
    # Months to reconcile: every month a real draft lands in.
    months = sorted({r["post_date"][:7] for r in real_rows if r.get("post_date")})

    existing = []
    seen_ids = set()
    try:
        for month in months:
            for r in (sb_store.list_month(account_key, month) or []):
                rid = r.get("id")
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                existing.append(r)
    except Exception as exc:
        return {"ok": False, "reason": f"store read failed: {type(exc).__name__}",
                "upserted": 0, "deleted": 0}

    plan = mirror_plan(account_key, store, existing)

    upserted = 0
    deleted = 0
    try:
        upsert = getattr(sb_store, "upsert_row", None)
        for row in plan["upsert"]:
            # Belt-and-braces gym scope: never hand the store a row for another gym.
            if str(row.get("gym_id")) != str(account_key):
                continue
            if upsert is not None:
                upsert(account_key, row)
                upserted += 1
        delete = getattr(sb_store, "delete_row", None)
        for rid in plan["delete_ids"]:
            if delete is not None:
                delete(account_key, rid)
                deleted += 1
    except Exception as exc:
        return {"ok": False, "reason": f"store write failed: {type(exc).__name__}",
                "upserted": upserted, "deleted": deleted}

    return {"ok": True, "upserted": upserted, "deleted": deleted,
            "delete_ids": list(plan["delete_ids"])}
