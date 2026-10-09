"""Test-only subprocess transports. No production module or guard is patched.

Loaded only by an explicitly prepended fixture PYTHONPATH. Configuration and
objects live in /data, permitting real CLI processes to resume across exits.
Every unlisted HTTP/DNS/S3 operation fails closed; no fallback reaches network.
"""
import base64
import hashlib
import http.client
import io
import json
import os
import re
import socket
import urllib.request
from pathlib import Path

ROOT = Path('/data/positive-transport')
CONFIG = ROOT / 'config.json'
HOST = 'owned.example'
PREFIX = '/echo-generated-originals/fixture-gym/'


def config():
    value = json.loads(CONFIG.read_text())
    assert set(value) == {'service_dsn'}
    assert 'host=/tmp/' in value['service_dsn'] and 'user=positive_service' in value['service_dsn']
    return value


def record(kind):
    with (ROOT/'calls.jsonl').open('a') as stream:
        stream.write(json.dumps({'kind':kind})+'\n')


def object_path(path):
    assert re.fullmatch(re.escape(PREFIX)+r'[0-9a-f]{64}\.png',path)
    return ROOT/'objects'/path.rsplit('/',1)[1]


def dns(host, port, *args, **kwargs):
    assert host == HOST and port == 443 and kwargs.get('type') == socket.SOCK_STREAM
    record('dns')
    # Finite synthetic DNS answer enters the unchanged global-address predicate.
    return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]


class BytesResponse(io.BytesIO):
    status = 200
    def __init__(self,data):
        super().__init__(data)
        self.headers={'Content-Length':str(len(data)),'Content-Type':'image/png'}
    def getheader(self,name,default=None):
        return self.headers.get(name,default)
    def read1(self,n=-1,**kwargs):
        return self.read(n)


def https_request(self,method,path,body=None,headers=None,**kwargs):
    assert self.host == HOST and getattr(self,'_address',None) == '93.184.216.34'
    assert method == 'GET' and body is None and headers == {'Accept-Encoding':'identity'}
    self._positive_path=object_path(path)
    record('issuer_get')


def https_response(self):
    assert self.host == HOST and hasattr(self,'_positive_path')
    return BytesResponse(self._positive_path.read_bytes())


def urlopen(request,*args,**kwargs):
    assert isinstance(request,urllib.request.Request)
    assert request.full_url == 'https://api.openai.com/v1/responses' and request.method == 'POST'
    assert os.environ.get('OPENAI_API_KEY') == 'SYNTHETIC'
    assert request.get_header('Authorization') == 'Bearer SYNTHETIC'
    payload=json.loads(request.data)
    assert payload['model'] == 'gpt-6-astra'
    if 'tools' in payload:
        assert set(payload) == {'model','store','metadata','input','tools','tool_choice'}
        assert payload['store'] is True and set(payload['metadata']) == {'echo_generation_job_id'}
        assert payload['tools'][0]['type'] == 'image_generation'
        record('generate')
        value={'id':'synthetic-generation','model':'gpt-6-astra','status':'completed','metadata':payload['metadata'],
               'output':[{'type':'image_generation_call','id':'synthetic-image',
                          'result':base64.b64encode((ROOT/'original.png').read_bytes()).decode()}]}
    else:
        assert set(payload) == {'model','input','text'}
        content=payload['input'][0]['content']
        assert len(content) == 2 and content[1]['type'] == 'input_image'
        actual=base64.b64decode(content[1]['image_url'].split(',',1)[1],validate=True)
        assert actual == (ROOT/'original.png').read_bytes()
        assert 'PALETTE REQUIREMENT' in content[0]['text']
        record('review')
        review=dict(scores=dict(concept=20,hierarchy=20,readability=20,craft=15,originality=15,integration=10),
                    copy_complete=True,copy_accurate=True,placement_safe=True,placement_violations=[],
                    issues=[],palette_matches=True)
        value={'id':'synthetic-review','output':[{'content':[{'type':'output_text','text':json.dumps(review)}]}]}
    return BytesResponse(json.dumps(value).encode())


