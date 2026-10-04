"""
Scene Zernio transport — a fail-closed, OFF-BY-DEFAULT adapter around
ZernioClient.create_post_raw / get_post for the scene original-use lane.

Naming and semantics align with docs/SCENE_ORIGINAL_USE_RECEIPT.md (attempt,
prepared, held, readback): the transport is the "provider send" step of that
contract. It NEVER decides finality on its own — an outcome it cannot prove
against a provider readback is returned HELD, never published.

Hard rules baked in here:

1. The caller owns the attempt UUID. It is validated and forwarded verbatim as
   the Zernio Idempotency-Key; this module never generates, regenerates, or
   mutates it. A missing/invalid/non-UUID attempt id refuses BEFORE any
   network call.
2. Account, channel, media URL and the expected gym profile are FROZEN inputs
   (frozen dataclasses). There is no mutation hook and no defaulting.
3. A Zernio 409 without a verifiable existingPostId is NOT success — held.
   A 409 WITH an existingPostId is accepted ONLY after get_post readback
   matches the frozen identity exactly.
4. A 2xx whose body carries no post id is NOT success — held (the
   published-but-not-posted class; see zernio_publisher's same rail).
5. Every accepted outcome requires a get_post readback that (a) names the
   SAME post id that was requested, (b) carries a conclusive successful
   top-level status AND a conclusive per-platform delivered/published state
   for the target platform, and (c) matches the frozen account id, channel,
   media URL and expected profile id EXACTLY. Failed, pending, draft,
   scheduled, unknown or missing states — and multiple ambiguous platform
   entries — are all held. Never invent success from unknown state.

The transport does nothing unless explicitly enabled: SceneZernioTransport
defaults enabled=False, and the environment flag
ECHO_SCENE_ZERNIO_TRANSPORT_ENABLED defaults OFF. When disabled, publish()
returns a held outcome WITHOUT any network call. This module is NOT wired
into any publish lane and is not production ready.
"""

import json
import os
import uuid as _uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from . import zernio

#: Environment flag. OFF unless exactly a truthy value is set. This is the
#: ONLY production-enabling switch this module knows, and it defaults off.
ENABLED_ENV = "ECHO_SCENE_ZERNIO_TRANSPORT_ENABLED"

#: Outcome statuses. "held" is the fail-closed bucket: ambiguous, unverifiable,
#: refused, or disabled all land here and never read as success.
STATUS_PUBLISHED = "published"
STATUS_HELD = "held"


def transport_enabled(env=None):
    """True only when ECHO_SCENE_ZERNIO_TRANSPORT_ENABLED is explicitly truthy.

    Defaults OFF; an unset or unrecognised value is OFF."""
    raw = (env if env is not None else os.environ).get(ENABLED_ENV, "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SceneChannelTarget:
    """The frozen destination identity for one scene publish attempt.

    Every field is a caller-supplied binding; the provider readback must
    reproduce ALL of them exactly or the outcome is held."""
    account_id: str            # Zernio connected-account _id
    channel: str               # Zernio platform spelling, e.g. "instagram"
    media_url: str             # the exact delivered media URL
    expected_profile_id: str   # the gym's expected Zernio profile id


@dataclass(frozen=True)
class ScenePublishAttempt:
    """One logical publish attempt. `attempt_id` is the caller-minted UUID that
    doubles as the Zernio Idempotency-Key. This object is immutable; the id is
    validated, never generated here."""
    attempt_id: str
    target: SceneChannelTarget
    content: str = ""

    def __post_init__(self):
        # Validate without normalising: the forwarded key is the verbatim string.
        try:
            _uuid.UUID(str(self.attempt_id))
        except (ValueError, AttributeError, TypeError):
            raise ValueError(
                "attempt_id must be a caller-supplied UUID string; the "
                "transport never generates one") from None


@dataclass(frozen=True)
class SceneTransportOutcome:
    """The result of one publish() call. `status` is STATUS_PUBLISHED only when
    a provider readback proved the frozen identity; otherwise STATUS_HELD."""
    status: str
    attempt_id: str
    post_id: str = ""
    reason: str = ""
    readback_verified: bool = False

    @property
    def ok(self):
        return self.status == STATUS_PUBLISHED


def _held(attempt_id, reason, post_id=""):
    return SceneTransportOutcome(status=STATUS_HELD, attempt_id=str(attempt_id),
                                 post_id=str(post_id or ""), reason=reason,
                                 readback_verified=False)


def _existing_post_id(detail):
    """Zernio's existingPostId out of a 409 body, or ''.

    Fail-closed against garbage: the detail may be a JSON dict string, a JSON
    list, a bare scalar, already-parsed data, or not JSON at all. Only a plain
    non-empty string (or a real number, coerced) sitting at
    {existingPostId: ...} of a DICT is accepted; anything else — a list, a
    scalar body, a nested dict/list value, None, malformed JSON — yields ''
    and the caller holds. Never raises."""
    try:
        data = detail
        if isinstance(data, (str, bytes)):
            data = json.loads(data or "")
        if not isinstance(data, dict):
            return ""
        value = data.get("existingPostId")
        if isinstance(value, bool) or value is None:
            return ""
        if isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str):
            return ""
        return value.strip()
    except Exception:  # noqa: BLE001 - any parse weirdness is just "no id"
        return ""


