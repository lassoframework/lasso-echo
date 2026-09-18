import json
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from agent import media_bridge as mb, config, db
from agent.media_bridge_route import Route


def _valid_jpeg():
    """A decoder-valid photo fixture, never placeholder bytes."""
    output = BytesIO()
    Image.new('RGB', (8, 8), color=(36, 92, 127)).save(output, format='JPEG')
    return output.getvalue()

@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path/'echo.db'))
    monkeypatch.setenv('AGENT_MEDIA_BRIDGE_ALERTS', 'true')
    monkeypatch.setenv('AGENT_MEDIA_BRIDGE_CHANNELS', '{"gymx":"C123CLIENT"}')
    monkeypatch.setattr(config, 'slack_convo_client_reply_armed', lambda _: True)
    from agent import intake_web
    monkeypatch.setattr(intake_web, 'is_revoked', lambda _: False)
    from agent import media_bridge_route
    monkeypatch.setattr(
        media_bridge_route, 'resolve_client_route',
        lambda base: (Route(channel='C0TENANT123', gym_id='tenant-gymx')
                      if base == 'gymx'
                      else Route(reason='no tenant-bound client channel')))

class Poster:
    def __init__(self): self.calls=[]; self.ok=True
    def _chat_post(self, **kw):
        self.calls.append(kw)
        return {'ok':self.ok,'ts':'1' if self.ok else None}

def test_ambiguous_send_is_withheld_until_reconciled_and_deduped():
    p=Poster();p.ok=False;a=SimpleNamespace(slack_channel='')
    first = mb.notify_bridge('gymx',a,poster=p)
    assert first['reason'] == 'unresolved send' and first['reconcile']
    p.ok=True
    assert mb.notify_bridge('gymx',a,poster=p)['reason'] == 'unresolved send'
    assert len(p.calls) == 1
    assert mb.reconcile_notice('gymx', delivered=False)['status'] == 'ready'
    assert mb.notify_bridge('gymx',a,poster=p)['sent']
    assert mb.notify_bridge('gymx',a,poster=p)['deduped']
    assert len(p.calls)==2 and all(x['channel']=='C0TENANT123' for x in p.calls)
    assert mb.notify_bridge('other',a,poster=p)['reason']=='no tenant-bound client channel'
    mb.reset_notice('gymx')
    assert mb.notify_bridge('gymx',a,poster=p)['sent']

def test_disabled_and_revoked_never_send(monkeypatch):
    p=Poster();a=SimpleNamespace(slack_channel='C123')
    monkeypatch.setenv('AGENT_MEDIA_BRIDGE_ALERTS','false')
    assert not mb.notify_bridge('gymx',a,poster=p)['ok']
    monkeypatch.setenv('AGENT_MEDIA_BRIDGE_ALERTS','true')
    from agent import intake_web
    monkeypatch.setattr(intake_web,'is_revoked',lambda _:True)
    from agent import media_bridge_route
    monkeypatch.setattr(media_bridge_route, 'resolve_client_route',
                        lambda _: Route(reason='account revoked'))
    assert mb.notify_bridge('gymx',a,poster=p)['reason']=='account revoked'
    assert not p.calls

def test_verified_tenant_route_ignores_account_and_environment_destinations(monkeypatch):
    from agent import echo_clients, media_bridge_route

    gym_id = '0f0f0f0f-0000-4000-8000-000000000001'
    clients = echo_clients.build(
        [{'gym_id': gym_id}], [{'gym_id': gym_id, 'echo_account_key': 'gymx'}],
        [{'id': gym_id, 'name': 'Gym X', 'slug': 'gym-x'}])
    verified = media_bridge_route.verify_rows(
        'gymx', clients,
        [{'id': gym_id, 'status': 'active', 'is_demo': False,
          'load_test': False, 'is_verification': False}],
        [{'client_id': gym_id, 'slack_channel_id': 'C0TENANT123', 'is_test': False}])
    assert verified.ok and verified.channel == 'C0TENANT123'
    monkeypatch.setattr(media_bridge_route, 'resolve_client_route', lambda _: verified)

    p=Poster();a=SimpleNamespace(slack_channel='CATTACK')
    monkeypatch.setenv('AGENT_MEDIA_BRIDGE_CHANNELS', '{"gymx":"CATTACK"}')
    assert mb.notify_bridge('gymx',a,poster=p)['sent']
    assert p.calls[0]['channel'] == 'C0TENANT123'


