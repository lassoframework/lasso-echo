"""Offline replay acceptance using a disposable PostgreSQL 17 cluster.

The transport exercises the real migration through the production Bus HTTP
contract, without network, secrets, installation, or production connections.
"""
import copy
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import threading
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from agent import support_sender_fence as fence
from agent.slack_convo import adapter, outbox, replay
from agent.slack_convo.bus import Bus
from tests.test_slack_convo import _deps, _ev, _who

ROOT = Path(__file__).resolve().parents[1]


def pg_binary(name):
    """Find a PostgreSQL 17 executable and fail closed on another major."""
    candidates = [
        Path('/opt/homebrew/opt/postgresql@17/bin') / name,
        Path('/usr/lib/postgresql/17/bin') / name,
    ]
    located = shutil.which(name)
    if located:
        candidates.append(Path(located))
    for candidate in candidates:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        server = candidate.parent / 'postgres'
        if not server.is_file():
            continue
        version = subprocess.check_output([str(server), '--version'], text=True)
        if ' 17.' in version:
            return str(candidate)
    raise AssertionError(f'PostgreSQL 17 executable {name!r} not found')


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


class Engine:
    def __init__(self, base):
        self.base = base

    def sql(self, statement):
        result = subprocess.run(self.base, input=statement, text=True,
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    def rows(self, table, where='true'):
        return json.loads(self.sql(f"select coalesce(jsonb_agg(to_jsonb(t)),'[]') "
                                   f"from public.{table} t where {where}"))

    def call(self, name, args):
        params = ','.join(f'{key}=>{q(json.dumps(value))}::jsonb'
                          if isinstance(value, (dict, list)) else
                          f'{key}=>null' if value is None else
                          f'{key}=>{q(value)}' for key, value in args.items())
        return json.loads(self.sql(f'select public.{name}({params})'))


@pytest.fixture(scope='session')
def pg_engine():
    initdb, pg_ctl = pg_binary('initdb'), pg_binary('pg_ctl')
    psql, postgres = pg_binary('psql'), pg_binary('postgres')
    version = subprocess.check_output([postgres, '--version'], text=True)
    assert ' 17.' in version, f'PostgreSQL 17 required, got: {version.strip()}'
    with tempfile.TemporaryDirectory(prefix='slack_replay_pg_', dir='/tmp') as tmp:
        path = Path(tmp)
        sock = path/'sock'
        sock.mkdir()
        data = path/'data'
        port = random.randrange(40000, 60000)
        subprocess.run([initdb, '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=30)
        subprocess.run([pg_ctl, '-D', str(data), '-l', str(path/'log'),
                        '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=30)
        engine = Engine([psql, '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                         '-h', str(sock), '-p', str(port), '-U', 'postgres', '-d', 'postgres'])
        try:
            engine.sql("""
            create role anon; create role authenticated; create role service_role;
            create table support_tickets (
              id uuid primary key default gen_random_uuid(), product text, source text,
              client_id text, reporter text, raw_text text, status text default 'new',
              slack_channel_id text, slack_thread_ts text, slack_user_id text,
              identity_kind text, bot_identity text, classification text, request_type text,
              verification_before jsonb,verification_after jsonb,escalated boolean default false,
              lane text,hold_tier text,request_version bigint default 0,
              created_at timestamptz default clock_timestamp(),resolved_at timestamptz,
              unique(slack_channel_id,slack_thread_ts));
            create table support_messages (
              id uuid primary key default gen_random_uuid(),ticket_id uuid references support_tickets(id),
              direction text,author_type text,author_id text,body text,attachments jsonb,
              delivery_status text,slack_event_id text unique,slack_ts text,
              delivery_request_version bigint,created_at timestamptz default clock_timestamp());
            create function request_cycle() returns trigger language plpgsql as $$
            begin
              if new.direction='inbound' and (new.author_type='client'
                or new.author_type in ('staff','blake') and new.attachments->>'surface'='mention'
                and new.attachments->>'identity_reason'='operator list') then
                update support_tickets set request_version=request_version+1,
                  status=case when status='resolved' then 'verification' else status end,
                  resolved_at=case when status='resolved' then null else resolved_at end
                  where id=new.ticket_id;
              end if;
              if new.direction='outbound' and new.delivery_request_version is not null
                and new.delivery_request_version<>(select request_version from support_tickets where id=new.ticket_id)
              then raise exception 'stale outbound request'; end if;
              return new;
            end $$;
            create trigger request_cycle before insert on support_messages
              for each row execute function request_cycle();
            """)
            engine.sql((ROOT/'migrations/DRAFT_support_slack_replay_20261009.sql').read_text())
            yield engine
        finally:
            subprocess.run([pg_ctl, '-D', str(data), '-m', 'fast', '-w', 'stop'],
                           capture_output=True, timeout=30, check=False)


class Transport:
    def __init__(self, engine):
        self.engine = engine
        self.lose_once = set()
        self.before_call = None

    def _response(self, action):
        try:
            result = action()
            return SimpleNamespace(status_code=200, text=json.dumps(result), json=lambda: result)
        except RuntimeError as exc:
            text = str(exc)
            status = 409 if 'duplicate key' in text else 400
            return SimpleNamespace(status_code=status, text=text, json=lambda: None)

    def post(self, url, *, data, **kw):
        body = json.loads(data)
        name = url.rsplit('/', 1)[-1]
        if '/rpc/' in url:
            if self.before_call:
                self.before_call(name, body)
            result = self._response(lambda: self.engine.call(name, body))
            if name in self.lose_once:
                self.lose_once.remove(name)
                raise TimeoutError('response lost after database committed')
            return result
        def insert():
            columns = ','.join(body)
            selects = ','.join('r.'+key for key in body)
            return json.loads(self.engine.sql(
                f'with written as (insert into public.{name} ({columns}) select {selects} '
                f'from jsonb_populate_record(null::public.{name},{q(data)}::jsonb) r returning *) '
                f"select jsonb_agg(to_jsonb(written)) from written"))
        return self._response(insert)

    def get(self, url, *, params, **kw):
        table = url.rsplit('/', 1)[-1]
        where = []
        for key, val in params.items():
            if key in ('select', 'order', 'limit'):
                continue
            if '->>' in key:
                column,json_key=key.split('->>',1)
                key=f'{column}->>{q(json_key)}'
            if key == 'or':
                if table==replay.TABLE:
                    where.append("(state='pending' or state='planning' and claim_until<clock_timestamp())")
                elif '.not.is.null' in val:
                    keys=[part.split('->>',1)[1].split('.not.',1)[0]
                          for part in val.strip('()').split(',')]
                    where.append('('+' or '.join(f'attachments->>{q(k)} is not null' for k in keys)+')')
                else:
                    ident=val.split('.eq.',1)[1].split(',',1)[0]
                    where.append(f"(attachments->>'identity'={q(ident)} or attachments->>'identity' is null)")
            elif val.startswith('eq.'):
                where.append(f'{key}={q(val[3:])}')
            elif val.startswith('gte.'):
                where.append(f'{key}>={q(val[4:])}')
            elif val.startswith('in.('):
                where.append(f"{key} in ({','.join(q(v) for v in val[4:-1].split(','))})")
            elif val=='is.null':
                where.append(f'{key} is null')
            elif val=='not.is.null':
                where.append(f'{key} is not null')
            else:
                raise AssertionError((key,val))
        order = ','.join(s.replace('.desc',' desc').replace('.asc',' asc')
                         for s in params.get('order','created_at.asc').split(','))
        selection = '*' if params.get('select') == '*' else params.get('select','*')
        statement = (f'select coalesce(jsonb_agg(to_jsonb(t)),\'[]\') from '
                     f'(select {selection} from public.{table} where '
                     f"{' and '.join(where) or 'true'} order by {order} "
                     f"limit {int(params.get('limit',10000))}) t")
        return self._response(lambda: json.loads(self.engine.sql(statement)))

    def patch(self, url, *, params, data, **kw):
        table = url.rsplit('/',1)[-1]
        fields = json.loads(data)
        where = ' and '.join(f't.{k}={q(v[3:])}' for k,v in params.items())
        assignments = ','.join(f'{key}=r.{key}' for key in fields)
        sql = (f'with written as (update public.{table} t set {assignments} '
               f'from jsonb_populate_record(null::public.{table},{q(data)}::jsonb) r '
               f'where {where} returning t.*) select coalesce(jsonb_agg(to_jsonb(written)),\'[]\') from written')
        return self._response(lambda: json.loads(self.engine.sql(sql)))


class ReplayBus(Bus):
    def __init__(self, engine):
        self.engine = engine
        self.transport = Transport(engine)
        super().__init__(url='http://offline', service_key='offline', http=self.transport)

    @property
    def tickets(self):
        return {row['id']:row for row in self.engine.rows('support_tickets')}

    @property
    def msgs(self):
        return self.engine.rows('support_messages')


@pytest.fixture
def pg_bus(pg_engine):
    pg_engine.sql('truncate support_slack_replay,support_messages,support_tickets cascade')
    return ReplayBus(pg_engine)


@pytest.fixture
def paused(monkeypatch, tmp_path):
    path = tmp_path/'fence.json'
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_ENABLED','true')
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_CONTROL_FILE',str(path))
    monkeypatch.setenv('AGENT_SLACK_BOT_USER_ID','U_ECHO_BOT')
    path.write_text(json.dumps({'paused':True,'generation':'replay-acceptance'}))
    return path


def resume(path):
    path.write_text(json.dumps({'paused':False,'generation':'replay-acceptance'}))


def queue(pg_bus):
    return pg_bus.engine.rows(replay.TABLE)


def capture_one(pg_bus, paused, text='my photos are broken', **kwargs):
    deps = _deps(pg_bus, **kwargs)
    result = adapter.handle_event(_ev(text), 'G0MPIM:1.001', deps)
    assert result.reason == 'support_sender_paused'
    return deps, queue(pg_bus)[0]


def test_pause_restart_resume_and_duplicate_never_strand_or_repeat(pg_bus, paused):
    deps,item = capture_one(pg_bus,paused)
    assert item['context']['event'] == _ev('my photos are broken')
    assert item['ticket_snapshot']['request_version'] == 1
    assert item['ticket_snapshot']['client_id'] == 'g-1'
    assert item['ticket_snapshot']['source'] == 'slack_conversation'
    assert all(m['direction']=='inbound' for m in pg_bus.msgs)
    assert replay.run_once(deps) == {}
    resume(paused)
    # A new deps instance models listener restart, with no adapter memory.
    assert replay.run_once(_deps(pg_bus)) == {'committed':1}
    assert pg_bus.tickets[item['ticket_id']]['status']=='triage'
    before = copy.deepcopy(pg_bus.msgs)
    result = adapter.handle_event(_ev('my photos are broken'), 'G0MPIM:1.001', _deps(pg_bus))
    assert result.duplicate
    assert pg_bus.msgs == before
    assert replay.run_once(_deps(pg_bus)) == {}


@pytest.mark.parametrize('lost', ['support_slack_replay_capture','support_slack_replay_claim',
                                 'support_slack_replay_commit'])
def test_lost_response_recovers_same_capture_claim_and_plan(pg_bus,paused,lost):
    pg_bus.transport.lose_once.add(lost)
    deps,item = capture_one(pg_bus,paused)
    resume(paused)
    assert replay.run_once(deps)=={'committed':1}
    assert len(queue(pg_bus))==1
    assert sum(m['direction']=='inbound' for m in pg_bus.msgs)==1
    ids = [m['id'] for m in pg_bus.msgs]
    assert len(ids)==len(set(ids))


def test_multi_event_newest_snapshot_composes_initial_request_once(pg_bus,paused):
    deps,first = capture_one(pg_bus,paused)
    adapter.handle_event(_ev('also the October calendar is empty',ts='1.002'),
                         'G0MPIM:1.002',deps)
    second = next(r for r in queue(pg_bus) if r['id']!=first['id'])
    assert second['context']['classification']=='follow_up'
    assert second['context']['dispatch']['classification']=='code_fix'
    assert second['context']['dispatch']['text']=='my photos are broken\n\nalso the October calendar is empty'
    assert second['ticket_snapshot']['request_version']==2
    resume(paused)
    assert replay.run_once(deps)=={'held':1,'committed':1}
    requests=[m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='fixer_request']
    assert len(requests)==1
    assert 'my photos are broken' in requests[0]['body']
    assert 'October calendar is empty' in requests[0]['body']
    assert requests[0]['delivery_request_version']==2
    assert replay.run_once(deps)=={}


@pytest.mark.parametrize('speaker',['client','staff'])
@pytest.mark.parametrize('armed',[True,False])
@pytest.mark.parametrize('lane',['hold','safe'])
def test_pending_client_request_plus_authorized_note_preserves_client_dispatch(
        pg_bus,paused,monkeypatch,speaker,armed,lane):
    from agent.slack_convo import identity_gate as ig
    actors={'U_CLIENT':_who(ig.CLIENT),
            'U_STAFF':replace(_who(ig.STAFF,'U_STAFF'),reason='operator list')}
    deps=replace(_deps(pg_bus,client_armed=armed,staff_armed=True),
                 resolve_identity=actors.__getitem__)
    assert adapter.handle_event(_ev('my photos are broken'),'G0MPIM:1.001',deps).reason=='support_sender_paused'
    first=queue(pg_bus)[0]
    pg_bus.set_ticket(first['ticket_id'],lane=lane)
    note=_ev('Please also inspect October',ts='1.002',thread_ts='1.001',
             user='U_STAFF' if speaker=='staff' else 'U_CLIENT',etype='app_mention')
    assert adapter.handle_event(note,'G0MPIM:1.002',deps).reason=='support_sender_paused'
    latest=next(r for r in queue(pg_bus) if r['event_key']=='G0MPIM:1.002')
    assert latest['ticket_snapshot']['request_version']==2
    assert latest['context']['who']['kind']==speaker
    assert latest['context']['surface']=='mention'
    dispatch=latest['context']['dispatch']
    assert dispatch['who']==first['context']['who']
    assert dispatch['event']==first['context']['event']
    assert dispatch['surface']=='mpim'
    assert dispatch['classification']=='code_fix'
    assert dispatch['text']=='my photos are broken\n\nPlease also inspect October'
    resume(paused)
    assert replay.run_once(deps)=={'held':1,'committed':1}
    ticket=pg_bus.ticket(first['ticket_id'])
    assert ticket['status']=='triage' and ticket['identity_kind']=='client'
    rows=[m for m in pg_bus.msgs if m['direction']=='outbound']
    request=next(m for m in rows if m['attachments']['kind']=='fixer_request')
    ack=next(m for m in rows if m['attachments']['kind']=='ack')
    assert len([m for m in rows if m['attachments']['kind']=='fixer_request'])==1
    assert request['delivery_status']=='held'  # Staff safe-lane privilege never transfers.
    assert ack['delivery_status']==('ready' if armed else 'held')
    assert all(m['attachments']['recipient_kind']=='client' and
               m['attachments']['surface']=='mpim' and m['delivery_request_version']==2 for m in rows)
    assert 'my photos are broken' in request['body'] and 'Please also inspect October' in request['body']
    stored=next(r for r in queue(pg_bus) if r['id']==latest['id'])
    assert stored['committed_plan']['decision']['identity_kind']=='client'
    monkeypatch.setattr(outbox,'_recipient_armed',lambda *args:armed)
    posts=[]
    def post(*args,**kwargs):
        posts.append((args,kwargs))
        return '2.001'
    if armed:
        assert replay.dispatch_allowed(pg_bus,ack,'echo')
        outbox._dispatch_one(pg_bus,post,ack,identity=deps.identity,
                             summary=Counter(),log=lambda *args:None)
        # Code-fix acknowledgements retain the existing verified-notice gate.
        assert not posts and pg_bus.message(ack['id'])['delivery_status']=='suppressed'
    else:
        assert not replay.dispatch_allowed(pg_bus,ack,'echo')
        assert not posts
    before=copy.deepcopy(pg_bus.msgs)
    assert adapter.handle_event(note,'G0MPIM:1.002',deps).duplicate
    assert replay.run_once(deps)=={} and pg_bus.msgs==before


def test_pending_client_question_plus_staff_note_posts_once_with_client_authority(pg_bus,paused,monkeypatch):
    from agent.slack_convo import identity_gate as ig
    actors={'U_CLIENT':_who(ig.CLIENT),
            'U_STAFF':replace(_who(ig.STAFF,'U_STAFF'),reason='operator list')}
    answered=[]
    def answer(*args):
        answered.append(args)
        return {'body':'There are three draft posts.','grounding':{'drafts':3}}
    deps=replace(_deps(pg_bus,client_armed=True,staff_armed=True,auto_answer=True,answer=answer),
                 resolve_identity=actors.__getitem__)
    adapter.handle_event(_ev('is the calendar loaded?'),'G0MPIM:1.001',deps)
    adapter.handle_event(_ev('Please also inspect October',ts='1.002',thread_ts='1.001',
                             user='U_STAFF',etype='app_mention'),'G0MPIM:1.002',deps)
    resume(paused)
    assert replay.run_once(deps)=={'held':1,'committed':1}
    assert len(answered)==1
    ack=next(m for m in pg_bus.msgs if m['attachments'].get('kind')=='ack')
    assert ack['attachments']['recipient_kind']=='client' and ack['attachments']['surface']=='mpim'
    monkeypatch.setattr(outbox,'_recipient_armed',lambda *args:True)
    posts=[]
    def post(*args,**kwargs):
        posts.append((args,kwargs))
        return '2.001'
    outbox._dispatch_one(pg_bus,post,ack,identity=deps.identity,summary=Counter(),log=lambda *args:None)
    assert len(posts)==1 and pg_bus.message(ack['id'])['delivery_status']=='posted'
    assert posts[0][1]['thread_ts'] is None
    assert replay.run_once(deps)=={}


@pytest.mark.parametrize('changed',['U_CLIENT','U_STAFF'])
def test_composed_client_dispatch_rechecks_both_origin_and_latest_identity(pg_bus,paused,changed):
    from agent.slack_convo import identity_gate as ig
    actors={'U_CLIENT':_who(ig.CLIENT),
            'U_STAFF':replace(_who(ig.STAFF,'U_STAFF'),reason='operator list')}
    deps=replace(_deps(pg_bus,client_armed=True),resolve_identity=actors.__getitem__)
    adapter.handle_event(_ev('my photos are broken'),'G0MPIM:1.001',deps)
    adapter.handle_event(_ev('Please also inspect October',ts='1.002',thread_ts='1.001',
                             user='U_STAFF',etype='app_mention'),'G0MPIM:1.002',deps)
    actors[changed]=replace(actors[changed],email='changed@example.test')
    resume(paused)
    assert replay.run_once(deps)=={'held':2}
    assert any(r['reason']=='identity_binding_changed' for r in queue(pg_bus))
    assert all(m['direction']=='inbound' for m in pg_bus.msgs)


def test_staff_note_cannot_remove_pending_client_rate_limit(pg_bus,paused):
    from agent.slack_convo import identity_gate as ig
    actors={'U_CLIENT':_who(ig.CLIENT),
            'U_STAFF':replace(_who(ig.STAFF,'U_STAFF'),reason='operator list')}
    deps=replace(_deps(pg_bus,cap=0,client_armed=False,staff_armed=True),
                 resolve_identity=actors.__getitem__)
    adapter.handle_event(_ev('my photos are broken'),'G0MPIM:1.001',deps)
    adapter.handle_event(_ev('Please also inspect October',ts='1.002',thread_ts='1.001',
                             user='U_STAFF',etype='app_mention'),'G0MPIM:1.002',deps)
    latest=next(r for r in queue(pg_bus) if r['event_key']=='G0MPIM:1.002')
    assert latest['context']['rate_limited'] is False
    assert latest['context']['dispatch']['rate_limited'] is True
    resume(paused)
    assert replay.run_once(deps)=={'held':1,'committed':1}
    stored=next(r for r in queue(pg_bus) if r['id']==latest['id'])
    assert stored['committed_plan']['decision']['reason']=='rate_limited'
    assert not any(m['attachments'].get('kind')=='fixer_request' for m in pg_bus.msgs)
    assert all(m['delivery_status']=='held' for m in pg_bus.msgs
               if m['attachments'].get('kind')=='template')


@pytest.mark.parametrize('text',['STOP','let the team handle'])
def test_staff_followup_cannot_remove_original_client_safety_hold(pg_bus,paused,text):
    from agent.slack_convo import identity_gate as ig
    actors={'U_CLIENT':_who(ig.CLIENT),
            'U_STAFF':replace(_who(ig.STAFF,'U_STAFF'),reason='operator list')}
    deps=replace(_deps(pg_bus,client_armed=True,staff_armed=True),resolve_identity=actors.__getitem__)
    adapter.handle_event(_ev(text),'G0MPIM:1.001',deps)
    adapter.handle_event(_ev('Please also inspect October',ts='1.002',thread_ts='1.001',
                             user='U_STAFF',etype='app_mention'),'G0MPIM:1.002',deps)
    resume(paused)
    assert replay.run_once(deps)=={'held':2}
    assert any(r['reason']=='STOP_DND_or_human_takeover' for r in queue(pg_bus))
    assert all(m['direction']=='inbound' for m in pg_bus.msgs)


@pytest.mark.parametrize('change',['decision_actor','recipient_actor','surface'])
def test_commit_refuses_staff_origin_metadata_for_composed_client_plan(pg_bus,paused,change):
    from agent.slack_convo import identity_gate as ig
    actors={'U_CLIENT':_who(ig.CLIENT),
            'U_STAFF':replace(_who(ig.STAFF,'U_STAFF'),reason='operator list')}
    deps=replace(_deps(pg_bus),resolve_identity=actors.__getitem__)
    adapter.handle_event(_ev('my photos are broken'),'G0MPIM:1.001',deps)
    adapter.handle_event(_ev('Please also inspect October',ts='1.002',thread_ts='1.001',
                             user='U_STAFF',etype='app_mention'),'G0MPIM:1.002',deps)
    latest=next(r for r in queue(pg_bus) if r['event_key']=='G0MPIM:1.002')
    args={'p_id':latest['id'],'p_identity':'echo','p_token':str(uuid.uuid4())}
    assert pg_bus.engine.call('support_slack_replay_claim',args)['state']=='planning'
    row={'id':str(uuid.uuid4()),'ticket_id':latest['ticket_id'],'direction':'outbound',
         'author_type':'echo','body':'Got it','delivery_status':'held',
         'attachments':{'kind':'ack','identity':'echo','surface':'mpim','recipient_kind':'client'}}
    decision={'ticket_id':latest['ticket_id'],'identity_kind':'client'}
    if change=='decision_actor':
        decision['identity_kind']='staff'
    elif change=='recipient_actor':
        row['attachments']['recipient_kind']='staff'
    else:
        row['attachments']['surface']='mention'
    args['p_plan']={'ticket_fields':{'status':'triage'},'rows':[row],'decision':decision}
    with pytest.raises(RuntimeError,match='invalid plan|invalid outbound'):
        pg_bus.engine.call('support_slack_replay_commit',args)
    assert pg_bus.ticket(latest['ticket_id'])['status']=='new'
    assert all(m['direction']=='inbound' for m in pg_bus.msgs)


def test_two_workers_one_claim_and_one_commit(pg_bus,paused):
    deps,item = capture_one(pg_bus,paused)
    resume(paused)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(lambda _:replay.process(deps,item['id']),range(2)))
    assert 'committed' in [r['state'] for r in results]
    assert sum((m.get('attachments') or {}).get('kind')=='fixer_request' for m in pg_bus.msgs)==1


@pytest.mark.parametrize('field,value', [('client_id','g-other'),('source','website_tab'),
    ('bot_identity','scout'),('slack_user_id','U_OTHER'),('slack_channel_id','G_OTHER'),
    ('request_version',99),('status','approved')])
def test_ticket_cas_after_plan_blocks_every_outbound(pg_bus,paused,field,value):
    deps,item = capture_one(pg_bus,paused)
    resume(paused)
    def change(name,body):
        if name=='support_slack_replay_commit':
            pg_bus.engine.sql(f'update support_tickets set {field}={q(value)} where id={q(item["ticket_id"])}')
    pg_bus.transport.before_call=change
    result=replay.process(deps,item['id'])
    assert result['state']=='held'
    assert not any(m['direction']=='outbound' for m in pg_bus.msgs)


@pytest.mark.parametrize('text',['STOP','unsubscribe','I want to talk to a human','human takeover'])
def test_later_stop_or_human_control_blocks_pending_and_committed_dispatch(pg_bus,paused,text):
    deps,item = capture_one(pg_bus,paused,client_armed=True)
    resume(paused)
    assert replay.process(deps,item['id'])['state']=='committed'
    original=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    assert replay.dispatch_allowed(pg_bus,original,'echo')
    adapter.handle_event(_ev(text,ts='1.002'),'G0MPIM:1.002',deps)
    assert not replay.dispatch_allowed(pg_bus,original,'echo')
    latest=next(r for r in queue(pg_bus) if r['event_key']=='G0MPIM:1.002')
    assert latest['state']=='held'
    adapter.handle_event(_ev('more photos are broken',ts='1.003'),'G0MPIM:1.003',deps)
    assert next(r for r in queue(pg_bus) if r['event_key']=='G0MPIM:1.003')['state']=='held'


def test_external_cancel_is_visible_held_without_calendar_effect(pg_bus,paused):
    called=[]
    deps=_deps(pg_bus)
    deps.cancel_post_enabled=lambda:True
    deps.cancel_post=lambda *args:called.append(args)
    adapter.handle_event(_ev('please cancel today\'s post'),'G0MPIM:1.001',deps)
    item=queue(pg_bus)[0]
    assert item['context']['dispatch']['classification']=='cancel_post'
    resume(paused)
    assert replay.run_once(deps)=={'held':1}
    assert queue(pg_bus)[0]['reason']=='external_cancel_requires_reconciliation'
    assert not called
    assert not any(m['direction']=='outbound' for m in pg_bus.msgs)


def test_replay_table_and_rpc_are_private(pg_bus):
    result=pg_bus.engine.sql("select has_table_privilege('authenticated','support_slack_replay','SELECT'),"
                            "has_function_privilege('anon','support_slack_replay_claim(uuid,text,uuid)','EXECUTE'),"
                            "has_function_privilege('service_role','support_slack_replay_claim(uuid,text,uuid)','EXECUTE')")
    assert result=='f|f|t'


def test_changed_plan_and_cross_identity_cannot_replay_committed_effect(pg_bus,paused):
    deps,item=capture_one(pg_bus,paused)
    resume(paused)
    assert replay.process(deps,item['id'])['state']=='committed'
    stored=queue(pg_bus)[0]
    args={'p_id':item['id'],'p_identity':'scout','p_token':stored['claim_token'],
          'p_plan':stored['committed_plan']}
    with pytest.raises(RuntimeError,match='ownership mismatch'):
        pg_bus.engine.call('support_slack_replay_commit',args)
    args['p_identity']='echo'
    args['p_plan']['decision']['reason']='changed'
    with pytest.raises(RuntimeError,match='changed committed plan'):
        pg_bus.engine.call('support_slack_replay_commit',args)


def test_unavailable_rpc_fails_closed_without_legacy_dispatch(pg_bus,paused):
    pg_bus.transport.before_call=lambda *args: (_ for _ in ()).throw(ConnectionError('DB unavailable'))
    with pytest.raises(ConnectionError):
        adapter.handle_event(_ev('my photos are broken'),'G0MPIM:1.001',_deps(pg_bus))
    assert not pg_bus.msgs


@pytest.mark.parametrize('kind',['ack','hold_notice'])
@pytest.mark.parametrize('change',['STOP','source','tenant','human_takeover'])
def test_outbox_late_guard_never_posts_after_claim_change(pg_bus,paused,monkeypatch,kind,change):
    if kind=='ack':
        deps,item=capture_one(pg_bus,paused,text='is the calendar loaded?',client_armed=True,
            auto_answer=True,answer=lambda *args:{'body':'There are three draft posts.',
                                                'grounding':{'drafts':3}})
    else:
        deps,item=capture_one(pg_bus,paused,client_armed=True)
    resume(paused)
    assert replay.process(deps,item['id'])['state']=='committed'
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')==kind)
    original=replay.claim_delivery
    def claim(bus, claimed_row):
        won=original(bus,claimed_row)
        if change in ('STOP','human_takeover'):
            pg_bus.record_inbound(ticket_id=item['ticket_id'],slack_event_id='later',
                slack_ts='2.001',author_type='client',author_id='U_CLIENT',body=change)
        else:
            field,value=('source','website_tab') if change=='source' else ('client_id','g-other')
            pg_bus.set_ticket(item['ticket_id'],**{field:value})
        return won
    monkeypatch.setattr(replay,'claim_delivery',claim)
    monkeypatch.setattr(outbox,'_recipient_armed',lambda *args:True)
    monkeypatch.setenv('AGENT_FIXER_CHANNEL_ID','C_FIXER')
    posts=[]
    summary=Counter()
    outbox._dispatch_one(pg_bus,lambda *args,**kwargs:posts.append(args),row,
        identity=deps.identity,log=lambda *args:None,summary=summary)
    assert not posts
    assert summary['held']>=1
    assert pg_bus.message(row['id'])['delivery_status']=='held'


def test_outbox_posts_one_ready_replay_ack_when_current(pg_bus,paused,monkeypatch):
    deps,item=capture_one(pg_bus,paused,text='is the calendar loaded?',client_armed=True,
        auto_answer=True,answer=lambda *args:{'body':'There are three draft posts.',
                                            'grounding':{'drafts':3}})
    resume(paused)
    assert replay.process(deps,item['id'])['state']=='committed'
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    monkeypatch.setattr(outbox,'_recipient_armed',lambda *args:True)
    posts=[]
    def post(*args,**kwargs):
        posts.append((args,kwargs))
        return '2.001'
    summary=Counter()
    outbox._dispatch_one(pg_bus,post,row,identity=deps.identity,log=lambda *args:None,summary=summary)
    assert len(posts)==1
    assert pg_bus.message(row['id'])['delivery_status']=='posted'


def test_expired_plan_token_cannot_commit_after_reclaim(pg_bus,paused):
    deps,item=capture_one(pg_bus,paused)
    first=str(uuid.uuid4())
    second=str(uuid.uuid4())
    args={'p_id':item['id'],'p_identity':'echo','p_token':first}
    assert pg_bus.engine.call('support_slack_replay_claim',args)['state']=='planning'
    pg_bus.engine.sql(f"update {replay.TABLE} set claim_until=clock_timestamp()-interval '1 minute' "
                      f"where id={q(item['id'])}")
    args['p_token']=second
    assert pg_bus.engine.call('support_slack_replay_claim',args)['state']=='planning'
    args['p_token']=first
    args['p_plan']={'ticket_fields':{},'rows':[],'decision':{'ticket_id':item['ticket_id']}}
    with pytest.raises(RuntimeError,match='ownership mismatch'):
        pg_bus.engine.call('support_slack_replay_commit',args)
    assert all(m['direction']=='inbound' for m in pg_bus.msgs)


def test_invalid_plan_rolls_back_all_ticket_and_outbound_changes(pg_bus,paused):
    deps,item=capture_one(pg_bus,paused)
    token=str(uuid.uuid4())
    args={'p_id':item['id'],'p_identity':'echo','p_token':token}
    pg_bus.engine.call('support_slack_replay_claim',args)
    valid={'id':str(uuid.uuid4()),'ticket_id':item['ticket_id'],'direction':'outbound',
      'author_type':'echo','body':'Got it','delivery_status':'ready',
      'attachments':{'kind':'ack','identity':'echo','surface':'mpim','recipient_kind':'client'}}
    args['p_plan']={'ticket_fields':{'status':'triage'},'rows':[valid,{**valid,'id':str(uuid.uuid4()),
        'ticket_id':str(uuid.uuid4())}],'decision':{'ticket_id':item['ticket_id'],'identity_kind':'client'}}
    with pytest.raises(RuntimeError,match='invalid outbound'):
        pg_bus.engine.call('support_slack_replay_commit',args)
    assert pg_bus.ticket(item['ticket_id'])['status']=='new'
    assert all(m['direction']=='inbound' for m in pg_bus.msgs)
    assert queue(pg_bus)[0]['state']=='planning'


def test_sender_pause_blocks_replay_outbox_without_rpc_or_provider_calls(pg_bus,paused):
    deps,item=capture_one(pg_bus,paused,client_armed=True)
    resume(paused)
    replay.process(deps,item['id'])
    paused.write_text(json.dumps({'paused':True,'generation':'replay-acceptance'}))
    pg_bus.transport.before_call=lambda *args: (_ for _ in ()).throw(AssertionError('paused RPC'))
    assert replay.run_once(deps)=={}
    assert outbox.run_once(pg_bus,lambda *args,**kw:pytest.fail('paused send'),
                           identity=deps.identity)=={'paused':1}


def test_stale_replay_claim_is_quarantined_and_cannot_be_released(pg_bus,paused):
    deps,item=capture_one(pg_bus,paused,client_armed=True)
    resume(paused)
    replay.process(deps,item['id'])
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    assert pg_bus.claim_message(row['id'])
    pg_bus.mark_message(row['id'],'posting',meta_update={
        'claimed_at':(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()})
    summary=Counter()
    assert outbox._recover_stale_claims(pg_bus,deps.identity,lambda *args:None,
                                       summary=summary)==1
    current=pg_bus.message(row['id'])
    assert current['delivery_status']=='held'
    assert current['attachments']['slack_replay_delivery_uncertain'] is True
    assert summary['quarantined_held']==1
    assert not outbox.release_held(pg_bus,row['id'],approved_by='operator',identity=deps.identity)
    # A raw operator status edit cannot waive the persisted uncertainty either.
    pg_bus.mark_message(row['id'],'ready')
    assert not replay.dispatch_allowed(pg_bus,pg_bus.message(row['id']),'echo')


def test_replay_provider_timeout_remains_held_without_second_send(pg_bus,paused,monkeypatch):
    deps,item=capture_one(pg_bus,paused,text='is the calendar loaded?',client_armed=True,
        auto_answer=True,answer=lambda *args:{'body':'There are three draft posts.',
                                            'grounding':{'drafts':3}})
    resume(paused)
    replay.process(deps,item['id'])
    # Process just the ack so any separate internal/answer rows cannot cloud the
    # assertion that this exact source row is never posted twice after timeout.
    original_outbox=pg_bus.outbox
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    monkeypatch.setattr(pg_bus,'outbox',lambda status='ready',**kw:
        [m for m in original_outbox(status,**kw) if m['id']==row['id']])
    monkeypatch.setattr(outbox,'_recipient_armed',lambda *args:True)
    sends=[]
    def post(*args,**kw):
        sends.append(args)
        raise TimeoutError('Slack acceptance is unknown')
    result=outbox.run_once(pg_bus,post,identity=deps.identity,log=lambda *args:None)
    assert result['held']==1
    assert len(sends)==1
    assert pg_bus.message(row['id'])['attachments']['slack_replay_delivery_uncertain'] is True
    outbox.run_once(pg_bus,post,identity=deps.identity,log=lambda *args:None)
    assert len(sends)==1


def test_uncertain_replay_blocks_local_fence_drain(pg_bus,paused,monkeypatch):
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA','offline-sha')
    deps,item=capture_one(pg_bus,paused)
    resume(paused)
    replay.process(deps,item['id'])
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    pg_bus.mark_message(row['id'],'held',meta_update={'slack_replay_delivery_uncertain':True})
    paused.write_text(json.dumps({'paused':True,'generation':'replay-acceptance'}))
    result=fence.receipt(pg_bus)
    assert result['local_drained'] is False
    assert f'held:{row["id"]}' in result['blockers']


def test_staff_note_without_version_bump_invalidates_old_dispatch(pg_bus,paused):
    from agent.slack_convo import identity_gate
    deps=_deps(pg_bus,who=identity_gate.STAFF,client_armed=True,staff_armed=True,
      answer=lambda *args:{'body':'There are three draft posts.','grounding':{'drafts':3}})
    adapter.handle_event(_ev('is the calendar loaded?',channel_type='im',user='U_STAFF'),
                         'G0MPIM:1.001',deps)
    item=queue(pg_bus)[0]
    resume(paused)
    assert replay.process(deps,item['id'])['state']=='committed'
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='answer')
    assert replay.dispatch_allowed(pg_bus,row,'echo')
    version=pg_bus.ticket(item['ticket_id'])['request_version']
    pg_bus.record_inbound(ticket_id=item['ticket_id'],slack_event_id='staff-note',
        slack_ts='2.002',author_type='staff',author_id='U_STAFF',body='Please check November instead')
    assert pg_bus.ticket(item['ticket_id'])['request_version']==version
    assert not replay.dispatch_allowed(pg_bus,row,'echo')


def test_paused_issue_then_thanks_preserves_initial_request_once(pg_bus,paused):
    deps,first=capture_one(pg_bus,paused)
    thanks=_ev('thanks',ts='1.002')
    noted=adapter.handle_event(thanks,'G0MPIM:1.002',deps)
    assert noted.reason=='support_sender_paused'
    assert len(queue(pg_bus))==2
    current=next(r for r in queue(pg_bus) if r['event_key']=='G0MPIM:1.002')
    assert current['context']['chatter'] is True
    assert current['context']['dispatch']['noop'] is False
    assert current['context']['dispatch']['classification']=='code_fix'
    resume(paused)
    assert replay.run_once(deps)=={'held':1,'committed':1}
    requests=[m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='fixer_request']
    assert len(requests)==1
    assert 'my photos are broken' in requests[0]['body']
    assert pg_bus.ticket(first['ticket_id'])['status']=='triage'
    before=copy.deepcopy(pg_bus.msgs)
    assert adapter.handle_event(thanks,'G0MPIM:1.002',deps).duplicate
    assert pg_bus.msgs==before


@pytest.mark.parametrize('armed',[True,False])
def test_chatter_renews_only_untouched_delivery_authority_preserving_plan_and_hold(pg_bus,paused,armed):
    deps,item=capture_one(pg_bus,paused,text='is the calendar loaded?',client_armed=armed,
        auto_answer=True,answer=lambda *args:{'body':'There are three draft posts.','grounding':{'drafts':3}})
    resume(paused)
    replay.process(deps,item['id'])
    original=copy.deepcopy(queue(pg_bus)[0])
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    noted=adapter.handle_event(_ev('thanks!',ts='1.002'),'G0MPIM:1.002',deps)
    assert noted.reason=='chatter_noted'
    assert not noted.outbound_kinds
    renewed=pg_bus.message(row['id'])
    assert renewed['body']==row['body']
    assert renewed['delivery_status']==row['delivery_status']
    assert renewed['delivery_request_version']==2
    stored=next(r for r in queue(pg_bus) if r['id']==item['id'])
    for key in ('committed_plan','committed_ticket','ticket_snapshot','messages_snapshot'):
        assert stored[key]==original[key]
    assert stored['delivery_ticket_snapshot']['request_version']==2
    assert replay.dispatch_allowed(pg_bus,renewed,'echo') is armed
    assert sum(m['direction']=='outbound' for m in pg_bus.msgs)==sum(
        m['direction']=='outbound' for m in original['committed_plan']['rows'])


def test_chatter_does_not_rebind_attempted_or_uncertain_row(pg_bus,paused):
    deps,item=capture_one(pg_bus,paused,text='is the calendar loaded?',client_armed=True,
        auto_answer=True,answer=lambda *args:{'body':'There are three draft posts.','grounding':{'drafts':3}})
    resume(paused)
    replay.process(deps,item['id'])
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    assert replay.claim_delivery(pg_bus,row)
    assert replay.hold_delivery(pg_bus,row,'unknown prior send')['delivery_status']=='held'
    adapter.handle_event(_ev('thanks',ts='1.002'),'G0MPIM:1.002',deps)
    held=pg_bus.message(row['id'])
    assert held['delivery_status']=='held'
    assert held['delivery_request_version']==1
    assert held['attachments']['slack_replay_delivery_uncertain'] is True
    assert not replay.dispatch_allowed(pg_bus,held,'echo')
    assert not outbox.release_held(pg_bus,row['id'],approved_by='operator',identity=deps.identity)


def test_substantive_chatter_prefix_cannot_renew_old_authority(pg_bus,paused):
    deps,item=capture_one(pg_bus,paused,client_armed=True)
    resume(paused)
    replay.process(deps,item['id'])
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    adapter.handle_event(_ev('thanks cancel post',ts='1.002'),'G0MPIM:1.002',deps)
    latest=next(r for r in queue(pg_bus) if r['event_key']=='G0MPIM:1.002')
    assert latest['context']['chatter'] is False
    assert pg_bus.message(row['id'])['delivery_request_version']==1
    assert not replay.dispatch_allowed(pg_bus,row,'echo')


def ready_ack(pg_bus,paused):
    deps,item=capture_one(pg_bus,paused,text='is the calendar loaded?',client_armed=True,
        auto_answer=True,answer=lambda *args:{'body':'There are three draft posts.','grounding':{'drafts':3}})
    resume(paused)
    replay.process(deps,item['id'])
    return deps,next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')


@pytest.mark.parametrize('kind',['ack','hold_notice'])
def test_stale_quarantine_wins_slow_provider_completion(pg_bus,paused,monkeypatch,kind):
    if kind=='ack':
        deps,row=ready_ack(pg_bus,paused)
    else:
        deps,item=capture_one(pg_bus,paused,client_armed=True)
        resume(paused)
        replay.process(deps,item['id'])
        row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')==kind)
    monkeypatch.setattr(outbox,'_recipient_armed',lambda *args:True)
    monkeypatch.setenv('AGENT_FIXER_CHANNEL_ID','C_FIXER')
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA','selected-p1-fixture')
    entered,release=threading.Event(),threading.Event()
    posts=[]
    errors=[]
    summary=Counter()
    def post(*args,**kw):
        posts.append(args)
        entered.set()
        assert release.wait(5)
        return '2.001'
    def dispatch():
        try:
            outbox._dispatch_one(pg_bus,post,row,identity=deps.identity,
                summary=summary,log=lambda *args:None)
        except Exception as exc:
            errors.append(exc)
    thread=threading.Thread(target=dispatch)
    thread.start()
    try:
        assert entered.wait(5)
        pg_bus.mark_message(row['id'],'posting',meta_update={
            'claimed_at':(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()})
        assert outbox._recover_stale_claims(pg_bus,deps.identity,lambda *args:None)==1
    finally:
        release.set()
        thread.join(5)
    assert not errors and not thread.is_alive()
    stored=pg_bus.message(row['id'])
    assert stored['delivery_status']=='held'
    assert stored['slack_ts'] is None
    assert stored['attachments']['slack_replay_delivery_uncertain'] is True
    assert stored['attachments']['slack_replay_provider_ts']=='2.001'
    assert summary['posted']==0 and summary['held']==1
    assert len(posts)==1
    assert replay.finish_delivery(pg_bus,row,'2.001')==stored
    with pytest.raises(Exception,match='changed quarantined provider receipt'):
        replay.finish_delivery(pg_bus,row,'2.002')
    paused.write_text(json.dumps({'paused':True,'generation':'replay-acceptance'}))
    assert not fence.receipt(pg_bus)['local_drained']


def test_completion_wins_delayed_stale_quarantine_without_overwrite(pg_bus,paused,monkeypatch):
    deps,row=ready_ack(pg_bus,paused)
    assert replay.claim_delivery(pg_bus,row)
    pg_bus.mark_message(row['id'],'posting',meta_update={
        'claimed_at':(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()})
    entered,release=threading.Event(),threading.Event()
    original=replay.hold_delivery
    def hold(bus,current,why):
        entered.set()
        assert release.wait(5)
        return original(bus,current,why)
    monkeypatch.setattr(replay,'hold_delivery',hold)
    result=[]
    worker=threading.Thread(target=lambda:result.append(outbox._recover_stale_claims(
        pg_bus,deps.identity,lambda *args:None)))
    worker.start()
    try:
        assert entered.wait(5)
        finished=replay.finish_delivery(pg_bus,row,'2.001')
        assert finished['delivery_status']=='posted'
    finally:
        release.set()
        worker.join(5)
    assert result==[0] and not worker.is_alive()
    stored=pg_bus.message(row['id'])
    assert stored['delivery_status']=='posted' and stored['slack_ts']=='2.001'
    assert not stored['attachments'].get('slack_replay_delivery_uncertain')
    assert replay.finish_delivery(pg_bus,row,'2.001')==stored
    with pytest.raises(Exception,match='changed delivery receipt'):
        replay.finish_delivery(pg_bus,row,'2.002')


def test_lost_delivery_claim_and_finish_keep_same_token_and_receipt(pg_bus,paused):
    deps,row=ready_ack(pg_bus,paused)
    pg_bus.transport.lose_once.update({'support_slack_replay_delivery_claim',
                                     'support_slack_replay_delivery_finish'})
    calls=[]
    pg_bus.transport.before_call=lambda name,args:calls.append((name,copy.deepcopy(args)))
    assert replay.claim_delivery(pg_bus,row)
    assert replay.finish_delivery(pg_bus,row,'2.001')['delivery_status']=='posted'
    for name in ('support_slack_replay_delivery_claim','support_slack_replay_delivery_finish'):
        args=[args for called,args in calls if called==name]
        assert len(args)==2 and args[0]==args[1]
    assert pg_bus.message(row['id'])['slack_ts']=='2.001'


@pytest.mark.parametrize('status',['ready','held','posting','posted','failed','suppressed',None])
def test_uncertainty_scan_blocks_drain_independent_of_status(pg_bus,paused,monkeypatch,status):
    deps,item=capture_one(pg_bus,paused)
    resume(paused)
    replay.process(deps,item['id'])
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    pg_bus.mark_message(row['id'],status,meta_update={'slack_replay_delivery_uncertain':True})
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA','p1-status-scan')
    paused.write_text(json.dumps({'paused':True,'generation':'replay-acceptance'}))
    receipt=fence.receipt(pg_bus)
    assert not receipt['local_drained']
    assert f'uncertain:{row["id"]}' in receipt['blockers']


def test_delivery_token_and_identity_cannot_complete_another_claim(pg_bus,paused):
    deps,row=ready_ack(pg_bus,paused)
    assert replay.claim_delivery(pg_bus,row)
    args={'p_message_id':row['id'],'p_identity':'echo',
          'p_token':str(uuid.uuid4()),'p_ts':'2.001'}
    with pytest.raises(RuntimeError,match='ownership mismatch'):
        pg_bus.engine.call('support_slack_replay_delivery_finish',args)
    args['p_token']=row['attachments']['slack_replay_delivery_token']
    args['p_identity']='scout'
    with pytest.raises(RuntimeError,match='ownership mismatch'):
        pg_bus.engine.call('support_slack_replay_delivery_finish',args)
    assert pg_bus.message(row['id'])['delivery_status']=='posting'


def test_ordinary_untagged_claim_completion_keeps_existing_behavior(monkeypatch):
    from tests.test_slack_convo import FakeBus
    monkeypatch.setenv('SUPPORT_MESSAGES_FENCE_ENABLED','false')
    monkeypatch.setenv('AGENT_SLACK_BOT_USER_ID','U_ECHO_BOT')
    bus=FakeBus()
    deps=_deps(bus,client_armed=True,auto_answer=True,
        answer=lambda *args:{'body':'There are three draft posts.','grounding':{'drafts':3}})
    adapter.handle_event(_ev('is the calendar loaded?'),'ordinary',deps)
    row=next(m for m in bus.msgs if (m.get('attachments') or {}).get('kind')=='ack')
    assert 'slack_replay_id' not in row['attachments']
    monkeypatch.setattr(outbox,'_recipient_armed',lambda *args:True)
    sent=[]
    def post(*args,**kw):
        sent.append(args)
        return '2.001'
    summary=Counter()
    outbox._dispatch_one(bus,post,row,identity=deps.identity,summary=summary,log=lambda *args:None)
    assert len(sent)==1 and summary['posted']==1
    assert bus.message(row['id'])['delivery_status']=='posted'
    assert 'slack_replay_delivery_token' not in bus.message(row['id'])['attachments']


def ready_answer(pg_bus,paused,monkeypatch):
    deps,ack=ready_ack(pg_bus,paused)
    monkeypatch.setattr(outbox,'_recipient_armed',lambda *args:True)
    monkeypatch.setattr(outbox.config,'slack_convo_auto_answer_armed',lambda *args:True)
    row=next(m for m in pg_bus.msgs if (m.get('attachments') or {}).get('kind')=='answer')
    return deps,row


@pytest.mark.parametrize('author',['client','staff'])
def test_new_inbound_between_delivery_and_resolve_cannot_close_new_request(
        pg_bus,paused,monkeypatch,author):
    deps,row=ready_answer(pg_bus,paused,monkeypatch)
    ticket=pg_bus.ticket(row['ticket_id'])
    calls=[]
    def before(name,args):
        if name=='support_slack_replay_delivery_resolve':
            calls.append(name)
            pg_bus.record_inbound(ticket_id=row['ticket_id'],slack_event_id='new-request',
                slack_ts='3.001',author_type=author,
                author_id='U_CLIENT' if author=='client' else 'U_STAFF',
                body='Please check November instead')
    pg_bus.transport.before_call=before
    summary=Counter()
    sends=[]
    def post(*args,**kwargs):
        sends.append(args)
        return '2.001'
    outbox._dispatch_one(pg_bus,post,row,identity=deps.identity,summary=summary,log=lambda *args:None)
    assert calls==['support_slack_replay_delivery_resolve']
    assert len(sends)==1 and summary['posted']==1
    assert summary['resolved']==0
    current=pg_bus.ticket(row['ticket_id'])
    assert current['status']=='verification' and current['resolved_at'] is None
    assert current['request_version']==ticket['request_version']+(1 if author=='client' else 0)
    stored=pg_bus.message(row['id'])
    assert stored['delivery_status']=='posted' and stored['slack_ts']=='2.001'
    assert 'slack_replay_resolution_ticket' not in stored['attachments']
    item=next(r for r in queue(pg_bus) if r['id']==stored['attachments']['slack_replay_id'])
    assert item['resolution_receipts']=={}


def test_current_replay_answer_resolves_once_after_lost_response_and_newer_note_refuses(
        pg_bus,paused,monkeypatch):
    deps,row=ready_answer(pg_bus,paused,monkeypatch)
    pg_bus.transport.lose_once.add('support_slack_replay_delivery_resolve')
    calls=[]
    pg_bus.transport.before_call=lambda name,args:calls.append((name,copy.deepcopy(args)))
    summary=Counter()
    outbox._dispatch_one(pg_bus,lambda *args,**kwargs:'2.001',row,
        identity=deps.identity,summary=summary,log=lambda *args:None)
    assert summary['posted']==1 and summary['resolved']==1
    args=[args for name,args in calls if name=='support_slack_replay_delivery_resolve']
    assert len(args)==2 and args[0]==args[1]
    first=replay.resolve_delivery(pg_bus,row)
    assert first['resolved'] is True
    assert pg_bus.ticket(row['ticket_id'])['status']=='resolved'
    proof=next(r for r in queue(pg_bus) if r['id']==row['attachments']['slack_replay_id'])['resolution_receipts'][row['id']]
    assert proof['ticket']['status']=='resolved' and proof['slack_ts']=='2.001'
    assert 'slack_replay_resolution_inbound' not in pg_bus.message(row['id'])['attachments']
    pg_bus.record_inbound(ticket_id=row['ticket_id'],slack_event_id='later-client',slack_ts='3.001',
        author_type='client',author_id='U_CLIENT',body='That was for another month')
    assert pg_bus.ticket(row['ticket_id'])['status']=='verification'
    assert replay.resolve_delivery(pg_bus,row)['resolved'] is False


@pytest.mark.parametrize('control',['STOP','DND','do not disturb','human takeover'])
def test_production_shape_uses_verified_inbound_safety_without_imaginary_columns(
        pg_bus,paused,control):
    assert pg_bus.engine.sql("select count(*) from information_schema.columns where "
        "table_schema='public' and table_name='support_tickets' "
        "and column_name in ('stop','dnd','human_takeover')")=='0'
    deps,row=ready_ack(pg_bus,paused)
    assert replay.dispatch_allowed(pg_bus,row,'echo')
    adapter.handle_event(_ev(control,ts='3.001'),'G0MPIM:3.001',deps)
    current=next(r for r in queue(pg_bus) if r['event_key']=='G0MPIM:3.001')
    assert current['state']=='held'
    assert current['reason']=='STOP_DND_or_human_takeover'
    assert not replay.dispatch_allowed(pg_bus,pg_bus.message(row['id']),'echo')
    before=len(pg_bus.msgs)
    adapter.handle_event(_ev('thanks',ts='3.002'),'G0MPIM:3.002',deps)
    assert len(pg_bus.msgs)==before+1
    assert not replay.dispatch_allowed(pg_bus,pg_bus.message(row['id']),'echo')
