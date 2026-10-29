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
