"""Tests for the auto-welcome-from-new-clients pipeline (offline; Stripe faked)."""
import datetime
import os

import pytest

from agent import welcome_posts as wp, website_scan, db


NOW = datetime.datetime(2026, 8, 4, tzinfo=datetime.timezone.utc)


def _epoch(days_ago):
    return int((NOW - datetime.timedelta(days=days_ago)).timestamp())


def _cutoff(window=45):
    return int((NOW - datetime.timedelta(days=window)).timestamp())


def _sub(days_ago, status="active", product="prod_UegCbUqO3fs1no",
         canceled_at=None, cancel_at=None, sid="sub_1"):
    return {"id": sid, "created": _epoch(days_ago), "status": status,
            "canceled_at": canceled_at, "cancel_at": cancel_at, "product_id": product}


def _cust(cid, subs, email="owner@ironforge.com", name="RYAN PARR",
          business_name="", website=""):
    return {"id": cid, "email": email, "name": name,
            "business_name": business_name, "website": website, "subs": subs}


# ---- normalization ----------------------------------------------------------

@pytest.mark.parametrize("raw,clean", [
    ("RYAN PARR", "Ryan Parr"),
    ("Just Estes", "Just Estes"),
    ("  mary   o'brien ", "Mary O'Brien"),
    ("sean mccoy", "Sean McCoy"),
    ("jean-luc picard", "Jean-Luc Picard"),
    ("", ""),
])
def test_normalize_owner(raw, clean):
    assert wp.normalize_owner(raw) == clean


def test_gym_name_from_domain():
    # a smashed token that has no known gym-word boundary stays joined ("birddog");
    # this is exactly why a domain-inferred name is INFERRED and confirmed by a
    # human before use. CrossFit keeps its brand capitalization.
    assert wp.gym_name_from_domain("birddogcrossfit.com") == "Birddog CrossFit"
    assert wp.gym_name_from_domain("oak-strength.co") == "Oak Strength"
    assert wp.gym_name_from_domain("https://www.summitfitness.com/") == "Summit Fitness"


# ---- classification ---------------------------------------------------------

def test_new_client_first_sub_in_window_active_core_tier():
    c = _cust("c1", [_sub(10)])
    r = wp.classify(c, _cutoff())
    assert r["status"] == wp.NEW and r["tier_label"] == "Launch"


def test_existing_client_adding_product_not_new():
    # first sub is 200 days old; a new product added 5 days ago
    c = _cust("c2", [_sub(200, sid="old"), _sub(5, sid="new")])
    r = wp.classify(c, _cutoff())
    assert r["status"] == wp.EXISTING_ADDING


def test_sponsor_with_no_subscription_ignored():
    c = _cust("inbody", [])
    r = wp.classify(c, _cutoff())
    assert r["status"] == wp.IGNORED and "sponsor" in r["reason"]


def test_delinquent_first_sub_excluded():
    c = _cust("c3", [_sub(10, status="past_due")])
    r = wp.classify(c, _cutoff())
    assert r["status"] == wp.IGNORED and "delinquent" in r["reason"]


def test_canceled_first_sub_excluded():
    c = _cust("c4", [_sub(10, cancel_at=_epoch(-5))])
    r = wp.classify(c, _cutoff())
    assert r["status"] == wp.IGNORED


def test_non_core_tier_excluded():
    # $250 weekly product is NOT a core tier
    c = _cust("c5", [_sub(10, product="prod_T2JYk7PBMzG8hl")])
    r = wp.classify(c, _cutoff())
    assert r["status"] == wp.IGNORED and "core tier" in r["reason"]


def test_first_sub_before_window_excluded():
    c = _cust("c6", [_sub(90)])
    r = wp.classify(c, _cutoff())
    assert r["status"] == wp.IGNORED


# ---- dedupe + resolution ----------------------------------------------------

def test_dedupe_key_same_domain_collapses():
    a = _cust("a", [_sub(5)], email="al@ironforge.com")
    b = _cust("b", [_sub(5)], email="bo@ironforge.com")
    assert wp.gym_dedupe_key(a) == wp.gym_dedupe_key(b)


def test_resolve_gym_business_name_confirmed():
    c = _cust("c", [_sub(5)], business_name="Iron Forge Fitness")
    g = wp.resolve_gym(c)
    assert g["confidence"] == wp.CONFIRMED and g["name"] == "Iron Forge Fitness"


def test_resolve_gym_domain_inferred():
    c = _cust("c", [_sub(5)], email="owner@birddogcrossfit.com", name="Sam", business_name="")
    g = wp.resolve_gym(c)
    assert g["confidence"] == wp.INFERRED and g["source"] == "email_domain"


