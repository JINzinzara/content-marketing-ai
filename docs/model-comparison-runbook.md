# 스푸키쿠키 모델 비교 — 직접 실행 안내

**구현·모의 검증 완료. 유료 API는 아직 호출하지 않았고 Notion은 수정하지 않았다.**
이 안내의 `--execute` 명령을 사용자가 실행하면 비용이 발생한다. 기본 `run`, `prepare`, `report`는 유료 API를 호출하지 않는다.

## 비교 범위와 초기 설정

최종 비교는 원본 1장과 합성 변형 4장을 각각 한 번씩 처리한다. 문구 `gpt-5.4-mini`·`gpt-5.4` 각 5개, 이미지 `gpt-image-1-mini`·`gpt-image-2` 각 5개로 **생성 총 20건**이다. 문구에는 같은 요청의 입력 토큰 사전 계산 HTTP 요청이 별도로 있다.

- 입력: `comparison/inputs/`에 이번 세트 5장을 복사해 두었다. 사진은 Git에서 제외된다. 다른 컴퓨터에서는 원본 자료 묶음의 `inputs/` 파일 5개를 이 위치에 복사한다.
- 설정: `comparison/spooky-cookie.json`. 문구는 추론 `none`·출력 최대 2048토큰, 이미지는 `low`·1024×1024를 초기값으로 둔다. 기존 앱 설정에 맞춘 **탐색 시작점**이며 품질 최적값으로 검증한 값은 아니다.
- 모델마다 설정을 다르게 탐색할 수 있다. 지원 크기는 1024×1024, 1024×1536, 1536×1024이고 품질은 low/medium/high다. Mini의 입력 fidelity는 low로, Image 2는 해당 필드를 생략한다.
- 모든 최종 미리보기는 앱과 같은 비율 변환으로 **1080×1350, 세로 4:5**가 된다. 중앙 크롭에서 상품이 잘리면 H04 실패다. 생성 원본도 별도 보관한다.
- 프롬프트는 앱의 문구 지시·출력 스키마와 이미지 편집 지시를 재사용한다. 이번 상품의 본문·태그 요건과 정확한 메인 쿠키 위치를 공통으로 추가한다. 스타일 설명은 고정 입력이며 스타일 추출 모델의 비교가 아니다.
- 탐색은 별도 폴더에서 반복할 수 있다. 최종 비교는 첫 시도만 보관하며 같은 슬롯의 자동 재호출·좋은 결과 골라 바꾸기는 없다.

## 1. 환경과 오프라인 검증

기존 비공개 `.env`와 `data/`를 그대로 사용한다. 새 패키지는 추가하지 않았다.

```bash
cd /Users/limhyunjin/Desktop/content-marketing-ai
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
```

`.env`에서 `OPENAI_API_KEY`, `OPENAI_ENABLED=true`, `OPENAI_IMAGE_ENABLED=true`, 양수의 `OPENAI_BUDGET_USD`가 필요하다. **키를 터미널 출력이나 Git에 넣지 않는다.** 다른 앱 단가는 기존 설정을 유지하고, 비교 모델의 단가는 JSON에서 가져온다.

예산은 앱의 기존 `data/app.sqlite3` 원장을 공유한다. `OPENAI_BUDGET_USD`는 이번 명령만의 추가 허용액이 아니라 기존 앱·탐색·최종 비교를 포함한 **누적 한도**다. 이미지 초기 예약금은 모델별 JSON의 `reserve_usd=0.10`이다. 예약금은 청구 상한이나 실제 비용이 아니며, 실제 사용량이 초과하면 후속 생성을 멈춘다. 동시 실행 중인 앱 요청도 같은 예산 검사를 받는다.

