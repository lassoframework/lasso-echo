"""Offline tests for the operator lock-attestation producer; no network."""
import base64
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from agent.r2_immutable_media import SignedLockRuleSource, verify_immutable_media
from exact_byte_sign_lock_attestation import (
    AttestationError, FRESHNESS_SECONDS, REHEARSAL_SCHEMA, SCHEMA,
    _load_private_key, attest, build_payload, fetch_lock_readback, main,
    normalize_lock_rules, rules_from_cloudflare_lock_readback, sign_payload,
    validate_serving_evidence,
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "exact_byte_sign_lock_attestation.py"

NOW = 2000000000
ACCOUNT = "account"
BUCKET = "bucket"
BASE = "https://media.example.test"
KEY = "echo-generated-originals/gym/asset.png"
URL = BASE + "/" + KEY
TOKEN = "cf-api-token"

RETENTION_DATE = __import__("datetime").datetime.fromtimestamp(
    NOW + 86400, __import__("datetime").timezone.utc).isoformat()

# Official Cloudflare REST shape:
# GET /accounts/{account_id}/r2/buckets/{bucket_name}/lock -> result.rules
CF_RULES = [{"id": "lock", "prefix": "echo-generated-originals/", "enabled": True,
             "condition": {"type": "Date", "date": RETENTION_DATE}}]
CF_LOCK_BODY = {"success": True, "errors": [], "result": {"rules": CF_RULES}}

RULES = [{"id": "lock", "enabled": True, "prefix": "echo-generated-originals/",
          "protection": "overwrite-and-delete", "retention_until": NOW + 86400}]
PROBE = {"key": KEY, "url": URL, "sha256": "a" * 64, "size_bytes": 5}
SERVING = {"account_id": ACCOUNT, "bucket": BUCKET, "public_base_url": BASE,
           "operator_review": {"reviewer": "operator-blake",
                               "reviewed_at": NOW - 60,
                               "configuration": "controlled-direct-byte-serving"}}


def _keypair():
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        Encoding.Raw, __import__("cryptography.hazmat.primitives.serialization",
                                 fromlist=["PrivateFormat"]).PrivateFormat.Raw,
        __import__("cryptography.hazmat.primitives.serialization",
                   fromlist=["NoEncryption"]).NoEncryption())
    pub = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return seed, pub


def _source(envelope, pub):
    return SignedLockRuleSource(lambda: json.dumps(envelope).encode(), pub)


def _doc(observed=NOW, rules=None, serving=SERVING):
    return build_payload(account_id=ACCOUNT, bucket=BUCKET, public_base_url=BASE,
                         rules=rules if rules is not None else RULES,
                         probe=PROBE, observed_at=observed,
                         serving_evidence=serving)


class S3:
    meta = SimpleNamespace(endpoint_url="https://account.r2.cloudflarestorage.com")

    def __init__(self, data=b"image"):
        self.data = data

    def get_object(self, **kwargs):
        assert kwargs == {"Bucket": BUCKET, "Key": KEY}
        return {"Body": io.BytesIO(self.data), "ContentLength": len(self.data)}

    def get_object_lock_configuration(self, **kwargs):  # never a lock source
        raise AssertionError("S3 Object Lock readback must never be called")

    def put_object(self, **kwargs):  # read-only guard: must never be called
        raise AssertionError("write operation attempted")

    def delete_object(self, **kwargs):
        raise AssertionError("write operation attempted")


class Response:
    status_code = 200
    history = []

    def __init__(self, data=b"image", url=URL, headers=None, json_body=None):
        self.url = url
        self.headers = headers or {}
        self.raw = io.BytesIO(json.dumps(json_body).encode()
                              if json_body is not None else data)
        self.closed = False

    @property
    def content(self):
        raise AssertionError("response body must never be buffered")

    def json(self):
        raise AssertionError("json() must never buffer the body")

    def close(self):
        self.closed = True


