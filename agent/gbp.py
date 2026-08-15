"""
Google Business Profile rail — pure helpers (no DB, no network).

Everything the GBP lane needs that can be unit-tested in isolation: the copy rules +
caption A+ gate (§5.2), UTM slugging + tagging (§5.2), the Zernio post payload builder
(§7.1, OFFER omits callToAction, CALL exempt from UTM), and the crop-before-approval
image pipeline (§5.3). The planner (client_content/client_month_run) and the publish
worker (zernio_publisher/calendar_autopublish) call INTO these; the rules live here so
they are enforced identically in the planner prompts AND re-validated at send time.

Spec: GBP_BUILD_SPEC.md. Rails carried over: no dashes, nothing invented, gen-pop,
human tap. GBP-added rails: no hashtags, no phone numbers in text, offers real+current,
owner's tap.
"""

import re

# --- vocab -----------------------------------------------------------------
TOPIC_TYPES = ("STANDARD", "EVENT", "OFFER")
CTA_TYPES = ("LEARN_MORE", "BOOK", "SIGN_UP", "CALL", "ORDER", "SHOP")
DEFAULT_CTA = "LEARN_MORE"            # LASSO standard; per-gym override when present
PLATFORM = "googlebusiness"

# --- caption bounds (§1, §5.2) --------------------------------------------
HARD_MAX_CHARS = 1500                 # Google's absolute cap
TARGET_MIN, TARGET_MAX = 150, 300     # the planner target band
HOOK_CHARS = 80                       # Google truncates ~here in Search
MIN_CONTENT_WORDS = 12                # reuse the A+ floor: a real caption, not a stub

