"""
Retroactive pixel fabrication scan (fail-closed).

Walk every PENDING / planned card in the queue and verify the text rendered INTO
each card's creative:

  - recorded sidecar text is used first (free);
  - otherwise the pixels are OCR-read RIGHT NOW (backfill) and the read is recorded
    so the next look is free.

Two ways a card is AUTO-BLOCKED (Blake's call), each named plainly:
  1. its rendered pixels carry a stat with no approved receipt (the number named);
  2. it HAS rendered pixels the gate could not read or verify (fail closed): an
     unreadable card is blocked, never passed through as "unverifiable".

A card with no renderable creative (a video, or no image) is exempt: there is
nothing to fabricate. `--dry-run` reports without flipping anything.

Nothing here publishes or fabricates.
"""

from .drafter import DraftStatus


def _creative_view(path):
    class _C:
        pass
    c = _C()
    c.path = path or ""
    c.client_note = ""
    return c


def scan(store=None, poster=None, auto_block=True, reader=None):
    """
    Returns a report dict:
      {
        "checked": int,
        "clean":   int,                      # verified clean OR nothing to verify
        "blocked": [{"draft_id","account","day","reason","kind","path"}],
                                             # kind is 'stat' or 'unverifiable'
      }
    Every card with a creative resolves to clean or BLOCKED; nothing is left in a
    passable 'unverifiable' state. When auto_block is False (dry run) offenders are
    listed but never flipped.
    """
    from . import pixel_gate, rotation, ops_alerts
    if store is None:
        from .store import PendingStore
        store = PendingStore()
    if poster is None:
        poster = ops_alerts._default_poster()

    approved = rotation._approved_claims()
    report = {"checked": 0, "clean": 0, "blocked": []}

    for draft in store.list_pending():
        path = draft.creative_path or ""
        if not path:
            continue
        report["checked"] += 1
        # require_verification=True: the scan is an explicit verification pass, so an
        # unreadable-with-pixels card fails closed regardless of the studio flag.
        ok, reason = pixel_gate.gate_creative(
            _creative_view(path), approved_claims=approved, reader=reader,
            require_verification=True)
        if ok:
            report["clean"] += 1
            continue
        kind = "unverifiable" if "could not verify" in reason else "stat"
        report["blocked"].append({
            "draft_id": draft.draft_id, "account": draft.account_key,
            "day": draft.day_key or "", "reason": reason, "kind": kind, "path": path})
        if auto_block:
            draft.status = DraftStatus.BLOCKED
            draft.blocked_reason = "Fabrication gate (pixels, retro scan): " + reason
            store.put(draft)
            ops_alerts.alert(
                f"fabrication scan AUTO-BLOCKED {draft.account_key} draft "
                f"{draft.draft_id} ({draft.day_key or 'no day'}): {reason}")
            try:
                if getattr(draft, "slack_ts", "") and getattr(draft, "slack_channel", ""):
                    poster.mark_expired(draft)  # retire the card in place (buttons gone)
            except Exception:
                pass
    return report


def format_report(report, dry_run=False):
    verb = "WOULD BLOCK" if dry_run else "BLOCKED"
    lines = ["FABRICATION SCAN (rendered pixels vs approved receipts, fail-closed)"]
    lines.append(f"  checked      : {report['checked']}")
    lines.append(f"  clean        : {report['clean']}")
    blocked = report["blocked"]
    stat = [e for e in blocked if e["kind"] == "stat"]
    unver = [e for e in blocked if e["kind"] == "unverifiable"]
    if blocked:
        lines.append(f"  {verb} ({len(blocked)})")
        for e in stat:
            lines.append(f"    [stat]         {e['account']} {e['draft_id']} "
                         f"({e['day'] or 'no day'})  {e['reason']}")
        for e in unver:
            lines.append(f"    [unverifiable] {e['account']} {e['draft_id']} "
                         f"({e['day'] or 'no day'})  {e['reason']}")
    else:
        lines.append(f"  {verb} (0): none")
    lines.append("  UNVERIFIABLE (passthrough): 0  (fail-closed: unreadable cards "
                 "with rendered pixels are BLOCKED, never passed)")
    return "\n".join(lines)
