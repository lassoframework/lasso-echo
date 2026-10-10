"""Focused tests for the exact-byte historical seed/export preparation tool."""
import hashlib
import importlib.util
import json
import os
import sys
import types
import uuid
from datetime import datetime, timezone

import pytest

from agent import exact_byte_history_seed_20261010 as seed

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
OBS_REF = 'evidence://observations/run-1'
TENANT_REF = 'evidence://tenants/registry-2026-10-10'
TARGET_REF = 'evidence://targets/audit-2026-10-10'
REVIEW_REF = 'evidence://census-review/signoff-2026-10-10'
FLOOR = seed.BASELINE_MIN_ROWS
FILLER_START = 100000


def rid(n):
    return str(uuid.UUID(int=n))


def rev_of(raw):
    return seed._sha256_text(json.dumps(raw, sort_keys=True))


def make_row(n, *, gym='gym-a', account='instagram', fmt='feed',
             image=True, thumb=False, status='published', date=None,
             logical=None, extra=None):
    raw = {
        'id': rid(n), 'gym_id': gym, 'account': account, 'format': fmt,
        'post_date': date or '2026-10-%02d' % (n % 28 + 1), 'status': status,
        'published_at': '2026-10-01T00:00:00+00:00', 'late_post_id': None,
        'image_url': 'https://cdn.example.com/img/%d.jpg' % n if image else '',
        'thumbnail_url': ('https://cdn.example.com/thumb/%d.jpg' % n
                          if thumb else ''),
        'logical_post_id': logical,
    }
    if extra:
        raw.update(extra)
    return {'row': raw, 'revision': rev_of(raw)}


def pad_items(explicit, total=None):
    """Pad with valid mapped filler rows so the census meets the floor."""
    items = list(explicit)
    n = FILLER_START
    target = FLOOR if total is None else total
    while len(items) < target:
        items.append(make_row(n))
        n += 1
    return items


def occurrence_count(items):
    n = 0
    for it in items:
        r = it['row']
        n += (1 if r.get('image_url') else 0) + (1 if r.get('thumbnail_url') else 0)
    return n


class FakeReader:
    def __init__(self, items, *, snapshot=None, fail_at_page=None):
        self.items = sorted(items, key=lambda i: i['row']['id'])
        self.snapshot = snapshot
        self.fail_at_page = fail_at_page
        self.pages_served = 0
        self.writes = []

    def census_page(self, after_id, limit):
        if self.fail_at_page is not None and self.pages_served >= self.fail_at_page:
            raise seed.SeedError('simulated census read failure')
        self.pages_served += 1
        page = [i for i in self.items
                if after_id is None or i['row']['id'] > after_id][:limit]
        return page

    def historical_snapshot(self):
        if self.snapshot is not None:
            return self.snapshot
        census = [seed._census_row(i) for i in self.items]
        return seed.local_historical_snapshot(census)


def make_fetcher(fail_urls=(), sizes=None):
    def fetch(url):
        assert url.startswith('https://cdn.example.com/'), 'fetch outside allowlist'
        if url in fail_urls:
            raise seed.FetchError('denied')
        size = (sizes or {}).get(url, 12345)
        return ('sha256:' + hashlib.sha256(url.encode()).hexdigest(), size)
    return fetch


def make_mapping(*, gyms=('gym-a',), accounts=('instagram',), thumb_declared=True,
                 proofs=(), members=(), no_media=()):
    tenants = [{'calendar_gym_id': g, 'canonical_tenant': 'tenant-' + g,
                'posting_timezone': 'America/Indiana/Indianapolis',
                'evidence_ref': TENANT_REF} for g in gyms]
    targets = [{'calendar_gym_id': g, 'account': a, 'format': 'feed',
                'provider_target': {'provider': 'zernio', 'platform': a,
                                    'account_id': 'acct-' + g + '-' + a},
                'image_fields': (['image_url', 'thumbnail_url'] if thumb_declared
                                 else ['image_url']),
                'evidence_ref': TARGET_REF}
               for g in gyms for a in accounts]
    return {'tenants': tenants, 'targets': targets,
            'sibling_proofs': list(proofs), 'sibling_members': list(members),
            'no_media_evidence': list(no_media),
            'observation_evidence_ref': OBS_REF}


def run_ok(items, mapping, fetcher=None, pad=True, review=True, **kw):
    if pad:
        items = pad_items(items)
    reader = FakeReader(items)
    if review:
        mapping.setdefault('census_review', {
            'historical_snapshot_sha256': reader.historical_snapshot(),
            'census_row_count': len(items),
            'evidence_ref': REVIEW_REF})
    return seed.run(reader, mapping, fetcher=fetcher or make_fetcher(),
                    now=NOW, baseline_min=kw.pop('baseline_min', FLOOR), **kw)


def _proof(identity, row_ids, tenant='tenant-gym-a', date='2026-10-02',
           logical=None):
    return {'shared_posting_identity': identity,
            'canonical_tenant': tenant,
            'post_date': date,
            'logical_post_id': logical or rid(900),
            'member_row_ids': list(row_ids),
            'evidence_ref': 'evidence://sibling/proof-1'}


def _member(row_n, identity):
    return {'calendar_row_id': rid(row_n), 'shared_posting_identity': identity,
            'evidence_ref': 'evidence://sibling/m%d' % row_n}


