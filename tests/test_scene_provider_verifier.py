"""Focused tests for the OFF-by-default Zernio scene provider readback verifier.

Everything runs on fakes: no network, no database, no credentials, no sends.
FakeReader stands in for the read-only verifier-credential authoritative
reader; FakeRpc records calls so every refusal/hold path can prove the
terminal attest_terminate boundary was NEVER crossed.
"""

import copy
import unittest

from agent.scene_provider_verifier import (MissingSeamError,
                                           SceneProviderVerifier,
                                           canonical_response_sha256)

TOKEN = "11111111-1111-1111-1111-111111111111"
BINDING_ID = "22222222-2222-2222-2222-222222222222"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
MD5_A = "md5:" + "1" * 32
MD5_B = "md5:" + "2" * 32
PHASH = "f" * 16
CAPTION = "hello gym"
MEDIA_URL = "https://cdn.example.com/delivered.jpg"


def _prepared(**over):
    prepared = {
        "claim_attempt_id": TOKEN,
        "tenant_id": "gym-alpha",
        "binding_id": BINDING_ID,
        "attester_id": "sender-attester-1",
        "canonical_payload_sha256": SHA_C,
        "delivered_url": MEDIA_URL,
        "delivered_sha256": SHA_A,
        "delivered_md5": MD5_A,
        "delivered_byte_length": 12345,
        "delivered_phash": PHASH,
        "prepared_caption": CAPTION,
        "prepared_media": [{
            "url": MEDIA_URL, "sha256": SHA_A,
            "md5": MD5_A, "byte_length": 12345,
        }],
    }
    prepared.update(over)
    return prepared


def _binding(**over):
    binding = {
        "binding_id": BINDING_ID,
        "tenant_id": "gym-alpha",
        "account_key": "instagram",
        "zernio_profile_id": "prof-alpha",
        "zernio_connected_account_id": "acct-ig-1",
        "channel": "instagram",
        "destination_page_id": "acct-ig-1",
        "surface": "feed",
    }
    binding.update(over)
    return binding


def _attempt(**over):
    attempt = {
        "claim_attempt_id": TOKEN,
        "tenant_id": "gym-alpha",
        "expected_provider": "zernio",
        "expected_channel": "instagram",
        "expected_provider_account_id": "acct-ig-1",
        "provider_post_id": "zpost-123",
        "delivered_url": MEDIA_URL,
        "delivered_md5": MD5_A,
        "delivered_phash": PHASH,
    }
    attempt.update(over)
    return attempt


def _platform_entry(**over):
    entry = {
        "platform": "instagram",
        "accountId": "acct-ig-1",
        "status": "published",
        "platformPostId": "ig-media-9",
        "platformSpecificData": {
            # Platform-authenticated DELIVERED evidence (fetched from the
            # platform post), NOT the submitted request mediaItems.
            "deliveredMedia": [{
                "url": MEDIA_URL, "sha256": SHA_A,
                "md5": "1" * 32, "byteLength": 12345,
            }],
        },
    }
    entry.update(over)
    return entry


def _readback(**over):
    post = {
        "_id": "zpost-123",
        "profileId": "prof-alpha",
        "content": CAPTION,
        "status": "published",
        # Submitted request media: hashes here are sender-supplied request
        # fields and must NEVER be accepted as delivered-byte proof.
        "mediaItems": [{
            "type": "image", "url": MEDIA_URL,
            "sha256": SHA_A, "md5": "1" * 32, "byteLength": 12345,
        }],
        "platforms": [_platform_entry()],
    }
    post.update(over)
    return post


class FakeClient:
    def __init__(self, post=None, error=None):
        self._post = post
        self._error = error
        self.calls = []

    def get_post(self, post_id):
        self.calls.append(post_id)
        if self._error:
            raise self._error
        return copy.deepcopy(self._post)


class FakeRpc:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def __call__(self, fn, payload):
        self.calls.append((fn, payload))
        if self.response is not None:
            return copy.deepcopy(self.response)
        return {"claim_attempt_id": payload["claim_attempt_id"],
                "provider_post_id": payload["provider_post_id"],
                "state": "finalized", "outcome": payload["outcome"],
                "replayed": False}


class FakeReader:
    def __init__(self, records=None, error=None):
        self._records = records
        self._error = error
        self.calls = []

    def __call__(self, claim_attempt_id):
        self.calls.append(claim_attempt_id)
        if self._error:
            raise self._error
        return copy.deepcopy(self._records)


