"""
day_shape.py: what TWO posts in ONE day actually are, and the assertion that a
plan pass is not allowed to write two of the same thing onto one day.

WHY THIS EXISTS
---------------
On 2026-09-04 Tough Temple, a paying client, was published six times on Instagram
in forty seconds. Fleet wide it was 84 extra publishes across 10 gyms, every one
of them live on a client account. The publisher was NOT the defect. It published
what the planner wrote: on a 2x per day gym the planner had put ONE caption into
TWO slots of the same day, and the publisher was told to publish both.

A caption guard that refuses a repeat stops the double post. It does not fix the
wound. If the planner keeps writing one concept into two slots and a guard eats
the second, the gym silently receives ONE post a day while its calendar claims
two. That is Dale's B8 ticket word for word, and a guard alone makes it
deterministic instead of random.

So the fix has two halves, and both live here.

HALF ONE: WHAT A TWO POST DAY IS (the content contract)
-------------------------------------------------------
A gen pop boutique fitness gym posting twice in one day is not posting the same
thing twice at two times. It is running a pair with two different jobs:

  SLOT 0, morning (07:30) -> PROOF.
      Story led. A member, a coach, a moment that actually happened. It leads
      with the reader's lived problem and it earns attention. The ask is soft:
      comment, tag someone, send a message. Pillars: faces, community, results,
      testimonial.

  SLOT 1, evening (18:30) -> INVITATION.
      Offer led. The named next step. It leads with the outcome and it converts.
      The ask is hard and specific: book the intro, claim the free week, start
      Monday. Pillars: offer, platform, doctrine, b2b, summit, podcast.

Proof in the morning, invitation in the evening. Different pillar, different
hook, different photo. Two concepts, not one concept reworded. That is the whole
design, and PROOF_PILLARS / INVITATION_PILLARS / role_for_slot() are how a
builder asks for it.

HALF TWO: THE ASSERTION (the guard that PREVENTS the damage)
------------------------------------------------------------
assert_day_distinct(rows) runs at PLAN TIME, on the rows a build is about to
write, BEFORE anything reaches content_calendar. Two rows that share a
(gym_id, account, post_date, format) must differ in BOTH caption and image_url.
A violation RAISES. The build writes nothing and a human is told which day broke.

It is grouped that way on purpose:
  * account is in the key because the Facebook mirror is a legitimate copy of the
    Instagram feed. Same caption on ig and on fb is the cross post, not a repeat.
  * format is in the key because a paired story legitimately carries its feed's
    caption. Feed is compared with feed, story with story.

A day that can honestly only produce one distinct concept must emit ONE post.
That is not a violation, it is the truth, and it passes. What may never pass is
two rows carrying the same words or the same photo onto one account on one day.

FLAGS
-----
  ECHO_DAY_SHAPE_ASSERT (config.day_shape_assert_enabled, default ON)
      The guard. It only ever PREVENTS a write that would repeat a client's post,
      so it is armed by default under the standing rule that a guard which
      prevents damage may default on. Escape hatch: ECHO_DAY_SHAPE_ASSERT=false
      restores the old silent behavior exactly.

  ECHO_DAY_SHAPE_ROLES (config.day_shape_roles_enabled, default OFF)
      The producer half: the proof / invitation role split above, threaded into
      the builders so slot 1 draws a genuinely different concept rather than
      relying on the guard to catch a repeat. A NEW capability, so it ships off
      and Blake arms it.

Pure: no I/O, no clock, no writes.
"""

# The two roles a 2x day carries, in slot order.
PROOF = "proof"
INVITATION = "invitation"
SLOT_ROLES = (PROOF, INVITATION)

# Pillars that carry PROOF: a real person, a real moment, a real result.
PROOF_PILLARS = ("faces", "community", "results", "testimonial")

# Pillars that carry the INVITATION: the named next step.
INVITATION_PILLARS = ("offer", "platform", "doctrine", "b2b", "summit", "podcast")

# The SB7 entry angles each role leads from (drafter.CAPTION_ANGLES vocabulary).
# Proof leads from belonging and lived struggle; invitation leads from the
# friction a next step removes. Style guidance only, never a fact.
PROOF_ANGLES = ("community/belonging", "no-results-yet", "consistency-struggle",
                "low-confidence")
INVITATION_ANGLES = ("time-scarcity", "low-energy", "lack-of-accountability",
                     "intimidation")


def role_for_slot(slot_index):
    """The role a cadence slot carries: slot 0 is PROOF, slot 1 is INVITATION.

    Any slot beyond the pair alternates from there, so a future 3x day never
    lands two invitations in a row. A None / unparseable index reads as slot 0
    (proof), which is the 1x per day shape and today's behavior."""
    try:
        i = int(slot_index)
    except (TypeError, ValueError):
        i = 0
    if i < 0:
        i = 0
    return SLOT_ROLES[i % len(SLOT_ROLES)]


