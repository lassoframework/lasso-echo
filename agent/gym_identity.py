"""
gym_identity.py — the gym's OWN identity tokens (city / brand) for an overlay anchor.

WHY THIS EXISTS (2026-09-04). story_overlay refuses to burn a Story without an identity
anchor and, by an earlier audit's ruling, carries no default of its own: "the frontend
must supply it" (story_overlay.py). That is right for a coach tap — the portal now sends
it (lasso-ops-portal#581) — but it is impossible for a request Echo builds SERVER-side,
which has no frontend. The event story offer (gym_event.story_studio_create_request) is
exactly that, and it HELD every time with "no identity_tokens (city/gym anchor) were
provided for this render".

This is NOT a default in the sense the audit ruled against. It invents nothing: the
tokens are read from the gym's OWN row in the shared plane (gyms.name, plus the city half
of gyms.market). A gym with no name resolves to [] and the render still HOLDS — the rail
is untouched, it just stops firing on gyms that do have a name.

The mapping is two hops, because Story Studio works in base keys ('pierce') while `gyms`
is keyed by uuid, and the base key is NOT gyms.slug (topfuel -> top-fuel,
district_h -> district-h-strength-fitness). echo_intake_tokens is the authoritative
(gym_id, echo_account_key) roster the portal mints, so it is the join.
"""
from __future__ import annotations

from . import config

# Per-process cache: a gym's name does not change inside one worker's life, and a render
# should not pay two HTTP round trips for it. Keyed by base key; "" results cache too, so
# a nameless gym is not re-queried on every attempt.
_CACHE: dict[str, list] = {}


def _rest(http, path, params):
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return None
    try:
        r = http.get(f"{url.rstrip('/')}/rest/v1/{path}", params=params,
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=30)
        if r.status_code >= 400:
            return None
        return r.json() or []
    except Exception as e:  # noqa: BLE001 - never raise into a render
        print(f"[gym-identity] {path} read failed: {type(e).__name__}: {e}")
        return None


def tokens_from(name, market):
    """PURE. The tokens a gym row yields: its name, plus the city half of `market`
    ("Carmel, IN" -> "Carmel"). Trimmed, de-duplicated case-insensitively, no blanks.
    Mirrors the portal's identityTokensFrom so both lanes anchor identically."""
    out = []
    for raw in (name, str(market or "").split(",")[0]):
        t = str(raw or "").strip()
        if t and not any(t.lower() == o.lower() for o in out):
            out.append(t)
    return out


def tokens_for(base_key, *, http=None, use_cache=True):
    """The gym's identity tokens for an overlay anchor, or [] when unknowable.

    [] is a legitimate answer, not an error: the caller then reaches story_overlay's
    existing rail and the render HOLDS honestly rather than burning an unbranded Story.
    Never raises — a lookup failure must not turn a render into a crash."""
    base = str(base_key or "").strip()
    if not base:
        return []
    if use_cache and base in _CACHE:
        return list(_CACHE[base])

    if http is None:
        import requests  # lazy
        http = requests

    # base key -> gym_id, via the roster the portal itself mints.
    roster = _rest(http, "echo_intake_tokens",
                   {"select": "gym_id,echo_account_key",
                    "echo_account_key": f"eq.{base}"})
    gym_id = ""
    for row in (roster or []):
        gym_id = str((row or {}).get("gym_id") or "").strip()
        if gym_id:
            break
    if not gym_id:
        # Not a mapped portal gym (LASSO's own accounts, a test base). Cache the miss.
        if use_cache:
            _CACHE[base] = []
        return []

    rows = _rest(http, "gyms", {"select": "name,market", "id": f"eq.{gym_id}"})
    row = (rows or [{}])[0] if rows else {}
    tokens = tokens_from(row.get("name"), row.get("market"))
    if use_cache:
        _CACHE[base] = list(tokens)
    return tokens


def reset_cache():
    """Tests and a re-arm: drop the per-process cache."""
    _CACHE.clear()