def _records(prepared=None, binding=None, attempt=None):
    return {
        "prepared": prepared if prepared is not None else _prepared(),
        "binding": binding if binding is not None else _binding(),
        "attempt": attempt if attempt is not None else _attempt(),
    }


def _verifier(client, rpc, reader, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("verifier_id", "independent-verifier-1")
    return SceneProviderVerifier(client=client, rpc=rpc, reader=reader, **kw)


def _run(prepared=None, binding=None, attempt=None, post=None, error=None,
         reader_error=None, records=None, **kw):
    client = FakeClient(post=post if post is not None else _readback(),
                        error=error)
    rpc = FakeRpc()
    reader = FakeReader(records=records if records is not None
                        else _records(prepared, binding, attempt),
                        error=reader_error)
    out = _verifier(client, rpc, reader, **kw).verify(TOKEN)
    return out, client, rpc, reader


class TestDisabledByDefault(unittest.TestCase):
    def test_off_by_default_refuses_before_any_io(self):
        client = FakeClient(post=_readback())
        rpc = FakeRpc()
        reader = FakeReader(records=_records())
        v = SceneProviderVerifier(client=client, rpc=rpc, reader=reader,
                                  verifier_id="independent-verifier-1")
        out = v.verify(TOKEN)
        self.assertEqual(out["decision"], "refused")
        self.assertEqual(out["reason"], "adapter_disabled")
        self.assertEqual(reader.calls, [])
        self.assertEqual(client.calls, [])
        self.assertEqual(rpc.calls, [])

    def test_unconfigured_boundary_refuses(self):
        v = SceneProviderVerifier(enabled=True, verifier_id="v")
        out = v.verify(TOKEN)
        self.assertEqual(out["reason"], "verifier_boundary_unconfigured")

    def test_reader_required(self):
        v = SceneProviderVerifier(client=FakeClient(), rpc=FakeRpc(),
                                  enabled=True, verifier_id="v")
        out = v.verify(TOKEN)
        self.assertEqual(out["reason"], "verifier_boundary_unconfigured")


class TestCallerSuppliedIdentityRefused(unittest.TestCase):
    def test_caller_dict_is_never_authority(self):
        client = FakeClient(post=_readback())
        rpc = FakeRpc()
        reader = FakeReader(records=_records())
        out = _verifier(client, rpc, reader).verify(
            {"claim_attempt_id": TOKEN, "binding": _binding()})
        self.assertEqual(out["decision"], "refused")
        self.assertEqual(out["reason"], "caller_supplied_identity")
        self.assertEqual(reader.calls, [])
        self.assertEqual(client.calls, [])
        self.assertEqual(rpc.calls, [])

    def test_malformed_claim_attempt_id_refused(self):
        for bad in ("", "   ", "not-a-uuid", TOKEN.replace("-", "")):
            client = FakeClient(post=_readback())
            rpc = FakeRpc()
            reader = FakeReader(records=_records())
            out = _verifier(client, rpc, reader).verify(bad)
            self.assertEqual(out["decision"], "refused", bad)
            self.assertEqual(out["reason"], "malformed_claim_attempt_id", bad)
            self.assertEqual(reader.calls, [])
            self.assertEqual(rpc.calls, [])


class TestAuthoritativeReadSeams(unittest.TestCase):
    def test_reader_failure_refuses_before_provider_io(self):
        out, client, rpc, _ = _run(reader_error=RuntimeError("db down"))
        self.assertEqual(out["decision"], "refused")
        self.assertEqual(out["reason"], "authoritative_read_failed")
        self.assertEqual(client.calls, [])
        self.assertEqual(rpc.calls, [])

    def test_missing_snapshot_refuses(self):
        out, client, rpc, _ = _run(records={"prepared": None,
                                            "binding": _binding(),
                                            "attempt": _attempt()})
        self.assertEqual(out["reason"], "authoritative_snapshot_missing")
        self.assertEqual(client.calls, [])

    def test_missing_attempt_row_is_named_sql_seam(self):
        # The DRAFT SQL grants the verifier role no read on the attempt
        # table: the adapter fails closed and names the exact seam.
        out, client, rpc, _ = _run(records={"prepared": _prepared(),
                                            "binding": _binding(),
                                            "attempt": None})
        self.assertEqual(out["decision"], "refused")
        self.assertEqual(out["reason"], "missing_seam_verifier_attempt_read")
        self.assertIn("visual_scene_original_use_attempt", out["seam"])
        self.assertEqual(client.calls, [])
        self.assertEqual(rpc.calls, [])

    def test_reader_reported_seam_refuses(self):
        err = MissingSeamError("no verifier read API on attempt table")
        err.seam = "no verifier read API on attempt table"
        out, client, rpc, _ = _run(reader_error=err)
        self.assertEqual(out["reason"], "missing_seam_verifier_attempt_read")
        self.assertEqual(client.calls, [])

    def test_mismatched_records_refuse_before_provider_io(self):
        out, client, rpc, _ = _run(binding=_binding(tenant_id="gym-OTHER"))
        self.assertEqual(out["decision"], "refused")
        self.assertTrue(out["reason"].startswith(
            "authoritative_record_mismatch"))
        self.assertEqual(client.calls, [])
        self.assertEqual(rpc.calls, [])

    def test_attempt_delivered_lineage_mismatch_refuses(self):
        out, client, rpc, _ = _run(
            attempt=_attempt(delivered_md5=MD5_B))
        self.assertTrue(out["reason"].startswith(
            "authoritative_record_mismatch"))
        self.assertEqual(client.calls, [])

    def test_wrong_token_record_refuses(self):
        prepared = _prepared(
            claim_attempt_id="33333333-3333-3333-3333-333333333333")
        attempt = _attempt(
            claim_attempt_id="33333333-3333-3333-3333-333333333333")
        out, client, rpc, _ = _run(prepared=prepared, attempt=attempt)
        self.assertTrue(out["reason"].startswith(
            "authoritative_record_mismatch"))
        self.assertEqual(client.calls, [])


class TestProviderPostIdSeam(unittest.TestCase):
    def test_no_immutable_post_id_holds_before_provider_io(self):
        # The DRAFT schema stores provider_post_id only at terminate time,
        # so pre-finalization there is no known immutable id to read back.
        out, client, rpc, _ = _run(attempt=_attempt(provider_post_id=None))
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "missing_seam_provider_post_id_store")
        self.assertEqual(client.calls, [])
        self.assertEqual(rpc.calls, [])


