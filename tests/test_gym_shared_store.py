"""
The echo.db split-brain fix: the shared `echo_gyms` record.

Echo runs as two Railway services (`echo` the worker, `echo-intake-web` the HTTP
service) with two SEPARATE volumes and therefore two SEPARATE /data/echo.db files.
Every gyms row a self-serve onboard wrote landed only on the web service's copy, and
the worker (where every read of that row actually happens) never saw it.

These tests are OFFLINE: a schema-faithful in-memory PostgREST fake stands in for
Supabase. It stores real rows, applies the real `account_key=eq.<k>` filter, and
returns PATCH's real "zero rows matched" behaviour, so a test cannot pass against a
fake that is more forgiving than production.

Invariants:
  1. gym_upsert dual-writes the row to the shared store.
  2. Token material (upload_link / intake_token_encrypted / hashes) NEVER leaves.
  3. A blank display_name is not mirrored (the PRESERVE-display_name contract).
  4. gym_get read-through hydrates a shared-only row into the local table.
  5. A read-through miss is negative-cached (no per-post network storm).
  6. A shared-store read error degrades to the local answer, never raises.
  7. A shared-store WRITE failure is LOUD (ops alert) and never silent.
  8. A mirror failure cannot deadlock on db._lock (it calls kv through alerting).
  9. gym_list pulls shared-only rows; a pull never overwrites a local value.
 10. gym-store-sync reports both directions plus real disagreements, and --apply
     is additive and idempotent.
 11. connect() repairs a legacy gyms table missing the later columns.
 12. With creds absent, or the kill switch off, NOTHING touches the network.
"""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, gym_shared_store as gss, gym_store_sync


# ---- schema-faithful PostgREST fake over echo_gyms ---------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class FakePostgrest:
    """In-memory echo_gyms. Honours the account_key=eq.<k> filter, PATCH's
    zero-rows-matched semantics, and merge-duplicates on POST."""

    def __init__(self, rows=None, fail_on=()):
        self.rows = {r["account_key"]: dict(r) for r in (rows or [])}
        self.calls = []
        self.fail_on = set(fail_on)   # {"get", "patch", "post"}

    def _key(self, params):
        raw = (params or {}).get("account_key") or ""
        return raw[3:] if raw.startswith("eq.") else ""

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", params or {}))
        if "get" in self.fail_on:
            return _Resp(500, [], "boom")
        key = self._key(params)
        if key:
            row = self.rows.get(key)
            return _Resp(200, [dict(row)] if row else [])
        return _Resp(200, [dict(v) for _, v in sorted(self.rows.items())])

    def patch(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("patch", params or {}, dict(json or {})))
        if "patch" in self.fail_on:
            return _Resp(500, [], "boom")
        key = self._key(params)
        if key not in self.rows:
            return _Resp(200, [])          # PostgREST: zero rows matched
        payload = dict(json or {})
        payload.pop("updated_at", None)
        self.rows[key].update(payload)
        return _Resp(200, [dict(self.rows[key])])

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("post", params or {}, list(json or [])))
        if "post" in self.fail_on:
            return _Resp(500, [], "boom")
        out = []
        for item in (json or []):
            row = dict(item)
            row.pop("updated_at", None)
            key = row.get("account_key") or ""
            if not key:
                return _Resp(400, [], "null value in column account_key")
            merged = self.rows.get(key, {})
            merged.update(row)
            self.rows[key] = merged
            out.append(dict(merged))
        return _Resp(201, out)

    def delete(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("delete", params or {}))
        self.rows.pop(self._key(params), None)
        return _Resp(200, [])


@pytest.fixture(autouse=True)
def _reset_caches():
    db._miss_cache.clear()
    db._last_list_refresh[0] = 0.0
    yield
    db._miss_cache.clear()
    db._last_list_refresh[0] = 0.0


