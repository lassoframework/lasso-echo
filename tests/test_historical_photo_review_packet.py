import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools import historical_photo_review_packet as pkt

SCOPE_KEY = Ed25519PrivateKey.generate()
SCOPE_KEY_ID = 'scope-key-1'
SCOPE_PUB_HEX = SCOPE_KEY.public_key().public_bytes_raw().hex()
APPROVED = {SCOPE_KEY_ID: SCOPE_PUB_HEX}
CUTOVER = {'cutover_id': 'cut-1', 'cutover_at': '2026-10-08T00:00:00Z',
           'evidence_refs': ['evidence:cutover-1']}


def sha(text):
    return 'sha256:' + hashlib.sha256(text.encode()).hexdigest()


def obj(name, size=100):
    return {'url': 'https://cdn.test/' + name, 'sha256': sha(name), 'length': size}


def candidate(**over):
    base = {
        'calendar_row_id': '11111111-1111-1111-1111-111111111111',
        'tenant_id': 'gym-a', 'group_key': 'grp-1', 'post_date': '2026-09-01',
        'source_asset_id': 'asset-1', 'source_version_id': 'v3',
        'source_receipt_ref': 'source-receipt:1',
        'source': obj('src'), 'delivered': obj('img'),
        'evidence_refs': ['evidence:bundle-1'],
    }
    return base | over


def frames(key, total):
    return [{'frame_index': i, 'sha256': sha(key + '-f' + str(i)), 'length': 50}
            for i in range(total)]


def visual(key, *, kind='still_photo', tenant='gym-a', route='instagram_feed',
           **over):
    base = {
        'history_key': key, 'tenant_id': tenant, 'send_route': route,
        'status': 'published', 'published_at': '2026-08-01T12:00:00Z',
        'late_post_id': None, 'provider_receipt_refs': ['rcpt:1'],
        'claim_ref': 'claim:1', 'reservation_ref': 'res:1',
        'media_kind': kind,
        'source': obj(key + '-src'), 'delivered': obj(key + '-img'),
        'thumbnail': None, 'snapshots': [],
    }
    if kind in ('video', 'carousel') and 'frames' not in over \
            and 'frames_hold_reason' not in over:
        base['frames'] = frames(key, 1)
        base['frames_total'] = 1
        base['frames_coverage_ref'] = 'frames-attest:' + key
    return base | over


def inspected_shas(v):
    def s(o):
        return o['sha256'] if 'sha256' in o else None
    shas = {x for x in [s(v['source']), s(v['delivered'])] if x}
    if v.get('thumbnail'):
        shas.add(v['thumbnail']['sha256'])
    for snap in v.get('snapshots', []):
        shas.add(snap['sha256'])
    for f in v.get('frames') or []:
        shas.add(f['sha256'])
    return sorted(shas)


def disposition(v, disp='reviewed_nonmatch', reason=None, ref='review:ev-1',
                inspected='auto'):
    if inspected == 'auto':
        inspected = [{'sha256': s, 'evidence_ref': 'obj-ev:' + s[-8:]}
                     for s in inspected_shas(v)]
    return {'history_key': v['history_key'], 'disposition': disp,
            'unresolved_reason': reason,
            'review_evidence_ref': None if disp == 'unresolved' else ref,
            'inspected_objects': None if disp == 'unresolved' else inspected}


def page(rows, cursor=None, next_cursor=None, **over):
    return {'cursor': cursor, 'next_cursor': next_cursor,
            'row_count': len(rows), 'page_digest': pkt.digest(rows),
            'rows': rows, **over}


