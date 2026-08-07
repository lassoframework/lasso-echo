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
}


def record_for(name):
    """The full registry record for a gym name, or None when it is not recorded."""
    return _REGISTRY.get(_norm(name))


def domain_for(name, gym_id=None):
    """The recorded, real domain for a gym name, or "" when it is not recorded. `gym_id`
    is accepted for a future id-keyed override but name is the current key. An empty return
    keeps the caller on its needs_logo path (never a fabricated domain)."""
    rec = _REGISTRY.get(_norm(name))
    return (rec.get("domain") or "") if rec else ""
