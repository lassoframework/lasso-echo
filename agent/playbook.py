"""playbook.py — Wave 7: the per-gym versioned playbook the planner reads, the
bounds the optimizer can NEVER cross, and the cross-gym priors.

The playbook is ONE versioned JSON per gym (gym_playbook table): pillar weights
ABOVE the Wave 2 floors only, hook_family weights, format split, top time
slots, boosted concepts, and retired concepts (each with a reason and an
auto-expiry date so nothing is banned forever on thin evidence).

BOUNDS, NON-NEGOTIABLE: the optimizer tunes INSIDE the A-grade structure,
never against it. Quota floors, the avatar rail, the ask rule, offer rules,
consent rules, and the copy gate are INVISIBLE to it — apply_bounds REFUSES
any key that touches them (PROTECTED_KEYS below). Max drift per weight per
month: plus or minus 20% (DRIFT_CAP). Every playbook write is a NEW version
row with updated_by='monthly_retro' and the evidence rows behind it; old
versions are never mutated, so a coach can read exactly why Echo now prefers
story openers for this gym, and revert it.

Cross-gym priors (7.6) do exactly two jobs: seed a NEW gym's day-one playbook,
and break ties when a gym's own data is under the sample floor. A gym's own
evidence always overrides the prior as it accumulates. Only NON-TAINTED gyms
contribute, and priors are anonymous lever aggregates — never one gym's rows.

Stores are injectable everywhere; the default store speaks PostgREST to the
shared Supabase (the portal_calendar_store pattern). content_calendar writes
still go ONLY through Echo's store — this module writes gym_playbook rows only.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

from . import config
from .learning_guards import MIN_SAMPLE, sample_floor

# ---- bounds ------------------------------------------------------------------------

DRIFT_CAP = 0.20   # max relative change per weight per month (matches learning_guards)

# Any proposed key that IS one of these, or CONTAINS one of these tokens, is
# REFUSED outright. The optimizer cannot trade a floor, a rail, an ask rule, a
# consent rule, or the copy gate away for engagement — they are invisible to it.
PROTECTED_KEYS = frozenset({
    "quota_floors", "quota_floor", "category_floors", "category_floor", "floors",
    "avatar", "avatar_rail", "avatar_rails", "gen_pop",
    "ask_rule", "ask_rules", "ask_required",
    "offer_rules", "offer_rule",
    "consent", "consent_rules", "consent_rule", "consent_guard",
    "copy_gate", "dash_rule", "dashes",
    "approval", "approval_gate", "auto_approve", "publish", "publish_gate",
})
_PROTECTED_TOKENS = ("floor", "avatar", "ask_rule", "consent", "copy_gate",
                     "approval", "publish", "offer_rule", "quota")

# The only keys a playbook may carry (7.5's shape). Anything else is dropped
# by apply_bounds (unknown keys can smuggle rail changes in under new names).
ALLOWED_KEYS = ("pillar_weights", "hook_family_weights", "format_split",
                "top_time_slots", "boosted_concepts", "retired_concepts")

EMPTY_PLAYBOOK = {
    "pillar_weights": {},        # above the Wave 2 floors ONLY; floors live elsewhere
    "hook_family_weights": {},
    "format_split": {},
    "top_time_slots": [],
    "boosted_concepts": [],
    "retired_concepts": [],      # each: {concept, reason, expires} — never forever
}


class PlaybookRefused(ValueError):
    """A proposed change touched a protected key. The change is refused whole."""


def _is_protected(key: str) -> bool:
    k = str(key or "").lower()
    if k in PROTECTED_KEYS:
        return True
    return any(tok in k for tok in _PROTECTED_TOKENS)


def clamp_drift(current: float, proposed: float, cap: float = DRIFT_CAP) -> float:
    """Clamp one weight's move to within plus or minus cap (relative) of its
    current value. A weight with no meaningful current value (<= 0 / missing)
    is a SEED and passes through — there is nothing to drift from."""
    if not isinstance(current, (int, float)) or isinstance(current, bool) or current <= 0:
        return float(proposed)
    lo = float(current) * (1.0 - cap)
    hi = float(current) * (1.0 + cap)
    return min(max(float(proposed), lo), hi)


def apply_bounds(current: dict, proposed: dict) -> tuple[dict, list]:
    """Apply the non-negotiable bounds to a proposed playbook change.

    Returns (bounded, refused):
      bounded — the change with every weight clamped to plus or minus DRIFT_CAP
                per month against `current`, and unknown keys dropped.
      refused — the list of protected keys that were REFUSED (raised to the
                caller's findings; the change to them is NEVER applied).

    A protected key at the top level OR inside any weight dict is refused.
    Old playbooks are never mutated (deep-copied in)."""
    cur = copy.deepcopy(current or {})
    refused = []
    bounded = {}
    for key, value in (proposed or {}).items():
        if _is_protected(key):
            refused.append(str(key))
            continue
        if key not in ALLOWED_KEYS:
            refused.append(str(key))
            continue
        if isinstance(value, dict):
            cur_weights = cur.get(key) or {}
            out = {}
            for wk, wv in value.items():
                if _is_protected(wk):
                    refused.append(f"{key}.{wk}")
                    continue
                if isinstance(wv, (int, float)) and not isinstance(wv, bool):
                    out[wk] = clamp_drift(cur_weights.get(wk), wv)
                else:
                    out[wk] = wv
            bounded[key] = out
        else:
            bounded[key] = copy.deepcopy(value)
    return bounded, refused


# ---- store (injectable; default PostgREST) -------------------------------------------


class PlaybookStoreError(Exception):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"gym_playbook {status}: {detail}")


class SupabasePlaybookStore:
    """Thin PostgREST client over gym_playbook. `http` injectable for tests
    (portal_calendar_store pattern). INSERT-ONLY on writes: a new version row is
    appended; no UPDATE or DELETE method exists here, so old versions are
    immutable by construction."""

    def __init__(self, url=None, service_key=None, http=None):
        self._url = url if url is not None else config.supabase_url()
        self._key = service_key if service_key is not None else config.supabase_service_key()
        self._http = http

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, repo pattern
        return requests

    def _headers(self, extra=None):
        h = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
             "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def latest(self, gym_id):
        """The latest gym_playbook row for gym_id, or None."""
        r = self._client().get(
            f"{self._url}/rest/v1/gym_playbook",
            params={"gym_id": f"eq.{gym_id}", "order": "version.desc", "limit": "1"},
            headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise PlaybookStoreError(r.status_code, (r.text or "")[:200])
        rows = r.json() or []
        return rows[0] if rows else None

    def insert_version(self, row):
        """INSERT one NEW version row. Never updates an existing row."""
        r = self._client().post(
            f"{self._url}/rest/v1/gym_playbook",
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=row, timeout=30)
        if r.status_code >= 400:
            raise PlaybookStoreError(r.status_code, (r.text or "")[:200])
        out = r.json() or []
        return out[0] if out else row


def _default_store():
    return SupabasePlaybookStore()


# ---- load / propose --------------------------------------------------------------------


def load_playbook(gym_id: str, store=None) -> dict:
    """The gym's LATEST playbook dict (the jsonb payload of the highest version
    row), or a fresh EMPTY_PLAYBOOK when the gym has none yet. Read-only. Any
    store failure degrades to the empty default (the planner then behaves
    exactly as before Wave 7 — never worse)."""
    store = store or _default_store()
    try:
        row = store.latest(gym_id)
    except Exception:
        return copy.deepcopy(EMPTY_PLAYBOOK)
    if not row:
        return copy.deepcopy(EMPTY_PLAYBOOK)
    pb = row.get("playbook")
    if isinstance(pb, str):
        try:
            pb = json.loads(pb)
        except ValueError:
            pb = None
    if not isinstance(pb, dict):
        return copy.deepcopy(EMPTY_PLAYBOOK)
    merged = copy.deepcopy(EMPTY_PLAYBOOK)
    merged.update(pb)
    return merged


def propose_update(gym_id: str, changes: dict, evidence, store=None,
                   updated_by: str = "monthly_retro", now=None) -> dict:
    """Apply bounds to `changes` then write a NEW gym_playbook version row.

    - Every weight change is clamped to plus or minus DRIFT_CAP per month.
    - Any key touching quota floors, avatar rails, ask rules, consent, or the
      copy gate is REFUSED (returned in 'refused', never written).
    - `evidence` is the jsonb evidence-row list behind the change — a playbook
      write without evidence is refused whole (no invented learning).
    - Old versions are NEVER mutated: the store only ever inserts.

    Returns {version, playbook, refused, wrote}. When the bounded change is
    empty (everything refused or a no-op), NO row is written (wrote=False)."""
    if not gym_id:
        raise ValueError("gym_id required")
    if not evidence:
        raise PlaybookRefused("refusing playbook write without evidence rows")
    store = store or _default_store()
    latest = None
    try:
        latest = store.latest(gym_id)
    except Exception:
        latest = None
    current_version = int((latest or {}).get("version") or 0)
    current = load_playbook(gym_id, store=store)

    bounded, refused = apply_bounds(current, changes)
    merged = copy.deepcopy(current)
    changed = False
    for key, value in bounded.items():
        if merged.get(key) != value:
            merged[key] = value
            changed = True
    if not changed:
        return {"version": current_version, "playbook": current,
                "refused": refused, "wrote": False}

    now = now or datetime.now(timezone.utc)
    row = {
        "gym_id": gym_id,
        "version": current_version + 1,
        "updated_by": updated_by,
        "playbook": merged,
        "evidence": evidence,
        "created_at": now.isoformat(),
    }
    store.insert_version(row)
    return {"version": current_version + 1, "playbook": merged,
            "refused": refused, "wrote": True}


# ---- planner consumption (pure helpers; behind AGENT_LEARNING_LOOP at the call site) ----


def bias_pillar_order(order, playbook) -> list:
    """Reorder a FALLBACK pillar order by the playbook's pillar_weights,
    descending, stable for un-weighted pillars. This only changes WHICH REAL
    pillar fills a fallback day — it never invents content, never touches the
    Wave 2 floors (the A-gate still grades the staged month), and never
    reorders a slot's own primary category."""
    pb = playbook or {}
    weights = pb.get("pillar_weights") or {}
    base = list(order or [])

    def _w(cat):
        v = weights.get(cat)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

    return sorted(base, key=lambda c: (-_w(c), base.index(c)))


