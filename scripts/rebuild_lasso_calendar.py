#!/usr/bin/env python3
"""Build a reviewable, resumable LASSO calendar; --apply stages the saved result.

Run inside the worker runtime. This command never calls a publisher or sends Slack
messages. The existing approval and scheduled publisher rules govern saved rows.
"""
import argparse
import calendar
import hashlib
from collections import Counter
import dataclasses
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import config, real_month_run, real_month_planner
from agent.drafter import Draft, DraftStatus


def encode(draft):
    return dict(vars(draft), status=draft.status.value)


def decode(data):
    data = dict(data)
    data['status'] = DraftStatus(data['status'])
    names = {f.name for f in dataclasses.fields(Draft)}
    draft = Draft(**{k:v for k,v in data.items() if k in names})
    for k,v in data.items():
        if k not in names:
            setattr(draft,k,v)
    return draft


def save(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2, default=str))
    temp.replace(path)


def require_reconciled_coverage(rows, start, days):
    combined=Counter((r['post_date'],r['account'],r['format']) for r in rows)
    incomplete=[(str(start+timedelta(days=i)),account,fmt)
                for i in range(days)
                for account,fmt in [('instagram','feed'),('facebook','feed')]
                if combined[(str(start+timedelta(days=i)),account,fmt)]!=2]
    if incomplete:
        raise SystemExit('Reconciled calendar is incomplete; no rows changed: '+str(incomplete))