def s3(self,operation,params):
    assert os.environ.get('AGENT_S3_ACCESS_KEY_ID') == 'SYNTHETIC'
    assert self.meta.endpoint_url == 'https://s3.synthetic.example'
    assert params['Bucket'] == 'synthetic-bucket'
    path=object_path('/'+params['Key'])
    if operation == 'PutObject':
        assert set(params) == {'Bucket','Key','Body','ContentType','IfNoneMatch'}
        assert params['ContentType'] == 'image/png' and params['IfNoneMatch'] == '*'
        data=params['Body']; assert type(data) is bytes and data == (ROOT/'original.png').read_bytes()
        assert hashlib.sha256(data).hexdigest()+'.png' == path.name
        with path.open('xb') as stream: stream.write(data)
        record('upload'); return {}
    assert operation == 'GetObject' and set(params) == {'Bucket','Key'}
    record('s3_get'); return {'Body':io.BytesIO(path.read_bytes())}


RPCS={
 'generated_client_service_preparation_20261009',
 'stage_forward_schedule_batch_20261008',
 'forward_schedule_batch_status_20261008',
}


def send(self,request,**kwargs):
    import requests
    from urllib.parse import urlsplit, parse_qs
    target=urlsplit(request.url)
    if target.hostname == HOST:
        assert request.method == 'GET' and target.scheme == 'https' and not target.query
        data=object_path(target.path).read_bytes(); record('owner_get')
        response=requests.Response();response.status_code=200
        response.headers={'Content-Length':str(len(data)),'Content-Type':'image/png'}
        response.raw=BytesResponse(data);response._content=False
        return response
    assert os.environ.get('SUPABASE_SERVICE_ROLE_KEY') == 'SYNTHETIC'
    assert target.scheme == 'https' and target.netloc == 'supabase.synthetic.example'
    assert request.headers['apikey'] == 'SYNTHETIC' and request.headers['Authorization'] == 'Bearer SYNTHETIC'
    if request.method == 'GET':
        assert target.path == '/rest/v1/fixer_forward_media_tenant_alias_20261006'
        assert parse_qs(target.query,strict_parsing=True) == {
            'select':['alias_key,tenant_id'],'alias_key':['eq.fixture-gym'],'limit':['2']}
        import psycopg
        with psycopg.connect(config()['service_dsn'],autocommit=True) as conn:
            rows=conn.execute('select alias_key,tenant_id from public.fixer_forward_media_tenant_alias_20261006 where alias_key=%s limit 2',('fixture-gym',)).fetchall()
        assert all(alias == tenant == 'fixture-gym' for alias,tenant in rows)
        record('tenant_alias_read')
        response=requests.Response();response.status_code=200
        response._content=json.dumps([dict(alias_key=alias,tenant_id=tenant) for alias,tenant in rows]).encode()
        response.headers={'Content-Type':'application/json'}
        return response
    assert request.method == 'POST' and target.path.startswith('/rest/v1/rpc/') and not target.query
    name=target.path.rsplit('/',1)[1];assert name in RPCS
    args=json.loads(request.body)
    assert all(re.fullmatch('p_[a-z_]+',k) for k in args)
    import psycopg
    from psycopg.types.json import Jsonb
    values=[Jsonb(v) if isinstance(v,(dict,list)) else v for v in args.values()]
    with psycopg.connect(config()['service_dsn'],autocommit=True) as conn:
        # Only actual immutable service RPCs execute under restricted identity.
        try:
            value=conn.execute('select public.'+name+'('+','.join(k+'=>%s' for k in args)+')',values).fetchone()[0]
        except psycopg.Error as exc:
            # Disposable synthetic DB only. Retain bounded SQL diagnostics while
            # preserving the original refusal; never fabricate an RPC response.
            with (ROOT/'calls.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(kind='rpc_refused',rpc=name,
                    sqlstate=exc.sqlstate,message=(exc.diag.message_primary or '')[:500]))+'\n')
            raise
    record(name)
    response=requests.Response();response.status_code=200;response._content=json.dumps(value).encode()
    response.headers={'Content-Type':'application/json'}
    return response


def deny_connect(self,address):
    raise AssertionError('unexpected socket transport')


def install():
    # Fatal startup refusal prevents Python's normal sitecustomize error fallback.
    config()
    socket.getaddrinfo=dns
    socket.socket.connect=deny_connect
    http.client.HTTPSConnection.request=https_request
    http.client.HTTPSConnection.getresponse=https_response
    urllib.request.urlopen=urlopen
    import requests
    requests.sessions.Session.send=send
    from botocore.client import BaseClient
    BaseClient._make_api_call=s3


if __name__ == 'sitecustomize':
    try:
        install()
    except BaseException:
        import os
        os._exit(90)
