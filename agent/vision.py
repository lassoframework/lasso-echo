"""
Echo Vision (ECHO_VISION_SPEC) — Phase 1: image understanding at ingest.

Extends DAM v1 (agent/dam.py autotag) to the v2 `media_analysis` schema: analyze once at
ingest, store as data on the DAM SIDECAR (ruling 1 — no new DB table), every consumer reads
the stored analysis. The analysis is treated as UNTRUSTED input: per-detail confidence, an
identity firewall on the descriptive fields, and safety routing so a wrong or unscreened
analysis can never auto-plan.

Provider: the existing Gemini vision path (ruling 2), reused from dam._default_reader; the
per-slot / per-gym-monthly accounting rides on top of the global daily cap in P4.

Nothing here publishes, picks, or captions — it only produces + validates the analysis and
answers "may this image auto-plan?" / "which details are caption-eligible?".
"""

import io
import json
import math
import re

VISION_VERSION = 2
CAPTION_CONFIDENCE = 0.85          # §2.2: only details >= this are caption-eligible
_REVIEW_CONFIDENCE = 0.7           # low overall confidence -> human review

SETTINGS = ("gym_floor", "front_desk", "exterior", "outdoor", "studio", "event", "other")
ACTIVITIES = ("strength", "cardio", "class", "coaching", "community", "facility", "food",
              "none")
PEOPLE_BUCKETS = ("none", "solo", "pair", "small_group", "crowd")
AVATAR_FITS = ("genpop", "athlete_leaning", "athlete", "unclear")
SAFETY_FLAGS = ("minor_prominent", "third_party_brand", "unsanitary", "injury_visible",
                "pii_visible")

# ---- identity firewall (§2.2, guardrail 11) --------------------------------------------
# Descriptive fields (one_line/subjects/visible_details) must carry NEUTRAL person terms
# only. These wordlists are the backstop behind the neutral prompt: a hit flags the analysis
# for review + strips the offending descriptive text from caption eligibility. text_in_image
# is NEVER scanned here (ruling 6) — it must capture name tags verbatim; a person-name there
# is caught by the model's contains_person_name flag instead.
_GENDER = (r"\bman\b", r"\bmen\b", r"\bwoman\b", r"\bwomen\b", r"\bmales?\b", r"\bfemales?\b",
           r"\bguys?\b", r"\bgirls?\b", r"\blady\b", r"\bladies\b", r"\bgentlem[ae]n\b",
           r"\bboys?\b", r"\bdudes?\b", r"\bhe\b", r"\bshe\b", r"\bhim\b", r"\bher\b",
           r"\bhis\b", r"\bhers\b")
# age terms are person-descriptors; kept conservative (over-blocking an "old rack" image is
# the SAFE failure per §10 — it routes to coach hand-pick, never a mismatch).
_AGE = (r"\byoung\b", r"\bold\b", r"\belderly\b", r"\bteenage[dr]?\b", r"\bteens?\b",
        r"\bseniors?\b", r"\bmiddle.aged\b", r"\bkids?\b")
# Body-appearance descriptors of a PERSON. `fat` excludes the common "fat-burning" compound
# (a class descriptor, not a body). A few terms (thin/blonde) can still hit object/facility
# descriptions ("thin mats", "blonde wood") — an accepted SAFE-direction over-block per §10
# (routes to coach, never a mismatch); revisit with an object-context allowlist only if the
# LASSO dogfood diff shows real exclusion at scale.
_APPEARANCE = (r"\bmuscular\b", r"\bripped\b", r"\bjacked\b", r"\boverweight\b", r"\bobese\b",
               r"\bskinny\b", r"\bslim\b", r"\bheavyset\b", r"\bchubby\b", r"\bplus.size\b",
               r"\btoned\b", r"\bshredded\b", r"\bbuff\b", r"\bfat\b(?!\s*[- ]?burn)",
               r"\bpetite\b", r"\bcurvy\b", r"\bstocky\b", r"\bbald\b", r"\bblonde?\b",
               r"\bbrunette\b", r"\bbearded\b", r"\bthin\b")
_HEALTH = (r"\binjured\b", r"\bunhealthy\b", r"\bdiabetic\b", r"\bpregnant\b", r"\bdisabled\b",
           r"\bobese\b")
_IDENTITY_RE = re.compile("|".join(_GENDER + _AGE + _APPEARANCE + _HEALTH), re.IGNORECASE)


def identity_issues(text):
    """Return the identity/appearance terms found in a descriptive string (empty when
    clean). The backstop behind the neutral prompt AND the caption gate (Phase 5)."""
    return sorted({m.group(0).lower() for m in _IDENTITY_RE.finditer(text or "")})


# ---- DCT perceptual hash (ruling 3) ----------------------------------------------------
_DCT_N = 32                        # downscale grid
_DCT_LOW = 8                       # low-frequency block kept for the hash
_COS = [[math.cos((2 * x + 1) * u * math.pi / (2 * _DCT_N)) for x in range(_DCT_N)]
        for u in range(_DCT_LOW)]  # precomputed cosine table (8 x 32)


