"""
Scene attester evidence normalization (DRAFT / OFF / pure).

Pure normalization helpers for a FUTURE, separately reviewed scene attester.
Given an already-fetched provider readback (a get_post body) or an
already-fetched complete posts range (a zernio.posts_range_complete result),
decide what the evidence proves — and, more importantly, what it does NOT.

This module makes NO provider calls and NO database calls. Every input is a
caller-supplied Python object; every output is a plain dict. It never fetches,
never writes, never flags. It imports only pure helpers from
agent.scene_zernio_transport (field extraction and the readback gate); the
import performs no I/O.

Vocabulary:
  DELIVERED_PROVEN  the readback proves the frozen identity in a conclusive
                    delivered state (the ONLY success outcome).
  HELD_UNPROVEN     the evidence disproves the claim or cannot prove it
                    (missing identity, drift, pending/draft/failed status,
                    duplicate destination/media, surface/page drift, a 409
                    that named no post, a matching post present in a range).
  ABSENT_PROVEN     RESERVED, NEVER RETURNED. Absence requires authenticating
                    provider pagination, which a pure helper over caller-
                    supplied lists cannot do — a caller saying
                    range_complete=True is forgeable. Until a trusted direct
                    provider caller or a verified range proof exists, no
                    absence is ever proven; the no-match outcome is AMBIGUOUS.
  AMBIGUOUS         the evidence cannot be judged at all (malformed body,
                    capped/incomplete range, unparseable post in range).

Hard rules:
  - Success is never invented from unknown state. Any required identity
    field the readback cannot prove (including surface contentType and
    pageId) holds the verdict; absence of a field is never inferred.
  - Absence is NEVER proven by this module: a pure helper cannot authenticate
    provider pagination, so range_complete=True (a bare caller attestation)
    is forgeable and even an empty list stays AMBIGUOUS. The no-send /
    not-present outcome is disabled by construction. There is deliberately
    NO caller-mintable token or wrapper that could confer fake trust.
  - A post inside the range whose identity cannot be fully extracted makes
    the range AMBIGUOUS — it might be the attempt, and we do not guess.
"""

from . import scene_zernio_transport as szt

DELIVERED_PROVEN = "delivered_proven"
HELD_UNPROVEN = "held_unproven"
#: Reserved vocabulary ONLY — absence_evidence NEVER returns this status.
#: Kept so serialized historic verdicts remain parseable; do not emit.
ABSENT_PROVEN = "absent_proven"
AMBIGUOUS = "ambiguous"


def _verdict(status, reasons, post_id=""):
    return {"status": status,
            "proven": status in (DELIVERED_PROVEN, ABSENT_PROVEN),
            "post_id": str(post_id or ""),
            "reasons": tuple(reasons)}


def _entry_surface_page(entry):
    ct, pid = szt._readback_surface(entry)
    return ct, str(pid or "")


def _identity_defects(post, target, *, expected_content=None,
                      require_singleton=True):
    """A list of human-readable identity defects between an unwrapped post
    dict and the frozen target. An EMPTY list means the post carries the
    full frozen identity (account, channel, profile, singleton platform &
    media, explicit surface, exact page, and content when bound). Status is
    deliberately NOT checked here — presence/identity only."""
    defects = []
    if not isinstance(post, dict):
        return ["post body is not a dict"]
    account_id, channel, urls, profile_id = szt._readback_identity(post)
    if not account_id:
        defects.append("no connected-account identity provable")
    elif account_id != str(target.account_id):
        defects.append(f"account drift: readback {account_id!r}")
    if not channel:
        defects.append("no channel provable")
    elif channel != str(target.channel):
        defects.append(f"channel drift: readback {channel!r}")
    if not profile_id:
        defects.append("no profile provable")
    elif profile_id != str(target.expected_profile_id):
        defects.append(f"profile drift: readback {profile_id!r} "
                       "(same account on another profile is not this attempt)")
    entries = szt._platform_entries(post, target.channel)
    all_entries = post.get("platforms")
    if require_singleton:
        if not isinstance(all_entries, list) or len(all_entries) != 1 \
                or len(entries) != 1:
            defects.append("not a singleton destination "
                           "(duplicate/extra platform entries)")
            entries = []
    if len(entries) == 1:
        entry = entries[0]
        entry_account = entry.get("accountId") or entry.get("account_id")
        if not entry_account or str(entry_account) != str(target.account_id):
            defects.append("platform entry account drift")
        surface, page_id = _entry_surface_page(entry)
        if surface is None:
            defects.append("surface not explicit in readback "
                           "(contentType missing; never inferred)")
        elif surface != str(target.content_type):
            defects.append(f"surface drift: readback {surface!r} vs "
                           f"{target.content_type!r}")
        if page_id != str(target.page_id or ""):
            defects.append(f"page drift: readback pageId {page_id!r} vs "
                           f"{target.page_id or ''!r}")
    media = post.get("mediaItems")
    if media is None:
        media = post.get("media")
    if require_singleton:
        if (not isinstance(media, list) or len(media) != 1
                or not isinstance(media[0], dict)
                or str(media[0].get("url") or "") != str(target.media_url)):
            defects.append("not a singleton exact media item")
        elif str(media[0].get("type") or "") != \
                szt.zernio._media_type(str(target.media_url)):
            # Media TYPE drift (video vs image/gif) on the exact URL: the
            # readback singleton must equal the canonical payload's expected
            # type.
            defects.append(
                f"media type drift: readback {media[0].get('type')!r} vs "
                f"expected {szt.zernio._media_type(str(target.media_url))!r}")
    else:
        if str(target.media_url) not in urls:
            defects.append("media URL not present")
    if expected_content is not None:
        content = post.get("content")
        if not isinstance(content, str) or content != str(expected_content):
            defects.append("content not proven verbatim by readback")
    return defects