def test_resolve_gym_portal_confirmed():
    c = _cust("c", [_sub(5)], business_name="")
    row = {"account_key": "ironforge_ig", "gym_name": "Iron Forge Fitness"}
    g = wp.resolve_gym(c, portal_row=row)
    assert g["confidence"] == wp.CONFIRMED and g["source"] == "portal"


def test_pick_template_deterministic_and_in_rotation():
    t1 = wp.pick_template("domain:ironforge.com")
    t2 = wp.pick_template("domain:ironforge.com")
    assert t1 == t2 and t1 in wp.ROTATION


# ---- ledger -----------------------------------------------------------------

def test_ledger_marks_and_reads():
    assert not wp.already_welcomed("domain:x.com")
    wp.mark_welcomed("domain:x.com")
    assert wp.already_welcomed("domain:x.com")


# ---- backfill end to end (fake reader + fake scraper) -----------------------

class FakeReader:
    def __init__(self, customers):
        self._c = customers

    def available(self):
        return True

    def customers(self):
        return self._c


def _ok_scraper(png):
    def scrape(website, account_key, out_dir=None, **kw):
        return website_scan.LogoResult(website_scan.STATUS_OK, png, "og:image", (400, 400))
    return scrape


def _make_png(tmp_path):
    from PIL import Image
    p = str(tmp_path / "logo.png")
    Image.new("RGBA", (400, 400), (20, 30, 60, 255)).save(p)
    return p


def test_backfill_includes_confirmed_new_client(tmp_path):
    png = _make_png(tmp_path)
    reader = FakeReader([_cust("c1", [_sub(10)], business_name="Iron Forge Fitness")])
    rep = wp.backfill(now=NOW, reader=reader, scraper=_ok_scraper(png),
                      portal_lookup=lambda c: None,
                      out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "bg"))
    assert len(rep["included"]) == 1
    g = rep["included"][0]
    assert g["confidence"] == wp.CONFIRMED and g["tier_label"] == "Launch"
    assert os.path.isfile(g["posts"]["feed"]) and os.path.isfile(g["posts"]["story"])


def test_backfill_excludes_sponsor_and_delinquent(tmp_path):
    png = _make_png(tmp_path)
    reader = FakeReader([
        _cust("inbody", [], business_name="InBody"),
        _cust("late", [_sub(10, status="past_due")], business_name="Late Gym"),
        _cust("good", [_sub(10)], business_name="Good Gym"),
    ])
    rep = wp.backfill(now=NOW, reader=reader, scraper=_ok_scraper(png),
                      portal_lookup=lambda c: None,
                      out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "bg"))
    included = {g["name"] for g in rep["included"]}
    assert included == {"Good Gym"}
    assert len(rep["excluded"]) == 2


def test_backfill_dedupes_two_contacts_one_gym(tmp_path):
    png = _make_png(tmp_path)
    reader = FakeReader([
        _cust("a", [_sub(5)], email="al@ironforge.com", business_name="Iron Forge"),
        _cust("b", [_sub(5)], email="bo@ironforge.com", business_name="Iron Forge"),
    ])
    rep = wp.backfill(now=NOW, reader=reader, scraper=_ok_scraper(png),
                      portal_lookup=lambda c: None,
                      out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "bg"))
    assert len(rep["included"]) == 1
    assert len(rep["collapsed"]) == 1


def test_backfill_inferred_name_needs_confirmation_no_post(tmp_path):
    png = _make_png(tmp_path)
    # no business name, only a domain => INFERRED => held for yes/no, no post generated
    reader = FakeReader([_cust("c", [_sub(5)], email="o@birddogcrossfit.com",
                               name="Sam", business_name="")])
    rep = wp.backfill(now=NOW, reader=reader, scraper=_ok_scraper(png),
                      portal_lookup=lambda c: None,
                      out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "bg"))
    assert not rep["included"]
    assert len(rep["needs_confirmation"]) == 1
    assert "posts" not in rep["needs_confirmation"][0]


def test_backfill_logo_not_found_surfaces_needs_logo(tmp_path):
    def no_logo(website, account_key, out_dir=None, **kw):
        return website_scan.LogoResult(website_scan.STATUS_NOT_FOUND, note="none")
    reader = FakeReader([_cust("c", [_sub(5)], business_name="Iron Forge Fitness")])
    rep = wp.backfill(now=NOW, reader=reader, scraper=no_logo,
                      portal_lookup=lambda c: None,
                      out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "bg"))
    assert not rep["included"] and len(rep["needs_logo"]) == 1