def dct_phash(data):
    """A 64-bit DCT perceptual hash (pHash) as a 16-char hex string, or None when the bytes
    are not a readable image. More robust to scale/compression than the v1 average hash, so
    burst near-dupes cluster reliably (§3). Pure-Python DCT (no numpy)."""
    try:
        from PIL import Image  # lazy
        img = Image.open(io.BytesIO(data)).convert("L").resize((_DCT_N, _DCT_N))
    except Exception:
        return None
    px = list(img.getdata())
    # LOW-VARIANCE GUARD (audit item 4): a near-uniform image (blank wall, empty frame) has
    # ~0 DCT AC energy, so the median-threshold sign bits are rounding noise and unrelated
    # flats false-cluster. Encode the quantized MEAN brightness deterministically instead, so
    # same-brightness flats match and different-brightness flats separate.
    mean = sum(px) / len(px)
    var = sum((p - mean) ** 2 for p in px) / len(px)
    if var < 16:            # std < 4 grey levels: effectively flat
        bucket = min(255, max(0, int(mean)))
        return f"{(bucket << 8 | bucket) & 0xFFFFFFFFFFFFFFFF:016x}"
    grid = [px[r * _DCT_N:(r + 1) * _DCT_N] for r in range(_DCT_N)]
    # 2D DCT-II, keep only the top-left 8x8 low-frequency coefficients
    rows = [[sum(grid[r][x] * _COS[u][x] for x in range(_DCT_N)) for u in range(_DCT_LOW)]
            for r in range(_DCT_N)]
    coeffs = [[sum(rows[r][u] * _COS[v][r] for r in range(_DCT_N)) for u in range(_DCT_LOW)]
              for v in range(_DCT_LOW)]
    flat = [coeffs[v][u] for v in range(_DCT_LOW) for u in range(_DCT_LOW)]
    med = sorted(flat[1:])[len(flat[1:]) // 2]     # median excluding the DC term
    bits = "".join("1" if c > med else "0" for c in flat)
    return f"{int(bits, 2):016x}"


def hamming(a, b):
    """Hamming distance between two 16-char hex pHashes (§3, cluster at <=6). Large when
    either is missing/malformed so a bad hash never falsely clusters."""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 999


# ---- analysis normalization + firewall -------------------------------------------------
_VISION_PROMPT = (
    "You are a careful visual describer for a gym's social media. Look ONLY at what is "
    "visibly in this image and reply with ONLY a JSON object, no other text:\n"
    '{"one_line": "one neutral sentence; use NEUTRAL person terms only (a person, three '
    'people, a member, a coach); NEVER a name, gender, age, body, appearance, or health",\n'
    ' "setting": one of gym_floor|front_desk|exterior|outdoor|studio|event|other,\n'
    ' "subjects": [up to 5 short lowercase nouns for what is shown],\n'
    ' "people_bucket": one of none|solo|pair|small_group|crowd (estimate the grouping, not '
    'an exact count),\n'
    ' "includes_children": true or false,\n'
    ' "activity": one of strength|cardio|class|coaching|community|facility|food|none,\n'
    ' "visible_details": [{"detail": short phrase, "confidence": 0.0-1.0} up to 6],\n'
    ' "text_in_image": any legible text VERBATIM (signs, whiteboards, name tags) or null,\n'
    ' "activity_confidence": 0.0-1.0,\n'
    ' "quality": {"sharp": bool, "well_lit": bool, "usable": bool, "reject_reason": string '
    'or null},\n'
    ' "avatar_fit": one of genpop|athlete_leaning|athlete|unclear (athlete = competitive/'
    'physique/heavy-barbell; genpop = everyday people),\n'
    ' "safety_flags": [any of minor_prominent|third_party_brand|unsanitary|injury_visible|'
    'pii_visible]}\n'
    "Rules: describe ONLY the visible; invent nothing; no names/gender/age/body/health in "
    "any field EXCEPT text_in_image (verbatim). If a whiteboard or screen shows member "
    "names, phone numbers, or payment info, add pii_visible."
)


def _one_of(val, allowed, default):
    v = str(val or "").strip().lower()
    return v if v in allowed else default


def coerce_analysis(raw, *, phash=None):
    """Normalize a raw Gemini JSON string/dict into the v2 media_analysis schema, applying
    the identity firewall to the descriptive fields. Returns the analysis dict, or None when
    the payload cannot be parsed (caller treats None as a failed analysis)."""
    try:
        body = raw if isinstance(raw, dict) else json.loads(
            raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        return None

    one_line = str(body.get("one_line") or body.get("description") or "").strip()[:300]
    subjects = [str(s).strip().lower() for s in (body.get("subjects") or [])][:5]
    details_in = body.get("visible_details") or []
    details = []
    for d in details_in[:6]:
        if isinstance(d, dict) and d.get("detail"):
            try:
                conf = max(0.0, min(1.0, float(d.get("confidence", 0))))
            except (TypeError, ValueError):
                conf = 0.0
            details.append({"detail": str(d["detail"]).strip()[:80], "confidence": conf})

    # IDENTITY FIREWALL (ruling 6): scan one_line/subjects/details ONLY, never text_in_image.
    leak_terms = set(identity_issues(one_line))
    for s in subjects:
        leak_terms.update(identity_issues(s))
    clean_details = []
    for d in details:
        hit = identity_issues(d["detail"])
        if hit:
            leak_terms.update(hit)          # a leaking detail is dropped from eligibility
            continue
        clean_details.append(d)
    identity_flag = bool(leak_terms)

    text_in_image = body.get("text_in_image")
    text_in_image = (str(text_in_image).strip() or None) if text_in_image else None
    contains_person_name = bool(body.get("contains_person_name", False))
    # a person-name in the image text routes like a safety flag (§2.2); the model may set it,
    # else we conservatively treat a name-tag-ish text as a name when it looks like one.
    if text_in_image and _looks_like_person_name(text_in_image):
        contains_person_name = True

    q = body.get("quality") or {}
    quality = {
        "sharp": bool(q.get("sharp", True)),
        "well_lit": bool(q.get("well_lit", True)),
        "usable": bool(q.get("usable", True)),
        "reject_reason": (str(q.get("reject_reason")).strip() or None)
        if q.get("reject_reason") else None,
    }
    safety = [str(f).strip().lower() for f in (body.get("safety_flags") or [])
              if str(f).strip().lower() in SAFETY_FLAGS]

    try:
        activity_conf = max(0.0, min(1.0, float(body.get("activity_confidence", 0.8))))
    except (TypeError, ValueError):
        activity_conf = 0.8

    return {
        "version": VISION_VERSION,
        "one_line": one_line,
        "setting": _one_of(body.get("setting"), SETTINGS, "other"),
        "subjects": subjects,
        "people": {"bucket": _one_of((body.get("people") or {}).get("bucket")
                                     if isinstance(body.get("people"), dict)
                                     else body.get("people_bucket"),
                                     PEOPLE_BUCKETS, "none"),
                   "includes_children": bool(body.get("includes_children", False))},
        "activity": _one_of(body.get("activity"), ACTIVITIES, "none"),
        "visible_details": clean_details,
        "text_in_image": text_in_image,
        "contains_person_name": contains_person_name,
        "quality": quality,
        "avatar_fit": _one_of(body.get("avatar_fit"), AVATAR_FITS, "unclear"),
        "safety_flags": sorted(set(str(f).lower() for f in safety)),
        "activity_confidence": activity_conf,
        "identity_flag": identity_flag,
        "identity_terms": sorted(leak_terms),
        "phash": phash,
    }


# gym/equipment/exercise/time vocabulary that legitimately appears Titlecase on SIGNAGE and
# must NOT be mistaken for a member name (fixes the signage over-exclusion, audit item 3).
_GYM_VOCAB = {
    "the", "and", "for", "gym", "fitness", "studio", "club", "box", "team", "crew", "class",
    "open", "week", "day", "days", "hours", "welcome", "coach", "coaches", "member",
    "members", "front", "desk", "area", "zone", "room", "floor", "wall", "rack", "racks",
    "squat", "bench", "press", "deadlift", "clean", "jerk", "snatch", "row", "rowing",
    "rower", "rowers", "barbell", "dumbbell", "kettlebell", "kettlebells", "cardio",
    "strength", "conditioning", "mobility", "yoga", "spin", "cycle", "hyrox", "crossfit",
    "wod", "amrap", "emom", "rx", "pr", "workout", "warmup", "cooldown", "reps", "sets",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "morning", "evening", "noon", "session", "sessions",
    "results", "goals", "flow", "lift", "lifting", "run", "running", "sprint",
}
_NAME_CUE = re.compile(
    r"\b(coach|member|welcome|congrats|congratulations|great job|great work|way to go|"
    r"nice work|shout ?out|thanks?|thank you|meet|by|from|for)\s+([A-Z][a-zA-Z]{1,})",
    re.IGNORECASE)
_FULL_NAME = re.compile(r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b")   # Firstname Lastname


def _looks_like_person_name(text):
    """Heuristic backstop for a member name in the image text (name tag / whiteboard) when
    the model does not set contains_person_name itself. PRECISE by design — it fires only on
    a clear name signal (a name CUE followed by a Titlecase word, or a Firstname Lastname
    pair) whose tokens are NOT gym vocabulary — so Titlecase signage like 'Deadlift Area' or
    'Barbell Club' is not mistaken for a name (audit item 3). A hit routes to coach
    hand-pick; the model's own contains_person_name / pii_visible remain the primary
    defense."""
    t = text or ""
    for m in _NAME_CUE.finditer(t):
        if m.group(2).lower() not in _GYM_VOCAB:
            return True
    for m in _FULL_NAME.finditer(t):
        a, b = m.group(0).split()[:2]
        if a.lower() not in _GYM_VOCAB and b.lower() not in _GYM_VOCAB:
            return True
    return False


# ---- routing (§2.2, §4, guardrails 13/14) ----------------------------------------------
def caption_eligible_details(analysis):
    """Details a caption may lean on: confidence >= 0.85 AND already identity-clean. Below
    threshold, details exist for search/debug only."""
    if not analysis:
        return []
    return [d["detail"] for d in analysis.get("visible_details", [])
            if d.get("confidence", 0) >= CAPTION_CONFIDENCE]


def auto_plannable(analysis):
    """(ok, reasons): may this image be AUTO-picked by the planner? False (with reasons)
    for a missing/failed analysis, any safety flag, a person-name in the image, an identity
    leak, or unusable quality. Athlete / competitive / HYROX shots ARE now plannable for
    every gym (Blake 2026-08-18: the LASSO avatar rule no longer excludes competitive
    athletes); only `unclear` stays soft-restricted to behind-the-scenes slots in the pick
    scorer (§4). Safety exclusions (minors, PII, etc.) and the identity/body firewall are
    unchanged."""
    reasons = []
    if not analysis or analysis.get("analysis_failed"):
        return False, ["no analysis"]
    if analysis.get("version") != VISION_VERSION:
        return False, ["stale analysis version"]
    if analysis.get("safety_flags"):
        reasons += [f"safety:{f}" for f in analysis["safety_flags"]]
    if analysis.get("contains_person_name"):
        reasons.append("person_name_in_image")
    if analysis.get("identity_flag"):
        reasons.append("identity_leak")
    if not (analysis.get("quality") or {}).get("usable", False):
        reasons.append("unusable")
    return (not reasons), reasons


def bts_restricted(analysis):
    """§4: an `unclear` avatar_fit may ONLY fill a Behind-the-scenes slot (conservative
    default when the model could not tell what the shot is). athlete / athlete_leaning are NO
    LONGER restricted (Blake 2026-08-18: competitive athletes are a valid audience) — they
    score like any other photo. The pick scorer enforces the `unclear` restriction."""
    return (analysis or {}).get("avatar_fit") in ("unclear",)


# ---- Phase 2: near-duplicate clustering (Hamming <= 6) ---------------------------------
CLUSTER_HAMMING = 6                 # §3: cluster near-dupes within this pHash distance
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _phash_for(creative_path):
    """The image's DCT pHash: the one already stored on the sidecar at ingest (free), else
    computed now. None when unreadable."""
    a = stored_analysis(creative_path)
    if a and a.get("phash"):
        return a["phash"]
    try:
        with open(creative_path, "rb") as fh:
            return dct_phash(fh.read())
    except OSError:
        return None


def cluster_library(library_path):
    """§3 near-duplicate collapse: group a gym's images whose DCT pHashes are within
    CLUSTER_HAMMING, writing a shared `dupe_group` (the leader's basename) into every member's
    sidecar so rotation.rotation_key treats a burst of near-identical shots as ONE creative.

    Uses UNION-FIND (transitive closure), NOT greedy leader matching: a burst a~b~c where the
    end frames are >6 apart still forms one cluster, and the result is DETERMINISTIC —
    independent of filename order (audit fix). The leader is the lexicographically smallest
    member. Reads the pHash stored at ingest (no re-hash). Returns {leader: sorted[members]}
    for multi-member groups only; singletons keep their own filename key (no dupe_group)."""
    import os as _os
    from . import dam
    if not _os.path.isdir(library_path):
        return {}
    hashed = []          # (name, phash) for readable images, name-sorted
    for name in sorted(_os.listdir(library_path)):
        if _os.path.splitext(name)[1].lower() not in _IMG_EXTS:
            continue
        h = _phash_for(_os.path.join(library_path, name))
        if h:
            hashed.append((name, h))

    n = len(hashed)
    parent = list(range(n))

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)   # lower index (earlier name) wins: stable

    for i in range(n):
        for j in range(i + 1, n):
            if hamming(hashed[i][1], hashed[j][1]) <= CLUSTER_HAMMING:
                _union(i, j)

    comps = {}
    for i in range(n):
        comps.setdefault(_find(i), []).append(hashed[i][0])
    groups = {}
    for members in comps.values():
        if len(members) < 2:
            continue
        leader = min(members)           # deterministic leader, order-independent
        groups[leader] = sorted(members)
        for m in members:
            dam.write_sidecar(_os.path.join(library_path, m), {"dupe_group": leader})
    return groups


# ---- Phase 3: content scoring (§4 — match the slot JOB to the picture) -----------------
# Each pillar/slot-job maps to preferred image CONTENT. Keys are matched as substrings
# against the (lowercased) pillar so client categories (offer/service/testimonial/faq/about/
# promo) AND GBP pillar names ("local update", "photo", "proof", ...) both resolve.
_SLOT_PREFS = {
    "testimonial":  {"activity": ("coaching", "strength"), "people": ("solo", "pair"),
                     "setting": ("gym_floor", "studio")},           # Transformation
    "transform":    {"activity": ("coaching", "strength"), "people": ("solo", "pair"),
                     "setting": ("gym_floor", "studio")},
    "service":      {"activity": ("coaching", "class"), "people": ("solo", "pair", "small_group"),
                     "setting": ("gym_floor", "studio")},           # Education
    "faq":          {"activity": ("coaching", "facility"), "people": ("solo", "small_group"),
                     "setting": ("gym_floor", "front_desk")},       # Education
    "education":    {"activity": ("coaching", "class"), "people": ("solo", "small_group"),
                     "setting": ("gym_floor",)},
    "community":    {"activity": ("class", "community"), "people": ("small_group", "crowd"),
                     "setting": ("gym_floor", "event")},            # Community
    "about":        {"activity": ("community", "facility"), "people": ("small_group", "crowd"),
                     "setting": ("gym_floor", "front_desk")},
    "promo":        {"activity": ("class", "community"), "people": ("small_group", "crowd"),
                     "setting": ("gym_floor",)},
    "offer":        {"activity": ("facility", "community"), "people": ("small_group", "crowd",
                     "none"), "setting": ("gym_floor", "exterior")},  # Offer: best-lit facility/group
    "photo":        {"activity": ("facility", "community"), "people": ("none", "small_group",
                     "crowd"), "setting": ("gym_floor", "exterior", "front_desk")},  # GBP photo drop
    "local":        {"activity": ("facility", "community"), "people": ("small_group", "crowd",
                     "none"), "setting": ("exterior", "front_desk", "gym_floor")},  # GBP local update
    "behind":       {"activity": ("coaching", "facility"), "people": ("solo", "none"),
                     "setting": ("gym_floor", "front_desk")},       # Behind the scenes
    "proof":        {"activity": ("coaching", "strength", "community"), "people": ("solo",
                     "pair", "small_group"), "setting": ("gym_floor",)},
}
_DEFAULT_PREFS = {"activity": ("class", "coaching", "community", "facility"),
                  "people": ("solo", "pair", "small_group"), "setting": ("gym_floor",)}
VISION_SCORE_FLOOR = 3.0            # below this, the best pick is flagged weak_match (§4)
# a slot that WANTS a behind-the-scenes / facility feel — the only home for an
# `unclear` image (§4).
_BTS_SLOTS = ("behind", "faq", "about", "photo", "local", "offer")


def _prefs_for(pillar):
    """Resolve a pillar to its content-preference profile + the matched key. EXACT match
    first (the closed client category set: offer/service/testimonial/faq/about/promo — no
    ambiguity), then a substring fall-through for multi-word GBP pillars ('All in one offer'
    -> offer). This ordering keeps single-token pillars unambiguous and prevents a stray
    substring from mis-routing a slot (audit hardening)."""
    p = (pillar or "").strip().lower()
    if p in _SLOT_PREFS:
        return _SLOT_PREFS[p], p
    for key, prefs in _SLOT_PREFS.items():
        if key in p:
            return prefs, key
    return _DEFAULT_PREFS, ""


def content_score(analysis, pillar, *, recency=0.0):
    """(score, ok_for_slot): how well this image's CONTENT fits the slot job (§4). Higher is
    better. ok_for_slot is False when the image must not fill THIS slot (an `unclear` image
    outside a behind-the-scenes/facility slot). Score blends pillar affinity (activity +
    people + setting matches) + quality + a caller-supplied recency bonus (0..1, higher =
    fresher). Deterministic given the analysis + pillar + recency."""
    if not analysis or analysis.get("analysis_failed"):
        return -1.0, False
    prefs, key = _prefs_for(pillar)
    # BTS restriction (§4): an `unclear` shot only in a behind/facility slot. Athlete /
    # competitive / HYROX shots are unrestricted (Blake 2026-08-18).
    if bts_restricted(analysis) and key not in _BTS_SLOTS:
        return -1.0, False
    score = 0.0
    if analysis.get("activity") in prefs["activity"]:
        score += 3.0
    if (analysis.get("people") or {}).get("bucket") in prefs["people"]:
        score += 2.0
    if analysis.get("setting") in prefs["setting"]:
        score += 1.5
    q = analysis.get("quality") or {}
    score += (0.5 if q.get("sharp") else 0.0) + (0.5 if q.get("well_lit") else 0.0)
    score += max(0.0, min(1.0, recency))          # fresher (less recently served) ranks up
    return score, True


# ---- Phase 3.5: crop-verify (verify what SHIPS) ----------------------------------------
_VERIFY_PROMPT_HEAD = (
    "Look ONLY at this image and answer strictly as JSON, no other text. Confirm what is "
    "actually visible; do not guess. "
)


def crop_verify(image_bytes, analysis, *, reader=None):
    """§3.5 anti-hallucination: re-check the SHIPPED pixels (GBP = the 1200x900 crop, IG/FB
    = the original, ruling 4) against the ingest analysis. Confirms the people bucket and
    yes/no each caption-eligible (>=0.85) detail, so a caption may lean ONLY on details that
    survived the crop. Returns {"bucket": confirmed|None, "verified_details": [survivors],
    "ok": bool}. ANY failure degrades SAFE: ok=False, bucket=None, no verified details -> the
    caption falls back to safe generality (never blocks the slot, §3.5)."""
    safe = {"bucket": None, "verified_details": [], "ok": False}
    if not analysis or analysis.get("analysis_failed"):
        return safe
    eligible = caption_eligible_details(analysis)
    reader = reader or _verify_reader()
    if reader is None or not image_bytes:
        return safe
    prompt = (_VERIFY_PROMPT_HEAD +
              '{"people_bucket": one of none|solo|pair|small_group|crowd, '
              '"details_present": {' +
              ", ".join(f'"{d}": true or false' for d in eligible) +
              "}}")
    try:
        raw = reader(image_bytes, prompt)
        body = raw if isinstance(raw, dict) else json.loads(
            raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:  # noqa: BLE001 - a verify failure degrades safe, never raises
        return safe
    # An UNCONFIRMED bucket must NOT fall back to the stale ingest bucket (audit 3b): the
    # crop may have removed people. Return None so the gate fails people-count claims CLOSED
    # rather than licensing a crowd word against pixels that no longer show a crowd.
    bucket = _one_of(body.get("people_bucket"), PEOPLE_BUCKETS, None)
    present = body.get("details_present") or {}
    survivors = [d for d in eligible if bool(present.get(d))]
    return {"bucket": bucket, "verified_details": survivors, "ok": True}


def _verify_reader():
    """A Gemini reader that takes (image_bytes, prompt). None when unarmed/keyless."""
    from . import config
    import os as _os
    if not config.creative_studio_enabled():
        return None
    key = _os.environ.get(config.NANO_API_KEY_ENV)
    if not key:
        return None
    from google import genai
    from google.genai import types as gtypes
    client = genai.Client(api_key=key)

    def _read(image_bytes, prompt):
        resp = client.models.generate_content(
            model=config.OCR_MODEL,
            contents=[gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                      prompt])
        return getattr(resp, "text", "") or ""

    return _read


# ---- Phase 5: the grounding gate (closed 4-claim contradiction check) -------------------
# Contradiction-ONLY (§7): fail only when a caption ASSERTS something the verified analysis
# says is FALSE (or a high-risk unsupported claim). Claims merely absent from the analysis
# PASS — the analysis cannot enumerate everything visible.
# CROWD words that genuinely assert many people. Deliberately EXCLUDES "busy" and "everyone"
# — LASSO's avatar is "busy parents/professionals" and "everyone can start", which are not
# crowd claims; including them false-flagged clean captions.
_CROWD_WORDS = (r"\bpacked\b", r"\bcrowd(ed)?\b", r"\bfull house\b", r"\bsold ?out\b",
                r"\bwhole (gym|crew|team|class)\b", r"\bslammed\b", r"\bstanding room\b")
# SOLO words that genuinely assert one-on-one. Excludes "private"/"alone" (a "private space"
# or "come alone or with friends" is not a one-on-one claim — scale false-positives, audit 3a).
_SOLO_WORDS = (r"\bone[ -]on[ -]one\b", r"\bone[ -]to[ -]one\b", r"\bjust you and (a |your )?coach\b")
# OUTDOOR words that assert a physical outdoor setting. Excludes figurative "outside"/"sunshine"
# ("outside of class", "sunshine and good vibes") — audit 3a.
_OUTDOOR_WORDS = (r"\boutdoors\b", r"\bparking lot\b", r"\bin the parking\b",
                  r"\btrain(ing)? outside\b", r"\boutdoor (workout|session|class|training)\b")
# RISKY numbers only: percentages, weights, and member/client counts — the stat-like claims a
# receipt must back. Program durations (weeks/days/reps/sessions/minutes) are ordinary copy and
# are NOT gated (they were false-failing "stronger in 6 weeks", audit 5).
_NUMBER_RE = re.compile(r"\b\d+\s?%|\b\d[\d,]*\s?(lbs?|pounds?|kg|members?|clients?)\b",
                        re.IGNORECASE)


def _rx_any(patterns, text):
    return any(re.search(p, text or "", re.IGNORECASE) for p in patterns)


# ---- Phase 6: client_context platform-policy screen (§8) --------------------------------
# client_context is RAW MATERIAL, never verbatim output. Before any caption may use it, it
# must pass this platform-policy screen (health/medical claims, review bait, before/after
# weight promises) AND the full post_quality gate. Policy-violating context routes to the
# coach, never silently becomes a caption.
_POLICY_HEALTH = (r"\bcure[sd]?\b", r"\bheal(s|ed|ing)?\b", r"\bdiagnos", r"\btreat(s|ed|ment)?\b",
                  r"\bdisease\b", r"\bmedical\b", r"\bclinical(ly)?\b", r"\bdoctor\b",
                  r"\bprescri", r"\bdepression\b", r"\banxiety\b", r"\bdiabet", r"\bblood pressure\b")
_POLICY_REVIEW_BAIT = (r"\bleave (us )?a (5|five)[ -]star\b", r"\breview us\b",
                       r"\bgive us a review\b", r"\bin exchange for a review\b",
                       r"\brate us\b", r"\bpost a review\b")
_POLICY_WEIGHT_PROMISE = (r"\blose \d+\s?(lbs?|pounds?|kg)\b", r"\bguarantee[d]?\b",
                          r"\bguaranteed results\b", r"\bmelt (away )?fat\b",
                          r"\bdrop \d+\s?(lbs?|pounds?|sizes?)\b", r"\bburn fat fast\b")


def policy_screen(text):
    """§8: platform-policy issues in owner-written client_context (empty == clean). Health/
    medical claims, review bait, and before/after weight PROMISES are real Meta/GBP policy
    violations even when owner-written and true — so context that trips this NEVER becomes a
    caption; it routes to the coach with the reason."""
    issues = []
    if _rx_any(_POLICY_HEALTH, text):
        issues.append("health/medical claim (platform policy)")
    if _rx_any(_POLICY_REVIEW_BAIT, text):
        issues.append("review incentive/bait (platform policy)")
    if _rx_any(_POLICY_WEIGHT_PROMISE, text):
        issues.append("before/after weight promise or guarantee (platform policy)")
    return issues


def context_usable(client_context, *, banned_words=()):
    """§8: (ok, reasons) — may this client_context feed a caption? It must clear the
    platform-policy screen AND the publish-hard-fail formatting a caption cannot carry
    (dashes, hashtags, a phone number, a banned word) — the things that would hard-fail at
    the publish worker after approval. It is RAW MATERIAL, not a finished caption, so the
    caption-SHAPE checks (length / thinness / scaffold) do NOT apply. Empty context is
    trivially ok (role-words-only is the default)."""
    ctx = (client_context or "").strip()
    if not ctx:
        return True, []
    reasons = policy_screen(ctx)
    try:
        from . import post_quality, gbp
        reasons += [i for i in post_quality.caption_issues(ctx, banned_words)
                    if "dash" in i or "banned" in i]   # keep hard-fails, drop shape checks
        if "#" in ctx:
            reasons.append("hashtag in context")
        if gbp.has_phone(ctx):
            reasons.append("phone number in context")
    except Exception:  # noqa: BLE001
        pass
    return (not reasons), reasons


def grounding_contradictions(caption, analysis, *, verified=None, gym_claims=(),
                             consent=False, client_context=""):
    """§5/§7 grounding gate: the list of CONTRADICTIONS between a caption and the verified
    analysis (empty = clean). Closed taxonomy — exactly four claim classes plus the high-risk
    guards:
      1. people quantity: a crowd word requires a crowd/small_group bucket (crop-verified);
         a one-on-one/solo word requires solo/pair.
      2. setting: an outdoor/outside word requires an outdoor/exterior setting.
      3. activity: (checked via verified details/subjects; only a hard mismatch fails).
      4. objects in the hook: an object the crop-verify REJECTED must not be asserted.
    High-risk unsupported (always fail): identity terms (gender/appearance/age/health), and
    numbers not backed by the gym record. Absence never fails."""
    issues = []
    cap = caption or ""
    ctx = (client_context or "").lower()
    # bucket: when a verify ran, use its CONFIRMED bucket (may be None = unconfirmed); a
    # people-count word is allowed only against a confirmed bucket (fail closed, audit 3b).
    # When no verify ran, fall back to the analysis bucket.
    if verified is not None:
        bucket = verified.get("bucket")
        bucket_confirmed = bool(verified.get("ok")) and bucket is not None
    else:
        bucket = (analysis or {}).get("people", {}).get("bucket")
        bucket_confirmed = bucket is not None
    # 1. people quantity honesty
    if _rx_any(_CROWD_WORDS, cap) and not (bucket_confirmed and bucket in ("crowd",
                                                                           "small_group")):
        issues.append(f"crowd word but bucket not confirmed crowd (got {bucket})")
    if _rx_any(_SOLO_WORDS, cap) and not (bucket_confirmed and bucket in ("solo", "pair")):
        issues.append(f"one-on-one word but bucket not confirmed solo/pair (got {bucket})")
    # 2. setting honesty
    if _rx_any(_OUTDOOR_WORDS, cap) and (analysis or {}).get("setting") not in (
            "outdoor", "exterior"):
        issues.append("outdoor word but image is indoors")
    # 4. objects the crop-verify REJECTED (eligible at ingest but not confirmed on the crop)
    #    must not be asserted. Word-boundary match, not substring ('chalk' != 'chalkboard').
    if verified is not None and verified.get("ok"):
        survived = set(verified.get("verified_details") or [])
        low = cap.lower()
        for d in caption_eligible_details(analysis):
            if d not in survived and re.search(r"\b" + re.escape(d.lower()) + r"\b", low):
                issues.append(f"asserts '{d}' which the crop did not confirm")
    # high-risk identity: allowed ONLY with consent AND the term in the client's OWN context
    # (§8: the checkbox grants consent; an image-derived gender/age never does).
    for t in identity_issues(cap):
        if not (consent and t in ctx):
            issues.append(f"identity term '{t}'")
    # high-risk numbers: allowed when the number is in an approved gym claim OR (consent AND
    # in the client's context). Normalized so format variants (40lbs vs 40 lbs) align.
    claims_norm = [_norm_num(c) for c in gym_claims]
    ctx_norm = _norm_num(ctx)
    for m in _NUMBER_RE.finditer(cap):
        frag = m.group(0).strip()
        fn = _norm_num(frag)
        if not (fn and (any(fn in cn for cn in claims_norm) or (consent and fn in ctx_norm))):
            issues.append(f"unsupported number '{frag}'")
    return issues


def _norm_num(s):
    """Normalize a number+unit for matching: lowercase, strip spaces, collapse pound/lbs/kg
    synonyms so '40 lbs', '40lbs', and '40 pounds' all compare equal."""
    t = re.sub(r"\s+", "", (s or "").lower())
    return t.replace("pounds", "lb").replace("pound", "lb").replace("lbs", "lb")


def cluster_count(library_path):
    """§3 starvation guard input: the number of DISTINCT near-dupe CLUSTERS in the library
    (each multi-shot burst counts once), plus every singleton. This — not the raw image
    count — is how many genuinely different photos the month can draw on."""
    import os as _os
    from . import dam
    if not _os.path.isdir(library_path):
        return 0
    seen_groups = set()
    count = 0
    for name in sorted(_os.listdir(library_path)):
        if _os.path.splitext(name)[1].lower() not in _IMG_EXTS:
            continue
        group = dam.read_sidecar(_os.path.join(library_path, name)).get("dupe_group")
        if group:
            if group in seen_groups:
                continue
            seen_groups.add(group)
        count += 1
    return count


# ---- store / read on the DAM sidecar (ruling 1: sidecar, no DB) ------------------------
MAX_ATTEMPTS = 3                   # §2.1: nightly retry sweep, then analysis_failed + alert
_SIDE_KEY = "media_analysis"
_ATTEMPT_KEY = "media_analysis_attempts"


def stored_analysis(creative_path):
    """The v2 analysis stored on the sidecar, or None. Returns the analysis dict even when
    it is the {analysis_failed:true} marker (so callers can tell 'failed' from 'missing')."""
    from . import dam
    return dam.read_sidecar(creative_path).get(_SIDE_KEY)


def analysis_state(creative_path):
    """'ok' | 'failed' | 'missing' for one asset — the planner's screen (§2.1)."""
    a = stored_analysis(creative_path)
    if not a:
        return "missing"
    if a.get("analysis_failed") or a.get("version") != VISION_VERSION:
        return "failed" if a.get("analysis_failed") else "missing"
    return "ok"


def _vision_reader():
    """Gemini vision reader with the v2 prompt (mirrors dam._default_reader wiring). None
    when the studio is unarmed / keyless."""
    from . import config
    import os as _os
    if not config.creative_studio_enabled():
        return None
    key = _os.environ.get(config.NANO_API_KEY_ENV)
    if not key:
        return None
    from google import genai  # lazy
    from google.genai import types as gtypes
    client = genai.Client(api_key=key)

    def _read(image_bytes):
        resp = client.models.generate_content(
            model=config.OCR_MODEL,
            contents=[gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                      _VISION_PROMPT])
        return getattr(resp, "text", "") or ""

    return _read


def within_gym_budget(gym, day, *, alert=None):
    """Ruling 2: a per-gym MONTHLY cap on vision calls, on top of the global daily cap.
    Increments a (gym, month) counter in the kv store and returns False once the cap is hit
    (alarming staff ONCE), so a runaway re-analysis loop cannot burn the budget. cap<=0 or no
    gym disables the check. Best-effort: a kv error never blocks a call (returns True)."""
    from . import config
    cap = config.vision_gym_monthly_cap()
    if cap <= 0 or not gym:
        return True
    try:
        from . import db
        month = str(day or "")[:7] or "unknown"
        key = f"vision_spend_{gym}_{month}"
        cur = int(db.kv_get(key, "0") or "0")
        if cur >= cap:
            akey = f"vision_budget_alarm_{gym}_{month}"
            if alert and not db.kv_get(akey):
                alert(f"vision: {gym} hit the monthly vision-call cap ({cap}) for {month} — "
                      "pausing further analysis/verify (possible re-analysis loop?)")
                db.kv_set(akey, "1")
            return False
        db.kv_set(key, str(cur + 1))
        return True
    except Exception:  # noqa: BLE001 - budget accounting must never block a call
        return True


def analyze_and_store(creative_path, *, reader=None, day=None, alert=None, force=False,
                      gym=None):
    """Analyze ONE image once and store the v2 analysis on its DAM sidecar. Idempotent:
    a sidecar already carrying a current-version analysis is SKIPPED (preserve-on-re-sync,
    ruling 1) unless force=True. Returns the analysis dict, the existing one on skip, or
    None when it could not run (unarmed / keyless / past the daily cap).

    Failure handling (§2.1): a parse/read failure bumps an attempt counter; on the 3rd
    failed attempt the sidecar is marked {analysis_failed:true} and staff is alerted. A
    failed/absent analysis EXCLUDES the image from auto-planning (auto_plannable)."""
    from . import dam, config
    from datetime import date

    existing = dam.read_sidecar(creative_path).get(_SIDE_KEY)
    if existing and existing.get("version") == VISION_VERSION and not force:
        return existing

    reader = reader or _vision_reader()
    if reader is None:
        return None
    day = day or date.today().isoformat()
    from .creative_studio import spend_allowed
    if not spend_allowed(account_key=None, day=day):
        return None
    if gym and not within_gym_budget(gym, day, alert=alert):
        return None                       # ruling 2: per-gym monthly cap (runaway guard)

    try:
        with open(creative_path, "rb") as fh:
            raw_bytes = fh.read()
    except OSError:
        return None
    phash = dct_phash(raw_bytes)
    try:
        analysis = coerce_analysis(reader(raw_bytes), phash=phash)
    except Exception as exc:  # noqa: BLE001 - never raise out of ingest
        print(f"[vision] analyze failed for {creative_path}: {type(exc).__name__}")
        analysis = None

    if analysis is not None:
        dam.write_sidecar(creative_path, {_SIDE_KEY: analysis, _ATTEMPT_KEY: 0,
                                          "media_analysis_version": VISION_VERSION})
        if analysis.get("identity_flag") and alert:
            alert(f"vision: identity terms in analysis for "
                  f"{creative_path} ({analysis.get('identity_terms')}); routed to review")
        return analysis

    # failure path: bump attempts; escalate on the 3rd.
    attempts = int(dam.read_sidecar(creative_path).get(_ATTEMPT_KEY, 0)) + 1
    if attempts >= MAX_ATTEMPTS:
        failed = {"version": VISION_VERSION, "analysis_failed": True,
                  "phash": phash, "attempts": attempts}
        dam.write_sidecar(creative_path, {_SIDE_KEY: failed, _ATTEMPT_KEY: attempts})
        if alert:
            alert(f"vision: analysis FAILED after {attempts} attempts for {creative_path}; "
                  "excluded from auto-planning (coach hand-pick only)")
        return failed
    dam.write_sidecar(creative_path, {_ATTEMPT_KEY: attempts})
    return None


def analyze_library(library_path, *, reader=None, day=None, alert=None, force=False,
                    logger=None, gym=None):
    """Backfill/ingest sweep: analyze every not-yet-analyzed image in a gym's library
    (idempotent, throttled by the daily spend cap). Videos are skipped (§2.1, out of scope
    for v1). Returns {analyzed, skipped, failed}.

    gym: the canonical base key for the per-gym monthly cap (ruling 2). Defaults to the
    library-folder basename so a standalone backfill still keys per-gym; callers with the
    real base key (the ingest sweep) should pass it so the cap counter is canonical."""
    import os as _os
    log = logger or (lambda m: print(f"[vision] {m}"))
    gym = gym or _os.path.basename(library_path.rstrip("/"))
    counts = {"analyzed": 0, "skipped": 0, "failed": 0}
    if not _os.path.isdir(library_path):
        return counts
    for name in sorted(_os.listdir(library_path)):
        if _os.path.splitext(name)[1].lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        path = _os.path.join(library_path, name)
        state = analysis_state(path)
        if state in ("ok", "failed") and not force:
            counts["skipped"] += 1
            continue
        res = analyze_and_store(path, reader=reader, day=day, alert=alert, force=force,
                                gym=gym)
        if res is None:
            counts["failed"] += 1        # could not run (cap/keyless) — retried next sweep
        elif res.get("analysis_failed"):
            counts["failed"] += 1
        else:
            counts["analyzed"] += 1
    log(f"{library_path}: analyzed {counts['analyzed']}, skipped {counts['skipped']}, "
        f"failed {counts['failed']}")
    return counts
