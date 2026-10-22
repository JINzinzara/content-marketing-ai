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

2026-10-22: 자동·모의 시험 82개 통과. 테스트는 임시 DB·사진과 모의 HTTP를 사용하며 실제 API나 공개 게시를 호출하지 않습니다.
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

이미지 제목의 이모지는 단색 Noto Emoji로 표시하며 별도의 OFL 라이선스를 포함했습니다.
출처: https://github.com/google/fonts/tree/main/ofl/notoemoji
본문·해시태그는 피드 미리보기에서 브라우저의 이모지 글꼴로 표시합니다. 글꼴과 복합 이모지 모양은 기기에 따라 다를 수 있습니다.

## 이미지 편집과 진행 표시

- 원본에서 선택한 상품 하나만 모델 입력에 남깁니다. 과거 사진은 스타일 분석에 사용하고 이미지 편집에는 분석한 색감·조명·배경·구도만 전달합니다.
- 상품 내부는 보존하며 안쪽 경계에만 2px Gaussian blur를 적용합니다. 이 값은 1024px 캔버스의 초기 보정값이며 잘못 그린 경계나 투명한 상품을 자동으로 분리하지 않습니다.
- 추가 상품·음식·음료의 생성을 금지하도록 요청합니다. 모델 결과를 완전히 보장할 수 없으므로 후보 선택 전에 상품 개수와 경계를 직접 확인해야 합니다. 기존 생성 후보는 다시 합성하지 않습니다.
- 스피너와 경과 시간을 표시합니다. 예상 시간은 본인 계정의 최근 완료 10건 중 같은 모델·품질·크기 기록의 중앙값입니다. 10건은 초기 관측 범위이며 예상 시간은 완료 보장이 아닙니다. 이력이 없으면 시간을 임의로 제시하지 않습니다.
- 진행 상태는 앱에만 2초 간격으로 조회하며 새 AI 호출을 만들지 않습니다. 완료 시 후보만 갱신해 작성 중인 본문과 보존 영역을 유지합니다.
- 피드 미리보기는 프로필·게시 이미지·본문·해시태그로 구성합니다. 실제 Instagram의 기기별 줄바꿈과 완전히 같지는 않습니다. 본문 수정은 API 호출 없이 즉시 반영하며 이미지 제목·배치는 저장 후 반영합니다.

오류 원인·수정 내용·검증 범위: [2026-10-21 수정 기록](docs/fixes-2026-10-21.md).
