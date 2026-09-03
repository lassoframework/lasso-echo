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

NO SECRETS, guaranteed three times over:
  1. callers only pass exception class + message (never tokens);
  2. scrub() redacts the VALUE of any secret-looking env var (…TOKEN / …SECRET /
     …KEY / …PASSWORD) before the text leaves this module;
  3. scrub() ALSO redacts by SHAPE (_PATTERNS below), because a secret can arrive
     from OUTSIDE our env entirely — a third-party response body echoing an
     Authorization header, a provider error quoting the key it rejected, a signed
     URL in a traceback. Env-value matching cannot see those: the value was never
     in os.environ. Shape matching covers bearer/basic credentials, provider-
     prefixed keys (sk-…, sk_live_…, xoxb-…, ghp_…, AKIA…, AIza…, EAA…, apify_api_…),
     JWTs, `token=`/`api_key=`/`password=` style fields, and long hex / base64
     blobs (signatures, session keys).
Redaction is deliberately eager: an over-redacted alert is a nuisance, a leaked
alert is an incident. Alerting never breaks the pipeline: a failed Slack post is
itself only logged.
"""

import os
import re

from . import config

_SECRET_NAME_HINTS = ("TOKEN", "SECRET", "KEY", "PASSWORD")
# Values shorter than this are never treated as secrets (flag values like "true"
# or "1" living under a …KEY name must not be redacted out of ordinary words).
_MIN_SECRET_LEN = 6

REDACTED = "[REDACTED]"

# ---- shape-based redaction ---------------------------------------------------
#
# ORDER MATTERS and is asserted by the tests:
#   1. bearer/basic FIRST — otherwise the named-field rule below matches
#      "Authorization: Bearer <token>" and redacts only the word "Bearer",
#      shipping the token itself.
#   2. provider-prefixed keys and JWTs next (most specific shapes).
#   3. named fields (token=…, api_key: …) — the catch-all for a value whose own
#      shape says nothing.
#   4. long hex / base64 blobs LAST (the broadest, most false-positive-prone).

# Only real HTTP auth SCHEMES. "Token" is deliberately NOT here: it is an
# ordinary English word in our own alert text ("token expiring") and would eat
# the next word of every such line. GitHub's `Authorization: token ghp_…` is
# still covered — by the ghp_ prefix rule below.
_BEARER_RE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{8,}", re.I)

_PREFIXED_RE = re.compile(
    r"("
    r"sk-[A-Za-z0-9_\-]{16,}"                 # OpenAI-style
    r"|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"   # Stripe-style
    r"|xox[baprse]-[A-Za-z0-9\-]{8,}"         # Slack bot/user/app tokens
    r"|xapp-[A-Za-z0-9\-]{8,}"                # Slack app-level
    r"|gh[pousr]_[A-Za-z0-9]{16,}"            # GitHub
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{12,}"            # AWS access key id
    r"|AIza[0-9A-Za-z_\-]{20,}"               # Google API key
    r"|ya29\.[0-9A-Za-z_\-]{20,}"             # Google OAuth
    r"|EAA[0-9A-Za-z]{40,}"                   # Meta / Facebook graph token
    r"|apify_api_[A-Za-z0-9]{16,}"
    r"|glpat-[A-Za-z0-9_\-]{16,}"
    r"|shpat_[A-Za-z0-9]{16,}"
    r")")

_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}")

# key=value / "key": "value" / key: value, where the KEY names a credential.
_NAMED_FIELD_RE = re.compile(
    r"(?i)\b(api[_\-]?key|apikey|access[_\-]?token|refresh[_\-]?token|"
    r"auth[_\-]?token|id[_\-]?token|bearer[_\-]?token|session[_\-]?token|"
    r"client[_\-]?secret|service[_\-]?key|service[_\-]?role|private[_\-]?key|"
    r"authorization|password|passwd|secret|signature|token)"
    r"(\"?\s*[:=]\s*\"?)"
    r"(?!\[REDACTED\])"
    r"([^\s\"',;&)}\]]{6,})")

# A long unbroken hex run: signatures, session keys, HMACs. A UUID never matches
# (its dashes break the run at 8 chars), so gym ids and row ids survive intact.
# _hex_sub additionally requires real variety (>= 8 distinct characters), so a
# run of padding ('aaaa…') or a repeated marker is not mistaken for entropy; a
# random 32-hex string carries ~16 distinct characters, so no real secret is
# missed by that floor.
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_HEX_MIN_DISTINCT = 8


def _hex_sub(match):
    s = match.group(0)
    return REDACTED if len(set(s.lower())) >= _HEX_MIN_DISTINCT else s

# A long base64-ish blob. Only redacted when it actually LOOKS like entropy
# (upper + lower + digit all present), so ordinary long words, file paths, and
# slugs are left alone.
_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}")


def _b64_sub(match):
    s = match.group(0)
    if not (any(c.islower() for c in s) and any(c.isupper() for c in s)
            and any(c.isdigit() for c in s)):
        return s          # a long ordinary word, not entropy
    return REDACTED


def _redact_shapes(text):
    """Redact anything SHAPED like a credential, wherever it came from."""
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    out = _PREFIXED_RE.sub(REDACTED, out)
    out = _JWT_RE.sub(REDACTED, out)
    out = _NAMED_FIELD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _HEX_RE.sub(_hex_sub, out)
    out = _B64_RE.sub(_b64_sub, out)
    return out


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
    """Redact secrets from `text` before it leaves this module.

    TWO passes, both always run:
      1. every secret-looking ENV VALUE that leaked into the text (a token this
         process holds, echoed back by a third party);
      2. every credential SHAPE (_redact_shapes) — the case env matching cannot
         cover, because the secret is not ours: a provider quoting the key it
         rejected, an upstream response body carrying its own Authorization
         header, a signed URL inside a traceback.
    """
    out = str(text)
    for value in _secret_values():
        if value in out:
            out = out.replace(value, REDACTED)
    return _redact_shapes(out)


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
        result = poster.post_notice(text)
    except Exception as e:
        # An alert must never take the pipeline down with it.
        print(f"[ops-alerts] failed to post alert: {type(e).__name__}: {scrub(e)}")
        return None
    _maybe_cross_post_ops_fix(text, poster)
    return result


# One systemic escalation per window. 30 minutes is long enough that a multi-gym outage
# escalates ONCE, short enough that a genuinely new incident half an hour later is not
# swallowed. Deliberately stamped in the same kv the rest of this repo's alert-once
# watches use, so it survives a restart mid-incident (a worker that restarts during an
# outage must not re-fan-out the whole fleet).
SYSTEMIC_ESCALATION_WINDOW = 30 * 60
_SYSTEMIC_KEY = "ops_fix_systemic_escalated_at"


def _claim_systemic_slot(db=None, now=None) -> bool:
    """True when THIS systemic alert is the one that gets escalated this window (and the
    slot is claimed). False when another already escalated recently.

    Fails OPEN: if the stamp cannot be read or written, the alert escalates. A dedupe that
    silently swallows an outage escalation is far worse than one duplicate request."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    try:
        if db is None:
            from . import db as db
        raw = db.kv_get(_SYSTEMIC_KEY) or ""
        if raw:
            last = datetime.fromisoformat(raw)
            if not last.tzinfo:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < SYSTEMIC_ESCALATION_WINDOW:
                return False
        db.kv_set(_SYSTEMIC_KEY, now.isoformat())
        return True
    except Exception as e:  # noqa: BLE001 - never swallow an escalation over a kv problem
        print(f"[ops-alerts] systemic dedupe unavailable ({type(e).__name__}); escalating")
        return True


