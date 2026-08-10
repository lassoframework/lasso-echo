"""
social_intake_reader.py: turn a gym's SUBMITTED social intake (the /socialmedia
capture stored in Supabase echo_social_intake) into an onboarded client: a drafted
brand bible + APPROVED, CITED client_sources, ready for the client month builder.

These gyms have NO photo library, so the intake is the only well of fact. The three
laws that govern every client path apply here without exception:

  1. NO FABRICATION. A category is landed ONLY when the intake actually carries text
     for it. Empty proof (no wins, no verifiable numbers) means NO testimonial source
     is written; the gym simply has no testimonial content, never a made-up one.
  2. The words_to_never_use list rides into the drafted bible verbatim, so the banned
     words are visible to a human reviewer AND available to the month builder's
     banned-word guard (build_client_month drops any draft that emits one).
  3. Nothing here publishes and no gate is weakened. onboard_from_social writes the
     brand_voice docs and lands client_sources; a human still approves every draft.

KEY MAPPING (do not conflate: three distinct keys):
  * base_key   = "gritx" / "topfuel": the tenant slug. This is BOTH
    echo_social_intake.client_key AND content_calendar.gym_id, and it names the
    brand_voice/<base>/ folder the bible lands in.
  * account_key = "gritx_ig" / "topfuel_ig": the generation Account key. client_sources
    and build_client_draft are keyed by THIS.
  * fb Account  = "gritx_fb" / "topfuel_fb": the Facebook mirror account.
So: read the intake by BASE, land sources under ACCOUNT, and (in client_month_run)
write content_calendar rows with gym_id = BASE.

This module reads (live) and writes local docs only. The live reader is injectable so
every test runs fully offline (no Gemini / Supabase call in the build or test path).
"""

import os

from . import bible_drafter, client_sources, config


def _clean(value):
    """A trimmed string for any answer value (None-safe)."""
    return (str(value) if value is not None else "").strip()


def _nonempty_lines(value):
    """The non-empty lines of a multi-line answer, split on newlines. A single-line
    answer yields one element; a blank answer yields []."""
    return [ln.strip() for ln in _clean(value).splitlines() if ln.strip()]


def _parse_banned(words_to_never_use):
    """The words_to_never_use answer parsed to a lowercased de-duplicated list,
    split on commas AND newlines. Blank -> []. Never invents a word."""
    raw = _clean(words_to_never_use)
    if not raw:
        return []
    parts = []
    for chunk in raw.replace("\n", ",").split(","):
        w = chunk.strip().lower()
        if w and w not in parts:
            parts.append(w)
    return parts


