"""Initial paired feed pixels, corrective edit isolation and sidecar proof."""
import hashlib,io,json
from types import SimpleNamespace
from PIL import Image
from agent import creative_studio as cs, image_engine, infographic_review, lasso_current_artifact
from agent.grade_gate import GradeResult
from test_lasso_current_artifact import pair

def test_initial_anchor_and_only_rejected_pixels_corrective(monkeypatch,tmp_path,pair):
    monkeypatch.setenv('AGENT_NANO_ENABLED','true')
    monkeypatch.setenv('AGENT_LASSO_INFOGRAPHIC_QUALITY','true')
    monkeypatch.setenv('OPENAI_API_KEY','offline-test')
    monkeypatch.setattr(cs.config,'astra_reference_max',lambda:2)
    monkeypatch.setattr(cs,'_reference_images_for',lambda *a,**kw:('',[]))
    monkeypatch.setattr(cs,'_generation_log_attempt',lambda **kw:None)
    f,s,fa,sa=pair
    raw=io.BytesIO();Image.new('RGB',(1080,1350),'tan').save(raw,format='PNG');feedbytes=raw.getvalue()
    sha=hashlib.sha256(feedbytes).hexdigest();fa['image_sha256']=fa['evidence']['image_sha256']=sha
    import requests
    monkeypatch.setattr(requests,'get',lambda *a,**kw:SimpleNamespace(content=feedbytes,raise_for_status=lambda:None))
    anchor=[]
    def reviewer(key,refs):
        anchor.extend(refs)
        return SimpleNamespace(response_id='real-offline-review')
    monkeypatch.setattr(infographic_review,'AstraReviewer',reviewer)
    outbytes=io.BytesIO();Image.new('RGB',(1080,1920),'red').save(outbytes,format='PNG')
    rendered=outbytes.getvalue();calls=[];grades=[]
    def generate(prompt,opts,**kw):
        calls.append(opts)
        return image_engine.ImageResult(image_bytes=rendered,engine='astra',model='test',prompt_used=prompt)
    def review(*a,**kw):
        g=GradeResult({},bool(grades),[],status='PASS' if grades else 'FAIL',reason='' if grades else 'Replace neon network with tactile scene')
        g.style_conformant=bool(grades);g.style_violations=[] if grades else ['neon'];g.visual_standard_version='lasso-grounded-editorial-2026-10-09-v1'
        grades.append(g);return g
    monkeypatch.setattr(image_engine,'generate_image',generate)
    monkeypatch.setattr(infographic_review,'evaluate',review)
    out=tmp_path/'story.png'
    result=cs.generate('Hook',['Approved fact'],client=object(),account_key='lasso_ig',surface='Story',aspect='9:16',pixels='1080x1920',out_path=str(out),paired_feed_reference={'feed':f,'artifact':fa})
    assert result is not None and len(calls)==2
    assert calls[0]['reference_images'][0]['bytes']==feedbytes
    assert calls[0]['reference_images'][0]['id']=='paired-feed:feed:'+sha
    assert calls[1]['repair_image_bytes']==rendered and 'reference_images' not in calls[1]
    assert anchor[0]['bytes']==feedbytes
    evidence=json.loads((tmp_path/'story.png.review.json').read_text())
    assert evidence['style_conformant'] is True and evidence['style_violations']==[]
    assert evidence['paired_feed_reference']==dict(feed_id='feed',image_url=f['image_url'],image_sha256=sha,review_response_id='real-review-feed',source_hash=fa['source_identity']['source_hash'])
    assert evidence['reviews'][0]['style_conformant'] is False

def test_real_reviewer_labels_exact_anchor_and_craft_separately():
    payloads=[]
    def transport(url,headers,payload):
        payloads.append(payload)
        return 200,json.dumps({'id':'review','output':[{'content':[{'type':'output_text','text':'{}'}]}]})
    refs=[dict(id='paired-feed:144c4a30-2e54-5667-88e4-7ea5b3ceaf00:'+'a'*64,b64='eA==',mime='image/jpeg'),dict(id='craft',b64='eQ==')]
    reviewer=infographic_review.AstraReviewer('offline-test',refs,transport=transport)
    reviewer.ask_image(b'candidate','Question')
    content=payloads[0]['input'][0]['content']
    assert 'exact current reviewed paired FEED style anchor' in content[2]['text']
    assert 'style_conformant must be false' in content[2]['text']
    assert content[3]['image_url'].startswith('data:image/jpeg;')
    assert 'craft benchmark only' in content[4]['text']
    ordinary=infographic_review.AstraReviewer('offline-test',[dict(id='craft',b64='eA==')],transport=transport)
    ordinary.ask_image(b'candidate','Question')
    assert 'paired FEED style anchor' not in payloads[-1]['input'][0]['content'][2]['text']
