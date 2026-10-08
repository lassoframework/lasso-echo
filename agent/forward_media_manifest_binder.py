"""Default-OFF service-role binder lane for forward-media render manifests.

This lane does exactly one thing: discover unsent active content_calendar
rows that have a complete explicit persisted source/group/media identity and
no render manifest digest yet, then ask the database authority
public.fixer_bind_forward_media_manifest_20261006(uuid) to bind the persisted
manifest digest. The RPC accepts ONLY the persisted calendar row ID and
itself derives and verifies owner authority, clearance, and the matching
immutable manifest. This module never supplies a digest, hash, URL, claim
token or owner credential, never connects to the owner or attester DSN, and
never edits approval, status, claims or media. Any RPC refusal, network or
configuration failure is a visible static hold.

A parallel STAGED lane (run_staged_once / --staged) discovers SQL-authorized
staged candidates and binds their exact persisted manifest digest through the
staged-specific RPC fixer_bind_forward_schedule_staged_manifest_20261008 only;
the active bind RPC stays active-only.

The lane is inert unless BOTH the fleet guard (AGENT_FORWARD_MEDIA_GUARD) and
the explicit worker flag (AGENT_FORWARD_MEDIA_BINDER_WORKER) are enabled AND a
nonempty tenant allowlist (AGENT_FORWARD_MEDIA_BINDER_TENANTS) is configured.
Nothing runs on import.
"""
from __future__ import annotations

import argparse
import os
import re
import threading
import uuid

from . import forward_media_guard as guard

TENANTS_ENV = 'AGENT_FORWARD_MEDIA_BINDER_TENANTS'
WORKER_ENV = 'AGENT_FORWARD_MEDIA_BINDER_WORKER'
RPC_NAME = 'fixer_bind_forward_media_manifest_20261006'
STAGED_PENDING_RPC = 'fixer_forward_schedule_staged_binder_pending_20261008'
STAGED_BIND_RPC = 'fixer_bind_forward_schedule_staged_manifest_20261008'

_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_SELECT = ('id,gym_id,status,variant_status,post_date,visual_group_key,'
           'source_media_asset_id,source_media_url,image_url,thumbnail_url,'
           'render_manifest_digest,publish_claim_token,published_at,late_post_id')
# Mirrors the RPC's unsent-active precondition as a read-only prefilter; the
# RPC remains the sole authority and re-verifies everything server-side.
_CANDIDATE_STATUSES = ('draft', 'pending', 'queued', 'approved')
_HTTPS = re.compile(r'https://\S+\Z')
_STAGED_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_STAGED_POST_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')


class ForwardMediaBinderHold(RuntimeError):
    """Static reason code only; never carries credential values or URLs."""


def _integer(env, name, default, minimum, maximum):
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        raise ForwardMediaBinderHold('worker_bounds_invalid') from None
    if not minimum <= value <= maximum:
        raise ForwardMediaBinderHold('worker_bounds_invalid')
    return value


def tenants_from_environment(env=None):
    """Nonempty, deduplicated, syntax-checked allowlist or a static hold."""
    env = os.environ if env is None else env
    tenants = tuple(dict.fromkeys(
        t.strip() for t in env.get(TENANTS_ENV, '').split(',') if t.strip()))
    if not tenants or len(tenants) > 32 or any(
            not _TENANT.fullmatch(t) for t in tenants):
        raise ForwardMediaBinderHold('explicit_tenant_allowlist_required')
    return tenants


def binder_enabled():
    return (guard.enabled()
            and os.getenv(WORKER_ENV, '').lower() in ('1', 'true', 'yes', 'on'))


def _row_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ForwardMediaBinderHold('persisted row identity invalid') from None


def _identity_complete(row):
    """Read-only prefilter for the explicit persisted identity the RPC demands."""
    if not isinstance(row, dict):
        return False
    if row.get('status') not in _CANDIDATE_STATUSES:
        return False
    if row.get('variant_status') != 'active' or not row.get('post_date'):
        return False
    if (row.get('publish_claim_token') is not None
            or row.get('published_at') is not None
            or row.get('late_post_id') is not None
            or row.get('render_manifest_digest') is not None):
        return False
    if not str(row.get('gym_id') or '').strip():
        return False
    if not str(row.get('visual_group_key') or '').strip():
        return False
    if not str(row.get('source_media_asset_id') or '').strip():
        return False
    if not _HTTPS.fullmatch(str(row.get('source_media_url') or '')):
        return False
    if not _HTTPS.fullmatch(str(row.get('image_url') or '')):
        return False
    thumb = row.get('thumbnail_url')
    if thumb is not None and not _HTTPS.fullmatch(str(thumb)):
        return False
    return True