@pytest.fixture
def armed(monkeypatch):
    """Shared store creds present (conftest strips SUPABASE_* by default)."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")


def _wire(monkeypatch, fake):
    """Point db's shared-store factory at the fake, exactly as production wires it."""
    monkeypatch.setattr(
        db, "_shared_store",
        lambda store=None: store if store is not None
        else gss.SharedGymStore(url="https://example.supabase.co",
                                service_key="k", http=fake))


# ---- 1. dual write -----------------------------------------------------------

def test_gym_upsert_mirrors_the_row_to_the_shared_store(armed, monkeypatch):
    fake = FakePostgrest()
    _wire(monkeypatch, fake)
    db.gym_upsert("splitfix1", display_name="Split Fix Gym",
                  posting_timezone="America/Denver")
    assert fake.rows["splitfix1"]["display_name"] == "Split Fix Gym"
    assert fake.rows["splitfix1"]["posting_timezone"] == "America/Denver"
    # and the local row is still written (the mirror never replaces the local write)
    assert (db.gym_get("splitfix1", _shared_read=False) or {})["display_name"] \
        == "Split Fix Gym"


def test_a_later_partial_upsert_does_not_blank_the_mirrored_name(armed, monkeypatch):
    fake = FakePostgrest()
    _wire(monkeypatch, fake)
    db.gym_upsert("splitfix2", display_name="Keeps Its Name")
    db.gym_upsert("splitfix2", zernio_profile_id="prof-9")   # no display_name
    assert fake.rows["splitfix2"]["display_name"] == "Keeps Its Name"
    assert fake.rows["splitfix2"]["zernio_profile_id"] == "prof-9"


# ---- 2. no token material ever leaves ---------------------------------------

def test_token_material_and_upload_link_are_never_mirrored(armed, monkeypatch):
    fake = FakePostgrest()
    _wire(monkeypatch, fake)
    db.gym_upsert("splitfix3", display_name="Tokenful",
                  upload_link="https://echo.example/u/RAWTOKEN.SIG",
                  intake_token_encrypted="ENCBLOB",
                  intake_token_hash="HASH",
                  stripe_customer_id="cus_123")
    stored = fake.rows["splitfix3"]
    for forbidden in ("upload_link", "intake_token_encrypted", "intake_token_hash",
                      "token_sha256"):
        assert forbidden not in stored, f"{forbidden} must never reach echo_gyms"
    assert stored["stripe_customer_id"] == "cus_123"
    # and nothing token-shaped appears anywhere in the recorded traffic
    assert "RAWTOKEN" not in repr(fake.calls)
    assert "ENCBLOB" not in repr(fake.calls)


def test_updated_at_is_a_real_timestamp_not_the_string_now(armed, monkeypatch):
    """The mirror stamps an explicit UTC ISO updated_at rather than leaning on
    Postgres' lenient parsing of a "now()" string (which it does in fact accept --
    verified against the live table on 2026-09-10). Pinning it here keeps the stamp
    deterministic and client-side, so a clock-skewed database cannot silently decide
    when this row was last mirrored."""
    from datetime import datetime
    fake = FakePostgrest()
    _wire(monkeypatch, fake)
    db.gym_upsert("stampcheck", display_name="Stamp Check")
    stamps = []
    for call in fake.calls:
        body = call[-1]
        if isinstance(body, list):
            body = body[0] if body else {}
        if isinstance(body, dict) and "updated_at" in body:
            stamps.append(body["updated_at"])
    assert stamps, "the mirror write must stamp updated_at"
    for s in stamps:
        assert s != "now()"
        datetime.fromisoformat(s)          # raises on anything unparseable


def test_mirrorable_filters_to_the_declared_column_set():
    out = gss.mirrorable({"display_name": "A", "upload_link": "u",
                          "posting_timezone": "UTC", "nonsense": 1})
    assert out == {"display_name": "A", "posting_timezone": "UTC"}
    assert gss.mirrorable({"display_name": "   "}) == {}


# ---- 4/5/6. read-through -----------------------------------------------------

