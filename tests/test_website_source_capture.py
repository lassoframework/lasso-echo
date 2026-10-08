"""Synthetic offline tests for agent/website_source_capture.

No live network, no DNS, no sleeping: transport, resolver, robots, sleep,
clock and wall-time are all injected fakes. The default pinned transport's
wiring is verified with a mocked urllib3 pool — no sockets are opened.
"""

import hashlib
from datetime import datetime, timezone

import pytest

import agent.website_source_capture as wsc
from agent.website_source_capture import (
    CaptureError,
    CapturedSource,
    PortalDomainEntry,
    PortalDomains,
    RawResponse,
    WebsiteSourceCapture,
    MAX_BYTES,
    MAX_ROBOTS_BYTES,
)

GYM = "gym-uuid-1"
KEY = "crossfitexample123456"
HOST = "gym-example.com"
PAGE = f"https://{HOST}/about"
PUBLIC_IP = "93.184.216.34"
FIXED_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def entry(**kw):
    base = dict(gym_id=GYM, echo_account_key=KEY, host=HOST,
                mapping_revision="rev-2026-10-07",
                mapping_evidence={"verified_by": "portal", "method": "dns"})
    base.update(kw)
    return PortalDomainEntry(**base)


def mapping(*entries):
    return PortalDomains(entries or (entry(),))


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class FakeResp:
    """RawResponse stand-in tracking close-after-read."""

    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self.body = list(chunks)
        self.closed = False

    def close(self):
        self.closed = True


def make_capture(routes, *, ips=None, robots=None, m=None, clock=None):
    """routes: {url: FakeResp or Exception}. ips: {host: [addrs]}.
    robots: {host: (status, bytes)} — default allows all.
    Returns (cap, calls, clock); calls are (url, pinned_ip) tuples."""
    calls = []
    ips = ips or {}
    robots = robots or {}
    clock = clock or FakeClock()

    def transport(url, pinned_ip, headers, timeout):
        calls.append((url, pinned_ip))
        r = routes[url]
        if isinstance(r, Exception):
            raise r
        return r

    def resolver(host):
        return ips.get(host, [PUBLIC_IP])

    def robots_fetch(url):
        host = url.split("//", 1)[1].split("/", 1)[0]
        return robots.get(host, (200, b"User-agent: *\nAllow: /\n"))

    cap = WebsiteSourceCapture(
        m or mapping(),
        transport=transport,
        resolver=resolver,
        robots_fetch=robots_fetch,
        sleep=clock.sleep,
        clock=clock.clock,
        now=lambda: FIXED_NOW,
        min_host_delay=1.0,
    )
    return cap, calls, clock


def body(data: bytes, status=200, headers=None):
    return FakeResp(status=status, headers=headers,
                    chunks=[data[i:i + 100] for i in range(0, len(data), 100)] or [b""])


def redirect(location, status=302):
    return FakeResp(status=status, headers={"Location": location}, chunks=[])


# --- happy path -----------------------------------------------------------

def test_capture_records_raw_bytes_final_url_entry_and_timestamp():
    raw = b"\x89PNG\r\n\x1a\n<html>bytes \xff\xfe</html>"
    cap, calls, _ = make_capture({PAGE: body(raw)})
    got = cap.capture(PAGE)
    assert isinstance(got, CapturedSource)
    assert got.raw_bytes == raw                       # exact bytes, never decoded
    assert got.bytes_sha256 == hashlib.sha256(raw).hexdigest()
    assert got.source_url == PAGE
    assert got.requested_url == PAGE
    assert got.gym_id == GYM
    assert got.echo_account_key == KEY
    assert got.source_kind == "website"
    assert got.mapping_revision == "rev-2026-10-07"
    assert got.mapping_evidence == {"verified_by": "portal", "method": "dns"}
    assert got.fetched_at == FIXED_NOW.isoformat()
    assert calls == [(PAGE, PUBLIC_IP)]  # robots fetch is injected, not via transport


def test_capture_follows_authorized_redirect_and_records_final_url():
    final = f"https://{HOST}/about-us"
    cap, _, _ = make_capture({PAGE: redirect(final), final: body(b"hi")})
    got = cap.capture(PAGE)
    assert got.raw_bytes == b"hi"
    assert got.source_url == final
    assert got.requested_url == PAGE