class TestExactMatchAttests(unittest.TestCase):
    def test_exact_match_calls_verifier_rpc(self):
        out, client, rpc, reader = _run()
        self.assertEqual(out["decision"], "delivered")
        self.assertEqual(reader.calls, [TOKEN])
        self.assertEqual(client.calls, ["zpost-123"])
        self.assertEqual(len(rpc.calls), 1)
        fn, payload = rpc.calls[0]
        self.assertEqual(fn, "visual_scene_attester_attest_terminate")
        self.assertEqual(payload["outcome"], "delivered")
        self.assertEqual(payload["provider_post_id"], "zpost-123")
        self.assertEqual(payload["verifier_id"], "independent-verifier-1")
        ev = payload["readback_evidence"]
        self.assertEqual(ev["response_sha256"],
                         canonical_response_sha256(_readback()))
        self.assertEqual(ev["response_envelope"], "raw")
        self.assertEqual(ev["missing_seams"], [])
        self.assertTrue(ev["caption_verified"])
        self.assertEqual(
            ev["media_fingerprints_verified"][0]["sha256"], SHA_A)
        self.assertEqual(ev["platform_post_id"], "ig-media-9")

    def test_zernio_object_account_id_is_normalized(self):
        entry = _platform_entry(accountId={"_id": "acct-ig-1"})
        out, _, rpc, _ = _run(post=_readback(platforms=[entry]))
        self.assertEqual(out["decision"], "delivered")
        self.assertEqual(len(rpc.calls), 1)

    def test_gbp_omitted_topic_type_defaults_to_standard(self):
        entry = _platform_entry(platform="googlebusiness",
                                accountId="acct-gbp-1",
                                platformPostId="gbp-1")
        entry["platformSpecificData"].update(locationId="loc-1")
        binding = _binding(channel="googlebusiness", account_key="googlebusiness",
                            zernio_connected_account_id="acct-gbp-1",
                            destination_page_id="loc-1")
        attempt = _attempt(expected_channel="googlebusiness",
                           expected_provider_account_id="acct-gbp-1")
        out, _, rpc, _ = _run(binding=binding, attempt=attempt,
                              post=_readback(platforms=[entry]))
        self.assertEqual(out["decision"], "delivered")
        self.assertEqual(len(rpc.calls), 1)

    def test_unverified_rpc_result_does_not_claim_attestation(self):
        for response in (
                {"state": "ambiguous_hold", "outcome": "delivered",
                 "claim_attempt_id": TOKEN},
                {"state": "finalized", "outcome": "confirmed_no_send",
                 "claim_attempt_id": TOKEN},
                {"state": "finalized", "outcome": "delivered",
                 "claim_attempt_id": "33333333-3333-3333-3333-333333333333"},
                {"state": "finalized", "outcome": "delivered",
                 "claim_attempt_id": TOKEN,
                 "provider_post_id": "zpost-OTHER", "replayed": False},
                {"state": "finalized", "outcome": "delivered",
                 "claim_attempt_id": TOKEN, "provider_post_id": "zpost-123",
                 "replayed": "false"},
                {"state": "finalized", "outcome": "delivered",
                 "claim_attempt_id": TOKEN, "provider_post_id": "zpost-123"}):
            with self.subTest(response=response):
                client = FakeClient(post=_readback())
                rpc = FakeRpc(response=response)
                reader = FakeReader(records=_records())
                out = _verifier(client, rpc, reader).verify(TOKEN)
                self.assertEqual(out["decision"], "hold")
                self.assertEqual(out["reason"], "terminal_result_unverified")
                self.assertEqual(len(rpc.calls), 1)

    def test_evidence_is_deterministic_for_replay(self):
        # SQL replay requires byte-identical readback_evidence; no
        # wall-clock fields may leak into it.
        out1, _, rpc1, _ = _run()
        out2, _, rpc2, _ = _run()
        self.assertEqual(rpc1.calls[0][1]["readback_evidence"],
                         rpc2.calls[0][1]["readback_evidence"])
        self.assertNotIn("captured_at",
                         rpc1.calls[0][1]["readback_evidence"])

    def test_post_envelope_normalized_and_raw_hash_kept(self):
        envelope = {"post": _readback()}
        out, client, rpc, _ = _run(post=envelope)
        self.assertEqual(out["decision"], "delivered")
        ev = rpc.calls[0][1]["readback_evidence"]
        self.assertEqual(ev["response_envelope"], "post_envelope")
        # The auditable hash covers the EXACT captured envelope payload.
        self.assertEqual(ev["response_sha256"],
                         canonical_response_sha256(envelope))

    def test_malformed_envelope_refused(self):
        out, _, rpc, _ = _run(post={"post": ["not", "a", "dict"]})
        self.assertEqual(out["decision"], "refused")
        self.assertEqual(out["reason"], "malformed_readback_envelope")
        self.assertEqual(rpc.calls, [])

    def test_malformed_post_shape_refused(self):
        out, _, rpc, _ = _run(post={"post": {}})
        self.assertEqual(out["reason"], "malformed_readback")
        self.assertEqual(rpc.calls, [])