def sibling_setup():
    """Two distinct member rows sharing one persisted logical_post_id."""
    logical = str(uuid.uuid4())
    identity = str(uuid.uuid4())
    items = [make_row(1, date='2026-10-02', logical=logical),
             make_row(2, date='2026-10-02', logical=logical)]
    proof = _proof(identity, [rid(1), rid(2)], logical=logical)
    members = [_member(1, identity), _member(2, identity)]
    return items, identity, logical, proof, members


# ---------------------------------------------------------------------------
# Happy path, pagination and completeness
# ---------------------------------------------------------------------------

def test_complete_run_emits_seed_and_readback():
    explicit = [make_row(n) for n in range(1, 8)]
    items = pad_items(explicit)
    result = run_ok(explicit, make_mapping(), page_size=3)
    assert result.blockers == []
    assert result.manifest['status'] == 'COMPLETE_UNAPPLIED'
    assert result.manifest['census']['total_rows'] == len(items)
    assert 'insert into public.exact_byte_history_observation_20261010' in result.seed_sql
    assert 'db_published_url_bytes' in result.seed_sql
    assert 'exact_byte_gate' not in result.seed_sql
    sql_lines = [l for l in result.seed_sql.splitlines() if not l.startswith('--')]
    # The only content_calendar statement allowed is the SHARE-mode lock
    # (read freeze); the seed never writes or updates content_calendar.
    assert [l for l in sql_lines if 'content_calendar' in l] == \
        ['lock table public.content_calendar in share mode;']
    assert 'exact_byte_corpus_digest_20261010()' in result.readback_sql
    for digest in (result.manifest['seed_sql_sha256'],
                   result.manifest['readback_sql_sha256']):
        assert seed.SHA256_RE.match(digest)


