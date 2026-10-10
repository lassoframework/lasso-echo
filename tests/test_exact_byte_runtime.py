"""Server-owned exact-byte runtime activation and durable proof sink tests."""
import base64
import dataclasses
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent import exact_byte_proof_store as sink_mod
from agent import exact_byte_runtime as runtime
from agent import r2_immutable_media as media
from agent.portal_calendar_store import SupabaseCalendarStore

ACCOUNT = "a" * 32
BUCKET = "echo-media"
BASE = "https://media.example.test"
KEY_BYTES = b"k" * 32  # Ed25519 seed; the pinned public key is derived from it
ADMIN_PUBLIC_KEY = Ed25519PrivateKey.from_private_bytes(
    KEY_BYTES).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
NOW = 2000000000


def signed_attestation_bytes(tmp_path, *, serving=True, identity=None):
    """A validly-signed attestation envelope for the pinned KEY_BYTES admin key."""
    key = Ed25519PrivateKey.from_private_bytes(KEY_BYTES)
    doc = {"schema": "echo-r2-lock-attestation-v1", "account_id": ACCOUNT,
           "bucket": BUCKET, "public_base_url": BASE,
           "public_serving": "controlled-direct-byte-serving",
           "observed_at": NOW, "expires_at": NOW + 300,
           "rules": [{"id": "lock", "enabled": True, "prefix": "media/",
                      "protection": "overwrite-and-delete",
                      "retention_until": NOW + 86400}]}
    if serving:
        bound = dict(account_id=ACCOUNT, bucket=BUCKET, public_base_url=BASE)
        if identity:
            bound.update(identity)
        doc["serving_evidence"] = dict(
            bound, operator_review={"reviewer": "operator-blake",
                                    "reviewed_at": NOW - 60,
                                    "configuration": "controlled-direct-byte-serving"})
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps({"payload": base64.b64encode(payload).decode(),
                       "signature": base64.b64encode(
                           key.sign(payload)).decode()}).encode()


def full_env(tmp_path, **over):
    attestation = tmp_path / "attestation.json"
    attestation.write_bytes(signed_attestation_bytes(tmp_path))
    env = {
        runtime.ENV_ACCOUNT_ID: ACCOUNT,
        runtime.ENV_BUCKET: BUCKET,
        runtime.ENV_PUBLIC_BASE_URL: BASE,
        runtime.ENV_RO_KEY_ID: "readonlykeyid",
        runtime.ENV_RO_SECRET: "readonlysecret",
        runtime.ENV_ADMIN_PUBLIC_KEY: base64.b64encode(ADMIN_PUBLIC_KEY).decode(),
        runtime.ENV_ATTESTATION_PATH: str(attestation),
        runtime.ENV_RETENTION_SECONDS: "600",
        sink_mod.ENV_SINK_MODE: "local",
        sink_mod.ENV_LOCAL_PATH: str(tmp_path / "proofs.jsonl"),
    }
    env.update(over)
    return env


class FakeS3:
    meta = SimpleNamespace(
        endpoint_url=f"https://{ACCOUNT}.r2.cloudflarestorage.com")

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(b"image"), "ContentLength": 5}


class FakeStore:
    """Stands in for the portal store's commit-before-return RPC wrapper."""

    def __init__(self, result=True, fail=None):
        self.result = result
        self.fail = fail
        self.calls = []

    def _reservation_rpc(self, fn, args, timeout=60):
        self.calls.append((fn, args, timeout))
        if self.fail is not None:
            raise self.fail
        return self.result


def make_proof(**over):
    fields = dict(public_url=BASE + "/k.png", account_id=ACCOUNT, bucket=BUCKET,
                  key="k.png", sha256="b" * 64, size_bytes=5, observed_at=NOW,
                  retention_until=NOW + 86400, lock_rule_id="lock",
                  attestation_sha256="c" * 64)
    fields.update(over)
    return media.ImmutableMediaProof(**fields)


# --- runtime activation: env-only, fail closed -----------------------------

def test_full_env_builds_pinned_config(tmp_path):
    env = full_env(tmp_path)
    activation = runtime.activate(env, s3_client=FakeS3())
    assert not activation.guard_holds and activation.hold_reason == ""
    config = activation.verifier_config
    assert config["bucket"] == BUCKET and config["account_id"] == ACCOUNT
    assert config["public_base_url"] == BASE and config["retention_seconds"] == 600
    assert isinstance(config["lock_source"], media.SignedLockRuleSource)
    assert isinstance(config["proof_sink"], sink_mod.LocalJsonlProofSink)
    assert config["s3"].meta.endpoint_url == (
        f"https://{ACCOUNT}.r2.cloudflarestorage.com")


