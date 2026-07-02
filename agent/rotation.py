"""
Creative rotation + variety guard for the daily feed slot.

Dormant behind AGENT_ROTATION_ENABLED (default OFF = selection exactly as today).
Armed, the day's feed creative is chosen under three rules:

  1. NO-REPEAT WINDOW: never the same creative within the last
     AGENT_ROTATION_WINDOW_DAYS days (default 14). Key = filename for library
     assets, a content signature for generated Nano cards. The served log
     persists on /data so restarts cannot forget it.
  2. PILLAR ROTATION: consecutive days never share a pillar. Library assets
     carry their pillar in the filename family (lasso_p1_* .. lasso_p4_*); the
     generated Nano candidate carries the content brain's pillar for the day.
  3. LIBRARY FIRST-CLASS: the approved content_library assets cycle alongside
     the generated Nano card; Nano is ONE source among several, not the daily
     default. Least-recently-served wins; never-served wins over everything.

FABRICATION GATE SUPREME, never weakened: a creative whose note carries a stat
or claim (a percentage, a dollar figure, an "N times" claim) is EXCLUDED from
rotation unless that sentence's claim is cleared in the approved sources (the
knowledge brain's USE-marked stats or an approved social proof entry). With the
knowledge flag off, every stat-bearing note is excluded - conservative by design.
If exclusions leave nothing fresh, rotation falls back to the OLDEST-served
approved creative and posts one ops alert about the thin approved pool. It never
fabricates and never posts an unapproved stat.

Approval gate, publish flag, and the trust ladder are untouched: this changes
WHICH approved creative a draft proposes, never whether it needs a tap.
"""

import hashlib
import json
import os
import re
from datetime import date

from . import config, content_planner, knowledge, ops_alerts, social_proof
from .daily_studio import build_daily_infographic_draft
from .drafter import draft_post
from .library import list_creatives

_STATE_FILE = "rotation_served.json"
_GENERATED_KEY_PREFIX = "nano:"
_PILLAR_RE = re.compile(r"lasso_(p\d)_", re.IGNORECASE)
# A sentence that carries a claim needing clearance: percents, dollars, "N times/x".
_CLAIM_RE = re.compile(r"%|\bpercent\b|\$\s?\d|\b\d+(?:\.\d+)?\s*(?:x|times)\b", re.IGNORECASE)


# ---- served log (SQLite on /data via agent/db.py; legacy json migrates once) ----
def _legacy_state_path():
    return os.path.join(os.environ.get("AGENT_ROTATION_STATE_DIR", "/data"), _STATE_FILE)


def _conn():
    from . import db as _db
    conn = _db.connect()
    _db.migrate_legacy(conn, served_json=_legacy_state_path())
    return conn


def load_served():
    """{account: [entries oldest..newest]} from the served table (same shape the
    json store returned, so every caller is unchanged)."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT account_key, key, pillar, date, archetype, set_name "
                "FROM served ORDER BY date, id").fetchall()
    except Exception:
        return {}
    served = {}
    for r in rows:
        served.setdefault(r["account_key"], []).append(
            {"key": r["key"], "pillar": r["pillar"], "date": r["date"],
             "archetype": r["archetype"], "set": r["set_name"]})
    return served


def save_served(served):
    """Replace-all write (kept for the few callers that mutate the whole dict)."""
    try:
        with _conn() as conn:
            conn.execute("DELETE FROM served")
            for account_key, entries in (served or {}).items():
                for e in entries:
                    conn.execute(
                        "INSERT INTO served (account_key, key, pillar, date, "
                        "archetype, set_name) VALUES (?,?,?,?,?,?)",
                        (account_key, e.get("key", ""), e.get("pillar", ""),
                         e.get("date", ""), e.get("archetype", ""),
                         e.get("set", "")))
            conn.commit()
    except Exception as e:
        print(f"[rotation] could not persist served log: {type(e).__name__}: {e}")


def record_served(account_key, key, pillar, day_key, archetype="", set_name=""):
    from . import db as _db
    try:
        # in-process serialization (the listener's threads share this process);
        # WAL covers cross-process safety. No write is ever silently dropped.
        with _db._lock, _conn() as conn:
            conn.execute(
                "INSERT INTO served (account_key, key, pillar, date, archetype, "
                "set_name) VALUES (?,?,?,?,?,?)",
                (account_key, key, pillar, day_key, archetype, set_name))
            # prune far beyond the window so the table never grows unbounded
            cutoff = _days_ago(day_key, config.ROTATION_WINDOW_DAYS * 3)
            conn.execute("DELETE FROM served WHERE account_key=? AND date < ?",
                         (account_key, cutoff))
            conn.commit()
    except Exception as e:
        print(f"[rotation] could not persist served log: {type(e).__name__}: {e}")


def _days_ago(day_key, n):
    from datetime import timedelta
    return (date.fromisoformat(day_key) - timedelta(days=n)).isoformat()


# ---- candidate metadata ---------------------------------------------------------
def pillar_of(creative_path):
    """The pillar family a library asset belongs to. VIDEO is its own pillar, so a
    clip day and an infographic day alternate naturally under the pillar rule. Then
    the lasso_p1_* .. p4 filename families, else the stem's first token, else misc."""
    base = os.path.basename(creative_path or "")
    if os.path.splitext(base)[1].lower() in (".mp4", ".mov"):
        return "video"
    m = _PILLAR_RE.search(base)
    if m:
        return m.group(1).lower()
    stem = os.path.splitext(base)[0]
    return (stem.split("_")[0].lower() or "misc") if stem else "misc"


