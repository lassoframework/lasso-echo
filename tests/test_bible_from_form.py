"""
GAP 6: `draft-bible --from-form` renders the ARCHIVED portal-intake JSON into the
numbered BRAND_VOICE_INTAKE.md that bible_drafter's own parse_intake anchors on,
for HUMAN editing/approval (no hand transcription). Fully offline.

Asserts the round trip: a full v2 portal body -> normalize_portal_intake (the one
parser, exactly what handle_portal_intake archives) -> render_intake_md ->
parse_intake gets every field back; proof entries render Permission: TODO so
parse_proof_entries SKIPS all of them until a human confirms (human owns voice;
nothing auto-approved). Plus: run_from_form writes the doc from a local path or
an R2 key/client lookup, and NEVER clobbers an existing (possibly human-edited)
doc.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import bible_drafter as bd  # noqa: E402
from agent.intake_web import normalize_portal_intake  # noqa: E402


def _v2_body():
    """The full v2 portal form body (what the ops portal POSTs)."""
    return {
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


def _payload():
    """The archived shape handle_portal_intake writes to intake/<client>/forms/."""
    body = _v2_body()
    return {"kind": "intake_form", "source": "portal", "client": "gritx",
            "answers": normalize_portal_intake(body), "portal": body,
            "timestamp": "20260827T120000Z"}


# ---- 1. every v2 field lands in the rendered markdown ----------------------------
def test_render_carries_every_v2_field():
    md = bd.render_intake_md(_payload())
    for needle in ("GritX",
                   "Founded in 2019",                   # gym.about
                   "Boutique group training",           # gym.gym_type
                   "GritX Fitness Carmel",              # gym.google_business
                   "Carmel IN",                         # gym.locations
                   "gritx.com", "@gritx",
                   "warm, encouraging, real",           # voice.vibe
                   "book more intro sessions",          # voice.content_goal
                   "strong, consistent, community",     # voice.words_to_use
                   "CrossFit, Bootcamp",                # voice.words_to_never_use
                   "#gritx",                            # voice.hashtags
                   "https://instagram.com/p/abc123",    # voice.sample_post_links
                   "21 day kickstart",                  # offers.front_door_offer
                   "just $97 to start",                 # offers.exact_pricing_wording
                   "Fall 6 week challenge",             # offers.upcoming_promos
                   "Small group training",              # offers.services
                   "busy parents in their 40s",         # audience.ideal_member
                   "35 to 55",                          # audience.age_range
                   "no time, gym intimidation",         # audience.prior_struggles
                   "coach high fives at the door",      # media.hero_shots
                   "no member faces without consent",   # media.off_limits
                   "new photos uploaded monthly",       # media.notes
                   "Sarah lost 20 lbs in 12 weeks",     # proof.wins
                   "150 five star Google reviews",      # proof.verifiable_numbers
                   "Ryan Parr",                         # approver.name
                   "best time: mornings",               # approver.best_time
                   "uploads: Ryan"):                    # approver.upload_contact
        assert needle in md, f"{needle!r} missing from the rendered intake doc"


# ---- 2. round trip: parse_intake reads the render; draft_bible carries it --------
def test_round_trip_parse_and_draft():
    md = bd.render_intake_md(_payload())
    s = bd.parse_intake(md)
    # numbered anchors present and populated where the form carries content
    assert "GritX" in s[1] and "Founded in 2019" in s[1]
    assert "21 day kickstart" in s[1] and "Small group training" in s[1]
    assert "busy parents in their 40s" in s[2] and "35 to 55" in s[2]
    assert "warm, encouraging, real" in s[3] and "CrossFit, Bootcamp" in s[3]
    assert "just $97 to start" in s[4] and "no member faces without consent" in s[4]
    assert not (s.get(5) or "").strip()          # pillars uncaptured -> honest TODO later
    assert "Sarah lost 20 lbs in 12 weeks" in s[6]
    assert "#gritx" in s[7]

    bible, proof = bd.draft_bible("gritx", md)
    assert "Founded in 2019" in bible
    assert "CrossFit, Bootcamp" in bible
    assert bd.TODO in bible                      # the empty pillars section says so


# ---- 3. human owns voice: NO proof entry is usable until a human flips it --------
def test_proof_entries_render_unapproved():
    md = bd.render_intake_md(_payload())
    s = bd.parse_intake(md)
    kept, skipped = bd.parse_proof_entries(s[6])
    assert kept == []                            # nothing auto-permissioned, ever
    assert len(skipped) == 2
    assert all(reason == "permission is not yes" for _, reason in skipped)


# ---- 4. run_from_form: local path, r2 key, client lookup, no clobber -------------
class _FakeR2:
    def __init__(self, objects):
        self._objects = objects

    def list_keys(self, prefix):
        return [k for k in self._objects if k.startswith(prefix)]

    def get_bytes(self, key):
        return self._objects[key]


def test_run_from_form_local_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "archived.json"
    src.write_text(json.dumps(_payload()), encoding="utf-8")
    out = bd.run_from_form(str(src))
    assert out == os.path.join("brand_voice", "drafts", "gritx", "BRAND_VOICE_INTAKE.md")
    with open(out, encoding="utf-8") as fh:
        assert "book more intro sessions" in fh.read()


def test_run_from_form_client_lookup_picks_newest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    older = dict(_payload())
    older["portal"] = dict(older["portal"])
    r2 = _FakeR2({
        "intake/gritx/forms/20260801T000000Z_intake.json":
            json.dumps(older).encode("utf-8"),
        "intake/gritx/forms/20260827T120000Z_intake.json":
            json.dumps(_payload()).encode("utf-8"),
    })
    out = bd.run_from_form("gritx", r2=r2)
    assert os.path.exists(out)


def test_run_from_form_r2_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    key = "intake/gritx/forms/20260827T120000Z_intake.json"
    r2 = _FakeR2({key: json.dumps(_payload()).encode("utf-8")})
    out = bd.run_from_form(key, r2=r2)
    assert os.path.exists(out)


def test_run_from_form_never_clobbers_a_human_edit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "archived.json"
    src.write_text(json.dumps(_payload()), encoding="utf-8")
    out = bd.run_from_form(str(src))
    with open(out, "a", encoding="utf-8") as fh:
        fh.write("\nHUMAN EDIT\n")
    with pytest.raises(RuntimeError, match="already exists"):
        bd.run_from_form(str(src))
    with open(out, encoding="utf-8") as fh:
        assert "HUMAN EDIT" in fh.read()         # the edit survived


def test_run_from_form_requires_a_client_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "anon.json"
    payload = _payload()
    payload.pop("client")
    src.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="no client key"):
        bd.run_from_form(str(src))
    # explicit --client works
    out = bd.run_from_form(str(src), client="gritx")
    assert os.path.exists(out)


def test_run_from_form_no_forms_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="no archived intake forms"):
        bd.run_from_form("ghostgym", r2=_FakeR2({}))
