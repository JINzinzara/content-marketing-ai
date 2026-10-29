"""제작 경로의 실제 저장·검증 동작을 확인한다."""
import httpx
import pytest
from app.captions import caption_parts, caption_text
from app.db import connect, owned_post
from test_generation import enable, response_body


@pytest.mark.parametrize('body,tags,expected', [
    ('쿠키\n\n10월 26일~11월 6일 한정\n\n#할로윈 #카페', '#할로윈 #카페 #쿠키 #쿠키', ('쿠키\n\n10월 26일~11월 6일 한정','#할로윈 #카페 #쿠키')),
    ('C# 수업 소개', '#codeit #CODEIT', ('C# 수업 소개','#codeit')),
    ('본문 #대구카페 안내', '#카페', ('본문  안내','#카페 #대구카페')),
])
def test_caption_cleanup_keeps_facts_and_uses_unique_tags(body, tags, expected):
    assert caption_parts(body,tags)==expected
    assert caption_parts(*expected)==expected
    assert caption_text({'caption':body,'hashtags':tags})=='\n\n'.join(expected)


def test_moving_tags_does_not_hide_unconfirmed_claims():
    from app.text_ai import Draft,validate_draft
    from fastapi import HTTPException
    body,tags=caption_parts('쿠키 소개 #무설탕','')
    with pytest.raises(HTTPException):
        validate_draft(Draft(title='쿠키',caption=body,hashtags=tags,layout='split'),{'product_name':'쿠키'})


def test_generated_edited_and_legacy_captions_have_one_tag_group(app, client, csrf, settings, new_post):
    identifier = new_post()
    enable(settings)
    draft={'title':'밤 라테','caption':'밤 라테를 소개해요\n\n#카페 #라테','hashtags':'#카페 #라테 #라테','layout':'frame'}
    def handler(request):
        return httpx.Response(200,json={'input_tokens':100} if request.url.path.endswith('input_tokens') else response_body(draft))
    with httpx.Client(transport=httpx.MockTransport(handler)) as external:
        app.state.ai.client=external
        assert client.post(f'/posts/{identifier}/generate',data={'csrf':csrf}).status_code==200
    with connect(settings) as con:
        post=owned_post(con,'local-owner',identifier)
    assert post['hashtags']=='#카페 #라테' and '#' not in post['caption']
    data={'csrf':csrf,'revision':post['revision'],'title':'밤 라테','caption':'사람이 고친 본문\n\n#카페','hashtags':'#카페 #카페','layout':'split','aspect':'portrait'}
    assert client.post(f'/posts/{identifier}/edit',data=data).status_code==200
    with connect(settings,write=True) as con:
        con.execute('UPDATE posts SET caption=?,hashtags=? WHERE id=?',('기존 본문\n\n#카페','#카페 #카페',identifier))
    page=client.get(f'/posts/{identifier}')
    assert page.context['post']['caption']=='기존 본문' and page.context['post']['hashtags']=='#카페'
    with connect(settings) as con:
        post=owned_post(con,'local-owner',identifier)
        assert post['caption'].endswith('#카페')  # 읽기만으로 과거 원본을 바꾸지 않는다.
        assert caption_text(post).count('#카페')==1
