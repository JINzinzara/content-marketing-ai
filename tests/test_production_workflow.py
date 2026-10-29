"""제작 경로의 실제 저장·검증 동작을 확인한다."""
import json
from decimal import Decimal
import httpx
import pytest
from app.captions import caption_parts, caption_text
from app.db import connect, ensure_account, owned_post
from app.styles import FIELDS, latest_profile
from test_generation import enable, response_body
from test_styles import profile_form


def upload_reference(client,csrf,photo):
    assert client.post('/shop/references',data={'csrf':csrf,'caption':'기존 메뉴 말투'},files={'photo':('ref.png',photo,'image/png')}).status_code==200


def test_timing_idempotency_validation_ownership_and_review_rounds(client,csrf,new_post,settings):
    identifier=new_post(measurement_id='a'*32)
    payload={'csrf':csrf,'id':'a'*32,'revision':1,'action':'initial_preview','elapsed_ms':'2450.25'}
    for _ in range(2): assert client.post(f'/posts/{identifier}/timings',data=payload).status_code==200
    measured=client.get(f'/posts/{identifier}/measurements').json()
    assert len(measured['timings'])==1 and measured['timings'][0]['elapsed_ms']==2450.25
    assert measured['reviews']==[] and measured['ai_usage']==[]
    for value in ('NaN','Infinity','-1'):
        assert client.post(f'/posts/{identifier}/timings',data={**payload,'elapsed_ms':value}).status_code==422
    assert client.post(f'/posts/{identifier}/timings',data={**payload,'revision':2}).status_code==409
    assert client.post(f'/posts/{identifier}/timings',data={**payload,'action':'image_candidate','image_id':'missing'}).status_code==409
    assert client.post(f'/posts/{identifier}/timings',data={**payload,'csrf':'wrong'}).status_code==403
    review={'csrf':csrf,'revision':1,'revision_rounds':3,'facts_grade':'충족','product_grade':'부분 충족','style_grade':'충족'}
    assert client.post(f'/posts/{identifier}/review',data={**review,'revision_rounds':2**63}).status_code==422
    assert client.post(f'/posts/{identifier}/review',data=review).status_code==200
    assert client.get(f'/posts/{identifier}/measurements').json()['reviews'][0]['revision_rounds']==3
    data={'csrf':csrf,'revision':1,'title':'수정','caption':'다시 고친 본문','hashtags':'','layout':'split','aspect':'portrait'}
    assert client.post(f'/posts/{identifier}/edit',data=data).status_code==200
    assert client.post(f'/posts/{identifier}/review',data=review).status_code==409
    with connect(settings,write=True) as con:
        ensure_account(con,'other'); con.execute("UPDATE posts SET owner_id='other' WHERE id=?",(identifier,))
    assert client.get(f'/posts/{identifier}/measurements').status_code==404
    assert client.post(f'/posts/{identifier}/timings',data={**payload,'revision':2}).status_code==404