def test_row_controlled_values_cannot_influence_config(tmp_path):
    env = full_env(tmp_path)
    # Row/tenant-shaped keys, and even attacker-chosen duplicates of the real
    # names smuggled under a row dict, are simply never read.
    row = {"account_id": "0" * 32, "bucket": "attacker",
           "public_base_url": "https://evil.example", "r2_secret": "x"}
    env.update({f"ROW_{k}": v for k, v in row.items()})
    env["account_id"] = "0" * 32  # not a server-pinned name: ignored
    config = runtime.activate(env, s3_client=FakeS3()).verifier_config
    assert config["account_id"] == ACCOUNT and config["bucket"] == BUCKET
    assert config["public_base_url"] == BASE


def test_rpc_sink_mode_requires_store(tmp_path):
    env = full_env(tmp_path, **{sink_mod.ENV_SINK_MODE: "rpc"})
    assert runtime.activate(env, s3_client=FakeS3()).guard_holds
    store = FakeStore()
    config = runtime.activate(env, store=store, s3_client=FakeS3()).verifier_config
    assert isinstance(config["proof_sink"], sink_mod.RpcProofSink)


@pytest.mark.parametrize("missing", [
    runtime.ENV_ACCOUNT_ID, runtime.ENV_BUCKET, runtime.ENV_PUBLIC_BASE_URL,
    runtime.ENV_RO_KEY_ID, runtime.ENV_RO_SECRET, runtime.ENV_ADMIN_PUBLIC_KEY,
    runtime.ENV_ATTESTATION_PATH, runtime.ENV_RETENTION_SECONDS,
    sink_mod.ENV_SINK_MODE])
def test_missing_env_holds_closed(tmp_path, missing, monkeypatch):
    monkeypatch.setattr(runtime, "_r2_read_only_client", lambda *a: FakeS3())
    env = full_env(tmp_path)
    env.pop(missing)
    activation = runtime.activate(env)
    assert activation.guard_holds and activation.verifier_config is None
    assert activation.hold_reason


@pytest.mark.parametrize("name,value", [
    (runtime.ENV_ACCOUNT_ID, "not-hex"),
    (runtime.ENV_ACCOUNT_ID, "A" * 32),
    (runtime.ENV_BUCKET, "Bad_Bucket"),
    (runtime.ENV_PUBLIC_BASE_URL, "http://media.example.test"),
    (runtime.ENV_PUBLIC_BASE_URL, "https://media.example.test/"),
    (runtime.ENV_PUBLIC_BASE_URL, "https://user:pw@media.example.test"),
    (runtime.ENV_ADMIN_PUBLIC_KEY, base64.b64encode(b"short").decode()),
    (runtime.ENV_ADMIN_PUBLIC_KEY, "!!!not-base64!!!"),
    (runtime.ENV_RETENTION_SECONDS, "0"),
    (runtime.ENV_RETENTION_SECONDS, "-5"),
    (runtime.ENV_RETENTION_SECONDS, "99999999"),
    (runtime.ENV_RETENTION_SECONDS, "soon"),
    (runtime.ENV_ATTESTATION_PATH, "relative/path.json"),
    (sink_mod.ENV_SINK_MODE, "sideways"),
])
def test_malformed_env_holds_closed(tmp_path, name, value):
    env = full_env(tmp_path, **{name: value})
    assert runtime.activate(env, s3_client=FakeS3()).guard_holds


def test_activate_never_raises():
    assert runtime.activate({"x": None}).guard_holds  # non-str values
    assert runtime.activate(object()).guard_holds  # not even a mapping
    assert runtime.activate({}).guard_holds
    assert runtime.verifier_config_or_none({}) is None


def test_store_hook_checks_local_attestation_at_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_r2_read_only_client", lambda *a: FakeS3())
    env = full_env(tmp_path)
    attestation = Path(env[runtime.ENV_ATTESTATION_PATH])
    attestation.unlink()
    assert runtime.verifier_config_or_none(env) is None
    attestation.write_bytes(signed_attestation_bytes(tmp_path))
    assert runtime.verifier_config_or_none(env) is not None


