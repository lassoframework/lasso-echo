"""
Retroactive pixel fabrication scan.

Walk every PENDING / planned card in the queue, resolve the text rendered INTO
each card's creative (recorded sidecar text first; a one-time OCR read otherwise,
recorded back so the next look is free), and check it against the approved
receipts. A card whose pixels carry a stat with no approved source is AUTO-BLOCKED
(Blake's call): its record flips to BLOCKED naming the number, its Slack card is
retired in place, and it drops out of the approvable queue. `--dry-run` reports
without blocking so the queue can be reviewed first.

Nothing here publishes or fabricates; it only pulls unverifiable stat cards out of
the queue and names the number so Blake can clear them in one pass.
"""

from .drafter import DraftStatus


def _creative_view(draft):
    """A minimal creative-like object (path + note) for pixel_gate.gate_creative."""
    class _C:
        path = draft.creative_path or ""
        client_note = ""
    return _C()


def scan(store=None, poster=None, auto_block=True, reader=None):
    """
    Returns a report dict:
      {
        "checked": int,
        "blocked": [{"draft_id","account","day","numbers","path"}],
        "clean":   int,
        "unverifiable": [{"draft_id","account","day","path"}],  # no record, no reader
      }
    When auto_block is False (dry run) the offending cards are only listed, never
    flipped.
    """
    from . import pixel_gate, rotation, ops_alerts
    if store is None:
        from .store import PendingStore
        store = PendingStore()
    if poster is None:
        poster = ops_alerts._default_poster()

    approved = rotation._approved_claims()
    report = {"checked": 0, "blocked": [], "clean": 0, "unverifiable": []}

    for draft in store.list_pending():
        path = draft.creative_path or ""
        if not path:
            continue
        report["checked"] += 1
        rendered, src = pixel_gate.resolve_rendered_text(path, reader=reader)
        if src == "none":
            report["unverifiable"].append({
                "draft_id": draft.draft_id, "account": draft.account_key,
                "day": draft.day_key or "", "path": path})
            continue
        nums = pixel_gate.offending_numbers(rendered, approved)
        if not nums:
            report["clean"] += 1
            continue
        report["blocked"].append({
            "draft_id": draft.draft_id, "account": draft.account_key,
            "day": draft.day_key or "", "numbers": nums, "path": path})
        if auto_block:
            draft.status = DraftStatus.BLOCKED
            draft.blocked_reason = ("Fabrication gate (pixels, retro scan): rendered "
                                    "stat with no approved receipt: " + ", ".join(nums))
            store.put(draft)
            ops_alerts.alert(
                f"fabrication scan AUTO-BLOCKED {draft.account_key} draft "
                f"{draft.draft_id} ({draft.day_key or 'no day'}): rendered stat with "
                f"no approved receipt: {', '.join(nums)}.")
            try:
                if getattr(draft, "slack_ts", "") and getattr(draft, "slack_channel", ""):
                    poster.mark_expired(draft)  # retire the card in place (buttons gone)
            except Exception:
                pass
    return report


def format_report(report, dry_run=False):
    verb = "WOULD BLOCK" if dry_run else "BLOCKED"
    lines = ["FABRICATION SCAN (rendered pixels vs approved receipts)"]
    lines.append(f"  checked      : {report['checked']}")
    lines.append(f"  clean        : {report['clean']}")
    if report["blocked"]:
        lines.append(f"  {verb} ({len(report['blocked'])})")
        for e in report["blocked"]:
            lines.append(f"    {e['account']} {e['draft_id']} ({e['day'] or 'no day'})"
                         f"  numbers: {', '.join(e['numbers'])}")
    else:
        lines.append(f"  {verb} (0): none")
    if report["unverifiable"]:
        lines.append(f"  UNVERIFIABLE ({len(report['unverifiable'])}) "
                     "(no recorded text and no OCR reader; arm the studio to read pixels)")
        for e in report["unverifiable"]:
            lines.append(f"    {e['account']} {e['draft_id']} ({e['day'] or 'no day'})")
    return "\n".join(lines)
