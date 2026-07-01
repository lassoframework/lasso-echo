"""
Media hosting tests: OFF-by-default gate, tenant isolation, content-address dedupe,
retry-then-success, retry-exhausted, order preservation, and the missing-file /
no-base-url no-ops. No network and no boto3 — a fake client only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, media_host  # noqa: E402


class FakeClient:
    """Records calls. `exists_ret` controls dedupe; `fail_times` fails the first N puts."""

    def __init__(self, exists_ret=False, fail_times=0):
        self.exists_ret = exists_ret
        self.fail_times = fail_times
        self.exists_calls = []
        self.put_calls = []

    def exists(self, key):
        self.exists_calls.append(key)
        return self.exists_ret

    def put(self, key, local_path):
        self.put_calls.append(key)
        if len(self.put_calls) <= self.fail_times:
            raise RuntimeError("transient upload error")


def _arm(monkeypatch, base="https://cdn.echo.test", retries=3):
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", base)
    monkeypatch.setattr(config, "S3_MAX_RETRIES", retries)
    monkeypatch.setattr(media_host.time, "sleep", lambda *_: None)  # no real waiting


def _file(tmp_path, name="creative.png", data=b"IMG-BYTES"):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ---- 1. flag OFF -> no client touched ---------------------------------------
def test_flag_off_no_upload(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)  # OFF
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    fc = FakeClient()
    assert media_host.host_media(_file(tmp_path), "gym-a", client=fc) is None
    assert fc.put_calls == [] and fc.exists_calls == []


# ---- 2. tenant isolation ----------------------------------------------------
def test_tenant_isolation(monkeypatch, tmp_path):
    _arm(monkeypatch)
    f = _file(tmp_path)
    url_a = media_host.host_media(f, "Gym A!!", client=FakeClient())
    url_b = media_host.host_media(f, "gym_b", client=FakeClient())
    assert "/echo/gym-a/" in url_a       # slugified to [a-z0-9_-]
    assert "/echo/gym_b/" in url_b
    assert url_a != url_b                 # same bytes, different tenant -> different key


# ---- 3. dedupe: an existing object skips the PUT ----------------------------
def test_dedupe_skips_upload(monkeypatch, tmp_path):
    _arm(monkeypatch)
    fc = FakeClient(exists_ret=True)
    url = media_host.host_media(_file(tmp_path), "gym-a", client=fc)
    assert url is not None
    assert fc.put_calls == []            # never uploaded
    assert len(fc.exists_calls) == 1


# ---- 4. retry then success --------------------------------------------------
def test_retry_then_success(monkeypatch, tmp_path):
    _arm(monkeypatch, retries=3)
    fc = FakeClient(fail_times=1)        # first put fails, second succeeds
    url = media_host.host_media(_file(tmp_path), "gym-a", client=fc)
    assert url is not None
    assert len(fc.put_calls) == 2


# ---- 5. retry exhausted -> None ---------------------------------------------
def test_retry_exhausted(monkeypatch, tmp_path):
    _arm(monkeypatch, retries=3)
    fc = FakeClient(fail_times=99)       # always fails
    url = media_host.host_media(_file(tmp_path), "gym-a", client=fc)
    assert url is None
    assert len(fc.put_calls) == 3        # exactly S3_MAX_RETRIES attempts


# ---- 6. host_many preserves order -------------------------------------------
def test_host_many_preserves_order(monkeypatch, tmp_path):
    _arm(monkeypatch)
    paths = [_file(tmp_path, f"slide_{i}.png", data=f"S{i}".encode()) for i in range(3)]
    urls = media_host.host_many(paths, "gym-a", client=FakeClient())
    assert len(urls) == 3
    for path, url in zip(paths, urls):
        assert url.endswith(os.path.basename(path))   # same order, filename preserved


def test_host_many_all_or_nothing(monkeypatch, tmp_path):
    _arm(monkeypatch)
    paths = [_file(tmp_path, f"s{i}.png", data=f"S{i}".encode()) for i in range(3)]
    # a client whose put always fails -> the whole batch returns None (no partial set)
    assert media_host.host_many(paths, "gym-a", client=FakeClient(fail_times=99)) is None


# ---- 7. missing file / no base url -> None ----------------------------------
def test_missing_file_returns_none(monkeypatch, tmp_path):
    _arm(monkeypatch)
    assert media_host.host_media(str(tmp_path / "nope.png"), "gym-a", client=FakeClient()) is None


def test_no_base_url_returns_none(monkeypatch, tmp_path):
    _arm(monkeypatch, base="")           # no public base configured
    fc = FakeClient()
    assert media_host.host_media(_file(tmp_path), "gym-a", client=fc) is None
    assert fc.put_calls == []
