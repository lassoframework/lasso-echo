"""Bounded read of the real tenant thumbnail route. Tokens never leave memory."""
from io import BytesIO
import re
import time

_ORIGIN = 'https://echo-intake-web-production.up.railway.app'
_MAX_BYTES = 1024 * 1024


def probe_job_status(gym, request_id):
    """Worker-owned readback; intake-web's separate volume cannot substitute it."""
    try:
        from . import db, auto_reels
        if not db.kv_is_durable():
            return None
        snapshot = auto_reels.gym_status(gym)
        if snapshot.get('ok') is not True or snapshot.get('gym') != gym:
            return None
        jobs = [j for j in snapshot.get('jobs', []) if j.get('request_id') == request_id]
        return jobs[0] if len(jobs) == 1 else None
    except Exception:
        return None


def probe_approved_cta(gym, ask):
    """Read the current approved voice, never infer an offer from ticket text."""
    try:
        from .auto_reel_copy import _resolve
        account, voice = _resolve(gym)
        return (account is not None and voice is not None and voice.auto_drafted is False
                and ask in voice.ctas and ask in voice.raw)
    except Exception:
        return None


def probe_thumbnail(gym, asset_id, *, http=None, token_resolver=None):
    """True=image verified, False=not an image, None=read unavailable.

    A fixed service origin and ID grammar prevent caller-controlled URL fetches.
    Never fall back to downloading full source footage or return the token URL.
    """
    if not isinstance(asset_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', asset_id):
        return False
    try:
        from .intake_web import _current_token_for
        from PIL import Image
        import requests
        token = (token_resolver or _current_token_for)(gym)
        if not token:
            return None
        deadline = time.monotonic() + 10
        with (http or requests).get(f'{_ORIGIN}/portal/{token}/media/thumb/{asset_id}',
                                   stream=True, timeout=(5, 5), allow_redirects=False) as response:
            if response.status_code != 200:
                return False if response.status_code in (403, 404) else None
            if not response.headers.get('Content-Type', '').lower().startswith('image/'):
                return False
            if int(response.headers.get('Content-Length') or 0) > _MAX_BYTES:
                return False
            data = bytearray()
            for chunk in response.iter_content(16384):
                if time.monotonic() > deadline:
                    return None
                data.extend(chunk)
                if len(data) > _MAX_BYTES:
                    return False
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > 16_000_000:
                    return False
                image.verify()
        return True
    except Exception:
        return None
