"""Exact-byte attestation transport: remote control reader + signer job."""
import base64
import io
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, PublicFormat, NoEncryption)

from agent import exact_byte_attestation_job as job
from agent import exact_byte_proof_store as sink_mod
from agent import exact_byte_runtime as runtime
from agent import r2_immutable_media as media

ACCOUNT = "a" * 32
CONTROL_ACCOUNT = "c" * 32
BUCKET = "echo-media"
CONTROL_BUCKET = "echo-control"
CONTROL_KEY = "attestation/current.json"
BASE = "https://media.example.test"
KEY_BYTES = b"k" * 32
PRIVATE = Ed25519PrivateKey.from_private_bytes(KEY_BYTES)
PUBLIC = PRIVATE.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
SEED_B64 = base64.b64encode(KEY_BYTES).decode()
NOW = 2000000000

RULES = [{"id": "lock", "enabled": True, "prefix": "media/",
          "protection": "overwrite-and-delete", "retention_until": NOW + 86400}]
PROBE = {"key": "media/k.png", "url": BASE + "/media/k.png",
         "sha256": "a" * 64, "size_bytes": 5}
SERVING = {"account_id": ACCOUNT, "bucket": BUCKET, "public_base_url": BASE,
           "operator_review": {"reviewer": "operator-blake",
                               "reviewed_at": NOW - 60,
                               "configuration": "controlled-direct-byte-serving"}}


def envelope_bytes(now=NOW, doc_over=None):
    doc = {"schema": "echo-r2-lock-attestation-v1", "account_id": ACCOUNT,
           "bucket": BUCKET, "public_base_url": BASE,
           "public_serving": "controlled-direct-byte-serving",
           "observed_at": now, "expires_at": now + 300, "rules": RULES,
           "probe": PROBE, "serving_evidence": SERVING}
    if doc_over:
        doc.update(doc_over)
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps({"payload": base64.b64encode(payload).decode(),
                       "signature": base64.b64encode(
                           PRIVATE.sign(payload)).decode()}).encode()


class ControlS3:
    """Fake control-bucket client with injectable failure modes."""

    def __init__(self, data=None, fail_get=None, fail_put=None,
                 length_over=None, truncate=0):
        self.data = data
        self.fail_get = fail_get
        self.fail_put = fail_put
        self.length_over = length_over
        self.truncate = truncate
        self.gets = 0
        self.puts = []

    def get_object(self, Bucket, Key):
        self.gets += 1
        if self.fail_get is not None:
            raise self.fail_get
        data = self.data if self.data is not None else envelope_bytes()
        length = (len(data) if self.length_over is None else self.length_over)
        if self.truncate:
            data = data[:-self.truncate]
        return {"Body": io.BytesIO(data), "ContentLength": length}

    def put_object(self, Bucket, Key, Body):
        if self.fail_put is not None:
            raise self.fail_put
        self.puts.append((Bucket, Key, bytes(Body)))
        self.data = bytes(Body)
        return {}


def control_env(**over):
    env = {
        runtime.ENV_ACCOUNT_ID: ACCOUNT,
        runtime.ENV_BUCKET: BUCKET,
        runtime.ENV_PUBLIC_BASE_URL: BASE,
        runtime.ENV_RO_KEY_ID: "media-ro-id",
        runtime.ENV_RO_SECRET: "media-ro-secret",
        runtime.ENV_ADMIN_PUBLIC_KEY: base64.b64encode(PUBLIC).decode(),
        runtime.ENV_RETENTION_SECONDS: "600",
        runtime.ENV_ATTESTATION_SOURCE: "r2-control",
        runtime.ENV_CONTROL_ACCOUNT_ID: CONTROL_ACCOUNT,
        runtime.ENV_CONTROL_BUCKET: CONTROL_BUCKET,
        runtime.ENV_CONTROL_KEY: CONTROL_KEY,
        runtime.ENV_CONTROL_RO_KEY_ID: "control-ro-id",
        runtime.ENV_CONTROL_RO_SECRET: "control-ro-secret",
        sink_mod.ENV_SINK_MODE: "local",
        sink_mod.ENV_LOCAL_PATH: "/tmp/proofs.jsonl",
    }
    env.update(over)
    return env


# --- remote reader ----------------------------------------------------------