class Session:
    """Routes api.cloudflare.com calls to the REST lock readback fake and
    all other URLs to the public byte probe; asserts auth/redirect posture."""

    def __init__(self, response=None, lock_body=None, lock_status=200,
                 lock_response=None):
        self.response = response or Response()
        self.lock_body = CF_LOCK_BODY if lock_body is None else lock_body
        self.lock_status = lock_status
        self.lock_response = lock_response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        if "api.cloudflare.com" in url:
            assert url == ("https://api.cloudflare.com/client/v4/accounts/%s"
                           "/r2/buckets/%s/lock" % (ACCOUNT, BUCKET))
            assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
            resp = self.lock_response or Response(url=url, json_body=self.lock_body)
            resp.status_code = self.lock_status
            return resp
        assert kwargs["headers"]["Accept-Encoding"] == "identity"
        return self.response


def _verify_with_verifier(envelope, pub, now=NOW):
    return verify_immutable_media(
        URL, key=KEY, bucket=BUCKET, account_id=ACCOUNT, public_base_url=BASE,
        s3=S3(), lock_source=_source(envelope, pub),
        required_retention_until=NOW + 600, now=now,
        session=Session(Response()))


def test_signed_attestation_verifies_end_to_end():
    seed, pub = _keypair()
    envelope = sign_payload(_doc(), seed)
    proof = _verify_with_verifier(envelope, pub)
    assert proof.lock_rule_id == "lock"
    assert proof.sha256 == __import__("hashlib").sha256(b"image").hexdigest()


def test_attest_assembles_payload_from_real_readback_and_probe():
    seed, pub = _keypair()
    s3, session = S3(), Session()
    envelope = attest(s3=s3, session=session, account_id=ACCOUNT, bucket=BUCKET,
                      public_base_url=BASE, probe_key=KEY, seed=seed,
                      token=TOKEN, now=NOW, serving_evidence=SERVING)
    doc, _ = _source(envelope, pub).verified_document()
    assert doc["schema"] == "echo-r2-lock-attestation-v1"
    assert doc["public_serving"] == "controlled-direct-byte-serving"
    assert doc["observed_at"] == NOW
    assert doc["expires_at"] == NOW + FRESHNESS_SECONDS == NOW + 300
    assert doc["rules"] == RULES
    assert doc["probe"]["sha256"] == __import__("hashlib").sha256(b"image").hexdigest()
    assert doc["schema"] == SCHEMA
    assert doc["serving_evidence"]["operator_review"]["reviewer"] == "operator-blake"
    # Cloudflare REST lock readback first, then the controlled public probe.
    assert "api.cloudflare.com" in session.calls[0][0]
    assert session.calls[1][0] == URL


def test_cloudflare_rest_readback_maps_date_condition():
    rules = fetch_lock_readback(ACCOUNT, BUCKET, session=Session(), token=TOKEN)
    assert rules == RULES


def test_cloudflare_rest_readback_refuses_age_and_indefinite_conditions():
    for condition in ({"type": "Age", "days": 30},
                      {"type": "Indefinite"},
                      {"type": "Age", "date": RETENTION_DATE},
                      {"type": "Date"},  # absolute date missing
                      {"type": "Date", "date": "not-a-date"},
                      {"type": "Date", "date": "2033-05-18T03:33:20"},  # naive
                      None):
        rule = {"id": "lock", "prefix": "echo-generated-originals/",
                "enabled": True, "condition": condition}
        with pytest.raises(AttestationError):
            rules_from_cloudflare_lock_readback(
                {"success": True, "errors": [], "result": {"rules": [rule]}})


def test_cloudflare_rest_readback_refuses_disabled_or_bad_rules():
    for raw in ({"success": True, "errors": [], "result": {"rules": []}},
                {"success": True, "errors": [], "result": {}},
                {"success": True, "errors": [],
                 "result": {"rules": [{"id": "x", "prefix": "a/", "enabled": False,
                                       "condition": {"type": "Date",
                                                     "date": RETENTION_DATE}}]}},
                {"success": True, "errors": [],
                 "result": {"rules": [{"id": "x", "prefix": "no-trailing-slash",
                                       "enabled": True,
                                       "condition": {"type": "Date",
                                                     "date": RETENTION_DATE}}]}},
                {"success": True, "errors": [], "result": "nope"}, []):
        with pytest.raises(AttestationError):
            rules_from_cloudflare_lock_readback(raw)


