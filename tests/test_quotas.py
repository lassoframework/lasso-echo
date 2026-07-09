"""
Tokenized upload quotas + tenant token watchdog (Stage 2 Part 9). Offline.

Asserts: an upload over the tenant's measured storage quota refuses (413) while
under-quota, unmeasurable storage, and legacy no-tenant clients all pass;
originals stream to R2 byte-identical (HEIC accepted, EXIF kept); the monthly
recreate budget counts down per calendar month and refuses at zero; the token
watchdog flags an upload-lane tenant with no token env and never prints a
token value.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, intake_web, ops_alerts, quotas, tenants, token_watchdog  # noqa: E402


class FakeR2:
    def __init__(self, used=0):
        self.objects = {}
        self._used = used

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data

    def total_bytes(self, prefix):
        return self._used


class NoMeasureR2:
    """A wrapper that cannot report usage (no total_bytes at all)."""

    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data


def _tenant(monkeypatch, tmp_path, key="upgym", quota_mb=1, lanes=("upload",)):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    out = tenants.intake_create({
        "key": key, "name": key, "avatar": "Families.",
        "voice": {"tone": "Warm."},
        "approver": {"name": "Sam", "phone": "+13175550701"},
        "sender_phones": ["+13175550701"], "media_lanes": list(lanes),
        "storage_quota_mb": quota_mb, "monthly_recreate_budget": 2,
    }, base_dir=str(tmp_path))
    assert not out.get("blocked"), out
    # point the tenants registry at tmp for every reader in this test
    monkeypatch.setattr(tenants, "tenants_dir", lambda base_dir=None: str(tmp_path))


def _arm_upload(monkeypatch, key="upgym", token="tok_upload_probe_123"):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv(f"AGENT_INTAKE_TOKEN_{key.upper()}", token)
    return token


_JPG = ("photo.heic", "image/heic", b"HEIC-ORIGINAL-BYTES-WITH-EXIF")


# ---- storage quota gate ---------------------------------------------------------------------

def test_upload_over_quota_refused_413(monkeypatch, tmp_path):
    _tenant(monkeypatch, tmp_path, quota_mb=1)
    token = _arm_upload(monkeypatch)
    r2 = FakeR2(used=1024 * 1024)     # already at the 1MB cap
    status, body = intake_web.handle_upload(token, [_JPG], r2=r2)
    assert status == 413
    assert "quota" in body["error"]
    assert r2.objects == {}           # nothing stored


def test_upload_under_quota_passes_and_streams_original(monkeypatch, tmp_path):
    _tenant(monkeypatch, tmp_path, quota_mb=1)
    token = _arm_upload(monkeypatch)
    r2 = FakeR2(used=0)
    status, body = intake_web.handle_upload(token, [_JPG], note="Opening day.",
                                            r2=r2)
    assert status == 200 and body["stored"] == 1
    # the HEIC original landed byte-identical: nothing recompressed, EXIF kept
    media = [v for k, v in r2.objects.items() if k.endswith(".heic")]
    assert media == [b"HEIC-ORIGINAL-BYTES-WITH-EXIF"]


def test_unmeasurable_storage_never_blocks(monkeypatch, tmp_path):
    _tenant(monkeypatch, tmp_path, quota_mb=1)
    token = _arm_upload(monkeypatch)
    status, _body = intake_web.handle_upload(token, [_JPG], r2=NoMeasureR2())
    assert status == 200


def test_legacy_client_without_tenant_record_unaffected(monkeypatch, tmp_path):
    """An env-token client with no tenant.json keeps working uncapped."""
    monkeypatch.setattr(tenants, "tenants_dir", lambda base_dir=None: str(tmp_path))
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_LEGACYGYM", "tok_legacy_probe_123")
    r2 = FakeR2(used=10 ** 12)        # a terabyte used; no record = no cap
    status, _body = intake_web.handle_upload("tok_legacy_probe_123", [_JPG], r2=r2)
    assert status == 200


# ---- monthly recreate budget ------------------------------------------------------------------

def test_recreate_budget_counts_down_and_refuses_at_zero(monkeypatch, tmp_path):
    _tenant(monkeypatch, tmp_path, key="budgetgym")
    from datetime import datetime, timezone
    now = datetime(2026, 7, 9, tzinfo=timezone.utc)
    key = quotas._spend_key("budgetgym", now)
    with db._lock, db.connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (key,))
        conn.commit()
    assert quotas.spend_recreate("budgetgym", now=now) is True    # 1 of 2
    assert quotas.spend_recreate("budgetgym", now=now) is True    # 2 of 2
    assert quotas.spend_recreate("budgetgym", now=now) is False   # exhausted
    # a NEW month starts fresh
    next_month = datetime(2026, 8, 1, tzinfo=timezone.utc)
    nkey = quotas._spend_key("budgetgym", next_month)
    with db._lock, db.connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (nkey,))
        conn.commit()
    assert quotas.spend_recreate("budgetgym", now=next_month) is True
    for k in (key, nkey):
        with db._lock, db.connect() as conn:
            conn.execute("DELETE FROM kv WHERE key=?", (k,))
            conn.commit()


def test_no_tenant_record_has_nothing_to_spend(monkeypatch, tmp_path):
    monkeypatch.setattr(tenants, "tenants_dir", lambda base_dir=None: str(tmp_path))
    assert quotas.spend_recreate("ghost_gym") is False


# ---- token watchdog covers tenant tokens --------------------------------------------------------

def test_watchdog_flags_missing_tenant_token(monkeypatch, tmp_path):
    _tenant(monkeypatch, tmp_path, key="tokgym", lanes=("upload", "sms"))
    monkeypatch.delenv("AGENT_INTAKE_TOKEN_TOKGYM", raising=False)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    results = token_watchdog.check_tenant_tokens(base_dir=str(tmp_path))
    row = next(r for r in results if r["tenant"] == "tokgym")
    assert row["status"] == "missing_token"
    assert any("tokgym" in m and "dead" in m for m in fired)


def test_watchdog_passes_when_token_set_and_never_prints_it(monkeypatch, tmp_path):
    _tenant(monkeypatch, tmp_path, key="tokgym2", lanes=("upload",))
    secret_value = "tok_super_secret_value_999"
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_TOKGYM2", secret_value)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    results = token_watchdog.check_tenant_tokens(base_dir=str(tmp_path))
    row = next(r for r in results if r["tenant"] == "tokgym2")
    assert row["status"] == "ok"
    assert fired == []
    assert secret_value not in str(results)   # the value never leaves env


def test_sms_only_tenant_not_flagged(monkeypatch, tmp_path):
    _tenant(monkeypatch, tmp_path, key="smsgym", lanes=("sms",))
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    results = token_watchdog.check_tenant_tokens(base_dir=str(tmp_path))
    assert all(r["tenant"] != "smsgym" for r in results)
    assert fired == []