def test_capture_website_asset_kind():
    css = f"https://{HOST}/static/site.css"
    m = mapping(entry(host=HOST, source_kind="website_asset",
                      path_prefixes=("/static/",)))
    cap, _, _ = make_capture({css: body(b"body { color: #0F1B33; }")}, m=m)
    got = cap.capture(css)
    assert got.source_kind == "website_asset"
    assert b"#0F1B33" in got.raw_bytes


# --- mapping is the only source of domains --------------------------------

def test_unmapped_url_is_refused_before_network():
    cap, calls, _ = make_capture({})
    with pytest.raises(CaptureError, match="no portal_domains entry"):
        cap.capture("https://unmapped-example.net/")
    assert calls == []


def test_attribution_follows_the_authorizing_entry():
    """The mapping is authoritative: whichever entry authorizes the URL owns
    the capture, including its gym and Echo account key."""
    m = mapping(entry(gym_id="other-gym", echo_account_key="otherkey12345"))
    cap, _, _ = make_capture({PAGE: body(b"x")}, m=m)
    got = cap.capture(PAGE)
    assert got.gym_id == "other-gym"
    assert got.echo_account_key == "otherkey12345"


def test_path_prefix_scope_is_enforced():
    m = mapping(entry(path_prefixes=("/allowed/",)))
    cap, calls, _ = make_capture({}, m=m)
    with pytest.raises(CaptureError):
        cap.capture(f"https://{HOST}/other/page")
    assert calls == []


def test_non_https_refused_even_when_host_mapped():
    cap, calls, _ = make_capture({})
    with pytest.raises(CaptureError, match="HTTPS"):
        cap.capture(f"http://{HOST}/about")
    assert calls == []


# --- SSRF guard + real IP pinning ------------------------------------------

@pytest.mark.parametrize("ip", ["10.0.0.5", "192.168.1.10", "127.0.0.1",
                                "169.254.1.1", "0.0.0.0", "::1",
                                "fd00::1", "224.0.0.1"])
def test_private_or_non_global_dns_is_refused(ip):
    cap, calls, _ = make_capture({}, ips={HOST: [PUBLIC_IP, ip]})
    with pytest.raises(CaptureError, match="non-public"):
        cap.capture(PAGE)
    assert calls == []


def test_dns_failure_fails_closed():
    def resolver(host):
        raise OSError("nxdomain")
    cap, calls, _ = make_capture({})
    cap._resolver = resolver
    with pytest.raises(CaptureError, match="DNS"):
        cap.capture(PAGE)
    assert calls == []


def test_connection_is_pinned_to_the_vetted_ip():
    """The transport receives the exact vetted resolver answer — the
    connection does not re-resolve."""
    ip2 = "93.184.216.35"
    cap, calls, _ = make_capture({PAGE: body(b"x")}, ips={HOST: [ip2, PUBLIC_IP]})
    cap.capture(PAGE)
    assert calls == [(PAGE, ip2)]  # first vetted answer is the pinned IP


def test_dns_rebinding_between_hops_fails_closed():
    """Every hop re-resolves and re-vets: an answer that flips to a private
    IP after the first fetch is caught before any connection is attempted."""
    answers = {HOST: [[PUBLIC_IP], ["10.9.9.9"]]}

    def resolver(host):
        return answers[host].pop(0) if answers[host] else [PUBLIC_IP]

    page2 = f"https://{HOST}/two"
    calls = []

    def transport(url, pinned_ip, headers, timeout):
        calls.append((url, pinned_ip))
        return body(b"x")

    cap = WebsiteSourceCapture(
        mapping(), transport=transport, resolver=resolver,
        robots_fetch=lambda url: (200, b"User-agent: *\nAllow: /\n"),
        sleep=lambda s: None, now=lambda: FIXED_NOW)
    cap.capture(PAGE)                     # first hop pinned to PUBLIC_IP
    with pytest.raises(CaptureError, match="non-public"):
        cap.capture(page2)                # rebound answer refused pre-connect
    assert calls == [(PAGE, PUBLIC_IP)]   # no connection to the rebound IP


def test_redirect_to_private_ip_host_is_refused():
    evil = "https://cdn-evil.example.net/x"
    cap, calls, _ = make_capture(
        {PAGE: redirect(evil)},
        ips={HOST: [PUBLIC_IP], "cdn-evil.example.net": ["10.1.2.3"]},
        m=mapping(entry(), entry(host="cdn-evil.example.net")),
    )
    with pytest.raises(CaptureError, match="non-public"):
        cap.capture(PAGE)
    assert not any(url == evil for url, _ in calls)  # never connected


