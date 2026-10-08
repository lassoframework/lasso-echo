"""Synthetic run/dataset API evidence; never starts a live Actor."""
import gzip
import hashlib
import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest

from agent import apify_run_capture as arc

TOKEN = "apify_SECRET_fixture_token"
HANDLE = "testgym"
OWNER = "17841400000000001"
ACTOR_ID = "realActor123"
RUN_ID = "run123"
DATASET_ID = "dataset123"


def run(status="SUCCEEDED", **overrides):
    return json.dumps({"data": {"id": RUN_ID, "actId": ACTOR_ID,
                                "defaultDatasetId": DATASET_ID,
                                "status": status, **overrides}}).encode()


def items(**overrides):
    return json.dumps([{"ownerUsername": HANDLE, "ownerId": OWNER,
                        "caption": "Actual provider text", **overrides}],
                      indent=2).encode()


class FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses if responses is not None else
                              [run("RUNNING"), run(), items()])
        self.calls = []

    def token(self):
        return TOKEN

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Clock:
    value = 0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def capture(tmp_path, client=None, **overrides):
    clock = Clock()
    options = dict(mapped_handle=HANDLE, mapped_provider_account_id=OWNER,
                   mapped_source_locator=f"https://www.instagram.com/{HANDLE}/",
                   gym_id="gym1", echo_account_key="gym1_ig",
                   mapping_revision="map1", mapping_evidence={"verified": True},
                   source_revision="source1", request_id="request1",
                   expected_actor_id=ACTOR_ID,
                   journal=arc.SQLiteStartJournal(tmp_path / "journal.sqlite3"),
                   max_total_charge_usd=1, enabled=True,
                   client=client or FakeClient(), monotonic=clock.monotonic,
                   sleep=clock.sleep, now=datetime(2026, 10, 7, tzinfo=timezone.utc))
    options.update(overrides)
    return arc.capture_social_source_run(**options)


def test_exact_entity_bytes_and_run_dataset_provenance(tmp_path):
    raw = items()
    client = FakeClient([run("RUNNING"), run(), raw])
    result = capture(tmp_path, client)
    assert result.ok
    assert result.raw_bytes == raw
    assert result.sha256 == hashlib.sha256(raw).hexdigest()
    p = result.provenance
    assert p["provider_response_id"] == "apify:run:run123:dataset:dataset123"
    assert p["provider_run_id"] == RUN_ID
    assert p["provider_dataset_id"] == DATASET_ID
    assert p["source_url"].startswith(f"{arc.API_ROOT}/datasets/{DATASET_ID}/items?")
    assert p["provider_account_id"] == OWNER
    assert p["gym_id"] == "gym1"
    assert p["identity_verified"] is True
    assert TOKEN not in json.dumps(p)
    assert [call[0] for call in client.calls] == ["POST", "GET", "GET"]
    assert "restartOnError=false" in client.calls[0][1]
    assert "maxTotalChargeUsd=1" in client.calls[0][1]
    assert client.calls[0][2]["payload"]["username"] == [HANDLE]
    assert client.calls[1][1] == f"{arc.API_ROOT}/actor-runs/{RUN_ID}"


def test_nested_mapping_mutation_during_request_cannot_change_validated_capture(tmp_path):
    evidence = {"mapping": {"gym_id": "gym1", "proof": ["verified"]}}
    expected = json.loads(json.dumps(evidence))

    class MutatingClient(FakeClient):
        def request(self, method, url, **kwargs):
            if method == "POST":
                evidence["mapping"]["gym_id"] = "otherGym"
                evidence["mapping"]["proof"].append(TOKEN)
            return super().request(method, url, **kwargs)

    result = capture(tmp_path, MutatingClient(), mapping_evidence=evidence)
    assert result.ok
    assert result.provenance["mapping_evidence"] == expected
    assert result.provenance["gym_id"] == "gym1"
    assert TOKEN not in json.dumps(result.provenance)
    assert "otherGym" not in json.dumps(result.provenance)
    # The journal binding was made from the same frozen evidence returned.
    retry = FakeClient([run(), items()])
    assert capture(tmp_path, retry, mapping_evidence=expected).ok
    assert all(call[0] == "GET" for call in retry.calls)


def test_nested_mapping_mutation_after_return_cannot_change_result(tmp_path):
    evidence = {"mapping": {"gym_id": "gym1", "proof": [{"verified": True}]}}
    expected = json.loads(json.dumps(evidence))
    result = capture(tmp_path, mapping_evidence=evidence)
    assert result.ok
    evidence["mapping"]["gym_id"] = "otherGym"
    evidence["mapping"]["proof"][0]["credential"] = TOKEN
    evidence["mapping"]["proof"].append("differentProof")
    assert result.provenance["mapping_evidence"] == expected
    assert TOKEN not in json.dumps(result.provenance)
    assert "otherGym" not in json.dumps(result.provenance)
    # Returning a mutable result does not give it access to caller-owned data.
    result.provenance["mapping_evidence"]["mapping"]["proof"][0]["verified"] = False
    assert evidence["mapping"]["proof"][0]["verified"] is True