def main():
    # Maintenance builds do not send slot-level messages.
    os.environ.pop('AGENT_SLACK_BOT_TOKEN', None)
    os.environ['AGENT_OPS_ALERTS_ENABLED'] = 'false'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default=(date.today()+timedelta(days=1)).isoformat())
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.days < 1 or args.days > 31:
        raise SystemExit('Days must be between 1 and 31')
    start = date.fromisoformat(args.start)
    end = start + timedelta(days=args.days - 1)
    if not config.lasso_editorial_calendar_enabled():
        raise SystemExit('AGENT_LASSO_EDITORIAL_CALENDAR is off')
    path = Path(args.checkpoint)
    path.parent.mkdir(parents=True,exist_ok=True)
    import fcntl
    lock = open(str(path)+'.lock','w')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another rebuild owns this checkpoint')
    data = json.loads(path.read_text()) if path.exists() else {
        'gym_id':'lasso','start':args.start,'days':args.days,'cache':{}}
    if data['gym_id']!='lasso' or data['start']!=args.start or data['days']!=args.days:
        raise SystemExit('Checkpoint belongs to a different scope')
    if args.apply:
        if not data.get('drafts'):
            raise SystemExit('Build checkpoint first')
        drafts = [decode(d) for d in data['drafts']]
        coverage = Counter((d.day_key, bool(d.is_story)) for d in drafts)
        missing = [str(start + timedelta(days=i)) for i in range(args.days)
                   if coverage[(str(start + timedelta(days=i)), False)] != 2
                   ]
        if missing:
            raise SystemExit('Incomplete feed coverage; no rows changed: ' + ', '.join(missing))
        if any(not d.creative_public_url or d.status != DraftStatus.PENDING for d in drafts):
            raise SystemExit('Checkpoint contains missing media or nonpending drafts')
        from agent.portal_calendar_store import SupabaseCalendarStore
        class ForwardStore(SupabaseCalendarStore):
            def delete_month(self, account_key, month, **kwargs):
                # Keep historical/current-day machine rows as well as every human row.
                first=date.fromisoformat(month+'-01')
                keep=[str(first+timedelta(days=i))
                      for i in range(calendar.monthrange(first.year, first.month)[1])
                      if not start <= first+timedelta(days=i) <= end]
                keep.extend(kwargs.pop('preserve_dates', ()))
                return super().delete_month(account_key,month,preserve_dates=keep,**kwargs)
        store = ForwardStore()
        months = real_month_planner.plan_span_months(args.start,args.days)
        # Persist the exact pre-apply rows for recovery before the first mutation.
        backup = path.with_name(path.stem + '-before-apply.json')
        if not backup.exists():
            save(backup, {'gym_id':'lasso','months':{
                m:store.list_month('lasso',m) for m in months}})
        # Run the same insertion belts against the post-delete snapshot BEFORE
        # any deletion, so pruning cannot silently turn a complete build into gaps.
        from agent.portal_calendar_store import (preserve_and_prune, _stage_belts,
            _media_stage_belt, _dedupe_slots, _WIPEABLE_STATUSES)
        from agent.plan_horizon import belt_filter
        incoming=real_month_planner.to_calendar_rows(drafts,'lasso')
        incoming,_=preserve_and_prune(store,'lasso',months,incoming)
        class AfterDelete:
            def list_month(self, key, month):
                return [r for r in store.list_month(key,month)
                        if not (args.start <= r['post_date'] <= str(end)
                                and (r.get('status') in _WIPEABLE_STATUSES or not r.get('status')))]
            def rows_in_range(self,key,first,last):
                return [r for month in real_month_planner.plan_span_months(first,
                        (date.fromisoformat(last)-date.fromisoformat(first)).days+1)
                        for r in self.list_month(key,month) if first<=r['post_date']<=last]
        check=AfterDelete()
        allowed,_=belt_filter('lasso',incoming)
        allowed=_stage_belts('lasso',allowed)
        allowed=_media_stage_belt(check,'lasso',allowed)
        allowed=_dedupe_slots(check,'lasso',allowed)
        if len(allowed)!=len(incoming):
            raise SystemExit('Staging belts rejected rows; no rows changed')
        preserved=[r for month in months for r in check.list_month('lasso',month)
                   if args.start<=r['post_date']<=str(end)
                   and r.get('status') not in ('denied','killed','deleted','superseded','expired')]
        require_reconciled_coverage(preserved+allowed,start,args.days)
        from agent.lasso_campaign_assets import MANIFEST
        for asset in json.loads(MANIFEST.read_text()).get('assets',[]):
            if args.start <= asset['date'] <= str(end):
                for fmt,url in [('feed',asset['feed_url']),('story',asset['story_url'])]:
                    if not any(r['post_date']==asset['date'] and r['account']=='instagram'
                               and r['format']==fmt and r.get('image_url')==url
                               for r in preserved+allowed):
                        raise SystemExit('Missing supplied artwork pair; no rows changed: '+asset['id'])
        if config.calendar_grade_enabled_for('lasso'):
            from agent.calendar_grade import grade_month,A_THRESHOLD
            grade=grade_month(real_month_planner.to_calendar_rows(drafts,'lasso'),profile='B2B')
            if grade.total<A_THRESHOLD:
                raise SystemExit('Calendar quality gate requires repair before apply')
        result = real_month_planner.apply_month_plan('lasso',
            drafts,store,
            span_months=real_month_planner.plan_span_months(args.start,args.days))
        data['apply_result']=result
        save(path,data)
        print(json.dumps(result),flush=True)
        return 0 if result.get('ok') else 1
    original=real_month_run.real_builders_map
    def cached_builders(account):
        builders=original(account)
        reserve=getattr(builders.get("podcast"), "reserve", None)
        if reserve:
            for item in data["cache"].values():
                if item.get("podcast_asset"):
                    reserve(decode(item))
        out={}
        attempts=Counter()
        for category,build in builders.items():
            def cached(target,day,cat=category,fn=build):
                base=cat+':'+day
                key=base+':'+str(attempts[base])
                attempts[base]+=1
                if key in data['cache']:
                    return decode(data['cache'][key])
                result=fn(target,day)
                if result is not None and result.status==DraftStatus.PENDING:
                    data['cache'][key]=encode(result)
                    save(path,data)
                    print('saved',key,flush=True)
                return result
            out[category]=cached
        return out
    original_story=real_month_run._real_story_builder
    def cached_story_factory(account):
        build=original_story(account)
        def cached_story(target,day,feed):
            key=hashlib.sha256((day+feed.creative_public_url+feed.caption).encode()).hexdigest()
            cache=data.setdefault('story_cache',{})
            if key in cache:
                return decode(cache[key])
            result=build(target,day,feed)
            if result is not None and result.status==DraftStatus.PENDING:
                cache[key]=encode(result)
                save(path,data)
                print('saved story',day,flush=True)
            return result
        return cached_story
    real_month_run._real_story_builder=cached_story_factory
    real_month_run.real_builders_map=cached_builders
    try:
        drafts=real_month_run.plan_and_build('lasso_ig',args.start,args.days)
    finally:
        real_month_run.real_builders_map=original
        real_month_run._real_story_builder=original_story
    data['drafts']=[encode(d) for d in drafts]
    save(path,data)
    print('built',len(drafts),'drafts; review checkpoint before --apply',flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
