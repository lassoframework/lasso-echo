"""
story_music.py — the LICENSED music bed engine for Story Studio (spec §3).

HONESTY (read this before assuming this attaches trending IG audio):
  The Instagram Graph API CANNOT attach native / trending IG audio to a published
  reel or story. Trending audio is a MANUAL in-app step only. So this engine does
  NOT attach trending audio. It selects a LICENSED music track (an Artlist /
  Soundstripe-class library covered by one LASSO license) and BURNS it into the
  rendered video as an audio bed. Licensed libraries sell chart-STYLE tracks (big
  drums / drops / hooks, 120+ BPM) — never the actual chart record (never the real
  Drake song). Curation of the library is a standing monthly ops job.

Shelves (spec §3):
  * hype  — the DEFAULT for every template. High energy, chart-STYLE. Blake's rule.
  * chill — an EXPLICIT opt-out a coach must pick on the card. A template can NEVER
            default to chill (enforced: default_shelf() coerces chill -> hype).
  * none  — no bed. Carries NO track_id and NO license_ref.

Every bed render stores track_id + license_ref on the story_render row. The actual
licensed audio files are an OPS ASSET (dropped into the bucket by the monthly
curation job); this module ships a small CONFIG-DRIVEN library interface with an
INJECTABLE library so the selection + storage + honesty rails run offline in tests.
The audio file is resolved lazily at burn time; a missing file HOLDS the render with
an honest reason (never a silent no-audio post, never a fabricated track).
"""
from __future__ import annotations

from dataclasses import dataclass

SHELF_HYPE = "hype"
SHELF_CHILL = "chill"
SHELF_NONE = "none"

VALID_SHELVES = (SHELF_HYPE, SHELF_CHILL, SHELF_NONE)

# The honest, standing statement shown on the approval card whenever a bed is used.
LICENSED_BED_NOTE = (
    "Music is a LICENSED chart-style bed burned into the video (not trending IG "
    "audio; the IG API cannot attach that). Trending audio stays a manual in-app "
    "step.")


@dataclass
class Track:
    """One licensed library track. `path` is resolved lazily at burn time (an ops
    asset); the metadata below is enough to select + store the honesty fields."""
    track_id: str
    license_ref: str            # the LASSO library license reference (e.g. 'artlist:LIC-2026')
    shelf: str                  # hype | chill
    title: str = ""
    bpm: int = 0
    path: str = ""              # local/bucket path, filled by the library at resolve time


@dataclass
class MusicSelection:
    """The result of a music pick for one render."""
    shelf: str
    track_id: str = ""
    license_ref: str = ""
    title: str = ""
    note: str = ""              # the honesty note (empty for shelf 'none')
    held: bool = False          # True when the shelf wanted a bed but none is available
    hold_reason: str = ""


def normalize_shelf(shelf) -> str:
    """Coerce a requested shelf to a valid value. An unknown value -> hype (the safe
    high-energy default). NOTE: this preserves an explicit 'chill' (a coach opt-out);
    the no-chill-DEFAULT rail lives in default_shelf, not here."""
    s = str(shelf or "").strip().lower()
    return s if s in VALID_SHELVES else SHELF_HYPE


def default_shelf() -> str:
    """The DEFAULT shelf when a template / card does not name one. Reads
    config.story_studio_music_shelf() (hype or none) and can NEVER be chill: a
    template defaulting to chill is coerced to hype here (spec §3, Blake's rule)."""
    from . import config
    s = config.story_studio_music_shelf()
    return SHELF_NONE if s == SHELF_NONE else SHELF_HYPE


# ---- the config-driven library interface (injectable) -----------------------
class StubMusicLibrary:
    """A tiny offline library so tests + a fresh deploy have a deterministic pick
    without the ops audio assets present. In production an ops library implementation
    is injected (same interface) that resolves real licensed files from the bucket.

    A shelf with NO tracks returns None from pick(), which makes the engine HOLD the
    render with an honest reason instead of shipping a silent, bed-less post."""

    def __init__(self, tracks=None):
        # Default catalogue: one hype + one chill track, metadata only (no real audio
        # file — `path` is empty until an ops library resolves it). The engine still
        # stores track_id + license_ref; the burn step is what needs the file.
        self._tracks = tracks if tracks is not None else [
            Track(track_id="hype_001", license_ref="lasso-lib:LIC-HYPE-001",
                  shelf=SHELF_HYPE, title="Chart-Style High Energy 01", bpm=128),
            Track(track_id="chill_001", license_ref="lasso-lib:LIC-CHILL-001",
                  shelf=SHELF_CHILL, title="Chill Opt-Out 01", bpm=92),
        ]

    def pick(self, shelf, *, seed=None):
        """The next track for a shelf, or None when the shelf is empty. Deterministic
        by `seed` (e.g. the request id) so a re-render picks the same bed."""
        pool = [t for t in self._tracks if t.shelf == shelf]
        if not pool:
            return None
        if seed is None:
            return pool[0]
        return pool[hash(str(seed)) % len(pool)]

    def resolve_path(self, track):
        """The local/bucket path of the track's audio file, or '' when the ops asset
        is not present (the stub has none)."""
        return track.path or ""


_DEFAULT_LIBRARY = None


def default_library():
    global _DEFAULT_LIBRARY
    if _DEFAULT_LIBRARY is None:
        _DEFAULT_LIBRARY = StubMusicLibrary()
    return _DEFAULT_LIBRARY


# ---- the engine -------------------------------------------------------------
def select(shelf=None, *, library=None, seed=None):
    """Pick a bed for ONE render and return a MusicSelection with the honesty fields.

    shelf: the requested shelf. None -> default_shelf() (hype or none, NEVER chill).
    An explicit 'chill' is honored (the coach opt-out). 'none' carries neither
    track_id nor license_ref and no note.

    A shelf that wants a bed (hype/chill) but whose library is empty returns a HELD
    selection (held=True, hold_reason set) — the caller HOLDS the render with an
    honest reason rather than shipping a silent bed-less post or fabricating a track.
    """
    lib = library or default_library()
    requested = default_shelf() if shelf is None else normalize_shelf(shelf)

    if requested == SHELF_NONE:
        return MusicSelection(shelf=SHELF_NONE)  # no track_id, no license_ref, no note

    track = lib.pick(requested, seed=seed)
    if track is None:
        return MusicSelection(
            shelf=requested, held=True,
            hold_reason=(f"no licensed track available on the '{requested}' shelf "
                         f"(the ops music library has not been curated for it). The "
                         f"render is HELD; a coach can pick 'none' to ship without a "
                         f"bed."),
            note=LICENSED_BED_NOTE)
    return MusicSelection(
        shelf=requested, track_id=track.track_id, license_ref=track.license_ref,
        title=track.title, note=LICENSED_BED_NOTE)


def audio_path_for(selection, *, library=None):
    """The local/bucket audio path to burn for a selection, or '' when there is no bed
    ('none') OR the ops asset is not present. An empty path on a hype/chill selection
    means the burn step must HOLD (never post silently)."""
    if selection.shelf == SHELF_NONE or not selection.track_id:
        return ""
    lib = library or default_library()
    track = Track(track_id=selection.track_id, license_ref=selection.license_ref,
                  shelf=selection.shelf, title=selection.title)
    return lib.resolve_path(track)
