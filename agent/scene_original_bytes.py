"""Scene original-use byte evidence (DRAFT / OFF / pure).

Pure byte-evidence helpers for proving that a DELIVERED render actually used a
declared ORIGINAL source image. Companion to docs/SCENE_ORIGINAL_USE_RECEIPT.md
(sibling package echo-scene-original-use-receipt): that SQL package proves
provider delivery; this module proves BYTE LINEAGE — which original bytes the
delivered bytes derive from.

WHAT THIS MODULE CAN AND CANNOT PROVE (read before trusting any result):

  - Byte identity is the ONLY proof available here. If delivered bytes are
    byte-identical to the original bytes (same SHA256), the delivery used the
    original, full stop. That is the only outcome prove_original_use will ever
    mark proven.
  - Byte-DIFFERING deliveries (any render step: crop, resize, recompress) are
    NOT provable by this module. A caller-created RenderEvidence is just two
    digests plus a note — anyone can construct one for any byte pair, and pHash
    similarity is a perceptual heuristic, not a causal link: an edited shot of
    the same scene, or a different but similar photo, can sit within the pHash
    tolerance without being a render of the original. Such evidence is
    structure-checked at most and NEVER proves anything. Differing bytes
    therefore always come back HELD / UNPROVEN — even when presented with a
    perfectly-formed, perfectly-matching RenderEvidence.
  - A trusted-renderer path (a render pipeline that signs its transforms and
    whose signatures this module could verify) does NOT exist yet. Until a
    separately reviewed integration provides one, differing-byte deliveries
    stay held by construction. This is deliberate fail-closed behavior, not a
    gap to route around.

Hard rules baked in:
  - Evidence comes from the BYTES ONLY. No URL fetching, no filesystem reads,
    no network, no I/O of any kind. Caller-supplied metadata (URLs, filenames,
    "this is the original") is never proof — a declared digest is a claim the
    bytes must independently reproduce, and verification FAILS CLOSED.
  - pHash convention: this repo's DCT pHash is agent.vision.dct_phash — a
    64-bit hash rendered as a 16-char lowercase hex string, None when the
    bytes are not a readable image. We reuse that implementation directly so
    lineage phashes compare against every phash already stored by the scene /
    vision stacks. It is exposed for reporting/record-keeping ONLY; it is not
    and cannot be a proof input.

Nothing here is wired to any lane, flag, or DB. OFF by default: every entry
point is a pure function called explicitly by a future, separately reviewed
integration. Naming aligns with the sibling agent/scene_zernio_transport.py
by name only (no import either direction).
"""

from dataclasses import dataclass
import hashlib

# pHash reporting convention, matching the scene stack (agent.vision). Used for
# EVIDENCE RECORDS ONLY — never as a proof input.
from . import vision

PHASH_HAMMING_TOLERANCE = vision.CLUSTER_HAMMING  # 6 — informational only

# Outcome vocabulary for prove_original_use.
PROVEN_IDENTICAL = "proven_identical"      # the ONLY provable outcome
HELD_UNPROVEN = "held_unproven"            # bytes differ: no proof possible here


class ByteEvidenceError(ValueError):
    """Raised on malformed INPUT: missing/empty/unreadable bytes, or a declared
    digest the bytes do not reproduce. Always fails closed. Note: differing
    bytes are NOT an error — they are a held outcome, not an exception."""


# ---- primitives ------------------------------------------------------------
def sha256_hex(data):
    """SHA256 of the supplied bytes as a lowercase hex string. Bytes only —
    rejects None / non-bytes / empty (an empty input has no evidence value and
    is indistinguishable from a missing one)."""
    if not isinstance(data, (bytes, bytearray)):
        raise ByteEvidenceError(
            f"byte evidence requires raw bytes (got {type(data).__name__})")
    if not data:
        raise ByteEvidenceError("byte evidence requires non-empty bytes")
    return hashlib.sha256(bytes(data)).hexdigest()


