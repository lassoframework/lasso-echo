"""Small OpenAI Responses text client shared by Echo's grounded model lanes.

No provider object or key survives the call. Provider failures raise a bounded error;
callers keep their existing hold, fallback, and approval behavior.
"""

import json
import os
import urllib.error
import urllib.request


RESPONSES_URL = "https://api.openai.com/v1/responses"
ALLOWED_TEXT_MODELS = frozenset({"gpt-6-astra", "gpt-6-luna"})


def validate_model(model):
    """Accept only Echo-approved Responses text models, never image or unknown IDs."""
    if not isinstance(model, str) or model not in ALLOWED_TEXT_MODELS:
        raise ValueError("Echo text model must be a supported OpenAI text model")
    return model


def startup_preflight(*, force_website_intake=False):
    """Fail boot before side effects when any enabled text lane lacks valid runtime config.

    ``force_website_intake`` covers the one-gym manual entrypoint even when its
    automatic sweep is disabled. This checks configuration only; it never calls
    a model or exposes a key.
    """
    from . import config
    from .slack_convo.identities import IDENTITIES

    lanes = []
    if any(config.slack_convo_identity_enabled(name) for name in IDENTITIES):
        lanes.append(("Slack grounded answer/classifier", config.slack_convo_model))
    if (config.sb7_enabled() or config.auto_reels_enabled()
            or config.gbp_mirror_enabled() or config.gbp_mirror_gyms()
            or config.gbp_month_sweep_enabled()):
        lanes.append(("SB7/caption", config.sb7_model))
    if force_website_intake or config.website_auto_intake_enabled():
        lanes.append(("website intake", config.sb7_model))
    if (config.clipper_enabled() or config.video_editor_enabled()
            or config.podcast_auto_enabled() or config.episode_inbox_enabled()):
        lanes.append(("clipper", config.clipper_model))
    if not lanes:
        return ()
    for lane, reader in lanes:
        try:
            validate_model(reader())
        except ValueError as exc:
            raise RuntimeError(f"OpenAI text startup preflight: {lane}: {exc}") from exc
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OpenAI text startup preflight: OPENAI_API_KEY not set for "
                           + ", ".join(lane for lane, _ in lanes))
    return tuple(lane for lane, _ in lanes)


def complete(system, user, *, model, max_output_tokens, transport=None, timeout=30):
    """Return completed output text, or raise without exposing provider body or secrets.

    ``transport`` is the offline test seam: (url, content_headers, payload) ->
    (http_status, json_text). It never receives the authorization header.
    """
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    validate_model(model)
    payload = {"model": model, "instructions": system, "input": user,
               "max_output_tokens": max_output_tokens}
    if transport is not None:
        status, body = transport(RESPONSES_URL, {"Content-Type": "application/json"},
                                 payload)
    else:
        request = urllib.request.Request(
            RESPONSES_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, body = response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"OpenAI text model HTTP {exc.code}") from None
    if status != 200:
        raise RuntimeError(f"OpenAI text model HTTP {status}")
    try:
        response = json.loads(body)
        if response.get("status") != "completed":
            raise ValueError("incomplete response")
        parts = [part.get("text", "") for item in response.get("output", [])
                 if item.get("type") == "message"
                 for part in item.get("content", [])
                 if part.get("type") == "output_text"]
        result = "".join(parts).strip()
        if not result:
            raise ValueError("empty response")
        return result
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise RuntimeError("OpenAI text model returned no complete text") from exc
