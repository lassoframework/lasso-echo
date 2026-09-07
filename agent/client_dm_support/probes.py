"""
probes.py — the read-only measurements behind readings.py, with PRODUCTION DEFAULTS.

EVERY SEAM HERE HAS A REAL DEFAULT, AND A TEST CALLS IT WITH NO FAKE.

The single most expensive defect of the previous build sat here. `store` was a
required keyword-only argument with no default; runner called run_once() with no deps;
the flagship remedy therefore died in production on

    TypeError: diagnose_drive_photos() missing 1 required keyword-only argument

and escalated with that written on a card -- byte-indistinguishable from a healthy
refusal. SIX independent audit rounds missed it, because every test in the suite
injected its own working fake into the exact seam production left empty, and the
module's own docstring asserted the opposite.

So, two rules, both asserted by tests/test_client_dm_no_fakes.py:

  1. every injectable in this file has a real production default, and
  2. at least one test per probe calls it with NO arguments beyond the gym key, and
     asserts the NAMED PRODUCER was actually invoked -- not merely that nothing
     raised, because a default of None satisfies that while leaving the seam unfilled.

THE JOIN KEY, WHICH IS EASY TO GET WRONG AND SILENT WHEN YOU DO.
`media_source.gym_id` and `media_asset.gym_id` hold the ECHO ACCOUNT-KEY SLUG (e.g.
'crossfitlocal'), NOT the portal's gym uuid. Pass a uuid and every query returns zero
rows and the diagnosis reads "nothing is connected" -- a false negative that looks
exactly like a real finding. require_account_key() REFUSES a uuid rather than querying
with it.

WHAT LIVE DATA SAYS ABOUT THAT KEY (measured 2026-09-07 against the production
project, read-only). Two of seventeen connected gyms have media rows that DISAGREE
about which key owns them:

    media_source.gym_id      media_asset.gym_id        rows
    toughtemple086f51    ->  toughtemple52040e          70
    crossfitsunnyside2616ac  crossfitsunnysidef574c0    13

That is the documented account-key split-brain landing squarely on this lane's
flagship fact. Asked "why do my posts have no photos?", a gym-id-keyed probe for
`toughtemple52040e` finds NO media_source at all while counting 70 assets. So
`drive_identity_split` is measured and every condition refuses on it.

HONEST LIMIT OF THAT DETECTION. `media_source_store.list_sources(gym_id=...)` and
`list_assets(gym_id)` both filter server-side by gym_id -- list_assets REQUIRES one,
for tenant isolation. So this probe can see assets that belong to no source of this
key (the 52040e side, which is the side the portal hands us), and it cannot see a
source under a *different* key with no assets of its own (the 086f51 side). That
second case is stated rather than papered over: it presents as "zero active sources",
which no condition matches, so it escalates anyway.
"""
from __future__ import annotations

import os
import re

from . import readings as _r

PROBE_DRIVE = "drive_media"
PROBE_CTA = "cta_pool"

# A portal gym id. Never a media_source.gym_id.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
# The shape an account key actually has in the shared plane.
_ACCOUNT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

DRIVE_SOURCE_KIND = "gym_drive"
VOICE_DOC_BASENAME = "lasso_voice.md"


class ProbeError(RuntimeError):
    """A probe could not be run at all. Always escalates."""


def require_account_key(gym_key):
    """The ONE join key these tables accept. Raises on a portal uuid or on anything
    that is not a plain slug, rather than running a query that would silently return
    nothing and read as a finding."""
    key = str(gym_key or "").strip()
    if not key:
        raise ProbeError("no gym key given")
    if _UUID_RE.match(key):
        raise ProbeError(
            f"{key!r} is a portal gym uuid. media_source.gym_id / media_asset.gym_id "
            f"are keyed by the Echo ACCOUNT-KEY SLUG (e.g. 'crossfitlocal'). Querying "
            f"with the uuid returns zero rows and reads as 'nothing connected' -- a "
            f"false negative indistinguishable from a real finding."
        )
    if not _ACCOUNT_KEY_RE.match(key):
        raise ProbeError(f"{key!r} is not a valid account-key slug")
    return key.lower()


# ---------------------------------------------------------------------------
# Production defaults. Named here, imported lazily so this module stays importable
# in a bare test process, and asserted by assert_producers_exist().
# ---------------------------------------------------------------------------
def default_media_store():
    from .. import gym_media_index as _idx
    return _idx.default_store()


def default_voice_dir():
    from .. import config as _config
    return _config.client_voice_dir()


def default_read_text(path):
    """None when the file is not there. Anything else is an error worth escalating."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        return None


def probe_drive(gym_key, *, stage="diagnosis", store=None):
    """Read-only. Every value is a count or a boolean this code computed itself."""
    key = require_account_key(gym_key)
    store = store if store is not None else default_media_store()

    sources = list(store.list_sources(gym_id=key, include_inactive=True) or [])
    drive_sources = [s for s in sources
                     if str(s.get("kind") or "") == DRIVE_SOURCE_KIND]
    active = [s for s in drive_sources if bool(s.get("active"))]

    assets = list(store.list_assets(key) or [])

    # TENANT re-assertion (defence in depth, same rule gym_media_selector applies):
    # never trust a row whose gym_id is not this gym, even though the store filtered.
    foreign = [a for a in assets if str(a.get("gym_id") or "") != key]
    own = [a for a in assets if str(a.get("gym_id") or "") == key]

    # An asset attributed to this key whose source belongs to no source of this key is
    # the live split-brain shape (toughtemple52040e: 70 assets, zero sources).
    known_source_ids = {str(s.get("id")) for s in sources if s.get("id")}
    orphaned = [a for a in own
                if a.get("source_id") and str(a.get("source_id")) not in known_source_ids]

    from .. import gym_media_selector as _sel
    usable = sum(1 for a in own if _sel.is_usable(a))

    values = {
        _r.DRIVE_ACTIVE_SOURCES.key: len(active),
        _r.DRIVE_SOURCE_REVOKED.key: any(bool(s.get("revoked_externally"))
                                         for s in active),
        _r.DRIVE_IDENTITY_SPLIT.key: bool(foreign or orphaned),
        _r.DRIVE_LIBRARY_USABLE.key: int(usable),
    }
    return _r.Snapshot.build(PROBE_DRIVE, stage, key, values)


def probe_cta(gym_key, *, stage="diagnosis", voice_dir=None, read_text=None):
    """Read-only. The CTA count is whatever the CAPTION PIPELINE's own extractor
    returns for this gym's doc -- never a second parse of the same file."""
    key = require_account_key(gym_key)
    voice_dir = voice_dir if voice_dir is not None else default_voice_dir()
    read_text = read_text if read_text is not None else default_read_text

    path = os.path.join(str(voice_dir), key, VOICE_DOC_BASENAME)
    raw = read_text(path)
    present = raw is not None
    if present:
        from .. import voice as _voice
        count = len(_voice._extract_ctas(raw) or [])   # noqa: SLF001 - the canonical reader
    else:
        count = 0

    return _r.Snapshot.build(PROBE_CTA, stage, key, {
        _r.VOICE_DOC_PRESENT.key: bool(present),
        _r.CTA_POOL_COUNT.key: int(count),
    })


PROBES = {
    PROBE_DRIVE: probe_drive,
    PROBE_CTA: probe_cta,
}
ALL_PROBES = frozenset(PROBES)


def run(probe_id, gym_key, *, stage="diagnosis", **kw):
    fn = PROBES.get(probe_id)
    if fn is None:
        raise ProbeError(f"no registered probe {probe_id!r}")
    return fn(gym_key, stage=stage, **kw)
