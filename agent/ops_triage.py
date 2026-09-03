"""
ops_triage.py — classify one ops-alert line as NOISE (self-heal / informational, safe to
suppress from a human) or NEEDS_TRIAGE (send it somewhere a human, or Claude Code, looks
at it).

WHY THIS EXISTS (Blake, 2026-09-02): ops_alerts.py has 168 call sites, all flat, one
channel, no severity. A genuine break and "recycled 4 expired rows into open days, no
action needed" post identically. Blake reads the whole flood every morning to find the
one or two lines that matter. This module is the filter: default to NEEDS_TRIAGE (fail
toward MORE eyes, never fewer) and only classify NOISE when the text matches an explicit,
narrow, tested pattern. An alert this module has never seen the shape of is NEEDS_TRIAGE
by construction — a new alert call site needs no update here to stay safe.

This module does NOT decide what happens to a NEEDS_TRIAGE alert. That is the job of the
consumer (scout-listener's ops-triage relay): fix it in code and ship, or ESCALATE with a
short human-readable line. Keeping that judgment out of this module is deliberate — this
is a deterministic, unit-tested classifier, not a place to hide a model call.

Every pattern below is backed by a real alert line seen in production 2026-09-02 (see
tests/test_ops_triage.py, which pins the exact text). When a genuinely new alert shape
turns out to always be noise, add ONE narrow pattern here with a comment naming the real
line that justified it — never widen an existing pattern to catch more.
"""
import re

NOISE = "noise"
NEEDS_TRIAGE = "needs_triage"

# ---- SYSTEMIC failures: one incident, not one incident per gym -----------------------
#
# 2026-09-02, the storm this exists to stop: Supabase's REST layer wedged, so EVERY gym's
# calendar became unreadable at once. Echo alerted per gym -- district_h, topfuel,
# piercefitness, theboltonclub, crossfitlocal, hillcountry, train7164ae502 -- each alert
# cross-posted an OPS-FIX REQUEST, and each request spawned a headless Claude Code session
# that ran LIVE DATABASE DIAGNOSTICS against the very database that was already face down.
# Seven sessions, accelerating (7:20, 7:21, 7:24, 7:24, 7:25, 7:26, 7:27), all diagnosing
# one shared cause.
#
# The lesson is not "alert less". It is that a SHARED-DEPENDENCY failure is ONE incident,
# and fanning it out per tenant turns a bad minute into a self-amplifying storm that
# competes with the recovery for the resource that is already exhausted.
#
# These alerts are still NEEDS_TRIAGE -- a human must absolutely hear about them -- but
# ops_alerts collapses them to one cross-post per window instead of one per gym.
# Phrases that NAME the shared dependency outright -- systemic on their own.
_SYSTEMIC_MARKERS = (
    "calendar_unreadable",
    "the shared calendar could not be read",
    "supabase creds/network",
)

# Transport failures. These are systemic ONLY when the text also names a shared host
# below. The exception CLASS says nothing about blast radius: the identical
# "ReadTimeout: HTTPSConnectionPool(...)" is a fleet-wide event when the host is
# Supabase and ONE GYM'S problem when the host is graph.facebook.com. Keying on the
# class alone let a single gym's Meta timeout claim the systemic slot and then
# suppress the escalation for a genuine database outage for the next 30 minutes
# (found by the 2026-09-02 verification audit, scenario G).
_TRANSPORT_MARKERS = (
    "readtimeout",
    "read timed out",
    "connection pool",
    "httpsconnectionpool",
    "connection terminated",
    "max retries exceeded",
)

# The dependencies EVERY gym shares. A transport failure naming one of these is one
# incident for the whole fleet; naming anything else it is that gym's own incident.
_SHARED_HOSTS = (
    "supabase.co",
    "supabase.in",
)


