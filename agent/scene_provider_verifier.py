"""Independent Zernio POST readback verifier for the DRAFT scene attester runtime.

STATUS: OFF BY DEFAULT. This adapter is the independent verifier process the
attester runtime migration (DRAFT_visual_scene_attester_runtime_20261004.sql,
sections 5-6) names as the only legitimate caller of
``visual_scene_attester_attest_terminate``. It is NEVER wired into the sender
path, NEVER invoked in production, and holds no credentials of its own.
``enabled`` defaults to False and every refusal path runs BEFORE any provider
read or RPC.

Authority model (repair of the Sol-reviewed flaws):
  * The public entry point accepts a ``claim_attempt_id`` string ONLY. The
    caller can never supply the snapshot, binding, attempt row, destination
    identity or provider post id: those are fetched through an INJECTED
    read-only ``reader`` bound to the scene_attester_verifier credential.
    A caller-supplied dict is refused (``caller_supplied_identity``).
  * Authoritative read path (P1 seam repair, DRAFT/UNAPPLIED/OFF): the
    migration now defines ``visual_scene_attester_verifier_read``, a
    SECURITY DEFINER read RPC granted to scene_attester_verifier ONLY
    (revoked from public, anon, authenticated, service_role AND the
    sending role). It takes a claim_attempt_id and nothing else, derives
    tenant scope from the claim's own rows, and returns the exact
    immutable prepared snapshot, binding, attempt row and send-return
    record — failing closed (raising) when a required row is missing or
    the prepared/binding/attempt tenants disagree. When the reader cannot
    return the attempt row through this API the adapter refuses with
    ``missing_seam_verifier_attempt_read``. A caller dict is NEVER used
    as a substitute authority.
  * Immutable provider post id (P1 seam repair, DRAFT/UNAPPLIED/OFF): the
    sender records, ONCE and before finalization, the provider post id it
    ASSERTS the send returned, through
    ``visual_scene_attester_record_send_return`` (scene_attester EXECUTE
    only; append-only, first-write-wins; tenant, binding and sender
    identity copied from the frozen snapshot, never caller-asserted).
    The verifier reads that record through the read RPC and
    ``attest_terminate`` refuses any delivered outcome whose post id
    differs from it. The adapter's post id comes ONLY from the immutable
    send-return record; when no record exists yet (send in flight or
    never returned) the adapter HOLDS with
    ``provider_post_id_unrecorded`` rather than inferring one, and a
    terminal attempt post id that disagrees with the send-return record
    is a data-integrity refusal, never an override.
  * TRUTH BOUNDARY — the send-return record is SENDER-ASSERTED, not
    independently provider-authenticated: no adapter yet captures and
    binds the authenticated provider send response (request/response
    correlation) to the recorded id. Even when every readback check
    passes, final delivered activation HOLDS with
    ``missing_seam_send_return_correlation`` until a real send-return
    adapter/API correlation contract exists. This hold is never relaxed
    by delivered media verification success, and failed/partial holds are
    never released by it either.

Scope (refuse everything else):
  * Zernio-routed Instagram / Facebook feed and story posts;
  * Zernio-routed Google Business STANDARD posts.
  * NOT Meta-direct sends, NOT GBP gallery media (gmb-media has no /v1/posts
    readback; an empty read proves nothing), NOT GBP EVENT/OFFER.

Evidence model:
  * MISSING IRREVERSIBLE NO-DELIVERY PROVIDER CONTRACT (Sol re-review P1,
    Zernio docs https://docs.zernio.com/guides/error-handling): Zernio
    documents that a FAILED post may be retried and become PUBLISHED, and a
    platform entry marked failed inside a partial/multi-platform post cannot
    prove nothing was sent. Therefore NO provider status — failed, deleted,
    rejected, partial, or any other non-delivered shape — releases a
    confirmed_no_send from this adapter. Only positive delivered evidence
    (published/posted WITH platform post id) attests; every non-delivered
    status, absence, 404s, empty reads and exceptions HOLD forever. This
    adapter currently has no legitimate way to attest confirmed_no_send.
  * confirmed_no_send is attested with provider_post_id = None: the attempt
    table enforces (state='confirmed_delivered') = (provider_post_id is not
    null), so a nonnull post id on a no-send outcome is a schema violation.
  * Zernio's GET /v1/posts/{id} readback echoes the SUBMITTED request
    (content + mediaItems). Hashes inside submitted mediaItems are request
    fields the sender itself supplied; they are NEVER accepted as proof of
    platform-delivered bytes. Delivered attestation additionally requires
    (a) the frozen prepared caption and full ordered prepared media set from
    the authoritative record, and (b) platform-authenticated delivered
    evidence in the readback (per-platform ``deliveredMedia`` entries fetched
    from the platform post, carrying url+sha256+md5+byteLength). When the
    GET provides only submitted/request media or omits content/bytes the
    adapter HOLDS with an explicit missing-seam reason.
  * readback evidence sent to attest_terminate is DETERMINISTIC (canonical
    response hash, provider ids, status, fingerprints; no wall-clock fields)
    so a retry re-reading the same provider state reproduces byte-identical
    evidence and the SQL replay path returns replayed=true instead of
    raising on distinct evidence. Raw envelopes (raw post JSON or
    {"post": {...}}) are normalized; the hash/auditable reference is computed
    over the exact captured payload BEFORE normalization; malformed envelopes
    are refused.
"""

