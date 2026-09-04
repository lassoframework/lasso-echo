"""
alert_repeat.py — the REPEAT gate: an unchanged NEEDS_TRIAGE alert stops re-announcing
itself every single morning until its window lapses or its underlying state changes.

WHY THIS EXISTS (Blake, 2026-09-04, looking at #echosupport): "why do i keep getting
this?" On 2026-09-04 the fleet raised 45 ops alerts and every one of them was DISTINCT --
there was no duplicate spam to dedupe within the day. The flood was day-over-day: the
same three gyms with zero connected platforms, the same two split account keys, announced
again at 08:03 exactly as they had been the morning before, and the morning before that,
because nothing in ops_alerts ever asked "have I already said this?".

This is a DIFFERENT question from the one the noise gate answers, and the two must not be
confused:

  * ops_triage.classify()  -> "does a human EVER need to see this shape?"  (NOISE is
    dropped forever; a gym with zero connected platforms is emphatically not noise.)
  * this module            -> "has a human already been told this EXACT thing recently,
    and has nothing changed since?"

Both alerts here are real work. The failure is not that Echo raised them; it is that a
gym waiting on its owner to click a connect link cannot be fixed by saying so again
tomorrow, and the repetition is what buries the ONE line that is genuinely new.

THREE INVARIANTS, each of which is a test:

  1. The FIRST occurrence always fires. This gate can only ever suppress a repeat.
  2. A STATE CHANGE always fires, immediately, without waiting out the window. That is
     what makes suppression safe, and it is why normalisation below is deliberately
     narrow: only clock/calendar tokens are erased. Every number, id, grade, count and
     gym name stays in the fingerprint, so "forward book went 82 -> 75" and
     "82 -> 70" are two different alerts and both are heard.
  3. The audit row is written by ops_alerts BEFORE any gate, so a suppressed alert is
     still on the permanent record and still queryable -- it just does not wake anyone.

FAILS OPEN, everywhere. A store that cannot be read, a clock that cannot be parsed, a
stamp that cannot be written: the alert fires. A dedupe that silently swallows a real
break is far worse than one duplicate line in Slack.
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone

from . import config

# Clock and calendar tokens ONLY. These move on their own every day while the underlying
# condition is frozen -- "no daily draft heartbeat for lasso_ig by 10:00 ET on
# 2026-09-04" is the same unfixed condition as the 09-03 line, and keying on the raw text
# would make this gate a no-op for exactly the alerts that repeat most.
#
# Nothing else is normalised. It is tempting to also collapse digits so that "only 25
# usable photo(s)" and "only 24 usable photo(s)" share a slot; that is precisely the
# change that would hide a state change, so it is not made. Fail toward MORE eyes.
_VOLATILE = (
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "<date>"),          # 2026-09-04
    (re.compile(r"\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?"), "<time>"),  # 10:00, 12:30:38.86
)

_KEY_PREFIX = "alert_repeat_"


def fingerprint(message):
    """A stable identity for 'this exact condition', with the calendar erased.

    Two alerts share a fingerprint when they say the same thing about the same subject on
    different days. They do NOT share one when any substantive token differs -- a count, a
    grade, a row id, a gym name, a reason code."""
    text = (message or "").strip()
    for pattern, placeholder in _VOLATILE:
        text = pattern.sub(placeholder, text)
    text = " ".join(text.split()).lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _window(now, stamped_at):
    hours = config.alert_repeat_window_hours()
    return stamped_at + timedelta(hours=hours) > now


def should_fire(message, now=None, db=None):
    """True when this alert should reach Slack; False only when it is a genuine,
    unchanged repeat inside the window. Claims the slot as a side effect when it
    returns True, so two callers in the same second cannot both fire.

    Never called for a `force` alert or for a systemic one -- see ops_alerts.alert."""
    if not config.alert_repeat_gate_enabled():
        return True
    now = now or datetime.now(timezone.utc)
    try:
        if db is None:
            from . import db as db_mod
            db = db_mod
        key = _KEY_PREFIX + fingerprint(message)
        raw = db.kv_get(key, "")
        if raw:
            stamped = datetime.fromisoformat(raw)
            if not stamped.tzinfo:
                stamped = stamped.replace(tzinfo=timezone.utc)
            if _window(now, stamped):
                return False
        db.kv_set(key, now.isoformat())
        return True
    except Exception as e:  # noqa: BLE001 - a repeat gate must never eat a real alert
        print(f"[alert-repeat] gate unavailable ({type(e).__name__}); firing the alert")
        return True
