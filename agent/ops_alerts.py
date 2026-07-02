"""
Ops alerts: loud, short, actionable failure lines for the Slack channel.

OFF BY DEFAULT (`config.ops_alerts_enabled()`). With the flag OFF, alert() is a
no-op and every failure branch keeps today's behavior (logged only). ON, each
currently-silent fallback in the draft pipeline posts ONE plain line prefixed
"ECHO ALERT:" so a failure is never invisible:

  - media hosting failed (exception class + message, never credentials)
  - creative generation returned empty
  - content plan blocked
  - publish attempt failed
  - store write failed

NO SECRETS, guaranteed twice over: callers only pass exception class + message
(never tokens), and scrub() additionally redacts the VALUE of any secret-looking
env var (…TOKEN / …SECRET / …KEY / …PASSWORD) before the text leaves this module.
Alerting never breaks the pipeline: a failed Slack post is itself only logged.
"""

import os

from . import config

_SECRET_NAME_HINTS = ("TOKEN", "SECRET", "KEY", "PASSWORD")
# Values shorter than this are never treated as secrets (flag values like "true"
# or "1" living under a …KEY name must not be redacted out of ordinary words).
_MIN_SECRET_LEN = 6


def _secret_values():
    """Values of every secret-looking env var, longest first so partial overlaps
    (one secret containing another) still redact cleanly."""
    vals = []
    for name, value in os.environ.items():
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        upper = name.upper()
        if any(hint in upper for hint in _SECRET_NAME_HINTS):
            vals.append(value)
    return sorted(set(vals), key=len, reverse=True)


def scrub(text):
    """Redact any secret env value that leaked into `text` (e.g. inside a
    third-party exception message)."""
    out = str(text)
    for value in _secret_values():
        if value in out:
            out = out.replace(value, "[REDACTED]")
    return out


def _default_poster():
    """Injection seam for tests; the real SlackPoster in production."""
    from .slack_surface import SlackPoster
    return SlackPoster()


def alert(message, poster=None, force=False):
    """
    Post one ops alert line to the Slack channel. Returns the Slack response, or
    None when dormant. Flag OFF -> None, no client touched (unless `force`, used
    by callers that carry their OWN default-OFF flag, e.g. the token watchdog).
    The message is scrubbed of secret env values either way.
    """
    # decision-trail: every alert (fired or dormant) lands in the audit table
    try:
        from datetime import datetime, timezone
        from . import db as _db
        _db.audit("ops_alert", "alert", scrub(message),
                  day=datetime.now(timezone.utc).date().isoformat())
    except Exception:
        pass
    if not force and not config.ops_alerts_enabled():
        return None
    text = "ECHO ALERT: " + scrub(message)
    poster = poster or _default_poster()
    try:
        return poster.post_notice(text)
    except Exception as e:
        # An alert must never take the pipeline down with it.
        print(f"[ops-alerts] failed to post alert: {type(e).__name__}: {scrub(e)}")
        return None