def test_keyset_pagination_short_and_exact_pages():
    explicit = [make_row(n) for n in range(1, 12)]
    items = pad_items(explicit)
    result = run_ok(explicit, make_mapping(), page_size=64)
    assert result.blockers == []
    assert result.manifest['census']['total_rows'] == len(items)
    # exact final page (page size divides the padded census evenly)
    result2 = run_ok(explicit, make_mapping(), page_size=len(items) // 2)
    assert result2.blockers == []
    assert result2.manifest['census']['total_rows'] == len(items)


def test_census_read_failure_propagates():
    reader = FakeReader(pad_items([make_row(1)]), fail_at_page=2)
    with pytest.raises(seed.SeedError):
        seed.run(reader, make_mapping(), fetcher=make_fetcher(),
                 baseline_min=FLOOR, now=NOW)


def test_census_below_baseline_floor_blocks():
    result = run_ok([make_row(n) for n in range(1, 5)], make_mapping(), pad=False)
    assert result.seed_sql is None
    assert result.manifest['status'] == 'INCOMPLETE_DO_NOT_APPLY'
    assert any('baseline floor' in b for b in result.blockers)


def test_baseline_floor_cannot_be_lowered():
    # A caller passing 1 still faces the 1467 floor: a small census blocks.
    result = run_ok([make_row(1)], make_mapping(), pad=False, baseline_min=1)
    assert result.seed_sql is None
    assert result.manifest['census']['baseline_floor'] == FLOOR
    assert any('baseline floor %d' % FLOOR in b for b in result.blockers)
    # A full census with baseline_min=1 is still evaluated against 1467 and
    # passes; the floor is non-lowerable, not an exact-count requirement.
    ok = run_ok([make_row(1)], make_mapping(), baseline_min=1)
    assert ok.blockers == []
    assert ok.manifest['census']['baseline_floor'] == FLOOR
    # Callers may raise the floor above 1467.
    raised = run_ok([make_row(1)], make_mapping(), baseline_min=FLOOR + 10)
    assert raised.seed_sql is None
    assert any('baseline floor %d' % (FLOOR + 10) in b for b in raised.blockers)


def test_historical_snapshot_mismatch_blocks():
    items = [make_row(n) for n in range(1, 4)]
    bad = 'sha256:' + '0' * 64
    reader = FakeReader(pad_items(items), snapshot=bad)
    mapping = make_mapping()
    mapping['census_review'] = {'historical_snapshot_sha256': bad,
                                'census_row_count': len(reader.items),
                                'evidence_ref': REVIEW_REF}
    result = seed.run(reader, mapping, fetcher=make_fetcher(),
                      baseline_min=FLOOR, now=NOW)
    assert result.seed_sql is None
    assert any('snapshot digest mismatch' in b for b in result.blockers)


def test_malformed_revision_blocks():
    item = make_row(1)
    item['revision'] = 'sha256:zzz'
    with pytest.raises(seed.SeedError):
        run_ok([item], make_mapping())


def test_deterministic_digests_across_runs():
    items = [make_row(n) for n in range(1, 6)]
    r1 = run_ok(items, make_mapping())
    r2 = run_ok(items, make_mapping())
    assert r1.seed_sql == r2.seed_sql
    assert r1.manifest['corpus_sha256'] == r2.manifest['corpus_sha256']
    assert r1.manifest['seed_sql_sha256'] == r2.manifest['seed_sql_sha256']


def test_row_revision_change_changes_snapshot_freeze():
    items = [make_row(n) for n in range(1, 4)]
    r1 = run_ok(items, make_mapping())
    items[0]['row']['caption'] = 'edited after census'
    items[0]['revision'] = rev_of(items[0]['row'])
    r2 = run_ok(items, make_mapping())
    assert r1.manifest['historical_snapshot_sha256'] != \
        r2.manifest['historical_snapshot_sha256']


# ---------------------------------------------------------------------------
# Media occurrence preservation
# ---------------------------------------------------------------------------

def test_image_and_thumbnail_occurrences_both_preserved():
    items = [make_row(1, thumb=True)]
    padded = pad_items(items)
    result = run_ok(items, make_mapping())
    obs = [l for l in result.seed_sql.splitlines() if 'cdn.example.com' in l]
    assert any('/img/1.jpg' in l for l in obs)
    assert any('/thumb/1.jpg' in l for l in obs)
    assert result.manifest['observations']['count'] == occurrence_count(padded)


def test_duplicate_url_across_rows_not_collapsed():
    items = [make_row(1), make_row(2)]
    items[1]['row']['image_url'] = items[0]['row']['image_url']
    items[1]['revision'] = rev_of(items[1]['row'])
    result = run_ok(items, make_mapping())
    assert result.manifest['observations']['count'] == occurrence_count(pad_items(items))
    assert result.seed_sql.count(items[0]['row']['image_url']) >= 2


def test_fetch_called_for_every_occurrence():
    items = [make_row(n, thumb=True) for n in range(1, 4)]
    calls = []

    def fetch(url):
        calls.append(url)
        return make_fetcher()(url)

    result = run_ok(items, make_mapping(), fetcher=fetch)
    assert result.blockers == []
    assert len(calls) == occurrence_count(pad_items(items))


def test_unsupported_media_field_blocks():
    result = run_ok([make_row(1, extra={'video_url': 'https://cdn.example.com/v.mp4'})],
                    make_mapping())
    assert result.seed_sql is None
    assert any('unsupported gallery/video' in b for b in result.blockers)


def test_empty_object_alternate_media_blocks():
    # '{}' is NOT an empty alternate-media value and must fail closed.
    result = run_ok([make_row(1, extra={'image_urls': {}})], make_mapping())
    assert result.seed_sql is None
    assert any('unsupported gallery/video' in b for b in result.blockers)


def test_empty_array_and_string_alternate_media_allowed():
    items = [make_row(1, extra={'image_urls': [], 'media_urls': ''}),
             make_row(2, extra={'slide_urls': None})]
    result = run_ok(items, make_mapping())
    assert result.blockers == []


def test_undeclared_thumbnail_blocks():
    result = run_ok([make_row(1, thumb=True)],
                    make_mapping(thumb_declared=False))
    assert result.seed_sql is None
    assert any('thumbnail_url not declared' in b for b in result.blockers)


def test_thumbnail_only_row_fails_closed_no_seed_sql():
    # image_url null + non-empty thumbnail_url can never satisfy migration
    # completeness (no-media requires BOTH urls null), so preparation must
    # fail closed with no seed SQL.
    result = run_ok([make_row(1, image=False, thumb=True)], make_mapping())
    assert result.seed_sql is None
    assert result.readback_sql is None
    assert result.manifest['status'] == 'INCOMPLETE_DO_NOT_APPLY'
    assert any('thumbnail_url but no image_url' in b for b in result.blockers)


def test_no_media_row_requires_reviewed_evidence():
    blocked = run_ok([make_row(1, image=False)], make_mapping())
    assert blocked.seed_sql is None
    assert any('no-media evidence' in b for b in blocked.blockers)
    ok = run_ok([make_row(1, image=False)],
                make_mapping(no_media=[{'calendar_row_id': rid(1),
                                        'evidence_ref': 'evidence://nomedia/1'}]))
    assert ok.blockers == []
    assert 'no_images_verified' in ok.seed_sql


# ---------------------------------------------------------------------------
# Reviewed census assertion binding (coverage.complete=true evidence)
# ---------------------------------------------------------------------------

def test_missing_census_review_fails_closed():
    with pytest.raises(seed.SeedError):
        run_ok([make_row(1)], make_mapping(), review=False)


def test_stale_census_review_snapshot_blocks():
    mapping = make_mapping()
    mapping['census_review'] = {
        'historical_snapshot_sha256': 'sha256:' + '1' * 64,
        'census_row_count': FLOOR,
        'evidence_ref': REVIEW_REF}
    result = run_ok([make_row(1)], mapping, review=False)
    assert result.seed_sql is None
    assert result.manifest['status'] == 'INCOMPLETE_DO_NOT_APPLY'
    assert any('reviewed census assertion is stale' in b for b in result.blockers)


def test_census_review_count_mismatch_blocks():
    mapping = make_mapping()
    items = pad_items([make_row(1)])
    reader = FakeReader(items)
    mapping['census_review'] = {
        'historical_snapshot_sha256': reader.historical_snapshot(),
        'census_row_count': len(items) + 1,
        'evidence_ref': REVIEW_REF}
    result = seed.run(reader, mapping, fetcher=make_fetcher(),
                      baseline_min=FLOOR, now=NOW)
    assert result.seed_sql is None
    assert any('reviewed census assertion count mismatch' in b
               for b in result.blockers)


def test_census_review_blank_evidence_ref_fails():
    mapping = make_mapping()
    mapping['census_review'] = {
        'historical_snapshot_sha256': 'sha256:' + '1' * 64,
        'census_row_count': 1,
        'evidence_ref': '   '}
    with pytest.raises(seed.SeedError):
        run_ok([make_row(1)], mapping, review=False)


def _section(sql, table):
    return sql.split('insert into public.%s' % table, 1)[1].split(';', 1)[0]


def test_media_coverage_binds_census_review_evidence_not_generic_obs_ref():
    result = run_ok([make_row(1, thumb=True)], make_mapping())
    cov = _section(result.seed_sql, 'exact_byte_history_coverage_20261010')
    assert REVIEW_REF in cov
    assert OBS_REF not in cov
    obs = _section(result.seed_sql, 'exact_byte_history_observation_20261010')
    assert OBS_REF in obs
    assert REVIEW_REF not in obs


def test_coverage_follows_review_ref_even_when_obs_ref_equals_review_ref():
    # If coverage were bound to the generic observation_evidence_ref, setting
    # obs_ref == REVIEW_REF with a DIFFERENT census_review ref would silently
    # pass. It must not: coverage rows must carry the census_review ref.
    mapping = make_mapping()
    mapping['observation_evidence_ref'] = REVIEW_REF
    other = 'evidence://census-review/actual-signoff'
    items = pad_items([make_row(1)])
    r = FakeReader(items)
    mapping['census_review'] = {
        'historical_snapshot_sha256': r.historical_snapshot(),
        'census_row_count': len(items),
        'evidence_ref': other}
    result = seed.run(r, mapping, fetcher=make_fetcher(),
                      baseline_min=FLOOR, now=NOW)
    assert result.blockers == []
    cov = _section(result.seed_sql, 'exact_byte_history_coverage_20261010')
    assert other in cov and REVIEW_REF not in cov
    obs = _section(result.seed_sql, 'exact_byte_history_observation_20261010')
    assert REVIEW_REF in obs and other not in obs


def test_no_media_coverage_keeps_per_row_no_media_evidence():
    nm = 'evidence://nomedia/row-1'
    result = run_ok([make_row(1, image=False)],
                    make_mapping(no_media=[{'calendar_row_id': rid(1),
                                            'evidence_ref': nm}]))
    assert result.blockers == []
    cov = _section(result.seed_sql, 'exact_byte_history_coverage_20261010')
    assert nm in cov and REVIEW_REF in cov


# ---------------------------------------------------------------------------
# Mapping and sibling proof validation
# ---------------------------------------------------------------------------

def test_missing_tenant_mapping_blocks():
    result = run_ok([make_row(1, gym='gym-x')], make_mapping())
    assert result.seed_sql is None
    assert any('missing tenant mapping' in b for b in result.blockers)


def test_ambiguous_tenant_mapping_blocks():
    mapping = make_mapping()
    mapping['tenants'].append(dict(mapping['tenants'][0]))
    with pytest.raises(seed.SeedError):
        run_ok([make_row(1)], mapping)


def test_unknown_timezone_blocks():
    mapping = make_mapping()
    mapping['tenants'][0]['posting_timezone'] = 'Mars/Olympus'
    with pytest.raises(seed.SeedError):
        run_ok([make_row(1)], mapping)


def test_missing_target_mapping_blocks():
    result = run_ok([make_row(1, account='facebook')], make_mapping())
    assert result.seed_sql is None
    assert any('missing provider target mapping' in b for b in result.blockers)


def test_target_missing_evidence_ref_blocks():
    mapping = make_mapping()
    mapping['targets'][0]['evidence_ref'] = '  '
    with pytest.raises(seed.SeedError):
        run_ok([make_row(1)], mapping)


def test_valid_sibling_proof_accepted():
    items, identity, logical, proof, members = sibling_setup()
    mapping = make_mapping(proofs=[proof], members=members)
    result = run_ok(items, mapping)
    assert result.blockers == []
    assert identity in result.seed_sql
    assert 'exact_byte_sibling_member_20261010' in result.seed_sql
    assert logical in result.seed_sql


def test_sibling_member_unknown_proof_rejected():
    mapping = make_mapping(members=[_member(1, str(uuid.uuid4()))])
    result = run_ok([make_row(1)], mapping)
    assert result.seed_sql is None
    assert any('unknown proof' in b for b in result.blockers)


def test_sibling_member_row_outside_census_rejected():
    items, identity, logical, proof, members = sibling_setup()
    members = [_member(999, identity)]
    proof['member_row_ids'] = [rid(999)]
    mapping = make_mapping(proofs=[proof], members=members)
    result = run_ok(items, mapping)
    assert result.seed_sql is None
    assert any('outside the census' in b for b in result.blockers)


def test_sibling_proof_tenant_date_mismatch_rejected():
    items, identity, logical, proof, members = sibling_setup()
    proof['canonical_tenant'] = 'tenant-other'
    mapping = make_mapping(proofs=[proof], members=members)
    result = run_ok(items, mapping)
    assert result.seed_sql is None
    assert any('does not cover the precise' in b for b in result.blockers)


def test_sibling_persisted_logical_post_id_mismatch_rejected():
    items, identity, logical, proof, members = sibling_setup()
    proof['logical_post_id'] = str(uuid.uuid4())  # proof != persisted rows
    mapping = make_mapping(proofs=[proof], members=members)
    result = run_ok(items, mapping)
    assert result.seed_sql is None
    assert any('persisted logical_post_id' in b for b in result.blockers)


def test_sibling_proof_missing_member_row_ids_fails():
    items, identity, logical, proof, members = sibling_setup()
    del proof['member_row_ids']
    mapping = make_mapping(proofs=[proof], members=members)
    with pytest.raises(seed.SeedError):
        run_ok(items, mapping)


def test_sibling_proof_partial_member_ids_rejected():
    items, identity, logical, proof, members = sibling_setup()
    proof['member_row_ids'] = [rid(1)]  # members input lists 1 and 2
    mapping = make_mapping(proofs=[proof], members=members)
    result = run_ok(items, mapping)
    assert result.seed_sql is None
    assert any('do not exactly equal' in b for b in result.blockers)


def test_sibling_proof_extra_member_ids_rejected():
    items, identity, logical, proof, members = sibling_setup()
    proof['member_row_ids'] = [rid(1), rid(2), rid(3)]
    mapping = make_mapping(proofs=[proof], members=members)
    result = run_ok(items, mapping)
    assert result.seed_sql is None
    assert any('do not exactly equal' in b for b in result.blockers)


def test_singleton_sibling_proof_rejected():
    logical = str(uuid.uuid4())
    identity = str(uuid.uuid4())
    items = [make_row(1, date='2026-10-02', logical=logical)]
    proof = _proof(identity, [rid(1)], logical=logical)
    mapping = make_mapping(proofs=[proof], members=[_member(1, identity)])
    result = run_ok(items, mapping)
    assert result.seed_sql is None
    assert any('fewer than two distinct member rows' in b for b in result.blockers)


# ---------------------------------------------------------------------------
# Fetch failure modes and classification separation
# ---------------------------------------------------------------------------

def test_fetch_failure_blocks_seed():
    items = [make_row(1)]
    fetcher = make_fetcher(fail_urls={items[0]['row']['image_url']})
    result = run_ok(items, make_mapping(), fetcher=fetcher)
    assert result.seed_sql is None
    assert any('observation failed' in b for b in result.blockers)


def test_fetcher_invalid_digest_blocks():
    def bad_fetch(url):
        return ('md5:abc', 10)
    result = run_ok([make_row(1)], make_mapping(), fetcher=bad_fetch)
    assert result.seed_sql is None
    assert any('invalid digest' in b for b in result.blockers)


def test_https_fetcher_rejects_non_allowlisted_and_bad_ports():
    fetcher = seed.make_https_fetcher(['cdn.example.com'])
    with pytest.raises(seed.FetchError):
        fetcher('http://cdn.example.com/x.jpg')
    with pytest.raises(seed.FetchError):
        fetcher('https://evil.example.com/x.jpg')
    with pytest.raises(seed.FetchError):
        fetcher('https://cdn.example.com:8443/x.jpg')
    with pytest.raises(seed.SeedError):
        seed.make_https_fetcher([])


# --- Injected-mock HTTP behavior over the address-pinned connection seam ---

class _FakePinnedConn:
    """Stands in for _open_pinned_connection: records the pinned peer and
    serves canned HTTP response bytes. No socket or TLS is touched."""

    def __init__(self, response_bytes, record):
        self._response = response_bytes
        self._record = record
        self.sent = b''

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode):
        import io
        return io.BytesIO(self._response)

    def close(self):
        pass


