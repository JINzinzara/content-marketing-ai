# 뽀스팅 · 카페 콘텐츠 제작

혼자 운영하는 카페 사장님이 **사진과 상품 정보로 가게에 맞는 홍보물을 완성하는 시간과 수고를 줄이는 서비스**입니다.
실제 시간 절감·만족도는 아직 측정하지 않았습니다.

현재 앱은 스타일 저장·적용, GPT 문구 생성, GPT Image 2 사진 편집, 미리보기·직접 수정·다운로드·Instagram 게시 연결을 제공합니다.
라인업·품절 공지와 기존 게시물 가져오기 후 스타일 빈칸 자동 제안은 다음 구현입니다.
문구는 `gpt-5.4`, 이미지 편집은 `gpt-image-2`를 사용합니다.

## 실행

Python 3.12 이상에서 실행합니다. 기존 프로젝트의 비공개 `.env`와 `data/`를 유지합니다.
새 환경에서는 API가 기본 비활성화이며 실제 호출에는 키·활성화·양수 예산·단가 설정이 필요합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8765 --no-access-log
```

브라우저에서 http://127.0.0.1:8765 를 엽니다.
Instagram 연동에는 공개 HTTPS·현재 콜백·전문 계정 권한이 필요합니다.
[실행·설정 안내](https://app.notion.com/p/3fec6fc0469681e78763f5e1cc292803)

## 평가

문구는 `gpt-5.4-mini`와 `gpt-5.4`, 이미지 편집은 `gpt-image-1-mini`와 `gpt-image-2`를 비교하는 독립 실행기를 제공합니다.
원본과 합성 변형 사진 총 5장을 모델별로 한 번씩 처리하고, 중앙의 메인 상품 한 개를 보존합니다.
개별 사실·말투·상품 보존·스타일·자연스러움과 결과 묶음의 일관성을 사람이 판정합니다. 부분 충족은 합격이 아닙니다.
탐색은 자유롭게 진행하고 최종 비교에서는 모델별 설정·재생성 규칙을 고정합니다. 생성 원본과 세로 4:5(1080×1350) 미리보기를 모두 보관합니다.
실행기와 모의 검증을 구현했습니다. 이 비교의 유료 실행·사람 판정 결과는 아직 없습니다. 반응 분석은 별도 과제로 평가합니다.

[평가 기준과 실행 범위](docs/evaluation-protocol.md) · [유료 시험 실행 순서와 명령](docs/model-comparison-runbook.md)

`model_compare.py prepare`와 `report`는 오프라인입니다. `run`도 기본적으로 호출하지 않으며, `run --execute`만 유료 API를 호출합니다. 실행 기록·사진·사람 판정은 `comparison/runs/`에 보관하며 Git에서 제외합니다.

## 코드 확인

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
```

시험은 임시 자료·모의 HTTP를 사용하며 유료 API·실제 Instagram 게시를 실행하지 않습니다.
앱은 단일 프로세스로 실행하며 이미지 작업 도중 재시작하면 결과 확인이 필요합니다.

## 유지하는 규칙

- 원본과 후보를 비교·승인합니다. AI의 상품 보존·사실 정확도를 보장하지 않습니다.
- 가격은 기본 비표시, 무료 완성본 5건과 AI 호출 한도는 별도입니다.
- 같은 완성본의 재다운로드·게시 재시도는 중복 차감하지 않습니다.
- 예산·비용 예약·소유권·CSRF·중복 요청·결과 불명 처리를 유지합니다.
- `.env`·`data/`·개인 평가 기록은 Git에 올리지 않습니다.
- 글꼴·캐릭터와 라이선스는 `app/assets/fonts/`, `app/static/`에 보존합니다.

[서비스 계획](https://app.notion.com/p/3fec6fc046968187be31fb2eed306e5b) · [개발일지](https://app.notion.com/p/3fec6fc0469681a8a6a8d8f8d4820a29)