def test_gym_get_read_through_hydrates_a_shared_only_row(armed, monkeypatch):
    fake = FakePostgrest(rows=[{"account_key": "onlyshared",
                                "display_name": "Only Shared",
                                "posting_timezone": "America/Chicago",
                                "zernio_profile_id": "prof-1"}])
    _wire(monkeypatch, fake)
    assert db.gym_get("onlyshared", _shared_read=False) is None   # not local yet
    row = db.gym_get("onlyshared")
    assert row is not None and row["display_name"] == "Only Shared"
    assert row["zernio_profile_id"] == "prof-1"
    # Hydrated. The SECOND read never makes another PER-KEY lookup; the only further
    # traffic it may cause is the throttled FULL-table refresh, which is one request
    # for the whole fleet and is capped at one per window (asserted below).
    before = list(fake.calls)
    again = db.gym_get("onlyshared")
    assert again["posting_timezone"] == "America/Chicago"
    new_calls = fake.calls[len(before):]
    assert not [c for c in new_calls if c[1].get("account_key")], (
        "a hydrated row still cost a per-key network lookup")
    n = len(fake.calls)
    db.gym_get("onlyshared")
    db.gym_get("onlyshared")
    assert len(fake.calls) == n, "the full-table refresh was not throttled"


def test_a_failed_hydrate_still_returns_the_shared_row(armed, monkeypatch):
    """A local cache-fill failure must not turn a row we DID find into a None: the
    caller asked what this gym's record is, and we have it."""
    fake = FakePostgrest(rows=[{"account_key": "hydratefail",
                                "display_name": "Hydrate Fail",
                                "zernio_profile_id": "prof-hf"}])
    _wire(monkeypatch, fake)
    def _boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "_local_gym_upsert", _boom)
    row = db.gym_get("hydratefail")
    assert row is not None and row["zernio_profile_id"] == "prof-hf"


def test_a_read_through_miss_is_negative_cached(armed, monkeypatch):
    fake = FakePostgrest()
    _wire(monkeypatch, fake)
    assert db.gym_get("neverexisted") is None
    assert db.gym_get("neverexisted") is None
    gets = [c for c in fake.calls if c[0] == "get"]
    assert len(gets) == 1, "a missing key must not hit the network on every read"


def test_a_shared_read_error_degrades_to_the_local_answer(armed, monkeypatch):
    fake = FakePostgrest(fail_on=("get",))
    _wire(monkeypatch, fake)
    assert db.gym_get("boomkey") is None          # no exception escapes
    db.gym_upsert("boomkey2", display_name="Local Wins")
    assert db.gym_get("boomkey2")["display_name"] == "Local Wins"


def test_an_explicit_conn_stays_a_pure_local_read(armed, monkeypatch):
    fake = FakePostgrest(rows=[{"account_key": "sharedonly2",
                                "display_name": "Shared Only 2"}])
    _wire(monkeypatch, fake)
    with db.connect() as conn:
        assert db.gym_get("sharedonly2", conn=conn) is None
    assert not fake.calls


# ---- 7/8. loud failure, no deadlock -----------------------------------------

def test_a_mirror_write_failure_fires_an_ops_alert(armed, monkeypatch):
    fake = FakePostgrest(fail_on=("patch", "post"))
    _wire(monkeypatch, fake)
    fired = []
    monkeypatch.setattr(db, "_mirror_alert",
                        lambda msg, account_key="", now=None: fired.append(msg))
    db.gym_upsert("loudfail", display_name="Loud Fail")
    assert fired, "a shared-store write failure must never be silent"
    assert "loudfail" in fired[0]
    # the local write still succeeded: nothing is lost
    assert db.gym_get("loudfail", _shared_read=False)["display_name"] == "Loud Fail"


