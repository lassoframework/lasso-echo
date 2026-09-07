"""
diagnostics.py — read-only, per-gym, typed-fact diagnostic queries.

Each diagnostic returns a facts.GroundingSnapshot: a flat dict of enumerated keys to
bool/int/float/str. Never free text, never a model's opinion, never a dict of dicts.
The same function serves both stages -- run it once for 'diagnosis' and again, after
the remedy, for 'verification'. verify.py refuses to accept a verification produced
by a DIFFERENT diagnostic, so "verified" can never mean "a friendlier query agreed".

THE JOIN KEY, WHICH IS EASY TO GET WRONG AND SILENT WHEN YOU DO.
`media_source.gym_id` and `media_asset.gym_id` are keyed by the ECHO ACCOUNT-KEY SLUG
(e.g. 'crossfitlocal'), NOT by the portal's gym uuid
(e.g. '43f2707f-6c25-4b18-ae12-bb3abd48907c'). Confirmed against the comment on the
media_source query in lasso-ops-portal
(src/app/api/gyms/[gymId]/social/flow/route.ts). Pass a uuid here and every query
returns zero rows and every diagnosis reads "nothing is connected" -- a false negative
that looks exactly like a real finding. So require_account_key() REFUSES a uuid rather
than querying with it. Failing loudly on the wrong key is the whole point; a diagnosis
built on the wrong join key is worse than no diagnosis.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from . import facts as _facts

DIAG_DRIVE_PHOTOS = "drive_photos"
DIAG_CTA_POOL = "cta_pool"

ALL_DIAGNOSTICS = frozenset({DIAG_DRIVE_PHOTOS, DIAG_CTA_POOL})

# A portal gym id. Never a media_source.gym_id.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
# The shape an account key actually has in the shared plane (see
# scout-listener/src/support-diagnostics.js ACCOUNT_KEY_RE — deliberately the same).
_ACCOUNT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class DiagnosticError(RuntimeError):
    pass


def require_account_key(gym_key):
    """The ONE join key these tables accept. Raises on a portal uuid or on anything
    that is not a plain slug, rather than running a query that would silently return
    nothing."""
    key = str(gym_key or "").strip()
    if not key:
        raise DiagnosticError("no gym key given")
    if _UUID_RE.match(key):
        raise DiagnosticError(
            f"{key!r} is a portal gym uuid. media_source.gym_id / media_asset.gym_id "
            f"are keyed by the Echo ACCOUNT-KEY SLUG (e.g. 'crossfitlocal'). Querying "
            f"with the uuid returns zero rows and reads as 'nothing connected' — a "
            f"false negative indistinguishable from a real finding."
        )
    if not _ACCOUNT_KEY_RE.match(key):
        raise DiagnosticError(f"{key!r} is not a valid account-key slug")
    return key.lower()


def _now(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        return datetime.fromisoformat(now.replace("Z", "+00:00"))
    return now


def _parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def next_scheduled_sync(after, daily_hour_utc):
    """The next time the nightly gym-media sync runs at or after `after`.

    The sync shares the daily slot at AGENT_DAILY_HOUR_UTC. This is computed, not
    assumed: Case 1 turns entirely on whether that run has happened yet, and
    "probably just needs to wait" is not a fact.
    """
    hour = int(daily_hour_utc)
    candidate = after.astimezone(timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# Case 1 shape: "the posts waiting for my approval have no photos"
# ---------------------------------------------------------------------------
def diagnose_drive_photos(gym_key, *, store=None, now=None, daily_hour_utc=None,
                          lane_active_for=None):
    """Read-only. Answers, with facts rather than a guess:

      * is the Connect-Drive lane even armed for this gym
      * does an active, un-revoked gym_drive source exist
      * how many media_asset rows that gym actually has
      * how long ago the connection happened
      * whether the next scheduled sync has ALREADY run since the connect

    That last pair is what separates "it just needs to wait for tonight's run" from
    "the run happened and produced nothing", which are opposite diagnoses with
    opposite remedies. A revoked share, an inactive source and an unarmed lane are
    each surfaced as their own fact, so none of them can hide behind "0 assets".
    """
    key = require_account_key(gym_key)
    now_dt = _now(now)

    # THE PRODUCTION DEFAULT. `store` was a required keyword-only argument with no
    # default, and nothing on the real path supplies one: runner.py calls run_once()
    # with no deps, consumer passes deps={}, and _diagnostic_kwargs therefore passes
    # nothing. Every Case-1 ticket -- the ONLY executable remedy this capability has --
    # died on `TypeError: missing 1 required keyword-only argument: 'store'` and
    # escalated with that on a card, which looks exactly like a healthy refusal.
    #
    # D68's "built but not wired", and this module's own docstring claimed the
    # opposite ("Every one of them has a real production default, so a test double is
    # never the only implementation of a seam"). Every test injected a fake store, so
    # nothing could catch it. tests/test_client_dm_wiring.py now calls every diagnostic
    # and every executor with NO deps at all and asserts the failure is never a
    # TypeError about a missing seam.
    if store is None:
        from .. import gym_media_index as _idx
        store = _idx.default_store()

    if daily_hour_utc is None:
        # The nightly gym-media sync shares the daily slot at AGENT_DAILY_HOUR_UTC
        # (agent/runner.py's run_daily). Read straight from the env, the same way the
        # scheduler does. (An earlier version imported config here and immediately
        # deleted it "to keep the dependency explicit" -- a comment describing an
        # effect the code did not have.)
        daily_hour_utc = int(os.environ.get("AGENT_DAILY_HOUR_UTC", "12"))

    if lane_active_for is None:
        from .. import config as _cfg
        lane_active_for = _cfg.gym_drive_connect_active_for

    # SECOND, INDEPENDENT TENANT CHECK. The store is asked to filter by gym_id, and
    # then every row it returns is re-checked here. The executor already did this; the
    # DIAGNOSTIC did not, and the diagnostic is where every fact in a reply comes from
    # -- so a broken server-side filter could put ANOTHER GYM'S folder state into a
    # message auto-sent to this client (the drive_revoked path takes no executor, so
    # the executor's guard never ran to catch it). Two independent controls must now
    # both fail for one gym to see another's data.
    sources = [
        s for s in store.list_sources(gym_id=key, include_inactive=True)
        if str(s.get("kind") or "gym_drive") == "gym_drive"
        and str(s.get("gym_id") or "") == key
    ]
    active = [s for s in sources if s.get("active")]
    # ONE SOURCE, OR NONE OF THIS LANE'S BUSINESS.
    #
    # This used to be `(active or sources or [None])[0]` -- the newest by connected_at,
    # while media_asset_count summed assets across ALL sources. The schema permits
    # several active sources per gym (media_source_gym_idx is on (gym_id, active); only
    # folder_id is globally unique) and gym_media_routes binds a second folder without
    # deactivating the first. Newest-revoked + older-healthy therefore auto-told a gym
    # with a working folder and a full library that "Your connected Google Drive folder
    # is no longer shared with us" -- false, singular, and unactionable, since the
    # folder name is deliberately excluded as client-controlled so they cannot tell
    # which one is meant.
    #
    # Every reply this lane can compose speaks of "your folder", singular. Rather than
    # pluralise copy nobody has reviewed, a gym with more than one active source is
    # reported as such and the remedy planner refuses it, so a human handles it.
    multi = len(active) > 1
    chosen = (active or sources or [None])[0]

    # ...and the same on the asset side, so a leaked list cannot inflate a count that
    # a reply then states as this gym's.
    assets = [a for a in (store.list_assets(key) if sources else [])
              if str(a.get("gym_id") or "") == key]

    connected_at = _parse_ts(chosen.get("connected_at")) if chosen else None
    if connected_at is None:
        hours_since = -1.0
        elapsed = False
        next_run = next_scheduled_sync(now_dt, daily_hour_utc)
    else:
        hours_since = round((now_dt - connected_at).total_seconds() / 3600.0, 2)
        next_run = next_scheduled_sync(connected_at, daily_hour_utc)
        elapsed = next_run <= now_dt

    return _facts.GroundingSnapshot.build(
        DIAG_DRIVE_PHOTOS,
        "diagnosis",
        key,
        {
            "drive_lane_active_for_gym": bool(lane_active_for(key)),
            "media_source_multiple_active": bool(multi),
            "media_source_present": bool(chosen),
            "media_source_active": bool(chosen and chosen.get("active")),
            "media_source_revoked": bool(chosen and chosen.get("revoked_externally")),
            "media_source_folder_name": str((chosen or {}).get("folder_name") or ""),
            "media_asset_count": int(len(assets)),
            "hours_since_connect": float(hours_since),
            "next_scheduled_sync_utc": next_run.isoformat(),
            "scheduled_sync_elapsed": bool(elapsed),
        },
    )


# ---------------------------------------------------------------------------
# Case 2 shape: "my posts have no call to action"
# ---------------------------------------------------------------------------
_CTA_HEADING_RE = re.compile(r"^#{2,4}\s*CTA rotation", re.I | re.M)

# THE POOL IS COUNTED BY THE EXTRACTOR THE CAPTION PIPELINE ACTUALLY USES.
#
# This diagnostic used to reimplement CTA parsing: `>\s*TODO` searched ANYWHERE in the
# section, and the count was hard-zeroed from that same boolean. So a gym with three
# real CTAs and one leftover "> TODO: add two more before spring" was auto-told that
# its CTA section "is still the blank placeholder from onboarding", that it had 0 CTAs,
# and that this was why its posts had none -- four claims, all false, because
# drafter.py appends one of those three to every caption. Leaving a note beside real
# copy is ordinary editing, and no fixture covered it because every fixture used
# bible_drafter's exact string.
#
# The byte-identity reply gate cannot catch this: it proves every SLOT traces to a fact
# key, never that the fact key measures what its name says. So the fix is not a better
# regex here -- it is to stop having a second implementation at all. agent.voice's
# _extract_ctas is what the drafter consumes, so counting with it means the fact the
# client is told is the fact their posts actually run on.
def _extract_ctas(text):
    from .. import voice as _voice
    return _voice._extract_ctas(text)              # noqa: SLF001 - one implementation


# The section is the untouched onboarding scaffold only when it contains NO usable CTA
# and does carry a placeholder marker. Both writers are covered: bible_drafter emits
# "> TODO: ..." and onboard.py emits a bare "TODO: ...".
_PLACEHOLDER_RE = re.compile(r"^\s*>?\s*TODO\b", re.I | re.M)


def diagnose_cta_pool(gym_key, *, voice_dir=None, read_text=None):
    """Read-only. NEVER writes. Answers whether the gym's CTA rotation section is a
    real pool or the unfilled onboarding TODO.

    There is deliberately no remedy that writes this file (scope_gate blocks every
    brand_voice/ path). A CTA is client-specific content — a real booking link, phone
    number or offer — and inventing one would break the repo's hardest rule
    (CLAUDE.md: "Client content only. No invented facts, offers, prices, or stats").
    So the honest outcome of this diagnostic is a QUESTION, not a fix.
    """
    key = require_account_key(gym_key)
    if voice_dir is None:
        from .. import config as _cfg
        voice_dir = _cfg.client_voice_dir()
    path = os.path.join(voice_dir, key, "lasso_voice.md")

    if read_text is not None:
        text = read_text(path)
    else:
        try:
            text = open(path, "r", encoding="utf-8").read()
        except OSError:
            text = None

    if text is None:
        return _facts.GroundingSnapshot.build(
            DIAG_CTA_POOL, "diagnosis", key,
            {"voice_doc_present": False, "cta_section_present": False,
             "cta_section_is_todo": False, "cta_pool_count": 0},
        )

    m = _CTA_HEADING_RE.search(text)
    if not m:
        return _facts.GroundingSnapshot.build(
            DIAG_CTA_POOL, "diagnosis", key,
            {"voice_doc_present": True, "cta_section_present": False,
             "cta_section_is_todo": False, "cta_pool_count": 0},
        )

    tail = text[m.end():]
    nxt = re.search(r"^#{2,4}\s+", tail, re.M)
    section = tail[: nxt.start()] if nxt else tail

    # The count comes from the pipeline's own extractor, over the whole doc, because
    # that is exactly what the drafter will find at caption time.
    pool = len(_extract_ctas(text))
    # "Still the onboarding placeholder" means BOTH: a placeholder marker is present
    # AND the extractor finds nothing usable. A TODO note sitting beside real CTAs is
    # not a blank section, and must never be described as one.
    is_todo = bool(_PLACEHOLDER_RE.search(section)) and pool == 0

    return _facts.GroundingSnapshot.build(
        DIAG_CTA_POOL, "diagnosis", key,
        {"voice_doc_present": True, "cta_section_present": True,
         "cta_section_is_todo": is_todo, "cta_pool_count": int(pool)},
    )


def run(diagnostic_id, gym_key, *, stage="diagnosis", **kw):
    """Dispatch by id. An id outside ALL_DIAGNOSTICS raises rather than defaulting to
    anything — there is no 'general' diagnostic, by design."""
    if diagnostic_id == DIAG_DRIVE_PHOTOS:
        snap = diagnose_drive_photos(gym_key, **kw)
    elif diagnostic_id == DIAG_CTA_POOL:
        snap = diagnose_cta_pool(gym_key, **kw)
    else:
        raise DiagnosticError(
            f"unknown diagnostic {diagnostic_id!r}; the set is {sorted(ALL_DIAGNOSTICS)}"
        )
    if stage == "diagnosis":
        return snap
    return _facts.GroundingSnapshot.build(
        snap.diagnostic_id, stage, snap.gym_key, dict(snap.facts)
    )
