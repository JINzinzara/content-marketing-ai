"""JS를 실제 Node에서 실행해 태그 규칙과 시간의 재전송·출처 연결을 확인한다."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.captions import caption_parts

ROOT = Path(__file__).parents[1]


def run_node(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('브라우저 로직 시험에는 Node.js가 필요합니다')
    subprocess.run([node,'-e',script],cwd=ROOT,check=True,capture_output=True,text=True)


def test_browser_caption_rules_match_saved_and_published_text():
    cases=[('스푸키쿠키\n\n10월 26일~11월 6일\n\n#할로윈 #카페','#할로윈 #쿠키 #쿠키'),
           ('C# 수업과 #카페 소개','#Codeit #codeit'),('본문\n\n#대구카페',''),('이모지 👻 #쿠키','#쿠키')]
    expected=[caption_parts(*c) for c in cases]
    run_node("const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');"
             "vm.runInThisContext(fs.readFileSync('app/static/captions.js','utf8'));"
             f"assert.deepEqual({json.dumps(cases,ensure_ascii=False)}.map(c=>captionParts(...c)),{json.dumps(expected,ensure_ascii=False)});")


def test_browser_timing_keeps_first_ready_duration_on_retry_and_never_uses_other_post():
    run_node(r"""
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const store=new Map(), sent=[]; let time=1000, reject=false;
const context={dataset:{post:'own-post',creation:'a'.repeat(32),revision:'2'}};
const message={textContent:''};
global.document={getElementById:id=>id==='production-context'?context:message,
  querySelector:selector=>selector==='[name="csrf"]'?{value:'test-csrf'}:{decode:async()=>{}},addEventListener:()=>{}};
global.sessionStorage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)};
Object.defineProperty(global,'performance',{value:{timeOrigin:0,now:()=>time},configurable:true});
global.requestAnimationFrame=callback=>callback();
global.fetch=async(url,options)=>{sent.push(Object.fromEntries(options.body));return {ok:!reject};};
vm.runInThisContext(fs.readFileSync('app/static/production.js','utf8'));
(async()=>{
  store.set('posting:preview-timing',JSON.stringify({id:'b'.repeat(32),action:'initial_preview',start:1}));
  await recordPreviewReady(); assert.equal(sent.length,0);
  store.set('posting:preview-timing',JSON.stringify({id:'d'.repeat(32),action:'edit_preview',post:'own-post',revision:2,start:100}));
  await recordPreviewReady(); assert.equal(sent.length,0);
  store.set('posting:preview-timing',JSON.stringify({id:'a'.repeat(32),action:'initial_preview',start:100}));
  reject=true; await recordPreviewReady(); assert.equal(sent[0].elapsed_ms,'900');
  time=5000; context.dataset.revision='3'; reject=false; await recordPreviewReady(); assert.equal(sent[1].elapsed_ms,'900'); assert.equal(sent[1].revision,'2');
  store.set('posting:image-timing',JSON.stringify({id:'c'.repeat(32),action:'image_candidate',post:'other-post',previous:'old',start:200}));
  await recordCandidateReady({id:'new'}); assert.equal(sent.length,2);
  store.set('posting:image-timing',JSON.stringify({id:'c'.repeat(32),action:'image_candidate',post:'own-post',previous:'old',start:200}));
  store.set('posting:creation-timing',JSON.stringify({id:'a'.repeat(32),action:'initial_preview',start:100}));
  await recordCandidateReady({id:'old'}); assert.equal(sent.length,2);
  await recordCandidateReady({id:'new'});
  assert.deepEqual(sent.slice(2).map(r=>r.action),['image_candidate','creation_to_ai_candidate']);
  assert.equal(sent[2].elapsed_ms,'4800'); assert.equal(sent[3].elapsed_ms,'4900');
  assert.notEqual(sent[2].id,sent[3].id);
  assert.equal(store.has('posting:image-timing'),false);
})().catch(error=>{console.error(error);process.exit(1);});
""")
