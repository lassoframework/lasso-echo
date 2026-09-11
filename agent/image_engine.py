"""
image_engine.py — the provider abstraction for image / infographic generation.

ASTRA IS THE DEFAULT. `IMAGE_ENGINE` (default "astra") names the primary engine;
Gemini (Nano Banana) is UNCHANGED and demoted to fallback. Nothing here publishes
and no approval gate is weakened: this module only decides WHO draws the pixels.
Every generated asset still lands in the same approval queue as uploaded creative.

THE CONTRACT (one interface, two engines):

    engine.generate(prompt, opts) -> ImageResult(
        image_bytes, image_url, model, engine, cost_estimate, revised_prompt,
        latency_ms, prompt_used)

A failed attempt RAISES ImageEngineError carrying the provider's REAL response
text (status + body, secret-scrubbed). It is never swallowed into a bare None,
because this repo has a documented history of silent-failure bugs.

THE FALLBACK CHAIN (generate_image):

    Astra -> retry once with backoff -> Gemini -> mark "needs human"

Every rung logs. The last rung fires an ops alert on the SAME surface as every
other ops alert AND writes an append-only `image_needs_human` audit row, so a
calendar slot can never fail silently. generate_image returns None only after
that marking has happened; None keeps the existing caller contract (block or
fall back to a library creative), so no gate changes behavior.

SECRETS: OPENAI_API_KEY is read from env at the moment of use, never stored on a
returned object, never logged. A missing key is not an error: the chain boots
with engine=gemini and logs ONE warning for the life of the process.

COST: every image logs engine, model, latency and an ESTIMATED cost, and bumps a
per-day running total that prints as a daily line. Crossing
AGENT_IMAGE_DAILY_COST_ALERT_USD (default 10.00) fires ONE alert per day. The
figures are estimates from env-tunable per-image rates, never a billing read.
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date as _date

from . import config

# The OpenAI Responses endpoint the Astra engine posts to. Overridable for a
# proxy / gateway, never for silently swapping providers.
ASTRA_RESPONSES_URL = os.environ.get(
    "AGENT_ASTRA_RESPONSES_URL", "https://api.openai.com/v1/responses")

# Name of the env var, never the value (repo convention, mirrors NANO_API_KEY_ENV).
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# Astra gets exactly ONE retry (two attempts) before the chain falls to Gemini.
ASTRA_MAX_RETRIES = 1
ASTRA_RETRY_BACKOFF_SECS = 2.0     # multiplied by the attempt number
ASTRA_TIMEOUT_SECS = 180.0

# Daily estimated-spend alert threshold (USD).
DAILY_COST_ALERT_DEFAULT_USD = 10.0

# kv key prefix for the per-day estimated image spend, in whole cents.
_COST_KEY_PREFIX = "image_cost_cents_"
_COUNT_KEY_PREFIX = "image_count_"
_COST_ALERT_KEY_PREFIX = "image_cost_alerted_"

# The engines this module knows how to build. Anything else in IMAGE_ENGINE is a
# typo, and a typo must announce itself rather than quietly demote to Gemini.
KNOWN_ENGINES = ("astra", "gemini")

# One warning per process when the key is absent (spec: "log a single warning").
_missing_key_warned = False
# One warning per process for an unrecognised IMAGE_ENGINE value.
_unknown_engine_warned = False


def _log(msg):
    print(f"[image-engine] {msg}")


# ---------------------------------------------------------------------------
# Result + error types
# ---------------------------------------------------------------------------


@dataclass
class ImageResult:
    """One generated image. `image_bytes` is the buffer when the provider returns
    inline data; `image_url` is set instead when it returns a link. At least one
    of the two is always populated on a success."""

    image_bytes: bytes = b""
    image_url: str = ""
    model: str = ""
    engine: str = ""
    cost_estimate: float = 0.0
    revised_prompt: str = ""
    latency_ms: int = 0
    prompt_used: str = ""

    def ok(self) -> bool:
        return bool(self.image_bytes) or bool(self.image_url)


class ImageEngineError(RuntimeError):
    """A single generation attempt failed. Carries the provider's REAL response
    (status + body) so the failure can be reported instead of guessed at. The
    body is secret-scrubbed before it is ever logged or alerted."""

    def __init__(self, message, *, engine="", model="", status=None,
                 response_text=""):
        super().__init__(message)
        self.engine = engine
        self.model = model
        self.status = status
        self.response_text = response_text or ""

    def detail(self, limit=600):
        """A one-line, secret-scrubbed summary safe for logs and ops alerts."""
        from . import ops_alerts
        bits = [f"engine={self.engine}", f"model={self.model!r}"]
        if self.status is not None:
            bits.append(f"http={self.status}")
        bits.append(str(self))
        if self.response_text:
            bits.append("response=" + str(self.response_text)[:limit])
        return ops_alerts.scrub(" ".join(bits))


# ---------------------------------------------------------------------------
# Routing: which engine, which model, what size
# ---------------------------------------------------------------------------


def engine_name() -> str:
    """The configured PRIMARY engine. Spec name is `IMAGE_ENGINE`; the
    AGENT_-prefixed alias is accepted so the repo's env conventions (and the
    test env sweep) still reach it. Default "astra"."""
    raw = (os.environ.get("IMAGE_ENGINE")
           or os.environ.get("AGENT_IMAGE_ENGINE") or "")
    return raw.strip().lower() or "astra"


def astra_brief_model() -> str:
    """The Responses-API model that reads the creative brief and calls the image
    tool. Spec value from Blake: "gpt-6-astra"."""
    raw = (os.environ.get("ASTRA_BRIEF_MODEL")
           or os.environ.get("AGENT_ASTRA_BRIEF_MODEL") or "")
    return raw.strip() or config.ASTRA_BRIEF_MODEL


def astra_image_model() -> str:
    """The Sunburst image model: infographics, carousels, any text overlay."""
    raw = (os.environ.get("ASTRA_IMAGE_MODEL")
           or os.environ.get("AGENT_ASTRA_IMAGE_MODEL") or "")
    return raw.strip() or config.ASTRA_IMAGE_MODEL


def astra_image_model_flare() -> str:
    """The Flare image model: story-format quick graphics with no rendered text."""
    raw = (os.environ.get("ASTRA_IMAGE_MODEL_FLARE")
           or os.environ.get("AGENT_ASTRA_IMAGE_MODEL_FLARE") or "")
    return raw.strip() or config.ASTRA_IMAGE_MODEL_FLARE


# Asset kinds that ALWAYS route to Sunburst regardless of surface.
_SUNBURST_KINDS = ("infographic", "carousel")


def select_astra_model(opts=None) -> str:
    """Pick the Astra image model for one asset.

    Precedence (first match wins):
      1. an explicit opts["model"] (the caller overrode it by hand)
      2. kind is an infographic or a carousel        -> Sunburst
      3. the asset carries a text overlay            -> Sunburst
      4. a STORY surface (quick graphic, no text)    -> Flare
      5. anything else                               -> Sunburst

    Text overlay beats the story surface on purpose: a Story card with a rendered
    headline is a text asset, and text accuracy is what Sunburst is for.
    """
    opts = opts or {}
    explicit = str(opts.get("model") or "").strip()
    if explicit:
        return explicit
    kind = str(opts.get("kind") or "").strip().lower()
    if kind in _SUNBURST_KINDS:
        return astra_image_model()
    if opts.get("has_text_overlay"):
        return astra_image_model()
    if "story" in str(opts.get("surface") or "").lower():
        return astra_image_model_flare()
    return astra_image_model()


def size_for(opts=None) -> str:
    """The pixel size for one asset: an explicit size/pixels opt, else the Story
    target (1080x1920) on a story surface, else the feed target (1080x1350)."""
    opts = opts or {}
    explicit = str(opts.get("size") or opts.get("pixels") or "").strip()
    if explicit:
        return explicit
    if "story" in str(opts.get("surface") or "").lower():
        return config.STORY_PIXELS
    return config.IMAGE_PIXELS


# The live Astra image tool rejects any size whose width or height is not a
# multiple of 16 ("Invalid size '1080x1350'. Width and height must both be
# divisible by 16.", HTTP 400, verified against the real API 2026-09-10). Our
# IG targets (1080x1350 feed, 1080x1920 story) both fail that rule on width, so
# every request must be SNAPPED before it is sent or Astra 400s forever and the
# chain lives on the Gemini rung by accident.
ASTRA_SIZE_MULTIPLE = 16

# Exact-ratio snaps for the two targets Echo actually uses. Both are the same
# aspect as the target with both dimensions divisible by 16, and both are
# confirmed HTTP 200 against the live API:
#   4:5  1080x1350 -> 1024x1280   (1024/16=64, 1280/16=80, ratio 0.800 exactly)
#   9:16 1080x1920 -> 1152x2048   (1152/16=72, 2048/16=128, ratio 0.5625 exactly)
_EXACT_SIZE_SNAPS = {
    "1080x1350": "1024x1280",
    "1080x1920": "1152x2048",
}


def parse_size(size):
    """('1080x1350') -> (1080, 1350), or None when it is not a WxH string."""
    try:
        w, h = str(size).lower().split("x", 1)
        return int(w), int(h)
    except (AttributeError, TypeError, ValueError):
        return None


def snap_size(size):
    """The size actually SENT to Astra: same aspect, both dimensions divisible by
    16. Known targets use an exact-ratio snap; anything else rounds each
    dimension to the nearest multiple of 16 (never below 16). An unparseable
    size passes through untouched so the provider can reject it loudly."""
    key = str(size or "").lower().strip()
    if key in _EXACT_SIZE_SNAPS:
        return _EXACT_SIZE_SNAPS[key]
    dims = parse_size(key)
    if dims is None:
        return size
    m = ASTRA_SIZE_MULTIPLE
    snapped = tuple(max(m, int(round(d / m)) * m) for d in dims)
    return f"{snapped[0]}x{snapped[1]}"


def fit_to_size(image_bytes, size):
    """Resize generated bytes to the caller's requested target (e.g. back to the
    1080x1350 feed frame after rendering at the snapped 1024x1280). The snap
    preserves aspect, so this is a clean scale, never a crop or a stretch.

    NEVER raises and never returns empty: if Pillow is missing or the bytes are
    not a readable image, the original bytes pass through unchanged. Losing a
    card to a resize would be worse than shipping it a few pixels off target."""
    dims = parse_size(size)
    if not image_bytes or dims is None:
        return image_bytes
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as im:
            if (im.width, im.height) == dims:
                return image_bytes
            out = io.BytesIO()
            im.convert("RGB").resize(dims, Image.LANCZOS).save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - a resize may never lose the card
        _log(f"resize to {size} failed ({type(exc).__name__}: {exc}); "
             "keeping the generated bytes as rendered")
        return image_bytes


def cost_estimate(engine, model) -> float:
    """ESTIMATED USD per image. Env-tunable rates; never a billing read."""
    def _rate(var, default):
        try:
            return float(os.environ.get(var, default))
        except (TypeError, ValueError):
            return float(default)

    if engine == "astra":
        if "flare" in str(model or "").lower():
            return _rate("AGENT_ASTRA_COST_FLARE_USD", "0.04")
        return _rate("AGENT_ASTRA_COST_SUNBURST_USD", "0.19")
    return _rate("AGENT_GEMINI_COST_PER_IMAGE_USD", "0.039")


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class ImageEngine:
    """One image provider. Implementations return an ImageResult or raise
    ImageEngineError; they never return None and never swallow a provider error."""

    name = ""

    def generate(self, prompt, opts=None) -> ImageResult:  # pragma: no cover
        raise NotImplementedError

    def prompt_for(self, prompt, opts=None) -> str:
        """The prompt THIS engine sends. A caller may hand each engine its own
        text via opts["engine_prompts"] = {"astra": ..., "gemini": ...}; anything
        unset falls back to the shared prompt."""
        opts = opts or {}
        per_engine = opts.get("engine_prompts") or {}
        return str(per_engine.get(self.name) or prompt or "")


# ---------------------------------------------------------------------------
# Astra (ChatGPT-6 / gpt-image-2.5) — the DEFAULT engine
# ---------------------------------------------------------------------------


class AstraImageEngine(ImageEngine):
    """ChatGPT-6 Astra via the OpenAI Responses API.

    POST /v1/responses with the brief model, `input` = the creative brief, and an
    `image_generation` tool pinned to the Astra image model. The generated image
    comes back inside the tool output. The API key is held for the call only: it
    is never logged, never returned, never placed on the result.
    """

    name = "astra"

    def __init__(self, api_key, *, transport=None):
        self._api_key = api_key          # never logged or exposed
        self._transport = transport      # test seam: (url, headers, body) -> (status, text)

    # -- HTTP ---------------------------------------------------------------
    def _post(self, payload):
        """Return (status, body_text). Uses urllib (repo pattern, no new dep) and
        ALWAYS reads the error body so a rejection can be reported verbatim."""
        if self._transport is not None:
            return self._transport(ASTRA_RESPONSES_URL,
                                   {"Content-Type": "application/json"},
                                   payload)
        import urllib.error
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            ASTRA_RESPONSES_URL, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=ASTRA_TIMEOUT_SECS) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # The REAL rejection body (e.g. an unknown-model error) lives here.
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            return exc.code, body

    # -- generate -----------------------------------------------------------
    def generate(self, prompt, opts=None) -> ImageResult:
        opts = dict(opts or {})
        brief = self.prompt_for(prompt, opts)
        image_model = select_astra_model(opts)
        brief_model = astra_brief_model()
        target_size = size_for(opts)
        # The tool only accepts dimensions divisible by 16; send the snapped
        # size and scale the result back to the caller's target below.
        size = snap_size(target_size)

        tool = {"type": "image_generation", "model": image_model, "size": size}
        payload = {"model": brief_model, "input": brief, "tools": [tool]}

        started = time.monotonic()
        status, body = self._post(payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        if status != 200:
            raise ImageEngineError(
                f"Astra responses call rejected (HTTP {status})",
                engine=self.name, model=f"{brief_model}+{image_model}",
                status=status, response_text=body)

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ImageEngineError(
                f"Astra returned unparseable JSON: {exc}",
                engine=self.name, model=image_model, status=status,
                response_text=body) from exc

        image_b64, image_url, revised = extract_astra_image(data)
        if not image_b64 and not image_url:
            raise ImageEngineError(
                "Astra returned no image in the image_generation tool output",
                engine=self.name, model=image_model, status=status,
                response_text=body)

        image_bytes = b""
        if image_b64:
            import base64
            try:
                image_bytes = base64.b64decode(image_b64)
            except Exception as exc:
                raise ImageEngineError(
                    f"Astra image payload was not valid base64: {exc}",
                    engine=self.name, model=image_model,
                    status=status) from exc
        elif image_url:
            # A link-only response still has to reach the caller as BYTES: every
            # consumer writes a file. A failed fetch is a loud engine failure,
            # never a silently empty card.
            try:
                image_bytes = fetch_image_bytes(image_url)
            except Exception as exc:
                raise ImageEngineError(
                    f"Astra returned an image url that could not be fetched: {exc}",
                    engine=self.name, model=image_model, status=status) from exc

        # Back to the caller's requested frame (1080x1350 feed / 1080x1920 story).
        image_bytes = fit_to_size(image_bytes, target_size)

        return ImageResult(
            image_bytes=image_bytes, image_url=image_url, model=image_model,
            engine=self.name, cost_estimate=cost_estimate(self.name, image_model),
            revised_prompt=revised, latency_ms=latency_ms, prompt_used=brief)


def fetch_image_bytes(url, timeout=60):
    """Download a link-only image result. Raises on any failure (the caller turns
    it into a loud engine failure)."""
    import urllib.request
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def extract_astra_image(data):
    """Pull (base64, url, revised_prompt) out of a Responses-API payload.

    Walks the output tree looking for an image_generation tool result. Tolerant
    of the shapes the Responses API uses (a top-level `image_generation_call`
    item, or an image part nested under a message's `content`); returns empty
    strings when there is no image, which the caller turns into a LOUD error.
    """
    revised = {"text": ""}
    _B64_FIELDS = ("result", "b64_json", "image_base64", "base64")
    _URL_FIELDS = ("image_url", "url")
    # A real image is thousands of base64 chars; this floor only rejects status
    # strings ("completed", "in_progress") that share a field name.
    _MIN_B64_LEN = 16

    def _walk(node, depth=0):
        if depth > 8:
            return "", ""
        if isinstance(node, list):
            for item in node:
                b64, url = _walk(item, depth + 1)
                if b64 or url:
                    return b64, url
            return "", ""
        if not isinstance(node, dict):
            return "", ""

        rp = node.get("revised_prompt")
        if isinstance(rp, str) and rp and not revised["text"]:
            revised["text"] = rp

        node_type = str(node.get("type") or "")
        if "image" in node_type:
            for field_name in _B64_FIELDS:
                val = node.get(field_name)
                if isinstance(val, str) and len(val) > _MIN_B64_LEN:
                    return val, ""
            for field_name in _URL_FIELDS:
                val = node.get(field_name)
                if isinstance(val, str) and val.startswith("http"):
                    return "", val

        for key in ("output", "content", "results", "data", "response"):
            if key in node:
                b64, url = _walk(node.get(key), depth + 1)
                if b64 or url:
                    return b64, url
        return "", ""

    b64, url = _walk(data)
    return b64, url, revised["text"]


# ---------------------------------------------------------------------------
# Gemini (Nano Banana) — UNCHANGED, demoted to fallback
# ---------------------------------------------------------------------------


class GeminiImageEngine(ImageEngine):
    """The existing Gemini path, wrapped in the ImageEngine interface. The call
    it makes is byte-for-byte what creative_studio made before: the same client,
    the same routed model, the same hard timeout and bounded retry."""

    name = "gemini"

    def __init__(self, client=None):
        self._client = client

    def _resolve_client(self):
        if self._client is not None:
            return self._client
        from . import creative_studio
        return creative_studio._default_client()

    def generate(self, prompt, opts=None) -> ImageResult:
        opts = dict(opts or {})
        text = self.prompt_for(prompt, opts)
        model = str(opts.get("gemini_model") or "").strip() or config.NANO_MODEL

        client = self._resolve_client()
        if client is None:
            raise ImageEngineError(
                "Gemini client unavailable (creative studio flag off or no key)",
                engine=self.name, model=model)

        from . import creative_studio
        started = time.monotonic()
        image_bytes = creative_studio._render_with_timeout(
            lambda: client.generate_image(prompt=text, model=model))
        latency_ms = int((time.monotonic() - started) * 1000)

        if image_bytes is None:
            raise ImageEngineError(
                "Gemini render returned nothing (timeout or provider error)",
                engine=self.name, model=model)

        return ImageResult(
            image_bytes=image_bytes, model=model, engine=self.name,
            cost_estimate=cost_estimate(self.name, model),
            latency_ms=latency_ms, prompt_used=text)


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def _warn_missing_key_once():
    """Spec 5: with no OPENAI_API_KEY, boot with engine=gemini and log ONE
    warning. Never raises, never names a value."""
    global _missing_key_warned
    if _missing_key_warned:
        return
    _missing_key_warned = True
    _log(f"WARNING: {OPENAI_API_KEY_ENV} is not set; Astra is unavailable. "
         "Booting with engine=gemini (fallback only).")


def _warn_unknown_engine_once(name):
    """An unrecognised IMAGE_ENGINE value silently running on Gemini is exactly
    the silent demotion this module exists to prevent. Warn ONCE, then run on the
    Gemini rung (which the chain always keeps)."""
    global _unknown_engine_warned
    if _unknown_engine_warned:
        return
    _unknown_engine_warned = True
    _log(f"WARNING: IMAGE_ENGINE={name!r} is not a known engine "
         f"({', '.join(KNOWN_ENGINES)}); running on gemini.")


def announce_boot():
    """ONE self-announcing startup line naming the engine actually in force, so a
    missing key or a typo'd engine name is visible the moment the service boots
    rather than at the first render. The effective name is read off the REAL
    chain, so this line can never disagree with what actually renders. Returns
    the effective engine name. Never raises."""
    selected = engine_name()
    chain = engine_chain()
    effective = chain[0].name if chain else "none"
    _log(f"[startup] IMAGE_ENGINE={selected} effective={effective} "
         f"brief={astra_brief_model()!r} sunburst={astra_image_model()!r} "
         f"flare={astra_image_model_flare()!r} feed={config.IMAGE_PIXELS} "
         f"story={config.STORY_PIXELS} "
         f"daily_cost_alert=${daily_cost_alert_usd():.2f}")
    return effective


def engine_chain(gemini_client=None):
    """The ordered engines for one generation.

      IMAGE_ENGINE=astra (default) -> [Astra, Gemini]   (Gemini is the fallback)
      IMAGE_ENGINE=gemini          -> [Gemini]          (Astra never called)
      astra selected but no key    -> [Gemini]          + ONE warning

    Gemini is ALWAYS the last rung when Astra is primary: demoting it must never
    remove it.
    """
    chain = []
    selected = engine_name()
    if selected not in KNOWN_ENGINES:
        _warn_unknown_engine_once(selected)
    if selected == "astra":
        key = os.environ.get(OPENAI_API_KEY_ENV)
        if key:
            chain.append(AstraImageEngine(key))
        else:
            _warn_missing_key_once()
    chain.append(GeminiImageEngine(gemini_client))
    return chain


def _attempts_for(engine):
    return ASTRA_MAX_RETRIES + 1 if engine.name == "astra" else 1


def generate_image(prompt, opts=None, *, gemini_client=None, account_key="",
                   subject="", sleep=None):
    """Run the fallback chain for ONE image.

    Astra -> retry once with backoff -> Gemini -> mark "needs human".

    Returns an ImageResult on success. Returns None ONLY after mark_needs_human
    has fired the ops alert and written the audit row, so a calendar slot can
    never fail silently. Never raises into the caller.
    """
    opts = dict(opts or {})
    sleep = sleep or time.sleep
    failures = []

    for engine in engine_chain(gemini_client):
        tries = _attempts_for(engine)
        for attempt in range(1, tries + 1):
            try:
                result = engine.generate(prompt, opts)
            except ImageEngineError as exc:
                detail = exc.detail()
                failures.append(f"{engine.name} attempt {attempt}/{tries}: {detail}")
                _log(f"FAILED engine={engine.name} attempt={attempt}/{tries} {detail}")
            except Exception as exc:  # noqa: BLE001 - a provider bug may not kill the run
                from . import ops_alerts
                detail = ops_alerts.scrub(f"{type(exc).__name__}: {exc}")
                failures.append(f"{engine.name} attempt {attempt}/{tries}: {detail}")
                _log(f"FAILED engine={engine.name} attempt={attempt}/{tries} {detail}")
            else:
                if not result.ok():
                    failures.append(
                        f"{engine.name} attempt {attempt}/{tries}: empty result")
                    _log(f"FAILED engine={engine.name} attempt={attempt}/{tries} "
                         "empty result")
                else:
                    _log(f"ok engine={result.engine} model={result.model!r} "
                         f"latency_ms={result.latency_ms} "
                         f"cost_est=${result.cost_estimate:.3f}")
                    record_cost(result.cost_estimate, account_key=account_key)
                    return result
            if attempt < tries:
                sleep(ASTRA_RETRY_BACKOFF_SECS * attempt)

    mark_needs_human(subject=subject, account_key=account_key, failures=failures)
    return None


def mark_needs_human(subject="", account_key="", failures=(), day=None):
    """Every engine failed: mark the asset for a HUMAN in the approval queue.

    Three surfaces, because a silent miss is the failure mode this repo has been
    burned by:
      1. ONE ops alert on the same surface as every other ops alert,
      2. an append-only `image_needs_human` audit row (queryable by day/account),
      3. a loud stdout line in the run output.

    No gate is weakened: the asset does NOT enter the queue as approvable art, it
    enters as a named human to-do, and the caller still receives None (block or
    library fallback, exactly as before). Never raises.
    """
    day = day or _date.today().isoformat()
    detail = "; ".join(str(f) for f in failures) or "no engine was available"
    label = str(subject or "").strip() or "(no headline)"
    who = account_key or "the shared pool"
    line = (f"NEEDS HUMAN: image generation failed on EVERY engine for {who}: "
            f"{label}. No asset was produced and the slot is NOT filled. "
            f"Attempts: {detail}")
    _log(line)
    try:
        from . import db as _db
        _db.audit("image_needs_human", label, detail, account_key, day)
    except Exception as exc:  # noqa: BLE001 - marking must never break the run
        _log(f"audit write for needs-human failed: {type(exc).__name__}: {exc}")
    try:
        from . import ops_alerts as _ops
        _ops.alert(line)
    except Exception as exc:  # noqa: BLE001
        _log(f"ops alert for needs-human failed: {type(exc).__name__}: {exc}")
    return {"needs_human": True, "subject": label, "account_key": account_key,
            "day": day, "detail": detail}


# ---------------------------------------------------------------------------
# Cost observability
# ---------------------------------------------------------------------------


def daily_cost_alert_usd() -> float:
    try:
        return float(os.environ.get("AGENT_IMAGE_DAILY_COST_ALERT_USD",
                                    DAILY_COST_ALERT_DEFAULT_USD))
    except (TypeError, ValueError):
        return DAILY_COST_ALERT_DEFAULT_USD


def daily_cost_usd(day=None) -> float:
    """Estimated image spend so far today, in USD."""
    from . import db as _db
    day = day or _date.today().isoformat()
    try:
        return int(_db.kv_get(_COST_KEY_PREFIX + day, "0") or 0) / 100.0
    except (TypeError, ValueError):
        return 0.0


def daily_image_count(day=None) -> int:
    from . import db as _db
    day = day or _date.today().isoformat()
    try:
        return int(_db.kv_get(_COUNT_KEY_PREFIX + day, "0") or 0)
    except (TypeError, ValueError):
        return 0


def daily_cost_line(day=None) -> str:
    """The daily cost line for the ops log."""
    day = day or _date.today().isoformat()
    return (f"daily image spend {day}: ${daily_cost_usd(day):.2f} estimated "
            f"across {daily_image_count(day)} images "
            f"(alert at ${daily_cost_alert_usd():.2f})")


def record_cost(usd, day=None, account_key=""):
    """Add one image's estimated cost to the day's running total, print the daily
    line, and fire ONE alert per day if the day crosses the threshold. Cost
    accounting is observability, never a gate: it never blocks a generation and
    never raises."""
    day = day or _date.today().isoformat()
    try:
        from . import db as _db
        cents = int(round(float(usd) * 100))
        total = int(_db.kv_get(_COST_KEY_PREFIX + day, "0") or 0) + cents
        _db.kv_set(_COST_KEY_PREFIX + day, str(total))
        count = int(_db.kv_get(_COUNT_KEY_PREFIX + day, "0") or 0) + 1
        _db.kv_set(_COUNT_KEY_PREFIX + day, str(count))
    except Exception as exc:  # noqa: BLE001
        _log(f"cost accounting failed: {type(exc).__name__}: {exc}")
        return
    _log(daily_cost_line(day))
    _maybe_alert_daily_cost(day, account_key=account_key)


def _maybe_alert_daily_cost(day, account_key=""):
    """ONE alert per day once estimated image spend crosses the threshold."""
    threshold = daily_cost_alert_usd()
    spend = daily_cost_usd(day)
    if spend <= threshold:
        return False
    try:
        from . import db as _db, ops_alerts as _ops
        alert_key = _COST_ALERT_KEY_PREFIX + day
        if _db.kv_get(alert_key) == "1":
            return False
        _db.kv_set(alert_key, "1")
        _ops.alert(f"Image spend over budget: ${spend:.2f} estimated today "
                   f"({daily_image_count(day)} images), over the "
                   f"${threshold:.2f} daily alert threshold. "
                   f"Engine: {engine_name()}.")
    except Exception as exc:  # noqa: BLE001
        _log(f"cost alert failed: {type(exc).__name__}: {exc}")
        return False
    return True