def _http_bytes(status=200, headers=None, body=b'data', reason='OK'):
    lines = ['HTTP/1.1 %d %s' % (status, reason)]
    for k, v in (headers or {}).items():
        lines.append('%s: %s' % (k, v))
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('ascii') + body


def _mock_http(monkeypatch, response_bytes, dns_results=None):
    """Patch DNS (validated once) and the pinned-connection seam. Returns the
    connection record: {'hostname', 'ip'} for every connection attempt."""
    record = {}
    results = list(dns_results) if dns_results else [
        [(2, 1, 6, '', ('93.184.216.34', 443))]]

    def fake_getaddrinfo(*a, **k):
        return results.pop(0) if len(results) > 1 else results[0]

    def fake_open(hostname, ip_address, timeout):
        record['hostname'] = hostname
        record['ip'] = ip_address
        return _FakePinnedConn(response_bytes, record)

    monkeypatch.setattr(seed.socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(seed, '_open_pinned_connection', fake_open)
    return record


def test_https_fetcher_redirect_rejected(monkeypatch):
    _mock_http(monkeypatch, _http_bytes(
        status=302, reason='Found',
        headers={'Location': 'https://x/'}, body=b''))
    fetcher = seed.make_https_fetcher(['cdn.example.com'])
    with pytest.raises(seed.FetchError):
        fetcher('https://cdn.example.com/x.jpg')


def test_https_fetcher_oversized_content_length_rejected(monkeypatch):
    _mock_http(monkeypatch, _http_bytes(
        headers={'Content-Length': str(seed.MAX_BYTES + 1)}, body=b'x'))
    fetcher = seed.make_https_fetcher(['cdn.example.com'])
    with pytest.raises(seed.FetchError):
        fetcher('https://cdn.example.com/x.jpg')


def test_https_fetcher_duplicate_content_length_rejected(monkeypatch):
    _mock_http(monkeypatch, b'HTTP/1.1 200 OK\r\n'
               b'Content-Length: 4\r\nContent-Length: 1\r\n\r\nABCD')
    with pytest.raises(seed.FetchError, match='public object unavailable'):
        seed.make_https_fetcher(['cdn.example.com'])(
            'https://cdn.example.com/x.jpg')


def test_https_fetcher_stream_overrun_rejected(monkeypatch):
    _mock_http(monkeypatch, _http_bytes(body=b'x' * 12))
    fetcher = seed.make_https_fetcher(['cdn.example.com'], max_bytes=10)
    with pytest.raises(seed.FetchError):
        fetcher('https://cdn.example.com/x.jpg')


def test_https_fetcher_stream_success(monkeypatch):
    payload = b'exact bytes'
    record = _mock_http(monkeypatch, _http_bytes(
        headers={'Content-Length': str(len(payload))}, body=payload))
    fetcher = seed.make_https_fetcher(['cdn.example.com'])
    digest, length = fetcher('https://cdn.example.com/x.jpg')
    assert digest == 'sha256:' + hashlib.sha256(payload).hexdigest()
    assert length == len(payload)
    assert record['hostname'] == 'cdn.example.com'
    assert record['ip'] == '93.184.216.34'


def test_https_fetcher_dns_rebinding_cannot_reroute_to_private(monkeypatch):
    """DNS validates to a public address, then a re-resolution would return a
    private address (rebinding). The fetch must connect to the originally
    validated public IP and never consult DNS again."""
    payload = b'pinned bytes'
    record = _mock_http(
        monkeypatch,
        _http_bytes(headers={'Content-Length': str(len(payload))},
                    body=payload),
        dns_results=[
            [(2, 1, 6, '', ('93.184.216.34', 443))],   # validation
            [(2, 1, 6, '', ('10.0.0.7', 443))],        # attacker rebinding
        ])
    calls = []
    real_getaddrinfo = seed.socket.getaddrinfo
    monkeypatch.setattr(seed.socket, 'getaddrinfo',
                        lambda *a, **k: (calls.append(a), real_getaddrinfo(*a, **k))[1])
    fetcher = seed.make_https_fetcher(['cdn.example.com'])
    digest, length = fetcher('https://cdn.example.com/x.jpg')
    assert digest == 'sha256:' + hashlib.sha256(payload).hexdigest()
    assert length == len(payload)
    # Exactly one DNS resolution (validation), and the pinned peer is the
    # validated public address, not the rebound private one.
    assert len(calls) == 1
    assert record['ip'] == '93.184.216.34'
    assert record['ip'] != '10.0.0.7'


def test_https_fetcher_rejects_private_dns_answer(monkeypatch):
    _mock_http(monkeypatch, _http_bytes(body=b'x'),
               dns_results=[[(2, 1, 6, '', ('192.168.1.10', 443))]])
    fetcher = seed.make_https_fetcher(['cdn.example.com'])
    with pytest.raises(seed.FetchError):
        fetcher('https://cdn.example.com/x.jpg')


def test_observation_kind_is_never_provider_receipt():
    result = run_ok([make_row(1, thumb=True)], make_mapping())
    assert result.manifest['observations']['observation_kind'] == 'db_published_url_bytes'
    assert 'provider_receipt_bytes' not in result.seed_sql
    assert 'platform_readback_bytes' not in result.seed_sql
    assert 'Never' in result.manifest['observations']['classification']


# ---------------------------------------------------------------------------
# Seed/readback SQL guard structure (pre/post-apply, single transaction)
# ---------------------------------------------------------------------------

def test_seed_sql_pre_and_post_guards_wrap_inserts_in_one_transaction():
    result = run_ok([make_row(1, thumb=True)], make_mapping())
    sql = result.seed_sql
    assert 'exact_byte_gate' not in sql
    assert sql.count('\nbegin;') == 1 and sql.rstrip().endswith('commit;')
    assert 'set transaction isolation level read committed' in sql
    pre = sql.index("refusing blind retry or partial-seed reuse")
    first_insert = sql.index('insert into public.')
    post = sql.index("post-insert seed row counts do not match")
    commit = sql.rindex('commit;')
    assert pre < first_insert < post < commit
    # pre-apply guards: frozen snapshot and expect-zero table emptiness
    assert 'exact_byte_historical_snapshot_20261010()' in sql
    for table in seed.SEED_TABLES:
        assert '(select count(*) from public.%s) <> 0' % table in sql
    # post-apply guards: exact counts, corpus digest, completeness, snapshot
    total = result.manifest['census']['total_rows']
    assert ('(select count(*) from public.exact_byte_history_coverage_20261010)'
            ' <> %d' % total) in sql
    assert 'exact_byte_corpus_digest_20261010()' in sql
    assert 'exact_byte_history_complete_20261010() is not true' in sql
    assert 'historical snapshot changed during seed apply' in sql
    assert 'raise exception' in sql


def test_readback_sql_raises_and_never_reads_gate():
    result = run_ok([make_row(1)], make_mapping())
    rb = result.readback_sql
    assert 'begin read only;' in rb and rb.rstrip().endswith('rollback;')
    assert 'raise exception' in rb
    assert 'exact_byte_gate' not in rb
    total = result.manifest['census']['total_rows']
    assert '<> %d' % total in rb
    for needle in ('corpus digest mismatch', 'historical snapshot digest mismatch',
                   'history completeness is not true',
                   'row revision drift', 'historical predicate',
                   'same-revision coverage row', 'db_published_url_bytes',
                   'unknown IANA timezone'):
        assert needle in rb


# ---------------------------------------------------------------------------
# Incomplete state never yields a usable seed artifact
# ---------------------------------------------------------------------------

def test_incomplete_manifest_marks_unusable_and_freezes_nothing():
    result = run_ok([make_row(1, gym='unknown')], make_mapping())
    assert result.seed_sql is None
    assert result.readback_sql is None
    assert result.manifest['status'] == 'INCOMPLETE_DO_NOT_APPLY'
    assert result.manifest['seed_sql_sha256'] is None
    assert result.manifest['readback_sql_sha256'] is None
    assert 'corpus_sha256' not in result.manifest


def test_seed_sql_lock_order_timeouts_and_guards():
    result = run_ok([make_row(1)], make_mapping())
    sql = result.seed_sql
    i_begin = sql.index('begin;')
    i_iso = sql.index('set transaction isolation level read committed;')
    i_lock_to = sql.index("set local lock_timeout = '5s';")
    i_stmt_to = sql.index("set local statement_timeout = '15min';")
    i_adv = sql.index("pg_advisory_xact_lock(")
    i_key = sql.index("hashtextextended('exact_byte_send_20261010', 0)")
    i_lock = sql.index('lock table public.content_calendar in share mode;')
    i_pre = sql.index('historical snapshot changed since the seed freeze')
    i_insert = sql.index('insert into public.exact_byte_tenant_20261010')
    i_post = sql.index('post-insert seed row counts')
    i_commit = sql.rindex('commit;')
    # Timeouts are set before any lock; the advisory lock precedes the SHARE
    # table lock (the exact order of exact_byte_activate_20261010, so the two
    # paths serialize instead of deadlocking); locks precede the pre-guard,
    # inserts, post-guard and commit.
    assert i_begin < i_iso < i_lock_to < i_stmt_to < i_adv < i_key < i_lock \
        < i_pre < i_insert < i_post < i_commit
    assert sql.rstrip().endswith('commit;')


def test_seed_sql_uses_migration_advisory_key_and_share_lock():
    # The exact key/order is the one the migration's send/activation path uses.
    mig = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'migrations',
        'delivered_byte_send_fence_20261010.sql')).read()
    key = "pg_advisory_xact_lock(hashtextextended('exact_byte_send_20261010',0))"
    assert key in mig
    assert mig.index(key) < mig.index(
        'lock table public.content_calendar in share mode;')
    result = run_ok([make_row(1)], make_mapping())
    assert "hashtextextended('exact_byte_send_20261010', 0)" in result.seed_sql


