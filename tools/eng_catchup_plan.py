"""Offline plan only: explicit cadence-approved slots + complete future inventory.

Input: {timezone, posts_per_day, as_of, rows, slots}. slots are free-or-reserved
cadence slots {platform, format, scheduled_at}; rows are an authoritative export.
Does not import agent modules, access credentials, contact services or write DBs.
"""
import argparse
from datetime import datetime
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    args = parser.parse_args()
    with open(args.input) as f:
        print(json.dumps(plan(json.load(f)), indent=2))
