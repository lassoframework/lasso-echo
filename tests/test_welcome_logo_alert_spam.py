"""
Welcome-queue alert-spam fix tests (Blake's Slack, 2026-08-27: ~30 repeated
"BLOCKED: no usable logo" lines per sweep, naming lead stubs and archived
duplicates, re-fired every scan). Offline (fake http, fake host_fn, no network).

Three layers under test:

  1. SOURCE HYGIENE: the portal reader excludes rows in a known non-client status
     (onboarding lead stubs like 'Dean Holcomb', inactive, archived duplicates like
     the second 'Bird Dog CrossFit') — evidence: the live gyms table, queried
     2026-08-27. Unknown/NULL statuses are kept (never drop a possibly-real gym).

  2. ALERT DEDUP: blocked-on-logo gyms collapse into AT MOST ONE Slack summary line
     per sweep, stamped per (gym, block state) in kv; the same broken state next
     sweep is silent, a state change (domain change, override appearing) re-fires,
     and an ephemeral kv store suppresses Slack entirely (durable-or-silent).

  3. PRUNE: queued portal entries whose live gyms row now fails the filter (or is
     gone) are removed — logged, audited, served/Stripe rows untouched, a failed
     portal read prunes NOTHING, dry-run deletes nothing.
"""

import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, portal_gyms, welcome_posts, welcome_queue  # noqa: E402
from agent import website_scan  # noqa: E402

# The REAL portal `gyms` columns (verified against project ooqcvmcjspeltuuhcvlh).
_REAL_COLS = {
    "id", "name", "slug", "market", "gym_brand", "status", "created_at",
    "updated_at", "is_demo", "load_test", "is_verification", "tier",
}


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._payload