def is_systemic(text) -> bool:
    """True when an alert describes a SHARED dependency failing (the database, the network),
    not one gym's own content problem. Such an alert is real and must be surfaced, but it
    fires once per gym for a single underlying cause, so the cross-post is collapsed.

    Fails toward FANNING OUT: a transport error with no recognisable host is treated as
    per-gym, so an unfamiliar shape gets more eyes rather than being collapsed away."""
    t = _ALERT_PREFIX_RE.sub("", str(text or "")).lower()
    if any(m in t for m in _SYSTEMIC_MARKERS):
        return True
    if any(m in t for m in _TRANSPORT_MARKERS):
        return any(h in t for h in _SHARED_HOSTS)
    return False

# Explicit self-heal / no-action phrasing. Alert authors already write this convention
# deliberately (see ops_alerts.py's own docstring examples) -- trust it, it is the single
# strongest signal in the whole corpus. Case-insensitive.
_EXPLICIT_NO_ACTION_RE = re.compile(
    r"no action needed|not blocked;|approvals preserved"
    r"|slot refills on the next plan pass",
    re.IGNORECASE)

# Pure summary / log lines: not alerts about a problem at all, just a count.
_INFORMATIONAL_PREFIXES = (
    "Calendar auto-published",
    "podcast index:",
    "New team media synced",
)

# "grade sweep: N gyms, ... held (...)" is a roll-up line, not a per-gym signal — the
# per-gym DROPPED / held-at-F alerts (below) are what carry the actual defects.
_GRADE_SWEEP_RE = re.compile(r"^grade sweep: \d+ gyms")

# "calendar grade: <gym> forward book held at N (GRADE) after self-fix." — noise only when
# the grade is A/B/C (self-fix ran, nothing new broken, steady state). An F or D held-state
# means the nightly repair cannot clear whatever is wrong; that stays NEEDS_TRIAGE.
_GRADE_HELD_RE = re.compile(
    r"calendar grade:.*forward book held at \d+ \(([A-F])\) after self-fix", re.IGNORECASE)
_HELD_NOISE_GRADES = {"A", "B", "C"}

# A grade REGRESSION ("DROPPED: ... went N -> M") is explicitly NOT noise, even though the
# words look similar to the held-at line above -- it means new defects are being built in
# faster than the nightly repair clears them. Listed for clarity; the default (no match ->
# NEEDS_TRIAGE) already covers it, this just documents the deliberate non-match.
_GRADE_DROPPED_RE = re.compile(r"calendar grade DROPPED", re.IGNORECASE)


_ALERT_PREFIX_RE = re.compile(r"^ECHO ALERT:\s*")


def classify(text):
    """NOISE or NEEDS_TRIAGE for one alert line. Never raises; an empty/odd input is
    NEEDS_TRIAGE (the fail-safe default), never a crash.

    Tolerant of the leading "ECHO ALERT: " ops_alerts.alert() adds before posting to
    Slack: the real consumer here is a live Slack message, which always carries it for
    anything posted through alert() (and never for the few paths that post directly),
    so every pattern below is checked against the text with that one optional prefix
    stripped, not the two shapes separately.
    """
    t = _ALERT_PREFIX_RE.sub("", str(text or ""))

    if _GRADE_DROPPED_RE.search(t):
        return NEEDS_TRIAGE  # regression signal, always surfaced (see docstring above)

    m = _GRADE_HELD_RE.search(t)
    if m:
        return NOISE if m.group(1).upper() in _HELD_NOISE_GRADES else NEEDS_TRIAGE

    if _GRADE_SWEEP_RE.search(t):
        return NOISE

    if any(t.startswith(p) for p in _INFORMATIONAL_PREFIXES):
        return NOISE

    if _EXPLICIT_NO_ACTION_RE.search(t):
        return NOISE

    return NEEDS_TRIAGE


def main(argv=None):
    """CLI entry point: classify a single alert line.

    Reads the line from argv[0] when given, else stdin (whole input, treated as one
    message — the caller is responsible for splitting a multi-line alert body itself if it
    wants per-line classification). Prints exactly "noise" or "needs_triage" and nothing
    else, so a shell caller (scout-listener) can consume it directly.
    """
    import sys
    args = sys.argv[1:] if argv is None else argv
    text = args[0] if args else sys.stdin.read()
    print(classify(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