#: Conclusive DELIVERED states, top-level or per-platform. This is the success
#: vocabulary of gbp_worker.classify_reconcile (the only existing lane that
#: classifies get_post responses). Anything not listed here — "failed",
#: "pending", "scheduled", "processing", "draft", "queued", "", a misspelling,
#: an absent field — is NOT success and the attempt stays held.
_DELIVERED_STATES = frozenset({"published", "live", "success", "succeeded", "posted"})


def _unwrap_post(post_json):
    """The innermost post dict of a get_post body, or None when the shape is
    not a dict carrying a post."""
    if not isinstance(post_json, dict):
        return None
    for holder in (post_json.get("post"), post_json.get("data")):
        if isinstance(holder, dict):
            return holder
    return post_json


def _norm_state(value):
    return str(value or "").strip().lower()


def _readback_identity(post_json) -> Tuple[Optional[str], Optional[str], frozenset, Optional[str]]:
    """(account_id, channel, media_urls, profile_id) provable from a get_post
    body, or Nones/empty when the shape cannot be verified. Strict: an
    unverifiable shape yields Nones, which can never match a frozen target.
    The per-platform delivered-state proof lives in readback_matches; this is
    field extraction only."""
    post = _unwrap_post(post_json)
    if post is None:
        return None, None, frozenset(), None
    account_id = channel = None
    for entry in post.get("platforms") or []:
        if not isinstance(entry, dict):
            continue
        aid = entry.get("accountId") or entry.get("account_id")
        plat = entry.get("platform")
        if aid and plat:
            account_id, channel = str(aid), str(plat)
            break
    if account_id is None:
        aid = post.get("accountId") or post.get("account_id")
        account_id = str(aid) if aid else None
        plat = post.get("platform")
        channel = str(plat) if plat else channel
    urls = frozenset(
        str(m.get("url")) for m in (post.get("mediaItems") or post.get("media") or [])
        if isinstance(m, dict) and m.get("url"))
    profile_id = post.get("profileId") or post.get("profile_id")
    return account_id, channel, urls, (str(profile_id) if profile_id else None)


def _readback_post_id(post_json):
    """The readback post's own _id/id, or '' — used to prove the provider
    returned the post we ASKED about, not a lookalike."""
    post = _unwrap_post(post_json)
    if post is None:
        return ""
    pid = post.get("_id") or post.get("id")
    return str(pid).strip() if pid and not isinstance(pid, bool) else ""


def _platform_entries(post_json, channel):
    """Every platforms[] entry for `channel`. A malformed platforms list
    yields no entries, which can never verify."""
    post = _unwrap_post(post_json)
    if post is None:
        return []
    entries = post.get("platforms")
    if not isinstance(entries, list):
        return []
    return [e for e in entries
            if isinstance(e, dict) and str(e.get("platform") or "") == str(channel)]


def readback_matches(post_json, target: SceneChannelTarget, post_id="") -> bool:
    """True iff a get_post readback PROVES this exact attempt landed:
      1. the readback post id equals the requested post id (when given);
      2. the top-level status is a conclusive delivered state;
      3. the target platform has exactly one unambiguous per-platform entry
         and it is in a conclusive delivered state for the frozen account;
      4. the frozen identity (account, channel, media URL, profile) matches
         EXACTLY.
    Any missing, drifted, failed, pending, draft or ambiguous element is a
    non-match — success is never invented from unknown state."""
    if post_id and _readback_post_id(post_json) != str(post_id):
        return False
    post = _unwrap_post(post_json)
    if post is None:
        return False
    if _norm_state(post.get("status")) not in _DELIVERED_STATES:
        return False
    all_entries = post.get("platforms")
    entries = _platform_entries(post_json, target.channel)
    # We sent exactly one destination. A second destination is an unexpected
    # provider side effect even if our requested channel succeeded.
    if not isinstance(all_entries, list) or len(all_entries) != 1 or len(entries) != 1:
        return False
    entry = entries[0]
    if _norm_state(entry.get("status")) not in _DELIVERED_STATES:
        return False
    entry_account = entry.get("accountId") or entry.get("account_id")
    if not entry_account or str(entry_account) != str(target.account_id):
        return False
    media = post.get("mediaItems")
    if media is None:
        media = post.get("media")
    # We sent exactly one image. Membership alone would accept a readback that
    # also published an unapproved second image.
    if (not isinstance(media, list) or len(media) != 1
            or not isinstance(media[0], dict)
            or str(media[0].get("url") or "") != str(target.media_url)):
        return False
    account_id, channel, urls, profile_id = _readback_identity(post_json)
    if not account_id or not channel or not profile_id:
        return False
    return (account_id == str(target.account_id)
            and channel == str(target.channel)
            and str(target.media_url) in urls
            and profile_id == str(target.expected_profile_id))