import hashlib
import json
import re

ADAPTER_VERSION = "scene_provider_verifier/2.1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MD5_RE = re.compile(r"^md5:[0-9a-f]{32}$")
_PHASH_RE = re.compile(r"^[0-9a-f]{16}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

PROVIDER = "zernio"
DELIVERED_STATUSES = ("published", "posted")
# NO_SEND_STATUSES is intentionally EMPTY. Zernio documents that a failed
# post may be retried and become published, and a failed platform entry
# inside a partial post cannot prove no send. There is NO irreversible
# no-delivery provider contract, so no status may release confirmed_no_send.
NO_SEND_STATUSES = ()
# failed / deleted / rejected / partial and every other non-delivered
# terminal or intermediate shape all HOLD, never attest.
AMBIGUOUS_STATUSES = ("failed", "deleted", "rejected", "partial")

SEAM_NO_DELIVERY_CONTRACT = (
    "Zernio error-handling docs (https://docs.zernio.com/guides/"
    "error-handling) allow a failed post to be retried into published and "
    "a failed platform entry inside a partial post cannot prove no send; "
    "no irreversible no-delivery provider contract exists, so "
    "confirmed_no_send can never be released from a provider status")

# Channels this adapter may verify. GBP gallery ("photo" surface) and any
# Meta-direct route are structurally out of scope and refused.
_CHANNEL_PLATFORMS = {
    "instagram": "instagram",
    "facebook": "facebook",
    "googlebusiness": "googlebusiness",
}
_SURFACES = ("feed", "story")

# Exact SQL seams this adapter reports verbatim (no secrets, no inference).
SEAM_ATTEMPT_READ = (
    "the authoritative read must come through "
    "visual_scene_attester_verifier_read (security definer, granted to "
    "scene_attester_verifier only); the attempt row is not directly "
    "readable by the verifier role and no caller-supplied record is a "
    "substitute authority")
SEAM_POST_ID_STORE = (
    "visual_scene_attester_send_return is the only immutable "
    "pre-finalization provider post id record (written once by the "
    "sending role through visual_scene_attester_record_send_return); "
    "without it there is no known immutable post id to read back")
SEAM_SEND_RETURN_CORRELATION = (
    "visual_scene_attester_send_return records the provider post id the "
    "SENDER asserts the send returned; no adapter yet captures and binds "
    "the authenticated provider send response (request/response "
    "correlation) to that record, so the recorded id is sender-asserted, "
    "not independently provider-authenticated, and final delivered "
    "activation must HOLD until a real send-return adapter/API "
    "correlation contract exists")
SEAM_DELIVERED_MEDIA = (
    "Zernio GET /v1/posts/{id} echoes the submitted request mediaItems; "
    "hashes in submitted mediaItems are sender-supplied request fields, "
    "not proof of bytes the platform delivered")


class VerifierInputError(ValueError):
    """Malformed authoritative record; a data-integrity failure, never
    provider evidence."""


class MissingSeamError(RuntimeError):
    """The injected reader could not reach an authoritative row through a
    safe read API. ``seam`` names the exact missing SQL/provider seam."""


def _s(value):
    return str(value or "").strip()


def canonical_response_sha256(payload):
    """SHA-256 over the canonical JSON of the exact provider payload the
    verifier captured (before envelope normalization). Computed here, never
    by the sender."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _result(decision, reason, **extra):
    out = {"decision": decision, "reason": reason, "adapter": ADAPTER_VERSION}
    out.update(extra)
    return out


def _platform_entries(post):
    return [e for e in (post.get("platforms") or []) if isinstance(e, dict)]


class SceneProviderVerifier:
    """Bounded, OFF-by-default independent readback verifier.

    Parameters
    ----------
    client:   read-only Zernio client (must expose ``get_post(post_id)``).
    rpc:      callable ``rpc(function_name, payload) -> dict`` bound to the
              scene_attester_verifier role; the ONLY terminal boundary this
              adapter may use (visual_scene_attester_attest_terminate).
    reader:   callable ``reader(claim_attempt_id) -> dict`` bound to the
              read-only scene_attester_verifier credential
              (visual_scene_attester_verifier_read). Returns
              {"prepared": {...}, "binding": {...}, "attempt": {...},
              "send_return": {...} | None} from the authoritative tables.
              A None ``attempt`` (or a raised MissingSeamError) is the
              read-path failure: the adapter refuses closed and names it.
              A None ``send_return`` means no immutable provider post id
              has been recorded yet: the adapter HOLDS, never infers.
              The reader is the ONLY source of snapshot, binding,
              destination identity and provider post id.
    verifier_id: stable independent verifier identity label. Must differ from
              the snapshot's sending attester_id (defense in depth; the role
              split is the load-bearing control).
    enabled:  OFF by default. When False every call refuses before any
              authoritative read, provider read or RPC.
    """

    def __init__(self, *, client=None, rpc=None, reader=None,
                 verifier_id=None, enabled=False):
        self._client = client
        self._rpc = rpc
        self._reader = reader
        self._verifier_id = _s(verifier_id)
        self._enabled = bool(enabled)

    # -- public entry point -------------------------------------------------

    def verify(self, claim_attempt_id):
        """Verify one claim attempt against an authenticated provider readback.

        Accepts the claim attempt id ONLY. Any caller-supplied snapshot,
        binding, destination identity or provider post id is refused: the
        authoritative records are fetched through the injected read-only
        verifier-credential reader.

        Returns a decision dict. ``decision`` is one of:
          delivered                      -> attest_terminate was called
                                            (result under ``terminal``).
                                            CURRENTLY UNREACHABLE: the
                                            send-return record is
                                            sender-asserted, so the final
                                            delivered gate HOLDS with
                                            missing_seam_send_return_correlation
                                            until a real send-return
                                            adapter/API correlation
                                            contract exists;
                                            confirmed_no_send is NEVER
                                            released: no irreversible
                                            no-delivery provider contract
                                            exists (Zernio allows failed ->
                                            published on retry);
          hold                           -> insufficient evidence, parked,
                                            no RPC (details under ``hold``);
          refused                        -> identity/scope/authority
                                            violation, no RPC.
        """
        if not self._enabled:
            return _result("refused", "adapter_disabled")
        if (self._client is None or not callable(self._rpc)
                or not callable(self._reader)):
            return _result("refused", "verifier_boundary_unconfigured")
        if not self._verifier_id:
            return _result("refused", "verifier_id_required")

        # The caller supplies an id, never identity. A dict (or any non-id)
        # is a caller-asserted snapshot/binding and is refused outright.
        if not isinstance(claim_attempt_id, str):
            return _result("refused", "caller_supplied_identity")
        token = claim_attempt_id.strip()
        if not token:
            return _result("refused", "malformed_claim_attempt_id")
        if not _UUID_RE.match(token):
            return _result("refused", "malformed_claim_attempt_id")

        # Authoritative read through the injected verifier-credential reader,
        # BEFORE any provider I/O. A read failure is a refusal, never a hold
        # and never an attestation.
        try:
            records = self._reader(token)
        except MissingSeamError as exc:
            return _result("refused", "missing_seam_verifier_attempt_read",
                           seam=_s(getattr(exc, "seam", "")) or str(exc))
        except Exception as exc:
            return _result("refused", "authoritative_read_failed",
                           hold={"error": type(exc).__name__})
        if not isinstance(records, dict):
            return _result("refused", "authoritative_read_malformed")
        prepared = records.get("prepared")
        binding = records.get("binding")
        attempt = records.get("attempt")
        if not isinstance(prepared, dict) or not isinstance(binding, dict):
            return _result("refused", "authoritative_snapshot_missing")
        if not isinstance(attempt, dict):
            # The DRAFT SQL grants the verifier role no read path to the
            # attempt row. Fail closed and name the exact seam; never treat
            # a caller dict as a substitute.
            return _result("refused", "missing_seam_verifier_attempt_read",
                           seam=SEAM_ATTEMPT_READ)

        send_return = records.get("send_return")
        if send_return is not None and not isinstance(send_return, dict):
            return _result("refused", "authoritative_read_malformed")
        try:
            ctx = self._validate_records(token, prepared, binding, attempt,
                                         send_return)
        except VerifierInputError as exc:
            return _result("refused", f"authoritative_record_mismatch:{exc}")

        # Self-certification defense in depth, before any provider read or
        # RPC: the verifier identity must be independent of the sender.
        if ctx["attester_id"] and ctx["attester_id"] == self._verifier_id:
            return _result("refused", "self_certification")

        scope = self._check_scope(ctx)
        if scope is not None:
            return scope

        # The known immutable provider post id comes ONLY from the
        # sender's immutable pre-finalization send-return record. When no
        # record exists yet the send is in flight (or never returned):
        # HOLD instead of inferring an id from anywhere else.
        if not ctx["provider_post_id"]:
            return _result("hold", "provider_post_id_unrecorded",
                           hold={"seam": SEAM_POST_ID_STORE})

        # Provider readback of the KNOWN immutable post id. A read failure,
        # 404 or empty read is a HOLD, never absence evidence.
        try:
            raw = self._client.get_post(ctx["provider_post_id"])
        except Exception as exc:
            return _result("hold", "provider_read_failed",
                           hold={"error": type(exc).__name__})
        if not isinstance(raw, dict) or not raw:
            # No inference from absence: a missing/empty readback can never
            # become confirmed_no_send.
            return _result("hold", "provider_readback_missing")

        # Envelope normalization: accept the raw post JSON or a
        # {"post": {...}} envelope; anything else is malformed and refused.
        # The auditable hash is computed over the EXACT captured payload
        # before normalization.
        response_sha256 = canonical_response_sha256(raw)
        envelope = "raw"
        post = raw
        if "post" in raw and isinstance(raw.get("post"), dict):
            envelope = "post_envelope"
            post = raw["post"]
        elif set(raw.keys()) == {"post"}:
            return _result("refused", "malformed_readback_envelope")
        if not isinstance(post, dict) or not post:
            return _result("refused", "malformed_readback")

        # Deterministic evidence only: no wall-clock fields, so a retry that
        # re-reads the same provider state reproduces byte-identical
        # readback_evidence and the SQL replay path returns replayed=true.
        evidence = {
            "adapter": ADAPTER_VERSION,
            "claim_attempt_id": ctx["claim_attempt_id"],
            "provider": PROVIDER,
            "provider_post_id": ctx["provider_post_id"],
            "response_sha256": response_sha256,
            "response_envelope": envelope,
            "missing_seams": [],
        }

        check = self._check_readback(ctx, post, evidence)
        if check is not None:
            check.setdefault("evidence", evidence)
            return check

        outcome = evidence["outcome"]
        return self._attest(ctx, outcome, evidence)

    # -- authoritative record validation ------------------------------------

    def _validate_records(self, token, prepared, binding, attempt,
                          send_return=None):
        def req(row, key, label):
            value = _s(row.get(key))
            if not value:
                raise VerifierInputError(f"missing {label}.{key}")
            return value

        if _s(prepared.get("claim_attempt_id")) != token:
            raise VerifierInputError("prepared claim_attempt_id mismatch")
        if _s(attempt.get("claim_attempt_id")) != token:
            raise VerifierInputError("attempt claim_attempt_id mismatch")

        ctx = {
            "claim_attempt_id": token,
            "tenant_id": req(prepared, "tenant_id", "prepared"),
            "attester_id": _s(prepared.get("attester_id")),
            "expected_provider": req(
                attempt, "expected_provider", "attempt").lower(),
            "expected_channel": req(
                attempt, "expected_channel", "attempt").lower(),
            "expected_provider_account_id": req(
                attempt, "expected_provider_account_id", "attempt"),
            # Terminal attempt post id (null pre-finalization); must
            # agree with the immutable send-return record when both exist.
            "attempt_provider_post_id": _s(attempt.get("provider_post_id")),
            "delivered_url": req(prepared, "delivered_url", "prepared"),
            "delivered_sha256": req(prepared, "delivered_sha256", "prepared"),
            "delivered_md5": req(prepared, "delivered_md5", "prepared"),
            "delivered_byte_length": prepared.get("delivered_byte_length"),
            "delivered_phash": req(prepared, "delivered_phash", "prepared"),
            "canonical_payload_sha256": req(
                prepared, "canonical_payload_sha256", "prepared"),
            # Optional frozen content seams: present only once the snapshot
            # schema stores them. Absent -> explicit missing-seam HOLD on
            # the delivered path, never skipped checks.
            "prepared_caption": prepared.get("prepared_caption"),
            "prepared_media": prepared.get("prepared_media"),
            "binding": {
                "binding_id": _s(binding.get("binding_id")),
                "tenant_id": _s(binding.get("tenant_id")),
                "account_key": _s(binding.get("account_key")),
                "zernio_profile_id": _s(binding.get("zernio_profile_id")),
                "zernio_connected_account_id": _s(
                    binding.get("zernio_connected_account_id")),
                "channel": _s(binding.get("channel")).lower(),
                "destination_page_id": _s(binding.get("destination_page_id")),
                "surface": _s(binding.get("surface")).lower(),
            },
        }
        for key, pattern in (("delivered_sha256", _SHA256_RE),
                             ("canonical_payload_sha256", _SHA256_RE),
                             ("delivered_md5", _MD5_RE),
                             ("delivered_phash", _PHASH_RE)):
            if not pattern.match(ctx[key]):
                raise VerifierInputError(f"malformed prepared.{key}")
        try:
            ctx["delivered_byte_length"] = int(ctx["delivered_byte_length"])
        except (TypeError, ValueError):
            raise VerifierInputError("malformed prepared.delivered_byte_length")
        if ctx["delivered_byte_length"] <= 0:
            raise VerifierInputError("malformed prepared.delivered_byte_length")

        b = ctx["binding"]
        for key in ("binding_id", "tenant_id", "account_key",
                    "zernio_profile_id", "zernio_connected_account_id",
                    "channel", "destination_page_id", "surface"):
            if not b[key]:
                raise VerifierInputError(f"missing binding.{key}")

        # Cross-record consistency, checked BEFORE any provider I/O: the
        # frozen snapshot, its binding and the attempt must agree exactly.
        if _s(prepared.get("binding_id")) != b["binding_id"]:
            raise VerifierInputError("prepared binding_id mismatch")
        if b["tenant_id"] != ctx["tenant_id"]:
            raise VerifierInputError("binding tenant mismatch")
        if _s(attempt.get("tenant_id")) != ctx["tenant_id"]:
            raise VerifierInputError("attempt tenant mismatch")
        if b["channel"] != ctx["expected_channel"]:
            raise VerifierInputError("binding channel mismatch")
        if b["zernio_connected_account_id"] != ctx[
                "expected_provider_account_id"]:
            raise VerifierInputError("binding account mismatch")
        if _s(attempt.get("delivered_url")) != ctx["delivered_url"]:
            raise VerifierInputError("attempt delivered_url mismatch")
        if _s(attempt.get("delivered_md5")) != ctx["delivered_md5"]:
            raise VerifierInputError("attempt delivered_md5 mismatch")
        if _s(attempt.get("delivered_phash")) != ctx["delivered_phash"]:
            raise VerifierInputError("attempt delivered_phash mismatch")

        # Frozen ordered media set: when the snapshot schema supplies it,
        # validate its shape and require the frozen singleton delivered
        # object to be a member (consistency, not proof).
        media = ctx["prepared_media"]
        if media is not None:
            if (not isinstance(media, list) or not media
                    or not all(isinstance(m, dict) for m in media)):
                raise VerifierInputError("malformed prepared.prepared_media")
            normalized = []
            for item in media:
                url = _s(item.get("url"))
                sha = _s(item.get("sha256")).lower()
                md5 = _s(item.get("md5")).lower()
                if md5 and not md5.startswith("md5:"):
                    md5 = "md5:" + md5
                try:
                    length = int(item.get("byte_length"))
                except (TypeError, ValueError):
                    raise VerifierInputError(
                        "malformed prepared.prepared_media byte_length")
                if (not url or not _SHA256_RE.match(sha)
                        or not _MD5_RE.match(md5) or length <= 0):
                    raise VerifierInputError(
                        "malformed prepared.prepared_media entry")
                normalized.append({"url": url, "sha256": sha, "md5": md5,
                                   "byte_length": length})
            frozen = {"url": ctx["delivered_url"],
                      "sha256": ctx["delivered_sha256"],
                      "md5": ctx["delivered_md5"],
                      "byte_length": ctx["delivered_byte_length"]}
            if frozen not in normalized:
                raise VerifierInputError(
                    "prepared_media does not contain the frozen delivered "
                    "object")
            ctx["prepared_media"] = normalized
        caption = ctx["prepared_caption"]
        if caption is not None and not isinstance(caption, str):
            raise VerifierInputError("malformed prepared.prepared_caption")

        # Immutable send-return record: the ONLY source of the provider
        # post id this adapter will read back. Validate its binding to
        # this exact claim and tenant; a terminal attempt post id that
        # disagrees with it is a data-integrity refusal, never an
        # override. Absent record -> post id stays empty and the caller
        # HOLDS (provider_post_id_unrecorded).
        ctx["provider_post_id"] = ""
        if send_return is not None:
            if _s(send_return.get("claim_attempt_id")) != token:
                raise VerifierInputError("send_return claim_attempt_id "
                                         "mismatch")
            if _s(send_return.get("tenant_id")) != ctx["tenant_id"]:
                raise VerifierInputError("send_return tenant mismatch")
            if _s(send_return.get("binding_id")) != b["binding_id"]:
                raise VerifierInputError("send_return binding mismatch")
            sr_post = _s(send_return.get("provider_post_id"))
            if not sr_post:
                raise VerifierInputError("missing "
                                         "send_return.provider_post_id")
            if (ctx["attempt_provider_post_id"]
                    and ctx["attempt_provider_post_id"] != sr_post):
                raise VerifierInputError(
                    "terminal attempt provider_post_id conflicts with the "
                    "immutable send-return record")
            ctx["provider_post_id"] = sr_post
        return ctx

    # -- scope gate ---------------------------------------------------------

    def _check_scope(self, ctx):
        b = ctx["binding"]
        if ctx["expected_provider"] != PROVIDER:
            # Meta-direct (or any non-Zernio) route: nothing this adapter may
            # verify. Refuse, never infer.
            return _result("refused", "non_zernio_provider")
        if b["channel"] not in _CHANNEL_PLATFORMS:
            return _result("refused", "channel_out_of_scope")
        if b["surface"] not in _SURFACES:
            # GBP gallery media uses /v1/accounts/{id}/gmb-media and has no
            # post readback; out of scope by construction.
            return _result("refused", "surface_out_of_scope")
        if b["channel"] == "googlebusiness" and b["surface"] != "feed":
            return _result("refused", "gbp_surface_out_of_scope")
        return None

    # -- readback checks ----------------------------------------------------

    def _check_readback(self, ctx, post, evidence):
        # Identity: the readback must be the exact known post.
        if _s(post.get("_id") or post.get("id")) != ctx["provider_post_id"]:
            return _result("refused", "provider_post_id_changed")
        if _s(post.get("profileId")) != ctx["binding"]["zernio_profile_id"]:
            return _result("refused", "profile_mismatch")

        entries = [e for e in _platform_entries(post)
                   if _s(e.get("platform")).lower()
                   == _CHANNEL_PLATFORMS[ctx["binding"]["channel"]]]
        if not entries:
            return _result("refused", "platform_entry_missing")
        if len(entries) != 1:
            return _result("refused", "duplicate_platform_entries")
        entry = entries[0]
        account_id = entry.get("accountId")
        if isinstance(account_id, dict):
            account_id = account_id.get("_id")
        if _s(account_id) != ctx["expected_provider_account_id"]:
            return _result("refused", "connected_account_mismatch")

        psd = entry.get("platformSpecificData") or {}
        if not isinstance(psd, dict):
            return _result("refused", "malformed_readback")

        # Destination must equal the bound destination.
        dest = ctx["binding"]["destination_page_id"]
        channel = ctx["binding"]["channel"]
        if channel == "facebook":
            if _s(psd.get("pageId")) != dest:
                return _result("refused", "destination_mismatch")
        elif channel == "googlebusiness":
            if _s(psd.get("locationId")) != dest:
                return _result("refused", "destination_mismatch")
            # GBP scope is STANDARD posts only.
            if _s(psd.get("topicType", "STANDARD")).upper() != "STANDARD":
                return _result("refused", "gbp_topic_out_of_scope")
        else:  # instagram: the connected account IS the destination
            if dest != ctx["expected_provider_account_id"]:
                return _result("refused", "destination_mismatch")

        # Surface: story vs feed must match exactly what was prepared.
        is_story = _s(psd.get("contentType")).lower() == "story"
        if (ctx["binding"]["surface"] == "story") != is_story:
            return _result("refused", "surface_mismatch")

        # Terminal status on the exact matching entry (entry status wins;
        # top-level status is the fallback, matching the reconciler).
        status = _s(entry.get("status") or post.get("status")).lower()
        platform_post_id = _s(entry.get("platformPostId"))
        evidence["provider_status"] = status
        evidence["platform_post_id"] = platform_post_id or None

        if status in DELIVERED_STATUSES:
            if not platform_post_id:
                return _result("hold",
                               "ambiguous_status_no_platform_post_id")
            evidence["outcome"] = "delivered"
        else:
            # EVERY non-delivered status HOLDS, never attests. Zernio
            # documents that a failed post may be retried into published and
            # a failed platform entry inside a partial post cannot prove no
            # send; deleted/rejected/partial are equally non-probative.
            # There is no irreversible no-delivery provider contract, so
            # confirmed_no_send is never released from a provider status.
            if status in AMBIGUOUS_STATUSES:
                evidence["missing_seams"].append(SEAM_NO_DELIVERY_CONTRACT)
            return _result("hold", "ambiguous_status",
                           evidence=evidence,
                           hold={"provider_status": status})

        # -- delivered path: frozen content vs authenticated delivered proof --

        # (1) Frozen prepared caption vs the readback content. The DRAFT
        # snapshot freezes only the canonical payload hash, not the caption
        # text: without a frozen caption seam this check cannot run, so HOLD
        # and name it. A readback that omits content is the provider-side
        # seam and also HOLDS. A present-but-different caption REFUSES.
        if ctx["prepared_caption"] is None:
            evidence["missing_seams"].append(
                "prepared snapshot freezes canonical_payload_sha256 only; no "
                "frozen caption text to compare against delivered content")
            return _result("hold", "missing_seam_frozen_caption",
                           evidence=evidence)
        content = post.get("content")
        if not isinstance(content, str):
            evidence["missing_seams"].append(
                "provider readback omits post content; delivered caption "
                "cannot be compared")
            return _result("hold", "missing_seam_provider_content",
                           evidence=evidence)
        if content != ctx["prepared_caption"]:
            return _result("refused", "caption_mismatch")
        evidence["caption_verified"] = True

        # (2) Full ordered media set vs platform-authenticated delivered
        # evidence. mediaItems in the readback are the SUBMITTED request
        # media: hashes there are sender-supplied request fields and are
        # NEVER proof of delivered bytes. Delivered proof requires the
        # platform entry to carry deliveredMedia entries (fetched from the
        # platform post) with url+sha256+md5+byteLength for EVERY media
        # object, in order. Absent -> explicit missing-seam HOLD.
        delivered_media = psd.get("deliveredMedia")
        if (not isinstance(delivered_media, list) or not delivered_media
                or not all(isinstance(m, dict) for m in delivered_media)):
            evidence["missing_seams"].append(SEAM_DELIVERED_MEDIA)
            return _result("hold", "missing_seam_provider_delivered_media",
                           evidence=evidence)
        if ctx["prepared_media"] is None:
            evidence["missing_seams"].append(
                "prepared snapshot freezes a single delivered object only; "
                "no frozen ordered media set to compare in full")
            return _result("hold", "missing_seam_frozen_media_set",
                           evidence=evidence)

        actual = []
        for item in delivered_media:
            url = _s(item.get("url"))
            sha = _s(item.get("sha256")).lower()
            md5 = _s(item.get("md5")).lower()
            if md5 and not md5.startswith("md5:"):
                md5 = "md5:" + md5
            try:
                length = int(item.get("byteLength"))
            except (TypeError, ValueError):
                return _result("refused",
                               "delivered_byte_length_malformed")
            if not url or not _SHA256_RE.match(sha) or length <= 0:
                return _result("refused", "malformed_readback")
            actual.append({"url": url, "sha256": sha, "md5": md5,
                           "byte_length": length})
        if actual != ctx["prepared_media"]:
            # Full ordered-set mismatch: wrong count, wrong order, wrong URL
            # or wrong fingerprint. Never partially accept.
            return _result("refused", "media_identity_mismatch")
        evidence["media_fingerprints_verified"] = actual

        # (3) SEND-RETURN CORRELATION SEAM — final delivered gate. The
        # immutable send-return record is the SENDER's asserted post id,
        # not independently provider-authenticated: no adapter yet
        # captures and binds the authenticated provider send response
        # (request/response correlation) to that record. Even with every
        # content, media, destination, surface and status check green,
        # delivered activation HOLDS here until a real send-return
        # adapter/API correlation contract exists. Never relaxed by
        # delivered media verification success; never a path to attest.
        evidence["missing_seams"].append(SEAM_SEND_RETURN_CORRELATION)
        return _result("hold", "missing_seam_send_return_correlation",
                       evidence=evidence)

    # -- terminal boundary --------------------------------------------------

    def _attest(self, ctx, outcome, evidence):
        """Call attest_terminate ONLY after every check has passed.

        UNREACHABLE TODAY (by design): the send-return correlation seam
        hold in _check_readback precedes this path, because the immutable
        send-return record is sender-asserted, not independently
        provider-authenticated. Retained intact for the future real
        send-return adapter/API correlation contract; do NOT wire around
        the hold without one.

        confirmed_no_send carries provider_post_id=None: the attempt schema
        enforces (state='confirmed_delivered') = (provider_post_id is not
        null), so a nonnull post id on a no-send outcome is invalid. This
        outcome is currently UNREACHABLE: no provider status releases it
        (no irreversible no-delivery contract; see module docstring), and
        the schema invariant is preserved for any future contract.
        """
        payload = {
            "claim_attempt_id": ctx["claim_attempt_id"],
            "outcome": outcome,
            "provider_post_id": (ctx["provider_post_id"]
                                 if outcome == "delivered" else None),
            "verifier_id": self._verifier_id,
            "readback_evidence": evidence,
        }
        try:
            terminal = self._rpc(
                "visual_scene_attester_attest_terminate", payload)
        except Exception as exc:
            return _result("hold", "terminal_result_unverified",
                           evidence=evidence,
                           hold={"error": type(exc).__name__})
        if (not isinstance(terminal, dict)
                or _s(terminal.get("state")) != "finalized"
                or _s(terminal.get("outcome")) != outcome
                or _s(terminal.get("claim_attempt_id"))
                != ctx["claim_attempt_id"]
                or _s(terminal.get("provider_post_id"))
                != _s(payload.get("provider_post_id"))
                or not isinstance(terminal.get("replayed"), bool)):
            return _result("hold", "terminal_result_unverified",
                           evidence=evidence, terminal=terminal)
        return _result(outcome, "attested", evidence=evidence,
                       terminal=terminal)