def test_a_fleet_wide_mirror_outage_sends_ONE_slack_line_not_one_per_gym(
        armed, monkeypatch):
    """A Supabase outage during a reconcile fails 100+ writes back to back. Every
    failure must be recorded, but the channel must get one line, not a hundred."""
    fake = FakePostgrest(fail_on=("patch", "post"))
    _wire(monkeypatch, fake)
    posted = []
    monkeypatch.setattr("agent.ops_alerts.alert",
                        lambda msg, poster=None, force=False: posted.append(msg))
    for i in range(25):
        db.gym_upsert(f"stormgym{i}", display_name=f"Storm {i}")
    assert len(posted) == 1, f"alert fan-out was not collapsed ({len(posted)} lines)"
    # ...but every single failure is still on the record in the audit table
    rows = db.audit_rows(limit=200)
    recorded = {r["account_key"] for r in rows if r["kind"] == "gym_shared_store"}
    assert len(recorded) == 25, "every failing gym must get its own audit row"


def test_the_alert_gate_reopens_after_its_window(armed, monkeypatch):
    fake = FakePostgrest(fail_on=("patch", "post"))
    _wire(monkeypatch, fake)
    posted = []
    monkeypatch.setattr("agent.ops_alerts.alert",
                        lambda msg, poster=None, force=False: posted.append(msg))
    db._mirror_alert("first outage line", account_key="g1", now=1000.0)
    db._mirror_alert("still down", account_key="g2", now=1000.0 + 60)
    assert len(posted) == 1
    db._mirror_alert("still down later", account_key="g3",
                     now=1000.0 + db._MIRROR_ALERT_WINDOW_SECONDS + 1)
    assert len(posted) == 2, "a new incident window must be able to alert again"


def test_mirror_failure_alert_path_does_not_deadlock(armed, monkeypatch):
    """_mirror_alert reaches kv_get/kv_set through alert_repeat, which take db._lock.
    Mirroring INSIDE that lock (non-reentrant) hangs the process on the first failure."""
    fake = FakePostgrest(fail_on=("patch", "post"))
    _wire(monkeypatch, fake)
    posted = []
    monkeypatch.setattr("agent.ops_alerts.alert",
                        lambda msg, poster=None, force=False: posted.append(msg))
    done = threading.Event()

    def _run():
        db.gym_upsert("deadlockcheck", display_name="No Deadlock")
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    assert done.wait(timeout=15), "gym_upsert deadlocked on the mirror failure path"
    assert posted, "the alert must actually reach ops_alerts"


# ---- 9. list pull ------------------------------------------------------------

def test_gym_list_pulls_shared_only_rows(armed, monkeypatch):
    fake = FakePostgrest(rows=[
        {"account_key": "pulled1", "display_name": "Pulled One"},
        {"account_key": "pulled2", "display_name": "Pulled Two",
         "stripe_customer_id": "cus_p2"},
    ])
    _wire(monkeypatch, fake)
    db.gym_upsert("localone", display_name="Local One")
    keys = {r["account_key"] for r in db.gym_list()}
    assert {"localone", "pulled1", "pulled2"} <= keys
    assert db.gym_get("pulled2", _shared_read=False)["stripe_customer_id"] == "cus_p2"


def test_an_update_to_an_EXISTING_row_propagates(armed, monkeypatch):
    """The gap the live end-to-end found on 2026-09-10: gym_get's read-through only
    fires on a local MISS, so a row this service already had answered from its stale
    local copy forever. A change made on the OTHER service must reach this one."""
    fake = FakePostgrest(rows=[{
        "account_key": "updateme", "display_name": "Update Me",
        "stripe_customer_id": "cus_written_by_the_other_service",
        "updated_at": "2026-09-10T20:00:00+00:00"}])
    _wire(monkeypatch, fake)
    # this service already HAS the row, with an older stamp and no customer id
    db._local_gym_upsert("updateme", "Update Me", {})
    with db.connect() as c:
        c.execute("UPDATE gyms SET updated_at='2026-09-10 19:00:00' "
                  "WHERE account_key='updateme'")
        c.commit()
    assert db.gym_get("updateme", _shared_read=False)["stripe_customer_id"] is None

    row = db.gym_get("updateme")
    assert row["stripe_customer_id"] == "cus_written_by_the_other_service", (
        "an update made on the other service never reached this one")