def test_whole_workflow_uses_selected_models_and_preserves_one_charge_and_publish(app,client,csrf,settings,new_post,photo):
    import base64
    from urllib.parse import parse_qs
    from test_instagram import attach,ready,publisher_handler
    from test_image_generation import enable_image
    from test_model_compare import image_bytes
    upload_reference(client,csrf,photo)
    enable(settings,'5'); enable_image(settings)
    settings.openai_model=settings.draft_model='gpt-5.4-mini'
    calls=[]; published=[]
    def handler(request):
        calls.append(request.url.path)
        if request.url.host=='api.openai.com':
            payload=json.loads(request.content)
            assert payload['model'] in {'gpt-5.4-mini','gpt-image-2'}
            if request.url.path.endswith('input_tokens'): return httpx.Response(200,json={'input_tokens':100})
            if payload['model']=='gpt-image-2':
                return httpx.Response(200,json={'usage':{'input_tokens':110,'output_tokens':100,'input_tokens_details':{'text_tokens':10,'image_tokens':100}},'data':[{'b64_json':base64.b64encode(image_bytes()).decode()}]})
            schema=payload['text']['format']['name']
            result={'summary':'따뜻한 스타일','attributes':{k:'따뜻한 '+k for k in FIELDS},'evidence':['직접 참고 사진'],'uncertainties':[]} if schema=='style' else {'title':'스푸키쿠키','caption':'스푸키쿠키\n\n할로윈 쿠키예요.\n10월 26일~11월 6일 한정 판매해요\n\n#할로윈 #쿠키','hashtags':'#할로윈 #쿠키','layout':'frame'}
            return httpx.Response(200,json=response_body(result))
        if request.method=='POST' and request.url.path.endswith('/media'):
            caption=parse_qs(request.content.decode())['caption'][0]
            assert caption.count('#할로윈')==caption.count('#쿠키')==1
        return publisher_handler(published)(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client=app.state.image_ai.client=app.state.instagram.client=external
        result=client.post('/shop/fill',data=profile_form(csrf,style='',reference_source='local')).json()
        form=profile_form(csrf,style=result['content']['summary'],proposal_version=result['proposal_version'],**{'style_'+k:v for k,v in result['content']['attributes'].items()})
        assert client.post('/shop',data=form).status_code==200
        identifier=new_post(product_name='스푸키쿠키',facts='할로윈 쿠키',period='10월 26일~11월 6일 한정 판매',measurement_id='f'*32)
        with connect(settings) as con: revision=owned_post(con,'local-owner',identifier)['revision']
        assert client.post(f'/posts/{identifier}/timings',data={'csrf':csrf,'id':'f'*32,'revision':revision,'action':'initial_preview','elapsed_ms':2000}).status_code==200
        response=client.post(f'/posts/{identifier}/images',data={'csrf':csrf,'revision':revision,'confirmed':'yes'},headers={'Accept':'application/json'})
        assert response.status_code==202
        image_id=response.json()['job_id']
        assert client.get(f'/posts/{identifier}/images/{image_id}').status_code==200
        for n,action in enumerate(('image_candidate','creation_to_ai_candidate')):
            assert client.post(f'/posts/{identifier}/timings',data={'csrf':csrf,'id':str(n)*32,'revision':revision,'action':action,'elapsed_ms':12000,'image_id':image_id}).status_code==200
        assert client.post(f'/posts/{identifier}/images/select',data={'csrf':csrf,'revision':revision,'confirmed':'yes','image_id':image_id}).status_code==200
        revision+=1
        assert client.post(f'/posts/{identifier}/edit',data={'csrf':csrf,'revision':revision,'title':'스푸키쿠키','caption':'스푸키쿠키\n\n할로윈 쿠키를 만나보세요.\n10월 26일~11월 6일 한정 판매해요\n\n#할로윈 #쿠키','hashtags':'#할로윈 #쿠키','layout':'split','aspect':'portrait'}).status_code==200
        revision+=1
        for _ in range(2): assert client.post(f'/posts/{identifier}/finalize',data={'csrf':csrf,'revision':revision,'confirmed':'yes'}).status_code==200
        assert client.get(f'/posts/{identifier}/download').content==client.get(f'/posts/{identifier}/image').content
        ready(settings); attach(settings)
        for _ in range(2): assert client.post(f'/posts/{identifier}/publish',data={'csrf':csrf,'revision':revision,'confirmed':'yes'}).status_code==200
    from app.db import balance
    with connect(settings) as con:
        assert owned_post(con,'local-owner',identifier)['publish_state']=='published'
        assert balance(con,'local-owner')==4
    assert published.count(('POST','/v25.0/12345/media_publish'))==1
    measured=client.get(f'/posts/{identifier}/measurements').json()
    assert len(measured['timings'])==3 and measured['unknown_cost_count']==0 and measured['known_cost_microusd']>0