def test_generated_sql_uses_only_insert_authority_tables():
    result = run_ok([make_row(1, thumb=True)], make_mapping())
    for stmt in (l for l in result.seed_sql.splitlines() if l.startswith('insert')):
        assert any(t in stmt for t in seed.SEED_TABLES), stmt
    sql_body = '\n'.join(l for l in result.seed_sql.lower().splitlines()
                         if not l.startswith('--'))
    for word in ('exact_byte_gate_20261010', 'exact_byte_cutover_20261010',
                 'exact_byte_send_attempt_20261010', 'exact_byte_activate',
                 'update ', 'delete ', 'drop ', 'truncate '):
        assert word not in sql_body


def test_readonly_reader_performs_no_writes():
    reader = FakeReader(pad_items([make_row(1)]))
    mapping = make_mapping()
    mapping['census_review'] = {
        'historical_snapshot_sha256': reader.historical_snapshot(),
        'census_row_count': len(reader.items), 'evidence_ref': REVIEW_REF}
    seed.run(reader, mapping, fetcher=make_fetcher(), baseline_min=FLOOR, now=NOW)
    assert reader.writes == []


def test_manifest_records_every_observation_occurrence_and_checksums():
    items = [make_row(1, thumb=True), make_row(2)]
    padded = pad_items(items)
    result = run_ok(items, make_mapping())
    occ = result.manifest['observations']['occurrences']
    assert len(occ) == occurrence_count(padded)
    roles = {}
    for o in occ:
        for key in ('observation_id', 'calendar_row_id', 'row_revision', 'role',
                    'exact_url', 'sha256', 'byte_length', 'observation_kind',
                    'evidence_ref'):
            assert key in o and o[key] not in (None, '')
        assert o['role'] in ('image', 'thumbnail')
        assert o['observation_kind'] == 'db_published_url_bytes'
        roles[(o['calendar_row_id'], o['role'])] = o
    assert (rid(1), 'image') in roles and (rid(1), 'thumbnail') in roles
    assert seed.SHA256_RE.match(result.manifest['seed_sql_sha256'])
    assert seed.SHA256_RE.match(result.manifest['readback_sql_sha256'])
    assert result.manifest['reviewed_census_assertion']['evidence_ref'] == REVIEW_REF


