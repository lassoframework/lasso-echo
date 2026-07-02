"""
Media hosting: publish local creatives to S3-compatible object storage so Instagram
(and the Slack card preview) have PUBLIC urls. Scale-hardened for 200+ clients.

OFF BY DEFAULT (`config.hosting_enabled()`). Guarantees that mirror the rest of Echo:
  - Tenant isolation: every key is scoped to the slugified tenant, so one client's
    media can never collide with or overwrite another's.
  - Content-addressed + deduped: the key carries a sha1 of the file bytes, and we
    HEAD the object first — an identical file is never re-uploaded.
  - Resilient: uploads retry with capped exponential backoff.
  - No secrets: credentials are read lazily by env-var NAME, passed only to boto3,
    never logged, printed, or returned.

Publishing is unaffected: hosting a file never posts anything.
"""

import hashlib
import os
import re
import time

from . import config, ops_alerts


def _fail(message):
    """A hosting failure is never invisible: it always lands in the log, and with
    AGENT_OPS_ALERTS_ENABLED it also posts one ops alert. The safe fallback
    (caller receives None) is unchanged. Messages carry exception class + message
    only, never credentials; ops_alerts.scrub redacts secrets belt-and-suspenders."""
    line = ops_alerts.scrub(message)
    print(f"[media-host] {line}")
    ops_alerts.alert(line)


def _slugify_tenant(tenant):
    """Reduce a tenant id to [a-z0-9_-] so it is a safe, stable key segment."""
    s = re.sub(r"[^a-z0-9_-]+", "-", str(tenant).lower())
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    return s or "tenant"


def _sha1_16(local_path):
    """First 16 hex chars of the sha1 of the file's bytes (content address)."""
    h = hashlib.sha1()
    with open(local_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _build_key(local_path, tenant):
    """echo/<tenant-slug>/<sha1-16>/<filename> — tenant-scoped + content-addressed."""
    return f"echo/{_slugify_tenant(tenant)}/{_sha1_16(local_path)}/{os.path.basename(local_path)}"


def _public_url(key):
    return f"{config.S3_PUBLIC_BASE_URL.rstrip('/')}/{key}"


class _S3Client:
    """
    Thin wrapper over a boto3 S3 client. Built only with present credentials; the
    keys go into boto3 and are never logged, printed, or returned.
    """

    def __init__(self, s3, bucket):
        self._s3 = s3
        self._bucket = bucket

    def exists(self, key):
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def put(self, key, local_path):
        self._s3.upload_file(local_path, self._bucket, key)


def _default_client():
    """
    Build the real S3 client, but ONLY when hosting is on AND both credentials are
    present. Returns None otherwise so host_media() no-ops safely. boto3 is imported
    lazily and configured with botocore adaptive retries. Credentials never logged.
    """
    if not config.hosting_enabled():
        return None
    key_id = os.environ.get(config.S3_ACCESS_KEY_ID_ENV)
    secret = os.environ.get(config.S3_SECRET_ACCESS_KEY_ENV)
    if not key_id or not secret:
        return None
    import boto3  # lazy: flag-off / tests never need the SDK installed
    from botocore.config import Config as BotoConfig

    s3 = boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT or None,
        region_name=config.S3_REGION or None,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        config=BotoConfig(retries={"max_attempts": config.S3_MAX_RETRIES, "mode": "adaptive"}),
    )
    return _S3Client(s3, config.S3_BUCKET)


def host_media(local_path, tenant, client=None):
    """
    Upload one local creative and return its public url, or None when it must not /
    cannot run:
      - hosting flag OFF                              -> None (no client touched)
      - file missing                                 -> None
      - no S3_PUBLIC_BASE_URL configured             -> None
      - no client and no credentials available       -> None
      - all upload retries fail                       -> None

    Dedupe: if the content-addressed key already exists, skip the PUT and return the
    url. Retries the upload up to config.S3_MAX_RETRIES with capped backoff (1s,2s,4s).
    """
    if not config.hosting_enabled():
        return None
    if not local_path or not os.path.isfile(local_path):
        _fail(f"media hosting failed: creative file missing: "
              f"{os.path.basename(local_path) if local_path else '(no path)'}")
        return None
    if not config.S3_PUBLIC_BASE_URL:
        _fail("media hosting failed: hosting is armed but AGENT_S3_PUBLIC_BASE_URL is not set.")
        return None
    client = client or _default_client()
    if client is None:
        _fail("media hosting failed: hosting is armed but no storage client could be "
              "built (credentials missing).")
        return None

    key = _build_key(local_path, tenant)
    url = _public_url(key)

    # Dedupe: an identical file (same tenant, same bytes) is already hosted.
    try:
        if client.exists(key):
            return url
    except Exception:
        pass  # a flaky HEAD never blocks the upload attempt

    max_retries = max(1, int(config.S3_MAX_RETRIES))
    for attempt in range(max_retries):
        try:
            client.put(key, local_path)
            return url
        except Exception as e:
            if attempt == max_retries - 1:
                # Fallback behavior unchanged (return None), but the exception is
                # captured and routed to the log + the ops alert path, not swallowed.
                _fail(f"media hosting failed for {os.path.basename(local_path)} after "
                      f"{max_retries} attempt(s): {type(e).__name__}: {e}")
                return None
            time.sleep(min(2 ** attempt, 4))  # 1s, 2s, 4s capped
    return None


def host_many(local_paths, tenant, client=None):
    """
    Upload several local creatives (e.g. carousel slides) and return their public
    urls IN THE SAME ORDER. All-or-nothing: if any one fails, return None so the
    caller keeps its existing slide_urls rather than a misaligned partial set.
    """
    if not config.hosting_enabled():
        return None
    if not local_paths:
        return None
    client = client or _default_client()
    if client is None:
        _fail("media hosting failed: hosting is armed but no storage client could be "
              "built (credentials missing).")
        return None

    urls = []
    for path in local_paths:
        url = host_media(path, tenant, client=client)
        if url is None:
            return None
        urls.append(url)
    return urls