def delivered_evidence(post_json, target, *, post_id="", expected_content=None):
    """Normalize an already-fetched get_post readback into a verdict.

    Returns DELIVERED_PROVEN only when the full readback gate passes
    (szt.readback_matches with the frozen target). Every other outcome is
    HELD_UNPROVEN with the specific defects, or AMBIGUOUS when the body is so
    malformed it cannot even be judged. Pure; never raises on provider
    weirdness — garbage in, held/ambiguous out."""
    reasons = []
    if post_id:
        readback_id = szt._readback_post_id(post_json)
        if not readback_id:
            reasons.append("readback carries no provable post id")
        elif readback_id != str(post_id):
            reasons.append(f"post id drift: asked {post_id!r}, "
                           f"readback names {readback_id!r}")
    post = szt._unwrap_post(post_json)
    if post is None:
        return _verdict(AMBIGUOUS, ["malformed readback body; cannot be judged"],
                        post_id=post_id)
    state = szt._norm_state(post.get("status"))
    if state not in szt._DELIVERED_STATES:
        reasons.append(f"top-level status {state or '<absent>'!r} is not a "
                       "conclusive delivered state")
    entries = szt._platform_entries(post, target.channel)
    if len(entries) == 1:
        entry_state = szt._norm_state(entries[0].get("status"))
        if entry_state not in szt._DELIVERED_STATES:
            reasons.append(f"per-platform status {entry_state or '<absent>'!r} "
                           "is not a conclusive delivered state")
    # Content binding is MANDATORY on the DELIVERED_PROVEN path: the caller
    # must supply the frozen attempt content, and the target's mandatory
    # payload digest must bind that exact content (account/channel/page/
    # surface/media/send mode are frozen on the target itself). When the
    # provider cannot prove content verbatim, hold — never publish.
    if not isinstance(expected_content, str):
        reasons.append("content binding is mandatory: expected_content was "
                       "not supplied; DELIVERED_PROVEN is impossible")
    elif szt.canonical_payload_digest(
            szt.canonical_payload(target, expected_content)) != \
            target.payload_sha256:
        reasons.append("expected_content is not bound by the target's "
                       "payload_sha256; refusing to prove delivery of an "
                       "unbound payload")
    reasons.extend(_identity_defects(post, target,
                                     expected_content=expected_content))
    if not reasons and szt.readback_matches(
            post_json, target, post_id=str(post_id or ""),
            expected_content=expected_content):
        return _verdict(DELIVERED_PROVEN,
                        ["readback proves the frozen identity in a conclusive "
                         "delivered state"], post_id=post_id)
    if not reasons:
        reasons.append("readback gate rejected the body (shape ambiguity)")
    return _verdict(HELD_UNPROVEN, reasons, post_id=post_id)


def absence_evidence(posts, *, range_complete, target, expected_content=None):
    """Normalize an already-fetched posts range into an ABSENCE verdict.

    THIS FUNCTION NEVER RETURNS ABSENT_PROVEN. A pure helper cannot
    authenticate provider pagination: `range_complete=True` is a bare caller
    attestation and is FORGEABLE, so even an empty list with
    range_complete=True cannot prove a post was never sent. Until a trusted
    direct provider caller or a verified range proof exists (none does), the
    no-match outcome is AMBIGUOUS — no-send is disabled by construction, and
    there is deliberately no caller-mintable token/wrapper that confers
    trust.

    What it still does:
      1. range_complete other than exactly True -> AMBIGUOUS.
      2. `posts` not a list, or any range post whose identity cannot be
         fully extracted -> AMBIGUOUS (that post might be the attempt).
      3. A post matching the full frozen identity -> HELD_UNPROVEN for the
         absence claim (the attempt is present; absence is disproven,
         delivery is a separate question for delivered_evidence).
      4. Otherwise (no matching post) -> AMBIGUOUS, never ABSENT_PROVEN.

    Pure; never raises on garbage."""
    if range_complete is not True:
        return _verdict(AMBIGUOUS,
                        ["range not proven complete (capped/partial ranges "
                         "cannot prove absence)"])
    if not isinstance(posts, list):
        return _verdict(AMBIGUOUS, ["posts range is not a list"])
    for i, raw in enumerate(posts):
        post = szt._unwrap_post(raw)
        if post is None:
            return _verdict(AMBIGUOUS,
                            [f"range post #{i} is malformed; absence cannot "
                             "be ruled in or out"])
        account_id, channel, _urls, profile_id = szt._readback_identity(post)
        # Surface must be extractable for the post's OWN channel (a post for
        # another channel still needs its identity readable to be ruled out).
        surface_entries = szt._platform_entries(post, channel) if channel else []
        surface_known = bool(surface_entries) and \
            _entry_surface_page(surface_entries[0])[0] is not None
        if not (account_id and channel and profile_id and surface_known):
            return _verdict(AMBIGUOUS,
                            [f"range post #{i} lacks extractable identity; "
                             "absence cannot be ruled in or out"])
        defects = _identity_defects(post, target,
                                    expected_content=expected_content)
        if not defects:
            pid = szt._readback_post_id(raw)
            return _verdict(HELD_UNPROVEN,
                            ["a post matching the full frozen identity is "
                             "present in the complete range; absence claim "
                             "fails"], post_id=pid)
    return _verdict(AMBIGUOUS,
                    ["no post in the caller-supplied range matches the frozen "
                     "identity, but a pure helper cannot authenticate "
                     "provider pagination (range_complete is a forgeable "
                     "caller attestation); absence is NOT proven — held "
                     "ambiguous by construction"])
