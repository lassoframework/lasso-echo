"""Small OpenAI Responses text client shared by Echo's grounded model lanes.

No provider object or key survives the call. Provider failures raise a bounded error;
callers keep their existing hold, fallback, and approval behavior.
"""

import json
import os
import urllib.error
import urllib.request


RESPONSES_URL = "https://api.openai.com/v1/responses"


def complete(system, user, *, model, max_output_tokens, transport=None, timeout=30):
    """Return completed output text, or raise without exposing provider body or secrets.

    ``transport`` is the offline test seam: (url, content_headers, payload) ->
    (http_status, json_text). It never receives the authorization header.
    """
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    if not isinstance(model, str) or not model.startswith("gpt-"):
        raise ValueError("Echo text model must be an OpenAI gpt model")
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