def test_sink_failure_holds_adapter_decision(tmp_path, monkeypatch):
    import time as _time
    env = full_env(tmp_path)

    def sink(_proof):
        raise RuntimeError("db down")

    config = runtime.activate(env, proof_sink=sink, s3_client=FakeS3()).verifier_config
    assert config is not None
    now = int(_time.time())
    doc = {"schema": "echo-r2-lock-attestation-v1", "account_id": ACCOUNT,
           "bucket": BUCKET, "public_base_url": BASE,
           "public_serving": "controlled-direct-byte-serving",
           "observed_at": now, "expires_at": now + 300,
           "rules": [{"id": "lock", "enabled": True, "prefix": "media/",
                      "protection": "overwrite-and-delete",
                      "retention_until": now + 86400}]}
    key = Ed25519PrivateKey.generate()
    payload = json.dumps(doc).encode()
    envelope = json.dumps({"payload": base64.b64encode(payload).decode(),
                           "signature": base64.b64encode(key.sign(payload)).decode()}).encode()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    config["lock_source"] = media.SignedLockRuleSource(lambda: envelope, pub)
    monkeypatch.setattr(media, "_public_bytes", lambda *a, **k: b"image")
    monkeypatch.setattr(_time, "time", lambda: now)
    verifier = media.trusted_bool_adapter(**config)
    # Verification itself succeeds, but the durable sink raises: the adapter
    # must convert that to a False (held) decision, never an exception.
    assert verifier(BASE + "/media/k.png", b"image") is False


# --- proof sink durability and append-only behavior ------------------------

def test_local_sink_fsyncs_every_append(tmp_path, monkeypatch):
    fsyncs = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync",
                        lambda fd: (fsyncs.append(fd), real_fsync(fd))[1])
    sink = sink_mod.LocalJsonlProofSink(str(tmp_path / "proofs.jsonl"))
    sink(make_proof())
    sink(make_proof(key="other.png", sha256="d" * 64))
    assert len(fsyncs) == 2


def test_local_sink_is_append_only(tmp_path):
    path = tmp_path / "proofs.jsonl"
    sink = sink_mod.LocalJsonlProofSink(str(path))
    first = make_proof()
    sink(first)
    before = path.read_text()
    sink(make_proof(key="other.png", sha256="d" * 64))
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] == before.strip()
    assert json.loads(lines[0])["sha256"] == "b" * 64
    # No mutation API exists on the sink at all.
    assert not hasattr(sink, "delete") and not hasattr(sink, "update")


def test_local_sink_rejects_non_proof_and_bad_path(tmp_path):
    with pytest.raises(sink_mod.ProofSinkError):
        sink_mod.LocalJsonlProofSink("relative.jsonl")
    sink = sink_mod.LocalJsonlProofSink(str(tmp_path / "p.jsonl"))
    with pytest.raises(sink_mod.ProofSinkError):
        sink({"not": "a proof"})
    assert not (tmp_path / "p.jsonl").exists()


def test_rpc_sink_appends_through_store():
    store = FakeStore()
    sink = sink_mod.RpcProofSink(store)
    sink(make_proof())
    fn, args, _ = store.calls[0]
    assert fn == sink_mod.PROOF_RPC
    record = args["p_proof"]
    assert record["sha256"] == "b" * 64 and record["key"] == "k.png"
    assert "proof_id" not in record and "recorded_at" not in record


def test_rpc_sink_pinned_and_fail_closed():
    store = FakeStore()
    with pytest.raises(sink_mod.ProofSinkError):
        sink_mod.RpcProofSink(store, rpc="some_other_rpc")
    with pytest.raises(sink_mod.ProofSinkError):
        sink_mod.RpcProofSink(object())  # no _reservation_rpc
    with pytest.raises(sink_mod.ProofSinkError):
        sink_mod.RpcProofSink(FakeStore(result={"ok": True}))(make_proof())
    with pytest.raises(sink_mod.ProofSinkError):
        sink_mod.RpcProofSink(FakeStore(fail=RuntimeError("lost")))(make_proof())


def test_sink_env_factory_fail_closed(tmp_path):
    assert sink_mod.proof_sink_from_env({}) is None
    assert sink_mod.proof_sink_from_env(
        {sink_mod.ENV_SINK_MODE: "local"}) is None  # no path
    assert sink_mod.proof_sink_from_env(
        {sink_mod.ENV_SINK_MODE: "rpc"}) is None  # no store
    assert sink_mod.proof_sink_from_env(
        {sink_mod.ENV_SINK_MODE: "bogus"}) is None
    sink = sink_mod.proof_sink_from_env(
        {sink_mod.ENV_SINK_MODE: "local",
         sink_mod.ENV_LOCAL_PATH: str(tmp_path / "p.jsonl")})
    assert isinstance(sink, sink_mod.LocalJsonlProofSink)


# --- portal store hook ------------------------------------------------------