def test_each_redirect_hop_is_re_pinned():
    final = f"https://{HOST}/about-us"
    hop_ips = [[PUBLIC_IP], ["93.184.216.44"]]

    def resolver(host):
        return hop_ips.pop(0)

    calls = []

    def transport(url, pinned_ip, headers, timeout):
        calls.append((url, pinned_ip))
        return redirect(final) if url == PAGE else body(b"ok")

    cap = WebsiteSourceCapture(
        mapping(), transport=transport, resolver=resolver,
        robots_fetch=lambda url: (200, b""),
        sleep=lambda s: None, now=lambda: FIXED_NOW)
    got = cap.capture(PAGE)
    assert got.raw_bytes == b"ok"
    assert calls == [(PAGE, PUBLIC_IP), (final, "93.184.216.44")]


# --- close-after-read -------------------------------------------------------

def test_response_closed_after_successful_read():
    resp = body(b"hello")
    cap, _, _ = make_capture({PAGE: resp})
    assert cap.capture(PAGE).raw_bytes == b"hello"
    assert resp.closed


def test_response_closed_after_failed_read():
    resp = body(b"a" * (MAX_BYTES + 10))
    cap, _, _ = make_capture({PAGE: resp})
    with pytest.raises(CaptureError, match="byte cap"):
        cap.capture(PAGE)
    assert resp.closed


def test_redirect_response_closed_after_drain():
    final = f"https://{HOST}/about-us"
    red = redirect(final)
    red.body = [b"discarded redirect body"]
    cap, _, _ = make_capture({PAGE: red, final: body(b"hi")})
    cap.capture(PAGE)
    assert red.closed


# --- redirects --------------------------------------------------------------

def test_redirect_limit_fails_closed():
    routes = {}
    urls = [f"https://{HOST}/r{i}" for i in range(8)]
    for i, u in enumerate(urls):
        routes[u] = redirect(urls[i + 1]) if i + 1 < len(urls) else body(b"x")
    cap, _, _ = make_capture(routes)
    with pytest.raises(CaptureError, match="redirect limit"):
        cap.capture(urls[0])


def test_redirect_to_non_https_refused():
    cap, _, _ = make_capture({PAGE: redirect(f"http://{HOST}/about")})
    with pytest.raises(CaptureError, match="non-HTTPS"):
        cap.capture(PAGE)


def test_redirect_to_unmapped_host_refused():
    target = "https://other-unmapped.example.org/x"
    cap, calls, _ = make_capture({PAGE: redirect(target)})
    with pytest.raises(CaptureError, match="not authorized"):
        cap.capture(PAGE)
    assert not any(url == target for url, _ in calls)


def test_cross_gym_redirect_refused():
    other = "https://gym2-example.com/x"
    m = mapping(entry(),
                entry(gym_id="gym-uuid-2", echo_account_key="gym2key0000000",
                      host="gym2-example.com"))
    cap, calls, _ = make_capture({PAGE: redirect(other)}, m=m)
    with pytest.raises(CaptureError, match="not authorized"):
        cap.capture(PAGE)
    assert not any(url == other for url, _ in calls)


# --- size caps and raw bytes -------------------------------------------------

def test_oversize_body_fails_closed_no_partial_capture():
    big = body(b"a" * (MAX_BYTES + 10))
    cap, _, _ = make_capture({PAGE: big})
    with pytest.raises(CaptureError, match="byte cap"):
        cap.capture(PAGE)


def test_exact_cap_is_accepted():
    cap, _, _ = make_capture({PAGE: body(b"a" * MAX_BYTES)})
    got = cap.capture(PAGE)
    assert len(got.raw_bytes) == MAX_BYTES


def test_empty_body_fails_closed():
    cap, _, _ = make_capture({PAGE: FakeResp(status=200, chunks=[])})
    with pytest.raises(CaptureError, match="empty"):
        cap.capture(PAGE)


def test_decoded_text_transport_fails_closed():
    resp = FakeResp(status=200, chunks=["decoded text, not bytes"])
    cap, _, _ = make_capture({PAGE: resp})
    with pytest.raises(CaptureError, match="raw bytes"):
        cap.capture(PAGE)


def test_http_error_status_fails_closed():
    cap, _, _ = make_capture({PAGE: body(b"nope", status=500)})
    with pytest.raises(CaptureError, match="HTTP 500"):
        cap.capture(PAGE)


def test_transport_exception_fails_closed():
    cap, _, _ = make_capture({PAGE: ConnectionError("boom")})
    with pytest.raises(CaptureError, match="failed"):
        cap.capture(PAGE)