def _approved_claims():
    """Every cleared claim sentence: USE-marked knowledge stats + approved social
    proof entry lines. Empty when those sources are off/absent (conservative)."""
    claims = list(knowledge.usable_stats())
    approved, _ = social_proof.load_entries(config.SOCIAL_PROOF_PATH)
    for entry in approved:
        claims.extend(entry.approved_lines())
    return claims


def is_gate_clean(note, approved_claims=None):
    """
    True when every claim-bearing sentence in the note is cleared by an approved
    source. A note with no stats/claims is clean. NEVER weakened: unresolved
    figures (the 80 percent conversions family included) fail this check until
    they appear in an approved source.
    """
    text = (note or "").strip()
    if not text:
        return True
    sentences = re.split(r"(?<=[.!?])\s+", text)
    dirty = [s for s in sentences if _CLAIM_RE.search(s)]
    if not dirty:
        return True
    claims = approved_claims if approved_claims is not None else _approved_claims()
    for sentence in dirty:
        s = sentence.strip()
        if not any(s in c or c in s for c in claims):
            return False
    return True


def _sidecar_field(creative_path, field):
    try:
        with open(os.path.splitext(creative_path)[0] + ".json", encoding="utf-8") as fh:
            return str((json.load(fh) or {}).get(field, ""))
    except Exception:
        return ""


def sidecar_archetype(creative_path):
    """The layout archetype recorded in a library card's json sidecar ('' when the
    sidecar or field is absent - older cards simply do not vote in alternation)."""
    return _sidecar_field(creative_path, "archetype")


def sidecar_set(creative_path):
    """The concept SET (brand | service) from the card's sidecar, '' when absent."""
    return _sidecar_field(creative_path, "set")


def candidate_set(item):
    """A candidate's concept set: the sidecar's for a library card; the generated
    Nano card drafts from the brand pillars, so it counts as 'brand'."""
    _key, _pillar, kind, payload = item
    if kind == "generate":
        return "brand"
    return sidecar_set(getattr(payload, "path", ""))


def candidate_archetype(item, day_key, library_path):
    """A candidate's archetype: the sidecar's for a library card, the day's
    deterministic rotation for the generated Nano card."""
    key, _pillar, kind, payload = item
    if kind == "generate":
        from .creative_studio import archetype_for_day
        return archetype_for_day(day_key)
    return sidecar_archetype(getattr(payload, "path", ""))


def style_exclusions(library_path):
    """OFF-STYLE creatives (content_library/style_exclusions.json) that rotation must
    never select. Blake regenerates a card in the house style, then removes its line.
    Missing/unreadable file = no exclusions."""
    try:
        with open(os.path.join(library_path, "style_exclusions.json"), encoding="utf-8") as fh:
            return set((json.load(fh) or {}).get("off_style", []))
    except Exception:
        return set()


def content_signature(fragments):
    """The no-repeat key for a generated card: sha1 of its approved fragments."""
    joined = "\n".join(fragments or [])
    return _GENERATED_KEY_PREFIX + hashlib.sha1(joined.encode()).hexdigest()[:16]