def test_unarmed_client_messages_never_send(monkeypatch):
    p=Poster();a=SimpleNamespace(slack_channel='C123CLIENT')
    monkeypatch.setattr(config,'slack_convo_client_reply_armed',lambda _:False)
    assert mb.notify_bridge('gymx',a,poster=p)['reason']=='client replies disabled'
    assert not p.calls

def test_notice_is_clear_and_no_forbidden_punctuation():
    text=mb.notice_text()
    assert 'two days' in text and 'Upload media' in text and 'review' in text
    assert ':' not in text and ';' not in text

def test_fill_cannot_expand_to_month(monkeypatch):
    from agent import client_infographic_fill as cif, client_sources, creative_studio
    monkeypatch.setenv('AGENT_CLIENT_INFOGRAPHIC_FILL','true')
    monkeypatch.setattr(config,'creative_studio_enabled',lambda:True)
    monkeypatch.setattr(client_sources,'approved_sources',lambda _: [object()])
    seen=[]
    monkeypatch.setattr(cif,'_empty_upcoming_days',lambda *a,**k: seen.append(a[3]) or [])
    cif.fill_gaps('gymx',SimpleNamespace(),object(),voice=object(),days_ahead=30)
    assert seen==[2]

def test_seed_cannot_expand_to_month(monkeypatch):
    from agent import no_media_astra_seed as seed, client_infographic_fill as cif
    monkeypatch.setenv('AGENT_NO_MEDIA_ASTRA_SEED','true')
    monkeypatch.setattr(seed,'_ensure_deep_brain_facts',lambda *a:[object()])
    seen=[]
    monkeypatch.setattr(cif,'_empty_upcoming_days',lambda *a,**k:seen.append(a[3]) or [])
    seed.seed_gaps('gymx',SimpleNamespace(),object(),days_ahead=30)
    assert seen==[2]

def test_real_calendar_across_month_boundary():
    from agent.client_infographic_fill import _empty_upcoming_days
    rows=[]
    class Store:
        def list_month(self,*_): return rows
    s=Store()
    days=_empty_upcoming_days(s,'gymx','UTC',2,now='2026-09-30T12:00:00Z')
    assert days==['2026-10-01','2026-10-02']
    rows.extend({'post_date':d,'format':'feed','account':'instagram','status':'pending'} for d in days)
    assert _empty_upcoming_days(s,'gymx','UTC',2,now='2026-09-30T12:00:00Z')==[]

def test_depletion_alert_even_when_no_image_client(monkeypatch):
    from agent import client_infographic_fill as cif, client_sources, creative_studio
    monkeypatch.setenv('AGENT_CLIENT_INFOGRAPHIC_FILL','true')
    monkeypatch.setattr(config,'creative_studio_enabled',lambda:True)
    monkeypatch.setattr(client_sources,'approved_sources',lambda _: [object()])
    monkeypatch.setattr(cif, 'real_media_depleted', lambda *a, **k: True)
    monkeypatch.setattr(mb, 'bridge_days', lambda *a, **k: ['2026-10-01'])
    monkeypatch.setattr(cif,'_empty_upcoming_days',lambda *a,**k:['2026-10-01'])
    monkeypatch.setattr(creative_studio,'_default_client',lambda:None)
    calls=[]
    monkeypatch.setattr(mb,'notify_bridge',lambda *a,**k:calls.append(a[0]))
    result=cif.fill_gaps('gymx',SimpleNamespace(),object(),voice=object())
    assert result['reason']=='no image client' and calls==['gymx']

