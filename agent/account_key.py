"""
account_key.py — the ONE canonical, collision-safe derivation of a gym's
account_key (its opaque Zernio-facing tenant slug, e.g. "zanshinfitness630e22").

THE ACCOUNT-KEY COLLISION BUG (Bird Dog CrossFit / Bolton Club, live): account_key
was set once, ad hoc, with NO canonical derivation and DERIVED FROM NAME ALONE. Two
gyms whose names normalise to the same slug collided onto ONE account_key (one gym's
posts could land on another gym's socials — a tenant-isolation hazard), while the same
gym re-keyed under a different code got TWO keys (duplicate Zernio profiles + a false
"not connected"). This module is the single source of truth that makes the derivation:

  * DETERMINISTIC     — same (gym_id, gym_name) always yields the same key.
  * TENANT-UNIQUE     — the STABLE gym_id (never the name alone) is folded into the key,
                        so two different gyms with near-identical names get DIFFERENT keys.
  * SLUGIFIED         — lowercase, [a-z0-9] only, safe as a URL/table key.
  * IDEMPOTENT        — re-deriving an already-issued key never changes it. If a key was
                        already issued for this gym_id, that exact key is returned verbatim
                        (via the optional issued_key argument), never recomputed away.
  * COLLISION-SAFE    — if the computed key is already TAKEN by a DIFFERENT gym_id (checked
                        via the injected is_taken callback), a deterministic disambiguator is
                        appended, walked in a fixed order until a free key is found.

RAILS: pure (no I/O, no env, no globals), no fabrication (a blank gym_name is honestly
rejected — we never invent a name), and it NEVER derives from the name alone. Behind no
flag itself (it computes nothing side-effecting); the WRITE path that consumes it stays
flagged + guarded (see account_key_guard.py).
"""

import hashlib
import re

# The length of the stable-id fingerprint folded into every key. Six hex chars = 24 bits
# = ~16.7M buckets: collision-safe across a fleet of gyms while keeping the key short and
# human-recognisable ("zanshinfitness630e22" style). The disambiguator below is the hard
# guarantee; this is the first line of separation.
_ID_FINGERPRINT_LEN = 6

# The name-slug is capped so a long gym name cannot crowd out the id fingerprint.
_NAME_SLUG_MAXLEN = 24


def _slugify_name(name: str) -> str:
    """Lowercase, strip to [a-z0-9], collapse everything else away. Conservative and
    stable: 'Bird Dog CrossFit' -> 'birddogcrossfit'. Empty when the name has no
    alphanumerics (the caller treats that as "no usable name" and must not fabricate one)."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s[:_NAME_SLUG_MAXLEN]


def _id_fingerprint(gym_id: str) -> str:
    """A stable, short hex fingerprint of the gym's STABLE id. This is the bit that makes
    the key tenant-unique: two gyms with the same name-slug but different gym_ids fold in
    different fingerprints, so they can never share a base key. Deterministic (sha256 of the
    exact id string), so re-deriving is idempotent."""
    digest = hashlib.sha256((gym_id or "").encode("utf-8")).hexdigest()
    return digest[:_ID_FINGERPRINT_LEN]


def _base_key(gym_id: str, gym_name: str) -> str:
    """The canonical base key BEFORE any collision walk: <name-slug><id-fingerprint>.
    Never name-alone (the fingerprint is always present). Never id-alone (the name-slug
    keeps it human-recognisable). Deterministic in both inputs."""
    return f"{_slugify_name(gym_name)}{_id_fingerprint(gym_id)}"


def canonical_account_key(gym_id, gym_name, *, is_taken=None, issued_key=None,
                          max_disambiguators=1000):
    """The canonical account_key for a gym.

    gym_id    : the gym's STABLE unique id (the portal gyms.id UUID, or any stable id).
                REQUIRED — without it we would be deriving from the name alone, the exact
                bug this module exists to kill. A blank gym_id raises ValueError.
    gym_name  : the gym's display / brand name. REQUIRED and must contain at least one
                alphanumeric — we slugify it, we never fabricate one. Blank raises ValueError.

    is_taken  : optional callable(candidate_key) -> bool. Returns True iff `candidate_key`
                is ALREADY bound to a DIFFERENT gym_id (the caller owns that check against
                its own store — it must return False for a key already owned by THIS gym_id
                so idempotency holds). When the base key is taken, a deterministic
                disambiguator (2, 3, 4, ...) is appended until a free key is found.
    issued_key: optional str. The account_key already issued to THIS gym_id, if any. When
                provided (non-empty), it is returned verbatim — an already-issued key is
                NEVER recomputed away, which is what makes re-runs idempotent even if the
                gym later renames. The caller is responsible for passing the true issued key.

    Returns a lowercase [a-z0-9] key. Pure: the only side effects are the is_taken callbacks
    the caller injected."""
    # IDEMPOTENCY (highest priority): an already-issued key is authoritative and returned
    # untouched. A gym that renames keeps its original key; a re-run never re-mints.
    if issued_key:
        issued = str(issued_key).strip()
        if issued:
            return issued

    gym_id = (str(gym_id) if gym_id is not None else "").strip()
    if not gym_id:
        raise ValueError("canonical_account_key requires a stable gym_id "
                         "(never derive from the name alone)")
    name_slug = _slugify_name(gym_name)
    if not name_slug:
        raise ValueError("canonical_account_key requires a gym_name with at least one "
                         "alphanumeric character (we never fabricate a name)")

    base = _base_key(gym_id, gym_name)
    if is_taken is None:
        return base

    # COLLISION WALK: only ever triggered when the base key is held by a DIFFERENT gym_id.
    # Deterministic order (2, 3, 4, ...) so the same collision always resolves to the same
    # disambiguated key across re-runs. The id fingerprint already makes a real collision
    # astronomically unlikely; this is the hard belt-and-suspenders guarantee.
    if not is_taken(base):
        return base
    for n in range(2, max_disambiguators + 1):
        candidate = f"{base}{n}"
        if not is_taken(candidate):
            return candidate
    raise RuntimeError(
        f"canonical_account_key: exhausted {max_disambiguators} disambiguators for base "
        f"{base!r}; refusing to fabricate a key")
