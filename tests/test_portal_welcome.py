"""
Portal welcome-trigger tests. Offline (fake http, fake host_fn, no network, no R2, no
live Supabase). Covers the fix that portal-added clients (who have no Stripe record) get
welcomed:

  * PortalGymsReader discovers the REAL columns first (select=*&limit=1) and only ever
    SELECTs columns that actually exist (information_schema-driven column use).
  * creds absent -> list is empty, nothing is read (Stripe path byte-for-byte unchanged).
  * a portal gym not yet welcomed -> enqueued with a feed + a genuine 9:16 story.
  * already-welcomed (ledger) / already-queued (same gym, either source) -> skipped.
  * logo-less (no override, no scrapable domain) -> needs_logo, NOT enqueued.
  * dedup a client present in BOTH Stripe and portal -> welcomed once.
"""

import os
import re
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, portal_gyms, welcome_queue, welcome_posts  # noqa: E402
from agent import website_scan  # noqa: E402

_DASH = re.compile(r"[‐-―−\-]")

# The REAL portal `gyms` columns (verified against project ooqcvmcjspeltuuhcvlh): NO
# website / domain / site / url column, NO stripe_customer_id. id/name/slug/created_at
# plus flags are what the reader keys on.
_REAL_COLS = {
    "id", "name", "slug", "market", "gym_brand", "status", "created_at",
    "updated_at", "is_demo", "load_test", "is_verification", "tier",
}


# ---- a tiny fake PostgREST http client -------------------------------------------------

class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._payload


class _FakeHTTP:
    """Records every GET and answers the column probe then the row query. `sample_row`
    is what select=*&limit=1 returns (drives column discovery); `rows` is the filtered
    list query result."""

    def __init__(self, sample_row, rows):
        self._sample_row = sample_row
        self._rows = rows
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        if (params or {}).get("select") == "*":
            return _Resp([self._sample_row] if self._sample_row else [])
        return _Resp(list(self._rows))


def _sample():
    # one representative row carrying EXACTLY the real columns, all-null values fine
    return {c: None for c in _REAL_COLS}


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://portal.example.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-xyz")


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("AGENT_WELCOME_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")


def _fake_host(path):
    return f"https://cdn.test/{os.path.basename(path)}"


def _ok_logo():
    return website_scan.LogoResult(website_scan.STATUS_OK, "/tmp/logo.png",
                                   "override", (400, 400), note="test")


def _no_logo():
    return website_scan.LogoResult(website_scan.STATUS_NOT_FOUND,
                                   note="no website on record")


def _stub_render(monkeypatch, tmp_path):
    """Make generate_posts return a real 9:16 story + a feed, without the heavy render."""
    feed = str(tmp_path / "feed.png")
    story = str(tmp_path / "story.png")
    Image.new("RGB", (1080, 1350), (10, 20, 30)).save(feed)
    Image.new("RGB", (1080, 1920), (10, 20, 30)).save(story)

    def fake_generate(template_id, gym_name, owner_name, logo_path, out_dir,
                      bg_client=None, cache_dir=None):
        return {"feed": feed, "story": story}

    monkeypatch.setattr(welcome_posts, "generate_posts", fake_generate)
    return feed, story


# ---- reader: column discovery + real-column-only select --------------------------------