class SceneZernioTransport:
    """Fail-closed adapter over ZernioClient for the scene original-use lane.

    `client` is injectable (tests pass a fake with create_post_raw / get_post).
    `enabled` defaults False; the environment flag alone does NOT enable an
    instance constructed with an explicit `enabled` value.
    """

    def __init__(self, client=None, enabled=None):
        self._client = client
        self._enabled = transport_enabled() if enabled is None else bool(enabled)

    @property
    def enabled(self):
        return self._enabled

    def _zernio(self):
        if self._client is not None:
            return self._client
        return zernio.ZernioClient()

    def _readback_or_hold(self, attempt, post_id, via):
        """Verify post_id against the frozen target via get_post. Published only
        on an exact identity match; every other outcome is held."""
        try:
            post = self._zernio().get_post(post_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable readback is ambiguous
            return _held(attempt.attempt_id,
                         f"{via}: readback of post {post_id} failed "
                         f"({type(exc).__name__}); outcome ambiguous, held",
                         post_id=post_id)
        if readback_matches(post, attempt.target, post_id=str(post_id)):
            return SceneTransportOutcome(
                status=STATUS_PUBLISHED, attempt_id=str(attempt.attempt_id),
                post_id=str(post_id), reason=f"{via}: readback identity match",
                readback_verified=True)
        return _held(attempt.attempt_id,
                     f"{via}: readback of post {post_id} does not prove the "
                     "frozen account/channel/media/profile identity in a "
                     "conclusive delivered state; held",
                     post_id=post_id)

    def publish(self, attempt: ScenePublishAttempt) -> SceneTransportOutcome:
        """Send (or reconcile) ONE scene publish attempt. Never raises for a
        provider ambiguity — anything unproven returns held.

        No network call happens unless the transport is explicitly enabled.
        """
        if not isinstance(attempt, ScenePublishAttempt):
            raise TypeError("publish() requires a frozen ScenePublishAttempt")
        if not self._enabled:
            return _held(attempt.attempt_id,
                         f"transport disabled ({ENABLED_ENV} OFF); no send attempted")

        target = attempt.target
        payload = {
            "content": attempt.content or "",
            "platforms": [{"accountId": str(target.account_id),
                           "platform": str(target.channel)}],
            "mediaItems": [{"type": zernio._media_type(str(target.media_url)),
                            "url": str(target.media_url)}],
        }
        try:
            resp = self._zernio().create_post_raw(
                payload, idempotency_key=str(attempt.attempt_id))
        except zernio.ZernioError as exc:
            if getattr(exc, "status", None) == 409:
                existing = _existing_post_id(getattr(exc, "detail", ""))
                if not existing:
                    # A 409 that names no verifiable post is NOT dedup success.
                    return _held(attempt.attempt_id,
                                 "zernio 409 without a verifiable "
                                 "existingPostId; outcome ambiguous, held")
                return self._readback_or_hold(attempt, existing, "409 dedup")
            # Any other provider error: the send may or may not have landed.
            return _held(attempt.attempt_id,
                         f"zernio error {getattr(exc, 'status', '?')}; outcome "
                         "ambiguous, held")
        except Exception as exc:  # noqa: BLE001 - transport failure is ambiguous
            return _held(attempt.attempt_id,
                         f"transport failure ({type(exc).__name__}); outcome "
                         "ambiguous, held")

        post_id = zernio.post_id_of(resp)
        if not str(post_id or "").strip():
            # A 2xx without a post id is NOT a publish (published-but-not-posted).
            return _held(attempt.attempt_id,
                         "zernio 2xx carried no post id; refusing to infer "
                         "success, held")
        return self._readback_or_hold(attempt, post_id, "created")
