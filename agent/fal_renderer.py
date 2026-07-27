"""
Headless fal.ai renderer for b-roll overlays.

Replaces the interactive Higgsfield MCP path for Railway cron runs.
Requires AGENT_FAL_API_KEY. Uses the fal.ai queue REST API — no fal-client
package, pure urllib so no extra dependency.

Flag: AGENT_FAL_API_KEY (set in Railway env, never logged or committed).
      AGENT_FAL_VIDEO_MODEL (default: fal-ai/kling-video/v1.6/standard/text-to-video)
      AGENT_FAL_IMAGE_MODEL (default: fal-ai/flux/schnell)

Callable interface (matches render_overlays renderer param):
    fal_renderer(beat, out_path, kind)  ->  writes asset to out_path or raises
"""

import json
import os
import time
import urllib.error
import urllib.request

_FAL_QUEUE = "https://queue.fal.run"
_POLL_INTERVAL_S = 8
_TIMEOUT_S = 420  # 7 min; video generation can take ~2-3 min


def fal_api_key():
    return os.environ.get("AGENT_FAL_API_KEY", "").strip()


def _video_model():
    return (os.environ.get("AGENT_FAL_VIDEO_MODEL", "")
            or "fal-ai/kling-video/v1.6/standard/text-to-video").strip()


def _image_model():
    return (os.environ.get("AGENT_FAL_IMAGE_MODEL", "")
            or "fal-ai/flux/schnell").strip()


def _auth_headers(key):
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


def _http_post(url, payload, key):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in _auth_headers(key).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _http_get(url, key):
    req = urllib.request.Request(url)
    for k, v in _auth_headers(key).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _submit(model, payload, key):
    url = f"{_FAL_QUEUE}/{model}"
    resp = _http_post(url, payload, key)
    rid = resp.get("request_id") or resp.get("requestId")
    if not rid:
        raise RuntimeError(f"fal: no request_id in submit response: {resp}")
    return rid


def _poll(model, rid, key):
    deadline = time.monotonic() + _TIMEOUT_S
    while time.monotonic() < deadline:
        url = f"{_FAL_QUEUE}/{model}/requests/{rid}/status"
        status_resp = _http_get(url, key)
        status = status_resp.get("status", "")
        if status == "COMPLETED":
            return
        if status in ("FAILED", "CANCELLED"):
            raise RuntimeError(
                f"fal: job {rid} {status}: {status_resp.get('error', '')}")
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"fal: job {rid} timed out after {_TIMEOUT_S}s")


def _fetch_result(model, rid, key):
    url = f"{_FAL_QUEUE}/{model}/requests/{rid}"
    return _http_get(url, key)


def _download_asset(asset_url, out_path):
    req = urllib.request.Request(asset_url)
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)


def fal_renderer(beat, out_path, kind):
    """
    Callable matching the render_overlays renderer interface.
      kind='video' -> fal.ai text-to-video (Kling or AGENT_FAL_VIDEO_MODEL)
      kind='image' -> fal.ai image generation (Flux or AGENT_FAL_IMAGE_MODEL)
    Writes the output to out_path. Raises on any failure.
    """
    key = fal_api_key()
    if not key:
        raise RuntimeError("AGENT_FAL_API_KEY not set — cannot render headless b-roll")

    prompt = (beat.get("prompt") or beat.get("visual") or "").strip()
    if not prompt:
        raise RuntimeError(f"fal: beat has no prompt or visual: {beat}")

    if kind == "image":
        model = _image_model()
        payload = {
            "prompt": prompt,
            "image_size": {"width": 1080, "height": 1920},
            "num_inference_steps": 4,
        }
    else:
        model = _video_model()
        dur = beat.get("duration", 5)
        payload = {
            "prompt": prompt,
            "duration": str(int(min(max(dur, 5), 10))),
            "aspect_ratio": "9:16",
        }

    print(f"[fal] submitting {kind} | model={model}", flush=True)
    rid = _submit(model, payload, key)
    print(f"[fal] job {rid} submitted — polling", flush=True)

    _poll(model, rid, key)

    result = _fetch_result(model, rid, key)

    # Extract output URL (Kling: result.video.url; Flux: result.images[0].url)
    asset_url = None
    if kind == "image":
        images = result.get("images") or []
        if images:
            asset_url = images[0].get("url")
    else:
        vid = result.get("video") or {}
        asset_url = vid.get("url")
        if not asset_url:
            vids = result.get("videos") or []
            if vids:
                asset_url = vids[0].get("url")

    if not asset_url:
        raise RuntimeError(f"fal: no output URL in result: {result}")

    print(f"[fal] downloading {kind} -> {out_path}", flush=True)
    _download_asset(asset_url, out_path)
    size = os.path.getsize(out_path)
    print(f"[fal] done: {size:,} bytes", flush=True)


def build_renderer():
    """Return fal_renderer callable when AGENT_FAL_API_KEY is set, else None."""
    return fal_renderer if fal_api_key() else None
