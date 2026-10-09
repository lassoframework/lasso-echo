"""Finite transport validation, preserving real public-reader guards."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from PIL import Image
from agent.generated_hosted_byte_authority import HostedObjectReader, HostedByteHold

FIXTURE=Path(__file__).parent/'fixtures/generated_client_linux_positive/sitecustomize.py'


def transport(tmp_path):
    spec=importlib.util.spec_from_file_location('positive_fixture_transport',FIXTURE)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.ROOT=tmp_path;module.CONFIG=tmp_path/'config.json'
    (tmp_path/'calls.jsonl').touch();(tmp_path/'objects').mkdir()
    image=Image.new('RGB',(1024,1280),'#112233');image.save(tmp_path/'original.png')
    data=(tmp_path/'original.png').read_bytes()
    import hashlib
    key=module.PREFIX+hashlib.sha256(data).hexdigest()+'.png'
    module.object_path(key).write_bytes(data)
    return module,key,data


def test_actual_issuer_public_guard_with_synthetic_https_below_guard(tmp_path):
    fixture,key,data=transport(tmp_path)
    import socket
    import http.client
    reader=HostedObjectReader({'fixture-gym':['https://owned.example'+fixture.PREFIX]})
    with patch.object(socket,'getaddrinfo',fixture.dns), \
         patch.object(http.client.HTTPSConnection,'request',fixture.https_request), \
         patch.object(http.client.HTTPSConnection,'getresponse',fixture.https_response):
        assert reader.read('fixture-gym','https://owned.example'+key) == data
        with pytest.raises(HostedByteHold,match='outside_tenant_scope'):
            reader.read('other-gym','https://owned.example'+key)
        with patch.object(socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with pytest.raises(HostedByteHold,match='address_not_public'):
                reader.read('fixture-gym','https://owned.example'+key)
    calls=[json.loads(line)['kind'] for line in (tmp_path/'calls.jsonl').read_text().splitlines()]
    assert calls == ['dns','issuer_get']


def test_unexpected_dns_https_rpc_s3_requests_refused(tmp_path):
    fixture,key,data=transport(tmp_path)
    with pytest.raises(AssertionError):fixture.dns('api.openai.com',443,type=1)
    with pytest.raises(AssertionError):fixture.object_path('/other-tenant/image.png')
    with pytest.raises(AssertionError):fixture.https_request(SimpleNamespace(host='other.example'),'GET',key)
    with pytest.raises(AssertionError):fixture.s3(SimpleNamespace(meta=SimpleNamespace(endpoint_url='https://other.example')),'PutObject',{})
    import requests
    request=requests.Request('POST','https://supabase.synthetic.example/rest/v1/rpc/finalize_forward_schedule_staged_batch_20261008').prepare()
    with pytest.raises(AssertionError):fixture.send(None,request)


def test_real_provider_and_reviewer_validate_synthetic_transport_responses(tmp_path):
    fixture,key,data=transport(tmp_path)
    from agent.generated_infographic_preparation import AstraOriginalProvider, original_bytes, GymPaletteReviewer
    from agent.infographic_review import AstraReviewer, evaluate
    import urllib.request
    import os
    with patch.dict(os.environ,{'OPENAI_API_KEY':'SYNTHETIC'},clear=True), \
         patch.object(urllib.request,'urlopen',fixture.urlopen):
        result=AstraOriginalProvider('SYNTHETIC').create('SYNTHETIC fixture','synthetic-job')
        assert original_bytes(result,'synthetic-job') == (data,'synthetic-image')
        reviewer=GymPaletteReviewer(AstraReviewer('SYNTHETIC'),{'colors':['#112233','#aabbcc']})
        grade=evaluate(data,headline='SYNTHETIC',facts=[],vision_client=reviewer)
        assert grade.passed
