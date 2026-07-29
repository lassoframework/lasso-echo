"""
Headless Higgsfield renderer for b-roll overlays.

Uses the higgsfield-client SDK with HF_API_KEY + HF_API_SECRET (or HF_KEY)
set in Railway env. Preferred over fal.ai when credentials are present.

Flags:
    HF_API_KEY + HF_API_SECRET  — combined into 'key:secret' by the SDK
    HF_KEY                       — single combined key alternative
    AGENT_HF_VIDEO_APP           — SDK application path for text-to-video
                                   (default: kling-video/v3.0/text-to-video)
    AGENT_HF_IMAGE_APP           — SDK application path for text-to-image
                                   (default: bytedance/seedream/v4/text-to-image)

NOTE on video endpoint paths: Higgsfield does not publish a full model path
catalog. Defaults are inferred from the confirmed image path pattern. If you
get a 404, set AGENT_HF_VIDEO_APP to the correct path — check the Higgsfield
platform dashboard or docs for the latest model endpoint strings.

Callable interface (matches render_overlays renderer param):
    higgsfield_renderer(beat, out_path, kind)  ->  writes asset to out_path or raises
"""

import os
import urllib.request

_HF_TIMEOUT = 600  # 10 min; video generation can take several minutes
_CHUNK = 65536


def _hf_key():
    """Return combined HF credentials string, or empty string if not set."""
    key = os.environ.get("HF_KEY", "").strip()
    if key:
        return key
    api_key = os.environ.get("HF_API_KEY", "").strip()
    api_secret = os.environ.get("HF_API_SECRET", "").strip()
    if api_key and api_secret:
        return f"{api_key}:{api_secret}"
    return ""


def _video_app():
    return (os.environ.get("AGENT_HF_VIDEO_APP", "")
            or "kling-video/v3.0/text-to-video").strip()


def _image_app():
    return (os.environ.get("AGENT_HF_IMAGE_APP", "")
            or "bytedance/seedream/v4/text-to-image").strip()


def _download(url, out_path):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)


def _extract_url(result, kind):
    if kind == "image":
        imgs = result.get("images") or []
        if imgs:
            return imgs[0].get("url")
    else:
        vid = result.get("video") or {}
        url = vid.get("url")
        if url:
            return url
        vids = result.get("videos") or []
        if vids:
            return vids[0].get("url")
    return None


def higgsfield_renderer(beat, out_path, kind):
    """
    Callable matching the render_overlays renderer interface.
      kind='video' -> Higgsfield text-to-video (Kling 3.0 or AGENT_HF_VIDEO_APP)
      kind='image' -> Higgsfield text-to-image (Seedream or AGENT_HF_IMAGE_APP)
    Writes the output to out_path. Raises on any failure.
    """
    key = _hf_key()
    if not key:
        raise RuntimeError(
            "HF_API_KEY + HF_API_SECRET (or HF_KEY) not set — "
            "cannot render headless b-roll via Higgsfield"
        )

    prompt = (beat.get("prompt") or beat.get("visual") or "").strip()
    if not prompt:
        raise RuntimeError(f"higgsfield: beat has no prompt or visual: {beat}")

    try:
        from higgsfield_client.http.client import SyncClient
    except ImportError as exc:
        raise RuntimeError(
            "higgsfield-client not installed — add it to requirements.txt"
        ) from exc

    client = SyncClient(api_key=key, timeout=_HF_TIMEOUT)

    if kind == "image":
        app = _image_app()
        args = {
            "prompt": prompt,
            "aspect_ratio": "9:16",
        }
    else:
        app = _video_app()
        dur = int(min(max(beat.get("duration", 5), 5), 10))
        args = {
            "prompt": prompt,
            "duration": dur,
            "aspect_ratio": "9:16",
        }

    print(f"[hf] submitting {kind} | app={app}", flush=True)
    result = client.subscribe(app, args)
    print("[hf] generation complete", flush=True)

    asset_url = _extract_url(result, kind)
    if not asset_url:
        raise RuntimeError(f"higgsfield: no output URL in result: {result}")

    print(f"[hf] downloading {kind} -> {out_path}", flush=True)
    _download(asset_url, out_path)
    size = os.path.getsize(out_path)
    print(f"[hf] done: {size:,} bytes", flush=True)


def build_renderer():
    """Return higgsfield_renderer callable when HF credentials are set, else None."""
    return higgsfield_renderer if _hf_key() else None
