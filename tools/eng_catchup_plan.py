"""Offline plan only: explicit cadence-approved slots + complete future inventory.

Input: {timezone, posts_per_day, as_of, rows, slots}. slots are free-or-reserved
cadence slots {platform, format, scheduled_at}; rows are an authoritative export.
Does not import agent modules, access credentials, contact services or write DBs.
"""
import argparse
from datetime import datetime, timedelta, time, date, timezone
import json
from zoneinfo import ZoneInfo


def stamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timestamps must include timezone offsets')
    return dt


def plan(data, expected=37):
    tz = ZoneInfo(data['timezone'])
    now = stamp(data['as_of'])
    cadence = data['posts_per_day']
    if cadence not in (1, 2):
        raise ValueError('confirm effective cadence: 1 or 2')
    rows = data['rows']
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('duplicate row IDs')
    blocked = [r for r in rows if r['gym_id'] == 'eng'
               and r['platform'] in ('facebook', 'instagram')
               and '2026-09-19' <= r['post_date'] <= '2026-09-25'
               and r['status'] == 'approved' and not r.get('late_post_id')
               and r.get('reject_reason') == 'media_asset_review_required']
    if len(blocked) != expected:
        raise ValueError(f'expected {expected} blocked rows, got {len(blocked)}; refresh evidence')
    blocked_ids = {r['id'] for r in blocked}
    occupied = set()
    for r in rows:
        if r['gym_id'] != 'eng':
            raise ValueError('cross-tenant input')
        if r['id'] in blocked_ids or r['status'] not in ('pending', 'approved', 'publishing', 'published'):
            continue
        if r['platform'] not in ('facebook', 'instagram'):
            continue
        if r['post_date'] >= now.astimezone(tz).date().isoformat():
            if not r.get('scheduled_at'):
                raise ValueError('future row lacks effective slot: supply authoritative schedule')
            occupied.add((r['platform'], stamp(r['scheduled_at'])))
    slots, seen, daily = [], set(), {}
    for slot in data['slots']:
        dt = stamp(slot['scheduled_at'])
        key = (slot['platform'], dt)
        if key in seen:
            raise ValueError('duplicate platform slot')
        seen.add(key)
        if dt <= now or slot['platform'] not in ('facebook', 'instagram'):
            raise ValueError('slots must be future FB/IG slots')
        day = dt.astimezone(tz).date().isoformat()
        family = 'story' if slot['format'] == 'story' else 'feed'
        capkey = (day, slot['platform'], family)
        daily[capkey] = daily.get(capkey, 0) + 1
        if daily[capkey] > (1 if family == 'story' else cadence):
            raise ValueError('slot inventory exceeds daily cadence')
        slots.append((dt, slot, day))
    # Every existing reservation must be included in the cadence inventory so it
    # cannot silently consume extra daily capacity outside the proposed slots.
    if not occupied <= seen:
        raise ValueError('slot inventory omits existing reservations')
    slots.sort(key=lambda s: s[0])
    result = []
    for row in sorted(blocked, key=lambda r: (r['post_date'], r['id'])):
        family = 'story' if row['format'] == 'story' else 'feed'
        for dt, slot, day in slots:
            slotfamily = 'story' if slot['format'] == 'story' else 'feed'
            key = (row['platform'], dt)
            if slot['platform'] != row['platform'] or slotfamily != family or key in occupied:
                continue
            occupied.add(key)
            result.append({'id': row['id'], 'platform': row['platform'],
                           'format': row['format'], 'source_media_asset_id': row.get('source_media_asset_id'),
                           'old_post_date': row['post_date'], 'old_scheduled_at': row.get('scheduled_at'),
                           'post_date': day, 'scheduled_at': dt.isoformat(),
                           'required_status': 'pending', 'requires_human_media_review': True})
            break
        else:
            raise ValueError(f'insufficient free {row["platform"]}/{family} slots; extend horizon')
    return {'gym_id': 'eng', 'timezone': data['timezone'], 'rows': result,
            'count': len(result), 'mode': 'offline proposal; no writes'}


