"""Bounded read-only diagnostics. Never run ops actions or initialize the worker DB."""
from __future__ import annotations
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
KEY = r"([A-Za-z0-9][A-Za-z0-9_-]{2,79})"
CAL_FIELDS = ('id', 'gym_id', 'post_date', 'time_slot', 'format', 'status', 'pillar',
              'caption', 'created_at', 'updated_at', 'variant_status')
GEN_FIELDS = ('id', 'draft_id', 'account_key', 'kind', 'headline', 'cta', 'engine',
              'model', 'route', 'grade_status', 'grade_reason', 'attempt', 'final_status', 'created_at')
MAX_BYTES = 128 * 1024

class EvidenceError(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)

def gym_from_ticket(ticket):
    if ticket.get('product') != 'echo' or ticket.get('source') != 'ops_fix':
        raise EvidenceError('unsupported_ticket', 409)
    text = str(ticket.get('raw_text') or '').strip()
    text = re.sub(r'^OPS-FIX REQUEST:\s*', '', text)
    text = re.sub(r'^ECHO ALERT:\s*', '', text)
    patterns = (rf'^calendar grade(?: DROPPED)?:\s*{KEY}\s+forward book\b',
                rf'^GRADE-STUCK\s+{KEY}:\s*the forward book\b')
    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            return match.group(1)
    raise EvidenceError('unrecognized_grade_alert', 409)

def scrub(value):
    # Caption text is untrusted; bounded output excludes credentials and URLs.
    if isinstance(value, str):
        value = re.sub(r'https?://[^\s<>]+', '[URL omitted]', value)
        value = re.sub(r'(?:sk-|xox[baprs]-|gh[pousr]_)[A-Za-z0-9_-]+', '[secret omitted]', value)
        value = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[secret omitted]', value)
        return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', value)[:2000]
    return value if value is None or isinstance(value, (int, float, bool)) else None