class _FakeHTTP:
    """Answers the column probe (select=*) then the row query."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        if (params or {}).get("select") == "*":
            return _Resp([{c: None for c in _REAL_COLS}])
        if "onboarding_intake" in url:
            return _Resp([])
        return _Resp(list(self._rows))


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://portal.example.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-xyz")


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_WELCOME_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")


@pytest.fixture
def alerts(monkeypatch):
    """Capture every ops alert the welcome queue would send."""
    sent = []
    monkeypatch.setattr(welcome_queue.ops_alerts, "alert",
                        lambda msg, **kw: sent.append(msg))
    return sent


def _fake_host(path):
    return f"https://cdn.test/{os.path.basename(path)}"


def _ok_logo():
    return website_scan.LogoResult(website_scan.STATUS_OK, "/tmp/logo.png",
                                   "override", (400, 400), note="test")


def _no_logo():
    return website_scan.LogoResult(website_scan.STATUS_NOT_FOUND,
                                   note="no website on record")


def _stub_render(monkeypatch, tmp_path):
    feed = str(tmp_path / "feed.png")
    story = str(tmp_path / "story.png")
    Image.new("RGB", (1080, 1350), (10, 20, 30)).save(feed)
    Image.new("RGB", (1080, 1920), (10, 20, 30)).save(story)
    monkeypatch.setattr(
        welcome_posts, "generate_posts",
        lambda template_id, gym_name, owner_name, logo_path, out_dir,
        bg_client=None, cache_dir=None: {"feed": feed, "story": story})


def _row(gym_id, name, status="active", **extra):
    r = {"id": gym_id, "name": name, "status": status,
         "created_at": "2026-08-05T00:00:00+00:00"}
    r.update(extra)
    return r


def _scan(rows, scraper, **kw):
    reader = portal_gyms.PortalGymsReader(http=_FakeHTTP(rows))
    return welcome_queue.scan_portal_and_enqueue(reader=reader, scraper=scraper,
                                                 host_fn=_fake_host, **kw)


def _queue_entry(gym_key, name, tmp_path):
    feed = str(tmp_path / f"f_{name.replace(' ', '_')}.png")
    story = str(tmp_path / f"s_{name.replace(' ', '_')}.png")
    Image.new("RGB", (1080, 1350), (0, 0, 0)).save(feed)
    Image.new("RGB", (1080, 1920), (0, 0, 0)).save(story)
    entry = {"gym_key": gym_key, "name": name, "owner": "", "template": "T1",
             "tier_label": "", "posts": {"feed": feed, "story": story}}
    assert welcome_queue.enqueue(entry, host_fn=_fake_host) is not None
    return entry


class _IdsReader:
    """Fake reader for the prune: gyms_by_ids returns canned rows or raises."""

    def __init__(self, rows=None, error=None):
        self._rows = rows or []
        self._error = error
        self.asked = []

    def gyms_by_ids(self, gym_ids):
        self.asked.append(list(gym_ids))
        if self._error:
            raise self._error
        return list(self._rows)


# ---- 1. SOURCE HYGIENE: non-client statuses never enter the scan -----------------------

def test_reader_excludes_known_non_client_statuses(creds):
    rows = [
        _row("g-active", "Bell House CrossFit", status="active"),
        _row("g-lead", "Dean Holcomb", status="onboarding"),
        _row("g-arch", "Bird Dog CrossFit", status="archived"),
        _row("g-inact", "Old Gym", status="inactive"),
        _row("g-null", "Null Status Gym", status=None),
        _row("g-new", "Future Status Gym", status="some_future_status"),
    ]
    out = portal_gyms.PortalGymsReader(http=_FakeHTTP(rows)).list_recent_portal_gyms()
    # active kept; onboarding/archived/inactive gone; NULL + unknown kept (fail open)
    assert [g["name"] for g in out] == \
        ["Bell House CrossFit", "Null Status Gym", "Future Status Gym"]


def test_is_excluded_is_case_insensitive_and_flag_aware():
    assert portal_gyms.is_excluded({"status": "ONBOARDING"})
    assert portal_gyms.is_excluded({"status": " Archived "})
    assert portal_gyms.is_excluded({"status": "active", "is_demo": True})
    assert not portal_gyms.is_excluded({"status": "active"})
    assert not portal_gyms.is_excluded({})  # missing everything = kept


def test_person_name_lead_never_reaches_the_logo_gate(armed, creds, alerts):
    # A lead stub in 'onboarding' does not scan at all: no needs_logo, no alert.
    out = _scan([_row("lead1", "Juan Martinez", status="onboarding")],
                scraper=lambda *a, **k: _no_logo())
    assert out["portal_seen"] == 0 and out["needs_logo"] == 0
    assert alerts == []


# ---- 2. ALERT DEDUP: one summary line per sweep, state-keyed stamps ---------------------

def test_blocked_gyms_collapse_into_one_summary_line(armed, creds, alerts):
    rows = [_row("b1", "Bell House CrossFit"), _row("b2", "Pierce Fitness"),
            _row("b3", "CrossFit Sabal Park")]
    out = _scan(rows, scraper=lambda *a, **k: _no_logo())
    assert out["needs_logo"] == 3 and out["logo_alerted_new"] == 3
    assert len(alerts) == 1  # ONE line, not three
    assert "3 blocked on logo (deduped), 3 new" in alerts[0]
    for name in ("Bell House CrossFit", "Pierce Fitness", "CrossFit Sabal Park"):
        assert name in alerts[0]


def test_same_block_state_next_sweep_is_silent(armed, creds, alerts):
    rows = [_row("b1", "Bell House CrossFit")]
    _scan(rows, scraper=lambda *a, **k: _no_logo())
    assert len(alerts) == 1
    out2 = _scan(rows, scraper=lambda *a, **k: _no_logo())
    assert out2["needs_logo"] == 1 and out2["logo_alerted_new"] == 0
    assert len(alerts) == 1  # no re-alert on an unchanged state


def test_domain_change_refires_once(armed, creds, alerts, monkeypatch):
    rows = [_row("b1", "Bell House CrossFit")]
    _scan(rows, scraper=lambda *a, **k: _no_logo())
    assert len(alerts) == 1
    # the state changes: a domain is now on record, but the scrape still fails
    monkeypatch.setattr(welcome_queue.portal_domains, "domain_for",
                        lambda name, gym_id=None: "bellhousecrossfit.com")
    _scan(rows, scraper=lambda *a, **k: _no_logo())
    assert len(alerts) == 2 and "1 new" in alerts[1]
    # and the NEW state deduped in turn
    _scan(rows, scraper=lambda *a, **k: _no_logo())
    assert len(alerts) == 2


def test_stamp_clears_when_logo_resolves(armed, creds, alerts, monkeypatch, tmp_path):
    _stub_render(monkeypatch, tmp_path)
    rows = [_row("b1", "Bell House CrossFit")]
    _scan(rows, scraper=lambda *a, **k: _no_logo())
    ak = welcome_queue._portal_ak("b1")
    assert db.kv_get(welcome_queue._LOGO_ALERT_PREFIX + ak)
    # a logo now resolves: the gym enqueues and the stamp clears (a future
    # re-block alerts fresh instead of dying against a stale stamp)
    out = _scan(rows, scraper=lambda *a, **k: _ok_logo())
    assert out["enqueued"] == 1
    assert not db.kv_get(welcome_queue._LOGO_ALERT_PREFIX + ak)
    assert len(alerts) == 1  # only the original block line ever went out


def test_ephemeral_kv_store_stays_off_slack(armed, creds, alerts, monkeypatch):
    # durable-or-silent (the gritx storm rule): stamps that cannot persist must
    # not gate a Slack line, so an ephemeral process never storms the channel.
    monkeypatch.setattr(welcome_queue.db, "kv_is_durable", lambda: False)
    out = _scan([_row("b1", "Bell House CrossFit")],
                scraper=lambda *a, **k: _no_logo())
    assert out["needs_logo"] == 1 and out["logo_alerted_new"] == 0
    assert alerts == []


# ---- 3. PRUNE: queued portal junk removed, everything else untouched --------------------

def test_prune_removes_filtered_and_gone_keeps_active(tmp_path):
    _queue_entry("portal:lead1", "Dean Holcomb", tmp_path)          # now onboarding
    _queue_entry("portal:arch1", "Bird Dog CrossFit", tmp_path)     # row deleted
    _queue_entry("portal:real1", "Bell House CrossFit", tmp_path)   # still active
    reader = _IdsReader(rows=[
        {"id": "lead1", "name": "Dean Holcomb", "status": "onboarding"},
        {"id": "real1", "name": "Bell House CrossFit", "status": "active"},
    ])
    out = welcome_queue.prune_portal_junk(reader=reader)
    assert sorted(p["name"] for p in out["pruned"]) == \
        ["Bird Dog CrossFit", "Dean Holcomb"]
    assert out["kept"] == ["Bell House CrossFit"]
    names = [r["name"] for r in welcome_queue.queue_status()]
    assert names == ["Bell House CrossFit"]
    # ledger un-stamped so a later REAL activation welcomes fresh
    assert not welcome_posts.already_welcomed("portal:lead1")
    assert not welcome_posts.already_welcomed("portal:arch1")
    assert welcome_posts.already_welcomed("portal:real1")
    # reader was asked about exactly the queued portal ids
    assert sorted(reader.asked[0]) == ["arch1", "lead1", "real1"]


def test_prune_clears_the_logo_alert_stamp(tmp_path):
    _queue_entry("portal:lead1", "Dean Holcomb", tmp_path)
    ak = welcome_queue._portal_ak("lead1")
    db.kv_set(welcome_queue._LOGO_ALERT_PREFIX + ak, "blocked|override=no|domain=none")
    reader = _IdsReader(rows=[
        {"id": "lead1", "name": "Dean Holcomb", "status": "onboarding"}])
    welcome_queue.prune_portal_junk(reader=reader)
    assert not db.kv_get(welcome_queue._LOGO_ALERT_PREFIX + ak)


def test_prune_never_touches_served_or_stripe_rows(tmp_path):
    _queue_entry("cust:cus_123", "Stripe Gym", tmp_path)             # Stripe-keyed
    _queue_entry("portal:served1", "Served Portal Gym", tmp_path)
    # serve the portal gym: it is posted history now
    assert welcome_queue.next_for_day("2026-08-27")["name"] == "Stripe Gym"
    assert welcome_queue.serve_one_more("2026-08-27")["name"] == "Served Portal Gym"
    reader = _IdsReader(rows=[])  # everything the reader knows is "gone"
    out = welcome_queue.prune_portal_junk(reader=reader)
    assert out["pruned"] == [] and out["kept"] == []
    assert reader.asked == []  # nothing queued from the portal -> reader never asked
    assert len(welcome_queue.queue_status()) == 2  # both rows intact


def test_prune_aborts_when_portal_read_fails(tmp_path):
    _queue_entry("portal:x1", "Maybe Real Gym", tmp_path)
    reader = _IdsReader(error=RuntimeError("supabase down"))
    out = welcome_queue.prune_portal_junk(reader=reader)
    assert out["pruned"] == [] and "portal read failed" in out["error"]
    assert out["kept"] == ["Maybe Real Gym"]  # unsure = keep, never guess
    assert len(welcome_queue.queue_status()) == 1


def test_prune_dry_run_deletes_nothing(tmp_path):
    _queue_entry("portal:lead1", "Dean Holcomb", tmp_path)
    reader = _IdsReader(rows=[
        {"id": "lead1", "name": "Dean Holcomb", "status": "onboarding"}])
    out = welcome_queue.prune_portal_junk(reader=reader, dry_run=True)
    assert out["dry_run"] is True
    assert [p["name"] for p in out["pruned"]] == ["Dean Holcomb"]
    assert len(welcome_queue.queue_status()) == 1  # still there
    assert welcome_posts.already_welcomed("portal:lead1")  # ledger untouched


# ---- reader.gyms_by_ids contract --------------------------------------------------------

def test_gyms_by_ids_raises_without_creds(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    with pytest.raises(Exception):
        portal_gyms.PortalGymsReader(http=_FakeHTTP([])).gyms_by_ids(["a"])


def test_gyms_by_ids_returns_raw_rows_including_excluded(creds):
    # the prune must SEE excluded rows to judge them: no filtering here
    rows = [_row("lead1", "Dean Holcomb", status="onboarding")]
    out = portal_gyms.PortalGymsReader(http=_FakeHTTP(rows)).gyms_by_ids(["lead1"])
    assert out and out[0]["status"] == "onboarding"


# ---- 90-day freshness window (Blake 2026-08-27) ------------------------------------

def test_stale_queued_welcome_expires_never_serves(tmp_path, monkeypatch):
    """A welcome queued > MAX_WELCOME_AGE_DAYS ago is expired in place: the gym is
    no longer a new client (and was often already welcomed pre-ledger)."""
    from agent import welcome_queue as wq, db
    with db._lock, wq._conn() as conn:
        conn.execute(
            "INSERT INTO welcome_queue (gym_key, name, status, created_at) "
            "VALUES (?,?,?,?)", ("portal:old", "Old Gym", "queued", "2026-04-01T00:00:00"))
        conn.execute(
            "INSERT INTO welcome_queue (gym_key, name, status, created_at) "
            "VALUES (?,?,?,?)", ("portal:new", "New Gym", "queued", "2026-08-20T00:00:00"))
        conn.commit()
    row = wq.next_for_day("2026-08-27")
    assert row is not None and row["gym_key"] == "portal:new"   # stale one skipped
    with wq._conn() as conn:
        old = conn.execute("SELECT status FROM welcome_queue WHERE gym_key='portal:old'").fetchone()
        assert old["status"] == "expired"                        # never served


def test_fresh_queue_empty_after_all_expire(tmp_path):
    from agent import welcome_queue as wq, db
    with db._lock, wq._conn() as conn:
        conn.execute(
            "INSERT INTO welcome_queue (gym_key, name, status, created_at) "
            "VALUES (?,?,?,?)", ("portal:old2", "Old Gym 2", "queued", "2026-01-01T00:00:00"))
        conn.commit()
    assert wq.next_for_day("2026-08-27") is None
