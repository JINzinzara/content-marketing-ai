# 추가 설정·사용 안내

[README로 돌아가기](../README.md)

아래 내용은 운영체제별 실행, AI 생성, 실제 Instagram 연동이나 개발 검사를 할 때 참고한다.<br>
Secret Key를 넣은 `.env`와 사용자 자료 `data/`는 반드시 Git에서 ignore 시킬 것.

## Windows

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m uvicorn backend.main:create_app --factory --port 8765 --workers 1 --no-access-log
```

## LLM 연결

`.env`를 만들고 아래 값을 설정한다. 이미 `.env`가 있을 경우, 같은 항목을 수정하고 서버를 재시작한다.

본인의 API 키와 허용 예산을 설정한 뒤 필요한 기능을 활성화한다.

- 스타일·문구: `OPENAI_API_KEY`, `OPENAI_ENABLED=true`, 양수의 `OPENAI_BUDGET_USD`.
- 스타일: `OPENAI_MODEL=gpt-5.4-mini`.<br> 비용: `OPENAI_STYLE_INPUT_CEILING_USD_PER_MILLION`,`OPENAI_STYLE_OUTPUT_USD_PER_MILLION`.
- 문구: `OPENAI_DRAFT_MODEL=gpt-5.4-mini`.<br> 비용: `OPENAI_DRAFT_INPUT_CEILING_USD_PER_MILLION`,`OPENAI_DRAFT_OUTPUT_USD_PER_MILLION`.
- 이미지: `OPENAI_IMAGE_ENABLED=true`, `OPENAI_IMAGE_MODEL=gpt-image-2`, `OPENAI_IMAGE_QUALITY=low`.  `IMAGE_CALL_RESERVE_USD`, `IMAGE_TEXT_INPUT_USD_PER_MILLION`, `IMAGE_INPUT_USD_PER_MILLION`, `IMAGE_OUTPUT_USD_PER_MILLION`.

### `.env` 예시

```dotenv
OPENAI_API_KEY=본인의-API-키
OPENAI_ENABLED=true
OPENAI_MODEL=gpt-5.4-mini
OPENAI_DRAFT_MODEL=gpt-5.4-mini
OPENAI_BUDGET_USD=1.00

OPENAI_STYLE_INPUT_CEILING_USD_PER_MILLION=0.75
OPENAI_STYLE_OUTPUT_USD_PER_MILLION=4.50

OPENAI_DRAFT_INPUT_CEILING_USD_PER_MILLION=0.75
OPENAI_DRAFT_OUTPUT_USD_PER_MILLION=4.50

OPENAI_IMAGE_ENABLED=true
OPENAI_IMAGE_MODEL=gpt-image-2
OPENAI_IMAGE_QUALITY=low

IMAGE_CALL_RESERVE_USD=0.10
IMAGE_TEXT_INPUT_USD_PER_MILLION=2.50
IMAGE_INPUT_USD_PER_MILLION=4.00
IMAGE_OUTPUT_USD_PER_MILLION=15.00
```

텍스트 비용의 기본값: 입력 0.75·출력 4.50 USD/100만 토큰이다.<br>
이미지 비용 기본값: 명시적으로 설정.<br> 
프로젝트에서 사용한 가격 스냅샷은 `backend/comparison/spooky-cookie.json`의 `price_snapshot_date`·`models`에서 확인할 수 있다. 실질적 유료 호출은 [공식 요금](https://developers.openai.com/api/docs/pricing)를 확인한다.

앱의 **연결 상태 → 설정 확인**에서 연결 확인이 가능하다. 변경 후 서버를 재시작한다. 예산은 DB에 기록된 이전 호출·미확정 예약을 포함하는 누적 한도이며 이미지 예약액은 청구 상한을 의미하지 않는다.

## Instagram 연동

실제 계정 연결·게시·반응 조회를 재현할 때만 아래 설정을 준비한다.

앱에 구현된 연결 경로는 Instagram Login으로 공개 HTTPS 주소·Meta 앱·계정의 권한을 준비하고 다음 변수를 설정한다.

```dotenv
APP_ENV=production
PUBLIC_BASE_URL=https://본인의-공개-주소
INSTAGRAM_APP_ID=본인의-앱-ID
INSTAGRAM_APP_SECRET=본인의-앱-시크릿
INSTAGRAM_REDIRECT_URI=https://본인의-공개-주소/auth/instagram/callback
INSTAGRAM_API_VERSION=v25.0
SESSION_SECRET=생성한-32자-이상-비밀값
TOKEN_ENCRYPTION_KEY=생성한-Fernet-키
```

Meta 앱의 콜백도 정확히 같은 Redirect URI를 등록한다. 코드 요청 범위는 `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_insights`이다. 

### 키 생성

```bash
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

공개 HTTPS 설정을 마친 경우 `APP_ENV=production`인 `.env`를 사용해 다음처럼 실행한다.

```bash
.venv/bin/python -m uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 8765 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log
```

위 프록시 설정은 로컬에서 연결되는 터널을 전제로 한다. 등록한 공개 HTTPS 주소로 접속 한다. 예약·반응 수집·브리핑은 이 프로세스 안에서 동작하므로 `--workers 1`을 유지한다.

## 사용 흐름

1. **가게 스타일:** 가게명·소개·스타일을 직접 입력 또는 기존 게시물 분석하여 제안 받고 확인·저장
2. **콘텐츠 작업실:** 사진과 상품명·확인된 사실·판매 기간·목적 입력
3. **검수와 완성:** 문구와 원본·생성 후보 비교, 수정한 뒤 현재 미리보기를 확정·다운로드 (게시는 별도 실행)
4. **오늘 메뉴·품절:** 메뉴명 등록 후, 당일 라인업 확정. 기본 글자 공지 또는 사진과 함께 라인업 Feed/Story와 품절 Story 생성
5. **반응과 브리핑:** 과거 글의 저장 반응을 조회·갱신, 브리핑을 통해 새 초안 제안 (자료 부족할 시, 제안 생성 제한)

어제 구성을 복사하면 미확정 상태이며 품절은 복사하지 않는다. 라인업이나 사진을 바꾸면 관련 예약을 취소하고 다시 확인한다. 예약 시각은 앱이 작업 완료 후 약 30초마다 확인하는 방식이며, 정확한 초 단위 실행을 보장하지 않는다.

무료 완성본 5건과 LLM 호출 한도는 별도이며, 확정 시 제작권이 차감된다. 같은 게시물의 재확정·다운로드·게시 재시도에는 중복 차감하지 않는다. 추가 결제 서비스는 미구현 상태이다.

## Test

검사를 실행할 때만 테스트 의존성을 추가gks다. 아래 명령은 가상환경이 활성화된 상태를 전제로 한다.

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m pip check
```

Node.js가 없으면 JavaScript 로직 검사 두 개는 제외한다. Windows 명령은 별도로 실행 검증하지 않는다.

## 문제 해결

- **모듈을 찾지 못함:** 저장소 루트에서 설치·실행했는지, `backend.main:create_app`을 사용했는지 확인
- **AI 생성 불가:** 연결 상태 또는 키·활성화·단가·양수 예산·누적 비용 확인
- **공개 HTTPS 실행 실패:** HTTPS 주소·콜백 일치·세션 키·Fernet 키·실제 접속 주소 확인
- **예약이 실행되지 않음:** 서버가 켜져 있는지, 당일 확정·버전·연결 상태가 유효한지 확인
- **과거 반응 없음:** 계정의 지표 지원·권한·수집 상태 확인. 결측과 실제 반응 0을 구분
