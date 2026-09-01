"""
portal_domains.py — curated gym name -> real domain (and handles) registry.

The portal `gyms` table has NO domain / website column (see portal_gyms), so the welcome
pipeline cannot scrape a new client's logo and would hold every portal-added gym on
needs_logo. Rather than ask a human to send the site each time, the AGENT looks a new gym
up (web search) and records its REAL, verified domain here; welcome_queue consults this
when the portal row carries no domain, so a recorded gym auto-resolves its logo going
forward.

No fabrication: only real domains that have been looked up and confirmed go in here. An
unknown gym returns "" and stays needs_logo (never a guessed or wrong logo on a card).
Keyed by the gym's NAME, normalized (lowercased, non-alphanumerics collapsed to single
spaces) so portal capitalization / punctuation differences still match.
"""

import re


def _norm(name):
    """Normalize a gym name for matching: lowercase, non-alphanumerics -> single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


# Normalized gym name -> {"domain", optional "instagram", optional "facebook"}.
# Add a gym here after looking it up (do NOT ask a human for it). Real domains only.
_REGISTRY = {
    "westwood athletics": {
        "domain": "westwoodathletics.com",
        "instagram": "westwood.athletics",
        "facebook": "Westwood Athletics",
    },
    "project evolve personal training": {
        "domain": "projectevolvenaples.com",
        "instagram": "projectevolvepersonaltraining",
        "facebook": "Project Evolve Personal Training For Adults 40+",
    },
    # Looked up + verified 2026-08-19 (portal-added clients, no domain column).
    "the bolton club": {
        "domain": "theboltonclub.com",           # Bolton, MA — small-group personal training
    },
    "hoosier athletic club": {
        "domain": "hoosierathleticclub.com",      # Bloomington, IN (Hoosier CrossFit)
        "facebook": "Hoosier Athletic Club",
    },
    "crossfit old glory": {
        "domain": "oldglorygym.com",              # Ashburn, VA (Old Glory Gym)
        "facebook": "Old Glory Gym",
    },
    "silk city crossfit": {
        "domain": "silkcityfit.com",              # Manchester, CT (Silk City Fit)
        "instagram": "silkcityfit",
    },
    "stillpoint fitness": {
        "domain": "stillpointfitness.com",        # Monmouth Junction, NJ (Princeton area)
        "instagram": "stillpoint_fitness",
    },
    "tricore": {
        "domain": "tricorefit.com",               # Phoenix, AZ (TriCore Fitness)
        "instagram": "tricore.fitness",
    },
    "x4 hoover": {
        "domain": "x4fit.com",                    # Hoover, AL location of the X4 FIT brand
    },
    # Looked up + verified 2026-08-31 (website auto-intake rollout; every domain
    # confirmed against the gym's own site + its known GBP street address). Each gym
    # is keyed by BOTH its human name and its exact account base key, because the
    # intake resolver tries the display name first and the base key second.
    "hill country mvmt": {
        "domain": "hillcountrymvmt.com",          # Dripping Springs, TX (150 Russell Ln)
    },
    "hillcountry": {"domain": "hillcountrymvmt.com"},
    "zanshin fitness": {
        "domain": "zanshin.fit",                  # Peachtree Corners, GA (4015 Holcomb Bridge Rd)
    },
    "zanshinfitness630e22": {"domain": "zanshin.fit"},
    "crossfit reverb": {
        "domain": "crossfitreverb.net",           # Upland, CA (1120 Dewey Way)
    },
    "crossfitreverb30b5b2": {"domain": "crossfitreverb.net"},
    "crossfit local": {
        "domain": "crossfitlocal.com",            # Chapel Hill, NC (7401 Rex Rd)
    },
    "crossfitlocal": {"domain": "crossfitlocal.com"},
    "crossfit newtown": {
        "domain": "crossfitnewtown.com",          # Newtown, PA (121 Friends Lane; NOT the CT box)
    },
    "crossfitnewtown": {"domain": "crossfitnewtown.com"},
    "train716": {
        "domain": "train716buffalo.com",          # Orchard Park, NY (Buffalo; 3356 Southwestern Blvd)
    },
    "train7164ae502": {"domain": "train716buffalo.com"},
    "district h strength fitness": {
        "domain": "districthsf.com",              # Houston, TX (two locations; verified 2026-08-25)
    },
    "district h": {"domain": "districthsf.com"},
    "district h strength and fitness": {"domain": "districthsf.com"},
}


def _compact(name):
    """The space-free form of a normalized name: 'the bolton club' -> 'theboltonclub'.
    Echo's BASE KEYS are exactly this shape, so a record filed under a human name is
    still reachable when a caller passes the gym's base key (the base-key-vs-human-name
    miss that left The Bolton Club without a voice bible on 2026-08-31)."""
    return _norm(name).replace(" ", "")


# Base-key index, built once: compact name -> record. An entry whose compact form
# collides with another is dropped from this index so an ambiguous base key resolves to
# nothing rather than to the wrong gym; the exact-name lookup still finds both.
_COMPACT_INDEX = {}
for _k, _v in _REGISTRY.items():
    _ck = _compact(_k)
    _COMPACT_INDEX[_ck] = None if _ck in _COMPACT_INDEX else _v
_COMPACT_INDEX = {k: v for k, v in _COMPACT_INDEX.items() if v is not None}


def record_for(name):
    """The full registry record for a gym name OR base key, or None when unrecorded."""
    return _REGISTRY.get(_norm(name)) or _COMPACT_INDEX.get(_compact(name))


def domain_for(name, gym_id=None):
    """The recorded, real domain for a gym name or base key, or "" when it is not
    recorded. `gym_id` is accepted for a future id-keyed override but name is the current
    key. An empty return keeps the caller on its needs_logo path (never a fabricated
    domain)."""
    rec = record_for(name)
    return (rec.get("domain") or "") if rec else ""