def test_backfill_never_welcomes_twice(tmp_path):
    png = _make_png(tmp_path)
    reader = FakeReader([_cust("c1", [_sub(10)], business_name="Iron Forge Fitness")])
    args = dict(now=NOW, reader=reader, scraper=_ok_scraper(png),
                portal_lookup=lambda c: None,
                out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "bg"))
    rep1 = wp.backfill(**args)
    g = rep1["included"][0]
    wp.mark_welcomed(g["gym_key"])          # simulate approval/post
    rep2 = wp.backfill(**args)
    assert not rep2["included"] and len(rep2["already_welcomed"]) == 1


def test_backfill_no_stripe_key_reports_and_does_nothing(tmp_path):
    class Empty:
        def available(self):
            return False
    rep = wp.backfill(now=NOW, reader=Empty(), out_dir=str(tmp_path))
    assert rep["included"] == [] and "error" in rep and "STRIPE_API_KEY" in rep["error"]


# ---- surfacing (nothing publishes) ------------------------------------------

class FakePoster:
    def __init__(self):
        self._channel = "C123"
        self.posts = []

    def _chat_post(self, text, blocks, channel=None, thread_ts=None):
        self.posts.append({"text": text, "blocks": blocks, "thread_ts": thread_ts})
        return {"ts": f"ts{len(self.posts)}"}


def test_surface_posts_feed_and_story_never_publishes(tmp_path):
    png = _make_png(tmp_path)
    reader = FakeReader([_cust("c1", [_sub(10)], business_name="Iron Forge Fitness")])
    rep = wp.backfill(now=NOW, reader=reader, scraper=_ok_scraper(png),
                      portal_lookup=lambda c: None,
                      out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "bg"))
    poster = FakePoster()
    summary = wp.surface_to_slack(rep, poster, lambda p, t: f"https://r2/{os.path.basename(p)}")
    assert summary["posted"] == 1
    joined = " ".join(p["text"] for p in poster.posts)
    assert "Iron Forge Fitness" in joined
    # a story image was threaded under the gym message
    assert any(p["thread_ts"] for p in poster.posts)
    # nothing about publishing to a client account
    assert all("publish" not in str(p["blocks"]).lower() or "held" in str(p["blocks"]).lower()
               for p in poster.posts)


def test_surface_stamps_ledger_so_second_run_skips(tmp_path):
    # regression (audit CRITICAL): surfacing must stamp the ledger itself, so a
    # second backfill run does NOT re-welcome the same gym.
    png = _make_png(tmp_path)
    reader = FakeReader([_cust("c1", [_sub(10)], business_name="Iron Forge Fitness")])
    args = dict(now=NOW, reader=reader, scraper=_ok_scraper(png),
                portal_lookup=lambda c: None,
                out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "bg"))
    rep1 = wp.backfill(**args)
    poster = FakePoster()
    wp.surface_to_slack(rep1, poster, lambda p, t: f"https://r2/{os.path.basename(p)}")
    assert wp.already_welcomed(rep1["included"][0]["gym_key"])
    # a second backfill now skips it as already-welcomed, generates no new post
    rep2 = wp.backfill(**args)
    assert not rep2["included"] and len(rep2["already_welcomed"]) == 1


def test_portal_logo_override_is_used_over_scrape(tmp_path, monkeypatch):
    # regression (audit MAJOR): a human-dropped portal logo must win over the scrape.
    logo_root = tmp_path / "logos"
    monkeypatch.setenv("AGENT_WELCOME_LOGO_DIR", str(logo_root))
    override_dir = logo_root / "overrides"
    override_dir.mkdir(parents=True)
    from PIL import Image
    Image.new("RGBA", (500, 500), (200, 20, 20, 255)).save(
        str(override_dir / "ironforge_ig.png"))

    used = {}

    def spy_scraper(website, account_key, override_path=None, out_dir=None, **kw):
        used["override_path"] = override_path
        return website_scan.LogoResult(website_scan.STATUS_OK,
                                       override_path or "scraped", "override", (500, 500))
    reader = FakeReader([_cust("c1", [_sub(10)], business_name="Iron Forge")])
    row = {"account_key": "ironforge_ig", "gym_name": "Iron Forge Fitness"}
    wp.backfill(now=NOW, reader=reader, scraper=spy_scraper,
                portal_lookup=lambda c: row,
                out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "bg"))
    assert used["override_path"] and used["override_path"].endswith("ironforge_ig.png")


def test_surface_reports_blocked_when_no_stripe(tmp_path):
    poster = FakePoster()
    rep = {"error": "no Stripe key (set STRIPE_API_KEY ...)", "included": [],
           "window_days": 45, "needs_confirmation": [], "needs_logo": [],
           "excluded": [], "collapsed": [], "already_welcomed": []}
    out = wp.surface_to_slack(rep, poster, lambda p, t: "u")
    assert out["posted"] == 0 and "error" in out
    assert "blocked" in poster.posts[0]["text"].lower()