def preferred_time_slot(playbook, index: int = 0):
    """The playbook's best time slot band for the index-th post of a day, or
    None when the playbook has none (the caller keeps the schedule default)."""
    slots = (playbook or {}).get("top_time_slots") or []
    slots = [s for s in slots if isinstance(s, str) and s]
    if not slots:
        return None
    return slots[index % len(slots)]


EXPERIMENT_FRACTION = 0.15
EXPERIMENT_LEVERS = ("hook_family", "ask_type", "time_slot", "caption_len_band")


def experiment_lever_for(gym_id: str, month: str, playbook=None) -> str:
    """ONE lever under test per gym per month — no factorial soup on a 30-post
    account. The playbook may pin next month's experiment; otherwise the lever
    rotates deterministically by gym+month."""
    pb = playbook or {}
    pinned = pb.get("next_experiment")
    if isinstance(pinned, str) and pinned in EXPERIMENT_LEVERS:
        return pinned
    seed = sum(ord(c) for c in f"{gym_id}:{month}")
    return EXPERIMENT_LEVERS[seed % len(EXPERIMENT_LEVERS)]


def label_experiments(rows, gym_id: str, month: str, playbook=None,
                      fraction: float = EXPERIMENT_FRACTION) -> list:
    """Mark ~fraction of the month's FEED rows as labeled experiments: one lever
    varied, everything else held. Stamps experiment_label
    ('<lever>:<YYYY-MM>') on evenly spaced feed rows, deterministically, in
    place. Only labeled experiments support causal claims in the retro. Rows
    already labeled are never relabeled."""
    rows = rows or []
    feed_rows = [r for r in rows
                 if isinstance(r, dict) and (r.get("format") or "feed") == "feed"]
    if not feed_rows:
        return rows
    lever = experiment_lever_for(gym_id, month, playbook)
    label = f"{lever}:{month}"
    count = max(1, int(round(len(feed_rows) * fraction)))
    step = max(1, len(feed_rows) // count)
    labeled = 0
    for i, r in enumerate(feed_rows):
        if labeled >= count:
            break
        if i % step == 0 and not r.get("experiment_label"):
            r["experiment_label"] = label
            labeled += 1
    return rows


# ---- 7.6 cross-gym priors ----------------------------------------------------------------


def compute_priors(all_gym_metrics) -> dict:
    """Anonymous lever priors from NON-TAINTED gyms only.

    Input: a list of per-gym lever stats:
      {gym_id, tainted: bool,
       lever_scores: {lever: {value: {"n": int, "mean_score": float}}}}

    Output (anonymous — no gym ids survive):
      {lever: {value: {"n": total_n, "mean_score": n-weighted mean}}}

    Priors do exactly two jobs (seed_playbook / break_tie below): seed a NEW
    gym's day-one playbook, and break ties under the sample floor. A gym's own
    evidence always overrides them."""
    agg = {}
    for gym in all_gym_metrics or []:
        if not isinstance(gym, dict) or gym.get("tainted"):
            continue
        for lever, values in (gym.get("lever_scores") or {}).items():
            for value, stat in (values or {}).items():
                n = (stat or {}).get("n") or 0
                s = (stat or {}).get("mean_score")
                if not n or not isinstance(s, (int, float)) or isinstance(s, bool):
                    continue
                slot = agg.setdefault(lever, {}).setdefault(
                    value, {"n": 0, "_sum": 0.0})
                slot["n"] += int(n)
                slot["_sum"] += float(s) * int(n)
    out = {}
    for lever, values in agg.items():
        out[lever] = {}
        for value, slot in values.items():
            out[lever][value] = {"n": slot["n"],
                                 "mean_score": slot["_sum"] / slot["n"]}
    return out


def seed_playbook_from_priors(priors: dict) -> dict:
    """Job 1: a NEW gym's day-one playbook seeded from the fleet's priors, so
    gym #41 starts where the fleet is. Weights are the priors' mean scores
    normalized per lever; nothing else is seeded (no concepts are boosted or
    retired on another gym's behavior)."""
    pb = copy.deepcopy(EMPTY_PLAYBOOK)
    hooks = (priors or {}).get("hook_family") or {}
    if hooks:
        total = sum(v["mean_score"] for v in hooks.values() if v.get("mean_score"))
        if total > 0:
            pb["hook_family_weights"] = {
                k: round(v["mean_score"] / total, 4) for k, v in hooks.items()}
    fmts = (priors or {}).get("format") or {}
    if fmts:
        total = sum(v["mean_score"] for v in fmts.values() if v.get("mean_score"))
        if total > 0:
            pb["format_split"] = {
                k: round(v["mean_score"] / total, 4) for k, v in fmts.items()}
    slots = (priors or {}).get("time_slot") or {}
    if slots:
        ranked = sorted(slots.items(), key=lambda kv: -(kv[1].get("mean_score") or 0))
        pb["top_time_slots"] = [k for k, _ in ranked[:3]]
    return pb


def break_tie(own_stats: dict, priors_for_lever: dict,
              min_sample: int = MIN_SAMPLE):
    """Job 2: pick between lever values when the gym's OWN data is under the
    sample floor. Own evidence at/above the floor ALWAYS wins; the prior only
    ever breaks a tie the gym cannot yet break itself. Returns the winning
    value name, or None when neither leg can honestly decide."""
    own = own_stats or {}
    scored = {k: v for k, v in own.items()
              if sample_floor(v.get("n") or 0, min_sample)
              and isinstance(v.get("mean_score"), (int, float))}
    if scored:
        return max(scored, key=lambda k: scored[k]["mean_score"])
    pri = {k: v for k, v in (priors_for_lever or {}).items()
           if isinstance((v or {}).get("mean_score"), (int, float))}
    if pri:
        return max(pri, key=lambda k: pri[k]["mean_score"])
    return None