def scope_payload(visuals, cutover=CUTOVER, **over):
    keys = sorted(v['history_key'] for v in visuals)
    counts = {}
    for v in visuals:
        pair = (v['tenant_id'], v['send_route'])
        counts[pair] = counts.get(pair, 0) + 1
    base = {
        'manifest_kind': 'historical_photo_cutover_scope',
        'cutover_id': cutover['cutover_id'],
        'cutover_at': cutover['cutover_at'],
        'generated_at': '2026-10-08T01:00:00Z',
        'source_query_id': 'fleet-send-corpus-query:v7',
        'expected_history_keys_digest': pkt.digest(keys),
        'expected_history_keys_count': len(keys),
        'tenant_route_counts': [
            {'tenant_id': t, 'send_route': r, 'expected_count': c}
            for (t, r), c in sorted(counts.items())],
    }
    return base | over


def sign_manifest(payload, key=SCOPE_KEY, key_id=SCOPE_KEY_ID):
    return {'payload': payload, 'key_id': key_id,
            'signature_hex': key.sign(pkt.canonical(payload).encode()).hex()}


def export(visuals, dispositions=None, pages=None, manifest='auto', **over):
    if pages is None:
        pages = [page(visuals)]
    if manifest == 'auto':
        manifest = sign_manifest(scope_payload(visuals))
    base = {
        'cutover': dict(CUTOVER),
        'scope_manifest': manifest,
        'candidate': candidate(),
        'pages': pages,
        'dispositions': dispositions if dispositions is not None
        else [disposition(v) for v in visuals],
    }
    return base | over


def build(ex, keys=APPROVED):
    return pkt.build_packet(ex, approved_keys=keys)


def test_complete_packet_is_review_evidence_only_never_clearance():
    packet = build(export([visual('h1'), visual('h2', tenant='gym-b')]))
    assert packet['packet_status'] == 'complete_for_independent_review'
    assert packet['decision'] == 'review_packet_only'
    assert packet['clearance'] is False
    assert packet['no_automatic_positive_decision'] is True
    assert packet['coverage']['total_visuals'] == 2
    assert packet['coverage']['by_disposition'] == {'reviewed_nonmatch': 2}
    assert packet['scope_manifest']['cutover_id'] == 'cut-1'
    assert packet['scope_manifest']['key_id'] == SCOPE_KEY_ID
    assert pkt.digest({k: v for k, v in packet.items() if k != 'packet_digest'}) \
        == packet['packet_digest']
    assert 'evidence:bundle-1' in packet['evidence_refs']
    assert 'evidence:cutover-1' in packet['evidence_refs']


def test_candidate_requires_authenticated_bytes_and_same_gym_version_binding():
    with pytest.raises(pkt.PacketHold, match='shape_invalid'):
        build(export([visual('h1')], candidate=candidate(source={'url': 'https://cdn.test/x'})))
    bad = candidate(source={'url': 'http://cdn.test/x', 'sha256': sha('x'), 'length': 1})
    with pytest.raises(pkt.PacketHold, match='shape_invalid'):
        build(export([visual('h1')], candidate=bad))
    held = build(export([visual('h1')], candidate=candidate(hold_reasons=['ambiguous_source_version'])))
    assert held['packet_status'] == 'hold'
    assert held['decision'] == 'review_packet_only' and held['clearance'] is False


def test_pagination_must_be_complete_cursor_chained_and_undruncated():
    v1, v2 = visual('h1'), visual('h2')
    ok = export([v1, v2], pages=[page([v1], next_cursor='c2'), page([v2], cursor='c2')])
    packet = build(ok)
    assert packet['coverage']['pages'] == 2
    assert [p['row_count'] for p in packet['pagination_ledger']] == [1, 1]

    with pytest.raises(pkt.PacketHold, match='pagination_cursor_chain_broken'):
        build(export([v1, v2], pages=[page([v1], next_cursor='c2'), page([v2], cursor='wrong')]))
    with pytest.raises(pkt.PacketHold, match='pagination_incomplete'):
        build(export([v1], pages=[page([v1], next_cursor='dangling')]))
    with pytest.raises(pkt.PacketHold, match='pagination_page_digest_mismatch'):
        build(export([v1], pages=[page([v1], page_digest=sha('bogus'))]))
    with pytest.raises(pkt.PacketHold, match='pagination_row_count_mismatch'):
        build(export([v1], pages=[page([v1], row_count=2)]))
    # No 2,500-row or 4 MiB style truncation is ever absorbed silently.
    for flag in ('truncated', 'row_limit_applied', 'byte_limit_applied', 'has_more'):
        with pytest.raises(pkt.PacketHold, match='pagination_truncated'):
            build(export([v1], pages=[page([v1], **{flag: True})]))