def test_a_CHANGED_field_is_overwritten_when_the_shared_row_is_newer(
        armed, monkeypatch):
    """The strict case: this service already holds a NON-EMPTY value and the other
    service CHANGED it. Filling only empty fields (the first cut) leaves the old value
    in place forever and calls the divergence a 'disagreement'. Newest wins."""
    fake = FakePostgrest(rows=[{
        "account_key": "changedfield", "display_name": "Changed Field",
        "zernio_profile_id": "prof-REPOINTED",
        "posting_timezone": "Europe/Lisbon",
        "updated_at": "2026-09-10T20:00:00+00:00"}])
    _wire(monkeypatch, fake)
    db._local_gym_upsert("changedfield", "Changed Field",
                         {"zernio_profile_id": "prof-STALE",
                          "posting_timezone": "America/New_York"})
    with db.connect() as c:
        c.execute("UPDATE gyms SET updated_at='2026-09-10 19:00:00' "
                  "WHERE account_key='changedfield'")
        c.commit()

    db.pull_shared_into_local(force=True)

    row = db.gym_get("changedfield", _shared_read=False)
    assert row["zernio_profile_id"] == "prof-REPOINTED", (
        "a value CHANGED on the other service never propagated")
    assert row["posting_timezone"] == "Europe/Lisbon"


def test_a_stale_shared_row_never_rolls_back_a_newer_local_value(armed, monkeypatch):
    """The mirror can fail (it alerts, and the local write stands). A shared copy that
    is OLDER than the local row must never overwrite it back."""
    fake = FakePostgrest(rows=[{
        "account_key": "newerlocal", "display_name": "Newer Local",
        "zernio_profile_id": "prof-OLD",
        "updated_at": "2026-09-10T18:00:00+00:00"}])
    _wire(monkeypatch, fake)
    db._local_gym_upsert("newerlocal", "Newer Local",
                         {"zernio_profile_id": "prof-NEW"})
    with db.connect() as c:
        c.execute("UPDATE gyms SET updated_at='2026-09-10 21:00:00' "
                  "WHERE account_key='newerlocal'")
        c.commit()
    db.pull_shared_into_local(force=True)
    assert db.gym_get("newerlocal", _shared_read=False)["zernio_profile_id"] \
        == "prof-NEW", "a stale shared copy rolled back a newer local write"


def test_naive_local_stamps_are_read_as_utc_not_local_time(armed):
    """SQLite writes datetime('now') (naive UTC) and the shared store an ISO string
    with an offset. Reading the naive one as LOCAL time would make every comparison
    wrong by the host's offset -- silently, and differently per machine."""
    from datetime import timezone
    naive = db._parse_ts("2026-09-10 19:00:00")
    aware = db._parse_ts("2026-09-10T19:00:00+00:00")
    assert naive is not None and naive.tzinfo is not None
    assert naive == aware
    assert db._parse_ts("2026-09-10T19:00:00Z") == aware
    assert db._parse_ts(None) is None and db._parse_ts("nonsense") is None


def test_a_pull_never_overwrites_a_non_empty_local_value(armed, monkeypatch):
    fake = FakePostgrest(rows=[{"account_key": "conflict1",
                                "display_name": "Shared Name",
                                "posting_timezone": "Asia/Tokyo"}])
    _wire(monkeypatch, fake)
    # local has a name already, but no timezone
    db._local_gym_upsert("conflict1", "Local Name", {})
    db.pull_shared_into_local(force=True)
    row = db.gym_get("conflict1", _shared_read=False)
    assert row["display_name"] == "Local Name", "a pull must not clobber a local edit"
    assert row["posting_timezone"] == "Asia/Tokyo", "an unset local field is filled"