class TestNoSendIsNeverReleasedFromProviderStatus(unittest.TestCase):
    def test_failed_without_platform_post_id_holds_never_attests(self):
        # Zernio docs: a failed post may be retried and become published,
        # so even terminal "failed" with no platform post id is NOT proof
        # of no delivery. HOLD, no RPC.
        post = _readback(status="failed",
                         platforms=[_platform_entry(
                             status="failed", platformPostId=None)])
        out, _, rpc, _ = _run(post=post)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "ambiguous_status")
        self.assertEqual(rpc.calls, [])
        self.assertTrue(out["evidence"]["missing_seams"])

    def test_failed_partial_platform_inside_post_holds_never_attests(self):
        # A failed platform entry inside a partial/multi-platform post
        # cannot prove nothing was sent. HOLD, no RPC.
        post = _readback(
            status="partial",
            platforms=[_platform_entry(status="failed",
                                       platformPostId=None)])
        out, _, rpc, _ = _run(post=post)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "ambiguous_status")
        self.assertEqual(rpc.calls, [])

    def test_deleted_is_never_confirmed_no_send(self):
        post = _readback(status="deleted",
                         platforms=[_platform_entry(
                             status="deleted", platformPostId=None)])
        out, _, rpc, _ = _run(post=post)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "ambiguous_status")
        self.assertEqual(rpc.calls, [])

    def test_rejected_holds(self):
        post = _readback(status="rejected",
                         platforms=[_platform_entry(
                             status="rejected", platformPostId=None)])
        out, _, rpc, _ = _run(post=post)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "ambiguous_status")
        self.assertEqual(rpc.calls, [])

    def test_failed_with_platform_post_id_is_ambiguous(self):
        post = _readback(status="failed",
                         platforms=[_platform_entry(
                             status="failed", platformPostId="ig-media-9")])
        out, _, rpc, _ = _run(post=post)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "ambiguous_status")
        self.assertEqual(rpc.calls, [])

    def test_absence_404_and_errors_hold_never_release(self):
        out, _, rpc, _ = _run(post=None, error=RuntimeError("404"))
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "provider_read_failed")
        self.assertEqual(rpc.calls, [])

        out, _, rpc, _ = _run(post={})
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "provider_readback_missing")
        self.assertEqual(rpc.calls, [])