def test_null_to_null_page_chain_alone_never_proves_completeness():
    # A self-reported complete cursor chain without the signed scope manifest
    # (or with a non-matching one) is a hold, never "complete".
    ex = export([visual('h1')])
    del ex['scope_manifest']
    with pytest.raises(pkt.PacketHold, match='scope_manifest_missing'):
        build(ex)
    ex = export([visual('h1')], manifest=None)
    with pytest.raises(pkt.PacketHold, match='scope_manifest_missing'):
        build(ex)
    # Complete chain but the manifest attests one more key the export omits.
    ex = export([visual('h1')],
                manifest=sign_manifest(scope_payload([visual('h1'), visual('h2')])))
    with pytest.raises(pkt.PacketHold, match='scope_manifest_count_mismatch'):
        build(ex)


def test_scope_manifest_signature_and_key_gates_fail_closed():
    v1 = visual('h1')
    payload = scope_payload([v1])
    # Unsigned / garbage signature.
    with pytest.raises(pkt.PacketHold, match='scope_manifest_unsigned'):
        build(export([v1], manifest={'payload': payload, 'key_id': SCOPE_KEY_ID,
                                     'signature_hex': 'zz'}))
    # Forged: signed by an attacker key not in the approved set.
    attacker = Ed25519PrivateKey.generate()
    forged = sign_manifest(payload, key=attacker, key_id='attacker-key')
    with pytest.raises(pkt.PacketHold, match='scope_manifest_key_unapproved'):
        build(export([v1], manifest=forged))
    # Same key id but attacker's key material offered as "approved": the
    # signature no longer verifies under the real approved key.
    with pytest.raises(pkt.PacketHold, match='scope_manifest_key_unapproved'):
        build(export([v1]), keys=None)
    with pytest.raises(pkt.PacketHold, match='scope_manifest_key_unapproved'):
        build(export([v1]), keys={})
    # Altered payload after signing (count bumped by one).
    m = sign_manifest(payload)
    m = {**m, 'payload': {**payload, 'expected_history_keys_count':
                          payload['expected_history_keys_count'] + 1}}
    with pytest.raises(pkt.PacketHold, match='scope_manifest_altered'):
        build(export([v1], manifest=m))
    # Re-signing the altered payload with the real key now fails reconciliation.
    m2 = sign_manifest({**payload, 'expected_history_keys_count':
                        payload['expected_history_keys_count'] + 1})
    with pytest.raises(pkt.PacketHold, match='scope_manifest_count_mismatch'):
        build(export([v1], manifest=m2))


def test_scope_manifest_stale_cutover_and_key_set_mismatch():
    v1 = visual('h1')
    stale = scope_payload([v1], cutover={'cutover_id': 'cut-0',
                                         'cutover_at': '2026-10-01T00:00:00Z'})
    with pytest.raises(pkt.PacketHold, match='scope_manifest_stale'):
        build(export([v1], manifest=sign_manifest(stale)))
    wrong_keys = scope_payload([v1],
                               expected_history_keys_digest=pkt.digest(['hX']))
    with pytest.raises(pkt.PacketHold, match='scope_manifest_key_set_mismatch'):
        build(export([v1], manifest=sign_manifest(wrong_keys)))