# --- robots -----------------------------------------------------------------

def test_robots_disallow_blocks_fetch():
    cap, calls, _ = make_capture(
        {PAGE: body(b"x")},
        robots={HOST: (200, b"User-agent: *\nDisallow: /\n")})
    with pytest.raises(CaptureError, match="robots"):
        cap.capture(PAGE)
    assert not any(url == PAGE for url, _ in calls)


def test_robots_path_scoped_disallow():
    cap, calls, _ = make_capture(
        {PAGE: body(b"x")},
        robots={HOST: (200, b"User-agent: *\nDisallow: /about\n")})
    with pytest.raises(CaptureError, match="robots"):
        cap.capture(PAGE)
    assert not any(url == PAGE for url, _ in calls)


@pytest.mark.parametrize("robots_result", [(None, None), (503, b"")])
def test_unreadable_robots_fails_closed(robots_result):
    def robots_fetch(url):
        if robots_result[0] is None:
            raise OSError("timeout")
        return robots_result
    cap = WebsiteSourceCapture(
        mapping(),
        transport=lambda url, pinned, h, t: body(b"x"),
        resolver=lambda host: [PUBLIC_IP],
        robots_fetch=robots_fetch,
        sleep=lambda s: None,
        now=lambda: FIXED_NOW,
    )
    with pytest.raises(CaptureError, match="robots"):
        cap.capture(PAGE)


def test_default_robots_fetch_is_pinned_and_bounded():
    """With no injected robots_fetch, robots.txt goes through the same
    pinned transport (vetting + pinned IP) and is capped."""
    calls = []
    big_robots = FakeResp(status=200, chunks=[b"a" * (MAX_ROBOTS_BYTES + 1)])

    def transport(url, pinned_ip, headers, timeout):
        calls.append((url, pinned_ip))
        return big_robots

    cap = WebsiteSourceCapture(
        mapping(), transport=transport, resolver=lambda host: [PUBLIC_IP],
        sleep=lambda s: None, now=lambda: FIXED_NOW)
    with pytest.raises(CaptureError, match="robots"):
        cap.capture(PAGE)   # oversize robots -> gate denies -> fail closed
    robots_url = f"https://{HOST}/robots.txt"
    assert calls == [(robots_url, PUBLIC_IP)]  # pinned fetch, no page fetch
    assert big_robots.closed                   # closed after the capped read


# --- the default pinned transport itself (mocked urllib3, no sockets) ------

def test_default_transport_pins_ip_and_verifies_original_hostname(monkeypatch):
    """Wiring proof for the real fix path: the pool connects to the pinned
    IP, TLS SNI + cert verification use the ORIGINAL hostname, the body is
    streamed undecoded, and close happens only after a full read."""
    constructed = {}

    class FakeHTTPResp:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "text/html"}
            self.closed = False
            self._chunks = [b"<html>raw ", b"bytes</html>"]

        def stream(self, n):
            assert n == 65536
            yield from self._chunks

        def close(self):
            self.closed = True

    class FakePool:
        def __init__(self, host, **kw):
            constructed["host"] = host
            constructed["kw"] = kw
            self.closed = False
            self.resp = FakeHTTPResp()

        def request(self, method, path, **kw):
            constructed["request"] = (method, path, kw)
            return self.resp

        def close(self):
            self.closed = True

    pools = []

    def pool_factory(host, **kw):
        pool = FakePool(host, **kw)
        pools.append(pool)
        return pool

    monkeypatch.setattr(wsc.urllib3, "HTTPSConnectionPool", pool_factory)
    resp = wsc._default_pinned_transport(
        PAGE, "93.184.216.34", {"User-Agent": "test"}, 15.0)

    assert constructed["host"] == "93.184.216.34"      # TCP to the pinned IP
    kw = constructed["kw"]
    assert kw["server_hostname"] == HOST               # SNI = original host
    assert kw["assert_hostname"] == HOST               # cert hostname check
    assert kw["cert_reqs"] == "CERT_REQUIRED"
    assert "cacert" in kw["ca_certs"] or kw["ca_certs"].endswith(".pem")
    method, path, rkw = constructed["request"]
    assert method == "GET" and path == "/about"
    assert rkw["headers"]["Host"] == HOST
    assert rkw["preload_content"] is False             # streamed, not buffered
    assert rkw["decode_content"] is False              # original entity bytes

    raw = b"".join(resp.body)
    assert raw == b"<html>raw bytes</html>"            # bytes preserved
    assert not pools[0].resp.closed                    # open during the read
    resp.close()
    assert pools[0].resp.closed and pools[0].closed    # closed after the read