def map_answers(answers):
    """PURE. Map a submitted social-intake `answers` dict to the pieces onboarding
    needs. Returns {bundle, bible_text, proof_text, approver, gym, banned_words}.

    answers schema (nested dicts, every field optional):
      gym{name, website, ig_handle, fb_page}
      proof{wins, verifiable_numbers}
      voice{vibe, words_to_use, words_to_never_use}
      offers{services, front_door_offer, exact_price}
      audience{ideal_member}
      media_notes (str)
      approver (str)

    bundle (client_sources.CLIENT_CATEGORIES): only NON-EMPTY categories are emitted,
    each item a (text, citation) pair with citation "client social intake":
      offer        <- offers.front_door_offer (with exact_price appended if present)
      service      <- each non-empty line of offers.services
      about        <- "Who we help: " + audience.ideal_member
      testimonial  <- proof.wins and/or proof.verifiable_numbers, ONLY if non-empty
                      (empty proof is SKIPPED: no fabrication)
      faq / promo  <- only if the intake carries them (answers['faq'] / answers['promo'])

    bible_text/proof_text come from bible_drafter.draft_bible fed an intake text
    assembled from the answers; the bible ALWAYS contains the words_to_never_use list.
    """
    answers = answers or {}
    gym = dict(answers.get("gym") or {})
    proof = dict(answers.get("proof") or {})
    voice = dict(answers.get("voice") or {})
    offers = dict(answers.get("offers") or {})
    audience = dict(answers.get("audience") or {})

    cite = "client social intake"
    bundle = {}

    def _add(category, text):
        text = _clean(text)
        if not text:
            return
        bundle.setdefault(category, []).append((text, cite))

    # offer: the front-door offer, with the exact price folded in when it is given.
    front_door = _clean(offers.get("front_door_offer"))
    exact_price = _clean(offers.get("exact_price"))
    if front_door:
        offer_text = f"{front_door} ({exact_price})" if exact_price else front_door
        _add("offer", offer_text)

    # service: one source per non-empty services line.
    for line in _nonempty_lines(offers.get("services")):
        _add("service", line)

    # about: who the gym helps.
    ideal = _clean(audience.get("ideal_member"))
    if ideal:
        _add("about", f"Who we help: {ideal}")

    # testimonial: ONLY from real proof. Empty proof -> no testimonial (no fabrication).
    for line in _nonempty_lines(proof.get("wins")):
        _add("testimonial", line)
    for line in _nonempty_lines(proof.get("verifiable_numbers")):
        _add("testimonial", line)

    # faq / promo: only when the intake actually carries them.
    for line in _nonempty_lines(answers.get("faq")):
        _add("faq", line)
    for line in _nonempty_lines(answers.get("promo")):
        _add("promo", line)

    banned_words = _parse_banned(voice.get("words_to_never_use"))

    intake_text = _build_intake_text(gym, voice, audience, answers, banned_words)
    base_key = _clean(answers.get("base_key")) or _slug(gym.get("name")) or "client"
    bible_text, proof_text = bible_drafter.draft_bible(base_key, intake_text)

    return {
        "bundle": bundle,
        "bible_text": bible_text,
        "proof_text": proof_text,
        "approver": _clean(answers.get("approver")),
        "gym": gym,
        "banned_words": banned_words,
    }


def _slug(name):
    """A conservative tenant slug from a gym name (letters/digits/underscore)."""
    s = "".join(c if c.isalnum() else "_" for c in _clean(name).lower())
    return "_".join(p for p in s.split("_") if p)


def _build_intake_text(gym, voice, audience, answers, banned_words):
    """Assemble the numbered `## N.` intake bible_drafter.parse_intake expects, from the
    answers. Section 3 (voice + tone) ALWAYS lists the words_to_never_use so the drafted
    bible carries the banned list verbatim. Only intake facts are used: nothing invented.
    A missing section is left blank (bible_drafter renders its own TODO)."""
    name = _clean(gym.get("name")) or "the gym"
    website = _clean(gym.get("website"))
    ig = _clean(gym.get("ig_handle"))
    fb = _clean(gym.get("fb_page"))
    vibe = _clean(voice.get("vibe"))
    words_use = _clean(voice.get("words_to_use"))
    ideal = _clean(audience.get("ideal_member"))
    media_notes = _clean(answers.get("media_notes"))

    socials = []
    if website:
        socials.append(f"Website: {website}")
    if ig:
        socials.append(f"Instagram: {ig}")
    if fb:
        socials.append(f"Facebook: {fb}")

    never_line = ", ".join(banned_words) if banned_words \
        else "(none provided in the intake)"

    lines = []
    lines.append("## 1. Who this gym is")
    who = [name]
    who.extend(socials)
    lines.append("\n".join(who))
    lines.append("")
    lines.append("## 2. Who we talk TO (the avatar)")
    lines.append(ideal)
    lines.append("")
    lines.append("## 3. Voice and tone")
    lines.append(f"Vibe: {vibe}" if vibe else "")
    if words_use:
        lines.append(f"Words to use: {words_use}")
    lines.append(f"Words to NEVER use: {never_line}")
    lines.append("")
    lines.append("## 4. Hard guardrails (never violate)")
    lines.append(f"Never use these words: {never_line}")
    if media_notes:
        lines.append(f"Media notes: {media_notes}")
    return "\n".join(lines)


# ---- live read (injectable; default Supabase REST) --------------------------------