def test_gym_list_pull_is_throttled(armed, monkeypatch):
    fake = FakePostgrest(rows=[{"account_key": "throttled", "display_name": "T"}])
    _wire(monkeypatch, fake)
    db.gym_list()
    n = len(fake.calls)
    db.gym_list()
    db.gym_list()
    assert len(fake.calls) == n, "gym_list must not pull on every call"


# ---- 10. reconcile -----------------------------------------------------------

def test_sync_reports_both_directions_and_real_disagreements(armed, monkeypatch):
    fake = FakePostgrest(rows=[
        {"account_key": "bothsides", "display_name": "Shared Name"},
        {"account_key": "sharedside", "display_name": "Shared Side"},
    ])
    _wire(monkeypatch, fake)
    db._local_gym_upsert("bothsides", "Local Name", {})
    db._local_gym_upsert("localside", "Local Side", {})
    store = gss.SharedGymStore(url="https://x", service_key="k", http=fake)
    report = gym_store_sync.compare(store=store)
    assert report["local_only"] == ["localside"]
    assert report["shared_only"] == ["sharedside"]
    assert [d["account_key"] for d in report["disagree"]] == ["bothsides"]
    assert "display_name" in report["disagree"][0]["fields"]


def test_sync_apply_moves_missing_rows_both_ways_and_is_idempotent(armed, monkeypatch):
    fake = FakePostgrest(rows=[{"account_key": "sharedside2",
                                "display_name": "Shared Side 2"}])
    _wire(monkeypatch, fake)
    db._local_gym_upsert("localside2", "Local Side 2", {})
    store = gss.SharedGymStore(url="https://x", service_key="k", http=fake)

    first = gym_store_sync.sync(apply=True, store=store)
    assert first["pushed"] == 1 and first["pulled"] >= 1
    assert "localside2" in fake.rows
    assert db.gym_get("sharedside2", _shared_read=False) is not None

    db._last_list_refresh[0] = 0.0
    second = gym_store_sync.sync(apply=True, store=store)
    assert second["local_only"] == [] and second["shared_only"] == []
    assert second["pushed"] == 0


def test_sync_reports_zernio_profile_ids_bound_to_two_keys(armed, monkeypatch):
    """One real gym known under two account keys (the separate duplicate-derivation
    problem) becomes visible on a service that previously saw only one twin. Not fixed
    here, but it must not be discovered later by accident."""
    fake = FakePostgrest(rows=[{"account_key": "reverbnew", "display_name": "Reverb",
                                "zernio_profile_id": "prof-shared"}])
    _wire(monkeypatch, fake)
    db._local_gym_upsert("reverbold", "Reverb", {"zernio_profile_id": "prof-shared"})
    db._local_gym_upsert("unrelated", "Unrelated", {"zernio_profile_id": "prof-solo"})
    store = gss.SharedGymStore(url="https://x", service_key="k", http=fake)
    report = gym_store_sync.compare(store=store)
    collisions = report["profile_collisions"]
    assert len(collisions) == 1
    assert collisions[0]["account_keys"] == ["reverbnew", "reverbold"]
    assert "reverbold" in gym_store_sync.format_report(report)
    # a profile bound to exactly one key is not a collision
    assert all(c["zernio_profile_id"] != "prof-solo" for c in collisions)


def test_an_unreadable_shared_store_reports_unknown_and_writes_nothing(
        armed, monkeypatch):
    """An operator command must REPORT an unreachable store, not traceback at it --
    and must never phrase an unread store as zero drift, which reads as 'the two
    services agree'."""
    fake = FakePostgrest(fail_on=("get",))
    _wire(monkeypatch, fake)
    db._local_gym_upsert("wouldpush", "Would Push", {})
    store = gss.SharedGymStore(url="https://x", service_key="k", http=fake)

    # Count the local reads: compare() makes exactly ONE. A sync that carries on into
    # its write phase after a failed read makes a second, which is the observable
    # signature of "acting on a comparison that never happened".
    reads = []
    real_list = db.gym_list
    monkeypatch.setattr(gym_store_sync._db, "gym_list",
                        lambda *a, **k: (reads.append(1), real_list(*a, **k))[1])

    report = gym_store_sync.sync(apply=True, store=store)
    assert report["error"]
    assert report["pushed"] == 0 and report["pulled"] == 0
    assert "wouldpush" not in fake.rows, "wrote on the strength of a read it never made"
    assert len(reads) == 1, (
        f"sync entered its write phase after a failed read ({len(reads)} local reads)")
    text = gym_store_sync.format_report(report)
    assert "UNKNOWN" in text and "nothing was written" in text


