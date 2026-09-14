from agent import real_month_planner as rmp, config
from agent.drafter import Draft, DraftStatus
from agent.portal_calendar_store import preserve_and_prune


def draft(cat, day):
    return Draft(draft_id=cat, account_key='lasso_ig', platform='instagram',
                 caption=cat, hashtags=[], creative_path=cat+'.png',
                 creative_public_url='https://cdn/'+cat+'.png', scheduled_for=day,
                 status=DraftStatus.PENDING, category=cat, day_key=day)


def test_fallback_preserves_pm_slot_and_story_pair(monkeypatch):
    monkeypatch.setattr(config, 'caption_cooldown_enabled', lambda: False)
    plan = [rmp.PlanSlot('2026-09-15', cat, fmt, cadence_slot=slot)
            for slot, cat in [(0, 'book'), (1, 'missing')]
            for fmt in ('feed', 'story')]
    anchors=[]
    def story(_, day, feed):
        anchors.append(feed.caption)
        return draft(feed.caption, day)
    result=rmp.build_month_drafts(plan, {'book':lambda _,d:draft('book',d),
             'podcast':lambda _,d:draft('podcast',d)}, story_builder=story)
    assert anchors == ['book', 'podcast']
    assert [d.cadence_slot_index for d in result] == [0,1,0,1]


def test_editorial_plan_has_two_distinct_topics_and_three_videos(monkeypatch):
    monkeypatch.setattr(config, 'lasso_editorial_calendar_enabled', lambda: True)
    plan=rmp.plan_month('lasso_ig','2026-09-14',7,posts_per_day=2,
                        book_dates=set(),sprint_day_fn=lambda _:False)
    feeds=[s for s in plan if s.fmt=='feed']
    assert len(feeds)==14
    assert sum(s.category=='podcast' for s in feeds)==3
    assert {'book','summit','echo','website','podcast'} <= {s.category for s in feeds}
    for day in {s.post_date for s in feeds}:
        pair=[s for s in feeds if s.post_date==day]
        assert {s.cadence_slot for s in pair}=={0,1}
        assert len({s.category for s in pair})==2


def test_preserve_approved_am_admits_distinct_pm_but_not_downgrade(monkeypatch):
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:True)
    monkeypatch.setattr(config,'cadence_2x_enabled',lambda:True)
    am=dict(post_date='2026-09-15',account='instagram',format='feed',slot_index=0,
            caption='approved morning',image_url='am.png',status='approved')
    pm=dict(am,slot_index=1,caption='different evening',image_url='pm.png',status='pending')
    class Store:
        capacity=2
        def gym_posts_per_day(self,_):return self.capacity
        def list_month(self,*_):return [am]
    store=Store()
    assert preserve_and_prune(store,'lasso',['2026-09'],[dict(am,status='pending'),pm])[0]==[pm]
    store.capacity=1
    assert preserve_and_prune(store,'lasso',['2026-09'],[pm])[0]==[]


def test_podcast_build_reservation_does_not_consume_usage():
    from tests.podcast_fakes import FakeDrive, FakeStore, FakeZernio, NOTES_DOC_TEXT, make_asset
    from tests.test_pending import ACCT, _probe_ok
    from agent.podcast_library_builder import build_podcast_clip_draft
    store=FakeStore([make_asset()])
    result=build_podcast_clip_draft(ACCT,'2026-09-20',store=store,
        drive=FakeDrive(docs={'doc140':NOTES_DOC_TEXT}),zernio_client=FakeZernio(),
        probe_fn=_probe_ok,feed_map={},defer_use=True)
    assert result.source_media_asset_id=='clip140s1'
    assert result.podcast_asset['episode']==140
    assert store.assets['clip140s1']['used_count']==0


def test_paragraph_notes_supply_verbatim_sentences():
    from agent.podcast_caption import parse_notes
    sentences=['A clear follow up process helps the team know who owns each conversation.',
               'The episode explains how the owner reviews those conversations each week.',
               'The coaches can then spend their time coaching instead of chasing the next lead.',
               'Each conversation remains visible to the owner in the same place.']
    notes='Episode 140: Follow Up\n'+' '.join(sentences)
    result=parse_notes(notes)
    assert result['claims']==sentences


def test_sprint_legacy_null_ordinals_keep_two_posts(monkeypatch):
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:True)
    monkeypatch.setattr(config,'cadence_2x_enabled',lambda:True)
    class Store:
        def gym_posts_per_day(self,_):return 2
        def list_month(self,*_):return []
    rows=[dict(post_date='2026-09-20',account='instagram',format='feed',
               caption=cat,image_url=cat+'.png',slot_index=None)
          for cat in ('summit','book')]
    kept,_=preserve_and_prune(Store(),'lasso',['2026-09'],rows)
    assert [r['slot_index'] for r in kept]==[0,1]


