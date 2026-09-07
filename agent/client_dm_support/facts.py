"""
facts.py — the CLOSED set of fact keys, and the snapshot type that carries them.

D68: "auto-send only when the reply is a restatement of an enumerated set of fact
keys from the grounding snapshot ... and contains nothing else." That sentence only
means something if the set of fact keys is a constant in the codebase rather than
whatever a diagnostic happened to return. ALL_FACT_KEYS is that constant.

Two rules hold everywhere below:

  1. A diagnostic may only emit keys in ALL_FACT_KEYS. Emitting an unknown key is a
     programming error and raises -- it does NOT silently widen the gate. (The gate
     fails closed either way, because reply.py checks membership again; this raise
     exists so the failure is loud at the producer rather than mysterious at the
     consumer.)
  2. A fact value is a bool, an int, a float or a str. Never a dict, never a list,
     never a model's prose. A reply slot interpolates a fact value directly, so a
     value that could itself carry a sentence would be a hole straight through the
     grounding gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

# ---------------------------------------------------------------------------
# THE CLOSED SET.
#
# Every key a diagnostic may produce and every key a reply template may name.
# Adding a key here is a deliberate act: it widens what Echo is allowed to say
# unattended, so it belongs in a review, which is exactly why the list is a
# constant and not a computed thing.
# ---------------------------------------------------------------------------

# Drive-photos lane (Case 1 shape: "my posts have no photos").
DRIVE_FACT_KEYS = frozenset({
    "drive_lane_active_for_gym",     # bool  is the Connect-Drive lane armed for THIS gym
    "media_source_multiple_active",  # bool  MORE THAN ONE active source for this gym
    "media_source_present",          # bool  does a gym_drive media_source row exist
    "media_source_active",           # bool  media_source.active
    "media_source_revoked",          # bool  media_source.revoked_externally
    "media_source_folder_name",      # str   media_source.folder_name (the gym's own label)
    "media_asset_count",             # int   rows in media_asset for THIS gym
    "hours_since_connect",           # float now - media_source.connected_at
    "next_scheduled_sync_utc",       # str   ISO-8601 of the next AGENT_DAILY_HOUR_UTC run
    "scheduled_sync_elapsed",        # bool  has that run already happened since connect
})

# Brand-voice CTA lane (Case 2 shape: "my posts have no call to action").
CTA_FACT_KEYS = frozenset({
    "voice_doc_present",             # bool  the gym's lasso_voice.md exists
    "cta_section_present",           # bool  the CTA rotation heading exists in it
    "cta_section_is_todo",           # bool  the section is the unfilled intake TODO
    "cta_pool_count",                # int   how many real CTA lines are in the pool
})

# Facts that describe the run itself rather than the gym.
RUN_FACT_KEYS = frozenset({
    "assets_inserted_this_run",      # int   what the executed sync actually inserted
    "sync_ran",                      # bool  did the scoped fix actually execute
})

ALL_FACT_KEYS = frozenset() | DRIVE_FACT_KEYS | CTA_FACT_KEYS | RUN_FACT_KEYS

# A fact value may only be one of these. See rule 2 above.
_ALLOWED_VALUE_TYPES = (bool, int, float, str)


class FactError(ValueError):
    """A diagnostic tried to emit a key or a value the closed set does not allow."""


def _check_pair(key, value):
    if key not in ALL_FACT_KEYS:
        raise FactError(
            f"{key!r} is not in facts.ALL_FACT_KEYS. A diagnostic may only emit "
            f"enumerated fact keys (D68); widen ALL_FACT_KEYS deliberately, in a "
            f"review, or the grounding gate means nothing."
        )
    if not isinstance(value, _ALLOWED_VALUE_TYPES):
        raise FactError(
            f"fact {key!r} has value type {type(value).__name__}; only bool/int/"
            f"float/str are allowed. A structured value could smuggle a sentence "
            f"through a reply slot."
        )


@dataclass(frozen=True)
class GroundingSnapshot:
    """One diagnostic's typed output, plus where it came from.

    `stage` is 'diagnosis' or 'verification'. verify.py requires the two to have been
    produced by the SAME diagnostic id, so "verified" can never mean "a different,
    friendlier query agreed with me".
    """

    diagnostic_id: str
    stage: str
    gym_key: str
    facts: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))

    @staticmethod
    def build(diagnostic_id, stage, gym_key, facts):
        if stage not in ("diagnosis", "verification"):
            raise FactError(f"unknown snapshot stage {stage!r}")
        clean = {}
        for k, v in dict(facts or {}).items():
            _check_pair(k, v)
            clean[k] = v
        return GroundingSnapshot(
            diagnostic_id=str(diagnostic_id),
            stage=stage,
            gym_key=str(gym_key),
            facts=MappingProxyType(clean),
        )

    def __contains__(self, key):
        return key in self.facts

    def get(self, key, default=None):
        return self.facts.get(key, default)

    def keys(self):
        return self.facts.keys()

    def merged_with(self, other):
        """A snapshot carrying both sides' facts, for a reply that needs a diagnosis
        fact AND a run fact. Both must be about the same gym and the same diagnostic;
        a collision keeps the LATER (verification) value, because that is the one
        that was measured after the fix."""
        if other is None:
            return self
        if other.gym_key != self.gym_key:
            raise FactError(
                f"refusing to merge snapshots for different gyms "
                f"({self.gym_key!r} vs {other.gym_key!r})"
            )
        if other.diagnostic_id != self.diagnostic_id:
            raise FactError(
                f"refusing to merge snapshots from different diagnostics "
                f"({self.diagnostic_id!r} vs {other.diagnostic_id!r})"
            )
        combined = dict(self.facts)
        combined.update(dict(other.facts))
        return GroundingSnapshot.build(
            self.diagnostic_id, other.stage, self.gym_key, combined
        )
