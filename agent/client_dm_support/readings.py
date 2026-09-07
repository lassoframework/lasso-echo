"""
readings.py — THE CLOSED SET: one object per fact, holding the measurement AND the
only sentence Echo may say about it.

WHY THIS FILE IS SHAPED LIKE THIS (read before adding a Reading).

The previous build of this capability oscillated D->B->B->C->B->C->B across seven
audit rounds. Its reply gate was sound: a reply had to be a byte-identical render of
a registered template over an enumerated set of fact keys, so novel input failed
closed. No round after the first found an escape from that gate.

What the later rounds DID find, over and over, was that the closed set was closed
over the KEYS and not over their MEANING, because a fact was spread across three
registries joined by name:

    diagnostics.py produced   cta_section_is_todo
    facts.py       enumerated cta_section_is_todo   ("the section is the unfilled TODO")
    reply.py       claimed    "still the blank placeholder from onboarding"

Three files, one name, and nothing making them agree. Round 7 found a gym with three
working CTAs and one leftover "> TODO: add two more" note that was auto-told all four
claims were true. Round 6 found a template sentence ("New posts will draw from those")
that no fact key measured at all -- constant prose the byte-identity gate structurally
cannot see, because the reconstruction contains the same words. The residual pass
found the section DETECTOR was wider than the section EXTRACTOR, so the two disagreed
about the same document.

Every one of those is the same defect: a name, a measurement and a claim that can
drift apart because they live in different places.

So this file makes them ONE OBJECT. A Reading carries:

  * `measure`  -- prose saying exactly what is counted or tested, and by WHICH
                  production reader;
  * `producer` -- the dotted name of that production function, asserted importable by
                  a test with no test double (D68: "name the PRODUCER of the value it
                  gates on, and assert that producer exists");
  * `say`      -- the ONLY client-facing sentence about this reading, or "" for a
                  reading Echo may use to decide but never to speak;
  * `say_when` -- the values for which `say` is truthful. Anything else REFUSES.

There is nowhere in this capability to type a client-facing sentence that is not
attached to a measurement. That is what closes round 6's hole structurally rather
than by asking the next author to write prose carefully.

AND THE SECOND RULE, which closes round 7's: a Reading may not re-implement a reading
another module already performs. `cta_pool_count` is whatever
`agent.voice._extract_ctas` returns, because that is the extractor the caption
pipeline actually consumes -- so the number the client is told is the number their
posts run on, and there is no second implementation to disagree with. Enforced by
tests/test_client_dm_readings.py::test_package_defines_no_parser_of_its_own.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import MappingProxyType

# say_when sentinel: this reading's sentence is truthful for ANY value it can hold.
# Only legitimate for a reading whose sentence interpolates the value itself.
ANY = "__any_value__"


class Ungrounded(ValueError):
    """A value was asked to become a sentence it does not support, or a key outside
    the closed set was offered. Always escalates; never softens into a partial reply."""


@dataclass(frozen=True)
class Reading:
    key: str
    type: type                      # bool or int. Nothing structured: see rule below.
    measure: str                    # what is counted/tested, and by which reader
    producer: str                   # dotted path of the production function
    say: str = ""                   # the ONLY sentence about this reading
    say_when: object = ()           # tuple of literal values, or ANY

    def speakable(self):
        return bool(self.say)

    def sentence(self, value):
        """The one sentence, or refuse. Never returns a hedge or a partial."""
        if not self.say:
            raise Ungrounded(
                f"reading {self.key!r} has no client-facing sentence; it may inform a "
                f"decision but Echo may not speak it"
            )
        self.check_value(value)
        if self.say_when is not ANY:
            ok = any(type(v) is type(value) and v == value for v in self.say_when)
            if not ok:
                raise Ungrounded(
                    f"reading {self.key!r} measured {value!r}, which is not in its "
                    f"say_when {self.say_when!r}; its sentence would not be true"
                )
        return self.say.format(v=value) if "{v}" in self.say else self.say

    def check_value(self, value):
        # bool is a subclass of int, so an int reading must not silently accept True.
        if self.type is int and isinstance(value, bool):
            raise Ungrounded(f"reading {self.key!r} is an int; got bool {value!r}")
        if not isinstance(value, self.type):
            raise Ungrounded(
                f"reading {self.key!r} is {self.type.__name__}; got "
                f"{type(value).__name__} {value!r}"
            )
        return value


# ---------------------------------------------------------------------------
# THE REGISTRY. Adding an entry widens what Echo may measure AND say unattended,
# in one place, so it belongs in a review.
#
# Every sentence below is pinned BYTE-FOR-BYTE by
# tests/test_client_dm_readings.py::test_every_sentence_echo_can_say_is_pinned.
# That test is a two-way guard (D68): editing a sentence fails it, and deleting one
# fails it too. The complete set of things this capability can say to a paying client
# is therefore reviewable in a single screen, and cannot grow silently.
# ---------------------------------------------------------------------------
_REGISTRY = {}


def _reg(r):
    if r.key in _REGISTRY:
        raise ValueError(f"duplicate reading {r.key!r}")
    if r.say and r.say_when == ():
        raise ValueError(
            f"reading {r.key!r} has a sentence but an empty say_when, so it could "
            f"never be spoken; use ANY or name the truthful values"
        )
    _REGISTRY[r.key] = r
    return r


# --- Drive media lane ----------------------------------------------------------
DRIVE_ACTIVE_SOURCES = _reg(Reading(
    key="drive_active_sources",
    type=int,
    measure=("count of media_source rows with kind='gym_drive' and active=true whose "
             "gym_id equals this gym's Echo account key, via "
             "media_source_store.list_sources(gym_id)"),
    producer="agent.gym_media_index.default_store",
    # DELIBERATELY UNSPEAKABLE. Every sentence this lane can say about Drive is
    # singular ("your folder"); a gym with 0 or 2+ active sources has no true singular
    # sentence, and pluralising copy nobody has reviewed is how round 7's MAJOR-3
    # happened. train7164ae502 really does have six active sources today
    # (measured live 2026-09-07), so this is not a hypothetical.
    say="",
))

DRIVE_SOURCE_REVOKED = _reg(Reading(
    key="drive_source_revoked",
    type=bool,
    measure=("media_source.revoked_externally on this gym's single active gym_drive "
             "source; set by jobs.sync_gym_media.sync_source on a Drive 403/404"),
    producer="agent.gym_media_index.default_store",
    say=("Your gym's Google Drive folder is no longer shared with Echo, so the photo "
         "sync cannot read it."),
    say_when=(True,),
))

DRIVE_IDENTITY_SPLIT = _reg(Reading(
    key="drive_identity_split",
    type=bool,
    measure=("true when this gym's media rows disagree about which account key owns "
             "them: an active source whose gym_id is not this key, or an asset under "
             "this key whose source_id belongs to no source of this key"),
    producer="agent.gym_media_index.default_store",
    # UNSPEAKABLE ON PURPOSE. A split is an internal data defect; it is a human's
    # problem, not a sentence for the gym owner.
    say="",
))

DRIVE_LIBRARY_USABLE = _reg(Reading(
    key="drive_library_usable",
    type=int,
    measure=("media_asset rows for this gym that gym_media_selector.is_usable accepts "
             "-- eligible IS TRUE and not excluded_by_coach. NOT a raw row count: "
             "counting every row is what told a client 'posts will draw from those' "
             "over six unprobed videos the selector rejects (round 6). Live "
             "2026-09-07: toughtemple52040e holds 70 rows of which 58 are usable."),
    producer="agent.gym_media_selector.is_usable",
    say="Your media library has {v} file(s) Echo can post.",
    say_when=ANY,
))

DRIVE_SYNC_RAN = _reg(Reading(
    key="drive_sync_ran",
    type=bool,
    measure=("true only when actions.gym_drive_sync actually called "
             "jobs.sync_gym_media.sync_source and it returned ok"),
    producer="agent.jobs.sync_gym_media.sync_source",
    say="I ran your photo sync just now.",
    say_when=(True,),
))

DRIVE_FILES_ADDED = _reg(Reading(
    key="drive_files_added",
    type=int,
    measure="the 'inserted' count returned by jobs.sync_gym_media.sync_source",
    producer="agent.jobs.sync_gym_media.sync_source",
    say="It added {v} new file(s).",
    say_when=ANY,
))

# --- Brand-voice CTA lane ------------------------------------------------------
VOICE_DOC_PRESENT = _reg(Reading(
    key="voice_doc_present",
    type=bool,
    measure=("<config.client_voice_dir()>/<gym key>/lasso_voice.md exists and is "
             "readable"),
    producer="agent.config.client_voice_dir",
    say="",
))

CTA_POOL_COUNT = _reg(Reading(
    key="cta_pool_count",
    type=int,
    measure=("len(agent.voice._extract_ctas(doc)) -- the SAME extractor the caption "
             "pipeline consumes, so the number the client is told is the number their "
             "posts actually run on. Round 7 shipped a second implementation of this "
             "and the two disagreed on a real document."),
    producer="agent.voice._extract_ctas",
    say=("Your brand voice doc has {v} call(s) to action for Echo to rotate through, "
         "which is why your posts are going out without one."),
    say_when=(0,),
))

READINGS = MappingProxyType(_REGISTRY)
ALL_KEYS = frozenset(READINGS)


# ---------------------------------------------------------------------------
# ASKS. The other half of what Echo may say, and the ONLY half that is not a
# measurement: a question back to the client. Kept as its own tiny closed set rather
# than smuggled into a Reading's sentence, because an ask makes no factual claim and
# must never be confused with one. Pinned byte-for-byte by the same test.
#
# An ask exists so the "there is no fix, only a question" path has somewhere to land.
# CLAUDE.md's hardest rule -- client content only, no invented facts, offers, prices
# or stats -- means a missing CTA can only ever be asked about, never written.
# ---------------------------------------------------------------------------
ASKS = MappingProxyType({
    "cta_needed": (
        "I cannot write one for you, because a call to action has to be your real "
        "booking link, phone number or offer. What would you like your posts to ask "
        "people to do?"
    ),
    "drive_reshare": (
        "Re-sharing that folder with Echo in the portal will let the sync resume."
    ),
})


# ---------------------------------------------------------------------------
# The snapshot.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Snapshot:
    """One probe's typed output, plus which probe and which stage produced it.

    `stage` is 'diagnosis' or 'verification'. A verification snapshot may only be
    compared against a diagnosis snapshot from the SAME probe, so "verified" can never
    mean "a different, friendlier query agreed with me".
    """

    probe_id: str
    stage: str
    gym_key: str
    values: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))

    @staticmethod
    def build(probe_id, stage, gym_key, values):
        if stage not in ("diagnosis", "verification"):
            raise Ungrounded(f"unknown snapshot stage {stage!r}")
        clean = {}
        for k, v in dict(values or {}).items():
            r = READINGS.get(k)
            if r is None:
                raise Ungrounded(
                    f"{k!r} is not a registered Reading. A probe may only emit keys in "
                    f"readings.READINGS; widen it deliberately, in a review, or the "
                    f"closed set means nothing."
                )
            r.check_value(v)
            clean[k] = v
        return Snapshot(str(probe_id), stage, str(gym_key), MappingProxyType(clean))

    def __contains__(self, key):
        return key in self.values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def require(self, key):
        if key not in self.values:
            raise Ungrounded(
                f"snapshot from {self.probe_id!r} does not carry reading {key!r}"
            )
        return self.values[key]

    def with_run_values(self, extra):
        """A snapshot carrying the probe's readings plus what the action reported.
        Same probe, same gym, same stage -- an action's own readings are not a second
        opinion about the gym, they are a record of what Echo did."""
        merged = dict(self.values)
        merged.update(dict(extra or {}))
        return Snapshot.build(self.probe_id, self.stage, self.gym_key, merged)


def assert_producers_exist():
    """D68's cheap static check, applied here: every Reading names the PRODUCER of its
    value, and that producer must actually exist -- resolved by real import, with no
    test double and no live service. A Reading whose producer was deleted or renamed is
    a capability that will fail at 3am with a TypeError and card a human, which is
    byte-indistinguishable from a healthy refusal.
    """
    missing = []
    for r in READINGS.values():
        mod_name, _, attr = r.producer.rpartition(".")
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{r.key}: cannot import {mod_name} ({type(e).__name__})")
            continue
        if not hasattr(mod, attr):
            missing.append(f"{r.key}: {mod_name} has no {attr!r}")
    if missing:
        raise Ungrounded(
            "readings name producers that do not exist: " + "; ".join(missing)
        )
    return True