def text_rows(text):
    """Parse the supplied read-only pipe export; never infer publication proof."""
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        fields = [v.strip() for v in line.split('|')]
        if len(fields) != 10:
            raise ValueError('expected ten export columns')
        rid, day, platform, fmt, label, idx, scheduled, status, reason, asset = fields
        rows.append(dict(id=rid, gym_id='eng', post_date=day, platform=platform,
                         format=fmt, time_slot=label,
                         slot_index=None if idx == '-' else int(idx),
                         scheduled_at=None if scheduled == '-' else scheduled.replace(' ', 'T') + ':00+00:00',
                         status=status, reject_reason=None if reason == '-' else reason,
                         source_media_asset_id=None if asset == '-' else asset))
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('duplicate row IDs')
    return rows


def forward_plan(rows, review_finished_at):
    """Move the whole export forward, oldest day/slot bundle first.

    One original feed+story bundle per local day keeps each shared media asset
    on one date and avoids the two exported stories colliding at 16:30 UTC.
    Conservative total cap: at most TWO rows per platform/day, including story.
    Future bundles follow every missed bundle. No live state is read or changed.
    """
    cutoff = stamp(review_finished_at).astimezone(timezone.utc)
    tz = ZoneInfo('America/New_York')
    groups, untouched = {}, []
    for row in rows:
        if row['platform'] == 'googlebusiness':
            untouched.append(row['id'])
            continue
        if (row['platform'] not in ('facebook', 'instagram')
                or row['format'] not in ('feed', 'story')
                or row['status'] not in ('approved', 'pending')
                or row.get('late_post_id')):
            raise ValueError('unsupported or already publishing/published row')
        idx = row['slot_index'] if row['slot_index'] is not None else 0
        if idx not in (0, 1):
            raise ValueError('unsupported slot index')
        groups.setdefault((row['post_date'], idx), []).append(row)
    next_day = cutoff.astimezone(tz).date()
    result = []
    asset_days = {}
    for (old_day, idx), group in sorted(groups.items()):
        day = max(next_day, date.fromisoformat(old_day))
        counts, slots = {}, set()
        for row in group:
            counts[row['platform']] = counts.get(row['platform'], 0) + 1
            slot = (row['platform'], row['format'])
            if slot in slots or counts[row['platform']] > 2:
                raise ValueError('bundle exceeds per-platform cadence')
            slots.add(slot)
        def scheduled(row):
            hour = 16 if row['format'] == 'story' else (11 if idx == 0 else 22)
            local = datetime.combine(day, time(hour - 4, 30), tzinfo=tz)
            return local.astimezone(timezone.utc)
        while any(scheduled(r) <= cutoff for r in group):
            day += timedelta(days=1)
        for row in sorted(group, key=lambda r: (r['platform'], r['format'], r['id'])):
            asset = row['source_media_asset_id']
            if asset and asset in asset_days and asset_days[asset] != day:
                raise ValueError('asset reused on different dates; operator review required')
            if asset:
                asset_days[asset] = day
            result.append(dict(row, old_post_date=old_day,
                               old_scheduled_at=row['scheduled_at'],
                               post_date=day.isoformat(), scheduled_at=scheduled(row).isoformat(),
                               proposed_slot_index=idx, required_status='pending'))
        next_day = day + timedelta(days=1)
    return dict(gym_id='eng', timezone='America/New_York', posts_per_day=2,
                review_finished_at=cutoff.isoformat(), count=len(result), rows=result,
                untouched_gbp_ids=untouched,
                review_asset_ids=list(asset_days), mode='offline conditional proposal; no writes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    parser.add_argument('--rows-text', action='store_true', help='shift the full pipe export oldest-first')
    parser.add_argument('--review-finished-at', help='offset-aware review cutoff; required with --rows-text')
    args = parser.parse_args()
    if args.rows_text and not args.review_finished_at:
        parser.error('--rows-text requires --review-finished-at')
    with open(args.input) as f:
        output = (forward_plan(text_rows(f.read()), args.review_finished_at)
                  if args.rows_text else plan(json.load(f)))
    print(json.dumps(output, indent=2))
