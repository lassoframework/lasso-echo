"""
Library audit: scan account creative libraries for MISSING and THIN creatives.

MISSING = file or directory does not exist on disk, or a pending draft
          references a creative path that is no longer in the library.
THIN    = creative exists but below the minimum quality bar:
            - single-slide carousel (cannot post as carousel)
            - image under MIN_IMAGE_BYTES (broken export or stub file)
            - video under MIN_VIDEO_BYTES (corrupt or zero-byte file)

Usage (CLI):
  python -m agent library-audit --account lasso_ig
  python -m agent library-audit --all
"""

import os

from .library import IMAGE_EXTS, list_creatives

MIN_IMAGE_BYTES = 10_000
MIN_VIDEO_BYTES = 100_000


def check_creative(creative):
    """
    Returns None when the creative is healthy; a non-empty string describing
    the issue when it is MISSING or THIN. Called by the audit CLI and by the
    run-daily preflight warning before drafting.
    """
    path = creative.path
    if creative.media_type == "carousel":
        if not os.path.isdir(path):
            return "MISSING (directory not found: {})".format(path)
        slides = creative.slides or []
        if len(slides) < 2:
            return "THIN (carousel has {} slide(s); need >= 2)".format(len(slides))
        for s in slides:
            if not os.path.isfile(s):
                return "MISSING (slide not found: {})".format(os.path.basename(s))
        return None
    if not os.path.isfile(path):
        return "MISSING (file not found: {})".format(path)
    size = os.path.getsize(path)
    min_b = MIN_VIDEO_BYTES if creative.media_type == "video" else MIN_IMAGE_BYTES
    if size < min_b:
        return "THIN ({} bytes < {} minimum)".format(size, min_b)
    return None


def _scan_stub_carousels(lib_path):
    """
    Scan library subdirectories for carousel stubs that list_creatives() silently
    drops because they have fewer than 2 image slides. Returns a list of
    {"stem": str, "media_type": "carousel", "reason": str} entries.
    """
    stubs = []
    if not os.path.isdir(lib_path):
        return stubs
    for name in sorted(os.listdir(lib_path)):
        if name.startswith("."):
            continue  # skip hidden dirs (.DS_Store, .claude-flow, etc.)
        full = os.path.join(lib_path, name)
        if not os.path.isdir(full):
            continue
        slides = [
            f for f in os.listdir(full)
            if os.path.isfile(os.path.join(full, f))
            and os.path.splitext(f)[1].lower() in IMAGE_EXTS
        ]
        if len(slides) >= 2:
            continue  # list_creatives() picks this up; check_creative handles it
        reason = (
            "THIN (carousel stub: {} slide(s) — need >= 2 to post)".format(len(slides))
        )
        stubs.append({"stem": name, "media_type": "carousel", "reason": reason})
    return stubs


def audit_account(account_key, lib_path):
    """
    Audit one account's creative library.
    Returns:
      {
        "account":  str,
        "lib_path": str,
        "total":    int,
        "missing":  [{"stem": str, "media_type": str, "reason": str}],
        "thin":     [{"stem": str, "media_type": str, "reason": str}],
      }
    """
    creatives = list_creatives(lib_path)
    missing, thin = [], []
    for c in creatives:
        issue = check_creative(c)
        if issue is None:
            continue
        entry = {"stem": c.stem, "media_type": c.media_type, "reason": issue}
        if issue.startswith("MISSING"):
            missing.append(entry)
        else:
            thin.append(entry)

    # Subdirectory stubs list_creatives() silently skips (< 2 slides).
    for stub in _scan_stub_carousels(lib_path):
        thin.append(stub)

    # Check pending drafts for creative paths that are absent from the library.
    try:
        from .store import PendingStore
        store = PendingStore()
        list_pending = getattr(store, "list_pending", None)
        if list_pending:
            known_paths = {c.path for c in creatives}
            for d in list_pending():
                if d.account_key != account_key:
                    continue
                cp = (d.creative_path or "").strip()
                if not cp or cp in known_paths:
                    continue
                if not os.path.exists(cp):
                    stem = os.path.splitext(os.path.basename(cp.rstrip("/\\")))[0]
                    missing.append({
                        "stem": stem,
                        "media_type": "unknown",
                        "reason": (
                            "MISSING (pending draft {} on {} references absent path)"
                            .format(d.draft_id, d.day_key or "?")
                        ),
                    })
    except Exception:
        pass

    return {
        "account": account_key,
        "lib_path": lib_path,
        "total": len(creatives),
        "missing": missing,
        "thin": thin,
    }


def audit_all():
    """Audit every active account's library. Returns a list of audit dicts."""
    from .accounts import active_accounts
    from . import config
    results = []
    for account in active_accounts():
        lib = account.library_prefix or config.LIBRARY_PATH
        results.append(audit_account(account.key, lib))
    return results


def format_result(r):
    """Render one audit result as a human-readable string."""
    lines = []
    label = "LIBRARY AUDIT -- {}  ({})".format(r["account"], r["lib_path"])
    lines.append(label)
    lines.append("  creatives found: {}".format(r["total"]))
    if r["missing"]:
        lines.append("  MISSING ({})".format(len(r["missing"])))
        for e in r["missing"]:
            lines.append("    {} [{}]  {}".format(e["stem"], e["media_type"], e["reason"]))
    else:
        lines.append("  MISSING (0): none")
    if r["thin"]:
        lines.append("  THIN ({})".format(len(r["thin"])))
        for e in r["thin"]:
            lines.append("    {} [{}]  {}".format(e["stem"], e["media_type"], e["reason"]))
    else:
        lines.append("  THIN (0): none")
    if not r["missing"] and not r["thin"]:
        lines.append("  Clean.")
    return "\n".join(lines)
