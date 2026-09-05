"""
testdata.py — which rows in the live bus are our own test probes, not client work.

Blake, 2026-09-05, looking at eight cards in #fixer that read exactly like unhandled client
tickets: "these are TEST ARTIFACTS from an earlier arming-verification session ... sitting in
the live #fixer channel looking exactly like real unhandled tickets."

They were. All eight came from the Phase 4 arming run: four "happy path" probes and four
"escalation path" probes, sent through the real bots into the real channel, and they are
indistinguishable from client tickets in every report, count and card unless something knows
how to tell them apart. This module is that something, and it is deliberately ONE predicate
in ONE file so a report, a metric and a card can never disagree about what counts.

THE RULE, and why each clause is safe:

  * raw_text carrying a bracketed probe tag ("[phase4-audit ...]"). Our own harnesses write
    it; no client message has ever contained it. This is the strongest signal and it is the
    one Blake verified by hand against the live rows.
  * the synthetic sender id U0000000000, which is not a real Slack id at all (Slack ids are
    assigned; this one is a literal placeholder our escalation-path probe uses to force an
    unresolvable identity).
  * a reporter address in our own test namespace: blake+zztest@ and the
    nonexistent-unresolvable-test@ address the escalation probe uses.

What this must NEVER match is a real client, so every clause is an exact structural marker
we mint ourselves, never a heuristic about content, a domain, or a time window. A real gym
owner cannot accidentally satisfy any of them: they would have to type our probe tag, or
write from an address inside our own test namespace.

Being test data is NOT a reason to hide a row from a human who goes looking. It is a reason
to keep it out of counts, dashboards and "what is waiting on you" surfaces, which is what
Blake actually asked for: "excluded from any future report/metric and never resurface".
"""
import re

# Any bracketed probe tag our own harnesses stamp at the FRONT of a synthetic message.
#
# m5 (audit): this used to match anywhere in the text, and bus.find_new_tickets drops what it
# matches -- so a client who pasted a log line containing "[smoke-test ...]" would have had
# their ticket silently dropped from the only intake poll the portal bridge has. Hiding a
# real client's ticket is the one direction that must never happen, so the tag is anchored to
# the start (after an optional leading @mention, which is how the Wrangler probes arrived).
PROBE_TAG_RE = re.compile(
    r"^\s*(?:<@[A-Z0-9]+>\s*)*\[(?:phase\d+-audit|arming-probe|smoke-test)[^\]]*\]",
    re.IGNORECASE)

# The literal placeholder id the escalation-path probe sends as; never a real Slack user.
SYNTHETIC_SLACK_IDS = frozenset({"U0000000000"})

# Our own test-address namespace.
TEST_REPORTER_PREFIXES = ("blake+zztest@", "nonexistent-unresolvable-test@")


def is_test_text(text):
    return bool(PROBE_TAG_RE.search(str(text or "")))


def is_test_reporter(reporter):
    r = str(reporter or "").strip().lower()
    if not r:
        return False
    if r in {s.lower() for s in SYNTHETIC_SLACK_IDS}:
        return True
    return any(r.startswith(p) for p in TEST_REPORTER_PREFIXES)


def is_test_ticket(ticket):
    """True when this support_tickets row is one of our own probes.

    Accepts a dict shaped like a bus row. Never raises: an unreadable row is treated as
    REAL, because wrongly hiding a client's ticket is far worse than counting one of ours."""
    try:
        t = ticket or {}
        if bool(t.get("is_test")):
            return True          # the durable column, once the portal migration lands
        if is_test_text(t.get("raw_text")):
            return True
        if str(t.get("slack_user_id") or "") in SYNTHETIC_SLACK_IDS:
            return True
        return is_test_reporter(t.get("reporter"))
    except Exception:  # noqa: BLE001
        return False


def exclude_test(tickets):
    """The list, minus our own probes. The one call every report and metric should use."""
    return [t for t in (tickets or []) if not is_test_ticket(t)]
