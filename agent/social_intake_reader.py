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
import re

from . import bible_drafter, client_sources, config, ops_alerts


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
    needs. Returns {bundle, bible_text, proof_text, approver, approver_contact,
    gym, banned_words}.

    ONE PARSER, ZERO DRIFT: all field parsing is delegated to
    intake_web.normalize_portal_intake, the same parser the portal's JSON intake
    endpoint uses. It accepts BOTH shapes this bridge sees:
      v1 legacy: gym{name,website,ig_handle,fb_page}, voice{vibe,words_to_use,
        words_to_never_use}, offers{services,front_door_offer,exact_price},
        audience{ideal_member}, media_notes (str), approver (str)
      v2 (form 2026-08-26): adds gym.about/gym_type/google_business/locations,
        voice.content_goal/hashtags/sample_post_links, offers.upcoming_promos +
        exact_pricing_wording, audience.age_range/prior_struggles, structured
        media{has_media,hero_shots,off_limits,notes}, approver{name,contact,
        best_time,upload_contact} (dict).

    bundle (client_sources.CLIENT_CATEGORIES): only NON-EMPTY categories are
    emitted, each item a (text, citation) pair with citation "client social
    intake". Fact fields map to categories via intake_ingest._FORM_SOURCE_SECTIONS
    (the portal ingest's own field->category table), plus:
      about        <- also "Who we help: " + audience.ideal_member (v1 behavior kept)
      testimonial  <- ONLY real proof lines (empty proof is SKIPPED: no fabrication)
      faq / promo  <- only if the intake carries them (answers['faq'] / answers['promo'])

    bible_text/proof_text come from bible_drafter.draft_bible fed an intake text
    assembled from the normalized answers; the bible ALWAYS contains the
    words_to_never_use list.
    """
    answers = answers or {}
    from . import intake_web  # lazy: avoids any import cycle with the web module
    from .intake_ingest import _FORM_SOURCE_SECTIONS
    # SIZE CAP, matching intake_web's own two call sites (handle_intake_form and the
    # portal status endpoint both truncate at _FIELD_MAX). This third path had none, so
    # an unbounded answer flowed uncapped into the drafted bible and into client_sources
    # text, and from there into a caption. Truncate here so every intake door agrees.
    flat = {k: (v[:intake_web._FIELD_MAX] if isinstance(v, str) else v)  # noqa: SLF001
            for k, v in intake_web.normalize_portal_intake(answers).items()}
    def _cap(d):
        """Same cap on the raw sub-dicts. They are read straight off `answers` rather
        than through `flat`, so capping only the flat path would leave the exact fields
        the bible is drafted from (gym.about, voice.vibe) uncapped."""
        return {k: (v[:intake_web._FIELD_MAX] if isinstance(v, str) else v)  # noqa: SLF001
                for k, v in dict(d or {}).items()}

    gym = _cap(answers.get("gym"))
    voice = _cap(answers.get("voice"))
    audience = _cap(answers.get("audience"))

    cite = "client social intake"
    bundle = {}

    def _add(category, text):
        text = _clean(text)
        if not text:
            return
        bundle.setdefault(category, []).append((text, cite))

    # Fact fields -> categories, exactly the portal ingest's own table, so a fact
    # lands in the same category no matter which door the intake came through:
    #   offers -> offer, pricing_rule -> offer, services -> service,
    #   proof -> testimonial (only if non-empty: no fabrication), about -> about.
    for field, category, _citation in _FORM_SOURCE_SECTIONS:
        for line in _nonempty_lines(flat.get(field)):
            _add(category, line)

    # about: who the gym helps (kept from v1; v2's gym.about landed above too).
    ideal = _clean(audience.get("ideal_member"))
    if ideal:
        _add("about", f"Who we help: {ideal}")

    # faq / promo: only when the intake actually carries them.
    for line in _nonempty_lines(answers.get("faq")):
        _add("faq", line)
    for line in _nonempty_lines(answers.get("promo")):
        _add("promo", line)

    banned_words = _parse_banned(voice.get("words_to_never_use"))

    intake_text = _build_intake_text(flat, banned_words)
    base_key = _clean(answers.get("base_key")) or _slug(gym.get("name")) or "client"
    bible_text, proof_text = bible_drafter.draft_bible(base_key, intake_text)

    return {
        "bundle": bundle,
        "bible_text": bible_text,
        "proof_text": proof_text,
        "approver": _clean(flat.get("approver_name")),
        "approver_contact": _clean(flat.get("approver_contact")),
        "gym": gym,
        "banned_words": banned_words,
    }


def _slug(name):
    """A conservative tenant slug from a gym name (letters/digits/underscore)."""
    s = "".join(c if c.isalnum() else "_" for c in _clean(name).lower())
    return "_".join(p for p in s.split("_") if p)


def _build_intake_text(flat, banned_words):
    """Assemble the numbered `## N.` intake bible_drafter.parse_intake expects, from
    the ONE-parser flat dict (intake_web.normalize_portal_intake output). Section 3
    (voice + tone) ALWAYS lists the words_to_never_use so the drafted bible carries
    the banned list verbatim. Only intake facts are used: nothing invented. A
    missing section is left blank (bible_drafter renders its own TODO)."""
    def _f(key):
        return _clean(flat.get(key))

    name = _f("gym_name") or "the gym"
    never_line = ", ".join(banned_words) if banned_words \
        else "(none provided in the intake)"

    who = [name]
    if _f("about"):
        who.append(_f("about"))                    # v2: gym_type + story
    if _f("city"):
        who.append(f"Locations: {_f('city')}")     # v2: gym.locations
    if _f("website"):
        who.append(f"Website: {_f('website')}")
    if _f("ig_handle"):
        who.append(f"Instagram: @{_f('ig_handle')}")
    if _f("fb_page"):
        who.append(f"Facebook: {_f('fb_page')}")
    if _f("google_business"):
        who.append(f"Google Business: {_f('google_business')}")  # v2

    lines = []
    lines.append("## 1. Who this gym is")
    lines.append("\n".join(who))
    lines.append("")
    lines.append("## 2. Who we talk TO (the avatar)")
    # v2 carries age_range + ideal_member + prior_struggles; v1 just ideal_member.
    lines.append(_f("audience"))
    lines.append("")
    lines.append("## 3. Voice and tone")
    # The flat voice block already carries Vibe / Content goal / Words to use /
    # Words to never use / Hashtags / Sample posts (whichever the intake gave).
    if _f("voice"):
        lines.append(_f("voice"))
    lines.append(f"Words to NEVER use: {never_line}")
    lines.append("")
    lines.append("## 4. Hard guardrails (never violate)")
    lines.append(f"Never use these words: {never_line}")
    if _f("pricing_rule"):
        lines.append(f"Exact pricing wording (use verbatim): {_f('pricing_rule')}")
    if _f("media_notes"):
        lines.append(f"Media notes: {_f('media_notes')}")
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


def write_brand_docs(account_key, answers, *, mapped=None):
    """Write THIS gym's durable brand bible + social_proof from its intake answers.

    Split out of onboard_from_social so BOTH ingest lanes can produce a bible from the
    same code. Only one lane used to: a successful portal forward stamps
    echo_forwarded=true, and the sweeper that called onboard_from_social lists only
    echo_forwarded=false rows, so a gym got a brand bible exactly when its forward
    FAILED. Every gym whose intake arrived cleanly had no voice doc at all, and the
    drafter then wrote captions with no avatar, no pillars and no CTAs.

    Idempotent and non-destructive: _write_doc never clobbers a file that already
    exists, so a reviewed bible survives every re-run. Returns
    {"note", "base", "bible_path", "wrote"}.
    """
    mapped = mapped if mapped is not None else map_answers(answers)
    base = _base_from_account(account_key)
    voice_dir = os.path.join(config.client_voice_dir(), base)
    bible_path = os.path.join(voice_dir, "lasso_voice.md")
    proof_path = os.path.join(voice_dir, "social_proof.md")
    wrote_bible = _write_doc(bible_path, mapped["bible_text"])
    _write_doc(proof_path, mapped["proof_text"])
    return {
        "note": (f"drafted, held for approval ({bible_path})" if wrote_bible
                 else f"exists, not overwritten ({bible_path})"),
        "base": base,
        "bible_path": bible_path,
        "wrote": wrote_bible,
    }


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
    bible_note = write_brand_docs(account_key, answers, mapped=mapped)["note"]

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
        # `approve` is the explicit caller override; otherwise the shared
        # intake posture decides (AGENT_INTAKE_AUTO_APPROVE).
        status = "approved" if approve else client_sources.intake_status()
        created = client_sources.submit_intake(account_key, fresh, status=status)

    return {
        "bible": bible_note,
        "sources_created": len(created),
        "banned_words": mapped["banned_words"],
        "approver": mapped["approver"],
        "base": base,
    }


# ---- automatic forward: map EVERY un-routed intake into Echo -----------------------
# This closes the gap that left CrossFit ENG stranded: an intake was CAPTURED in
# echo_social_intake but never forwarded (echo_forwarded=false, not_routed). The
# forward step existed (onboard_from_social) but nothing ran it automatically.
# sync_unrouted() runs it for every un-routed row and marks the row routed, so no
# gym is ever silently stranded again. Nothing publishes; a human still approves
# every draft. Gated by AGENT_SOCIAL_INTAKE_SYNC (default OFF).

def _default_lister():
    """Live: the client_key of every echo_social_intake row not yet forwarded to
    Echo, newest first. Reads creds lazily; NEVER logs the key. No creds -> []."""
    from . import config
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return []
    import requests  # lazy, matches the repo pattern
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Accept": "application/json"}
    # PAGINATED so a large un-routed backlog is never silently truncated (the exact
    # "silent stranding" class this feature exists to kill). Walk Range windows until
    # a short page; a full page that keeps coming is followed, not dropped.
    page = 1000
    seen, out, offset = set(), [], 0
    while True:
        params = {
            "echo_forwarded": "is.false",
            "select": "client_key,submitted_at",
            "order": "submitted_at.desc",
        }
        hdr = dict(headers)
        hdr["Range-Unit"] = "items"
        hdr["Range"] = f"{offset}-{offset + page - 1}"
        # A read failure here used to be indistinguishable from "nothing un-routed":
        # a non-2xx broke out and a network error escaped the whole sweep to be caught
        # by the listener's bare print. Either way EVERY stranded gym stayed stranded
        # with no Slack signal. Both now alert, and a first-page failure is called out
        # separately because that is the case that looks exactly like a clean run.
        try:
            r = requests.get(f"{url}/rest/v1/echo_social_intake", params=params,
                             headers=hdr, timeout=30)
        except Exception as exc:  # noqa: BLE001
            ops_alerts.alert(
                f"social-intake-sync: could not read the un-routed intake list "
                f"({type(exc).__name__}). "
                + ("NO rows were read, so this pass looks clean but is not. "
                   if offset == 0 else
                   f"Partial: {len(out)} row(s) read before the failure. ")
                + "Un-routed gyms stay un-routed until the next successful pass.")
            break
        if r.status_code >= 400:
            ops_alerts.alert(
                f"social-intake-sync: un-routed intake list read returned "
                f"{r.status_code}. "
                + ("NO rows were read, so this pass looks clean but is not. "
                   if offset == 0 else
                   f"Partial: {len(out)} row(s) read before the failure. ")
                + "Un-routed gyms stay un-routed until the next successful pass.")
            break
        rows = r.json() or []
        for row in rows:
            ck = _clean(row.get("client_key"))
            if ck and ck not in seen:
                seen.add(ck)
                out.append(ck)
        if len(rows) < page:
            break
        offset += page
    return out


def _default_marker(base_key, account_key):
    """Live: mark every echo_social_intake row for base_key (the row's raw
    client_key) as forwarded to Echo, recording account_key (the RESOLVED Echo
    account it was routed into; equals base_key when no resolution happened).
    Best effort; a failed mark never loses the (already landed) onboarding work.
    Reads creds lazily; NEVER logs the key."""
    from . import config
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key:
        return False
    import requests  # lazy
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    body = {"echo_forwarded": True, "echo_status": "account_forwarded",
            "echo_account_key": account_key}
    r = requests.patch(f"{url}/rest/v1/echo_social_intake",
                       params={"client_key": f"eq.{base_key}"},
                       headers=headers, json=body, timeout=30)
    return r.status_code < 400


def _default_token_resolver(client_key):
    """Live: the EXISTING echo_account_key the portal minted for this intake row,
    from the portal's echo_intake_tokens table, or None.

    Self-serve rows carry the portal gym UUID as echo_social_intake.client_key;
    provisioning a tenant straight from that raw key would mint a second,
    UUID-named account DIVERGING from the name-slug account portal onboarding
    already created. The token row (gym_id -> echo_account_key) maps the raw key
    back to that existing account. Tries gym_id first (the UUID case), then
    echo_account_key (the already-a-slug case; a match there is a no-op resolve).
    A 400 on one column (e.g. a slug against a uuid-typed gym_id) just moves to
    the next; ANY failure returns None so the caller falls back to the raw key.
    Reads creds lazily; NEVER logs the key."""
    from . import config
    client_key = _clean(client_key)
    url = config.supabase_url()
    key = config.supabase_service_key()
    if not url or not key or not client_key:
        return None
    import requests  # lazy, matches the repo pattern
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Accept": "application/json"}
    for column in ("gym_id", "echo_account_key"):
        try:
            r = requests.get(f"{url}/rest/v1/echo_intake_tokens",
                             params={"select": "echo_account_key",
                                     column: f"eq.{client_key}",
                                     "limit": "1"},
                             headers=headers, timeout=8)
            if r.status_code >= 400:
                continue
            rows = r.json() or []
            if rows:
                acct = _clean(rows[0].get("echo_account_key"))
                if acct:
                    return acct
        except Exception:
            return None
    return None


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _looks_like_uuid(value):
    """True for a canonical 8-4-4-4-12 UUID. Used to tell a portal gym IDENTIFIER apart
    from a real account key, so only the identifier case gets re-minted."""
    return bool(_UUID_RE.match((str(value) if value is not None else "").strip()))


def _canonical_base(gym_id, answers):
    """The canonical account base for a gym that has NO portal token row yet, or "" when
    one cannot honestly be derived (no gym name). Same derivation as portal onboarding
    (account_key.canonical_account_key), so a gym arriving through the intake door and one
    arriving through the portal door land on the SAME key instead of two divergent tenants.
    Pure apart from reading `answers`; never raises out."""
    name = _clean(((answers or {}).get("gym") or {}).get("name"))
    if not name:
        return ""
    try:
        from .account_key import canonical_account_key
        return _clean(canonical_account_key(gym_id, name))
    except Exception:  # noqa: BLE001 - a derivation miss is an honest "" , never a crash
        return ""


def sync_unrouted(*, lister=None, reader=None, marker=None, onboard=None,
                  resolver=None, approve=False):
    """Map EVERY un-routed social intake into Echo. Returns a per-base summary list.

    For each un-routed row (keyed by its raw client_key):
      - resolve the raw client_key against the portal's echo_intake_tokens FIRST:
        a self-serve row carries the portal gym UUID, and provisioning straight
        from it would mint a second, UUID-named tenant diverging from the
        name-slug account portal onboarding already created. An existing token
        row's echo_account_key WINS; only a key with NO token row keeps its raw
        value (and may then provision fresh),
      - resolve the generation account (<base>_ig); when AGENT_DYNAMIC_ACCOUNTS is
        armed, a base with no account is AUTO-PROVISIONED (an inactive Account record
        built from the intake's gym info) so onboarding is zero-touch and scales to
        100+ gyms without hand-editing accounts.py. When dynamic accounts are OFF, a
        base with no account is SKIPPED with one ops alert (never fabricate an account),
      - run onboard_from_social (writes voice/proof docs if absent, lands client_sources),
      - mark the row routed (by its RAW client_key, recording the resolved account)
        so it is never re-processed.

    approve defaults FALSE: intake-derived client_sources land PENDING for one human
    review, matching client_sources.submit_intake's own contract ("client input is
    NEVER auto-trusted as fact"). Every downstream POST still cards for approval too.

    All I/O is injectable so tests run fully offline. Gated by the caller; this
    function itself always runs when called (the flag lives at the call sites)."""
    from . import accounts as _accounts
    from . import config as _config
    lister = lister or _default_lister
    reader = reader or _default_reader
    marker = marker or _default_marker
    onboard = onboard or onboard_from_social
    resolver = resolver or _default_token_resolver

    results = []
    for raw_key in lister():
        raw_key = _clean(raw_key)
        if not raw_key:
            continue
        # Token-row resolution BEFORE any provisioning (see docstring). The
        # intake row itself is still read and marked by its RAW client_key.
        resolved = _clean(resolver(raw_key) or "")
        base = resolved or raw_key
        account_key = f"{base}_ig"
        answers = read_social_intake(raw_key, reader=reader)
        have_account = (_accounts.get_account(account_key) is not None
                        or _accounts.get_account(base) is not None)

        # NO TOKEN ROW *AND* A UUID CLIENT KEY: the row carries the portal gym UUID, which
        # is an identifier, not an account key. The token-row branch above already refuses
        # to provision a UUID-named tenant, but with NO token row there was nothing to
        # resolve against and register_gym took the UUID verbatim. That is exactly the
        # corruption found in /data/gym_accounts.json on 2026-08-30 and repaired by hand.
        # Mint the CANONICAL key from (gym_id, gym name) instead, the same derivation
        # portal onboarding uses, so both doors land on the SAME key.
        # DELIBERATELY narrow: a slug-shaped client_key ('freshbox') is a real account key
        # already and is left exactly as it was. Re-minting those would hand every existing
        # gym a brand new key, which is the very stranding this guards against.
        if not have_account and not resolved and _looks_like_uuid(raw_key):
            minted = _canonical_base(raw_key, answers)
            if minted and minted != base:
                base = minted
                account_key = f"{base}_ig"
                have_account = (_accounts.get_account(account_key) is not None
                                or _accounts.get_account(base) is not None)
            elif not minted:
                # No usable gym name. We never fabricate one, and a UUID key is worse
                # than waiting, so leave it un-routed for a human rather than mint junk.
                ops_alerts.alert(
                    f"social-intake-sync: '{raw_key}' has no portal token row and no "
                    "usable gym name, so no canonical account key can be derived. "
                    "Left un-routed (a UUID-keyed account would strand it). Add the "
                    "gym name to its intake, or mint its key in the portal, then re-run.")
                results.append({"base": raw_key, "ok": False,
                                "reason": "no canonical key"})
                continue

        # AUTO-PROVISION (AGENT_DYNAMIC_ACCOUNTS): create the inactive Account record
        # from the intake's gym info so no gym is stranded waiting on a hand-added
        # accounts.py entry. Tokens stay by-hand (env); nothing publishes.
        if not have_account and _config.dynamic_accounts_enabled():
            gym = (answers or {}).get("gym") or {}
            try:
                _accounts.register_gym(
                    base,
                    name=_clean(gym.get("name")) or base,
                    ig_handle=_clean(gym.get("ig_handle")),
                    fb_page=_clean(gym.get("fb_page")))
                have_account = _accounts.get_account(account_key) is not None
            except Exception as e:
                ops_alerts.alert(f"social-intake-sync: auto-provision of '{base}' "
                                 f"failed: {type(e).__name__}: {e}. Left un-routed.")

        if not have_account:
            ops_alerts.alert(
                f"social-intake-sync: '{raw_key}' submitted a social intake but has no "
                f"registry account ('{account_key}' / '{base}'). Add the Account "
                "entry (or arm AGENT_DYNAMIC_ACCOUNTS), then re-run. The intake is "
                "safe and left un-routed.")
            results.append({"base": raw_key, "ok": False, "reason": "no account"})
            continue
        gen_key = account_key if _accounts.get_account(account_key) is not None else base
        if answers is None:
            results.append({"base": raw_key, "ok": False, "reason": "no answers"})
            continue
        try:
            out = onboard(gen_key, answers, approve=approve)
            # Mark the RAW row routed, recording the RESOLVED account it went to.
            marked = marker(raw_key, base)
            result = {"base": raw_key, "ok": True,
                      "account": gen_key,
                      "sources_created": out.get("sources_created", 0),
                      "marked_routed": bool(marked)}
            if resolved and resolved != raw_key:
                result["resolved"] = resolved
            results.append(result)
        except Exception as e:
            ops_alerts.alert(f"social-intake-sync: onboarding '{raw_key}' failed: "
                             f"{type(e).__name__}: {e}. Intake left un-routed.")
            results.append({"base": raw_key, "ok": False,
                            "reason": f"{type(e).__name__}"})
    return results