def _default_reader(base_key):
    """Live read of echo_social_intake via Supabase REST (same style as
    portal_calendar_store): the newest submission for client_key == base_key. Returns
    the row's `answers` dict, or None. Reads creds lazily from config; NEVER logs the
    key. No creds -> None (the caller reports missing data, never guesses)."""
    from . import config
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return None
    import requests  # lazy, matches the repo pattern
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Accept": "application/json"}
    params = {
        "client_key": f"eq.{base_key}",
        "order": "created_at.desc",
        "limit": "1",
    }
    r = requests.get(f"{url}/rest/v1/echo_social_intake", params=params,
                     headers=headers, timeout=30)
    if r.status_code >= 400:
        return None
    rows = r.json() or []
    if not rows:
        return None
    row = rows[0]
    answers = row.get("answers")
    return answers if isinstance(answers, dict) else None


def read_social_intake(base_key, *, reader=None):
    """The newest submitted social intake answers for the tenant base_key, or None.
    `reader(base_key) -> answers|None` is injectable so tests run offline; the default
    is the live Supabase REST read. base_key is the TENANT slug (echo_social_intake
    .client_key), e.g. "gritx"."""
    base_key = _clean(base_key)
    if not base_key:
        return None
    reader = reader or _default_reader
    return reader(base_key)


# ---- onboard: write the docs + land approved sources -------------------------------

def _base_from_account(account_key):
    """The tenant base for an account key: strip a trailing _ig / _fb so voice docs
    land under brand_voice/<base>/. 'gritx_ig' -> 'gritx'."""
    account_key = _clean(account_key)
    for suffix in ("_ig", "_fb"):
        if account_key.endswith(suffix):
            return account_key[: -len(suffix)]
    return account_key


def _write_doc(path, text):
    """Write `text` to `path` only when the file does NOT already exist (idempotent,
    never clobbers a reviewed doc). Returns True when it wrote, False when it left an
    existing file untouched."""
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return True


def onboard_from_social(account_key, answers, *, approve=True):
    """Onboard a client from its submitted social intake.

    * Writes the DURABLE client bible + social_proof (under config.client_voice_dir(),
      i.e. <DATA_DIR>/brand_voice/<base>/) from the drafted bible/proof, but NEVER
      clobbers an existing file (idempotent; a reviewed doc is safe across re-runs).
      The durable location is the persistent data volume, so the generated bible is
      NOT wiped when the /app container image is replaced on every deploy/restart
      (BUG 1: a repo-relative brand_voice/<base>/ vanished on restart, starving the
      client month builder of the voice doc it needs).
    * Lands the mapped bundle as client_sources for account_key, deduped against
      everything already stored (mirrors intake_onboard._step_sources), status
      "approved" when approve is True else "pending".

    account_key is the GENERATION account key (e.g. "gritx_ig"); the voice-doc folder
    is keyed off the tenant BASE ("gritx"). Returns
    {bible, sources_created, banned_words, approver, base}. No publish, no gate weakened.
    """
    mapped = map_answers(answers)
    base = _base_from_account(account_key)

    voice_dir = os.path.join(config.client_voice_dir(), base)
    bible_path = os.path.join(voice_dir, "lasso_voice.md")
    proof_path = os.path.join(voice_dir, "social_proof.md")
    wrote_bible = _write_doc(bible_path, mapped["bible_text"])
    _write_doc(proof_path, mapped["proof_text"])
    bible_note = (f"drafted, held for approval ({bible_path})" if wrote_bible
                  else f"exists, not overwritten ({bible_path})")

    # Dedup vs everything already stored for the account (any status), exactly like
    # intake_onboard._step_sources, so a re-run adds nothing twice.
    existing = {(s.category, s.text)
                for s in client_sources.all_sources(account_key)}
    fresh = {}
    for cat, items in mapped["bundle"].items():
        for text_item, citation in items:
            if (cat, text_item) in existing:
                continue
            fresh.setdefault(cat, []).append((text_item, citation))

    created = []
    if fresh:
        status = "approved" if approve else "pending"
        created = client_sources.submit_intake(account_key, fresh, status=status)

    return {
        "bible": bible_note,
        "sources_created": len(created),
        "banned_words": mapped["banned_words"],
        "approver": mapped["approver"],
        "base": base,
    }