def _maybe_cross_post_ops_fix(alert_text, poster):
    """Cross-post a NEEDS_TRIAGE alert into #echosupport as an OPS-FIX REQUEST
    (Blake, 2026-09-02: "it should go to echo support that is already wired" --
    #echosupport already gets live Slack events and already has a proven relay to
    headless Claude Code; #echoclaude, where every alert still posts unchanged
    above, does not).

    Best-effort and silent-safe by construction: OFF unless
    config.ops_fix_triage_enabled() is armed; a classification failure or a Slack
    failure here is logged and swallowed, never raised into the caller -- the
    primary alert already posted and must not be affected by this side effect.
    """
    if not config.ops_fix_triage_enabled():
        return
    try:
        channel = config.support_channel_id()
        if not channel:
            return
        from . import ops_triage
        if ops_triage.classify(alert_text) != ops_triage.NEEDS_TRIAGE:
            return
        # SYSTEMIC COLLAPSE (2026-09-02): a shared-dependency failure fires once per gym
        # for ONE underlying cause. On the night Supabase's REST layer wedged, seven gyms
        # each raised 'calendar_unreadable', each cross-posted, and each spawned a fix
        # session that ran live database diagnostics against the database that was already
        # down -- competing with the recovery for the exact resource that was exhausted.
        # Collapse to ONE cross-post per window. The per-gym alerts still land in
        # #echoclaude in full; only the fix-request fan-out is deduped.
        if ops_triage.is_systemic(alert_text) and not _claim_systemic_slot():
            print("[ops-alerts] systemic alert already escalated this window; "
                  "not fanning out another ops-fix request")
            return
        poster._chat_post(text=f"OPS-FIX REQUEST: {alert_text}", blocks=None,
                          channel=channel)
    except Exception as e:  # noqa: BLE001 - a side effect must never affect the caller
        print(f"[ops-alerts] ops-fix cross-post failed: {type(e).__name__}: {scrub(e)}")