# ---------------------------------------------------------------------------
# CLI output-directory and artifact permission guards
# ---------------------------------------------------------------------------

def _load_cli():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'scripts', 'exact_byte_history_seed_20261010.py')
    spec = importlib.util.spec_from_file_location('exact_byte_seed_cli', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_rejects_stale_output_directory(tmp_path):
    cli = _load_cli()
    stale = tmp_path / 'out'
    stale.mkdir()
    (stale / 'exact_byte_history_seed_20261010.sql').write_text('-- stale')
    with pytest.raises(seed.SeedError):
        cli.prepare_output_dir(str(stale))
    not_a_dir = tmp_path / 'file'
    not_a_dir.write_text('x')
    with pytest.raises(seed.SeedError):
        cli.prepare_output_dir(str(not_a_dir))


def test_cli_creates_owner_only_dir_and_files(tmp_path):
    cli = _load_cli()
    out = tmp_path / 'fresh'
    cli.prepare_output_dir(str(out))
    assert os.stat(out).st_mode & 0o777 == 0o700
    artifact = out / 'a.sql'
    cli.write_artifact(str(artifact), 'select 1;\n')
    assert os.stat(artifact).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cli.write_artifact(str(artifact), 'select 2;\n')
    # An existing EMPTY directory is accepted but forced to 0700.
    empty = tmp_path / 'empty'
    empty.mkdir(mode=0o755)
    cli.prepare_output_dir(str(empty))
    assert os.stat(empty).st_mode & 0o777 == 0o700


def test_cli_main_fails_before_any_write_on_stale_out(tmp_path, capsys):
    cli = _load_cli()
    mapping = tmp_path / 'mapping.json'
    mapping.write_text(json.dumps(make_mapping()))
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'stale.sql').write_text('-- stale')
    rc = cli.main(['--mapping', str(mapping), '--out', str(out),
                   '--allowed-host', 'cdn.example.com'])
    assert rc == 2
    assert (out / 'stale.sql').read_text() == '-- stale'
    assert len(os.listdir(out)) == 1