def test_remote_source_activates_and_refetches_per_send(monkeypatch, tmp_path):
    monkeypatch.setattr(media.time, "time", lambda: NOW)
    monkeypatch.setattr(media, "_public_bytes", lambda *a, **k: b"image")
    control = ControlS3()
    env = control_env(**{sink_mod.ENV_LOCAL_PATH: str(tmp_path / "p.jsonl")})
    config = runtime.activate(env, s3_client=FakeMediaS3(),
                              control_s3_client=control).verifier_config
    assert config is not None
    assert control.gets == 0  # lazy: no fetch at activation
    verifier = media.trusted_bool_adapter(**config)
    assert isinstance(verifier(BASE + "/media/k.png", b"image"),
                      media.ImmutableMediaEvidence)
    assert isinstance(verifier(BASE + "/media/k.png", b"image"),
                      media.ImmutableMediaEvidence)
    assert control.gets == 2  # fresh fetch on every verification


class FakeMediaS3:
    meta = SimpleNamespace(
        endpoint_url=f"https://{ACCOUNT}.r2.cloudflarestorage.com")

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(b"image"), "ContentLength": 5}


def test_remote_valid_refresh_readback(tmp_path, monkeypatch):
    monkeypatch.setattr(media.time, "time", lambda: NOW)
    monkeypatch.setattr(media, "_public_bytes", lambda *a, **k: b"image")
    control = ControlS3()
    env = control_env(**{sink_mod.ENV_LOCAL_PATH: str(tmp_path / "p.jsonl")})
    config = runtime.activate(env, s3_client=FakeMediaS3(),
                              control_s3_client=control).verifier_config
    verifier = media.trusted_bool_adapter(**config)
    evidence = verifier(BASE + "/media/k.png", b"image")
    assert isinstance(evidence, media.ImmutableMediaEvidence)
    assert evidence.proof.lock_rule_id == "lock"
    assert len(evidence.proof.attestation_sha256) == 64


@pytest.mark.parametrize("mode", ["missing", "denied", "timeout",
                                  "truncated", "oversized"])