def test_sync_without_apply_writes_nothing(armed, monkeypatch):
    fake = FakePostgrest()
    _wire(monkeypatch, fake)
    db._local_gym_upsert("dryrunonly", "Dry Run", {})
    store = gss.SharedGymStore(url="https://x", service_key="k", http=fake)
    report = gym_store_sync.sync(apply=False, store=store)
    assert report["local_only"] == ["dryrunonly"]
    assert "dryrunonly" not in fake.rows
    assert report["pushed"] == 0


# ---- 11. legacy schema repair -----------------------------------------------

def test_connect_repairs_a_legacy_gyms_table(tmp_path, monkeypatch):
    """The `echo` worker's volume predates upload_link / gym_name / token_sha256 /
    token_status / publish_creds, and _SCHEMA is CREATE TABLE IF NOT EXISTS, so only
    an additive ALTER can ever reach it. Without the repair, gym_upsert(upload_link=)
    raises 'no such column' on that service."""
    import sqlite3
    path = str(tmp_path / "legacy.db")
    monkeypatch.setenv("AGENT_DB_PATH", path)
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE gyms (account_key TEXT PRIMARY KEY, "
                   "display_name TEXT DEFAULT '', publish_flag TEXT, "
                   "created_at TEXT, updated_at TEXT)")
    legacy.commit()
    legacy.close()

    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(gyms)")}
    for col in ("upload_link", "gym_name", "token_sha256", "token_status",
                "publish_creds"):
        assert col in cols, f"{col} was not repaired onto the legacy gyms table"
    db.gym_upsert("legacygym", display_name="Legacy",
                  upload_link="https://echo.example/u/x.y")
    assert db.gym_get("legacygym", _shared_read=False)["upload_link"]


# ---- 12. off by default ------------------------------------------------------

def test_no_creds_means_no_network_and_unchanged_behaviour(monkeypatch):
    calls = []
    monkeypatch.setattr(gss.SharedGymStore, "_client",
                        lambda self: calls.append("used") or (_ for _ in ()).throw(
                            AssertionError("network touched without creds")))
    db.gym_upsert("nocreds", display_name="No Creds")
    assert db.gym_get("nocreds")["display_name"] == "No Creds"
    assert db.gym_list()
    assert not calls


def test_kill_switch_disables_the_mirror_even_with_creds(armed, monkeypatch):
    monkeypatch.setenv("AGENT_GYM_SHARED_STORE", "false")
    fake = FakePostgrest()
    monkeypatch.setattr(gss.SharedGymStore, "_client", lambda self: fake)
    db.gym_upsert("killswitch", display_name="Kill Switch")
    assert not fake.calls, "AGENT_GYM_SHARED_STORE=false must stop every shared call"
    assert db.gym_get("killswitch")["display_name"] == "Kill Switch"


# ---- safety defaults are untouched ------------------------------------------

def test_the_shared_store_carries_no_trust_or_publish_kill_switch():
    """Trust still fails safe to FULL_APPROVAL from accounts.py, and the publish
    kill switch is still the worker's AGENT_PUBLISH_ENABLED env var. Neither is a
    mirrored column, so nothing here can loosen either guardrail."""
    assert "trust" not in gss.MIRRORED_COLUMNS
    assert "trust_level" not in gss.MIRRORED_COLUMNS
    from agent import trust
    assert int(trust.default_trust_for_new_account()) == 0
