"""Default-OFF runtime bridge for fresh gym Astra originals.

The scheduler is a publisher process and cannot open the dedicated owner DSN.
Its queue-only dispatch requests dates; the isolated owner atomically binds
empty dates to persisted calendar rows. The row runner uses the existing owner,
Astra, reviewer and R2 adapters; it never approves or clears calendar holds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import uuid

from . import generated_infographic_preparation as prep

FLAG = 'AGENT_GENERATED_INFOGRAPHIC_RUNTIME'
JOURNAL = '/data/generated-infographic-jobs.sqlite'
PREFIX = 'generated-astra:'


class RuntimeHold(RuntimeError):
    """Static diagnostic only, never provider/credential/private source data."""


def enabled():
    return os.getenv(FLAG, '').strip().lower() in ('1', 'true', 'yes', 'on')


def journal_path():
    # One durable journal for every creator and send-boundary reader. Never
    # fall back to the repository, /tmp or a different SQLite job journal.
    path = os.path.realpath(os.getenv('AGENT_GENERATED_INFOGRAPHIC_JOURNAL', JOURNAL))
    if (not path.startswith('/data/') or not os.path.isdir('/data')
            or not os.path.ismount('/data')):
        raise RuntimeHold('generated_durable_journal_unavailable')
    return path


def verified_palette(base, account_key):
    """Read the existing controlled gym palette file with its evidence revision.

    Source notes plus a first-party source URL (or recorded owner approval) are
    required. A bare URL/list cannot supply owner verified-palette authority.
    Revision covers the full evidence file, so edits invalidate prepared art.
    """
    from . import astra_prompt as ap
    if account_key not in (base + '_ig', base + '_fb') or ap._account_base(account_key) != base:
        raise RuntimeHold('generated_account_gym_mismatch')
    loaded = ap.load_gym_brand_palette(account_key)
    if not loaded:
        raise RuntimeHold('generated_palette_unverified')
    try:
        with open(loaded['path'], 'rb') as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError()
        raw = json.loads(data)
        if (not isinstance(raw, dict) or not
                (raw.get('owner_approved') is True or
                 (isinstance(raw.get('source_url'), str) and raw['source_url'].startswith('https://')
                  and isinstance(raw.get('source_note'), str) and raw['source_note'].strip()))):
            raise ValueError()
        # Load again from the identical source to detect a concurrent file edit.
        if ap.load_gym_brand_palette(account_key) != loaded:
            raise ValueError()
        with open(loaded['path'], 'rb') as stream:
            if stream.read(65537) != data:
                raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        raise RuntimeHold('generated_palette_unverified') from None
    evidence = 'brand-colors:sha256:' + hashlib.sha256(data).hexdigest()
    palette = dict(gym_id=base, verified=True, colors=loaded['colors'], evidence_ref=evidence)
    return palette, 'sha256:' + prep.digest(palette)


def _account_binding(base, account):
    key = getattr(account, 'key', None)
    if (key not in (base + '_ig', base + '_fb') or
            getattr(account, 'platform', None) !=
            ('instagram' if key == base + '_ig' else 'facebook_page')):
        raise RuntimeHold('generated_account_gym_mismatch')
    return key


def approved_copy(base, caption, sources):
    """Exact currently approved source and immutable metadata revision."""
    source = next((item for item in sources(base + '_ig')
                   if getattr(item, 'account_key', None) in (base, base + '_ig')
                   and getattr(item, 'status', None) == 'approved'
                   and getattr(item, 'text', None) == caption), None)
    if source is None or not isinstance(getattr(source, 'id', None), int) or source.id <= 0:
        raise RuntimeHold('generated_approved_copy_receipt_missing')
    fields = ('id', 'account_key', 'category', 'text', 'citation', 'status', 'created_at')
    revision = 'client-source:sha256:' + prep.digest({k: getattr(source, k, None) for k in fields})
    copy = dict(headline=' '.join(caption.split()[:8]), facts=[caption], cta='', footer='')
    return copy, revision


class OwnerSnapshotLoader:
    """Compose DB owner facts with exact approved copy and controlled palette.

    This minimal route accepts a caption that exactly equals a currently approved
    same-gym source. Rephrased captions require a persisted approved copy receipt;
    no producer flag or successful figure-only check manufactures that receipt.
    Reads finish before provider/storage work; the final owner rechecks DB facts.
    """
    def __init__(self, persistence, *, sources=None, palette_loader=None, reader=None):
        from . import client_sources
        self.persistence = persistence
        self.sources = sources or client_sources.approved_sources
        self.palette_loader = palette_loader or verified_palette
        self.reader = reader or persistence._reader.read

    def load(self, row_id, base, account, *, observe_history=True):
        from . import forward_media_guard as guard
        key = _account_binding(base, account)
        try:
            snap = guard.generated_snapshot(self.persistence, row_id)
        finally:
            self.persistence._conn.rollback()
        if (not isinstance(snap, dict) or snap.get('gym_id') != base
                or snap.get('account') != ('instagram' if key.endswith('_ig') else 'facebook')
                or snap.get('format') != 'feed'):
            raise RuntimeHold('generated_calendar_binding_changed')
        copy = snap.get('copy')
        if (not isinstance(copy, dict) or copy.get('gym_id') != base
                or any(copy.get(k) != snap.get(k) for k in ('local_date', 'logical_post_id', 'group_key'))
                or not isinstance(copy.get('caption'), str) or not copy['caption'].strip()):
            raise RuntimeHold('generated_copy_binding_changed')
        caption = copy['caption']
        graphic_copy, source_revision = approved_copy(base, caption, self.sources)
        palette, palette_revision = self.palette_loader(base, key)
        snap = {**snap, 'copy': graphic_copy, 'copy_approved': True, 'copy_verified': True,
                'copy_digest': prep.digest(graphic_copy), 'palette': palette,
                'palette_verified': True, 'palette_digest': prep.digest(palette),
                'palette_revision': palette_revision, 'approved_source_revision': source_revision}
        binding = {k: snap.get(k) for k in prep.BINDING_FIELDS}
        prep.validated_binding(binding)
        prep.checked_snapshot(binding, snap)
        history = snap.get('history')
        if (not isinstance(history, dict) or history.get('scope_complete') is not True
                or history.get('spine_digest') != snap.get('history_revision')
                or not isinstance(history.get('rows'), list) or len(history['rows']) > 10000):
            raise RuntimeHold('generated_history_uncertain')
        visuals = []
        if observe_history:
            seen, observed = set(), {}
            from .visual_scene import scene_fingerprint, normalize_scene
            import re
            for item in history['rows']:
                if (not isinstance(item, dict) or not item.get('history_key')
                        or item['history_key'] in seen or not item.get('published_binding_ref')):
                    raise RuntimeHold('generated_history_uncertain')
                seen.add(item['history_key'])
                proof = item.get('history_proof_ref')
                if proof is not None:
                    # This is returned ONLY by the dedicated owner DB snapshot.
                    # Never accept caller cache flags or recompute authority from
                    # URLs. SQL joins the exact tuple and sealed baseline epoch.
                    if (not isinstance(proof, str) or not re.fullmatch(
                            r'generated-history:sha256:[0-9a-f]{64}', proof)
                            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(item.get('visual_sha256')))
                            or not isinstance(item.get('phash'), str)
                            or normalize_scene(item['phash']) != item['phash']):
                        raise RuntimeHold('generated_history_cache_invalid')
                    visuals.append(dict(item))
                    continue
                url = item.get('visual_url')
                if url not in observed:
                    data = self.reader(url)
                    if not isinstance(data, bytes) or not 0 < len(data) <= prep.MAX_BYTES:
                        raise RuntimeHold('generated_history_bytes_unavailable')
                    sha = 'sha256:' + hashlib.sha256(data).hexdigest()
                    phash = scene_fingerprint(data)
                    if not phash:
                        raise RuntimeHold('generated_history_bytes_unavailable')
                    observed[url] = (sha, phash)
                sha, phash = observed[url]
                if item.get('visual_sha256') not in (None, sha):
                    raise RuntimeHold('generated_history_bytes_changed')
                visuals.append({**item, 'visual_sha256': sha, 'phash': phash})
        return snap, visuals


def _generation_binding(request, snapshot):
    binding = {**request, 'copy_digest': prep.digest(snapshot['copy']),
               'palette_digest': prep.digest(snapshot['palette']), 'review_policy_id': prep.POLICY}
    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'echo-astra:' + prep.canonical(binding)))
    return job_id, binding


def _runtime_record(jobs, row_id, *, binding=None, source_revision=None, state=None):
    """Persist row->job BEFORE provider claim; retain incomplete A jobs too."""
    with sqlite3.connect(jobs.path, timeout=10) as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('CREATE TABLE IF NOT EXISTS generated_runtime_rows ('
                    'row_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, state TEXT NOT NULL)')
        columns = {r[1] for r in con.execute('PRAGMA table_info(generated_runtime_rows)')}
        if 'binding' not in columns:
            con.execute('ALTER TABLE generated_runtime_rows ADD COLUMN binding TEXT')
        if 'source_revision' not in columns:
            con.execute('ALTER TABLE generated_runtime_rows ADD COLUMN source_revision TEXT')
        row = con.execute('SELECT job_id,state,binding,source_revision FROM generated_runtime_rows WHERE row_id=?',
                          (row_id,)).fetchone()
        if binding is not None:
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'echo-astra:' + prep.canonical(binding)))
            if row and row[0] != job_id:
                raise RuntimeHold('generated_row_job_changed')
            if not row:
                con.execute('INSERT INTO generated_runtime_rows(row_id,job_id,state,binding,source_revision) VALUES (?,?,?,?,?)',
                            (row_id, job_id, 'ready', prep.canonical(binding), source_revision))
                row = (job_id, 'ready', prep.canonical(binding), source_revision)
        if state is not None:
            if not row:
                raise RuntimeHold('generated_row_job_unavailable')
            con.execute('UPDATE generated_runtime_rows SET state=? WHERE row_id=?', (state, row_id))
            row = (row[0], state, row[2], row[3])
        if not row:
            return None
        if row[1] not in ('ready', 'committing', 'committed'):
            raise RuntimeHold('generated_row_job_unavailable')
        job = con.execute('SELECT binding,state,candidate FROM generated_jobs WHERE job_id=?',
                          (row[0],)).fetchone()
        frozen_binding = json.loads(row[2]) if row[2] else (json.loads(job[0]) if job else None)
        if frozen_binding is None:
            raise RuntimeHold('generated_row_job_unavailable')
        candidate = prep.validate_candidate(json.loads(job[2])) if job and job[1] == 'prepared' else None
        return dict(state=row[1], job_id=row[0], binding=frozen_binding,
                    job_state=job[1] if job else 'unstarted', candidate=candidate, source_revision=row[3])



def _same_post_job(jobs, row_id, frozen, source_revision):
    """Pin logical IG/FB siblings to one existing execution across spine drift.

    No journal fact authorizes reservation. Current owner facts, strict original
    validation and B's sealed history epoch checks still run for each row.
    """
    with sqlite3.connect(jobs.path, timeout=10) as con:
        rows = con.execute('SELECT row_id,binding,source_revision FROM generated_runtime_rows WHERE row_id<>?',
                           (row_id,)).fetchall()
    matches = []
    keys = tuple(k for k in frozen if k != 'history_revision')
    for other_id, raw, revision in rows:
        if raw is None:
            continue
        binding = json.loads(raw)
        if not all(binding.get(k) == frozen.get(k) for k in ('gym_id','local_date','logical_post_id')):
            continue
        if revision != source_revision or any(binding.get(k) != frozen.get(k) for k in keys):
            raise RuntimeHold('generated_logical_binding_changed')
        matches.append(_runtime_record(jobs, other_id))
    if not matches:
        return None
    if len({r['job_id'] for r in matches}) != 1:
        raise RuntimeHold('generated_logical_job_ambiguous')
    if any(r['state'] == 'committing' for r in matches):
        raise RuntimeHold('generated_owner_commit_uncertain')
    if any(r['job_state'] == 'generating' for r in matches):
        raise RuntimeHold('generated_execution_pending_reconciliation')
    return _runtime_record(jobs, row_id, binding=matches[0]['binding'], source_revision=source_revision)


def run_calendar_row(base, account, row_id, *, persistence, loader=None, jobs=None,
                     provider=None, reviewer=None, storage=None):
    """Generate one exact persisted feed row and acknowledge owner reservation.

    Missing/changed photos, palette, copy and delivered-byte history hold before
    paid work. A lost commit stays ambiguous; generation journal prevents a new
    provider execution on retry. Successful return establishes reservation only.
    """
    if not enabled():
        return dict(ok=False, held=True, reason='generated_runtime_disabled')
    committing = False
    try:
        _account_binding(base, account)
        if str(uuid.UUID(row_id)) != row_id:
            raise RuntimeHold('generated_calendar_binding_changed')
        from . import forward_media_guard as guard, forward_media_owner as owner
        if not guard.enabled():
            raise RuntimeHold('generated_forward_authority_disabled')
        from .forward_media_owner_worker import settings_from_environment
        tenants, _ = settings_from_environment()
        if base not in tenants:
            raise RuntimeHold('generated_owner_tenant_not_allowed')
        if type(persistence) is not owner.ForwardMediaOwnerPersistence:
            raise RuntimeHold('generated_owner_required')
        loader = loader or OwnerSnapshotLoader(persistence)
        jobs = jobs or prep.SQLiteGenerationJobs(journal_path())
        existing = _runtime_record(jobs, row_id)
        if existing and existing['state'] == 'committing':
            raise RuntimeHold('generated_owner_commit_uncertain')
        if existing and existing['job_state'] == 'generating':
            raise RuntimeHold('generated_execution_pending_reconciliation')
        before, visuals = loader.load(row_id, base, account)
        request = {k: before[k] for k in prep.BINDING_FIELDS}
        expected_job, frozen = _generation_binding(request, before)
        if not existing:
            existing = _same_post_job(jobs, row_id, frozen, before['approved_source_revision'])
        if existing and existing['source_revision'] != before['approved_source_revision']:
            raise RuntimeHold('generated_approved_source_changed')
        if existing and existing['candidate'] is None and existing['binding'] != frozen:
            # An ambiguous/reviewing execution remains pinned even if unrelated
            # history changes. Never let a new history revision create a new job.
            raise RuntimeHold('generated_execution_pending_reconciliation')
        if (not existing or existing['candidate'] is None) and (provider is None or reviewer is None):
            from .image_engine import OPENAI_API_KEY_ENV
            key = os.getenv(OPENAI_API_KEY_ENV, '')
            if not key:
                raise RuntimeHold('generated_astra_unavailable')
            from .infographic_review import AstraReviewer
            provider = provider or prep.AstraOriginalProvider(key)
            reviewer = reviewer or AstraReviewer(key)
        if not existing:
            existing = _runtime_record(jobs, row_id, binding=frozen,
                                       source_revision=before['approved_source_revision'])
        result = (dict(ok=True, candidate=existing['candidate']) if existing['candidate'] is not None else
                  prep.prepare_candidate(request, before, jobs=jobs, provider=provider,
                                         reviewer=reviewer, storage=storage, enabled=True))
        if not result.get('ok'):
            return result
        candidate = result['candidate']
        # Enforce A's strict job/storage/original shape before B's DB seam.
        data = loader.reader(candidate.get('original_url'))
        prep.validate_candidate(candidate, data)
        current, _ = loader.load(row_id, base, account, observe_history=False)
        # A successful reservation adds its own historical row. Reuse the
        # exact durable job and let B revalidate current history under locks;
        # never regenerate because this row's reservation changed the spine.
        bindings = tuple(k for k in prep.BINDING_FIELDS if existing['candidate'] is None or k != 'history_revision')
        if any(current.get(k) != candidate.get(k) for k in
               (*bindings, 'copy_digest', 'palette_digest')):
            raise RuntimeHold('generated_snapshot_binding_changed')
        if current['approved_source_revision'] != existing['source_revision']:
            raise RuntimeHold('generated_approved_source_changed')
        if candidate['job_id'] != existing['job_id']:
            raise RuntimeHold('generated_row_job_changed')
        reservation = guard.reserve_generated(persistence, row_id, candidate, current,
                                              history_visuals=visuals, read_bytes=loader.reader)
        _runtime_record(jobs, row_id, state='committing')
        committing = True
        persistence._conn.commit()
        _runtime_record(jobs, row_id, state='committed')
        return dict(ok=True, reserved=True, calendar_row_id=row_id,
                    job_id=candidate['job_id'], receipt_ref=reservation.get('receipt_ref'))
    except Exception as exc:
        if committing:
            return dict(ok=False, held=True, reason='generated_owner_commit_uncertain')
        try:
            persistence._conn.rollback()
        except Exception:
            pass
        reason = str(exc) if isinstance(exc, (RuntimeHold, prep.PreparationHold)) else 'generated_runtime_unavailable'
        return dict(ok=False, held=True, reason=reason)


def run_scheduled(base, account, store, *, logger=None, now=None, days_ahead=2):
    """Dispatch empty photo-depleted dates, never calendar writes or owner DSN."""
    if not enabled():
        return dict(ok=False, held=True, filled=0, reason='generated_runtime_disabled')
    try:
        _account_binding(base, account)
        from . import client_infographic_fill as fill, config
        state, detail = fill.real_media_status(base, now=now)
        if state == fill.MEDIA_AVAILABLE:
            return dict(ok=True, dispatched=0, filled=0, reason='generated_photo_available')
        if state != fill.MEDIA_DEPLETED:
            raise RuntimeHold('generated_photo_inventory_uncertain')
        if store is None or not all(callable(getattr(store, name, None))
                                    for name in ('_client','_rest','_headers','list_month')):
            raise RuntimeHold('generated_gap_owner_transport_missing')
        gaps = fill._empty_upcoming_days(store, base, config.posting_timezone_for(base),
                                         min(max(int(days_ahead),1),2), now=now)
        dispatched = []
        from .accounts import get_account
        for day in gaps:
            # A same-gym connected FB mirror gets its own row request with the
            # exact same logical-post identity derived by the owner.
            platforms = ['instagram']
            facebook = get_account(base+'_fb')
            if facebook is not None:
                _account_binding(base, facebook)
                platforms.append('facebook')
            for platform in platforms:
                request_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                    'echo-generated-gap-request:'+base+':'+day+':'+platform+':feed'))
                payload = dict(p_request_id=request_id,p_gym=base,p_local_date=day,
                               p_account=platform,p_format='feed')
                response = store._client().post(store._rest('rpc/fixer_generated_gap_dispatch_20261007'),
                    headers=store._headers({'Content-Type':'application/json'}),json=payload,timeout=30)
                result = response.json()
                if (not 200<=response.status_code<300 or not isinstance(result,dict)
                        or result.get('dispatched') is not True or result.get('request_id') != request_id
                        or any(result.get(k)!=v for k,v in dict(gym_id=base,local_date=day,
                                                              account=platform,format='feed').items())):
                    raise RuntimeHold('generated_gap_dispatch_unavailable')
                dispatched.append(request_id)
        return dict(ok=True, dispatched=len(dispatched), request_ids=dispatched, filled=0)
    except Exception as exc:
        reason = str(exc) if isinstance(exc,RuntimeHold) else 'generated_gap_dispatch_unavailable'
        if logger:
            logger(f'{base}: fresh infographic held ({reason})')
        return dict(ok=False, held=True, filled=0, reason=reason)


def _sql_publish_readback(store, row_id):
    """Narrow read-only reservation binding; the publisher's only authority.

    The owner SQLite journal lives on the owner's own /data volume and is never
    consulted here. Any unavailable/malformed readback fails closed.
    """
    if store is None:
        raise RuntimeHold('generated_publish_binding_unavailable')
    try:
        response = store._client().post(
            store._rest('rpc/fixer_generated_publish_readback_20261007'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json={'p_id': str(uuid.UUID(str(row_id)))}, timeout=30)
        payload = response.json()
        if not 200 <= response.status_code < 300:
            raise ValueError()
        return payload
    except RuntimeHold:
        raise
    except Exception:
        raise RuntimeHold('generated_publish_binding_unavailable') from None


def validate_publish_palette(row, *, store=None, readback=None, palette_loader=None, sources=None):
    """Check the persisted SQL publish binding and current palette at every
    generated provider boundary.

    The immutable generated reservation readback is the sole publisher
    authority for job/row/gym/account/date/logical/group/original/manifest and
    the owner-approved source/copy/palette refs; the local owner journal grants
    no publish authority and is not read at this boundary. Missing SQL
    readback, identity drift, or a CURRENT approved source/palette mismatch
    fails closed. DB lineage and owned-claim checks remain mandatory.
    """
    asset = str(row.get('source_media_asset_id') or '')
    if not asset.startswith(PREFIX):
        return True
    if not enabled():
        raise RuntimeHold('generated_runtime_disabled')
    from . import forward_media_guard
    if not forward_media_guard.enabled():
        raise RuntimeHold('generated_forward_authority_disabled')
    try:
        job_id = asset[len(PREFIX):]
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError()
        row_id = str(uuid.UUID(str(row.get('id'))))
        binding = readback(row_id) if readback is not None else _sql_publish_readback(store, row_id)
        suffix = {'instagram': '_ig', 'facebook': '_fb'}[row['account']]
        if (not isinstance(binding, dict)
                or binding.get('job_id') != job_id
                or binding.get('calendar_row_id') != row_id
                or binding.get('gym_id') != row.get('gym_id')
                or binding.get('account') != row.get('account')
                or binding.get('local_date') != row.get('post_date')
                or binding.get('logical_post_id') != row.get('logical_post_id')
                or binding.get('group_key') != row.get('visual_group_key')
                or binding.get('original_url') != row.get('image_url')
                or row.get('image_url') != row.get('source_media_url')
                or row.get('thumbnail_url') is not None or row.get('format') != 'feed'
                or binding.get('manifest_digest') != row.get('render_manifest_digest')
                or any(not isinstance(binding.get(key), str) or not binding[key]
                       for key in ('source_revision', 'copy_digest',
                                   'palette_revision', 'palette_digest', 'receipt_ref'))):
            raise ValueError()
        from . import client_sources
        copy, source_revision = approved_copy(row['gym_id'], row.get('caption'),
                                              sources or client_sources.approved_sources)
        if (binding['source_revision'] != source_revision
                or binding['copy_digest'] != prep.digest(copy)):
            raise RuntimeHold('generated_approved_source_changed')
        palette, revision = (palette_loader or verified_palette)(row['gym_id'], row['gym_id'] + suffix)
        if binding['palette_revision'] != revision or binding['palette_digest'] != prep.digest(palette):
            raise RuntimeHold('generated_palette_changed')
    except RuntimeHold:
        raise
    except Exception:
        raise RuntimeHold('generated_publish_binding_unavailable') from None
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--scan', action='store_true')
    parser.add_argument('--gym')
    parser.add_argument('--account')
    parser.add_argument('--row')
    args = parser.parse_args(argv)
    if not enabled():
        report = dict(ok=False, held=True, reason='generated_runtime_disabled')
    else:
        persistence = None
        try:
            from .accounts import get_account
            from .forward_media_owner import ForwardMediaOwnerPersistence
            persistence = ForwardMediaOwnerPersistence.connect_from_environment()
            if args.scan:
                from .generated_infographic_gap_owner import run_pending
                report = run_pending(persistence=persistence)
            elif all((args.gym,args.account,args.row)):
                report = run_calendar_row(args.gym, get_account(args.account), args.row,
                                          persistence=persistence)
            else:
                report = dict(ok=False,held=True,reason='generated_gap_request_invalid')
        except Exception:
            report = dict(ok=False, held=True, reason='generated_owner_environment_unavailable')
        finally:
            if persistence is not None:
                persistence._conn.close()
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get('ok') or not enabled() else 2


if __name__ == '__main__':
    raise SystemExit(main())
