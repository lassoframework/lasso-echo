#!/usr/bin/env python3
"""Privileged operator-only producer of signed R2 lock-rule attestations.

Builds a FRESH (<=300s) Ed25519-signed attestation consumed by
``agent/r2_immutable_media.SignedLockRuleSource``. Lock rules are read back
from the official Cloudflare REST endpoint
GET /accounts/{account_id}/r2/buckets/{bucket_name}/lock (bearer token from
env). R2's S3 compatibility layer does NOT support Object Lock, so S3-shaped
lock readback is never used or trusted; the S3 API is used only for the
plain object GET in the byte-identity probe (origin bytes vs. public GET
bytes, byte-identical, no redirects, identity encoding). Nothing is
fabricated.

Hard rules:
  * Read-only against Cloudflare: REST lock GET, S3 object GET, public GET
    only. Never Put/Delete/Lifecycle mutations.
  * The Ed25519 private key arrives at runtime only (env var or key file
    path). It is never printed, logged, committed, or copied into the
    attestation output.
  * Refuses to run (exit 2) when required credentials are absent, when the
    arming flag ECHO_LOCK_ATTESTATION_ENABLED=true is unset, when any
    readback evidence is missing or unsupported (age-based lock rules), or
    when the operator-reviewed serving configuration evidence file
    (--serving-evidence-file, bound to account/bucket/public base URL) is
    absent or mismatched. The Cloudflare REST readback is accepted only as
    the official envelope: success:true, errors:[], wrapped result.rules,
    within a bounded response size.
  * --dry-run performs NO network calls; the operator supplies a lock-rule
    readback JSON file instead. Used by tests and offline rehearsal.
    Dry-run documents are signed under the rehearsal schema
    echo-r2-lock-attestation-v1-rehearsal, which the production
    SignedLockRuleSource can never accept: fabricated rehearsal evidence
    is never production evidence.

Envelope format (exactly what SignedLockRuleSource verifies):
  {"payload": BASE64(json doc), "signature": BASE64(ed25519 over payload bytes)}
Payload doc schema: echo-r2-lock-attestation-v1 with account_id, bucket,
public_base_url, public_serving="controlled-direct-byte-serving",
observed_at, expires_at (= observed_at + 300), rules[] (each: id, enabled,
prefix ending '/', protection="overwrite-and-delete", absolute
retention_until epoch mapped from the REST rule's condition {type: "Date",
date: ISO8601}; Age/Indefinite conditions are refused), probe evidence
(sha256/size/URL of the byte-identical public-vs-origin comparison), and
serving_evidence: the operator-reviewed serving configuration record
(account_id, bucket, public_base_url, operator_review{reviewer,
reviewed_at, configuration}) signed into the document. The byte probe
alone never asserts controlled-direct-byte-serving.

Exit codes: 0 = attestation written, 1 = evidence/probe failure,
2 = refused (missing credentials, flag off, malformed input).
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import sys
import time
from pathlib import Path

SCHEMA = "echo-r2-lock-attestation-v1"
#: Dry-run/rehearsal documents use a distinct schema that the production
#: SignedLockRuleSource can never accept (it pins SCHEMA exactly), so
#: fabricated rehearsal evidence can never authorize a production send.
REHEARSAL_SCHEMA = "echo-r2-lock-attestation-v1-rehearsal"
PUBLIC_SERVING = "controlled-direct-byte-serving"
FRESHNESS_SECONDS = 300  # must equal agent/r2_immutable_media.MAX_FRESHNESS_SECONDS
MAX_PROBE_BYTES = 128 * 1024 * 1024
MAX_ATTESTATION_SOURCE_BYTES = 65536

SIGNING_KEY_ENV = "ECHO_LOCK_SIGNING_KEY"
ENABLE_ENV = "ECHO_LOCK_ATTESTATION_ENABLED"
R2_KEY_ID_ENV = "R2_ACCESS_KEY_ID"
R2_SECRET_ENV = "R2_SECRET_ACCESS_KEY"
R2_API_TOKEN_ENV = "R2_API_TOKEN"


class AttestationError(Exception):
    """Safe fixed reason; provider exceptions and secrets are never included."""


def _load_private_key(*, key_file=None, env=os.environ):
    """Return the 32-byte Ed25519 seed, or raise AttestationError.

    Value source is env var (base64 or hex) or a key file. The key bytes are
    never printed, logged, or included in any output artifact.
    """
    raw = None
    if key_file is not None:
        try:
            raw = Path(key_file).read_bytes().strip()
        except OSError:
            raise AttestationError("signing key file unreadable")
    else:
        raw = env.get(SIGNING_KEY_ENV)
        if raw is None:
            raise AttestationError("signing key absent")
        raw = raw.strip()
    if isinstance(raw, str):
        for decoder in (base64.b64decode, bytes.fromhex):
            try:
                candidate = decoder(raw)
            except (ValueError, binascii.Error):
                continue
            if len(candidate) == 32:
                return candidate
        raise AttestationError("signing key malformed")
    if isinstance(raw, bytes) and len(raw) == 32:
        return raw
    # A key file may hold base64/hex text rather than raw bytes.
    if isinstance(raw, bytes):
        try:
            return _load_private_key(env={SIGNING_KEY_ENV: raw.decode("utf-8").strip()})
        except (UnicodeDecodeError, AttestationError):
            pass
    raise AttestationError("signing key malformed")


def _signer(seed):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(seed)


def normalize_lock_rules(raw_rules):
    """Validate normalized absolute-date lock rules; reject age-based ones.

    raw_rules is a list of dicts with id, enabled, prefix, protection,
    retention_until (absolute epoch). Age-based rules (Days/Years style)
    are unsupported by the verifier and must never be signed.
    """
    if not isinstance(raw_rules, list) or not raw_rules:
        raise AttestationError("enabled retention lock missing")
    rules = []
    for r in raw_rules:
        if not isinstance(r, dict):
            raise AttestationError("lock rule readback malformed")
        if "Days" in r or "Years" in r or "days" in r or "years" in r:
            raise AttestationError("age-based lock rule unsupported")
        rid = r.get("id")
        prefix = r.get("prefix")
        protection = r.get("protection")
        until = r.get("retention_until")
        if (not isinstance(rid, str) or not rid
                or not isinstance(prefix, str) or not prefix.endswith("/")
                or protection != "overwrite-and-delete"
                or not isinstance(until, int) or isinstance(until, bool)
                or r.get("enabled") is not True):
            raise AttestationError("lock rule readback malformed")
        rules.append({"id": rid, "enabled": True, "prefix": prefix,
                      "protection": protection, "retention_until": until})
    return rules


def _absolute_epoch(iso_date):
    from datetime import datetime, timezone
    if not isinstance(iso_date, str):
        raise AttestationError("lock rule readback malformed")
    try:
        parsed = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except ValueError:
        raise AttestationError("lock rule readback malformed")
    if parsed.tzinfo is None:
        raise AttestationError("lock rule readback malformed")
    return int(parsed.astimezone(timezone.utc).timestamp())


def rules_from_cloudflare_lock_readback(body):
    """Normalize the official Cloudflare REST bucket-lock readback.

    Source of truth: GET /accounts/{account_id}/r2/buckets/{bucket_name}/lock
    -> {"result": {"rules": [{id, prefix, enabled,
        condition: {type: "Date"|"Age"|"Indefinite", date: ISO8601}}]}}.
    R2 bucket lock prevents overwrite AND delete, so protection maps to the
    verifier's "overwrite-and-delete". Only absolute-date (type "Date")
    rules are signable; Age/Indefinite conditions are unsupported by the
    verifier and are refused here rather than signed. R2's S3 compatibility
    layer does NOT support Object Lock, so S3-shaped lock readback is never
    accepted.
    """
    if not isinstance(body, dict):
        raise AttestationError("lock rule readback malformed")
    # Official Cloudflare v4 envelope only: success:true, no errors, and a
    # wrapped result object. An unwrapped or error envelope is never evidence.
    if (body.get("success") is not True
            or not isinstance(body.get("errors"), list) or body["errors"]):
        raise AttestationError("lock rule readback envelope invalid")
    result = body.get("result")
    if not isinstance(result, dict):
        raise AttestationError("lock rule readback malformed")
    raw = result.get("rules")
    if not isinstance(raw, list) or not raw:
        raise AttestationError("enabled retention lock missing")
    normalized = []
    for r in raw:
        if not isinstance(r, dict):
            raise AttestationError("lock rule readback malformed")
        condition = r.get("condition")
        if not isinstance(condition, dict):
            raise AttestationError("lock rule readback malformed")
        ctype = condition.get("type")
        if ctype != "Date":
            # Age and Indefinite conditions can never satisfy the verifier's
            # absolute retention_until requirement; refuse to sign them.
            raise AttestationError("non-absolute lock condition unsupported")
        normalized.append({
            "id": r.get("id"),
            "enabled": r.get("enabled"),
            "prefix": r.get("prefix"),
            "protection": "overwrite-and-delete",
            "retention_until": _absolute_epoch(condition.get("date")),
        })
    return normalize_lock_rules(normalized)


def fetch_lock_readback(account_id, bucket, *, session, token,
                        cap=MAX_ATTESTATION_SOURCE_BYTES):
    """Read-only Cloudflare REST GET of the bucket lock rules.

    The bearer token is used only in the request header; it is never
    printed, logged, or included in any output artifact.
    """
    url = ("https://api.cloudflare.com/client/v4/accounts/%s/r2/buckets/%s/lock"
           % (account_id, bucket))
    response = None
    try:
        response = session.get(url, stream=True, allow_redirects=False, timeout=(10, 30),
                               headers={"Authorization": "Bearer " + token,
                                        "Accept": "application/json"})
        if getattr(response, "status_code", None) != 200 or getattr(response, "history", None):
            raise AttestationError("lock rule readback unavailable")
        # stream=True prevents requests from buffering the body. Read only
        # cap+1 bytes from the raw stream; never access .content or .json().
        raw = getattr(response, "raw", None)
        if not callable(getattr(raw, "read", None)):
            raise AttestationError("lock rule readback unavailable")
        content = bytearray()
        while len(content) <= cap:
            chunk = raw.read(min(65536, cap + 1 - len(content)))
            if not isinstance(chunk, bytes):
                raise AttestationError("lock rule readback unavailable")
            if not chunk:
                break
            content.extend(chunk)
        if not 0 < len(content) <= cap:
            raise AttestationError("lock rule readback exceeds size bound")
        try:
            body = json.loads(content)
        except ValueError:
            raise AttestationError("lock rule readback malformed")
        return rules_from_cloudflare_lock_readback(body)
    finally:
        if response is not None and callable(getattr(response, "close", None)):
            response.close()


def _public_probe_bytes(url, *, session, cap=MAX_PROBE_BYTES):
    """Controlled public GET: 200 only, no redirects, identity encoding."""
    response = None
    try:
        response = session.get(url, stream=True, allow_redirects=False,
                               timeout=(10, 30),
                               headers={"Accept-Encoding": "identity"})
        if getattr(response, "status_code", None) != 200 or getattr(response, "history", None):
            raise AttestationError("public object redirects or unavailable")
        headers = getattr(response, "headers", {}) or {}
        if headers.get("Content-Encoding", "").lower() not in ("", "identity"):
            raise AttestationError("public object content encoding unsupported")
        body = response.raw if hasattr(response, "raw") else response
        data = body.read(cap + 1) if callable(getattr(body, "read", None)) else None
        if not isinstance(data, bytes) or not 0 < len(data) <= cap:
            raise AttestationError("public object read unavailable")
        return data
    finally:
        if response is not None and callable(getattr(response, "close", None)):
            response.close()


def _origin_bytes(s3, bucket, key, cap=MAX_PROBE_BYTES):
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"]
    try:
        data = body.read(cap + 1)
    finally:
        body.close()
    if not isinstance(data, bytes) or not 0 < len(data) <= cap:
        raise AttestationError("origin object read unavailable")
    length = obj.get("ContentLength")
    if isinstance(length, int) and not isinstance(length, bool) and length != len(data):
        raise AttestationError("origin content length mismatch")
    return data


def controlled_public_serving_evidence(*, s3, session, bucket, public_base_url, probe_key):
    """Prove the pinned public URL serves the exact origin bytes.

    Returns probe evidence dict; raises AttestationError on any mismatch so a
    public_serving assertion is never signed without controlled proof.
    """
    from urllib.parse import quote
    url = public_base_url + "/" + quote(probe_key, safe="/")
    origin = _origin_bytes(s3, bucket, probe_key)
    public = _public_probe_bytes(url, session=session)
    if public != origin:
        raise AttestationError("public and origin bytes differ")
    return {"key": probe_key, "url": url, "sha256": hashlib.sha256(public).hexdigest(),
            "size_bytes": len(public)}


def validate_serving_evidence(evidence, *, account_id, bucket, public_base_url):
    """Operator-reviewed, pinned serving-configuration evidence.

    One public byte probe can never by itself assert
    controlled-direct-byte-serving; production attestations must also carry
    an explicit operator review record bound to the exact account, bucket,
    and public base URL. The record is included in the signed document, so
    it is cryptographically bound to the rules and probe it vouches for.
    """
    if not isinstance(evidence, dict):
        raise AttestationError("serving configuration evidence absent")
    if (evidence.get("account_id") != account_id
            or evidence.get("bucket") != bucket
            or evidence.get("public_base_url") != public_base_url):
        raise AttestationError("serving evidence identity mismatch")
    review = evidence.get("operator_review")
    if not isinstance(review, dict):
        raise AttestationError("serving evidence operator review absent")
    reviewer = review.get("reviewer")
    reviewed_at = review.get("reviewed_at")
    configuration = review.get("configuration")
    if (not isinstance(reviewer, str) or not reviewer
            or not isinstance(reviewed_at, int) or isinstance(reviewed_at, bool)
            or reviewed_at <= 0
            or configuration != PUBLIC_SERVING):
        raise AttestationError("serving evidence operator review malformed")
    return {"account_id": account_id, "bucket": bucket,
            "public_base_url": public_base_url,
            "operator_review": {"reviewer": reviewer,
                                "reviewed_at": reviewed_at,
                                "configuration": configuration}}


def build_payload(*, account_id, bucket, public_base_url, rules, probe, observed_at,
                  serving_evidence=None, schema=SCHEMA):
    if not all(isinstance(v, str) and v for v in (account_id, bucket, public_base_url)):
        raise AttestationError("attestation identity invalid")
    if not isinstance(observed_at, int) or isinstance(observed_at, bool) or observed_at <= 0:
        raise AttestationError("attestation freshness invalid")
    if schema == SCHEMA:
        # Production documents are impossible without operator-reviewed,
        # identity-bound serving evidence. Fail closed.
        serving_evidence = validate_serving_evidence(
            serving_evidence, account_id=account_id, bucket=bucket,
            public_base_url=public_base_url)
    elif schema != REHEARSAL_SCHEMA:
        raise AttestationError("attestation schema invalid")
    doc = {
        "schema": schema,
        "account_id": account_id,
        "bucket": bucket,
        "public_base_url": public_base_url,
        "public_serving": PUBLIC_SERVING,
        "observed_at": observed_at,
        "expires_at": observed_at + FRESHNESS_SECONDS,
        "rules": rules,
        "probe": probe,
        "serving_evidence": serving_evidence,
    }
    return doc


def sign_payload(doc, seed):
    """Return the exact envelope SignedLockRuleSource verifies."""
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = _signer(seed).sign(payload)
    return {"payload": base64.b64encode(payload).decode("ascii"),
            "signature": base64.b64encode(signature).decode("ascii")}


def attest(*, s3, session, account_id, bucket, public_base_url, probe_key,
           seed, token=None, now=None, rules_reader=None, serving_evidence=None):
    """Full online path: Cloudflare REST lock readback + probe + sign.

    Lock rules come from the official Cloudflare REST bucket-lock endpoint
    (rules_reader / token). The byte-identity probe still reads origin bytes
    via the R2 S3 API (plain object GET is supported there).
    """
    observed = int(time.time()) if now is None else now
    if rules_reader is None:
        if not token:
            raise AttestationError("Cloudflare API token absent")
        rules_reader = lambda: fetch_lock_readback(
            account_id, bucket, session=session, token=token)
    rules = rules_reader()
    if not isinstance(rules, list) or not rules:
        raise AttestationError("enabled retention lock missing")
    probe = controlled_public_serving_evidence(
        s3=s3, session=session, bucket=bucket,
        public_base_url=public_base_url, probe_key=probe_key)
    doc = build_payload(account_id=account_id, bucket=bucket,
                        public_base_url=public_base_url, rules=rules,
                        probe=probe, observed_at=observed,
                        serving_evidence=serving_evidence)
    return sign_payload(doc, seed)


def _redacted_summary(envelope, doc, out_path):
    payload_bytes = base64.b64decode(envelope["payload"])
    return {
        "written": str(out_path),
        "schema": doc["schema"],
        "account_id": doc["account_id"],
        "bucket": doc["bucket"],
        "public_base_url": doc["public_base_url"],
        "observed_at": doc["observed_at"],
        "expires_at": doc["expires_at"],
        "rules": len(doc["rules"]),
        "probe_sha256": doc["probe"]["sha256"],
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }


def _load_serving_evidence(path):
    try:
        evidence = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        raise AttestationError("serving configuration evidence malformed")
    if not isinstance(evidence, dict):
        raise AttestationError("serving configuration evidence malformed")
    return evidence


def _s3_client(account_id, env):
    import boto3  # lazy: not needed for dry-run or tests
    key_id = env.get(R2_KEY_ID_ENV)
    secret = env.get(R2_SECRET_ENV)
    if not key_id or not secret:
        raise AttestationError("R2 read credentials absent")
    return boto3.client(
        "s3", endpoint_url="https://%s.r2.cloudflarestorage.com" % account_id,
        aws_access_key_id=key_id, aws_secret_access_key=secret,
        region_name="auto")


def main(argv=None, env=os.environ):
    parser = argparse.ArgumentParser(
        description="Sign a fresh R2 lock-rule attestation (operator-only, read-only).")
    parser.add_argument("--account-id")
    parser.add_argument("--bucket")
    parser.add_argument("--public-base-url")
    parser.add_argument("--probe-key",
                        help="Object key used for the controlled public byte probe.")
    parser.add_argument("--out", required=True, help="Attestation JSON output path.")
    parser.add_argument("--key-file", help="Path to 32-byte Ed25519 seed (raw/base64/hex).")
    parser.add_argument("--dry-run", action="store_true",
                        help="No network: read lock rules from --readback-file and sign.")
    parser.add_argument("--readback-file",
                        help="Dry-run JSON: {lock: <Cloudflare REST lock body>, "
                             "probe: {key,url,sha256,size_bytes}}.")
    parser.add_argument("--serving-evidence-file",
                        help="Operator-reviewed serving configuration evidence "
                             "JSON: {account_id, bucket, public_base_url, "
                             "operator_review: {reviewer, reviewed_at, "
                             "configuration}}. Required for production signing.")
    args = parser.parse_args(argv)

    try:
        if env.get(ENABLE_ENV) != "true" and not args.dry_run:
            raise AttestationError("attestation producer not armed")
        seed = _load_private_key(key_file=args.key_file, env=env)
        for name in ("account_id", "bucket", "public_base_url"):
            if not getattr(args, name):
                raise AttestationError("attestation identity invalid")

        if args.dry_run:
            if not args.readback_file:
                raise AttestationError("dry-run readback absent")
            try:
                readback = json.loads(Path(args.readback_file).read_text("utf-8"))
            except (OSError, ValueError):
                raise AttestationError("dry-run readback malformed")
            if not isinstance(readback, dict):
                raise AttestationError("dry-run readback malformed")
            rules = rules_from_cloudflare_lock_readback(readback.get("lock"))
            probe = readback.get("probe")
            if not isinstance(probe, dict) or not isinstance(probe.get("sha256"), str):
                raise AttestationError("dry-run probe evidence absent")
            # Rehearsal output uses the rehearsal schema: the production
            # SignedLockRuleSource can never accept it, so fabricated dry-run
            # evidence is harmless by construction.
            serving = None
            if args.serving_evidence_file:
                serving = _load_serving_evidence(args.serving_evidence_file)
            doc = build_payload(account_id=args.account_id, bucket=args.bucket,
                                public_base_url=args.public_base_url, rules=rules,
                                probe=probe, observed_at=int(time.time()),
                                serving_evidence=serving, schema=REHEARSAL_SCHEMA)
            envelope = sign_payload(doc, seed)
        else:
            if not args.probe_key:
                raise AttestationError("probe key absent")
            token = env.get(R2_API_TOKEN_ENV)
            if not token:
                raise AttestationError("Cloudflare API token absent")
            if not args.serving_evidence_file:
                raise AttestationError("serving configuration evidence absent")
            serving = _load_serving_evidence(args.serving_evidence_file)
            import requests
            session = requests.Session()
            session.trust_env = False
            try:
                envelope = attest(
                    s3=_s3_client(args.account_id, env), session=session,
                    account_id=args.account_id, bucket=args.bucket,
                    public_base_url=args.public_base_url,
                    probe_key=args.probe_key, seed=seed, token=token,
                    serving_evidence=serving)
            finally:
                session.close()
            doc = json.loads(base64.b64decode(envelope["payload"]))

        out_path = Path(args.out)
        out_path.write_text(json.dumps(envelope) + "\n", "utf-8")
        print(json.dumps(_redacted_summary(envelope, doc, out_path), sort_keys=True))
        return 0
    except AttestationError as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        # Never leak provider details or secrets; fail closed with a safe reason.
        print(json.dumps({"refused": "attestation unavailable"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