def test_reader_creds_absent_returns_empty(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    # http must never be touched when creds are absent
    http = _FakeHTTP(_sample(), [{"id": "x", "name": "N", "created_at": "2026-08-01"}])
    r = portal_gyms.PortalGymsReader(http=http)
    assert r.list_recent_portal_gyms(days=45) == []
    assert http.calls == []  # nothing read


def test_reader_selects_only_real_columns(creds):
    row = {"id": "g1", "name": "Westwood Athletics", "slug": "westwood",
           "created_at": "2026-08-05T00:00:00+00:00"}
    http = _FakeHTTP(_sample(), [row])
    r = portal_gyms.PortalGymsReader(http=http)
    out = r.list_recent_portal_gyms(days=45)
    assert len(out) == 1 and out[0]["name"] == "Westwood Athletics"
    assert out[0]["gym_id"] == "g1" and out[0]["domain"] == ""
    # the SECOND call (the row query) must select ONLY columns that exist. Since the
    # real table has no website/domain/site/url, none of those appear in the select.
    row_query = [c for c in http.calls if c["params"].get("select") != "*"][0]
    selected = set(row_query["params"]["select"].split(","))
    assert "id" in selected and "name" in selected and "created_at" in selected
    assert not ({"domain", "website", "site", "url"} & selected)


def test_reader_enriches_owner_name_from_onboarding_intake(creds):
    # the gyms table has no owner column; the owner the client typed lives in
    # onboarding_intake. list_recent must join it so a welcome card gets the owner.
    gym_row = {"id": "g1", "name": "Project Evolve Personal Training",
               "created_at": "2026-08-05T00:00:00+00:00"}

    class _HTTP:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, headers=None, timeout=None):
            self.calls.append({"url": url, "params": params or {}})
            if (params or {}).get("select") == "*":
                return _Resp([_sample()])
            if "onboarding_intake" in url:
                return _Resp([{"gym_id": "g1", "owner_name": "JAKE RALEIGH"}])
            return _Resp([gym_row])

    out = portal_gyms.PortalGymsReader(http=_HTTP()).list_recent_portal_gyms(days=45)
    assert len(out) == 1
    assert out[0]["owner_name"] == "JAKE RALEIGH"


def test_reader_owner_missing_stays_blank(creds):
    gym_row = {"id": "g2", "name": "No Owner Gym", "created_at": "2026-08-05"}

    class _HTTP:
        def get(self, url, params=None, headers=None, timeout=None):
            if (params or {}).get("select") == "*":
                return _Resp([_sample()])
            if "onboarding_intake" in url:
                return _Resp([])              # no intake row -> no owner
            return _Resp([gym_row])

    out = portal_gyms.PortalGymsReader(http=_HTTP()).list_recent_portal_gyms(days=45)
    assert out[0]["owner_name"] == ""         # blank, never a crash


def test_reader_excludes_demo_and_test_gyms(creds):
    rows = [
        {"id": "real", "name": "Project Evolve", "created_at": "2026-08-05"},
        {"id": "d", "name": "Demo Gym", "created_at": "2026-08-05", "is_demo": True},
        {"id": "t", "name": "LT Gym", "created_at": "2026-08-05", "load_test": True},
        {"id": "v", "name": "Verify Gym", "created_at": "2026-08-05",
         "is_verification": True},
        {"id": "noname", "name": "  ", "created_at": "2026-08-05"},
    ]
    http = _FakeHTTP(_sample(), rows)
    out = portal_gyms.PortalGymsReader(http=http).list_recent_portal_gyms()
    assert [g["name"] for g in out] == ["Project Evolve"]


def test_reader_empty_table_discovers_no_columns_and_returns_empty(creds):
    # a truly empty table: select=*&limit=1 -> [], so id/name/created_at are unknown and
    # the reader reads nothing rather than SELECT a possibly-absent column.
    http = _FakeHTTP(None, [])
    assert portal_gyms.PortalGymsReader(http=http).list_recent_portal_gyms() == []


# ---- trigger: gating -------------------------------------------------------------------

def test_portal_scan_skips_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_WELCOME_QUEUE_ENABLED", raising=False)
    out = welcome_queue.scan_portal_and_enqueue()
    assert out["scanned"] is False and "off" in out["reason"].lower()


def test_portal_scan_force_still_needs_hosting(monkeypatch):
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    out = welcome_queue.scan_portal_and_enqueue(force=True)
    assert out["scanned"] is False and "hosting" in out["reason"].lower()


