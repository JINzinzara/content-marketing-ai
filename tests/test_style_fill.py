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


def test_auto_style_fills_only_blanks_and_needs_confirmation(app,client,csrf,settings,photo):
    client.post('/shop',data=profile_form(csrf,style_palette='기존 베이지'))
    upload_reference(client,csrf,photo)
    enable(settings)
    calls=[]
    def handler(request):
        payload=json.loads(request.content); calls.append(payload)
        data=json.loads(payload['input'][0]['content'][0]['text'])
        assert data['past_post_examples'][0]['id'].startswith('local:')
        if request.url.path.endswith('input_tokens'):
            return httpx.Response(200,json={'input_tokens':100})
        result={'summary':'AI 요약','attributes':{k:'AI '+k for k in FIELDS},'evidence':['직접 추가한 참고 자료'],'uncertainties':['정확한 글꼴 미확인']}
        return httpx.Response(200,json=response_body(result))
    form=profile_form(csrf,1,style_palette='아직 저장 안 한 우드',style_voice='직접 입력한 존댓말',reference_source='local')
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client=external
        response=client.post('/shop/fill',data=form)
    assert response.status_code==200 and len(calls)==2
    result=response.json(); attrs=result['content']['attributes']
    assert attrs['palette']=='아직 저장 안 한 우드' and attrs['voice']=='직접 입력한 존댓말'
    assert attrs['background']=='AI background' and result['content']['summary']==form['style']
    with connect(settings) as con:
        assert latest_profile(con,'local-owner')['content']['attributes']['palette']=='기존 베이지'
    accepted={**form,'proposal_version':result['proposal_version'],**{'style_'+k:v for k,v in attrs.items()}}
    assert client.post('/shop',data=accepted).status_code==200
    with connect(settings) as con:
        current=latest_profile(con,'local-owner')
        assert current['content']['attributes']==attrs
        assert current['content']['reference_scope']['source']=='local'


def test_instagram_fill_is_bounded_owned_and_skips_deleted(app,client,csrf,settings,monkeypatch):
    enable(settings); settings.history_page_size=2
    with connect(settings,write=True) as con:
        con.execute("UPDATE accounts SET token='fake' WHERE id='local-owner'")
        ensure_account(con,'other')
        con.execute("INSERT INTO history(owner_id,media_id,caption,media_type,media_url,published_at) VALUES ('other','foreign','PRIVATE','IMAGE','https://example/foreign.jpg','9999')")
        for n in range(4):
            con.execute("INSERT INTO history(owner_id,media_id,caption,media_type,media_url,published_at,deleted_at) VALUES ('local-owner',?,?,?,?,?,?)",(str(n),'기존 본문','IMAGE' if n%2==0 else 'VIDEO','https://example/past.jpg',str(n),1 if n==3 else None))
    syncs=[]
    monkeypatch.setattr(app.state.instagram,'sync_history',lambda who:syncs.append(who))
    def handler(request):
        payload=json.loads(request.content); content=payload['input'][0]['content']; examples=json.loads(content[0]['text'])['past_post_examples']
        assert [r['id'] for r in examples]==['ig:2','ig:1']
        assert [r['image_index'] for r in examples]==[1,None] and len(content)==2
        if request.url.path.endswith('input_tokens'): return httpx.Response(200,json={'input_tokens':100})
        return httpx.Response(200,json=response_body({'summary':'피드 요약','attributes':{k:'' for k in FIELDS},'evidence':['ig:2 배경'],'uncertainties':[]}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client=external
        response=client.post('/shop/fill',data=profile_form(csrf,reference_source='instagram'))
    assert response.status_code==200 and syncs==['local-owner']
    assert response.json()['content']['reference_scope']['analyzed_ids']==['ig:2','ig:1']


def test_style_fill_empty_no_holes_failed_or_concurrent_does_not_overwrite(app,client,csrf,settings,photo,monkeypatch):
    form=profile_form(csrf,reference_source='instagram')
    response=client.post('/shop/fill',data=form)
    assert response.status_code==409 and response.json()['connect_url']=='/auth/instagram'
    form['reference_source']='local'
    assert client.post('/shop/fill',data=form).status_code==422
    full={**form,**{'style_'+k:'직접 입력' for k in FIELDS}}
    assert client.post('/shop/fill',data=full).json()['message']=='채울 빈칸이 없어요'
    upload_reference(client,csrf,photo); enable(settings)
    from fastapi import HTTPException
    def failed(*args,**kwargs): raise HTTPException(502,'mock timeout')
    monkeypatch.setattr(app.state.ai,'generate',failed)
    assert client.post('/shop/fill',data=form).status_code==502
    with connect(settings) as con: assert latest_profile(con,'local-owner','proposal') is None
    def concurrent(*args,**kwargs):
        from app.styles import Style
        assert client.post('/shop',data=profile_form(csrf,style_background='사람이 새로 저장')).status_code==200
        return Style(summary='AI 결과',attributes={k:'AI' for k in FIELDS},evidence=[],uncertainties=[])
    monkeypatch.setattr(app.state.ai,'generate',concurrent)
    assert client.post('/shop/fill',data=form).status_code==409
    with connect(settings) as con:
        assert latest_profile(con,'local-owner')['content']['attributes']['background']=='사람이 새로 저장'
        assert latest_profile(con,'local-owner','proposal') is None


def test_expired_instagram_connection_can_continue_via_oauth(app,client,csrf,settings,monkeypatch):
    from app.instagram import InstagramError
    with connect(settings,write=True) as con:
        con.execute("UPDATE accounts SET token='expired' WHERE id='local-owner'")
    def expired(who): raise InstagramError('다시 연결해주세요',kind='auth')
    monkeypatch.setattr(app.state.instagram,'sync_history',expired)
    response=client.post('/shop/fill',data=profile_form(csrf,reference_source='instagram'))
    assert response.status_code==409 and response.json()['connect_url']=='/auth/instagram'