def md5_hex(data):
    """MD5 of the supplied bytes as a lowercase hex string. REPORTING /
    SECONDARY SIGNAL ONLY: MD5 is collision-broken and is never a proof input
    here — the proof digest is SHA256. A declared MD5 the bytes do not
    reproduce still fails closed (caller metadata is a claim, not proof)."""
    sha256_hex(data)  # shared byte-shape rejection (type/empty)
    return hashlib.md5(bytes(data)).hexdigest()


def phash_hex(data):
    """The repo-convention 64-bit DCT pHash (16-char hex) of the supplied image
    bytes, via agent.vision.dct_phash. Raises ByteEvidenceError when the bytes
    are missing/empty or do not decode as a readable image (dct_phash returns
    None there) — an unreadable 'original' can never be lineage evidence.
    Reporting value ONLY: pHash similarity is not causal proof of rendering."""
    sha256_hex(data)  # shared byte-shape rejection (type/empty)
    h = vision.dct_phash(bytes(data))
    if h is None:
        raise ByteEvidenceError("bytes do not decode as a readable image; "
                                "no pHash evidence possible")
    return h


# ---- evidence objects ------------------------------------------------------
@dataclass(frozen=True)
class ByteEvidence:
    """Computed evidence for ONE byte string: its sha256, repo-convention
    pHash and length. Built ONLY from the bytes themselves."""
    sha256: str
    phash: str
    byte_len: int
    md5: str = ""


@dataclass(frozen=True)
class RenderEvidence:
    """A caller's CLAIM that delivered bytes are a render of the original.

    THIS IS NOT PROOF AND IS NEVER TREATED AS PROOF. It is two digests plus a
    note; any caller can construct one for any byte pair (forgeable by
    construction). validate_render_evidence checks STRUCTURE ONLY. There is no
    trusted renderer in this system yet — until a separately reviewed,
    signature-verifying render pipeline exists, RenderEvidence cannot upgrade a
    differing-byte delivery past HELD_UNPROVEN, no matter how well-formed.

    renderer:   who/what claims to have produced the render (a label).
    original_sha256 / delivered_sha256: the digests the claim connects.
    attestation: free-form provenance note from the claiming lane.
    """
    renderer: str
    original_sha256: str
    delivered_sha256: str
    attestation: str


# ---- validation ------------------------------------------------------------
def build_byte_evidence(data, *, declared_sha256=None, declared_md5=None,
                        declared_byte_len=None):
    """Compute the ByteEvidence for `data` from the ACTUAL BYTES: SHA256,
    MD5, byte count and the repo-convention DCT pHash. Every declared_* value
    is a CLAIM, never proof: the bytes must reproduce each given declared
    value exactly (case-insensitive hex compare for digests) or this fails
    closed with ByteEvidenceError. Also rejects bytes that do not decode as
    a readable image (no pHash possible)."""
    digest = sha256_hex(data)
    md5 = md5_hex(data)
    phash = phash_hex(data)
    if declared_sha256 is not None:
        declared = str(declared_sha256).strip().lower()
        if not declared or declared != digest:
            raise ByteEvidenceError(
                "declared sha256 does not match the supplied bytes "
                "(fail-closed: caller metadata is not proof)")
    if declared_md5 is not None:
        declared = str(declared_md5).strip().lower()
        if not declared or declared != md5:
            raise ByteEvidenceError(
                "declared md5 does not match the supplied bytes "
                "(fail-closed: caller metadata is not proof)")
    if declared_byte_len is not None:
        # Exact-int ONLY: a bool is not a byte count (True != 1 byte), a
        # float would silently truncate, and a string would silently coerce —
        # all three are caller-metadata defects and fail closed.
        if type(declared_byte_len) is not int:
            raise ByteEvidenceError(
                "declared byte count must be an exact int "
                f"(got {type(declared_byte_len).__name__}; bool, float and "
                "string coercion are rejected)")
        if declared_byte_len != len(data):
            raise ByteEvidenceError(
                "declared byte count does not match the supplied bytes "
                "(fail-closed: caller metadata is not proof)")
    return ByteEvidence(sha256=digest, phash=phash, byte_len=len(data),
                        md5=md5)