def pillars_for_role(role):
    """The pillar pool a role draws from. An unknown role reads as proof."""
    return INVITATION_PILLARS if role == INVITATION else PROOF_PILLARS


def angles_for_role(role):
    """The SB7 entry angles a role leads from. An unknown role reads as proof."""
    return INVITATION_ANGLES if role == INVITATION else PROOF_ANGLES


def angle_for_slot(slot_index, rotation=0):
    """A deterministic angle for a cadence slot: the role's angle pool, rotated by
    `rotation` (the build's accepted-post index) so the same role does not lead
    the same way every single day. Pure and stable across re-runs."""
    pool = angles_for_role(role_for_slot(slot_index))
    try:
        r = int(rotation)
    except (TypeError, ValueError):
        r = 0
    return pool[r % len(pool)]


def normalize_caption(text):
    """Whitespace and case normalized caption, for repeat comparison. Two captions
    that differ only in spacing, newlines or case are THE SAME POST to a reader,
    so they compare equal here."""
    return " ".join((text or "").split()).strip().lower()


def normalize_media(url):
    """Normalized media reference for repeat comparison. Compared as an exact
    trimmed string: two calendar rows pointing at the same hosted asset are the
    same photo on the page, which is Pete's B5 repeat complaint."""
    return (url or "").strip()


class DayViolation:
    """One broken day: two rows on one (gym_id, account, post_date, format) that
    share a caption or a photo. Carries enough to name the day out loud."""

    __slots__ = ("gym_id", "account", "post_date", "fmt", "kind", "count", "sample")

    def __init__(self, gym_id, account, post_date, fmt, kind, count, sample=""):
        self.gym_id = gym_id
        self.account = account
        self.post_date = post_date
        self.fmt = fmt
        self.kind = kind          # 'caption' or 'image'
        self.count = count        # how many rows shared it
        self.sample = sample      # a short excerpt, for the log line

    def __repr__(self):
        return (f"DayViolation({self.gym_id} {self.account} {self.post_date} "
                f"{self.fmt}: {self.count} rows share the same {self.kind})")

    def message(self):
        """The line a human reads."""
        excerpt = (self.sample or "")[:80]
        return (f"{self.gym_id} {self.post_date} {self.account} {self.fmt}: "
                f"{self.count} posts share the same {self.kind} "
                f"({excerpt!r}). Two slots on one day must be two different "
                f"posts, not one post twice.")


class DayShapeViolation(Exception):
    """Raised when a plan pass tried to write a repeated post onto one day.

    Carries `.violations`, the full list of DayViolation, so the caller can log
    every broken day rather than only the first."""

    def __init__(self, violations):
        self.violations = list(violations or ())
        super().__init__("; ".join(v.message() for v in self.violations)
                         or "day shape violation")


# Rows in these statuses are not live plan output and are excluded from the
# comparison: a soft deleted row is already off the calendar.
_IGNORED_STATUS = ("deleted",)


def day_violations(rows):
    """Every place a batch of content_calendar rows would put the SAME post twice
    on one day.

    Groups by (gym_id, account, post_date, format) so the Facebook mirror of an
    Instagram feed and a paired story carrying its feed's caption are both
    correctly left alone, then inside each group reports any caption or any
    image_url shared by more than one row.

    Empty captions and empty image_urls are ignored: a row with nothing in the
    field is a hold or a media-pending row, not a repeat.

    Pure. Returns a list of DayViolation ordered by gym, account, date and
    format, so the report reads the same on every run."""
    groups = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().lower() in _IGNORED_STATUS:
            continue
        post_date = str(row.get("post_date") or "").strip()
        if not post_date:
            continue
        key = (str(row.get("gym_id") or ""), str(row.get("account") or ""),
               post_date, str(row.get("format") or ""))
        groups.setdefault(key, []).append(row)

    out = []
    for key in sorted(groups):
        gym_id, account, post_date, fmt = key
        bucket = groups[key]
        if len(bucket) < 2:
            continue
        for kind, field, norm in (("caption", "caption", normalize_caption),
                                  ("image", "image_url", normalize_media)):
            seen = {}
            for row in bucket:
                value = norm(row.get(field))
                if not value:
                    continue
                seen.setdefault(value, []).append(row)
            for value in sorted(seen):
                shared = seen[value]
                if len(shared) < 2:
                    continue
                sample = str(shared[0].get(field) or "")
                out.append(DayViolation(gym_id, account, post_date, fmt, kind,
                                        len(shared), sample))
    return out


def assert_day_distinct(rows, *, enabled=True):
    """FAIL the plan pass when two rows would put the same post twice on one day.

    Raises DayShapeViolation carrying every broken day. Returns the (empty) list
    of violations when the batch is clean, so a caller can log a clean pass.

    `enabled=False` (the ECHO_DAY_SHAPE_ASSERT escape hatch) skips the check
    entirely and returns [], restoring the pre-guard behavior byte for byte."""
    if not enabled:
        return []
    found = day_violations(rows)
    if found:
        raise DayShapeViolation(found)
    return found
