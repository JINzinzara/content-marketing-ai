# content-marketing-ai

카페의 Instagram 게시물 제작·편집·게시·반응 분석을 위한 개인 프로젝트입니다.
텍스트·사진 이해는 `gpt-5.4`, 이미지 편집은 `gpt-image-2`를 사용합니다.
API는 기본 비활성화이며 실제 연결과 유료 생성 품질은 아직 검증하지 않았습니다.

## 설치와 실행

Python 3.12 이상에서 저장소 폴더를 열고 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
cp -n .env.example .env
.venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8765 --no-access-log
```

브라우저에서 http://127.0.0.1:8765 를 엽니다. 종료는 Control+C입니다.
단일 프로세스로 실행합니다. 이미지 작업 중 재시작하면 결과 불명으로 남기고 자동 재호출하지 않습니다.

## 테스트

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
```

2026-10-21: 자동·모의 시험 75개 통과. 테스트는 임시 DB·사진과 모의 HTTP를 사용하며 실제 API나 공개 게시를 호출하지 않습니다.
설치 패키지에도 글꼴·라이선스·템플릿·스키마가 포함되는지 별도로 확인했습니다.
Starlette TestClient의 httpx 호환 경로 폐기 예정 경고 1개는 남아 있습니다.

## 이름과 처리 규칙

- 텍스트 AI: `app/text_ai.py`의 `TextAI`, `validate_draft()`.
- Python 설정 속성: `Settings.redirect_url`. Meta 요청 필드명은 프로토콜에 맞게 `redirect_uri`입니다.
- Instagram 인증 코드 교환: `Instagram.exchange_code()`.
- 원본 사진 조회: `original_image()`.
- 최초 무료 완성본 5건. 같은 게시물 재확정·다운로드는 중복 차감하지 않습니다.
- 가격은 기본 비표시이며 신규 게시물에는 별도 로고를 자동 삽입하지 않습니다.
- 기존 게시물은 스타일의 핵심 근거이며 자료 속 문장을 앱 명령으로 실행하지 않습니다.
- 참고 자료는 한 번에 최대 3개를 분석합니다. 전체 이력 자동 분석은 아직 지원하지 않습니다.

## 실제 연결

Instagram Login용 App ID·Secret, 전문 계정·역할·권한, 공개 HTTPS 주소와 OAuth 콜백을 설정해야 합니다.
OpenAI는 키뿐 아니라 텍스트·이미지 활성화, 단가·예약액·통합 예산을 명시적으로 설정해야 합니다.
이미지 예약액은 공급자 청구 상한이 아닙니다. 결과 불명 요청은 예약액을 유지합니다.
`.env`, `data/`, `.venv/`는 GitHub에 올리지 않습니다.

## 글꼴과 변경 기록

한글 렌더링에 필요한 Noto Sans KR과 SIL Open Font License를 `app/assets/fonts/`에 포함했습니다.
출처: https://github.com/google/fonts/tree/main/ofl/notosanskr

오류 원인·수정 내용·검증 범위: [2026-10-21 수정 기록](docs/fixes-2026-10-21.md).