def _store_env(monkeypatch, flag):
    monkeypatch.setenv("AGENT_EXACT_BYTE_SEND_GUARD", flag)


def test_store_hook_inert_when_guard_off(monkeypatch):
    _store_env(monkeypatch, "")
    store = SupabaseCalendarStore(url="https://db.example", service_key="k",
                                  http=object())
    assert store._immutable_media_verifier_config is None


def test_store_hook_arms_from_env_only(monkeypatch, tmp_path):
    _store_env(monkeypatch, "true")
    # Guard armed: production must use the durable RPC proof sink; the local
    # JSONL mode is rejected while AGENT_EXACT_BYTE_SEND_GUARD=true.
    env = full_env(tmp_path, **{sink_mod.ENV_SINK_MODE: "rpc"})
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(runtime, "_r2_read_only_client",
                        lambda *a: FakeS3())
    store = SupabaseCalendarStore(url="https://db.example", service_key="k",
                                  http=object())
    config = store._immutable_media_verifier_config
    assert config["account_id"] == ACCOUNT and config["bucket"] == BUCKET
    assert isinstance(config["proof_sink"], sink_mod.RpcProofSink)


def test_store_hook_holds_on_missing_env(monkeypatch):
    _store_env(monkeypatch, "true")
    for name in (runtime.ENV_ACCOUNT_ID, runtime.ENV_BUCKET):
        monkeypatch.delenv(name, raising=False)
    store = SupabaseCalendarStore(url="https://db.example", service_key="k",
                                  http=object())
    assert store._immutable_media_verifier_config is None


# --- migration SQL sanity ---------------------------------------------------

def test_migration_is_append_only_and_unapplied():
    sql = (Path(__file__).resolve().parents[1]
           / "migrations" / "exact_byte_proof_sink_20261010.sql").read_text()
    lowered = sql.lower()
    assert "unapplied" in lowered and "default off" in lowered
    assert "exact_byte_proof_sink_20261010" in sql
    assert "exact_byte_proof_append_20261010" in sql
    # Append-only: triggers block row update/delete and truncate; the table
    # grants no direct write; the RPC is executable by service_role only.
    assert "before update or delete on public.exact_byte_proof_sink_20261010" in lowered
    assert "before truncate on public.exact_byte_proof_sink_20261010" in lowered
    assert "revoke all on public.exact_byte_proof_sink_20261010" in lowered
    assert "grant execute on function public.exact_byte_proof_append_20261010(jsonb)\n  to service_role" in lowered
    assert "grant insert" not in lowered
    assert "drop table" not in lowered and "delete from" not in lowered
    assert "update public.exact_byte_proof_sink_20261010" not in lowered


# --- production sink selection (P1: durable RPC only when guard armed) -------

def test_local_sink_rejected_when_send_guard_armed(tmp_path):
    env = {sink_mod.ENV_SINK_MODE: "local",
           sink_mod.ENV_LOCAL_PATH: str(tmp_path / "p.jsonl"),
           sink_mod.ENV_SEND_GUARD: "true"}
    assert sink_mod.proof_sink_from_env(env) is None
    # Case/whitespace variants of the armed flag still reject local.
    env[sink_mod.ENV_SEND_GUARD] = " TRUE "
    assert sink_mod.proof_sink_from_env(env) is None
    # Unarmed guard keeps the dev/test local sink available.
    env[sink_mod.ENV_SEND_GUARD] = ""
    assert isinstance(sink_mod.proof_sink_from_env(env),
                      sink_mod.LocalJsonlProofSink)


def test_runtime_holds_with_local_sink_when_guard_armed(tmp_path):
    env = full_env(tmp_path, **{sink_mod.ENV_SEND_GUARD: "true"})
    activation = runtime.activate(env, s3_client=FakeS3())
    assert activation.guard_holds and activation.hold_reason
    # The same env with the durable RPC sink mode activates.
    env[sink_mod.ENV_SINK_MODE] = "rpc"
    config = runtime.activate(env, store=FakeStore(),
                              s3_client=FakeS3()).verifier_config
    assert isinstance(config["proof_sink"], sink_mod.RpcProofSink)


# --- operator-reviewed serving evidence prerequisite --------------------------

def test_activation_holds_without_signed_serving_evidence(tmp_path):
    env = full_env(tmp_path)
    (tmp_path / "attestation.json").write_bytes(
        signed_attestation_bytes(tmp_path, serving=False))
    activation = runtime.activate(env, s3_client=FakeS3())
    assert activation.guard_holds and "serving evidence" in activation.hold_reason