# ---------------------------------------------------------------------------
# jsonb rendering sanity (digests are re-validated in SQL at readback)
# ---------------------------------------------------------------------------

def test_pg_jsonb_text_key_order_and_escapes():
    assert seed.pg_jsonb_text({'bb': 1, 'a': 2}) == '{"a": 2, "bb": 1}'
    assert seed.pg_jsonb_text({'x': 'a"b\n'}) == '{"x": "a\\"b\\n"}'
    assert seed.pg_jsonb_text([True, None, 'é']) == '[true, null, "é"]'
    assert seed.pg_jsonb_text(3) == '3'


def test_sql_literal_escaping():
    mapping = make_mapping()
    mapping['tenants'][0]['canonical_tenant'] = "o'brien gym"
    result = run_ok([make_row(1)], mapping)
    assert "o''brien gym" in result.seed_sql


def test_per_account_counts_every_census_row_despite_blockers():
    """per_account is the fresh census by account, not the builder's
    mapping-sensitive counter: rows blocked by a missing tenant/target
    mapping still appear in manifest.census.per_account."""
    items = [make_row(1, gym='gym-a', account='instagram'),
             make_row(2, gym='gym-b', account='facebook'),
             make_row(3, gym='gym-b', account='facebook')]
    result = run_ok(items, make_mapping(gyms=('gym-a',)), pad=False)
    assert result.manifest['status'] == 'INCOMPLETE_DO_NOT_APPLY'
    assert result.seed_sql is None
    assert any('missing tenant mapping' in b for b in result.blockers)
    assert result.manifest['census']['total_rows'] == 3
    assert result.manifest['census']['per_account'] == {
        'facebook': 2, 'instagram': 1}