def test_remote_transport_failures_hold(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(media.time, "time", lambda: NOW)
    monkeypatch.setattr(media, "_public_bytes", lambda *a, **k: b"image")
    if mode == "missing":
        control = ControlS3(fail_get=RuntimeError("NoSuchKey"))
    elif mode == "denied":
        control = ControlS3(fail_get=RuntimeError("AccessDenied"))
    elif mode == "timeout":
        control = ControlS3(fail_get=TimeoutError("read timed out"))
    elif mode == "truncated":
        control = ControlS3(truncate=4)
    else:
        control = ControlS3(length_over=media.MAX_ATTESTATION_BYTES + 1)
    env = control_env(**{sink_mod.ENV_LOCAL_PATH: str(tmp_path / "p.jsonl")})
    config = runtime.activate(env, s3_client=FakeMediaS3(),
                              control_s3_client=control).verifier_config
    assert config is not None  # config complete; verification must hold
    verifier = media.trusted_bool_adapter(**config)
    assert verifier(BASE + "/media/k.png", b"image") is False


@pytest.mark.parametrize("doc_over", [
    {"schema": "echo-r2-lock-attestation-v1-rehearsal"},
    {"account_id": "b" * 32},
    {"bucket": "other-bucket"},
    {"public_base_url": "https://evil.example"},
    {"public_serving": "transformed"},
    {"observed_at": NOW - 301},                      # stale
    {"observed_at": NOW + 301, "expires_at": NOW + 601},  # future
    {"serving_evidence": None},
])
def test_remote_bad_document_holds(tmp_path, monkeypatch, doc_over):
    monkeypatch.setattr(media.time, "time", lambda: NOW)
    monkeypatch.setattr(media, "_public_bytes", lambda *a, **k: b"image")
    control = ControlS3(data=envelope_bytes(doc_over=doc_over))
    env = control_env(**{sink_mod.ENV_LOCAL_PATH: str(tmp_path / "p.jsonl")})
    config = runtime.activate(env, s3_client=FakeMediaS3(),
                              control_s3_client=control).verifier_config
    verifier = media.trusted_bool_adapter(**config)
    assert verifier(BASE + "/media/k.png", b"image") is False


def test_remote_wrong_signature_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(media.time, "time", lambda: NOW)
    monkeypatch.setattr(media, "_public_bytes", lambda *a, **k: b"image")
    raw = bytearray(envelope_bytes())
    raw[20] ^= 1  # corrupt inside the payload
    control = ControlS3(data=bytes(raw))
    env = control_env(**{sink_mod.ENV_LOCAL_PATH: str(tmp_path / "p.jsonl")})
    config = runtime.activate(env, s3_client=FakeMediaS3(),
                              control_s3_client=control).verifier_config
    verifier = media.trusted_bool_adapter(**config)
    assert verifier(BASE + "/media/k.png", b"image") is False


def test_failure_after_success_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(media.time, "time", lambda: NOW)
    monkeypatch.setattr(media, "_public_bytes", lambda *a, **k: b"image")
    control = ControlS3()
    env = control_env(**{sink_mod.ENV_LOCAL_PATH: str(tmp_path / "p.jsonl")})
    config = runtime.activate(env, s3_client=FakeMediaS3(),
                              control_s3_client=control).verifier_config
    verifier = media.trusted_bool_adapter(**config)
    assert isinstance(verifier(BASE + "/media/k.png", b"image"),
                      media.ImmutableMediaEvidence)
    control.fail_get = RuntimeError("transport down")
    assert verifier(BASE + "/media/k.png", b"image") is False
    control.fail_get = None
    assert isinstance(verifier(BASE + "/media/k.png", b"image"),
                      media.ImmutableMediaEvidence)


def test_outage_recovery_requires_fresh_proof(tmp_path, monkeypatch):
    """A store built during an outage recovers, but only with fresh proof."""
    monkeypatch.setattr(media, "_public_bytes", lambda *a, **k: b"image")
    control = ControlS3(fail_get=RuntimeError("outage"))
    env = control_env(**{sink_mod.ENV_LOCAL_PATH: str(tmp_path / "p.jsonl")})
    # Store-hook path: lazy document verification keeps the pinned config.
    config = runtime.activate(
        env, s3_client=FakeMediaS3(), control_s3_client=control,
        verify_initial=False).verifier_config
    assert config is not None
    verifier = media.trusted_bool_adapter(**config)
    monkeypatch.setattr(media.time, "time", lambda: NOW)
    assert verifier(BASE + "/media/k.png", b"image") is False  # outage: held
    control.fail_get = None
    assert isinstance(verifier(BASE + "/media/k.png", b"image"),
                      media.ImmutableMediaEvidence)
    # Stale envelope after outage still holds: freshness is per-fetch.
    monkeypatch.setattr(media.time, "time", lambda: NOW + 301)
    assert verifier(BASE + "/media/k.png", b"image") is False


def test_source_ambiguity_fails(tmp_path):
    local = {runtime.ENV_ATTESTATION_PATH: str(tmp_path / "a.json")}
    # remote source + local path
    env = control_env(**local)
    assert runtime.activate(env, s3_client=FakeMediaS3(),
                            control_s3_client=ControlS3()).guard_holds
    # local source + control envs
    env = control_env(**{runtime.ENV_ATTESTATION_SOURCE: "local"}, **local)
    assert runtime.activate(env, s3_client=FakeMediaS3()).guard_holds
    # unknown source value
    env = control_env(**{runtime.ENV_ATTESTATION_SOURCE: "carrier-pigeon"})
    assert runtime.activate(env, s3_client=FakeMediaS3()).guard_holds
    # partial control envs
    env = control_env()
    env.pop(runtime.ENV_CONTROL_KEY)
    assert runtime.activate(env, s3_client=FakeMediaS3()).guard_holds
    # explicit local source with a valid file still activates
    (tmp_path / "a.json").write_bytes(envelope_bytes())
    env = control_env(**{runtime.ENV_ATTESTATION_SOURCE: "local",
                         runtime.ENV_ATTESTATION_PATH: str(tmp_path / "a.json")})
    for name in runtime._CONTROL_ENVS:
        env.pop(name)
    assert not runtime.activate(env, s3_client=FakeMediaS3()).guard_holds


# --- signer job -------------------------------------------------------------

def job_env(tmp_path, **over):
    serving = tmp_path / "serving.json"
    serving.write_text(json.dumps(SERVING))
    env = control_env()
    env.pop(sink_mod.ENV_SINK_MODE)
    env.pop(sink_mod.ENV_LOCAL_PATH)
    env.update({
        job.ENV_ENABLED: "true",
        job.ENV_PROBE_KEY: "media/k.png",
        job.ENV_SERVING_EVIDENCE_PATH: str(serving),
        job.ENV_CONTROL_ACCESS_KEY_ID: "control-write-id",
        job.ENV_CONTROL_SECRET: "control-write-secret",
        "ECHO_LOCK_SIGNING_KEY": SEED_B64,
        "R2_API_TOKEN": "cf-lock-read-token",
        "R2_ACCESS_KEY_ID": "media-ro-id",
        "R2_SECRET_ACCESS_KEY": "media-ro-secret",
    })
    env.update(over)
    return env


@pytest.fixture
def live_signer(monkeypatch, tmp_path):
    """Patch the job's live evidence path with controlled fakes."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import exact_byte_sign_lock_attestation as signer
    monkeypatch.setattr(signer, "fetch_lock_readback", lambda *a, **k: RULES)
    monkeypatch.setattr(signer, "controlled_public_serving_evidence",
                        lambda **k: dict(PROBE))
    return signer


def test_job_off_by_default(tmp_path, capsys):
    env = job_env(tmp_path, **{job.ENV_ENABLED: ""})
    assert job.run(env, lock_handle=io.BytesIO()) == 0
    out = capsys.readouterr().out
    assert '"event": "disabled"' in out


def _lock_handle(tmp_path):
    return open(tmp_path / "job.lock", "a+")


def test_job_refresh_upload_readback(tmp_path, capsys, live_signer):
    control = ControlS3()
    env = job_env(tmp_path)
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=lambda s: None, cycles=1)
    assert code == 0
    assert len(control.puts) == 1
    bucket, key, body = control.puts[0]
    assert (bucket, key) == (CONTROL_BUCKET, CONTROL_KEY)
    doc, _ = media.SignedLockRuleSource(lambda: body, PUBLIC).verified_document()
    assert doc["account_id"] == ACCOUNT and doc["bucket"] == BUCKET
    assert doc["schema"] == "echo-r2-lock-attestation-v1"
    assert doc["serving_evidence"]["operator_review"]["reviewer"]
    out = capsys.readouterr().out
    assert '"event": "refreshed"' in out
    for secret in (SEED_B64, "cf-lock-read-token", "control-write-secret",
                   "media-ro-secret"):
        assert secret not in out


def test_job_upload_failure_never_replaces_good_object(tmp_path, live_signer):
    control = ControlS3(data=b"good-envelope", fail_put=RuntimeError("denied"))
    env = job_env(tmp_path)
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=lambda s: None, cycles=1)
    assert code == 0  # worker survives the failed cycle
    assert control.data == b"good-envelope"  # untouched


def test_job_readback_mismatch_fails(tmp_path, live_signer):
    class CorruptingS3(ControlS3):
        def put_object(self, Bucket, Key, Body):
            super().put_object(Bucket, Key, Body)
            self.data = bytes(Body) + b"x"  # readback diverges

    control = CorruptingS3()
    env = job_env(tmp_path)
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=lambda s: None, cycles=1)
    assert code == 0  # failed cycle logged, worker keeps running


def test_job_missing_config_refused(tmp_path, live_signer):
    env = job_env(tmp_path)
    env.pop("ECHO_LOCK_SIGNING_KEY")
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=ControlS3(),
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=lambda s: None, cycles=1)
    assert code == 0  # armed but unbuildable: bounded attempts, loop survives


def test_job_in_flight_lock_skips(tmp_path, live_signer):
    import fcntl
    held = _lock_handle(tmp_path)
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    env = job_env(tmp_path)
    control = ControlS3()
    # A second open file description on the same path sees the held lock.
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path))
    assert code == 0
    assert control.puts == []  # no refresh attempted
    fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    held.close()


# --- always-on worker loop ---------------------------------------------------

def test_job_repeats_refresh_each_cycle(tmp_path, capsys, live_signer):
    control = ControlS3()
    env = job_env(tmp_path)
    slept = []
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=slept.append, cycles=3)
    assert code == 0
    assert len(control.puts) == 3  # one proven refresh per cycle
    assert sum(slept) == pytest.approx(2 * job.DEFAULT_REFRESH_SECONDS,
                                       rel=0, abs=0.5)
    out = capsys.readouterr().out
    assert out.count('"event": "refreshed"') == 3
    assert '"event": "worker_started"' in out
    assert '"event": "shutdown"' in out


def test_job_refresh_cadence_accounts_for_proof_duration(tmp_path, monkeypatch):
    env = job_env(tmp_path)
    env[job.ENV_REFRESH_SECONDS] = "240"
    timeline = {"seconds": 0.0}
    starts = []

    def refresh_cycle(*args, **kwargs):
        starts.append(timeline["seconds"])
        timeline["seconds"] += 40.0
        return True

    def sleep(seconds):
        timeline["seconds"] += seconds

    monkeypatch.setattr(job, "_refresh_cycle", refresh_cycle)
    code = job.run(env, lock_handle=_lock_handle(tmp_path),
                   sleeper=sleep, clock=lambda: timeline["seconds"],
                   cycles=2)
    assert code == 0
    assert starts == [0.0, 240.0]


def test_job_recovers_after_transient_failure(tmp_path, capsys, live_signer):
    class FlakyS3(ControlS3):
        calls = 0

        def put_object(self, Bucket, Key, Body):
            type(self).calls += 1
            if type(self).calls <= 3:  # exhaust cycle 1's bounded attempts
                raise RuntimeError("transient")
            return super().put_object(Bucket, Key, Body)

    control = FlakyS3()
    env = job_env(tmp_path)
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=lambda s: None, cycles=2)
    assert code == 0  # transient cycle failure did not exit the worker
    assert len(control.puts) == 1  # second cycle refreshed successfully
    out = capsys.readouterr().out
    assert '"event": "cycle_failed"' in out
    assert '"event": "refreshed"' in out


def test_job_shutdown_during_interval_sleep(tmp_path, capsys, live_signer):
    control = ControlS3()
    env = job_env(tmp_path)
    state = {"stop": False}

    def sleeper(seconds):
        state["stop"] = True  # SIGTERM lands while waiting between cycles

    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=sleeper, should_stop=lambda: state["stop"])
    assert code == 0
    assert len(control.puts) == 1  # first immediate refresh completed
    out = capsys.readouterr().out
    assert '"event": "shutdown"' in out


def test_job_refresh_interval_env_clamped(tmp_path):
    env = job_env(tmp_path, **{job.ENV_REFRESH_SECONDS: "1"})
    assert job._refresh_seconds(env) == job.MIN_REFRESH_SECONDS
    env = job_env(tmp_path, **{job.ENV_REFRESH_SECONDS: "9999"})
    assert job._refresh_seconds(env) == job.MAX_REFRESH_SECONDS
    env = job_env(tmp_path, **{job.ENV_REFRESH_SECONDS: "bogus"})
    assert job._refresh_seconds(env) == job.DEFAULT_REFRESH_SECONDS


def test_railway_template_is_always_on_worker_not_cron():
    import json as _json
    from pathlib import Path
    template = _json.loads((Path(__file__).resolve().parent.parent
                            / "railway.exact-byte-attester.json").read_text())
    deploy = template["deploy"]
    assert "cronSchedule" not in deploy  # Railway cron cannot meet <=300s TTL
    assert deploy["restartPolicyType"] == "ON_FAILURE"
    assert deploy["startCommand"].endswith(
        "-m agent.exact_byte_attestation_job")
    assert template["variables"][
        "AGENT_EXACT_BYTE_ATTESTATION_JOB_ENABLED"] == "false"  # default OFF


# --- P2 availability hardening ------------------------------------------------

def _other_public_b64():
    other = Ed25519PrivateKey.from_private_bytes(b"z" * 32)
    return base64.b64encode(
        other.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()


def test_job_wrong_pinned_key_zero_puts(tmp_path, capsys, live_signer):
    control = ControlS3(data=b"good-envelope")
    env = job_env(tmp_path, **{runtime.ENV_ADMIN_PUBLIC_KEY: _other_public_b64()})
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=lambda s: None, cycles=1)
    assert code == 0  # worker survives; the cycle is refused
    assert control.puts == []  # seed-derived key != pinned key: zero PUTs
    assert control.data == b"good-envelope"  # good object untouched
    out = capsys.readouterr().out
    assert '"event": "cycle_failed"' in out
    assert "signing identity mismatch" in out
    for secret in (SEED_B64, "cf-lock-read-token", "control-write-secret"):
        assert secret not in out


def test_job_missing_pinned_key_zero_puts(tmp_path, capsys, live_signer):
    control = ControlS3(data=b"good-envelope")
    env = job_env(tmp_path)
    env.pop(runtime.ENV_ADMIN_PUBLIC_KEY)
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=lambda s: None, cycles=1)
    assert code == 0
    assert control.puts == []
    assert control.data == b"good-envelope"
    out = capsys.readouterr().out
    assert '"event": "cycle_failed"' in out
    assert "pinned administrator key absent" in out


@pytest.fixture
def forged_signer(monkeypatch, tmp_path):
    """Like live_signer, but attest is replaceable for timestamp control."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import exact_byte_sign_lock_attestation as signer
    monkeypatch.setattr(signer, "fetch_lock_readback", lambda *a, **k: RULES)
    monkeypatch.setattr(signer, "controlled_public_serving_evidence",
                        lambda **k: dict(PROBE))
    return signer


def _run_forged(tmp_path, signer, envelope, monkeypatch):
    monkey_envelope = json.loads(envelope)
    monkeypatch.setattr(signer, "attest", lambda **kwargs: monkey_envelope)
    control = ControlS3(data=b"good-envelope")
    env = job_env(tmp_path)
    code = job.run(env, media_s3=FakeMediaS3(), control_s3=control,
                   session=object(), lock_handle=_lock_handle(tmp_path),
                   sleeper=lambda s: None, cycles=1)
    return code, control


def test_job_fresh_signed_envelope_still_refreshes(tmp_path, forged_signer,
                                                    monkeypatch):
    import time as _time
    now = int(_time.time())
    code, control = _run_forged(
        tmp_path, forged_signer, envelope_bytes(now=now), monkeypatch)
    assert code == 0
    assert len(control.puts) == 1  # valid envelope passes the new gate


@pytest.mark.parametrize("doc_over,expected", [
    (None, "attestation stale"),                      # observed 301s ago
    ({"observed_at": None}, "attestation timestamps malformed"),
    ({"observed_at": True}, "attestation timestamps malformed"),
    ({"expires_at": "soon"}, "attestation timestamps malformed"),
])
def test_job_delayed_envelope_zero_puts(tmp_path, capsys, forged_signer,
                                        monkeypatch, doc_over, expected):
    import time as _time
    now = int(_time.time())
    over = {"observed_at": now - 301}
    if doc_over:
        over.update(doc_over)
    code, control = _run_forged(
        tmp_path, forged_signer,
        envelope_bytes(now=now - 301, doc_over=over), monkeypatch)
    assert code == 0
    assert control.puts == []  # freshness gate fires BEFORE upload
    assert control.data == b"good-envelope"
    out = capsys.readouterr().out
    assert '"event": "cycle_failed"' in out
    assert expected in out


def test_job_future_envelope_zero_puts(tmp_path, capsys, forged_signer,
                                       monkeypatch):
    import time as _time
    now = int(_time.time())
    code, control = _run_forged(
        tmp_path, forged_signer,
        envelope_bytes(now=now,
                       doc_over={"observed_at": now + 120,
                                 "expires_at": now + 420}), monkeypatch)
    assert code == 0
    assert control.puts == []
    assert control.data == b"good-envelope"
    assert "attestation from the future" in capsys.readouterr().out


def test_job_overlong_signed_lifetime_zero_puts(tmp_path, capsys,
                                                 forged_signer, monkeypatch):
    import time as _time
    now = int(_time.time())
    code, control = _run_forged(
        tmp_path, forged_signer,
        envelope_bytes(now=now,
                       doc_over={"expires_at": now + 301}), monkeypatch)
    assert code == 0
    assert control.puts == []
    assert control.data == b"good-envelope"
    assert "attestation lifetime invalid" in capsys.readouterr().out


@pytest.mark.parametrize("expires_in,expected", [
    (0, "attestation expired"),
    (30, "attestation remaining ttl insufficient"),
])
def test_job_expired_or_short_ttl_envelope_zero_puts(tmp_path, capsys,
                                                     forged_signer, monkeypatch, expires_in,
                                                     expected):
    import time as _time
    now = int(_time.time())
    code, control = _run_forged(
        tmp_path, forged_signer,
        envelope_bytes(now=now - 10,
                       doc_over={"expires_at": now + expires_in}), monkeypatch)
    assert code == 0
    assert control.puts == []
    assert control.data == b"good-envelope"
    assert expected in capsys.readouterr().out