def test_activation_holds_on_serving_evidence_identity_drift(tmp_path):
    for drift in ({"account_id": "b" * 32}, {"bucket": "other-bucket"},
                  {"public_base_url": "https://evil.example"}):
        env = full_env(tmp_path)
        (tmp_path / "attestation.json").write_bytes(
            signed_attestation_bytes(tmp_path, identity=drift))
        activation = runtime.activate(env, s3_client=FakeS3())
        assert activation.guard_holds and activation.hold_reason


def test_activation_holds_on_unsigned_or_rehearsal_attestation(tmp_path):
    env = full_env(tmp_path)
    (tmp_path / "attestation.json").write_bytes(b"{}")  # unsigned garbage
    assert runtime.activate(env, s3_client=FakeS3()).guard_holds
    # Rehearsal schema can never activate a production lane.
    key = Ed25519PrivateKey.from_private_bytes(KEY_BYTES)
    doc = json.loads(base64.b64decode(json.loads(
        signed_attestation_bytes(tmp_path))["payload"]))
    doc["schema"] = "echo-r2-lock-attestation-v1-rehearsal"
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    (tmp_path / "attestation.json").write_bytes(json.dumps(
        {"payload": base64.b64encode(payload).decode(),
         "signature": base64.b64encode(key.sign(payload)).decode()}).encode())
    assert runtime.activate(env, s3_client=FakeS3()).guard_holds


@pytest.mark.parametrize("flag", ["1", "true", "yes", "on", "TRUE", " YES "])
def test_all_armed_guard_forms_reject_local_sink(tmp_path, monkeypatch, flag):
    from agent import delivered_byte_send_guard as guard
    monkeypatch.setenv(guard.FLAG, flag.strip())
    assert guard.enabled()
    env = full_env(tmp_path, **{sink_mod.ENV_SEND_GUARD: flag})
    assert sink_mod.proof_sink_from_env(env) is None
    assert runtime.activate(env, s3_client=FakeS3()).guard_holds


@pytest.mark.parametrize("drift", [
    "missing", "legacy", "account", "bucket", "base", "review_missing",
    "reviewer_missing", "review_time_bool", "review_time_zero", "configuration",
])
def test_per_send_reload_rejects_signed_serving_evidence_drift(
        tmp_path, monkeypatch, drift):
    env = full_env(tmp_path)
    proofs = []
    config = runtime.activate(env, s3_client=FakeS3(),
                              proof_sink=proofs.append).verifier_config
    assert config is not None
    monkeypatch.setattr(media.time, "time", lambda: NOW)
    monkeypatch.setattr(media, "_public_bytes", lambda *a, **k: b"image")
    verifier = media.trusted_bool_adapter(**config)
    assert isinstance(verifier(BASE + "/media/k.png", b"image"),
                      media.ImmutableMediaEvidence)
    assert len(proofs) == 1

    doc = json.loads(base64.b64decode(json.loads(
        signed_attestation_bytes(tmp_path))["payload"]))
    evidence = doc["serving_evidence"]
    if drift == "missing":
        del doc["serving_evidence"]
    elif drift == "legacy":
        doc["serving_evidence"] = "controlled-direct-byte-serving"
    elif drift in ("account", "bucket", "base"):
        field = {"account": "account_id", "bucket": "bucket",
                 "base": "public_base_url"}[drift]
        evidence[field] = "other"
    elif drift == "review_missing":
        del evidence["operator_review"]
    elif drift == "reviewer_missing":
        evidence["operator_review"]["reviewer"] = ""
    elif drift == "review_time_bool":
        evidence["operator_review"]["reviewed_at"] = True
    elif drift == "review_time_zero":
        evidence["operator_review"]["reviewed_at"] = 0
    elif drift == "configuration":
        evidence["operator_review"]["configuration"] = "transformed"
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    key = Ed25519PrivateKey.from_private_bytes(KEY_BYTES)
    path = tmp_path / "attestation.json"
    path.write_bytes(json.dumps({
        "payload": base64.b64encode(payload).decode(),
        "signature": base64.b64encode(key.sign(payload)).decode()}).encode())
    # This is a fresh, validly signed replacement, so only the serving
    # contract can reject it. No additional durable proof may authorize send.
    assert verifier(BASE + "/media/k.png", b"image") is False
    assert len(proofs) == 1

    # A subsequently repaired signed document is loaded afresh as well.
    path.write_bytes(signed_attestation_bytes(tmp_path))
    assert isinstance(verifier(BASE + "/media/k.png", b"image"),
                      media.ImmutableMediaEvidence)
    assert len(proofs) == 2
