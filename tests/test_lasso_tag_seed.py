"""lasso_tag_seed: LASSO's gym_tag_allowlist tracks the live Zernio roster.

Offline: fake Zernio client, fake upsert. Behind AGENT_MENTIONS (flag OFF =
no-op); dry-run reads the roster and writes nothing; the nightly hook is
kv-deduped to once per day.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.lasso_tag_seed import run_nightly, seed_lasso_allowlist

PARTNERS = ["thecrossfiteng.official", "traingritx", "topfuelcrossfit_valpo_",
            "piercefitnesskitchener", "hillcountrymvmt", "theboltonclub"]


class _FakeZernio:
    def list_profiles(self):
        return [{"_id": f"p{i}"} for i in range(len(PARTNERS))]

    def list_accounts(self, pid):
        i = int(pid[1:])
        return [{"handle": PARTNERS[i]}]


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_MENTIONS", "true")
    monkeypatch.setenv("ZERNIO_API_KEY", "zk")


def test_flag_off_is_a_noop(monkeypatch):
    out = seed_lasso_allowlist(zernio_client=_FakeZernio(), dry_run=True)
    assert out["ok"] is False and "AGENT_MENTIONS" in out["reason"]


def test_dry_run_reports_roster_writes_nothing(monkeypatch):
    _arm(monkeypatch)
    wrote = []
    out = seed_lasso_allowlist(zernio_client=_FakeZernio(), dry_run=True,
                               upsert=lambda *a: wrote.append(a))
    assert out["ok"] and out["dry_run"]
    assert wrote == []
    assert out["handles"][0] == "lassoframework"          # own handle first
    assert set(PARTNERS) <= set(out["handles"])           # ~7 partner handles
    kinds = {r["handle"]: r["kind"] for r in out["rows"]}
    assert kinds["lassoframework"] == "own"
    assert all(kinds[h] == "partner" for h in PARTNERS)
    assert all(r["gym_id"] == "lasso" and r["consent"] for r in out["rows"])


def test_live_seed_upserts_rows(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://sb.example")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sk")
    wrote = []
    out = seed_lasso_allowlist(zernio_client=_FakeZernio(),
                               upsert=lambda url, key, rows: wrote.append(rows))
    assert out["ok"] and out["seeded"] == 1 + len(PARTNERS)
    assert len(wrote) == 1 and len(wrote[0]) == 1 + len(PARTNERS)


def test_missing_creds_reports_and_writes_nothing(monkeypatch):
    _arm(monkeypatch)
    out = seed_lasso_allowlist(zernio_client=_FakeZernio())
    assert out["ok"] is False and "creds" in out["reason"]


def test_nightly_hook_runs_once_per_day(monkeypatch):
    _arm(monkeypatch)
    calls = []
    monkeypatch.setattr("agent.lasso_tag_seed.seed_lasso_allowlist",
                        lambda **k: calls.append(1) or {"ok": True, "seeded": 7})
    assert run_nightly(now_date="2026-08-27")["seeded"] == 7
    assert run_nightly(now_date="2026-08-27")["reason"] == "already ran today"
    assert len(calls) == 1