def test_mapping_is_snapshotted_before_client_token_callback(tmp_path):
    evidence = {"mapping": {"gym_id": "gym1", "proof": ["verified"]}}
    expected = json.loads(json.dumps(evidence))

    class MutatingTokenClient(FakeClient):
        def token(self):
            evidence["mapping"]["gym_id"] = "otherGym"
            evidence["mapping"]["proof"].append(TOKEN)
            return TOKEN

    result = capture(tmp_path, MutatingTokenClient(), mapping_evidence=evidence)
    assert result.ok and result.provenance["mapping_evidence"] == expected


def test_non_json_mapping_evidence_holds_before_provider(tmp_path):
    client = FakeClient()
    result = capture(tmp_path, client, mapping_evidence={"opaque": object()})
    assert not result.ok and not client.calls


def test_default_off_makes_no_request_or_journal_claim(tmp_path):
    client = FakeClient()
    result = capture(tmp_path, client, enabled=False)
    assert not result.ok and "disabled" in result.reason
    assert not client.calls


@pytest.mark.parametrize("overrides", [
    {"mapped_handle": ""}, {"mapped_provider_account_id": "account-key"},
    {"mapped_source_locator": "https://instagram.com/othergym/"},
    {"mapped_source_locator": "https://instagram.com/testgym/?token=hello"},
    {"mapped_source_locator": "https://instagram.com:443/testgym/"},
    {"gym_id": ""}, {"echo_account_key": ""}, {"mapping_revision": ""},
    {"source_revision": ""}, {"request_id": ""}, {"mapping_evidence": {}},
    {"expected_actor_id": ""}, {"max_bytes": 2_000_001},
    {"results_limit": 501}, {"max_total_charge_usd": 0},
    {"max_total_charge_usd": float("nan")}, {"timeout_seconds": 0},
    {"poll_interval": 0}, {"journal": None},
])
def test_invalid_binding_holds_before_network(tmp_path, overrides):
    client = FakeClient()
    result = capture(tmp_path, client, **overrides)
    assert not result.ok and not result.raw_bytes and not client.calls


@pytest.mark.parametrize("field,value", [("id", "anotherRun"),
                                        ("defaultDatasetId", "anotherDataset"),
                                        ("actId", "anotherActor")])
def test_exact_run_readback_identity_required(tmp_path, field, value):
    client = FakeClient([run(), run(**{field: value})])
    result = capture(tmp_path, client)
    assert not result.ok and "identity mismatch" in result.reason
    assert not result.raw_bytes
    assert len(client.calls) == 2


@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT", "ABORTED", "UNKNOWN"])
def test_failed_or_unknown_status_cannot_fetch_dataset(tmp_path, status):
    client = FakeClient([run(), run(status)])
    result = capture(tmp_path, client)
    assert not result.ok and not result.raw_bytes
    assert len(client.calls) == 2


def test_start_must_supply_genuine_run_dataset_actor(tmp_path):
    client = FakeClient([json.dumps({"data": {"id": RUN_ID}}).encode()])
    assert not capture(tmp_path, client).ok
    retry = FakeClient()
    result = capture(tmp_path, retry)
    assert not result.ok and "durably fenced" in result.reason
    assert not retry.calls


def test_uncertain_paid_start_no_automatic_retry_even_new_client(tmp_path):
    client = FakeClient([RuntimeError("Bearer " + TOKEN)])
    result = capture(tmp_path, client)
    assert not result.ok and TOKEN not in result.reason
    assert len(client.calls) == 1
    retry = FakeClient()
    second = capture(tmp_path, retry)
    assert "durably fenced" in second.reason
    assert not retry.calls
    assert TOKEN.encode() not in (tmp_path / "journal.sqlite3").read_bytes()


@pytest.mark.parametrize("unsafe_reason", [TOKEN, "provider account details",
                                        "Authorization: Bearer " + TOKEN])
def test_injected_capture_hold_is_static_in_result_and_traceback(tmp_path, unsafe_reason, capsys):
    error = arc.CaptureHold(unsafe_reason)
    client = FakeClient([error])
    result = capture(tmp_path, client)
    assert not result.ok and not result.raw_bytes and not result.provenance
    assert result.reason == "run-scoped capture failed; holding"
    assert unsafe_reason not in repr(result)
    assert unsafe_reason not in "".join(traceback.format_exception(error))
    assert unsafe_reason not in repr(error)
    output = capsys.readouterr()
    assert unsafe_reason not in output.out + output.err
    retry = FakeClient()
    assert "durably fenced" in capture(tmp_path, retry).reason
    assert not retry.calls