class TestDeliveredContentAndMediaProof(unittest.TestCase):
    def test_submitted_media_hashes_are_never_delivered_proof(self):
        # Readback carries ONLY submitted request mediaItems whose hashes
        # match the frozen values exactly. Still a HOLD: request fields are
        # not platform-delivered byte proof.
        post = _readback()
        post["platforms"][0]["platformSpecificData"] = {}
        out, _, rpc, _ = _run(post=post)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"],
                         "missing_seam_provider_delivered_media")
        self.assertTrue(out["evidence"]["missing_seams"])
        self.assertEqual(rpc.calls, [])

    def test_missing_frozen_caption_seam_holds(self):
        prepared = _prepared(prepared_caption=None)
        out, _, rpc, _ = _run(prepared=prepared)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "missing_seam_frozen_caption")
        self.assertEqual(rpc.calls, [])

    def test_readback_without_content_holds(self):
        post = _readback()
        del post["content"]
        out, _, rpc, _ = _run(post=post)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "missing_seam_provider_content")
        self.assertEqual(rpc.calls, [])

    def test_changed_caption_refuses(self):
        post = _readback(content="DIFFERENT caption")
        out, _, rpc, _ = _run(post=post)
        self.assertEqual(out["decision"], "refused")
        self.assertEqual(out["reason"], "caption_mismatch")
        self.assertEqual(rpc.calls, [])

    def test_missing_frozen_media_set_seam_holds(self):
        prepared = _prepared(prepared_media=None)
        out, _, rpc, _ = _run(prepared=prepared)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"], "missing_seam_frozen_media_set")
        self.assertEqual(rpc.calls, [])

    def test_frozen_media_must_contain_delivered_singleton(self):
        prepared = _prepared(prepared_media=[{
            "url": "https://cdn.example.com/OTHER.jpg", "sha256": SHA_B,
            "md5": MD5_B, "byte_length": 12000}])
        out, client, rpc, _ = _run(prepared=prepared)
        self.assertTrue(out["reason"].startswith(
            "authoritative_record_mismatch"))
        self.assertEqual(client.calls, [])

    def test_delivered_media_fingerprint_mismatch_refuses(self):
        entry = _platform_entry()
        entry["platformSpecificData"]["deliveredMedia"][0]["sha256"] = (
            "9" * 64)
        out, _, rpc, _ = _run(post=_readback(platforms=[entry]))
        self.assertEqual(out["decision"], "refused")
        self.assertEqual(out["reason"], "media_identity_mismatch")
        self.assertEqual(rpc.calls, [])

    def test_full_ordered_set_compared(self):
        # Extra delivered media object the prepared set does not contain.
        prepared = _prepared(prepared_media=[
            {"url": MEDIA_URL, "sha256": SHA_A, "md5": MD5_A,
             "byte_length": 12345},
            {"url": "https://cdn.example.com/two.jpg", "sha256": SHA_B,
             "md5": MD5_B, "byte_length": 12000},
        ])
        entry = _platform_entry()
        entry["platformSpecificData"]["deliveredMedia"].append({
            "url": "https://cdn.example.com/two.jpg", "sha256": SHA_D,
            "md5": "3" * 32, "byteLength": 12000})
        out, _, rpc, _ = _run(prepared=prepared,
                              post=_readback(platforms=[entry]))
        self.assertEqual(out["reason"], "media_identity_mismatch")
        self.assertEqual(rpc.calls, [])