def read_rest(table, params):
    """Fixed callers choose tables/columns. No caller-supplied SQL, paths, or tokens."""
    from . import config
    import requests
    url, key = config.supabase_url(), config.supabase_service_key()
    if not url or not key:
        raise EvidenceError('store_unavailable')
    started = time.monotonic()
    with requests.get(f'{url}/rest/v1/{table}', params=params,
                      headers={'apikey': key, 'Authorization': f'Bearer {key}'},
                      timeout=(3, 5), stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            raise EvidenceError('store_unavailable')
        data = bytearray()
        for part in response.iter_content(8192):
            data.extend(part)
            if len(data) > 2 * MAX_BYTES or time.monotonic() - started > 8:
                raise EvidenceError('source_too_large_or_slow')
        rows = json.loads(data)
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise EvidenceError('source_shape_invalid')
        return rows

def resolve_gym(gym, read):
    # Use the authoritative settings alias map. No fuzzy/display-name lookup.
    rows = read('echo_intake_tokens', {'echo_account_key': f'eq.{gym}',
                'select': 'gym_id,echo_account_key', 'limit': '2'})
    ids = {str(r.get('gym_id') or '') for r in rows
           if gym == r.get('echo_account_key')}
    if len(ids) == 1 and '' not in ids:
        return next(iter(ids))
    # Older configured accounts use a registry base (e.g. district_h).
    # Do not use accounts._load_registry_rows: its error path posts an alert.
    from . import config
    try:
        with open(config.gym_registry_path(), 'rb') as handle:
            data = handle.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise EvidenceError('registry_too_large')
        records = json.loads(data)
        if not isinstance(records, list):
            raise EvidenceError('registry_unavailable')
        matches = {str(r.get('gym_id') or '') for r in records
                   if isinstance(r, dict) and r.get('base') == gym}
        if not rows and len(matches) == 1 and '' not in matches:
            return next(iter(matches))
    except (OSError, ValueError):
        pass
    raise EvidenceError('gym_unconfirmed', 409)

def local_history(gym, db_file=None):
    from . import db
    p = Path(db_file or db.db_path()).resolve()
    if not p.is_file():
        raise EvidenceError('generation_store_unavailable')
    # Existing sqlite only: db.connect() initializes/migrates and is forbidden here.
    conn = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + 3
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        conn.execute('PRAGMA query_only=ON')
        # Exact base and documented IG/FB accounts, never other tenant prefixes.
        keys = [gym, gym + '_ig', gym + '_fb']
        columns = ','.join(GEN_FIELDS)
        rows = conn.execute(f'SELECT {columns} FROM generation_records WHERE account_key IN (?,?,?) ORDER BY id DESC LIMIT 51', keys).fetchall()
        records = [dict(r) for r in rows[:50]]
        return {'status': 'available' if records else 'empty', 'rows': records,
                'limit': 50, 'more_available': len(rows) > 50}
    finally:
        conn.close()

def optional(fn, reason):
    try:
        return fn()
    except Exception:
        return {'status': 'unavailable', 'reason': reason}

def gather(ticket_id, *, deps=None, now=None):
    deps = deps or {}
    if not UUID.fullmatch(ticket_id):
        raise EvidenceError('bad_ticket_id', 400)
    read = deps.get('read', read_rest)
    tickets = read('support_tickets', {'id': f'eq.{ticket_id}', 'select': 'id,product,source,client_id,raw_text,status', 'limit': '1'})
    if len(tickets) != 1 or tickets[0].get('id') != ticket_id:
        raise EvidenceError('ticket_not_found', 404)
    ticket = tickets[0]
    gym = gym_from_ticket(ticket)
    gym_id = deps.get('resolve', lambda g: resolve_gym(g, read))(gym)
    if not gym_id or (ticket.get('client_id') and ticket['client_id'] not in (gym, gym_id)):
        raise EvidenceError('ticket_tenant_mismatch', 409)
    captured = now or datetime.now(timezone.utc)
    start, end = captured.date(), captured.date() + timedelta(days=30)
    rows = read('content_calendar', {'gym_id': f'eq.{gym}', 'post_date': [f'gte.{start}', f'lte.{end}'],
                'variant_status': 'eq.active', 'select': ','.join(CAL_FIELDS), 'order': 'post_date.asc,id.asc', 'limit': '501'})
    if len(rows) > 500:
        raise EvidenceError('calendar_too_large', 409)
    if any(r.get('gym_id') != gym or not start.isoformat() <= str(r.get('post_date', '')) <= end.isoformat() for r in rows):
        raise EvidenceError('calendar_scope_mismatch', 409)
    def previous():
        records = read('gym_social_grades', {'gym_id': f'eq.{gym}', 'window': 'eq.forward_book',
                       'select': 'gym_id,total,letter,graded_at', 'order': 'graded_at.desc', 'limit': '1'})
        if any(r.get('gym_id') != gym for r in records):
            raise EvidenceError('grade_tenant_mismatch')
        return {'status': 'available' if records else 'empty', 'record': {
            k: scrub(v) for k, v in records[0].items()} if records else None,
            'meaning': 'latest stored grade, not necessarily the previous alert generation'}
    def media():
        assets = read('media_asset', {'gym_id': f'eq.{gym}', 'select': 'id,gym_id,kind,eligible,excluded_by_coach,last_used_at,used_count', 'limit': '1001'})
        if len(assets) > 1000 or any(a.get('gym_id') != gym for a in assets):
            raise EvidenceError('media_scope_or_size')
        # Only use the selector with an already-confirmed local array; no network fallback.
        from .gym_media_selector import pickable
        class Store:
            def available(self):
                return True
            def list_assets(self, _gym):
                if _gym != gym:
                    raise EvidenceError('media_tenant_mismatch')
                return assets
        available = pickable(gym, store=Store(), now=captured)
        return {'status': 'available' if assets else 'empty', 'total_assets': len(assets),
                'pickable_count': len(available),
                'pickable_photos': sum(a.get('kind') == 'photo' for a in available),
                'pickable_videos': sum(a.get('kind') == 'video' for a in available)}
    history = optional(lambda: deps.get('history', local_history)(gym), 'generation_store_unavailable')
    if history.get('status') in ('available', 'empty'):
        records = history.get('rows')
        if not isinstance(records, list) or len(records) > 50 or any(r.get('account_key') not in (gym,gym+'_ig',gym+'_fb') for r in records):
            raise EvidenceError('generation_scope_mismatch', 409)
        history['rows'] = [{k: scrub(r.get(k)) for k in GEN_FIELDS if k in r} for r in records]
    result = {'ok': True, 'schema_version': 1, 'ticket_id': ticket_id, 'gym_key': gym, 'gym_id': gym_id,
              'captured_at': captured.isoformat(), 'window': {'start_date': str(start), 'end_date': str(end), 'days': 31},
              'calendar': {'status': 'available', 'rows': [{k: scrub(r.get(k)) for k in CAL_FIELDS if k in r} for r in rows], 'row_count': len(rows)},
              'current_grade': {'status': 'unavailable', 'reason': 'No new grade computed: the live grader consults additional brand/source state. Use raw rows and latest stored grade.'},
              'previous_grade': optional(previous, 'stored_grade_unavailable'),
              'media': optional(media, 'media_inventory_unavailable'), 'generation_history': history}
    if len(json.dumps(result).encode()) > MAX_BYTES:
        raise EvidenceError('snapshot_too_large', 409)
    return result