def test_injected_capture_hold_subclass_cannot_override_safe_result(tmp_path):
    class UnsafeHold(arc.CaptureHold):
        def __str__(self):
            raise AssertionError("must never stringify injected exception")

    result = capture(tmp_path, FakeClient([UnsafeHold(TOKEN)]))
    assert result.reason == "run-scoped capture failed; holding"
    assert TOKEN not in repr(result)


def test_injected_capture_hold_cannot_forge_enum_field(tmp_path):
    error = arc.CaptureHold("provider transport failed; holding")
    error.safe_reason = TOKEN
    result = capture(tmp_path, FakeClient([error]))
    assert result.reason == "run-scoped capture failed; holding"
    assert TOKEN not in repr(result)


def test_injected_capture_hold_missing_enum_and_forged_args_fail_closed(tmp_path):
    error = arc.CaptureHold("provider transport failed; holding")
    del error.safe_reason
    error.args = (TOKEN,)
    result = capture(tmp_path, FakeClient([error]))
    assert result.reason == "run-scoped capture failed; holding"
    assert TOKEN not in repr(result)


def test_known_run_resumed_without_second_paid_start(tmp_path):
    first = FakeClient([run(), TimeoutError(TOKEN)])
    assert not capture(tmp_path, first).ok
    retry = FakeClient([run(), items()])
    result = capture(tmp_path, retry)
    assert result.ok
    assert [c[0] for c in retry.calls] == ["GET", "GET"]


@pytest.mark.parametrize("changed", [{"gym_id": "otherGym"},
                                      {"echo_account_key": "otherAccount"},
                                      {"mapping_revision": "differentRevision"},
                                      {"mapped_provider_account_id": "999"},
                                      {"source_revision": "source2"}])
def test_request_fence_cannot_be_rebound_to_another_tenant(tmp_path, changed):
    assert capture(tmp_path).ok
    client = FakeClient()
    result = capture(tmp_path, client, **changed)
    assert not result.ok and "binding changed" in result.reason
    assert not client.calls


def test_atomic_claim_allows_one_start_only(tmp_path):
    journal = arc.SQLiteStartJournal(tmp_path / "journal.sqlite3")

    def claim(_):
        try:
            return journal.claim("request", "binding")[1]
        except arc.CaptureHold:
            return "fenced"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert results.count(None) == 1
    assert results.count("fenced") == 3


def test_poll_timeout_keeps_known_run_and_never_restarts(tmp_path):
    client = FakeClient([run(), run("RUNNING"), run("RUNNING")])
    result = capture(tmp_path, client, timeout_seconds=2)
    assert not result.ok and "timeout" in result.reason
    assert [c[0] for c in client.calls].count("POST") == 1
    retry = FakeClient([run(), items()])
    assert capture(tmp_path, retry, timeout_seconds=2).ok
    assert all(c[0] == "GET" for c in retry.calls)


@pytest.mark.parametrize("raw", [b"[]", b"[{}]", b"[null]", b"{}",
    items(ownerUsername="othergym"), items(ownerId="999"),
    items(username="othergym"), items(owner_id="999"),
    items(ownerId=True), items(ownerId=float(OWNER)),
    b'[{"ownerUsername":"testgym","ownerId":"17841400000000001","ownerId":"999"}]',
    items(ownerId=None), items(ownerUsername=None)])
def test_every_item_identity_required_and_aliases_cannot_hide_ambiguity(tmp_path, raw):
    result = capture(tmp_path, FakeClient([run(), run(), raw]))
    assert not result.ok and not result.raw_bytes and not result.provenance


def test_second_item_foreign_owner_holds_entire_artifact(tmp_path):
    good = json.loads(items())[0]
    raw = json.dumps([good, {**good, "ownerId": "999"}]).encode()
    assert not capture(tmp_path, FakeClient([run(), run(), raw])).ok


def test_identity_without_actual_post_text_is_insufficient(tmp_path):
    result = capture(tmp_path, FakeClient([run(), run(), items(caption=" ")]))
    assert not result.ok and "no actual post text" in result.reason


def test_dataset_over_size_or_count_holds(tmp_path):
    too_large = items(caption="x" * 1000)
    assert not capture(tmp_path, FakeClient([run(), run(), too_large]), max_bytes=500).ok


@pytest.mark.parametrize("secret_raw", [items(caption=TOKEN),
    items(caption=TOKEN).replace(b"apify_SECRET", b"apify_\\u0053ECRET")])