def test_cloudflare_rest_readback_fails_closed_on_http_error():
    with pytest.raises(AttestationError):
        fetch_lock_readback(ACCOUNT, BUCKET,
                            session=Session(lock_status=403), token=TOKEN)


def test_attest_refuses_without_api_token():
    seed, _ = _keypair()
    with pytest.raises(AttestationError):
        attest(s3=S3(), session=Session(), account_id=ACCOUNT, bucket=BUCKET,
               public_base_url=BASE, probe_key=KEY, seed=seed, now=NOW,
               serving_evidence=SERVING)


def test_wrong_key_signature_rejected_by_verifier():
    seed, _ = _keypair()
    _, other_pub = _keypair()
    envelope = sign_payload(_doc(), seed)
    with pytest.raises(Exception):
        _source(envelope, other_pub).verified_document()


def test_stale_attestation_rejected_by_verifier():
    seed, pub = _keypair()
    envelope = sign_payload(_doc(observed=NOW - 301), seed)
    with pytest.raises(Exception):
        _verify_with_verifier(envelope, pub)


def test_future_observed_attestation_rejected_by_verifier():
    seed, pub = _keypair()
    envelope = sign_payload(_doc(observed=NOW + 1), seed)
    with pytest.raises(Exception):
        _verify_with_verifier(envelope, pub)


def test_expiry_window_exactly_matches_verifier():
    seed, pub = _keypair()
    # Verifier: 0 <= observed - observed_at <= 300 AND observed < expires_at,
    # so the oldest still-valid attestation at verify time is 299s old.
    ok = sign_payload(_doc(observed=NOW - 299), seed)
    proof = _verify_with_verifier(ok, pub)
    assert proof.observed_at == NOW
    with pytest.raises(Exception):  # 300s old: observed == expires_at, rejected
        _verify_with_verifier(sign_payload(_doc(observed=NOW - 300), seed), pub)


def test_probe_byte_mismatch_refuses_to_sign():
    seed, _ = _keypair()
    with pytest.raises(AttestationError):
        attest(s3=S3(data=b"origin"), session=Session(Response(data=b"public")),
               account_id=ACCOUNT, bucket=BUCKET, public_base_url=BASE,
               probe_key=KEY, seed=seed, token=TOKEN, now=NOW,
               serving_evidence=SERVING)


def test_probe_redirect_or_transform_refuses_to_sign():
    seed, _ = _keypair()

    class Redirect(Response):
        history = ["redirect"]

    with pytest.raises(AttestationError):
        attest(s3=S3(), session=Session(Redirect()), account_id=ACCOUNT,
               bucket=BUCKET, public_base_url=BASE, probe_key=KEY, seed=seed,
               token=TOKEN, now=NOW, serving_evidence=SERVING)
    with pytest.raises(AttestationError):
        attest(s3=S3(), session=Session(Response(headers={"Content-Encoding": "gzip"})),
               account_id=ACCOUNT, bucket=BUCKET, public_base_url=BASE,
               probe_key=KEY, seed=seed, token=TOKEN, now=NOW,
               serving_evidence=SERVING)