def validate_render_evidence(render_evidence, *, original, delivered):
    """STRUCTURE-ONLY validation of a RenderEvidence against the computed
    evidence of the actual byte strings: the claim must be well-formed and must
    reference the digests computed from these exact bytes. Raises
    ByteEvidenceError on any structural defect; returns the evidence unchanged
    when structurally sound.

    A structurally valid result PROVES NOTHING about rendering — it says only
    'the claim is internally consistent with these bytes'. It must never be
    used to mark a differing-byte delivery proven; see prove_original_use."""
    if not isinstance(render_evidence, RenderEvidence):
        raise ByteEvidenceError("render evidence must be a RenderEvidence object")
    if not str(render_evidence.renderer or "").strip():
        raise ByteEvidenceError("render evidence requires a renderer label")
    if not str(render_evidence.attestation or "").strip():
        raise ByteEvidenceError("render evidence requires a non-empty attestation")
    if str(render_evidence.original_sha256).strip().lower() != original.sha256:
        raise ByteEvidenceError(
            "render evidence does not reference the computed original sha256")
    if str(render_evidence.delivered_sha256).strip().lower() != delivered.sha256:
        raise ByteEvidenceError(
            "render evidence does not reference the computed delivered sha256")
    return render_evidence


def prove_original_use(original_bytes, delivered_bytes, *,
                       render_evidence=None,
                       declared_original_sha256=None,
                       declared_delivered_sha256=None,
                       declared_original_md5=None,
                       declared_delivered_md5=None,
                       declared_original_byte_len=None,
                       declared_delivered_byte_len=None):
    """Determine whether `delivered_bytes` provably used `original_bytes`.

    Returns a dict:
      {status, proven, identical, original, delivered, render_evidence, reason}

    The ONLY provable outcome is status=PROVEN_IDENTICAL: the byte strings are
    byte-identical (same SHA256), which is self-proving and needs no render
    evidence.

    ANY byte-differing delivery returns status=HELD_UNPROVEN (proven=False),
    unconditionally — including when a well-formed, digest-matching
    RenderEvidence is presented, because caller-created evidence is forgeable
    and pHash similarity is not causal proof. When render_evidence is supplied
    for a differing pair it is structure-checked (validate_render_evidence) and
    attached to the result for the record, but it NEVER changes the outcome.
    A structurally INVALID render evidence raises ByteEvidenceError (fail
    closed on malformed claims).

    Raises ByteEvidenceError on malformed input: missing/empty/unreadable
    bytes, or a declared digest the bytes do not reproduce.
    Pure: no I/O, no network, no flags."""
    original = build_byte_evidence(
        original_bytes, declared_sha256=declared_original_sha256,
        declared_md5=declared_original_md5,
        declared_byte_len=declared_original_byte_len)
    delivered = build_byte_evidence(
        delivered_bytes, declared_sha256=declared_delivered_sha256,
        declared_md5=declared_delivered_md5,
        declared_byte_len=declared_delivered_byte_len)
    identical = original.sha256 == delivered.sha256
    if identical:
        return {"status": PROVEN_IDENTICAL, "proven": True, "identical": True,
                "original": original, "delivered": delivered,
                "render_evidence": None,
                "reason": "byte-identical: sha256 match is self-proving"}

    re_record = None
    if render_evidence is not None:
        # Structure-check the claim for the record. This cannot prove anything;
        # the outcome below is HELD_UNPROVEN regardless of how well it passes.
        re_record = validate_render_evidence(render_evidence,
                                             original=original, delivered=delivered)
    return {"status": HELD_UNPROVEN, "proven": False, "identical": False,
            "original": original, "delivered": delivered,
            "render_evidence": re_record,
            "reason": "delivered bytes differ from the original: no trusted "
                      "renderer exists, caller-supplied render evidence is "
                      "forgeable, and pHash similarity is not causal proof — "
                      "held unproven by construction"}