def test_source_secret_rejected_without_rewriting_raw_bytes(tmp_path, secret_raw):
    result = capture(tmp_path, FakeClient([run(), run(), secret_raw]))
    assert not result.ok and "secret" in result.reason
    assert TOKEN not in result.reason and not result.raw_bytes


def test_mapping_secret_not_written_or_sent(tmp_path):
    client = FakeClient()
    result = capture(tmp_path, client, mapping_evidence={"unsafe": TOKEN})
    assert not result.ok and not client.calls
    assert TOKEN.encode() not in (tmp_path / "journal.sqlite3").read_bytes()


class RawStream(httpx.SyncByteStream):
    def __init__(self, raw):
        self.raw = raw

    def __iter__(self):
        for offset in range(0, len(self.raw), 17):
            yield self.raw[offset:offset + 17]


def transport_client(raw=items(), headers=None, status=200, inspect=None):
    def handler(request):
        if inspect:
            inspect(request)
        return httpx.Response(status, headers={"content-type": "application/json",
                                              **(headers or {})},
                              stream=RawStream(raw))
    return arc.RunScopedApifyClient(TOKEN, httpx.MockTransport(handler))


def test_real_transport_auth_header_and_exact_stream_entity():
    observed = []
    client = transport_client(inspect=observed.append)
    url = f"{arc.API_ROOT}/datasets/{DATASET_ID}/items?format=json"
    assert client.request("GET", url) == items()
    request = observed[0]
    assert request.headers["Authorization"] == "Bearer " + TOKEN
    assert TOKEN not in str(request.url)
    assert request.headers["Accept-Encoding"] == "identity"


def test_gzip_is_bounded_and_original_compressed_bytes_exact():
    raw = items()
    compressed = gzip.compress(raw)
    client = transport_client(compressed, {"content-encoding": "gzip"})
    assert client.request("GET", f"{arc.API_ROOT}/datasets/d/items") == compressed
    assert arc._json(compressed) == json.loads(raw)


def test_compressed_source_artifact_preserved_with_verified_identity(tmp_path):
    raw = gzip.compress(items())
    result = capture(tmp_path, FakeClient([run(), run(), raw]))
    assert result.ok and result.raw_bytes == raw
    assert result.sha256 == hashlib.sha256(raw).hexdigest()


def test_compressed_json_bomb_holds_before_identity_materialization(tmp_path):
    raw = gzip.compress(items(caption="x" * (arc.MAX_CAPTURE_BYTES + 1)))
    result = capture(tmp_path, FakeClient([run(), run(), raw]))
    assert not result.ok and not result.raw_bytes


@pytest.mark.parametrize("raw,headers,cap", [
    (b"x" * 1001, {}, 1000),
    (b"[]", {"content-length": "2001"}, 1000),
    (b"[]", {"content-length": "1"}, 1000),
    (gzip.compress(b"x" * 10001), {"content-encoding": "gzip"}, 1000),
    (gzip.compress(b"[]") + gzip.compress(b"[]"), {"content-encoding": "gzip"}, 1000),
    (gzip.compress(b"[]")[:-2], {"content-encoding": "gzip"}, 1000),
    (b"badgzip", {"content-encoding": "gzip"}, 1000),
    (b"[]", {"content-encoding": "br"}, 1000),
])
def test_transport_oversize_decompression_ambiguity_and_truncation_hold(raw, headers, cap):
    client = transport_client(raw, headers)
    with pytest.raises(arc.CaptureHold):
        client.request("GET", f"{arc.API_ROOT}/datasets/d/items", max_bytes=cap)


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_http_errors_never_surface_secret_body_or_retry(status):
    calls = []
    client = transport_client(TOKEN.encode(), status=status, inspect=calls.append)
    with pytest.raises(arc.CaptureHold) as exc:
        client.request("GET", f"{arc.API_ROOT}/datasets/d/items")
    assert TOKEN not in str(exc.value)
    assert len(calls) == 1


def test_transport_exception_secret_suppressed():
    def handler(_):
        raise RuntimeError("Authorization: Bearer " + TOKEN)
    client = arc.RunScopedApifyClient(TOKEN, httpx.MockTransport(handler))
    with pytest.raises(arc.CaptureHold) as exc:
        client.request("GET", f"{arc.API_ROOT}/datasets/d/items")
    assert TOKEN not in str(exc.value)


def test_no_untrusted_host_or_query_token():
    client = transport_client()
    for url in ("https://evil.example/v2/datasets/d/items",
                f"{arc.API_ROOT}/datasets/d/items?token={TOKEN}"):
        with pytest.raises(arc.CaptureHold):
            client.request("GET", url)


def test_private_journal_mode_required(tmp_path):
    path = tmp_path / "journal.sqlite3"
    path.write_text("")
    path.chmod(0o644)
    with pytest.raises(arc.CaptureHold):
        arc.SQLiteStartJournal(path)
