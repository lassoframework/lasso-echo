import hashlib
import io
import json
import os
import socket
import stat

import pytest

from tools.audit_published_visual_bytes import (
    MAX_BYTES,
    _fetch_and_hash,
    _resolve_public_ip,
    _validate_url,
    audit,
    load_rows,
    main,
    write_receipt,
)

ORIGIN = "https://cdn.example.com"
URL = "https://cdn.example.com/media/img1.jpg?x=1"
BODY = b"hello delivered bytes" * 100
PUBLIC_IP = "93.184.216.34"


def row(**kw):
    base = {"id": "r1", "gym_id": "g1", "image_url": URL, "status": "published", "post_date": "2026-10-01"}
    base.update(kw)
    return base


def public_resolver(host, port, type=None):  # noqa: A002
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443))]


class FakeResp:
    def __init__(self, body, status=200):
        self._stream = io.BytesIO(body)
        self.status = status

    def read(self, n=-1):
        return self._stream.read(n)


class FakeConn:
    """Stands in for the pinned HTTPS connection; records dial target."""

    def __init__(self, mapping, calls, pinned_ip, host):
        self._mapping = mapping
        self._calls = calls
        self.pinned_ip = pinned_ip
        self.host = host
        self.requests = []
        self.closed = False

    def request(self, method, target, headers=None):
        self.requests.append((method, target, headers))

    def getresponse(self):
        entry = self._mapping[URL_KEY]
        if isinstance(entry, Exception):
            raise entry
        self._calls.append(self)
        if isinstance(entry, tuple):
            return FakeResp(entry[0], status=entry[1])
        return FakeResp(entry)

    def close(self):
        self.closed = True


URL_KEY = "default"


def make_factory(mapping, calls):
    def factory(pinned_ip, host, timeout):
        return FakeConn(mapping, calls, pinned_ip, host)

    return factory


def test_validate_url_accepts_allowed_origin():
    ok, reason = _validate_url(URL, ORIGIN)
    assert ok and reason is None


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://cdn.example.com/a.jpg", "not_https"),
        ("https://evil.example.com/a.jpg", "host_not_allowed"),
        ("https://cdn.example.com", "missing_path"),
        ("", "missing_image_url"),
        ("https://user:pw@cdn.example.com/a.jpg", "userinfo_not_allowed"),
        ("https://cdn.example.com:8443/a.jpg", "non_default_port"),
        ("https://cdn.example.com:443/a.jpg", None),
    ],
)
def test_validate_url_rejects(url, reason):
    ok, got = _validate_url(url, ORIGIN)
    if reason is None:
        assert ok and got is None
    else:
        assert not ok
        assert got == reason


@pytest.mark.parametrize(
    "origin",
    [
        "https://cdn.example.com:8443",
        "https://cdn.example.com/some/path",
        "http://cdn.example.com",
        "https://user@cdn.example.com",
        "https://cdn.example.com?x=1",
    ],
)
def test_validate_url_rejects_non_bare_allowed_origin(origin):
    ok, got = _validate_url(URL, origin)
    assert not ok
    assert got == "invalid_allowed_origin"


# --- resolution / SSRF guards -------------------------------------------------


def test_resolve_public_ip_accepts_public():
    ip, reason = _resolve_public_ip("cdn.example.com", resolver=public_resolver)
    assert ip == PUBLIC_IP and reason is None


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.1.1", "::1"])
def test_resolve_public_ip_rejects_private_loopback(addr):
    def resolver(host, port, type=None):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 443))]

    ip, reason = _resolve_public_ip("cdn.example.com", resolver=resolver)
    assert ip is None and reason == "non_public_resolution"


def test_resolve_public_ip_rejects_if_any_address_is_private():
    def mixed(host, port, type=None):  # noqa: A002
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443)),
        ]

    ip, reason = _resolve_public_ip("cdn.example.com", resolver=mixed)
    assert ip is None and reason == "non_public_resolution"


def test_fetch_refuses_private_resolution_without_connecting():
    def loopback(host, port, type=None):  # noqa: A002
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    calls = []
    out = _fetch_and_hash(
        URL, ORIGIN, resolver=loopback, connection_factory=make_factory({URL_KEY: BODY}, calls)
    )
    assert out == {"status": "failed", "reason": "non_public_resolution"}
    assert calls == []


def test_fetch_dials_pinned_ip_but_keeps_original_host_header():
    calls = []
    resolve_calls = []

    def resolver(host, port, type=None):  # noqa: A002
        resolve_calls.append(host)
        return public_resolver(host, port, type=type)

    out = _fetch_and_hash(
        URL, ORIGIN, resolver=resolver, connection_factory=make_factory({URL_KEY: BODY}, calls)
    )
    assert out["status"] == "fetched"
    conn = calls[0]
    assert conn.pinned_ip == PUBLIC_IP  # socket dials the pinned public IP
    assert conn.host == "cdn.example.com"  # TLS SNI / cert verify host
    method, target, headers = conn.requests[0]
    assert method == "GET" and target == "/media/img1.jpg?x=1"
    assert headers["Host"] == "cdn.example.com"


def test_fetch_and_hash_computes_exact_digests():
    calls = []
    out = _fetch_and_hash(
        URL, ORIGIN, resolver=public_resolver, connection_factory=make_factory({URL_KEY: BODY}, calls)
    )
    assert out["status"] == "fetched"
    assert out["length"] == len(BODY)
    assert out["md5"] == hashlib.md5(BODY).hexdigest()
    assert out["sha256"] == hashlib.sha256(BODY).hexdigest()


