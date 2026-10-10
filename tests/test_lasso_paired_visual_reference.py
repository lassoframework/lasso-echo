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
    monkeypatch.setattr(cs,'_generation_log_attempt',lambda **kw:None)
    f,s,fa,sa=pair
    raw=io.BytesIO();Image.new('RGB',(1080,1350),'tan').save(raw,format='PNG');feedbytes=raw.getvalue()
    sha=hashlib.sha256(feedbytes).hexdigest();fa['image_sha256']=fa['evidence']['image_sha256']=sha
    import base64
    craft={'id':'craft-book','bytes':feedbytes,'b64':base64.b64encode(feedbytes).decode(),'mime':'image/png'}
    monkeypatch.setattr(cs,'_reference_images_for',lambda *a,**kw:('book',[craft]))
    import requests
    monkeypatch.setattr(requests,'get',lambda *a,**kw:SimpleNamespace(content=feedbytes,raise_for_status=lambda:None))
    anchor=[]
    def reviewer(key,refs):
        anchor.extend(refs)
        return SimpleNamespace(response_id='real-offline-review')
    monkeypatch.setattr(infographic_review,'AstraReviewer',reviewer)
    rendered=[]
    for color in ('red','green','blue'):
        output=io.BytesIO();Image.new('RGB',(1080,1920),color).save(output,format='PNG');rendered.append(output.getvalue())
    calls=[];grades=[]
    def generate(prompt,opts,**kw):
        pixels=rendered[len(calls)]
        calls.append(opts)
        return image_engine.ImageResult(image_bytes=pixels,engine='astra',model='test',prompt_used=prompt)
    def review(image_bytes,**kw):
        assert image_bytes==rendered[len(grades)] and kw['owned_lasso'] is True
        passed=len(grades)==2
        g=GradeResult({},passed,[],status='PASS' if passed else 'FAIL',
             reason='' if passed else 'Erase and reflow the complete wordmark inside x11 to89 percent')
        g.style_conformant=passed;g.style_violations=[] if passed else ['placement'];g.visual_standard_version='lasso-grounded-editorial-2026-10-09-v1'
        grades.append(g);return g
    monkeypatch.setattr(image_engine,'generate_image',generate)
    monkeypatch.setattr(infographic_review,'evaluate',review)
    out=tmp_path/'story.png'
    result=cs.generate('Hook',['Approved fact'],client=object(),account_key='lasso_ig',surface='Story',aspect='9:16',pixels='1080x1920',out_path=str(out),paired_feed_reference={'feed':f,'artifact':fa})
    assert result is not None and len(calls)==3
    assert calls[0]['reference_images'][0]['bytes']==feedbytes
    assert calls[0]['reference_images'][0]['id']=='paired-feed:feed:'+sha
    assert 'PAIRED FEED STYLE ONLY' in calls[0]['engine_prompts']['astra']
    assert 'VISUAL REFERENCE' in calls[0]['engine_prompts']['astra']
    assert calls[0]['reference_images'][1]==craft
    from agent.astra_prompt import story_text_grid
    for index in (1,2):
        assert calls[index]['repair_image_bytes']==rendered[index-1]
        assert 'reference_images' not in calls[index]
        prompt=calls[index]['engine_prompts']['astra']
        assert 'PAIRED FEED STYLE ONLY' not in prompt and 'VISUAL REFERENCE' not in prompt
        assert 'INDEPENDENT REVIEW CORRECTIONS' in prompt
        assert prompt.endswith(story_text_grid('1080x1920'))
    assert calls[2]['repair_image_bytes']!=rendered[0]
    assert out.read_bytes()==rendered[2]
    assert anchor[0]['bytes']==feedbytes and anchor[1]==craft
    evidence=json.loads((tmp_path/'story.png.review.json').read_text())
    assert evidence['style_conformant'] is True and evidence['style_violations']==[]
    assert evidence['paired_feed_reference']==dict(feed_id='feed',image_url=f['image_url'],image_sha256=sha,review_response_id='real-review-feed',source_hash=fa['source_identity']['source_hash'])
    assert [r['image_sha256'] for r in evidence['reviews']]==[hashlib.sha256(raw).hexdigest() for raw in rendered]
    assert [r['style_conformant'] for r in evidence['reviews']]==[False,False,True]

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