def test_https_fetcher_bounds_slow_dns_without_opening_connection(monkeypatch):
    import threading
    import time

    release = threading.Event()
    finished = threading.Event()
    connected = []

    def slow_dns(*args, **kwargs):
        try:
            release.wait(2)
            return [(2, 1, 6, '', ('93.184.216.34', 443))]
        finally:
            finished.set()

    monkeypatch.setattr(seed.socket, 'getaddrinfo', slow_dns)
    monkeypatch.setattr(seed, '_open_pinned_connection',
                        lambda *args: connected.append(args))
    started = time.monotonic()
    try:
        with pytest.raises(seed.FetchError, match='bounded public object'):
            seed.make_https_fetcher(['cdn.example.com'], timeout=0.05)(
                'https://cdn.example.com/x.jpg')
        elapsed = time.monotonic() - started
        assert 0.04 <= elapsed < 0.5
        assert not finished.is_set()  # The caller did not wait for libc DNS.
        assert not connected
    finally:
        release.set()
        assert finished.wait(1)
    assert not connected  # Finishing DNS after timeout cannot start a fetch.


@pytest.mark.parametrize('response', [
    b'HTTP/1.1 200 OK\r\n' + b'X: a\r\n' * (seed.MAX_HEADER_COUNT + 1)
        + b'\r\ndata',
    b'HTTP/1.1 200 OK\r\n' + (b'X: ' + b'a' * 7000 + b'\r\n') * 10
        + b'\r\ndata',
    b'HTTP/1.1 200 OK\r\nX: ' + b'a' * seed.MAX_HEADER_LINE_BYTES
        + b'\r\n\r\ndata',
])
def test_https_fetcher_caps_header_count_total_bytes_and_line(monkeypatch, response):
    _mock_http(monkeypatch, response)
    with pytest.raises(seed.FetchError, match='bounded public object'):
        seed.make_https_fetcher(['cdn.example.com'])(
            'https://cdn.example.com/x.jpg')


@pytest.mark.parametrize('prefix,fragment', [
    (b'HTTP/1.1 200 OK\r\nX: ', b'a'),  # A single unterminated header.
    (b'HTTP/1.1 200 OK\r\n', b'X: a\r\n'),  # Cumulative header loop.
    (b'HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n', b'a'),
    (b'HTTP/1.1 200 OK\r\n\r\n', b'a'),  # Body without Content-Length.
])
def test_https_fetcher_bounds_trickled_headers_and_body(monkeypatch, prefix, fragment):
    import socket
    import threading
    import time

    _mock_http(monkeypatch, b'')
    client, server = socket.socketpair()
    stop = threading.Event()

    def trickle():
        try:
            server.recv(8192)  # Consume the request before responding.
            server.sendall(prefix)
            while not stop.wait(0.01):
                server.sendall(fragment)
        except OSError:
            pass
        finally:
            server.close()

    worker = threading.Thread(target=trickle, daemon=True)
    worker.start()
    monkeypatch.setattr(seed, '_open_pinned_connection', lambda *args: client)
    started = time.monotonic()
    try:
        with pytest.raises(seed.FetchError):
            seed.make_https_fetcher(['cdn.example.com'], timeout=0.08)(
                'https://cdn.example.com/x.jpg')
        elapsed = time.monotonic() - started
        assert 0.06 <= elapsed < 0.5
        assert client.fileno() == -1
    finally:
        stop.set()
        client.close()
        worker.join(1)
        assert not worker.is_alive()
