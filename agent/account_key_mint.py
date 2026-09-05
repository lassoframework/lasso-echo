"""
account_key_mint.py — derive the CANONICAL account_key for a gym at mint time, so a
NEWLY minted intake link can never carry an ad-hoc key that later disagrees with
gyms.slug / the Zernio handle (the topfuel -> top-fuel / district_h stranding class).

WHERE THIS SITS: the ONE mint choke point (intake_web.link_for -> intake_tokens.mint) works
on a bare string client_key and has NO gym_id in hand. The layer that DOES have gym context
is onboard.run(account_key, display_name). This module is the derivation that layer calls:
given the caller's chosen account_key + the gym's display name, it resolves the portal
gyms.id UUID (via the same read-only resolve_gym_uuid every shared-plane path shares) and
folds it into canonical_account_key, returning a deterministic, collision-safe,
slug-consistent key that the onboard artifacts + the mint then use.

WHY THIS CANNOT BREAK EXISTING LINKS: already-signed tokens self-decode their OWN key from
the HMAC signature (intake_tokens.verify recomputes the MAC over the embedded key). This
module only chooses the key handed to a FRESH mint; it never re-signs, re-mints, or
re-resolves an already-issued token. Two idempotency guards make a re-run a no-op:
  1. issued_key — if a gym row already exists locally under the passed key, that key is
     treated as ISSUED and returned verbatim (canonical_account_key honors issued_key first),
     so re-onboarding a live gym never re-keys it and never strands its existing link.
  2. no-uuid honesty — if the portal uuid can't be resolved (dev host with no Supabase creds,
     or an unresolvable base), the passed key is kept verbatim. We NEVER fabricate a gym_id
     and NEVER block the mint.

RAILS: read-only (only reads gyms via resolve_gym_uuid; writes nothing here), no fabrication,
behind config.canonical_mint_enabled() (defaults ON because it ONLY affects new links). All
I/O is injectable (resolve_uuid / gym_exists) so tests run fully offline.
"""

from . import config
from .account_key import canonical_account_key


def _base_of(account_key):
    """The portal base for an account key: strip a trailing _ig / _fb. Mirrors
    zernio_routes._base_from_account so the uuid resolver sees the same base the rest of the
    shared plane uses."""
    base = (account_key or "").strip()
    for suf in ("_ig", "_fb"):
        if base.endswith(suf):
            return base[: -len(suf)]
    return base


def _default_resolve_uuid(base):
    """Live portal gyms.id UUID for a base, or None. Read-only; a missing creds / unresolved
    base is an honest None (never a crash, never a guess). Best effort so onboarding on a dev
    host with no Supabase never fails."""
    try:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
        if not store.available():
            return None
        return store.resolve_gym_uuid(base)
    except Exception:  # noqa: BLE001 - resolver failure is an honest None
        return None


def _default_gym_exists(account_key):
    """True iff a local Echo gyms row already exists under this exact key (a re-run signal).
    Best effort; a read failure is treated as 'not existing' so a fresh derivation still
    happens rather than crashing."""
    try:
        from . import db
        return db.gym_get(account_key) is not None
    except Exception:  # noqa: BLE001
        return False


def derive_mint_key(account_key, display_name, *, resolve_uuid=None, gym_exists=None):
    """The account_key a NEW intake link should be minted under.

    Returns (key, info) where info is a dict describing what happened:
      {"derived": bool, "reason": str, "gym_uuid": str|None}

    Rules, in order:
      * flag OFF (config.canonical_mint_enabled() false) -> passed key verbatim.
      * gym already exists locally under the passed key (re-run) -> passed key verbatim
        (idempotent: never re-key a live gym, never strand its existing link).
      * portal uuid unresolved -> passed key verbatim (never fabricate a gym_id).
      * blank display_name -> passed key verbatim (canonical_account_key would honestly
        reject a nameless derivation; we never fabricate a name).
      * otherwise -> canonical_account_key(uuid, display_name), which is deterministic,
        tenant-unique, slug-consistent, and collision-safe.

    Pure aside from the injected resolve_uuid / gym_exists callbacks. Never raises: any
    derivation error falls back to the passed key (safe default, honest)."""
    passed = (account_key or "").strip()
    info = {"derived": False, "reason": "", "gym_uuid": None}

    if not config.canonical_mint_enabled():
        info["reason"] = "flag off (AGENT_CANONICAL_MINT=false)"
        return passed, info

    resolve_uuid = resolve_uuid or _default_resolve_uuid
    gym_exists = gym_exists or _default_gym_exists

    # Idempotency: a gym that already has a local row under this key keeps its key. Its link
    # (if any) is already signed under this key; re-keying would strand it.
    try:
        if passed and gym_exists(passed):
            info["reason"] = "existing gym row (idempotent, key kept)"
            return passed, info
    except Exception:  # noqa: BLE001
        pass

    base = _base_of(passed)
    try:
        gym_uuid = resolve_uuid(base)
    except Exception:  # noqa: BLE001
        gym_uuid = None
    if not gym_uuid:
        info["reason"] = "portal uuid unresolved (key kept, no fabrication)"
        return passed, info
    info["gym_uuid"] = str(gym_uuid)

    if not (display_name or "").strip():
        info["reason"] = "no display name (key kept, no fabrication)"
        return passed, info

    # THE PORTAL'S KEY WINS (2026-09-04). The portal is where a gym is created and where
    # its key is first minted (social-onboard.ts deriveAccountKey = slug + rawUUID[:6]).
    # Echo deriving its OWN key from the same gym_id (slug + sha256(gym_id)[:6]) is exactly
    # what gave one gym two live keys -- CrossFit Reverb ran as crossfitreverb30b5b2 in the
    # portal and crossfitreverb6cdf33 in Echo, and Dean got "93 posts drafted" over an empty
    # approve list. Deriving here at all is only correct when the portal has issued nothing.
    from . import account_key_resolve as _akr
    portal_key = _akr.portal_key_for_gym(str(gym_uuid), fresh=True)
    if portal_key:
        canonical = portal_key
        info["source"] = "portal"
    else:
        try:
            canonical = canonical_account_key(str(gym_uuid), display_name)
        except (ValueError, RuntimeError):
            info["reason"] = "canonical derivation rejected (key kept)"
            return passed, info
        info["source"] = "derived"

    # Preserve the _ig / _fb suffix convention the mint / env-suffix layer expects: the
    # canonical key is a base; if the caller passed a suffixed key, re-apply the suffix so
    # downstream lane logic (calendar_autopublish, accounts registry) still splits it.
    for suf in ("_ig", "_fb"):
        if passed.endswith(suf):
            canonical = f"{canonical}{suf}"
            break

    info["derived"] = (canonical != passed)
    if not info["derived"]:
        info["reason"] = "already canonical"
    elif info.get("source") == "portal":
        info["reason"] = "portal's issued key (authoritative; Echo never re-derives)"
    else:
        info["reason"] = "canonical (portal uuid folded in)"
    return canonical, info
