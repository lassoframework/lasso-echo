"""
Social-intake -> onboarded client. Offline: no Supabase/Gemini call (the live reader
is never invoked; onboarding drafts the bible locally). Uses GritX + Top Fuel-shaped
answers. Asserts: map_answers builds the right bundle with NO fabricated testimonial,
the banned-words list is parsed and rides into the drafted bible, and onboard_from_social
lands APPROVED sources (idempotent) and never clobbers an existing bible.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_sources as cs, social_intake_reader as sir  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    # Pin the DURABLE client-voice dir to the tmp cwd so onboard's bible lands under
    # tmp/brand_voice/<base> deterministically (independent of whether a real /data
    # volume exists on the test host); onboard now writes to config.client_voice_dir().
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", str(tmp_path / "brand_voice"))
    monkeypatch.chdir(tmp_path)          # brand_voice/ docs land under tmp
    yield


def _gritx_answers():
    return {
        "base_key": "gritx",
        "approver": "Ryan Parr",
        "gym": {"name": "GritX", "website": "gritx.com",
                "ig_handle": "@gritx", "fb_page": "GritX"},
        "proof": {"wins": "", "verifiable_numbers": ""},     # NO proof -> no testimonial
        "voice": {"vibe": "warm, encouraging, real",
                  "words_to_use": "strong, consistent, community",
                  "words_to_never_use": "CrossFit, Bootcamp, Cardio, Hyrox, Intensity, Compete"},
        "offers": {"services": "Small group training\nPersonal coaching",
                   "front_door_offer": "21 day kickstart",
                   "exact_price": "$97"},
        "audience": {"ideal_member": "busy parents in their 40s getting back in shape"},
        "media_notes": "no photos yet, house infographics only",
    }


def _topfuel_answers():
    return {
        "base_key": "topfuel",
        "approver": "Dana Cole",
        "gym": {"name": "Top Fuel", "website": "topfuel.fit",
                "ig_handle": "@topfuel", "fb_page": "Top Fuel"},
        "proof": {"wins": "", "verifiable_numbers": ""},     # NO proof -> no testimonial
        "voice": {"vibe": "clean and motivating",
                  "words_to_use": "results, habits",
                  "words_to_never_use": "shred\nshame"},
        "offers": {"services": "Nutrition coaching\nGroup classes",
                   "front_door_offer": "No Sweat Intro",
                   "exact_price": ""},
        "audience": {"ideal_member": "beginners who want a simple plan"},
        "media_notes": "",
    }


# ---- 1. map_answers on GritX: bundle + banned words + bible carries never-use ----
def test_map_answers_gritx():
    m = sir.map_answers(_gritx_answers())
    b = m["bundle"]
    assert "offer" in b and "service" in b and "about" in b
    assert "testimonial" not in b               # empty proof -> NO fabricated testimonial
    # offer carries the price; two services landed
    assert any("$97" in t for t, _ in b["offer"])
    assert len(b["service"]) == 2
    assert m["banned_words"] == ["crossfit", "bootcamp", "cardio", "hyrox",
                                 "intensity", "compete"]
    assert m["approver"] == "Ryan Parr"
    # the drafted bible carries the never-use words verbatim
    low = m["bible_text"].lower()
    for w in m["banned_words"]:
        assert w in low, f"banned word {w!r} missing from bible"
    # citation convention
    assert all(cite == "client social intake" for _, cite in b["offer"])


# ---- 2. map_answers on Top Fuel: no testimonial, front door No Sweat Intro -------
def test_map_answers_topfuel():
    m = sir.map_answers(_topfuel_answers())
    b = m["bundle"]
    assert "testimonial" not in b
    assert any("No Sweat Intro" in t for t, _ in b["offer"])
    # no exact_price -> offer text has no trailing parenthetical price
    assert not any("(" in t for t, _ in b["offer"])
    assert m["banned_words"] == ["shred", "shame"]   # split on newlines too


# ---- 3. onboard lands APPROVED sources, dedups, writes bible, never clobbers -----
def test_onboard_lands_approved_and_is_idempotent():
    ans = _gritx_answers()
    rep = sir.onboard_from_social("gritx_ig", ans, approve=True)
    assert rep["base"] == "gritx"
    assert rep["approver"] == "Ryan Parr"
    assert rep["sources_created"] > 0
    # sources are APPROVED (readable by the drafting path)
    approved = cs.approved_sources("gritx_ig")
    assert approved and all(s.status == "approved" for s in approved)
    # bible written under brand_voice/gritx/
    bible_path = os.path.join("brand_voice", "gritx", "lasso_voice.md")
    assert os.path.exists(bible_path)
    with open(bible_path, encoding="utf-8") as fh:
        original = fh.read()

    # re-run: dedup adds nothing, bible not clobbered
    rep2 = sir.onboard_from_social("gritx_ig", ans, approve=True)
    assert rep2["sources_created"] == 0
    assert "not overwritten" in rep2["bible"]
    with open(bible_path, encoding="utf-8") as fh:
        assert fh.read() == original


# ---- 4. read_social_intake uses the injected reader (offline) --------------------
def test_read_social_intake_injectable():
    captured = {}

    def _fake_reader(base_key):
        captured["base"] = base_key
        return _gritx_answers()

    out = sir.read_social_intake("gritx", reader=_fake_reader)
    assert captured["base"] == "gritx"
    assert out["approver"] == "Ryan Parr"
    # blank base -> None, no reader call
    assert sir.read_social_intake("", reader=_fake_reader) is None


# ---- 5. FORM V2: every v2 field lands (one parser with intake_web, zero drift) ----
def _v2_answers():
    """A FULL v2 payload, the shape the portal form sends since 2026-08-26."""
    return {
        "base_key": "gritx",
        "gym": {"name": "GritX", "website": "gritx.com", "ig_handle": "@gritx",
                "fb_page": "GritX",
                "about": "Founded in 2019 by two coaches who hated big box gyms.",
                "gym_type": "Boutique group training",
                "google_business": "GritX Fitness Carmel",
                "locations": ["Carmel IN", "Westfield IN"]},
        "voice": {"vibe": "warm, encouraging, real",
                  "words_to_use": "strong, consistent, community",
                  "words_to_never_use": "CrossFit, Bootcamp",
                  "content_goal": "book more intro sessions",
                  "hashtags": ["#gritx", "#carmelfitness"],
                  "sample_post_links": ["https://instagram.com/p/abc123"]},
        "offers": {"services": "Small group training\nPersonal coaching",
                   "front_door_offer": "21 day kickstart",
                   "exact_pricing_wording": "just $97 to start",
                   "upcoming_promos": "Fall 6 week challenge"},
        "audience": {"ideal_member": "busy parents in their 40s",
                     "age_range": "35 to 55",
                     "prior_struggles": "no time, gym intimidation"},
        "proof": {"wins": "Sarah lost 20 lbs in 12 weeks",
                  "verifiable_numbers": "150 five star Google reviews"},
        "media": {"has_media": "yes", "hero_shots": "coach high fives at the door",
                  "off_limits": "no member faces without consent",
                  "notes": "new photos uploaded monthly"},
        "approver": {"name": "Ryan Parr", "role": "Owner", "cell": "555 0100",
                     "email": "ryan@gritx.com", "best_time": "mornings",
                     "upload_contact": "Ryan"},
    }


def test_map_answers_v2_every_field_lands():
    m = sir.map_answers(_v2_answers())
    b = m["bundle"]

    # offer: front door + exact pricing wording + upcoming promos all land
    offer_texts = [t for t, _ in b["offer"]]
    assert any("21 day kickstart" in t for t in offer_texts)
    assert any("just $97 to start" in t for t in offer_texts)
    assert any("Fall 6 week challenge" in t for t in offer_texts)
    # service: both lines
    assert len(b["service"]) == 2
    # about: gym story, gym_type, and who-we-help all land
    about_texts = [t for t, _ in b["about"]]
    assert any("Founded in 2019" in t for t in about_texts)
    assert any("Boutique group training" in t for t in about_texts)
    assert any(t == "Who we help: busy parents in their 40s" for t in about_texts)
    # testimonial: BOTH real proof lines (and only those)
    testi = [t for t, _ in b["testimonial"]]
    assert any("Sarah lost 20 lbs" in t for t in testi)
    assert any("150 five star" in t for t in testi)
    assert len(testi) == 2

    # approver: parsed via the ONE parser -- the dict-repr bug is dead
    assert m["approver"] == "Ryan Parr (Owner)"
    assert "{" not in m["approver"] and "'" not in m["approver"]
    assert "best time: mornings" in m["approver_contact"]
    assert "uploads: Ryan" in m["approver_contact"]

    # banned words parsed and in the bible
    assert m["banned_words"] == ["crossfit", "bootcamp"]
    low = m["bible_text"].lower()
    for w in m["banned_words"]:
        assert w in low

    # every remaining v2 voice/audience/media field lands in the drafted bible
    bible = m["bible_text"]
    for needle in ("book more intro sessions",        # voice.content_goal
                   "#gritx",                          # voice.hashtags
                   "https://instagram.com/p/abc123",  # voice.sample_post_links
                   "35 to 55",                        # audience.age_range
                   "busy parents in their 40s",       # audience.ideal_member
                   "no time, gym intimidation",       # audience.prior_struggles
                   "coach high fives at the door",    # media.hero_shots
                   "no member faces without consent", # media.off_limits
                   "new photos uploaded monthly",     # media.notes
                   "Carmel IN",                       # gym.locations
                   "GritX Fitness Carmel",            # gym.google_business
                   "just $97 to start"):              # offers.exact_pricing_wording
        assert needle in bible, f"v2 field content {needle!r} missing from bible"


# ---- 6. V1 legacy payloads still parse (backward compat through the one parser) ---
def test_map_answers_v1_legacy_backward_compat():
    m = sir.map_answers(_gritx_answers())
    b = m["bundle"]
    # exact_price (the v1 name) still lands as offer material
    assert any("$97" in t for t, _ in b["offer"])
    # the v1 string approver parses to a plain name, never a dict repr
    assert m["approver"] == "Ryan Parr"
    # the v1 media_notes STRING still reaches the bible
    assert "no photos yet, house infographics only" in m["bible_text"]
    # empty v1 proof still fabricates nothing
    assert "testimonial" not in b


# ---- 7. REGRESSION: section 7 (CTA/hashtags) actually reaches the bible ----------
# bible_drafter.draft_bible's CTA-rotation AND hashtag-strategy blocks both read
# _sec(s, 7) (bible_drafter.py:117,120). _build_intake_text used to never emit a
# section 7 at all, so EVERY gym onboarded through this bridge shipped a bible with
# an empty CTA/hashtag section even when the intake's own front-door offer was real.
# Confirmed on toughtemple52040e (front_door_offer = "Free tour and intro session
# for new members" sitting unused in the raw intake) and crossfitreverb30b5b2.
def test_section7_cta_lands_in_bible_from_real_intake_offer():
    m = sir.map_answers(_gritx_answers())  # front_door_offer: "21 day kickstart"
    bible = m["bible_text"]
    assert "### CTA rotation" in bible
    cta_block = bible.split("### CTA rotation", 1)[1].split("### Hashtag strategy", 1)[0]
    assert "21 day kickstart" in cta_block, \
        "real front-door offer from the intake never reached the CTA rotation block"
    assert "TODO" not in cta_block, \
        "a TODO placeholder must not survive when the intake carries a real offer"


def test_section7_ctas_are_actually_machine_parseable_by_voice_load_voice():
    """The functional half of the fix, not just the display half. drafter.py and
    content_planner.py both read voice.ctas (agent/voice.py's _extract_ctas), which
    parses ONLY quoted or bulleted/numbered list items out of '### CTA rotation' --
    a free-text paragraph there renders fine for a human but is INVISIBLE to that
    parser. Section 7 must render as bullets so a real front-door offer actually
    reaches the live CTA pool, not just the bible's display text."""
    from agent import voice as voice_mod
    ans = _gritx_answers()  # front_door_offer: "21 day kickstart"
    m = sir.map_answers(ans)
    doc = voice_mod.VoiceDoc(raw=m["bible_text"], hashtags=[],
                             ctas=voice_mod._extract_ctas(m["bible_text"]))
    assert any("21 day kickstart" in c for c in doc.ctas), \
        f"real CTA text did not survive voice._extract_ctas parsing: {doc.ctas!r}"


def test_section7_hashtags_land_in_bible_from_real_intake_hashtags():
    ans = _v2_answers()  # voice.hashtags: ["#gritx", "#carmelfitness"]
    m = sir.map_answers(ans)
    bible = m["bible_text"]
    hashtag_block = bible.split("### Hashtag strategy", 1)[1]
    assert "#gritx" in hashtag_block, \
        "real hashtags from the intake never reached the hashtag strategy block"


def test_section7_multiline_hashtags_all_land_not_just_the_first():
    """Train716's real intake shape: one hashtag per line, so the flattened
    'voice' field's Hashtags label sits on its own line followed by several
    MORE lines with no label at all. Only the first would survive a naive
    'grab this one line' extraction."""
    ans = {
        "base_key": "train716",
        "gym": {"name": "Train716"},
        "voice": {"hashtags": "#orchardpark\n#groupfitness\n#train716\n#HYROX\n"
                               "#strengthtraining"},
        "offers": {"front_door_offer": "Free Intro Session + InBody Scan"},
    }
    m = sir.map_answers(ans)
    hashtag_block = m["bible_text"].split("### Hashtag strategy", 1)[1]
    for tag in ("#orchardpark", "#groupfitness", "#train716", "#HYROX",
                "#strengthtraining"):
        assert tag in hashtag_block, f"{tag!r} dropped from the hashtag block"


def test_section7_stays_todo_with_no_fabrication_when_intake_carries_neither():
    ans = _gritx_answers()
    ans["offers"] = {"services": "Small group training", "front_door_offer": "",
                      "exact_price": ""}
    m = sir.map_answers(ans)
    bible = m["bible_text"]
    cta_block = bible.split("### CTA rotation", 1)[1].split("### Hashtag strategy", 1)[0]
    # no front-door offer, no upcoming promos, no hashtags in this payload -> the
    # honest TODO placeholder, never an invented CTA
    assert "TODO" in cta_block


# ---- 8. backfill_section7: recover section 7 for an ALREADY-onboarded gym --------
def _write_existing_bible(tmp_path, base, cta_body, hashtag_body):
    voice_dir = tmp_path / "brand_voice" / base
    voice_dir.mkdir(parents=True)
    from agent import bible_drafter as bd
    text = (
        f"# {base} Brand Bible\n\n## 6. Platform rules\n\n"
        f"{bd.CTA_HEADER}\n{cta_body}\n\n"
        f"{bd.HASHTAG_HEADER}\n{hashtag_body}\n"
    )
    (voice_dir / "lasso_voice.md").write_text(text, encoding="utf-8")
    return str(tmp_path / "brand_voice")


def test_backfill_section7_recovers_real_cta_for_an_already_onboarded_gym(tmp_path):
    from agent import bible_drafter as bd
    voice_dir = _write_existing_bible(tmp_path, "toughtemple", bd.TODO, bd.TODO)
    ans = {
        "base_key": "toughtemple",
        "gym": {"name": "Tough Temple"},
        "offers": {"front_door_offer": "Free tour and intro session for new members",
                   "upcoming_promos": "Hyrox PFT September 19th"},
        "voice": {"words_to_never_use": ""},
    }
    result = sir.backfill_section7("toughtemple", reader=lambda b: ans, voice_dir=voice_dir)
    assert result["ok"] is True
    assert result["had_recoverable_data"] is True
    assert result["changed"]["cta"] is True
    on_disk = open(os.path.join(voice_dir, "toughtemple", "lasso_voice.md")).read()
    assert "Free tour and intro session for new members" in on_disk
    assert bd.TODO not in on_disk


def test_backfill_section7_uses_intake_key_for_self_serve_uuid_split(tmp_path):
    """THE REAL PRODUCTION SHAPE: a self-serve gym's echo_social_intake.client_key
    is the portal's raw UUID (captured before any canonical base existed), while
    its bible lands under the RESOLVED base folder. Looking up the intake by the
    bible's own folder name finds nothing for these gyms; intake_key must be used
    for the lookup while base_key still names the file."""
    from agent import bible_drafter as bd
    voice_dir = _write_existing_bible(tmp_path, "toughtemple52040e", bd.TODO, bd.TODO)
    ans = {
        "base_key": "toughtemple52040e",
        "gym": {"name": "Tough Temple"},
        "offers": {"front_door_offer": "Free tour and intro session for new members"},
    }
    seen_keys = []

    def _reader(key):
        seen_keys.append(key)
        return ans if key == "52040e09-986f-43d6-a60d-306fa8e234fe" else None

    result = sir.backfill_section7(
        "toughtemple52040e", reader=_reader, voice_dir=voice_dir,
        intake_key="52040e09-986f-43d6-a60d-306fa8e234fe")
    assert seen_keys == ["52040e09-986f-43d6-a60d-306fa8e234fe"]
    assert result["ok"] is True and result["had_recoverable_data"] is True
    on_disk = open(os.path.join(voice_dir, "toughtemple52040e",
                                 "lasso_voice.md")).read()
    assert "Free tour and intro session for new members" in on_disk


def test_backfill_section7_many_parses_base_equals_intake_key():
    calls = []

    def _fake_backfill(base, *, reader=None, voice_dir=None, intake_key=None):
        calls.append((base, intake_key))
        return {"base": base, "ok": True, "had_recoverable_data": False}

    import agent.social_intake_reader as sir_mod
    orig = sir_mod.backfill_section7
    sir_mod.backfill_section7 = _fake_backfill
    try:
        sir.backfill_section7_many(["gritx", "toughtemple52040e=52040e09-uuid"])
    finally:
        sir_mod.backfill_section7 = orig
    assert calls == [("gritx", None), ("toughtemple52040e", "52040e09-uuid")]


def test_backfill_section7_never_fabricates_when_intake_genuinely_has_nothing(tmp_path):
    """Dean's gym case: real intake carries no front-door offer, no promos, no
    hashtags. The bible must stay exactly as it was -- no invented CTA."""
    from agent import bible_drafter as bd
    voice_dir = _write_existing_bible(tmp_path, "crossfitreverb", bd.TODO, bd.TODO)
    ans = {
        "base_key": "crossfitreverb",
        "gym": {"name": "CrossFit Reverb"},
        "offers": {"front_door_offer": "", "upcoming_promos": ""},
        "voice": {"words_to_never_use": ""},
    }
    before = open(os.path.join(voice_dir, "crossfitreverb", "lasso_voice.md")).read()
    result = sir.backfill_section7("crossfitreverb", reader=lambda b: ans, voice_dir=voice_dir)
    assert result["ok"] is True
    assert result["had_recoverable_data"] is False
    after = open(os.path.join(voice_dir, "crossfitreverb", "lasso_voice.md")).read()
    assert before == after                       # byte-for-byte untouched


def test_backfill_section7_never_overwrites_an_already_filled_block(tmp_path):
    """A bible whose CTA block was already filled (human edit, or a generic
    fallback from a different mechanism) must be left untouched even though the
    real intake DOES carry recoverable data -- the file no longer carries the
    literal TODO, which is the signal this was already handled."""
    from agent import bible_drafter as bd
    voice_dir = _write_existing_bible(
        tmp_path, "hillcountry", "- Learn more at hillcountrymvmt.com", bd.TODO)
    ans = {
        "base_key": "hillcountry",
        "gym": {"name": "Hill Country MVMT"},
        "offers": {"front_door_offer": "Free No Sweat Intro"},
        "voice": {"words_to_never_use": ""},
    }
    result = sir.backfill_section7("hillcountry", reader=lambda b: ans, voice_dir=voice_dir)
    assert result["ok"] is True
    assert result["changed"]["cta"] is False     # left alone
    on_disk = open(os.path.join(voice_dir, "hillcountry", "lasso_voice.md")).read()
    assert "Learn more at hillcountrymvmt.com" in on_disk
    assert "Free No Sweat Intro" not in on_disk.split(bd.HASHTAG_HEADER)[0]


def test_backfill_section7_reports_missing_intake_honestly():
    result = sir.backfill_section7("nosuchgym", reader=lambda b: None)
    assert result["ok"] is False
    assert "no intake" in result["reason"]


def test_backfill_section7_reports_missing_bible_honestly(tmp_path):
    ans = {"base_key": "ghost", "gym": {"name": "Ghost Gym"},
           "offers": {"front_door_offer": "Free trial"}, "voice": {}}
    result = sir.backfill_section7("ghost", reader=lambda b: ans,
                                    voice_dir=str(tmp_path / "brand_voice"))
    assert result["ok"] is False
    assert "no existing bible" in result["reason"]


# ---- 7. onboard on a v2 payload: sources land, no repr garbage anywhere ----------
def test_onboard_v2_payload_lands_clean_sources():
    rep = sir.onboard_from_social("gritx_ig", _v2_answers(), approve=True)
    assert rep["base"] == "gritx"
    assert rep["approver"] == "Ryan Parr (Owner)"
    assert rep["sources_created"] > 0
    for s in cs.approved_sources("gritx_ig"):
        assert not s.text.startswith("{"), f"dict repr leaked into source: {s.text!r}"