def test_fetch_fails_closed_on_transport_error():
    calls = []
    out = _fetch_and_hash(
        URL, ORIGIN, resolver=public_resolver,
        connection_factory=make_factory({URL_KEY: RuntimeError("boom")}, calls),
    )
    assert out["status"] == "failed"
    assert out["reason"].startswith("fetch_error:")


@pytest.mark.parametrize("status", [301, 302, 307, 404, 500])
def test_fetch_refuses_redirect_and_non_200(status):
    calls = []
    out = _fetch_and_hash(
        URL, ORIGIN, resolver=public_resolver,
        connection_factory=make_factory({URL_KEY: (BODY, status)}, calls),
    )
    assert out == {"status": "failed", "reason": "redirect_or_non_200_refused"}


# --- audit() behavior ----------------------------------------------------------


def test_audit_dedupes_url_reads_and_marks_no_fetch_default():
    rows = [row(id="a"), row(id="b"), row(id="c", image_url=URL + "&y=2")]
    receipt, counts = audit(rows, ORIGIN, fetch=False)
    assert counts["unique_urls"] == 2
    assert counts["urls_fetched"] == 0
    assert receipt["a"]["status"] == "not_fetched"
    assert receipt["a"]["url"] == URL
    assert receipt["b"]["url"] == URL


def test_audit_fetches_each_unique_url_once():
    calls = []
    rows = [row(id="a"), row(id="b"), row(id="c", image_url="https://cdn.example.com/media/img2.jpg")]
    receipt, counts = audit(
        rows, ORIGIN, fetch=True, resolver=public_resolver,
        connection_factory=make_factory({URL_KEY: BODY}, calls),
    )
    assert len(calls) == 2
    assert counts["urls_fetched"] == 2
    assert receipt["a"]["sha256"] == receipt["b"]["sha256"]


def test_audit_nonpublished_row_triggers_no_network():
    def fail_resolver(host, port, type=None):  # noqa: A002
        raise AssertionError("resolver must not be called for non-published rows")

    def fail_factory(pinned_ip, host, timeout):
        raise AssertionError("no connection allowed for non-published rows")

    rows = [row(id="sched", status="scheduled"), row(id="del", status="deleted")]
    receipt, counts = audit(
        rows, ORIGIN, fetch=True, resolver=fail_resolver, connection_factory=fail_factory
    )
    assert receipt["sched"] == {"status": "skipped", "reason": "status_not_published"}
    assert receipt["del"] == {"status": "skipped", "reason": "status_not_published"}
    assert counts["rows_skipped_nonpublished"] == 2
    assert counts["urls_fetched"] == 0
    assert counts["unique_urls"] == 0


def test_audit_missing_fields_fail_closed():
    receipt, counts = audit([{"id": "x"}, "not-a-dict"], ORIGIN, fetch=False)
    assert receipt["x"]["reason"] == "missing_required_field"
    assert receipt["__row_index_1"]["reason"] == "missing_required_field"
    assert counts["rows_failed"] == 2


# --- receipt file safety -------------------------------------------------------


def test_write_receipt_mode_0600(tmp_path):
    out = tmp_path / "receipt.json"
    write_receipt({"r1": {"status": "not_fetched"}}, str(out))
    mode = stat.S_IMODE(os.stat(out).st_mode)
    assert mode == 0o600
    payload = json.loads(out.read_text())
    assert payload["audit"] == "published_visual_bytes"


def test_write_receipt_refuses_existing_private_file(tmp_path):
    out = tmp_path / "receipt.json"
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    with pytest.raises(FileExistsError):
        write_receipt({"r1": {"status": "not_fetched"}}, str(out))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_write_receipt_refuses_existing_permissive_file(tmp_path):
    out = tmp_path / "receipt.json"
    out.write_text("{}")
    os.chmod(str(out), 0o644)
    with pytest.raises(FileExistsError):
        write_receipt({"r1": {"status": "not_fetched"}}, str(out))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o644  # untouched


def test_write_receipt_refuses_symlink_without_touching_target(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("untouched")
    out = tmp_path / "receipt.json"
    out.symlink_to(target)
    with pytest.raises(FileExistsError):
        write_receipt({"r1": {"status": "not_fetched"}}, str(out))
    assert target.read_text() == "untouched"


def test_main_end_to_end_no_network_and_coverage_stdout(tmp_path, capsys):
    src = tmp_path / "rows.json"
    src.write_text(json.dumps([
        row(id="a"),
        row(id="bad", image_url="http://nope.example.com/x.jpg"),
        row(id="np", status="scheduled"),
    ]))
    out = tmp_path / "receipt.json"
    rc = main(["--input", str(src), "--allowed-origin", ORIGIN, "--receipt", str(out)])
    stdout = capsys.readouterr().out
    assert rc == 1
    assert "rows_total=3" in stdout and "rows_failed=1" in stdout
    assert "rows_skipped_nonpublished=1" in stdout
    assert URL not in stdout  # no raw URLs on stdout
    payload = json.loads(out.read_text())
    assert payload["rows"]["a"]["status"] == "not_fetched"
    assert payload["rows"]["bad"]["reason"] == "not_https"
    assert payload["rows"]["np"]["reason"] == "status_not_published"


def test_load_rows_requires_array(tmp_path):
    src = tmp_path / "bad.json"
    src.write_text("{}")
    with pytest.raises(ValueError):
        load_rows(str(src))


def test_cap_constant_is_128_mib():
    assert MAX_BYTES == 128 * 1024 * 1024