# no em/en/figure-dash/minus, and no hyphen used AS a dash (spaced or doubled)
_DASH_RE = re.compile(r"[‐-―−]|(?:\s-\s)|--")
_HASHTAG_RE = re.compile(r"(?:^|\s)#\w")
# a phone number: 7+ digits with common separators, or a (xxx) xxx-xxxx shape
_PHONE_RE = re.compile(
    r"(?:\+?\d[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
_SCAFFOLD_RE = re.compile(
    r"^\s*(#{1,6}\s|(caption( body| text)?|body|post)\s*:)", re.IGNORECASE)


def _content_words(caption):
    lines = [ln for ln in (caption or "").splitlines()
             if not ln.strip().startswith("#")]
    return " ".join(lines).split()


def has_phone(text):
    return bool(_PHONE_RE.search(text or ""))


def caption_issues(caption, city=""):
    """Reasons a GBP caption is NOT A+ (empty list == A+). Hard rules only; the hook
    and city are also checked so the planner cannot skip them. `city` is the gym's
    city/neighborhood — when provided, the caption must name it once (a GBP ranking
    signal). Enforced in the planner AND re-validated by the worker at send time."""
    issues = []
    cap = (caption or "").strip()
    if not cap:
        return ["empty caption"]
    words = _content_words(cap)
    if len(cap) > HARD_MAX_CHARS:
        issues.append(f"caption over Google's {HARD_MAX_CHARS}-char cap ({len(cap)})")
    if len(words) < MIN_CONTENT_WORDS:
        issues.append(f"caption too thin ({len(words)} < {MIN_CONTENT_WORDS} words)")
    if _DASH_RE.search(cap):
        issues.append("caption contains a dash (no-dash copy law)")
    if _SCAFFOLD_RE.match(cap):
        issues.append("caption starts with LLM scaffolding, not real copy")
    if _HASHTAG_RE.search(cap):
        issues.append("caption contains a hashtag (GBP forbids hashtags)")
    if has_phone(cap):
        issues.append("caption contains a phone number (use the CALL button, not text)")
    if not cap[:HOOK_CHARS].strip():
        issues.append("empty hook (first 80 chars carry the whole load)")
    if city and city.strip().lower() not in cap.lower():
        issues.append(f"caption does not name the city '{city.strip()}'")
    return issues


def is_a_plus(caption, city=""):
    return not caption_issues(caption, city)


# --- UTM (§5.2) ------------------------------------------------------------
def pillar_slug(pillar):
    """lowercase, spaces -> underscores, ascii only, collapse repeats."""
    s = (pillar or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "post"


def utm_url(url, pillar):
    """Append the GBP organic UTM set to a destination URL. Idempotent-ish: uses & when
    the URL already has a query string. Empty/None url -> '' (a CALL CTA has no url and
    is exempt — the caller must not pass one)."""
    url = (url or "").strip()
    if not url:
        return ""
    utm = ("utm_source=google&utm_medium=organic_gbp"
           f"&utm_campaign=echo_{pillar_slug(pillar)}")
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{utm}"


# --- Zernio payload (§7.1) -------------------------------------------------
class GbpPayloadError(ValueError):
    """The requested GBP post is structurally invalid (bad topic type, OFFER with a
    callToAction, missing required field). Raised by the builder so a malformed post can
    never reach Zernio."""


def build_platform_data(*, account_id, topic_type, location_id, pillar,
                        cta_type=DEFAULT_CTA, cta_url="", event=None, offer=None):
    """The `platforms[0]` entry for a GBP post. Enforces the topic-type rules:
      * STANDARD/EVENT -> callToAction {type, url(utm)}; CALL type carries NO url.
      * OFFER          -> NO callToAction (Google renders 'View offer' from the offer);
                          UTM rides offer.redeemOnlineUrl instead.
      * EVENT/OFFER    -> event.schedule required (EVENT details, or the OFFER window).
    """
    tt = (topic_type or "").upper()
    if tt not in TOPIC_TYPES:
        raise GbpPayloadError(f"unknown topicType {topic_type!r}")
    if not account_id:
        raise GbpPayloadError("missing account_id")
    if not location_id:
        raise GbpPayloadError("missing location_id")

    psd = {"topicType": tt, "locationId": location_id}

    if tt == "OFFER":
        offer = dict(offer or {})
        if not offer:
            raise GbpPayloadError("OFFER requires offer fields")
        # UTM on the redeem url; NO callToAction on an OFFER.
        if offer.get("redeemOnlineUrl"):
            offer["redeemOnlineUrl"] = utm_url(offer["redeemOnlineUrl"], pillar)
        psd["offer"] = offer
        if event:                       # the offer WINDOW rides event.schedule
            psd["event"] = event
        # belt: never let a callToAction onto an OFFER
        psd.pop("callToAction", None)
    else:
        ct = (cta_type or DEFAULT_CTA).upper()
        if ct not in CTA_TYPES:
            raise GbpPayloadError(f"unknown cta_type {cta_type!r}")
        cta = {"type": ct}
        if ct != "CALL":                # CALL has no url; all others get UTM'd url
            if not cta_url:
                raise GbpPayloadError(f"{ct} CTA requires a url")
            cta["url"] = utm_url(cta_url, pillar)
        psd["callToAction"] = cta
        if tt == "EVENT":
            if not event:
                raise GbpPayloadError("EVENT requires event details")
            psd["event"] = event

    return {"platform": PLATFORM, "accountId": account_id,
            "platformSpecificData": psd}


def build_post_payload(*, caption, image_url, platform_data):
    """The full Zernio POST /v1/posts body for one GBP post. Exactly one image."""
    if not (caption or "").strip():
        raise GbpPayloadError("empty caption")
    if not (image_url or "").strip():
        raise GbpPayloadError("GBP post requires exactly one image")
    return {
        "content": caption,
        "mediaItems": [{"type": "image", "url": image_url}],
        "platforms": [platform_data],
    }


# --- image crop-before-approval (§5.3) -------------------------------------
GBP_W, GBP_H = 1200, 900              # 4:3
MIN_W, MIN_H = 400, 300
MAX_BYTES = 5 * 1024 * 1024


def crop_4x3(src_path, out_path):
    """Cover-crop `src_path` to 4:3 and resize to 1200x900, save JPEG q90. This runs at
    PLANNING time so the owner approves the exact pixels that publish (no post-approval
    transform, ever). Returns out_path. Raises on an unreadable/too-small image."""
    from PIL import Image, ImageOps
    im = Image.open(src_path).convert("RGB")
    if im.width < MIN_W or im.height < MIN_H:
        raise GbpPayloadError(
            f"image {im.size} below GBP minimum {(MIN_W, MIN_H)}")
    out = ImageOps.fit(im, (GBP_W, GBP_H), Image.LANCZOS)   # cover-crop, no distortion
    out.save(out_path, "JPEG", quality=90)
    return out_path