def test_portal_scan_creds_absent_noops(armed, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    out = welcome_queue.scan_portal_and_enqueue()
    assert out["scanned"] is True and out["portal_seen"] == 0 and out["enqueued"] == 0
    assert welcome_queue.queue_status() == []  # Stripe path untouched


# ---- trigger: enqueue a ready portal gym (feed + 9:16 story) ---------------------------

def test_portal_gym_enqueued_with_feed_and_9_16_story(armed, creds, monkeypatch, tmp_path):
    _stub_render(monkeypatch, tmp_path)
    row = {"id": "024911d4", "name": "Westwood Athletics", "slug": "westwood",
           "created_at": "2026-08-05T00:00:00+00:00"}
    reader = portal_gyms.PortalGymsReader(http=_FakeHTTP(_sample(), [row]))
    out = welcome_queue.scan_portal_and_enqueue(reader=reader, scraper=lambda *a, **k: _ok_logo(),
                                                host_fn=_fake_host)
    assert out["enqueued"] == 1 and out["source"] == "portal"
    rows = welcome_queue.queue_status()
    assert len(rows) == 1 and rows[0]["name"] == "Westwood Athletics"
    # ledger stamped under the portal key so a re-scan never re-queues it
    assert welcome_posts.already_welcomed("portal:024911d4")
    # a genuine 9:16 story_url was hosted (not empty), feed present
    served = welcome_queue.next_for_day("2026-08-06")
    assert served["feed_url"] and served["story_url"]
    assert not _DASH.search(served["caption"])


def test_portal_gym_already_welcomed_is_skipped(armed, creds, monkeypatch, tmp_path):
    _stub_render(monkeypatch, tmp_path)
    welcome_posts.mark_welcomed("portal:pe1")
    row = {"id": "pe1", "name": "Project Evolve", "created_at": "2026-08-05"}
    reader = portal_gyms.PortalGymsReader(http=_FakeHTTP(_sample(), [row]))
    out = welcome_queue.scan_portal_and_enqueue(reader=reader,
                                                scraper=lambda *a, **k: _ok_logo(),
                                                host_fn=_fake_host)
    assert out["enqueued"] == 0 and out["already_welcomed"] == 1
    assert welcome_queue.queue_status() == []


def test_portal_gym_logoless_is_needs_logo_not_enqueued(armed, creds, monkeypatch, tmp_path):
    _stub_render(monkeypatch, tmp_path)
    row = {"id": "nolomo", "name": "No Logo Gym", "created_at": "2026-08-05"}
    reader = portal_gyms.PortalGymsReader(http=_FakeHTTP(_sample(), [row]))
    # no override on disk + no domain to scrape -> scraper returns NOT_FOUND
    out = welcome_queue.scan_portal_and_enqueue(reader=reader,
                                                scraper=lambda *a, **k: _no_logo(),
                                                host_fn=_fake_host)
    assert out["enqueued"] == 0 and out["needs_logo"] == 1
    assert welcome_queue.queue_status() == []
    # never stamped, so a later logo drop lets it enqueue on the next scan
    assert not welcome_posts.already_welcomed("portal:nolomo")


# ---- portal_domains registry: agent-looked-up domains, no human sends a site -----------

def test_portal_domains_registry_normalizes_name():
    from agent import portal_domains
    # case / punctuation insensitive match to a real, looked-up domain
    assert portal_domains.domain_for("WESTWOOD ATHLETICS") == "westwoodathletics.com"
    assert portal_domains.domain_for("Project Evolve Personal Training") == \
        "projectevolvenaples.com"
    # an un-recorded gym returns "" (stays needs_logo; never a fabricated domain)
    assert portal_domains.domain_for("Some Gym Not In The Registry") == ""


def test_registry_domain_is_passed_to_scraper_for_known_gym(armed, creds, monkeypatch, tmp_path):
    # A portal gym with NO domain column but whose NAME is in portal_domains resolves its
    # real domain from the registry, so the logo scrape runs and it enqueues WITHOUT a human
    # sending the site.
    _stub_render(monkeypatch, tmp_path)
    seen = {}

    def _capture(domain, ak, override_path=None, out_dir=None):
        seen["domain"] = domain
        return _ok_logo()

    row = {"id": "pe_reg_known", "name": "Project Evolve Personal Training",
           "created_at": "2026-08-06T00:00:00+00:00"}
    reader = portal_gyms.PortalGymsReader(http=_FakeHTTP(_sample(), [row]))
    out = welcome_queue.scan_portal_and_enqueue(reader=reader, scraper=_capture,
                                                host_fn=_fake_host)
    assert out["enqueued"] == 1
    assert seen["domain"] == "projectevolvenaples.com"  # from the registry, not ""


def test_registry_unknown_gym_passes_empty_domain_and_needs_logo(armed, creds, monkeypatch,
                                                                 tmp_path):
    # A gym NOT in the registry passes "" to the scraper (no fabricated domain) and stays
    # needs_logo when the scrape finds nothing.
    _stub_render(monkeypatch, tmp_path)
    seen = {}

    def _capture(domain, ak, override_path=None, out_dir=None):
        seen["domain"] = domain
        return _no_logo()

    row = {"id": "unk_reg", "name": "Totally Unknown Gym ZZ", "created_at": "2026-08-06"}
    reader = portal_gyms.PortalGymsReader(http=_FakeHTTP(_sample(), [row]))
    out = welcome_queue.scan_portal_and_enqueue(reader=reader, scraper=_capture,
                                                host_fn=_fake_host)
    assert seen["domain"] == ""
    assert out["needs_logo"] == 1 and out["enqueued"] == 0


def test_portal_scan_is_idempotent_on_rerun(armed, creds, monkeypatch, tmp_path):
    _stub_render(monkeypatch, tmp_path)
    row = {"id": "g9", "name": "Repeat Gym", "created_at": "2026-08-05"}

    def fresh_reader():
        return portal_gyms.PortalGymsReader(http=_FakeHTTP(_sample(), [row]))

    first = welcome_queue.scan_portal_and_enqueue(reader=fresh_reader(),
                                                  scraper=lambda *a, **k: _ok_logo(),
                                                  host_fn=_fake_host)
    second = welcome_queue.scan_portal_and_enqueue(reader=fresh_reader(),
                                                   scraper=lambda *a, **k: _ok_logo(),
                                                   host_fn=_fake_host)
    assert first["enqueued"] == 1 and second["enqueued"] == 0
    assert len(welcome_queue.queue_status()) == 1


# ---- dedup a client present in BOTH Stripe and portal ----------------------------------

def test_dedup_when_gym_already_queued_by_stripe(armed, creds, monkeypatch, tmp_path):
    _stub_render(monkeypatch, tmp_path)
    # Stripe already queued this gym under a DOMAIN key (different key than the portal id)
    stripe_entry = {"gym_key": "domain:westwood.com", "name": "Westwood Athletics",
                    "owner": "", "template": "T1", "tier_label": "Launch",
                    "posts": {"feed": str(tmp_path / "s_feed.png"),
                              "story": str(tmp_path / "s_story.png")}}
    Image.new("RGB", (1080, 1350), (0, 0, 0)).save(stripe_entry["posts"]["feed"])
    Image.new("RGB", (1080, 1920), (0, 0, 0)).save(stripe_entry["posts"]["story"])
    assert welcome_queue.enqueue(stripe_entry, host_fn=_fake_host) is not None

    # now the portal scan sees the SAME gym by name -> must NOT welcome it again
    row = {"id": "portal-westwood", "name": "Westwood Athletics", "created_at": "2026-08-05"}
    reader = portal_gyms.PortalGymsReader(http=_FakeHTTP(_sample(), [row]))
    out = welcome_queue.scan_portal_and_enqueue(reader=reader,
                                                scraper=lambda *a, **k: _ok_logo(),
                                                host_fn=_fake_host)
    assert out["enqueued"] == 0 and out["deduped_with_stripe"] == 1
    # still exactly one welcome for that gym
    assert len([r for r in welcome_queue.queue_status()
                if r["name"] == "Westwood Athletics"]) == 1


def test_dedup_holds_after_stripe_gym_served(armed, creds, monkeypatch, tmp_path):
    _stub_render(monkeypatch, tmp_path)
    stripe_entry = {"gym_key": "domain:evolve.com", "name": "Project Evolve",
                    "owner": "", "template": "T1", "tier_label": "",
                    "posts": {"feed": str(tmp_path / "e_feed.png"),
                              "story": str(tmp_path / "e_story.png")}}
    Image.new("RGB", (1080, 1350), (0, 0, 0)).save(stripe_entry["posts"]["feed"])
    Image.new("RGB", (1080, 1920), (0, 0, 0)).save(stripe_entry["posts"]["story"])
    welcome_queue.enqueue(stripe_entry, host_fn=_fake_host)
    welcome_queue.next_for_day("2026-08-06")  # serve it (row stays, status flips)

    row = {"id": "portal-evolve", "name": "Project Evolve", "created_at": "2026-08-05"}
    reader = portal_gyms.PortalGymsReader(http=_FakeHTTP(_sample(), [row]))
    out = welcome_queue.scan_portal_and_enqueue(reader=reader,
                                                scraper=lambda *a, **k: _ok_logo(),
                                                host_fn=_fake_host)
    assert out["deduped_with_stripe"] == 1 and out["enqueued"] == 0