class TestIdentityAndScopeRefusals(unittest.TestCase):
    def _refused(self, **kw):
        out, client, rpc, _ = _run(**kw)
        self.assertEqual(out["decision"], "refused")
        self.assertEqual(rpc.calls, [])
        return out

    def test_changed_readback_id_refuses(self):
        out = self._refused(post=_readback(_id="zpost-999"))
        self.assertEqual(out["reason"], "provider_post_id_changed")

    def test_profile_mismatch_refuses(self):
        out = self._refused(post=_readback(profileId="prof-OTHER"))
        self.assertEqual(out["reason"], "profile_mismatch")

    def test_connected_account_mismatch_refuses(self):
        out = self._refused(post=_readback(
            platforms=[_platform_entry(accountId="acct-OTHER")]))
        self.assertEqual(out["reason"], "connected_account_mismatch")

    def test_duplicate_platform_entries_refuse(self):
        post = _readback()
        post["platforms"].append(dict(post["platforms"][0]))
        out = self._refused(post=post)
        self.assertEqual(out["reason"], "duplicate_platform_entries")

    def test_surface_mismatch_refuses(self):
        entry = _platform_entry()
        entry["platformSpecificData"]["contentType"] = "story"
        out = self._refused(post=_readback(platforms=[entry]))
        self.assertEqual(out["reason"], "surface_mismatch")

    def test_published_without_platform_post_id_holds(self):
        post = _readback(platforms=[_platform_entry(platformPostId=None)])
        out, _, rpc, _ = _run(post=post)
        self.assertEqual(out["decision"], "hold")
        self.assertEqual(out["reason"],
                         "ambiguous_status_no_platform_post_id")
        self.assertEqual(rpc.calls, [])

    def test_self_certification_refuses(self):
        client = FakeClient(post=_readback())
        rpc = FakeRpc()
        reader = FakeReader(records=_records())
        v = SceneProviderVerifier(client=client, rpc=rpc, reader=reader,
                                  enabled=True,
                                  verifier_id="sender-attester-1")
        out = v.verify(TOKEN)
        self.assertEqual(out["reason"], "self_certification")
        self.assertEqual(client.calls, [])
        self.assertEqual(rpc.calls, [])

    def test_meta_direct_route_out_of_scope(self):
        out = self._refused(attempt=_attempt(expected_provider="meta_direct"))
        self.assertEqual(out["reason"], "non_zernio_provider")

    def test_gbp_gallery_out_of_scope(self):
        out = self._refused(
            binding=_binding(channel="googlebusiness",
                             zernio_connected_account_id="acct-gbp-1",
                             destination_page_id="loc-1", surface="photo"),
            attempt=_attempt(expected_channel="googlebusiness",
                             expected_provider_account_id="acct-gbp-1"))
        self.assertEqual(out["reason"], "surface_out_of_scope")

    def test_gbp_offer_out_of_scope(self):
        entry = _platform_entry(
            platform="googlebusiness", accountId="acct-gbp-1",
            platformPostId="gbp-1")
        entry["platformSpecificData"]["topicType"] = "OFFER"
        entry["platformSpecificData"]["locationId"] = "loc-1"
        out = self._refused(
            binding=_binding(channel="googlebusiness",
                             zernio_connected_account_id="acct-gbp-1",
                             destination_page_id="loc-1"),
            attempt=_attempt(expected_channel="googlebusiness",
                             expected_provider_account_id="acct-gbp-1"),
            post=_readback(platforms=[entry]))
        self.assertEqual(out["reason"], "gbp_topic_out_of_scope")

    def test_facebook_destination_mismatch_refuses(self):
        entry = _platform_entry(
            platform="facebook", accountId="acct-fb-1",
            platformPostId="fb-1")
        entry["platformSpecificData"]["pageId"] = "page-OTHER"
        out = self._refused(
            binding=_binding(channel="facebook", account_key="facebook",
                             zernio_connected_account_id="acct-fb-1",
                             destination_page_id="page-1"),
            attempt=_attempt(expected_channel="facebook",
                             expected_provider_account_id="acct-fb-1"),
            post=_readback(platforms=[entry]))
        self.assertEqual(out["reason"], "destination_mismatch")


if __name__ == "__main__":
    unittest.main()
