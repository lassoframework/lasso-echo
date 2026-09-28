"""Shared owner-calendar visibility rules for the portal read surfaces."""

from . import config


CLIENT_HIDDEN_STATUSES = ("coach_review", "denied", "killed", "deleted")


def _status_value(row):
    if isinstance(row, dict):
        status = row.get("status")
    else:
        try:
            status = row["status"]
        except (KeyError, TypeError, IndexError):
            status = getattr(row, "status", "")
    if hasattr(status, "value"):
        status = status.value
    return str(status or "").strip().lower()


def client_visible(rows):
    """Return rows that may appear on a gym owner's calendar.

    The escape hatch restores the historical rejected-row payload while keeping
    coach-screened rows private until a coach releases them.
    """
    hidden = CLIENT_HIDDEN_STATUSES
    if config.portal_show_rejected():
        hidden = ("coach_review",)
    return [row for row in (rows or []) if _status_value(row) not in hidden]