def _discover_page(store, tenant, cursor, limit):
    """Bounded keyset page of candidate rows for one tenant.

    Keyset (id > cursor, ordered by id) so rows held earlier in the pass are
    passed over instead of starving the scan behind them.
    """
    params = {
        'select': _SELECT,
        'gym_id': f'eq.{tenant}',
        'variant_status': 'eq.active',
        'status': 'in.(' + ','.join(_CANDIDATE_STATUSES) + ')',
        'render_manifest_digest': 'is.null',
        'publish_claim_token': 'is.null',
        'published_at': 'is.null',
        'late_post_id': 'is.null',
        'order': 'id',
        'limit': str(limit),
    }
    if cursor is not None:
        params['id'] = f'gt.{cursor}'
    try:
        response = store._client().get(
            store._rest('content_calendar'),
            params=params, headers=store._headers(), timeout=30)
    except Exception:
        raise ForwardMediaBinderHold('candidate_discovery_unavailable') from None
    if response.status_code >= 400:
        raise ForwardMediaBinderHold('candidate_discovery_unavailable')
    try:
        rows = response.json()
    except Exception:
        raise ForwardMediaBinderHold('candidate_discovery_malformed') from None
    if not isinstance(rows, list) or len(rows) > limit:
        raise ForwardMediaBinderHold('candidate_discovery_malformed')
    previous = cursor
    for row in rows:
        if not isinstance(row, dict) or row.get('gym_id') != tenant:
            raise ForwardMediaBinderHold('candidate_discovery_malformed')
        try:
            row_id = _row_id(row.get('id'))
        except ForwardMediaBinderHold:
            raise ForwardMediaBinderHold('candidate_discovery_malformed') from None
        if previous is not None and row_id <= previous:
            raise ForwardMediaBinderHold('candidate_discovery_malformed')
        previous = row_id
    return rows