def test_age_based_s3_style_rules_unsupported():
    # S3 Object Lock is unsupported by R2 and its shapes are never accepted.
    with pytest.raises(AttestationError):
        normalize_lock_rules([{"id": "x", "enabled": True, "prefix": "a/",
                               "protection": "overwrite-and-delete", "Days": 30}])
    with pytest.raises(AttestationError):
        rules_from_cloudflare_lock_readback({"ObjectLockConfiguration": {
            "ObjectLockEnabled": "Enabled",
            "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 30}}}})


def test_private_key_never_in_output():
    seed, _ = _keypair()
    envelope = sign_payload(_doc(), seed)
    blob = json.dumps(envelope)
    for encoding in (seed.hex(), base64.b64encode(seed).decode()):
        assert encoding not in blob
    doc = json.loads(base64.b64decode(envelope["payload"]))
    assert doc["probe"]["key"] == KEY  # object key, not a credential
    for forbidden in ("private", "signing", "secret", "seed"):
        assert forbidden not in blob
    assert set(envelope) == {"payload", "signature"}


def test_load_private_key_missing_or_malformed():
    with pytest.raises(AttestationError):
        _load_private_key(env={})
    with pytest.raises(AttestationError):
        _load_private_key(env={"ECHO_LOCK_SIGNING_KEY": "not-a-key!!"})
    seed, _ = _keypair()
    assert _load_private_key(env={"ECHO_LOCK_SIGNING_KEY": base64.b64encode(seed).decode()}) == seed
    assert _load_private_key(env={"ECHO_LOCK_SIGNING_KEY": seed.hex()}) == seed


def test_cli_refuses_without_credentials(tmp_path, capsys):
    out = tmp_path / "a.json"
    rc = main(["--account-id", ACCOUNT, "--bucket", BUCKET,
               "--public-base-url", BASE, "--probe-key", KEY, "--out", str(out)],
              env={"ECHO_LOCK_ATTESTATION_ENABLED": "true",
                   "R2_API_TOKEN": TOKEN,
                   "R2_ACCESS_KEY_ID": "x", "R2_SECRET_ACCESS_KEY": "y"})
    assert rc == 2
    assert not out.exists()
    err = capsys.readouterr().err
    assert "refused" in err
    # The refusal summary must never echo any credential value.
    assert '"x"' not in err and '"y"' not in err and TOKEN not in err


def test_cli_refuses_without_api_token(tmp_path, capsys):
    seed, _ = _keypair()
    rc = main(["--account-id", ACCOUNT, "--bucket", BUCKET,
               "--public-base-url", BASE, "--probe-key", KEY,
               "--out", str(tmp_path / "a.json")],
              env={"ECHO_LOCK_ATTESTATION_ENABLED": "true",
                   "ECHO_LOCK_SIGNING_KEY": seed.hex(),
                   "R2_ACCESS_KEY_ID": "x", "R2_SECRET_ACCESS_KEY": "y"})
    assert rc == 2
    assert "token" in capsys.readouterr().err


def test_cli_refuses_when_flag_not_armed(tmp_path):
    seed, _ = _keypair()
    rc = main(["--account-id", ACCOUNT, "--bucket", BUCKET,
               "--public-base-url", BASE, "--probe-key", KEY,
               "--out", str(tmp_path / "a.json")],
              env={"ECHO_LOCK_SIGNING_KEY": seed.hex()})
    assert rc == 2


def test_cli_dry_run_signs_and_verifies(tmp_path, capsys):
    seed, pub = _keypair()
    readback = tmp_path / "readback.json"
    readback.write_text(json.dumps({"lock": CF_LOCK_BODY, "probe": PROBE}))
    out = tmp_path / "att.json"
    rc = main(["--account-id", ACCOUNT, "--bucket", BUCKET,
               "--public-base-url", BASE, "--out", str(out),
               "--dry-run", "--readback-file", str(readback)],
              env={"ECHO_LOCK_SIGNING_KEY": seed.hex()})
    assert rc == 0
    envelope = json.loads(out.read_text())
    doc, _ = _source(envelope, pub).verified_document()
    assert doc["expires_at"] - doc["observed_at"] == 300
    assert doc["rules"] == RULES  # Date condition mapped to absolute epoch
    summary = json.loads(capsys.readouterr().out)
    assert seed.hex() not in json.dumps(summary)
    assert "signature" not in summary and "payload" not in summary


def test_cli_dry_run_refuses_non_absolute_condition(tmp_path):
    seed, _ = _keypair()
    bad = {"lock": {"result": {"rules": [{
        "id": "lock", "prefix": "echo-generated-originals/", "enabled": True,
        "condition": {"type": "Indefinite"}}]}}, "probe": PROBE}
    readback = tmp_path / "readback.json"
    readback.write_text(json.dumps(bad))
    rc = main(["--account-id", ACCOUNT, "--bucket", BUCKET,
               "--public-base-url", BASE, "--out", str(tmp_path / "a.json"),
               "--dry-run", "--readback-file", str(readback)],
              env={"ECHO_LOCK_SIGNING_KEY": seed.hex()})
    assert rc == 2


def test_cli_dry_run_refuses_missing_readback(tmp_path):
    seed, _ = _keypair()
    rc = main(["--account-id", ACCOUNT, "--bucket", BUCKET,
               "--public-base-url", BASE, "--out", str(tmp_path / "a.json"),
               "--dry-run"],
              env={"ECHO_LOCK_SIGNING_KEY": seed.hex()})
    assert rc == 2


def test_cli_subprocess_malformed_readback_fails_closed(tmp_path):
    seed, _ = _keypair()
    readback = tmp_path / "bad.json"
    readback.write_text("not json")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--account-id", ACCOUNT, "--bucket", BUCKET,
         "--public-base-url", BASE, "--out", str(tmp_path / "a.json"),
         "--dry-run", "--readback-file", str(readback)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "ECHO_LOCK_SIGNING_KEY": seed.hex()})
    assert proc.returncode == 2
    assert seed.hex() not in proc.stdout + proc.stderr


# --- strict Cloudflare envelope (P2) -----------------------------------------

def test_cloudflare_envelope_requires_success_true_and_no_errors():
    for body in ({"success": False, "errors": [], "result": {"rules": CF_RULES}},
                 {"success": True, "errors": [{"code": 1000}],
                  "result": {"rules": CF_RULES}},
                 {"errors": [], "result": {"rules": CF_RULES}},  # success absent
                 {"success": True, "result": {"rules": CF_RULES}},  # errors absent
                 {"success": True, "errors": {}, "result": {"rules": CF_RULES}},
                 {"success": True, "errors": [], "result": None}):
        with pytest.raises(AttestationError):
            rules_from_cloudflare_lock_readback(body)


def test_cloudflare_envelope_rejects_unwrapped_rules():
    # Rules without the official success/errors/result envelope are never
    # accepted, even when the rules themselves are perfectly shaped.
    with pytest.raises(AttestationError):
        rules_from_cloudflare_lock_readback({"rules": CF_RULES})


def test_cloudflare_readback_bounded_response_size():
    oversized = Response(url="https://api.cloudflare.com/x")
    oversized.raw = io.BytesIO(b"x" * (65536 + 1))
    with pytest.raises(AttestationError):
        fetch_lock_readback(ACCOUNT, BUCKET,
                            session=Session(lock_response=oversized), token=TOKEN)
    empty = Response(url="https://api.cloudflare.com/x")
    empty.raw = io.BytesIO(b"")
    with pytest.raises(AttestationError):
        fetch_lock_readback(ACCOUNT, BUCKET,
                            session=Session(lock_response=empty), token=TOKEN)
    bad_json = Response(url="https://api.cloudflare.com/x")
    bad_json.raw = io.BytesIO(b"not json")
    with pytest.raises(AttestationError):
        fetch_lock_readback(ACCOUNT, BUCKET,
                            session=Session(lock_response=bad_json), token=TOKEN)


# --- serving evidence prerequisite (P1 serving claim) ------------------------

def test_build_payload_refuses_without_serving_evidence():
    for serving in (None, {}, {"account_id": ACCOUNT}, "controlled"):
        with pytest.raises(AttestationError):
            _doc(serving=serving)


def test_serving_evidence_identity_must_match_pinned_identity():
    for drift in ({"account_id": "other"}, {"bucket": "other"},
                  {"public_base_url": "https://evil.example"}):
        bad = dict(SERVING)
        bad.update(drift)
        with pytest.raises(AttestationError):
            validate_serving_evidence(bad, account_id=ACCOUNT, bucket=BUCKET,
                                      public_base_url=BASE)
    for review in (None, {}, {"reviewer": "", "reviewed_at": NOW,
                              "configuration": "controlled-direct-byte-serving"},
                   {"reviewer": "op", "reviewed_at": 0,
                    "configuration": "controlled-direct-byte-serving"},
                   {"reviewer": "op", "reviewed_at": True,
                    "configuration": "controlled-direct-byte-serving"},
                   {"reviewer": "op", "reviewed_at": NOW,
                    "configuration": "public-bucket-guess"}):
        bad = dict(SERVING, operator_review=review)
        with pytest.raises(AttestationError):
            validate_serving_evidence(bad, account_id=ACCOUNT, bucket=BUCKET,
                                      public_base_url=BASE)


def test_attest_refuses_without_serving_evidence():
    seed, _ = _keypair()
    with pytest.raises(AttestationError):
        attest(s3=S3(), session=Session(), account_id=ACCOUNT, bucket=BUCKET,
               public_base_url=BASE, probe_key=KEY, seed=seed, token=TOKEN,
               now=NOW)


def test_cli_online_refuses_without_serving_evidence_file(tmp_path):
    seed, _ = _keypair()
    rc = main(["--account-id", ACCOUNT, "--bucket", BUCKET,
               "--public-base-url", BASE, "--probe-key", KEY,
               "--out", str(tmp_path / "a.json")],
              env={"ECHO_LOCK_ATTESTATION_ENABLED": "true",
                   "ECHO_LOCK_SIGNING_KEY": seed.hex(),
                   "R2_API_TOKEN": TOKEN,
                   "R2_ACCESS_KEY_ID": "x", "R2_SECRET_ACCESS_KEY": "y"})
    assert rc == 2  # refused before any network or credential use


# --- dry-run rehearsal can never authorize production (P1) -------------------

def test_dry_run_document_is_rehearsal_schema_and_verifier_rejects(tmp_path):
    seed, pub = _keypair()
    readback = tmp_path / "readback.json"
    readback.write_text(json.dumps({"lock": CF_LOCK_BODY, "probe": PROBE}))
    out = tmp_path / "att.json"
    rc = main(["--account-id", ACCOUNT, "--bucket", BUCKET,
               "--public-base-url", BASE, "--out", str(out),
               "--dry-run", "--readback-file", str(readback)],
              env={"ECHO_LOCK_SIGNING_KEY": seed.hex()})
    assert rc == 0
    envelope = json.loads(out.read_text())
    doc = json.loads(base64.b64decode(envelope["payload"]))
    assert doc["schema"] == REHEARSAL_SCHEMA != SCHEMA
    # Signature is valid for the rehearsal key, yet the production verifier
    # must still refuse: rehearsal evidence is never production evidence.
    verified_doc, _ = _source(envelope, pub).verified_document()
    assert verified_doc["schema"] == REHEARSAL_SCHEMA
    # Pin "now" to the document's own freshness window so ONLY the rehearsal
    # schema (never the production schema) is the rejection cause.
    with pytest.raises(Exception):
        _verify_with_verifier(envelope, pub, now=int(__import__("time").time()))


def test_build_payload_rejects_unknown_schema():
    with pytest.raises(AttestationError):
        build_payload(account_id=ACCOUNT, bucket=BUCKET, public_base_url=BASE,
                      rules=RULES, probe=PROBE, observed_at=NOW,
                      serving_evidence=SERVING, schema="echo-r2-lock-attestation-v9")


@pytest.mark.parametrize("body", [b"x" * 1000000, b"", b"invalid json"])
def test_rest_readback_streams_bounded_bytes_and_closes_without_buffering(body):
    class StreamingResponse:
        status_code = 200
        history = []
        closed = False

        def __init__(self):
            self.raw = io.BytesIO(body)

        @property
        def content(self):
            raise AssertionError("response body must never be buffered")

        def json(self):
            raise AssertionError("json() must never buffer the body")

        def close(self):
            self.closed = True

    response = StreamingResponse()
    session = Session(lock_response=response)
    with pytest.raises(AttestationError):
        fetch_lock_readback(ACCOUNT, BUCKET, session=session, token=TOKEN, cap=64)
    assert response.raw.tell() <= 65
    assert response.closed
    assert session.calls[0][1]["stream"] is True
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("status,history", [(302, []), (200, [object()])])
def test_rest_readback_rejects_redirects_without_reading_body(status, history):
    response = Response(json_body=CF_LOCK_BODY)
    response.history = history
    session = Session(lock_response=response, lock_status=status)
    with pytest.raises(AttestationError):
        fetch_lock_readback(ACCOUNT, BUCKET, session=session, token=TOKEN)
    assert response.raw.tell() == 0
    assert response.closed