def test_default_transport_closes_pool_on_request_failure(monkeypatch):
    class BoomPool:
        def __init__(self, host, **kw):
            self.closed = False

        def request(self, *a, **kw):
            raise OSError("connect failed")

        def close(self):
            self.closed = True

    pool = BoomPool("x")
    monkeypatch.setattr(wsc.urllib3, "HTTPSConnectionPool",
                        lambda host, **kw: pool)
    with pytest.raises(OSError):
        wsc._default_pinned_transport(PAGE, PUBLIC_IP, {}, 1.0)
    assert pool.closed


# --- rate limiting ------------------------------------------------------------

def test_rate_limit_sleeps_between_same_host_fetches():
    page2 = f"https://{HOST}/contact"
    clock = FakeClock()
    cap, _, clock = make_capture({PAGE: body(b"1"), page2: body(b"2")},
                                 clock=clock)
    cap.capture(PAGE)
    cap.capture(page2)
    assert clock.slept == [pytest.approx(1.0)]


def test_robots_fetch_does_not_consume_rate_slot_but_page_fetches_do():
    clock = FakeClock()
    cap, _, clock = make_capture({PAGE: body(b"1")}, clock=clock)
    cap.capture(PAGE)
    # second capture of same URL: robots cached, one page fetch -> owes delay
    cap.capture(PAGE)
    assert len(clock.slept) == 1


# --- port and userinfo rails --------------------------------------------------

def test_explicit_443_port_allowed():
    url = f"https://{HOST}:443/about"
    cap, calls, _ = make_capture({url: body(b"ok")})
    assert cap.capture(url).raw_bytes == b"ok"
    assert calls == [(url, PUBLIC_IP)]


def test_scheme_default_port_allowed():
    cap, calls, _ = make_capture({PAGE: body(b"ok")})
    assert cap.capture(PAGE).raw_bytes == b"ok"
    assert calls == [(PAGE, PUBLIC_IP)]


@pytest.mark.parametrize("url", [
    f"https://{HOST}:8443/about",
    f"https://{HOST}:80/about",
    f"https://{HOST}:abc/about",          # unparseable port
    f"https://user@{HOST}/about",         # userinfo tricks
    f"https://user:pass@{HOST}/about",
    f"https://user:pass@{HOST}:8443/about",
])
def test_non_443_port_and_userinfo_refused_before_dns_or_connect(url):
    resolved = []
    cap, calls, _ = make_capture({})
    cap._resolver = lambda host: (resolved.append(host), [PUBLIC_IP])[1]
    with pytest.raises(CaptureError):
        cap.capture(url)
    assert calls == [] and resolved == []


def test_redirect_to_non_443_port_refused():
    target = f"https://{HOST}:8443/loot"
    cap, calls, _ = make_capture({PAGE: redirect(target)})
    with pytest.raises(CaptureError, match="port"):
        cap.capture(PAGE)
    assert not any(url == target for url, _ in calls)


def test_redirect_with_userinfo_refused():
    target = f"https://evil@{HOST}/about"
    cap, calls, _ = make_capture({PAGE: redirect(target)})
    with pytest.raises(CaptureError, match="userinfo"):
        cap.capture(PAGE)
    assert not any(url == target for url, _ in calls)


def test_mapping_entry_never_authorizes_non_443_or_userinfo():
    e = entry()
    assert not e.authorizes(f"https://{HOST}:8443/")
    assert not e.authorizes(f"https://user@{HOST}/")
    assert e.authorizes(f"https://{HOST}:443/")
    assert e.authorizes(PAGE)


# --- module boundary ----------------------------------------------------------

def test_no_capped_fetcher_dependency():
    import inspect
    src = inspect.getsource(wsc)
    assert "CappedFetcher" not in src.split('"""', 2)[-1] or \
        "CappedFetcher" not in "".join(
            l for l in src.splitlines()
            if not l.strip().startswith("#") and "does NOT use" not in l
            and "CappedFetcher decoded output" not in l)


def test_capture_output_has_no_decoded_text_or_facts():
    cap, _, _ = make_capture({PAGE: body("<p>héllo</p>".encode("utf-8"))})
    got = cap.capture(PAGE)
    fields = set(got.__dataclass_fields__)
    assert not {"text", "html", "facts", "colors", "palette"} & fields
    assert isinstance(got.raw_bytes, bytes)