def test_source_pool_produces_distinct_monthly_echo_and_website_topics(monkeypatch):
    from pathlib import Path
    from agent import content_planner
    from agent.lasso_editorial import source_pillar
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:True)
    doc=content_planner.load_source_doc(Path(__file__).resolve().parents[1]/'brand_voice/lasso_editorial.md')
    plan=rmp.plan_month('lasso_ig','2026-09-14',28,posts_per_day=2,
                        book_dates=set(),sprint_day_fn=lambda _:False)
    for category in ('echo','website'):
        names=[source_pillar(category,s.post_date,doc) for s in plan
               if s.fmt=='feed' and s.category==category]
        assert len(names)==8
        assert len(set(names))==8


def test_legacy_duplicate_approved_ordinals_use_full_capacity(monkeypatch):
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:True)
    monkeypatch.setattr(config,'cadence_2x_enabled',lambda:True)
    row=dict(post_date='2026-09-15',account='instagram',format='feed',
             caption='old',image_url='old.png',slot_index=0,status='approved')
    class Store:
        def gym_posts_per_day(self,_):return 2
        def list_month(self,*_):return [row,dict(row,caption='other',image_url='other.png')]
    incoming=dict(row,caption='new',image_url='new.png',slot_index=1,status='pending')
    assert preserve_and_prune(Store(),'lasso',['2026-09'],[incoming])[0]==[]


def test_editorial_sprint_and_varied_feed_have_distinct_cadence(monkeypatch):
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:True)
    plan=rmp.plan_month('lasso_ig','2026-09-15',1,posts_per_day=2,
        book_dates=set(),sprint_day_fn=lambda _:True,sprint_feed_count_fn=lambda _:1)
    assert [s.cadence_slot for s in plan if s.fmt=='feed']==[0,1]
    assert [s.cadence_slot for s in plan if s.fmt=='story']==[0,1]


def test_user_campaign_original_pairs_to_own_story_and_is_lasso_only(monkeypatch):
    from agent.lasso_campaign_assets import wrap_builders
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:True)
    monkeypatch.setattr(config,'stories_enabled',lambda:True)
    entry=dict(id='01',date='2026-09-15',slot_index=0,category='summit',
               is_sprint=True,caption='Approved summit caption',
               feed_url='https://cdn/original.png',story_url='https://cdn/portrait.png')
    blank=lambda *a:None
    args=({'book':blank},blank,blank,blank)
    wrapped=wrap_builders('lasso_ig',*args,manifest={'assets':[entry]})
    feed=wrapped[2]('lasso','2026-09-15',0)
    story=wrapped[3]('lasso','2026-09-15',0,feed)
    assert feed.creative_public_url==entry['feed_url']
    assert story.creative_public_url==entry['story_url'] and story.is_story
    assert wrap_builders('other_ig',*args,manifest={'assets':[entry]})==args


def test_rebuild_requires_post_preservation_coverage():
    import pytest
    from datetime import date
    from scripts.rebuild_lasso_calendar import require_reconciled_coverage
    rows=[dict(post_date='2026-09-15',account=a,format=f)
          for a,f in [('instagram','feed'),('facebook','feed'),('instagram','story')]]
    # A single preserved AM plus pruned replacements is not a complete day.
    with pytest.raises(SystemExit,match='incomplete'):
        require_reconciled_coverage(rows,date(2026,9,15),1)
    require_reconciled_coverage(rows+rows,date(2026,9,15),1)


def test_all_supplied_campaign_assets_have_dated_original_and_story(monkeypatch):
    import json
    from agent.lasso_campaign_assets import MANIFEST
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:True)
    entries=json.loads(MANIFEST.read_text())['assets']
    assert len(entries)==13 and len({e['source_sha256'] for e in entries})==13
    plan=rmp.plan_month('lasso_ig','2026-09-15',30,posts_per_day=2)
    feeds={(s.post_date,s.cadence_slot):s for s in plan if s.fmt=='feed'}
    for e in entries:
        assert e['feed_url'].startswith('https://') and e['story_url'].startswith('https://')
        assert e['feed_url']!=e['story_url']
        assert feeds[e['date'],e['slot_index']].category==e['category']


def test_lasso_calendar_displays_both_real_publish_times(monkeypatch):
    monkeypatch.setattr(config,'lasso_editorial_calendar_enabled',lambda:True)
    monkeypatch.setattr(config,'cadence_2x_enabled',lambda:True)
    monkeypatch.setattr(config,'posting_timezone_for',lambda _: 'America/New_York')
    am=draft('book','2026-09-15');am.cadence_slot_index=0
    pm=draft('echo','2026-09-15');pm.cadence_slot_index=1
    rows=rmp.to_calendar_rows([am,pm],'lasso')
    assert [r['scheduled_at'][11:16] for r in rows]==['07:30','07:30','18:30','18:30']