def test_scope_manifest_omitted_tenant_or_route_holds():
    v1, v2 = visual('h1', tenant='gym-a'), visual('h2', tenant='gym-b')
    # Manifest covers both keys but attests gym-a only; the export silently
    # carries a gym-b row.
    payload = scope_payload([v1, v2])
    payload['tenant_route_counts'] = [
        {'tenant_id': 'gym-a', 'send_route': 'instagram_feed', 'expected_count': 2}]
    with pytest.raises(pkt.PacketHold, match='scope_manifest_tenant_route_mismatch'):
        build(export([v1, v2], manifest=sign_manifest(payload)))
    # Manifest splits gym-a into a bogus second route the export lacks.
    payload = scope_payload([v1])
    payload['tenant_route_counts'] = [
        {'tenant_id': 'gym-a', 'send_route': 'instagram_feed', 'expected_count': 0},
        {'tenant_id': 'gym-a', 'send_route': 'instagram_story', 'expected_count': 1}]
    with pytest.raises(pkt.PacketHold, match='scope_manifest_tenant_route_mismatch'):
        build(export([v1], manifest=sign_manifest(payload)))


def test_dispositions_cover_every_visual_and_unresolved_needs_reason():
    v1, v2 = visual('h1'), visual('h2')
    with pytest.raises(pkt.PacketHold, match='dispositions_incomplete'):
        build(export([v1, v2], dispositions=[disposition(v1)]))
    with pytest.raises(pkt.PacketHold, match='dispositions_incomplete'):
        build(export([v1], dispositions=[disposition(v1), disposition(v1)]))
    with pytest.raises(pkt.PacketHold, match='dispositions_incomplete'):
        build(export([v1],
                     dispositions=[disposition(v1, 'unresolved')]))
    with pytest.raises(pkt.PacketHold, match='dispositions_incomplete'):
        build(export([v1],
                     dispositions=[disposition(v1, 'reviewed_nonmatch', 'stray reason')]))
    packet = build(export(
        [v1, v2],
        dispositions=[disposition(v1, 'exact_match'),
                      disposition(v2, 'unresolved', 'delivered bytes unreachable')]))
    assert packet['packet_status'] == 'hold'
    assert packet['coverage']['unresolved_visuals'] == 1
    by_key = {v['history_key']: v for v in packet['visuals']}
    assert by_key['h1']['disposition'] == 'exact_match'
    assert by_key['h2']['unresolved_reasons'] == ['delivered bytes unreachable']


def test_disposition_must_bind_every_inspected_byte_sha_with_evidence_ref():
    v1 = visual('h1')
    full = disposition(v1)['inspected_objects']
    # Missing one inspected object (delivered sha dropped).
    partial = [e for e in full if e['sha256'] != v1['delivered']['sha256']]
    with pytest.raises(pkt.PacketHold, match='disposition_evidence_binding_invalid'):
        build(export([v1], dispositions=[disposition(v1, inspected=partial)]))
    # Extra sha not part of the visual.
    extra = full + [{'sha256': sha('stray'), 'evidence_ref': 'obj-ev:stray'}]
    with pytest.raises(pkt.PacketHold, match='disposition_evidence_binding_invalid'):
        build(export([v1], dispositions=[disposition(v1, inspected=extra)]))
    # Missing immutable evidence ref for one object.
    no_ref = [dict(e, evidence_ref='') if i == 0 else e
              for i, e in enumerate(full)]
    with pytest.raises(pkt.PacketHold, match='disposition_evidence_binding_invalid'):
        build(export([v1], dispositions=[disposition(v1, inspected=no_ref)]))
    # Unresolved dispositions must not smuggle inspected bindings.
    bad = disposition(v1, 'unresolved', 'bytes missing')
    bad['inspected_objects'] = full
    with pytest.raises(pkt.PacketHold, match='dispositions_incomplete'):
        build(export([v1], dispositions=[bad]))