단가는 2026-10-28에 확인한 표준 요금 스냅샷이다. 실행 전에 [공식 요금](https://developers.openai.com/api/docs/pricing)과 모델 접근 권한을 확인하고 필요하면 JSON을 수정한다. 단가·모델·설정은 `prepare` 전에 결정한다. 최종 비교 도중에는 바꾸지 않는다.

## 2. 탐색용 유료 시험 — 원본 1장, 모델별 1건

이 단계는 선택 사항이다. 진행하면 최종 20건과 별도로 최대 4건의 생성 비용이 발생한다.

```bash
.venv/bin/python model_compare.py prepare --phase explore --case original --out comparison/runs/explore-01
.venv/bin/python model_compare.py run comparison/runs/explore-01
```

두 번째 명령은 호출하지 않고 남은 계획만 확인한다. 다음 명령부터 유료다.

```bash
.venv/bin/python model_compare.py run comparison/runs/explore-01 --execute --limit 1
.venv/bin/python model_compare.py report comparison/runs/explore-01
open comparison/runs/explore-01/review.html
```

첫 슬롯의 모델·실행 순서는 `prepare`에서 무작위로 고정된다. 결과·응답·비용을 확인한 뒤 같은 폴더의 나머지 3건을 진행한다.

```bash
.venv/bin/python model_compare.py run comparison/runs/explore-01 --execute
open comparison/runs/explore-01/review.html
```

한 모델만 탐색하려면 새 폴더에 다음처럼 준비한다. 이 명령 자체는 과금하지 않는다.

```bash
.venv/bin/python model_compare.py prepare --phase explore --model gpt-image-2 --case original --out comparison/runs/explore-image2-02
```

품질·크기·추론·출력 한도·예약금 등을 JSON에서 바꾼 뒤 **새 폴더로 prepare**한다. 기존 실행 폴더의 명세·요청·입력을 고치지 않는다. 입력이 한 장인 탐색 결과는 5개 묶음의 일관성 판정 대상이 아니다.

## 3. 최종 비교 — 설정 고정 후 총 20건

탐색 결과를 보고 JSON의 모델별 설정을 결정한 뒤 진행한다. 탐색 결과를 최종 5개 중 하나로 가져오지 않는다.

```bash
.venv/bin/python model_compare.py prepare --phase final --out comparison/runs/final-01
.venv/bin/python model_compare.py run comparison/runs/final-01
.venv/bin/python model_compare.py run comparison/runs/final-01 --execute --limit 1
```

한 건의 파일·비용·오류를 확인한 다음, 동일 폴더에서 나머지를 실행한다. 첫 한 건도 최종 비교의 일부이므로 다시 만들지 않는다.

```bash
.venv/bin/python model_compare.py run comparison/runs/final-01 --execute
open comparison/runs/final-01/review.html
```

`prepare`는 입력·요청·단가·모델 설정·평가 기준·코드 해시와 무작위 실행/검토 순서를 고정한다. 이미 있는 폴더를 덮어쓰지 않는다. 코드나 고정 입력이 변경되면 추가 유료 실행을 거절한다. 동일 폴더의 동시 실행도 막는다.

중단 후 같은 `run --execute`를 다시 실행하면 아직 기록이 없는 슬롯만 처리한다. 완료·실패·거절·미전송 슬롯을 자동 재시도하지 않는다. 요청 결과/비용이 불명하거나 예약금 초과가 확인되면 확인 전에는 계속 진행하지 않는다. 실패를 없애려고 원장이나 결과 파일을 지우지 않는다.

## 4. 사람 검증과 집계

모델명·비용이 가려진 `review.html`에서 같은 입력 순서의 결과 5개를 나란히 본다. 문구와 이미지 비교를 분리한다. 이미지 비교에는 생성 문구를 붙이지 않고, 문구 비교에는 동일한 입력 사진을 제시한다.

아래 CSV를 Numbers·Excel 또는 텍스트 편집기로 열어 **파일명·ID·행을 유지한 UTF-8 CSV**로 저장한다. 앱이나 AI가 사람 판정을 대신 입력하지 않는다.

- `reviews.csv`: 항목별 `grade`, `reason`, `evaluator` 입력. 문구 T01~T09, 이미지 H01~H04. 기준 전문은 실행 폴더의 `protocol.md`에 고정돼 있다.
- `bundles.csv`: 5개 묶음의 이미지 분위기 또는 문구 말투 일관성을 별도로 판정. `grade`, `reason`, `evaluator` 입력.
- `preferences.csv`: 같은 영역의 A/B 상대 비교. `preference`는 `A 우세` / `비슷함` / `B 우세` 중 하나와 이유·평가자 입력. 단일 모델 탐색에는 행이 없다.

등급은 `충족` / `부분 충족` / `미충족`. 아직 검토하지 않은 등급 칸은 빈칸으로 둔다. **부분 충족은 불합격**이다. 개별 합격과 묶음 일관성은 따로 표시하며, 개별 합격 5개를 묶음 합격으로 자동 바꾸지 않는다. 탐색 결과가 5개 미만이면 묶음 판정 칸을 비워 둔다.

평가를 저장한 뒤 다시 집계한다.

```bash
.venv/bin/python model_compare.py report comparison/runs/final-01
open comparison/runs/final-01/review.html
open comparison/runs/final-01/summary.csv
```

`summary.csv`에는 모델명이 드러난다. 사람 검토를 끝낸 뒤 열면 된다. 완료 수·개별 합격 수·미평가·실패·미전송·묶음 일관성과 비용/시간을 분리해 확인한다. 동일 입력 반복 실험이나 실제 별도 촬영의 재현성으로 해석하지 않는다.

## 비용·시간 해석

- 시도별 원시 사용량, 예약금, 비용 추정, 오류, API 처리 시간은 `samples/<ID>/attempt.json`에 남는다. 응답 전문은 `response.json`이다. 문구 토큰 사전 계산 응답·소요 시간도 별도 보관한다.
- 문구 비용: `service_tier=default`로 요청하고 입력·캐시·출력 토큰과 표준 단가로 추정한다. 응답의 처리 등급이 다르면 비용 미확정으로 남긴다. GPT-5.4의 272K 초과 입력 할증도 적용한다.
- 이미지 비용: 응답의 텍스트 입력·이미지 입력·출력 토큰에 **캐시 할인 미적용 단가**를 적용한 상한 추정치다. 캐시 구분을 알 수 없으므로 정확한 청구액으로 부르지 않는다. 제공사 청구 내역과 대조한 금액은 `settle`로 기록한다.
- 사용량 누락·응답 불명 비용은 0으로 채우지 않는다. 원장에 예약을 유지하고 추가 생성을 멈춘다. 실패에 청구된 비용도 합계에 포함한다.
- `cost_per_pass_usd`는 해당 실행 폴더·모델의 전체 비용 / 개별 합격 수다. 모든 슬롯이 생성 전송됐고, 완성된 출력의 사람 검토가 끝났고, 비용 불명이 없을 때만 계산한다. 합격 0개면 빈칸이다. 탐색 폴더 비용은 최종 폴더 비용에 자동 합산하지 않으므로 전체 개발 지출을 보고할 때 각 폴더 비용을 별도로 함께 제시한다.
- `api_seconds`는 생성 요청부터 응답 수신까지이며 성공·실패 모두 남긴다. 집계에는 전체 처리 시간과 합격 출력의 처리 시간을 나눠 표시한다. `cli_artifact_ready_seconds`는 사전 계산·저장·렌더링을 포함한 CLI 진단값이다.
- **제작 버튼→이미지·문구가 함께 준비된 실제 브라우저 미리보기 시간은 미측정**이다. CLI 수치를 그 값으로 보고하지 않는다. 이 비교는 모델별 문구/이미지 단독 평가이며, 조립·검증하지 않은 조합의 합격 완성본 수를 만들지 않는다.

## 결과 불명·예약금 초과가 발생했을 때

먼저 해당 `samples/<ID>/response.json`, `attempt.json`과 제공사의 사용량·청구 기록을 확인한다. 확인되지 않은 비용을 임의로 0으로 정산하지 않는다.

아래는 **실제 청구액이 0.0123달러로 확인된 경우의 예시**다. `<실제_ID>`를 해당 폴더 이름으로 바꾸고 금액·근거를 실제 확인한 값으로 입력한다. API를 재호출하지 않는다.

```bash
.venv/bin/python model_compare.py settle comparison/runs/final-01 --sample '<실제_ID>' --cost-usd 0.0123 --note '제공사 청구 기록과 요청 ID를 대조해 확인'
.venv/bin/python model_compare.py run comparison/runs/final-01 --execute
```

정산은 공유 원장의 비용도 갱신한다. 예약금 초과를 사용자가 청구 내역으로 확인한 경우에만 원장의 예약 추정치를 확인 금액까지 조정해 차단을 해제한다. 최초 예약 추정치와 정산 근거는 실행 기록에 남긴다. 전체 누적 예산 검사는 계속 적용된다.

결과 불명이었던 요청을 정산해도 해당 슬롯을 다시 생성하지 않는다. 출력이 이미 정상 보관된 사용량 누락 건은 출력을 유지한다. 전송 전에 예산 부족·사전 계산 오류로 `not_sent`가 된 슬롯도 이번 실행에서는 다시 보내지 않으며, 모델의 품질 실패와 구분해 표시한다. 그런 실행은 완전한 5개 결과 비교가 아니다. 원인을 해결한 뒤 새 최종 실행을 준비할지 판단한다.

## 보관 파일과 한계

`manifest.json`은 모델명과 설정을 포함한 관리자용 원본이다. 사람 검증 중에는 `review.html`과 판정 CSV를 사용한다. `generated-original.png`와 `preview.jpg`를 구분하고, 원시 응답·실패·판정을 함께 보관한다. 입력 사진·요청의 base64 사진·상품 정보가 포함되므로 `comparison/inputs/`·`comparison/runs/`는 Git에서 제외한다.

API 키·권한·모델 이용 가능성·실제 사용량 응답 형식·라이브 지연 시간은 모의 시험으로 확인할 수 없다. 유료 시험에서 확인해야 한다. 이미지 예약금은 강제 청구 상한이 아니다. 제공사/사용자 코드 외부의 재시도까지 제어하지는 못한다.

확인일 2026-10-28. [GPT Image 1 Mini 종료 일정](https://developers.openai.com/api/docs/deprecations#2026-06-02-gpt-image-model-deprecations)은 **2026-12-01**이다. 후보를 자동 대체하지 않는다. [Images 편집 요청 규격](https://developers.openai.com/api/reference/resources/images/methods/edit), [GPT-5.4 Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini), [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4), [GPT Image 2](https://developers.openai.com/api/docs/models/gpt-image-2)를 근거로 요청·단가를 구성했다.