def test_calendar_gap_with_real_library_media_does_not_alert(monkeypatch, tmp_path):
    """A gap alone is not depletion: an available client photo suppresses Slack."""
    from agent import client_infographic_fill as cif, client_sources, creative_studio
    monkeypatch.setenv('AGENT_CLIENT_INFOGRAPHIC_FILL','true')
    monkeypatch.setattr(config, 'LIBRARY_PATH', str(tmp_path))
    (tmp_path / 'gymx').mkdir()
    (tmp_path / 'gymx' / 'still.jpg').write_bytes(_valid_jpeg())
    (tmp_path / 'gymx' / 'still.json').write_text(json.dumps({
        'approved': True, 'moderation': 'clean', 'review': False,
        'people': True, 'consent': 'granted',
    }))
    monkeypatch.setattr(config, 'creative_studio_enabled', lambda: True)
    monkeypatch.setattr(client_sources, 'approved_sources', lambda _: [object()])
    monkeypatch.setattr(cif, '_empty_upcoming_days', lambda *a, **k: ['2026-10-01'])
    monkeypatch.setattr(creative_studio, '_default_client', lambda: None)
    calls = []
    monkeypatch.setattr(mb, 'notify_bridge', lambda *a, **k: calls.append(a[0]))
    result = cif.fill_gaps('gymx', SimpleNamespace(), object(), voice=object())
    assert result['reason'] == 'usable media available' and calls == []

def test_active_drive_inventory_suppresses_depletion_alert(monkeypatch):
    """A reviewed, consent-cleared Drive asset is available without local files."""
    from agent import client_infographic_fill as cif
    monkeypatch.setattr(config, 'gym_drive_stage_enabled', lambda: True)
    monkeypatch.setattr(config, 'gym_drive_connect_active_for', lambda _: True)
    class MediaStore:
        def available(self): return True
        def list_assets(self, gym):
                return [{'id': 'a1', 'gym_id': gym, 'eligible': True,
                         'excluded_by_coach': False, 'used_count': 0,
                         'last_used_at': None, 'kind': 'photo',
                         'review_status': 'approved', 'reviewed_by': 'operator',
                         'reviewed_at': '2026-09-18T00:00:00Z',
                         'moderation_status': 'clean',
                         'moderation_json': {'verdict': 'clean', 'provider': 'test-review'},
                         'people_detected': False,
                         'consent_status': 'not_required'}]
    from agent import gym_media_index
    monkeypatch.setattr(gym_media_index, 'default_store', lambda: MediaStore())
    assert cif.real_media_depleted('gymx') is False

def test_seed_honors_gym_timezone_and_alerts_without_facts(monkeypatch):
    from agent import no_media_astra_seed as seed, client_infographic_fill as cif
    monkeypatch.setenv('AGENT_NO_MEDIA_ASTRA_SEED','true')
    monkeypatch.setattr(config,'posting_timezone_for',lambda _:'Pacific/Honolulu')
    monkeypatch.setattr(seed,'_ensure_deep_brain_facts',lambda *a:[])
    seen=[]; alerts=[]
    monkeypatch.setattr(cif,'_empty_upcoming_days',lambda *a,**k: seen.append(a[2]) or ['2026-10-01'])
    monkeypatch.setattr(mb,'notify_bridge',lambda *a,**k:alerts.append(a[0]))
    assert seed.seed_gaps('gymx',SimpleNamespace(),object())==0
    assert seen==['Pacific/Honolulu'] and alerts==['gymx']

def test_display_preserves_cached_history_and_blocks_new_far_future(monkeypatch,tmp_path):
    from agent import no_creative_fallback as ncf, calendar_autopublish
    monkeypatch.setenv('AGENT_NO_CREATIVE_FALLBACK','true')
    monkeypatch.setattr(calendar_autopublish,'_local_now',lambda *a:datetime(2026,9,18,tzinfo=timezone.utc))
    ncf._URL_CACHE.clear()
    p={'id':'bridge','day_key':'2026-09-19','caption':'Coaching for your goals.','format':'feed'}
    renders=[]
    def render(*a,**k): renders.append(1);return str(tmp_path/'art.png')
    url=ncf.display_image_for(p,tenant='gymx',renderer=render,host=lambda *a:'https://example.com/art.png')
    assert url
    monkeypatch.setattr(calendar_autopublish,'_local_now',lambda *a:datetime(2026,9,20,tzinfo=timezone.utc))
    assert ncf.display_image_for(p,tenant='gymx',renderer=render)==url
    assert ncf.display_image_for(dict(p,id='future',day_key='2026-10-10'),tenant='gymx',renderer=render) is None
    assert len(renders)==1
    ncf._URL_CACHE.clear()