def test_unreachable_bytes_hold_never_drop_and_video_carousel_frames_required():
    v = visual('h1', delivered={'url': 'https://cdn.test/gone', 'reachable': False})
    d = disposition(v)
    d['inspected_objects'] = [e for e in d['inspected_objects']
                              if e['sha256'] != sha('h1-img')]
    packet = build(export([v], dispositions=[d]))
    assert packet['packet_status'] == 'hold'
    assert packet['coverage']['byte_unreachable_visuals'] == 1
    assert 'object_bytes_unreachable' in packet['visuals'][0]['unresolved_reasons']

    with pytest.raises(pkt.PacketHold, match='visual_frames_coverage_required'):
        build(export([visual('h1', kind='video', frames=None)]))
    held = build(export([visual('h1', kind='carousel', frames=None,
                                frames_hold_reason='frame extraction unavailable')]))
    assert held['packet_status'] == 'hold'
    assert held['coverage']['frames_held_visuals'] == 1
    ok = build(export([visual('h1', kind='video')]))
    assert ok['packet_status'] == 'complete_for_independent_review'


def test_single_frame_of_multi_frame_video_is_insufficient():
    # One frame where the attested total is three: coverage is partial.
    partial = visual('h1', kind='video', frames=frames('h1', 1),
                     frames_total=3, frames_coverage_ref='frames-attest:h1')
    with pytest.raises(pkt.PacketHold, match='visual_frames_incomplete'):
        build(export([partial]))
    # Frames complete but the coverage attestation ref is missing.
    no_ref = visual('h1', kind='video', frames=frames('h1', 2), frames_total=2)
    with pytest.raises(pkt.PacketHold, match='visual_frames_coverage_required'):
        build(export([no_ref]))
    # Duplicate / unsorted frame indices are never complete.
    dup = visual('h1', kind='video',
                 frames=[frames('h1', 1)[0], frames('h1', 1)[0]],
                 frames_total=2, frames_coverage_ref='frames-attest:h1')
    with pytest.raises(pkt.PacketHold, match='visual_frames_coverage_required'):
        build(export([dup]))
    # Complete multi-frame coverage passes.
    full = visual('h1', kind='video', frames=frames('h1', 3), frames_total=3,
                  frames_coverage_ref='frames-attest:h1')
    packet = build(export([full]))
    assert packet['packet_status'] == 'complete_for_independent_review'
    assert packet['visuals'][0]['frames_total'] == 3


def test_duplicate_history_key_and_bad_shapes_fail_closed():
    with pytest.raises(pkt.PacketHold, match='duplicate_history_key'):
        build(export([visual('h1'), visual('h1', tenant='gym-b')]))
    with pytest.raises(pkt.PacketHold, match='visual_shape_invalid'):
        build(export([visual('h1', status='sent')]))
    with pytest.raises(pkt.PacketHold, match='visual_shape_invalid'):
        build(export([visual('h1', kind='gif')]))
    with pytest.raises(pkt.PacketHold, match='packet_shape_invalid'):
        build({'candidate': candidate()})


def test_cli_writes_0600_packet(tmp_path, capsys):
    src = tmp_path / 'export.json'
    src.write_text(json.dumps(export([visual('h1')])), encoding='utf-8')
    out = tmp_path / 'packet.json'
    pkt.main([str(src), '-o', str(out),
              '--approved-key', SCOPE_KEY_ID + ':' + SCOPE_PUB_HEX])
    assert out.stat().st_mode & 0o777 == 0o600
    packet = json.loads(out.read_text())
    assert packet['packet_status'] == 'complete_for_independent_review'
    summary = json.loads(capsys.readouterr().out)
    assert summary['packet_digest'] == packet['packet_digest']


def test_cli_requires_approved_key(tmp_path):
    src = tmp_path / 'export.json'
    src.write_text(json.dumps(export([visual('h1')])), encoding='utf-8')
    with pytest.raises(SystemExit):
        pkt.main([str(src), '-o', str(tmp_path / 'p.json')])
