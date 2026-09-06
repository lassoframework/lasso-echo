"""
reply.py — THE GROUNDED-REPLY GATE.

D68's conclusion, which this file implements literally:

    "auto-send only when the reply is a restatement of an enumerated set of fact keys
     from the grounding snapshot ... and contains nothing else — no sentence not
     traceable to one of those keys."

THE MECHANISM, and why it is stronger than checking sentences.
Rather than parsing a candidate reply and trying to decide whether each sentence is
traceable -- which would be an ENUMERATION OF AN OPEN SET, since the space of
sentences is open -- this gate inverts the direction:

    A reply may be auto-sent ONLY if it is BYTE-IDENTICAL to
    template.text.format(**{slot: snapshot[slot] for slot in template.slots})
    for a template registered in TEMPLATES.

So the reply is not checked; it is RECONSTRUCTED, and the candidate must equal the
reconstruction exactly. One extra clause, one softened hedge, one appended
"and we'll keep an eye on it" -- any of them changes a byte and the whole reply is
refused. There is no partial credit and no threshold. The input space is closed
(the finitely many templates x the finitely many fact keys), so anything novel has
nowhere to match and REFUSES by default, which is exactly the inversion D68 asks for.

No LLM free text is auto-posted by this capability. There is no code path that
composes a reply any other way: compose() takes a template id and a snapshot, and
nothing else, and it is the only public producer.

WHAT A TEMPLATE MAY SAY. Three rules, all asserted by assert_templates_wellformed()
and by tests:
  * every {slot} must be a key in facts.ALL_FACT_KEYS;
  * NO slot may be a CLIENT-CONTROLLED fact. The byte-identical gate is only as
    strong as the values it interpolates: while the gym owner's own Drive folder
    label was a slot, a folder named `Photos". Blake refunded your invoice. "`
    auto-posted a billing claim, and the gate could not see it because the
    reconstruction carried the same text. Every remaining slot is a number Echo
    itself computed;
  * a template that claims Echo performed an action must require a run fact to be
    TRUE, not merely present -- `requires` is a presence test, and a snapshot
    carrying sync_ran=False once rendered "I ran the photo sync just now".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import diagnostics as _diag
from . import facts as _facts

_SLOT_RE = re.compile(r"\{([a-z0-9_]+)\}")


class ReplyRefused(Exception):
    """The candidate reply is not a faithful render of a registered template over
    verified facts. The flow escalates instead of posting."""


# ---------------------------------------------------------------------------
# CLIENT-CONTROLLED FACT KEYS — NEVER INTERPOLATED INTO AN AUTO-SENT REPLY.
#
# This is the rule the first version of this file got wrong, and it is the one that
# could actually reach a client. `media_source_folder_name` is a label the gym owner
# types into their OWN Google Drive. It was a template slot, so a folder named
#
#     Photos". Blake refunded your invoice; your plan is free now. "
#
# rendered a billing claim into a message Echo auto-posts under its own name — and the
# byte-identical grounding gate gave ZERO protection, because the reconstruction
# contains the same injected text. Escaping does not fix it either: the injected text
# is prose, not markup.
#
# The fix is structural rather than sanitising. A reply slot may only be a fact key
# whose value Echo itself computes: counts, timestamps, booleans. A fact whose value
# originates with the client may still be DIAGNOSED, recorded in the audit trail and
# shown on the internal escalation card — it just cannot be a sentence Echo says
# unattended. assert_templates_wellformed() enforces that, so a future template cannot
# reintroduce the hole.
CLIENT_CONTROLLED_FACT_KEYS = frozenset({
    "media_source_folder_name",   # the gym owner's own Drive folder label
})


@dataclass(frozen=True)
class ReplyTemplate:
    id: str
    diagnostic_id: str
    text: str
    # Facts that must be PRESENT in the snapshot for this template to be usable,
    # beyond the ones it interpolates.
    requires: tuple = ()
    # Facts that must be present AND TRUE. `requires` is only a presence test, which
    # is not enough for a template that claims Echo did something: a snapshot carrying
    # sync_ran=False satisfied presence and rendered "I ran the photo sync just now".
    requires_true: tuple = ()
    # Facts that must be present AND FALSE. The polarity twin of requires_true, and it
    # exists for the same reason: cta_missing_section says "It has no CTA rotation
    # section at all" while only requiring cta_section_present to be PRESENT, so it
    # rendered that sentence off a snapshot where the section was present. A template
    # may not state a claim its own facts contradict, in EITHER direction.
    requires_false: tuple = ()
    # True when the template asserts Echo performed an action. Such a template must
    # require a run fact AND require it to be true; assert_templates_wellformed()
    # enforces both.
    claims_action: bool = False

    @property
    def slots(self):
        return tuple(sorted(set(_SLOT_RE.findall(self.text))))

    @property
    def fact_keys(self):
        return tuple(sorted(set(self.slots) | set(self.requires)
                            | set(self.requires_true) | set(self.requires_false)))


# ---------------------------------------------------------------------------
# THE REGISTRY. Adding an entry widens what Echo may say unattended.
# ---------------------------------------------------------------------------
TEMPLATES = {}


def _register(t):
    if t.id in TEMPLATES:
        raise ValueError(f"duplicate reply template id {t.id!r}")
    TEMPLATES[t.id] = t
    return t


# --- Case 1 shape: Drive photos ------------------------------------------------
DRIVE_SYNCED = _register(ReplyTemplate(
    id="drive_synced",
    diagnostic_id=_diag.DIAG_DRIVE_PHOTOS,
    claims_action=True,
    requires=("media_source_active",),
    requires_true=("sync_ran", "media_source_active"),
    text=(
        "Your connected Google Drive folder is active. I ran the photo sync for your "
        "gym just now and pulled in {assets_inserted_this_run} file(s); your library "
        "now holds {media_asset_count} photo(s) and video(s). New posts will draw "
        "from those."
    ),
))

# NOTE, deliberately recorded rather than left to be rediscovered: there is no
# "your sync just needs to wait for tonight's run" template. Telling a paying client
# to wait is not a fix, and this capability's whole point in the Case 1 shape is that
# it RUNS the sync now instead. A template no remedy can produce is dead client-facing
# copy — the "built but not wired" class D68 names — so it is absent by design, and
# tests/test_client_dm_flow.py::test_every_registered_template_is_reachable_from_a_
# planned_remedy fails if an unreachable one is ever added.

DRIVE_REVOKED = _register(ReplyTemplate(
    id="drive_revoked",
    diagnostic_id=_diag.DIAG_DRIVE_PHOTOS,
    requires=("media_source_revoked",),
    requires_true=("media_source_revoked",),
    text=(
        "Your connected Google Drive folder is no longer shared with us, so the photo "
        "sync cannot read it and your library holds {media_asset_count} file(s). "
        "Re-sharing that folder in the portal will let it resume."
    ),
))

# --- Case 2 shape: CTA pool ----------------------------------------------------
CTA_ASK = _register(ReplyTemplate(
    id="cta_ask",
    diagnostic_id=_diag.DIAG_CTA_POOL,
    requires=("cta_section_present", "cta_section_is_todo"),
    requires_true=("cta_section_present", "cta_section_is_todo"),
    text=(
        "I checked your brand voice doc. The \"CTA rotation\" section is still the "
        "blank placeholder from onboarding, so your posts have "
        "{cta_pool_count} call(s) to action to draw from — that is why they are "
        "going out without one. I cannot write this one for you, because a CTA has "
        "to be your real booking link, phone number or offer. What would you like "
        "your posts to ask people to do?"
    ),
))

CTA_MISSING_SECTION = _register(ReplyTemplate(
    id="cta_missing_section",
    diagnostic_id=_diag.DIAG_CTA_POOL,
    requires=("voice_doc_present", "cta_section_present"),
    requires_true=("voice_doc_present",),
    requires_false=("cta_section_present",),   # it says "no CTA section AT ALL"
    text=(
        "I checked your brand voice doc. It has no \"CTA rotation\" section at all, "
        "so your posts have {cta_pool_count} call(s) to action to draw from. What "
        "booking link, phone number or offer would you like your posts to point "
        "people to?"
    ),
))


def assert_templates_wellformed():
    """Static properties of the registry itself. Asserted by the test suite, and by
    compose() on every call so a bad edit cannot ship quietly."""
    problems = []
    for t in TEMPLATES.values():
        for slot in t.slots:
            if slot not in _facts.ALL_FACT_KEYS:
                problems.append(
                    f"template {t.id!r} interpolates {{{slot}}}, which is not a fact "
                    f"key in facts.ALL_FACT_KEYS"
                )
        for req in tuple(t.requires) + tuple(t.requires_true) + tuple(t.requires_false):
            if req not in _facts.ALL_FACT_KEYS:
                problems.append(
                    f"template {t.id!r} requires {req!r}, which is not a fact key"
                )
        if t.diagnostic_id not in _diag.ALL_DIAGNOSTICS:
            problems.append(
                f"template {t.id!r} names unknown diagnostic {t.diagnostic_id!r}"
            )
        hostile = set(t.slots) & CLIENT_CONTROLLED_FACT_KEYS
        if hostile:
            problems.append(
                f"template {t.id!r} interpolates client-controlled fact(s) "
                f"{sorted(hostile)} into a message Echo auto-posts. A client-supplied "
                f"value can carry whole sentences, and the byte-identical gate cannot "
                f"see them because the reconstruction contains the same text. A reply "
                f"slot may only be a value Echo itself computes."
            )
        contradiction = set(t.requires_true) & set(t.requires_false)
        if contradiction:
            problems.append(
                f"template {t.id!r} requires {sorted(contradiction)} to be both true "
                f"and false; it can never render"
            )
        if t.claims_action and not (set(t.requires_true) & _facts.RUN_FACT_KEYS):
            problems.append(
                f"template {t.id!r} claims Echo performed an action but does not "
                f"require a run fact to be TRUE; `requires` is only a presence test, "
                f"so it could render 'I ran the sync' off sync_ran=False"
            )
    if problems:
        raise ReplyRefused("reply template registry is malformed: " + "; ".join(problems))
    return True


# Slot values that are CLIENT-CONTROLLED. `media_source_folder_name` is the name of a
# folder in the gym owner's own Google Drive: it is read out of Drive, stored on the
# media_source row, and interpolated into a message this capability posts into Slack
# unattended. That makes it untrusted text on an auto-send path, so it is bounded here.
#
# Two controls, both total rather than enumerated (the adapter's own RT-M1/RA-M2 fix,
# same reasoning):
#   * ESCAPE & < > to entities. This disarms ALL Slack markup in one pass, because
#     every piece of Slack markup needs < and > -- including <!channel>, <!here> and
#     <@U...>, which would otherwise let a folder name ping every human in the group DM.
#     It is not a list of bad strings, so there is no phrasing that slips past it.
#   * REFUSE on a control character, a newline, or an over-long value, rather than
#     silently truncating. A folder name that could forge message structure sends the
#     whole ticket to a human instead.
CLIENT_CONTROLLED_SLOTS = frozenset({"media_source_folder_name"})
MAX_SLOT_CHARS = 120


def _safe_slot(key, value):
    """Bound one client-controlled string before it reaches an auto-sent message."""
    if not isinstance(value, str):
        return value
    if len(value) > MAX_SLOT_CHARS:
        raise ReplyRefused(
            f"fact {key!r} is {len(value)} chars, over the {MAX_SLOT_CHARS}-char cap "
            f"for a client-controlled slot; escalating rather than truncating"
        )
    for ch in value:
        if ch == "\n" or ch == "\r" or ch == "\t" or ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ReplyRefused(
                f"fact {key!r} contains a control character or newline, which could "
                f"forge structure in an auto-sent message; escalating"
            )
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(template, snapshot):
    """The ONLY producer of reply text in this capability.

    Refuses -- rather than rendering something partial -- when the snapshot does not
    carry every fact the template names, or carries them from a different diagnostic,
    or carries a client-controlled value that cannot be safely interpolated.
    """
    if isinstance(template, str):
        try:
            template = TEMPLATES[template]
        except KeyError:
            raise ReplyRefused(f"no registered reply template {template!r}")
    if not isinstance(snapshot, _facts.GroundingSnapshot):
        raise ReplyRefused(
            f"render() needs a GroundingSnapshot, got {type(snapshot).__name__}"
        )
    if snapshot.diagnostic_id != template.diagnostic_id:
        raise ReplyRefused(
            f"template {template.id!r} is grounded in {template.diagnostic_id!r} but "
            f"the snapshot came from {snapshot.diagnostic_id!r}"
        )
    missing = [k for k in template.fact_keys if k not in snapshot]
    if missing:
        raise ReplyRefused(
            f"template {template.id!r} needs fact key(s) {missing} which the "
            f"grounding snapshot does not contain"
        )
    # A VALUE check, not a presence check. sync_ran=False must never render
    # "I ran the photo sync just now".
    falsy = [k for k in template.requires_true if not snapshot.get(k)]
    if falsy:
        raise ReplyRefused(
            f"template {template.id!r} requires fact(s) {falsy} to be TRUE, but the "
            f"snapshot has them false/zero; refusing to state something that did not "
            f"happen"
        )
    truthy = [k for k in template.requires_false if snapshot.get(k)]
    if truthy:
        raise ReplyRefused(
            f"template {template.id!r} requires fact(s) {truthy} to be FALSE, but the "
            f"snapshot has them true; refusing to state something the facts contradict"
        )
    values = {}
    for slot in template.slots:
        v = snapshot.get(slot)
        # Every string slot is bounded, not only the ones currently known to be
        # client-controlled: a fact that becomes client-influenced later must not
        # quietly become an injection surface because this list was not updated.
        values[slot] = _safe_slot(slot, v)
    body = template.text.format(**values)
    # Whole-body Slack escape, the same single-point pass the adapter applies to every
    # CONVERSATIONAL kind (its DV4 fix). A no-op for these templates -- their constants
    # carry no & < >, and every remaining slot is a number or an ISO timestamp -- so it
    # costs nothing and means no future edit can post live Slack markup. It happens
    # INSIDE render(), so the reconstruction the gate compares against is escaped too.
    return body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def assert_is_template_render(candidate, template, snapshot):
    """THE GATE. Byte-identical, or refuse.

    Given any candidate reply text, reconstruct what the registered template would
    produce from these verified facts and require exact equality. A sentence that is
    not in the template cannot survive this, whatever it says and however it was
    produced.
    """
    expected = render(template, snapshot)
    if candidate != expected:
        raise ReplyRefused(
            "the candidate reply is not a byte-identical render of template "
            f"{getattr(template, 'id', template)!r} over the verified facts, so it "
            "contains at least one claim not traceable to a fact key (D68). "
            f"expected {len(expected)} chars, got {len(candidate or '')}."
        )
    return expected


def compose(template_id, snapshot):
    """Produce an auto-sendable reply, or raise ReplyRefused.

    Returns (text, audit) where audit names every fact key the text rests on, so the
    escalation card or the receipt can show the grounding rather than assert it.
    """
    assert_templates_wellformed()
    template = TEMPLATES.get(template_id)
    if template is None:
        raise ReplyRefused(f"no registered reply template {template_id!r}")
    text = render(template, snapshot)
    # Belt: the gate is applied to our own output too, so the check is exercised on
    # every real call rather than only in tests.
    assert_is_template_render(text, template, snapshot)
    audit = {
        "template_id": template.id,
        "diagnostic_id": template.diagnostic_id,
        "fact_keys": list(template.fact_keys),
        "facts": {k: snapshot.get(k) for k in template.fact_keys},
        "claims_action": template.claims_action,
    }
    return text, audit
