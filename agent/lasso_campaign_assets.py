"""Dated user-supplied LASSO artwork and its separately composed Stories.

The manifest carries approved copy and hosted originals, never generated claims.
No uploads, scheduling writes or approval changes occur in this module.
"""
import hashlib
import json
from pathlib import Path

from . import config
from .drafter import Draft

MANIFEST = Path(__file__).resolve().parents[1] / 'brand_voice' / 'lasso_calendar_campaign.json'


def wrap_builders(account, builders, story, sprint_feed, sprint_story, manifest=None):
    key = getattr(account, 'key', account)
    if not config.lasso_editorial_calendar_enabled() or key not in ('lasso','lasso_ig','lasso_fb'):
        return builders, story, sprint_feed, sprint_story
    if manifest is None:
        if not MANIFEST.exists():
            return builders, story, sprint_feed, sprint_story
        manifest = json.loads(MANIFEST.read_text())
    entries = manifest.get('assets', [])
    by_id = {e['id']:e for e in entries}
    by_slot = {(e['date'], e['slot_index']):e for e in entries}
    if len(by_id) != len(entries) or len(by_slot) != len(entries):
        raise ValueError('Duplicate campaign asset or dated slot')
    for e in entries:
        if e['category'] not in ('book','summit') or not all(e.get(k) for k in ('caption','feed_url','story_url')):
            raise ValueError('Incomplete campaign original or Story')

    def make(entry, day, is_story=False):
        url = entry['story_url' if is_story else 'feed_url']
        d = Draft(
            draft_id=hashlib.sha256(f"{key}|{day}|{entry['id']}|{is_story}".encode()).hexdigest()[:16],
            account_key=key, platform=getattr(account,'platform','instagram'),
            caption='' if is_story else entry['caption'], hashtags=[],
            creative_path=url.rsplit('/',1)[-1], creative_public_url=url,
            scheduled_for=day, day_key=day, category=entry['category'],
            is_story=is_story, draft_type='story' if is_story else 'feed',
            source_fragments=[entry['caption']])
        d.campaign_asset_id=entry['id']
        return d

    result={}
    for category, builder in builders.items():
        def feed(target, day, cat=category, original=builder):
            candidates=[e for e in entries if e['date']==day and e['category']==cat and not e.get('is_sprint')]
            if len(candidates)>1:
                raise ValueError('Multiple campaign assets for a non-sprint category/day')
            return make(candidates[0],day) if candidates else original(target,day)
        result[category]=feed
    def paired(target, day, draft):
        entry=by_id.get(getattr(draft,'campaign_asset_id',''))
        return make(entry,day,True) if entry else story(target,day,draft)
    def sprint(target,day,index):
        entry=by_slot.get((day,index))
        return make(entry,day) if entry and entry.get('is_sprint') else sprint_feed(target,day,index)
    def sprint_pair(target,day,index,draft):
        entry=by_id.get(getattr(draft,'campaign_asset_id',''))
        return make(entry,day,True) if entry else sprint_story(target,day,index,draft)
    return result, paired, sprint, sprint_pair