def _bind_row(store, row_id):
    """Call the service-role binder RPC with the persisted row UUID only.

    Returns True on a confirmed bind; raises ForwardMediaBinderHold with a
    static reason code on any refusal, malformed reply or transport failure.
    Never sends a digest, hash, URL, claim token or credential in the payload.
    """
    try:
        response = store._client().post(
            store._rest(f'rpc/{RPC_NAME}'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json={'p_calendar_row_id': row_id}, timeout=30)
    except ForwardMediaBinderHold:
        raise
    except Exception:
        raise ForwardMediaBinderHold('rpc_transport_unavailable') from None
    if response.status_code != 200:
        raise ForwardMediaBinderHold('rpc_bind_refused')
    try:
        bound = response.json()
    except Exception:
        raise ForwardMediaBinderHold('rpc_bind_malformed') from None
    if bound is not True:
        raise ForwardMediaBinderHold('rpc_bind_refused')
    return True


def staged_candidates(store, *, env=None, limit=None, tenants=None, after=None):
    """Read-only discovery of SQL-authorized STAGED binder candidates.

    The SQL RPC joins registered nonterminal batch membership, the canonical
    tenant and the exact owner authority tuple, and requires the preparation
    eligibility predicate; this client sends only the tenant allowlist and a
    bound — never a digest, URL, claim token or credential. Binding itself is
    performed only by run_staged_once through the staged-specific bind RPC;
    fixer_bind_forward_media_manifest_20261006 remains active-only and is
    never called for a staged row. The active lane is unchanged.
    """
    if not binder_enabled():
        raise ForwardMediaBinderHold('binder_lane_disabled')
    env = os.environ if env is None else env
    if tenants is None:
        tenants = tenants_from_environment(env)
    if limit is None:
        limit = _integer(env, 'AGENT_FORWARD_MEDIA_BINDER_BATCH_SIZE', 25, 1, 100)
    payload = {'p_tenants': list(tenants), 'p_limit': limit}
    if after is not None:
        # Bounded per-tenant keyset cursor: (post_date, calendar_row_id) of the
        # last attempted candidate. The SQL authority advances past it before
        # its LIMIT so a definitively held row cannot starve later candidates.
        after_date, after_id = after
        payload['p_after_post_date'] = after_date
        payload['p_after_row_id'] = after_id
    try:
        response = store._client().post(
            store._rest(f'rpc/{STAGED_PENDING_RPC}'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json=payload, timeout=30)
    except Exception:
        raise ForwardMediaBinderHold('candidate_discovery_unavailable') from None
    if response.status_code != 200:
        raise ForwardMediaBinderHold('candidate_discovery_unavailable')
    try:
        rows = response.json()
    except Exception:
        raise ForwardMediaBinderHold('candidate_discovery_malformed') from None
    if not isinstance(rows, list) or len(rows) > limit:
        raise ForwardMediaBinderHold('candidate_discovery_malformed')
    admitted = []
    for row in rows:
        if not isinstance(row, dict) or row.get('tenant_id') not in tenants:
            raise ForwardMediaBinderHold('candidate_discovery_malformed')
        digest = row.get('manifest_digest')
        if not isinstance(digest, str) or not _STAGED_DIGEST.fullmatch(digest):
            raise ForwardMediaBinderHold('candidate_discovery_malformed')
        post_date = row.get('post_date')
        if not isinstance(post_date, str) or not _STAGED_POST_DATE.fullmatch(post_date):
            raise ForwardMediaBinderHold('candidate_discovery_malformed')
        try:
            row_id = _row_id(row.get('calendar_row_id'))
            batch_id = _row_id(row.get('batch_id'))
        except ForwardMediaBinderHold:
            raise ForwardMediaBinderHold('candidate_discovery_malformed') from None
        admitted.append({'calendar_row_id': row_id, 'batch_id': batch_id,
                         'tenant_id': row['tenant_id'], 'post_date': post_date,
                         'manifest_digest': digest})
    return admitted


def _bind_staged_row(store, row_id):
    """Call the staged-specific bind RPC with the persisted row UUID only.

    Same contract as _bind_row: True on a confirmed bind, a static hold on any
    refusal, malformed reply or transport failure. Never sends a digest, hash,
    URL, claim token or credential; the SQL authority derives and verifies the
    exact manifest from persisted owner evidence and batch membership.
    """
    try:
        response = store._client().post(
            store._rest(f'rpc/{STAGED_BIND_RPC}'),
            headers=store._headers({'Content-Type': 'application/json'}),
            json={'p_calendar_row_id': row_id}, timeout=30)
    except ForwardMediaBinderHold:
        raise
    except Exception:
        raise ForwardMediaBinderHold('rpc_transport_unavailable') from None
    if response.status_code != 200:
        raise ForwardMediaBinderHold('rpc_bind_refused')
    try:
        bound = response.json()
    except Exception:
        raise ForwardMediaBinderHold('rpc_bind_malformed') from None
    if bound is not True:
        raise ForwardMediaBinderHold('rpc_bind_refused')
    return True


def run_staged_once(*, store=None, env=None, limit=None, held=None, cursors=None,
                    tenant_offset=0, logger=None):
    """One bounded STAGED binder pass: SQL-authorized discovery then bind.

    Binds the staged manifest digest to each exact staged row BEFORE
    finalization so the finalizer sees the trusted preparation output field
    filled. Any refusal is a visible static hold. Discovery is a bounded
    per-tenant keyset scan: the cursor advances past every attempted row
    (including refused ones) so a definitively held candidate is never
    re-selected ahead of valid rows, and an exhausted tenant wraps (cursor
    cleared) so a later pass revisits it once its evidence is repaired. The
    active lane is never used.
    """
    log = logger if logger is not None else (lambda _msg: None)
    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    env = os.environ if env is None else env
    tenants = tenants_from_environment(env)
    batch = limit
    if batch is None:
        batch = _integer(env, 'AGENT_FORWARD_MEDIA_BINDER_BATCH_SIZE', 25, 1, 100)
    held = {} if held is None else held
    cursors = {} if cursors is None else cursors
    summary = {'bound': 0, 'held': 0, 'scanned': 0, 'attempted': 0}
    order = [tenants[(tenant_offset + i) % len(tenants)] for i in range(len(tenants))]
    for tenant in order:
        if summary['attempted'] >= batch:
            break
        cursor = cursors.get(tenant)
        candidates = staged_candidates(store, env=env,
                                       limit=batch - summary['attempted'],
                                       tenants=(tenant,), after=cursor)
        summary['scanned'] += len(candidates)
        if not candidates:
            # Wrap: restart this tenant's keyset next pass so a row whose
            # owner evidence was repaired is discovered again.
            cursors.pop(tenant, None)
            continue
        for candidate in candidates:
            if summary['attempted'] >= batch:
                break
            rid = candidate['calendar_row_id']
            cursors[tenant] = (candidate['post_date'], rid)
            if rid in held:
                continue
            summary['attempted'] += 1
            try:
                _bind_staged_row(store, rid)
            except ForwardMediaBinderHold as exc:
                held[rid] = str(exc)
                summary['held'] += 1
                log(f'forward-media staged binder row={rid} status=hold reason={exc}')
            else:
                summary['bound'] += 1
                log(f'forward-media staged binder row={rid} status=bound')
    return summary


def run_staged_forever(*, stop=None, sleep=None, store=None, logger=print):
    """Optional staged run loop; single pass semantics come from run_staged_once."""
    import time
    stop = stop or threading.Event()
    sleep = sleep or time.sleep
    env = os.environ
    interval = _integer(env, 'AGENT_FORWARD_MEDIA_BINDER_INTERVAL_SECONDS', 60, 5, 900)
    cursors, offset = {}, 0
    while not stop.is_set():
        try:
            run_staged_once(store=store, held={}, cursors=cursors,
                            tenant_offset=offset, logger=logger)
        except ForwardMediaBinderHold as exc:
            logger(f'forward-media staged binder status=hold reason={exc}')
        offset += 1
        sleep(interval)


def run_once(*, store=None, env=None, cursors=None, held=None,
             tenant_offset=0, logger=None):
    """One bounded, tenant-fair pass. Returns a small status summary.

    `store` is injectable for offline tests; the default is the existing
    Supabase service-role calendar store (never the owner or attester DSN).
    `cursors` maps tenant -> last seen row id. `held` is only for one bounded
    pass; the service loop clears it before the next pass so a row can recover
    after its owner evidence becomes available.
    """
    log = logger if logger is not None else (lambda _msg: None)
    if not binder_enabled():
        raise ForwardMediaBinderHold('binder_lane_disabled')
    tenants = tenants_from_environment(env)
    env = os.environ if env is None else env
    batch = _integer(env, 'AGENT_FORWARD_MEDIA_BINDER_BATCH_SIZE', 25, 1, 100)
    page = _integer(env, 'AGENT_FORWARD_MEDIA_BINDER_PAGE_SIZE', 50, 1, 200)
    max_pages = _integer(env, 'AGENT_FORWARD_MEDIA_BINDER_MAX_PAGES', 4, 1, 20)
    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    cursors = {} if cursors is None else cursors
    held = {} if held is None else held

    summary = {'bound': 0, 'held': 0, 'scanned': 0, 'attempted': 0}
    order = [tenants[(tenant_offset + i) % len(tenants)] for i in range(len(tenants))]
    for tenant in order:
        if summary['attempted'] >= batch:
            break
        cursor = cursors.get(tenant)
        for _ in range(max_pages):
            if summary['attempted'] >= batch:
                break
            rows = _discover_page(store, tenant, cursor, page)
            if not rows:
                cursors.pop(tenant, None)  # wrap: restart keyset next pass
                break
            for row in rows:
                if summary['attempted'] >= batch:
                    break
                cursor = row['id']
                cursors[tenant] = cursor
                summary['scanned'] += 1
                try:
                    rid = _row_id(row.get('id'))
                except ForwardMediaBinderHold as exc:
                    log(f'forward-media binder row=invalid reason={exc}')
                    continue
                if rid in held:
                    continue
                if not _identity_complete(row):
                    held[rid] = 'incomplete_persisted_identity'
                    summary['held'] += 1
                    log(f'forward-media binder row={rid} status=hold '
                        f'reason=incomplete_persisted_identity')
                    continue
                summary['attempted'] += 1
                try:
                    _bind_row(store, rid)
                except ForwardMediaBinderHold as exc:
                    held[rid] = str(exc)
                    summary['held'] += 1
                    log(f'forward-media binder row={rid} status=hold reason={exc}')
                else:
                    summary['bound'] += 1
                    log(f'forward-media binder row={rid} status=bound')
        # Wraparound restart: a fully consumed tenant starts over next pass so
        # newly eligible rows are found; held rows are skipped via `held`.
    return summary


def run_forever(*, stop=None, sleep=None, store=None, logger=print):
    """Optional run loop; single pass semantics come from run_once."""
    import time
    stop = stop or threading.Event()
    sleep = sleep or time.sleep
    env = os.environ
    interval = _integer(env, 'AGENT_FORWARD_MEDIA_BINDER_INTERVAL_SECONDS', 60, 5, 900)
    cursors, offset = {}, 0
    while not stop.is_set():
        try:
            run_once(store=store, cursors=cursors, held={},
                     tenant_offset=offset, logger=logger)
        except ForwardMediaBinderHold as exc:
            logger(f'forward-media binder status=hold reason={exc}')
        offset += 1
        sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Default-OFF forward-media manifest binder lane (one pass).')
    parser.add_argument('--loop', action='store_true',
                        help='keep polling on the configured interval')
    parser.add_argument('--staged', action='store_true',
                        help='run the staged-candidate lane instead of the active lane')
    args = parser.parse_args(argv)
    if not binder_enabled():
        print('forward-media binder status=hold reason=binder_lane_disabled')
        return 2
    if args.loop:
        if args.staged:
            run_staged_forever()
        else:
            run_forever()
        return 0
    try:
        summary = (run_staged_once(logger=print) if args.staged
                   else run_once(logger=print))
    except ForwardMediaBinderHold as exc:
        print(f'forward-media binder status=hold reason={exc}')
        return 2
    print('forward-media binder status=ok '
          f'bound={summary["bound"]} held={summary["held"]} '
          f'scanned={summary["scanned"]}')
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