# ---- selection --------------------------------------------------------------------
def choose(account_key, day_key, library_path, poster=None):
    """
    The day's creative choice for this account:
      ("library", Creative)   - draft from this approved library asset
      ("generate", pillar)    - generate the Nano card (content brain pillar given)
      (None, None)            - rotation has nothing to add (caller keeps today's path)
    Enforces the window, pillar alternation, and the fabrication gate; falls back
    to the oldest-served approved creative (with one ops alert) on a thin pool.
    """
    entries = load_served().get(account_key, [])
    cutoff = _days_ago(day_key, config.ROTATION_WINDOW_DAYS)
    recent = [e for e in entries if e.get("date", "") >= cutoff and e.get("date", "") < day_key]
    recent_keys = {e["key"] for e in recent}
    yesterday_pillar = None
    yesterday_archetype = None
    yesterday_set = None
    if entries:
        prior = [e for e in entries if e.get("date", "") < day_key]
        if prior:
            last = sorted(prior, key=lambda e: e["date"])[-1]
            yesterday_pillar = last.get("pillar")
            yesterday_archetype = last.get("archetype") or None
            yesterday_set = last.get("set") or None

    approved_claims = _approved_claims()

    # Candidates: every gate-clean, IN-STYLE library asset + the generated-Nano option.
    off_style = style_exclusions(library_path)
    candidates = []  # (key, pillar, kind, payload)
    excluded_dirty = 0
    from . import dam, db
    for c in list_creatives(library_path):
        base = os.path.basename(c.path)
        if base in off_style:
            db.audit("exclusion", base, "off-style (pre house-style card)",
                     account_key, day_key)
            continue  # OFF-STYLE (pre house-style card): never selected, never deleted
        if base.startswith("lasso_v2_") and os.path.splitext(base)[0].endswith("_story"):
            continue  # a generated 9:16 story VARIANT (regen convention) is never a
            # feed candidate; a topic card that merely ends in "story" still rotates
        if dam.consent_blocked(c.path):
            db.audit("exclusion", base, "consent guard (fail safe)",
                     account_key, day_key)
            continue  # consent guard (fail safe): the card path never sees it
        if not is_gate_clean(getattr(c, "client_note", ""), approved_claims):
            excluded_dirty += 1
            db.audit("exclusion", base, "fabrication gate (uncleared claim in note)",
                     account_key, day_key)
            continue
        # near-dupes share a rotation key, so the window blocks the whole group
        candidates.append((dam.rotation_key(c.path), pillar_of(c.path), "library", c))

    gen_pillar = None
    plan = content_planner.plan_for(day_key)
    if not plan.get("blocked"):
        gen_pillar = plan["pillar"]
        gen_key = content_signature(plan.get("fragments"))
        candidates.append((gen_key, f"brain:{gen_pillar}", "generate", gen_pillar))

    if not candidates:
        return None, None

    last_served = {e["key"]: e["date"] for e in sorted(entries, key=lambda e: e["date"])}

    def _eligible(item):
        key, pillar, _, _ = item
        if key in recent_keys:
            return False  # no repeat inside the window
        if yesterday_pillar is not None and pillar == yesterday_pillar:
            return False  # never the same pillar two days running
        return True

    pool = [it for it in candidates if _eligible(it)]
    if pool:
        # SOFT variety preferences first: a different archetype AND a different
        # concept set (brand vs service) than yesterday. Both are preferences,
        # never filters - if every eligible candidate matches yesterday, one still
        # gets picked. Then never-served, then least-recently-served; name-stable
        # tiebreak. The hard rules (window, pillar, gate) already filtered above.
        def _variety_penalty(item):
            penalty = 0
            arch = candidate_archetype(item, day_key, library_path)
            if yesterday_archetype and arch and arch == yesterday_archetype:
                penalty += 1
            cset = candidate_set(item)
            if yesterday_set and cset and cset == yesterday_set:
                penalty += 1
            return penalty

        pool.sort(key=lambda it: (_variety_penalty(it), last_served.get(it[0], ""), it[0]))
        key, pillar, kind, payload = pool[0]
        db.audit("selection", key,
                 f"kind={kind} pillar={pillar} eligible={len(pool)} "
                 f"variety_penalty={_variety_penalty(pool[0])} "
                 f"last_served={last_served.get(key, 'never')} window_ok=yes",
                 account_key, day_key)
        return kind, payload

    # Thin approved pool: everything fresh was excluded. Fall back to the OLDEST
    # served approved creative and say so, loudly, once.
    ops_alerts.alert(f"rotation: approved creative pool is thin for {account_key} "
                     f"({excluded_dirty} asset(s) held by the fabrication gate); "
                     "falling back to the oldest approved creative. Approve more "
                     "creatives or clear the pending stats.")
    served_candidates = [it for it in candidates if it[0] in last_served]
    if served_candidates:
        served_candidates.sort(key=lambda it: (last_served.get(it[0], ""), it[0]))
        key, pillar, kind, payload = served_candidates[0]
        db.audit("selection", key,
                 f"THIN POOL fallback: oldest served approved creative "
                 f"({excluded_dirty} held by the fabrication gate)",
                 account_key, day_key)
        return kind, payload
    return None, None


def build_rotated_draft(account, day_key, voice, library_path, poster=None,
                        nano_client=None, s3_client=None):
    """
    The rotation-guided feed draft, or None when dormant / nothing to add (the
    caller's existing selection order then runs unchanged).
    """
    if not config.rotation_enabled():
        return None

    kind, payload = choose(account.key, day_key, library_path, poster=poster)
    if kind == "generate":
        draft = build_daily_infographic_draft(account, day_key,
                                              nano_client=nano_client,
                                              s3_client=s3_client)
        if draft is None:
            return None  # studio unavailable; caller's fallback takes the day
        if draft.status.value != "blocked":
            from .creative_studio import archetype_for_day
            record_served(account.key, content_signature(draft.source_fragments),
                          f"brain:{payload}", day_key,
                          archetype=archetype_for_day(day_key), set_name="brand")
        return draft
    if kind == "library":
        from . import schedule
        draft = draft_post(account, payload, schedule.scheduled_for(day_key), voice=voice)
        if draft.status.value != "blocked":
            from . import dam
            record_served(account.key, dam.rotation_key(payload.path),
                          pillar_of(payload.path), day_key,
                          archetype=sidecar_archetype(payload.path),
                          set_name=sidecar_set(payload.path))
        return draft
    return None
