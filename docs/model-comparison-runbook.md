# 스푸키쿠키 모델 비교 — 직접 실행 안내

**리뷰의 5개 수정·회귀 검증 완료. 이 작업에서 유료 API를 호출하거나 Notion을 수정하지 않았다.**

초기 AI 응답의 최신 문구 덮어쓰기, 동등한 날짜의 오판정, 기존 앱 비용 초과 정산, 탐색 범위의 과장 표기, 정산 후 비용 근거 표시를 수정했다.
수정 전 `prepare`한 폴더는 보관한다. 코드 해시가 달라 유료 실행을 이어갈 수 없으므로 아래의 새 폴더로 준비한다. 기존 결과의 오프라인 `report`와 정산은 가능하다.
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

기존 비공개 `.env`와 `data/`를 그대로 사용한다. 새 패키지는 추가하지 않았다. 비교 실행기는 터미널에서 독립 실행한다. 기존 웹앱에는 서버 재시작 후 수정 내용이 적용된다.

```bash
cd /Users/limhyunjin/Desktop/content-marketing-ai
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
.venv/bin/python model_compare.py budget
```

예상 결과: 모든 테스트 통과, `No broken requirements found`, 설정한 누적 예산과 기존 사용량·미확정 예약의 합계가 표시된다. `budget`은 API를 호출하거나 원장을 초기화하지 않는다. 예약 초과 요청이 나오면 아래의 기존 앱 정산 절차로 청구 내역을 확인한다.

`.env`에서 `OPENAI_API_KEY`, `OPENAI_ENABLED=true`, `OPENAI_IMAGE_ENABLED=true`, 양수의 `OPENAI_BUDGET_USD`가 필요하다. **키를 터미널 출력이나 Git에 넣지 않는다.** 다른 앱 단가는 기존 설정을 유지하고, 비교 모델의 단가는 JSON에서 가져온다.

예산은 앱의 기존 `data/app.sqlite3` 원장을 공유한다. `OPENAI_BUDGET_USD`는 이번 명령만의 추가 허용액이 아니라 기존 앱·탐색·최종 비교를 포함한 **누적 한도**다. 이미지 초기 예약금은 모델별 JSON의 `reserve_usd=0.10`이다. 예약금은 청구 상한이나 실제 비용이 아니며, 실제 사용량이 초과하면 후속 생성을 멈춘다. 동시 실행 중인 앱 요청도 같은 예산 검사를 받는다.

단가는 2026-10-28에 확인한 표준 요금 스냅샷이다. 실행 전에 [공식 요금](https://developers.openai.com/api/docs/pricing)과 모델 접근 권한을 확인하고 필요하면 JSON을 수정한다. 단가·모델·설정은 `prepare` 전에 결정한다. 최종 비교 도중에는 바꾸지 않는다.

## 2. 탐색용 유료 시험 — 원본 1장, 모델별 1건

이 단계는 선택 사항이다. 진행하면 최종 20건과 별도로 최대 4건의 생성 비용이 발생한다.

```bash
.venv/bin/python model_compare.py prepare --phase explore --case original --out comparison/runs/explore-fixed-01
.venv/bin/python model_compare.py run comparison/runs/explore-fixed-01
```

예상 결과: `4개 계획 준비 완료`와 `유료 호출 없음. 남은 계획 4개`가 표시된다. 이때 `results.json`의 범위는 입력 1장(원본 1장·합성 0장), 모델 4개·모델별 계획 1건, 전체 계획 4건·완료 0건이다.

두 번째 명령은 호출하지 않고 남은 계획만 확인한다. 다음 명령부터 유료다.

```bash
.venv/bin/python model_compare.py run comparison/runs/explore-fixed-01 --execute --limit 1
.venv/bin/python model_compare.py report comparison/runs/explore-fixed-01
open comparison/runs/explore-fixed-01/review.html
```

정상 응답 시 예상 결과: `<영역> original <모델>: completed` 한 줄이 나오고 완료 기록이 한 건 생긴다. 문구면 `draft.json`·`caption.txt`, 이미지면 `generated-original.png`·`preview.jpg`가 저장된다. 사람 판정 전에는 합격으로 집계하지 않는다.

첫 슬롯의 모델·실행 순서는 `prepare`에서 무작위로 고정된다. 결과·응답·비용을 확인한 뒤 같은 폴더의 나머지 3건을 진행한다.

```bash
.venv/bin/python model_compare.py run comparison/runs/explore-fixed-01 --execute
open comparison/runs/explore-fixed-01/review.html
```

모두 정상 응답하면 탐색 완료는 총 4건(문구 2건·이미지 2건)이다. 입력 한 장의 결과이므로 묶음 일관성은 미평가로 둔다.

한 모델만 탐색하려면 새 폴더에 다음처럼 준비한다. 이 명령 자체는 과금하지 않는다.

```bash
.venv/bin/python model_compare.py prepare --phase explore --model gpt-image-2 --case original --out comparison/runs/explore-image2-fixed-02
```

품질·크기·추론·출력 한도·예약금 등을 JSON에서 바꾼 뒤 **새 폴더로 prepare**한다. 기존 실행 폴더의 명세·요청·입력을 고치지 않는다. 입력이 한 장인 탐색 결과는 5개 묶음의 일관성 판정 대상이 아니다.

## 3. 최종 비교 — 설정 고정 후 총 20건

탐색 결과를 보고 JSON의 모델별 설정을 결정한 뒤 진행한다. 탐색 결과를 최종 5개 중 하나로 가져오지 않는다.

```bash
.venv/bin/python model_compare.py prepare --phase final --out comparison/runs/final-fixed-01
.venv/bin/python model_compare.py run comparison/runs/final-fixed-01
.venv/bin/python model_compare.py run comparison/runs/final-fixed-01 --execute --limit 1
```

예상 결과: 준비 시 `20개 계획 준비 완료`, 기본 `run`에서는 `남은 계획 20개`가 표시된다. 첫 유료 명령은 이 중 한 건만 실행한다.

한 건의 파일·비용·오류를 확인한 다음, 동일 폴더에서 나머지를 실행한다. 첫 한 건도 최종 비교의 일부이므로 다시 만들지 않는다.

```bash
.venv/bin/python model_compare.py run comparison/runs/final-fixed-01 --execute
open comparison/runs/final-fixed-01/review.html
```

모두 정상 응답하면 최종 완료는 총 20건이며 모델별 5건이다. 문구 파일 10세트와 이미지 생성 원본 10장·최종 미리보기 10장이 생긴다. 각 이미지 미리보기는 1080×1350이다. 아직 사람 판정을 입력하지 않았다면 모델별 `passed=0`, `unreviewed=5`, 묶음 일관성 `미평가`가 정상이다. 기술적 완료가 품질 합격을 의미하지 않는다.

`prepare`는 입력·요청·단가·모델 설정·평가 기준·코드 해시와 무작위 실행/검토 순서를 고정한다. 이미 있는 폴더를 덮어쓰지 않는다. 코드나 고정 입력이 변경되면 추가 유료 실행을 거절한다. 동일 폴더의 동시 실행도 막는다.

중단 후 같은 `run --execute`를 다시 실행하면 아직 기록이 없는 슬롯만 처리한다. 완료·실패·거절·미전송 슬롯을 자동 재시도하지 않는다. 요청 결과/비용이 불명하거나 예약금 초과가 확인되면 확인 전에는 계속 진행하지 않는다. 실패를 없애려고 원장이나 결과 파일을 지우지 않는다.

## 4. 사람 검증과 집계

모델명·비용이 가려진 `review.html`에서 같은 입력 순서의 결과 5개를 나란히 본다. 문구와 이미지 비교를 분리한다. 이미지 비교에는 생성 문구를 붙이지 않고, 문구 비교에는 동일한 입력 사진을 제시한다.

아래 CSV를 Numbers·Excel 또는 텍스트 편집기로 열어 **파일명·ID·행을 유지한 UTF-8 CSV**로 저장한다. 앱이나 AI가 사람 판정을 대신 입력하지 않는다.

- `reviews.csv`: **결과당 한 행, 최종 비교는 총 20행**이다. 문구 행의 `T01`~`T09`, 이미지 행의 `H01`~`H04`에 등급을 적는다. 다른 영역의 칸은 비워 둔다. `reason`은 결과당 한 번만 입력한다. 기준 전문은 실행 폴더의 `protocol.md`에 고정돼 있다.
- `bundles.csv`: 5개 묶음의 이미지 분위기 또는 문구 말투 일관성을 별도로 판정. `grade`, `reason` 입력. 최종 비교는 4행이다.
- `preferences.csv`: 같은 영역의 A/B 상대 비교. `preference`는 `A 우세` / `비슷함` / `B 우세` 중 하나와 `reason` 입력. 최종 비교는 2행이며 단일 모델 탐색에는 행이 없다.
- 평가자는 공통 `evaluator.txt`에 기본값 `본인`으로 지정돼 있다. 그대로 사용해도 되고 이름을 바꾸려면 이 파일만 한 번 수정한다. CSV마다 반복 입력하지 않는다. 집계 JSON에는 평가자를 함께 보관한다.

`충족` 판정은 `reason`을 생략할 수 있다. 개별 결과에 `부분 충족`·`미충족`이 하나라도 있으면 결과당 한 번 이유를 적는다. 자연어 한 문장으로 충분하며 `T03: 판매 종료일이 11월 5일로 잘못 표기됨`처럼 해당 기준을 표시한다. 여러 문제가 있으면 같은 칸에 이어 쓴다. 묶음 판정도 충족은 이유 생략 가능, 부분 충족·미충족은 이유 필수다. A/B 선호 판정은 `A 우세`·`비슷함`·`B 우세` 모두 이유가 필요하다. 빈 이유를 AI가 대신 작성하지 않는다.

20행은 결과의 수다. 문구 10개×9기준 + 이미지 10개×4기준으로 **세부 판정은 여전히 130개**다. 이번 수정은 기준을 줄이지 않고 이유·평가자의 반복 입력을 줄인다. 전체 합격은 세부 등급으로 계산하므로 `reviews.csv`에 별도 종합 `grade` 칸은 없다.

등급은 `충족` / `부분 충족` / `미충족`. 아직 검토하지 않은 등급 칸은 빈칸으로 둔다. **부분 충족은 불합격**이다. 개별 합격과 묶음 일관성은 따로 표시하며, 개별 합격 5개를 묶음 합격으로 자동 바꾸지 않는다. 탐색 결과가 5개 미만이면 묶음 판정 칸을 비워 둔다.

평가를 저장한 뒤 다시 집계한다.

```bash
.venv/bin/python model_compare.py report comparison/runs/final-fixed-01
open comparison/runs/final-fixed-01/review.html
open comparison/runs/final-fixed-01/summary.csv
```

예상 결과: `사람 판정 집계 완료`가 나오고 CSV·JSON·검토 화면이 갱신된다. 개별 필수 항목을 모두 `충족`으로 판정한 완성 출력만 합격 수에 더한다. 하나라도 `부분 충족` 또는 `미충족`이면 불합격이고, 빈칸은 미평가다.

종전의 기준당 한 행 형식은 위 `report` 명령이 자동 변환한다. 변환 전 CSV는 같은 폴더의 `reviews.legacy.csv`, `bundles.legacy.csv`, `preferences.legacy.csv`에 보관하며 기존 판정과 이유를 유지한다. 생성 원본·요청·비용·시간·고정 명세는 수정하지 않고 API도 호출하지 않는다. 기존 `protocol.md`는 당시 기준의 원문으로 유지하며 입력 형식은 이 안내를 따른다. 이미 변환된 파일은 다시 변환하지 않는다. 기존 기록에 평가자가 여러 명이면 자동 통합하지 않고 중단한다.

`summary.csv`에는 모델명이 드러난다. 사람 검토를 끝낸 뒤 열면 된다. 완료 수·개별 합격 수·미평가·실패·미전송·묶음 일관성과 비용/시간을 분리해 확인한다. 동일 입력 반복 실험이나 실제 별도 촬영의 재현성으로 해석하지 않는다.

## 비용·시간 해석

- 시도별 원시 사용량, 예약금, 비용 추정, 오류, API 처리 시간은 `samples/<ID>/attempt.json`에 남는다. 응답 전문은 `response.json`이다. 문구 토큰 사전 계산 응답·소요 시간도 별도 보관한다.
- 문구 비용: `service_tier=default`로 요청하고 입력·캐시·출력 토큰과 표준 단가로 추정한다. 응답의 처리 등급이 다르면 비용 미확정으로 남긴다. GPT-5.4의 272K 초과 입력 할증도 적용한다.
- 이미지 비용: 응답의 텍스트 입력·이미지 입력·출력 토큰에 **캐시 할인 미적용 단가**를 적용한 상한 추정치다. 캐시 구분을 알 수 없으므로 정확한 청구액으로 부르지 않는다. 제공사 청구 내역과 대조한 금액은 `settle`로 기록한다.
- `summary.csv`의 `cost_basis`는 실제 기록을 따른다. 문구 추정은 `usage_standard_rates`, 이미지 상한 추정은 `usage_uncached_upper_estimate`, 알려진 비용이 모두 청구 확인 정산이면 `manual_billing_reconciliation`, 여러 근거가 섞이면 `mixed`다. 미확정만 있으면 `unknown`, 미실행이면 `not_measured`다. 금액을 정산하면 표시도 함께 갱신된다.
- 사용량 누락·응답 불명 비용은 0으로 채우지 않는다. 원장에 예약을 유지하고 추가 생성을 멈춘다. 실패에 청구된 비용도 합계에 포함한다.
- `cost_per_pass_usd`는 해당 실행 폴더·모델의 전체 비용 / 개별 합격 수다. 모든 슬롯이 생성 전송됐고, 완성된 출력의 사람 검토가 끝났고, 비용 불명이 없을 때만 계산한다. 합격 0개면 빈칸이다. 탐색 폴더 비용은 최종 폴더 비용에 자동 합산하지 않으므로 전체 개발 지출을 보고할 때 각 폴더 비용을 별도로 함께 제시한다.
- `api_seconds`는 생성 요청부터 응답 수신까지이며 성공·실패 모두 남긴다. 집계에는 전체 처리 시간과 합격 출력의 처리 시간을 나눠 표시한다. `cli_artifact_ready_seconds`는 사전 계산·저장·렌더링을 포함한 CLI 진단값이다.
- **제작 버튼→이미지·문구가 함께 준비된 실제 브라우저 미리보기 시간은 미측정**이다. CLI 수치를 그 값으로 보고하지 않는다. 이 비교는 모델별 문구/이미지 단독 평가이며, 조립·검증하지 않은 조합의 합격 완성본 수를 만들지 않는다.

## 기존 앱 요청 때문에 예산이 차단됐을 때

```bash
.venv/bin/python model_compare.py budget
```

예상 결과: 확인할 요청이 없다면 `비용 미확정·예약 초과 요청 없음`이 나온다. 문제가 있는 경우 요청 ID·목적·모델·상태·예약금·비용이 표시된다. `compare:`로 시작하는 요청은 다음 절의 실행 폴더별 `settle`을 사용한다.

기존 앱의 요청은 앱 서버를 중지하고 제공사 청구 기록을 확인한 다음 정산한다. 서버를 켜 둔 터미널에서 `Ctrl+C`로 중지할 수 있다. 아래 금액은 **실제 청구액이 $0.12로 확인됐을 때의 예시**이며 ID·금액·근거를 확인한 값으로 바꾼다. 확인되지 않은 비용을 임의로 0으로 입력하지 않는다.

```bash
.venv/bin/python model_compare.py settle-call --call-id '<budget에 표시된 앱 요청 ID>' --cost-usd 0.12 --note '제공사 청구 기록과 요청 ID를 대조해 확인'
.venv/bin/python model_compare.py budget
```

예상 결과: `기존 앱 요청 정산 완료`가 나오고 해당 예약 초과 항목이 목록에서 사라진다. 다른 미확정·초과 항목은 남는다. 변경 전 예약금·비용과 정산 후 비용·근거는 `ai_cost_reconciliations`에 보관된다. 누적 예산 검사와 기존 작업 상태는 유지되며 모델을 다시 호출하지 않는다. 모든 초과 건을 정산하고 누적 잔여 예산이 충분해야 새 생성이 가능하다.

## 결과 불명·예약금 초과가 발생했을 때

먼저 해당 `samples/<ID>/response.json`, `attempt.json`과 제공사의 사용량·청구 기록을 확인한다. 확인되지 않은 비용을 임의로 0으로 정산하지 않는다.

아래는 **실제 청구액이 0.0123달러로 확인된 경우의 예시**다. `<실제_ID>`를 해당 폴더 이름으로 바꾸고 금액·근거를 실제 확인한 값으로 입력한다. API를 재호출하지 않는다.

```bash
.venv/bin/python model_compare.py settle comparison/runs/final-fixed-01 --sample '<실제_ID>' --cost-usd 0.0123 --note '제공사 청구 기록과 요청 ID를 대조해 확인'
.venv/bin/python model_compare.py run comparison/runs/final-fixed-01 --execute
```

정산은 공유 원장의 비용도 갱신하고 `ai_cost_reconciliations`에 변경 전후 금액·근거를 남긴다. 예약금 초과를 사용자가 청구 내역으로 확인한 경우에만 원장의 예약 추정치를 확인 금액까지 조정해 차단을 해제한다. 최초 예약 추정치와 정산 근거는 실행 기록에 남긴다. 전체 누적 예산 검사는 계속 적용된다.

결과 불명이었던 요청을 정산해도 해당 슬롯을 다시 생성하지 않는다. 출력이 이미 정상 보관된 사용량 누락 건은 출력을 유지한다. 전송 전에 예산 부족·사전 계산 오류로 `not_sent`가 된 슬롯도 이번 실행에서는 다시 보내지 않으며, 모델의 품질 실패와 구분해 표시한다. 그런 실행은 완전한 5개 결과 비교가 아니다. 원인을 해결한 뒤 새 최종 실행을 준비할지 판단한다.

## 보관 파일과 한계

`manifest.json`은 모델명과 설정을 포함한 관리자용 원본이다. 사람 검증 중에는 `review.html`과 판정 CSV를 사용한다. `generated-original.png`와 `preview.jpg`를 구분하고, 원시 응답·실패·판정을 함께 보관한다. 입력 사진·요청의 base64 사진·상품 정보가 포함되므로 `comparison/inputs/`·`comparison/runs/`는 Git에서 제외한다.

API 키·권한·모델 이용 가능성·실제 사용량 응답 형식·라이브 지연 시간은 모의 시험으로 확인할 수 없다. 유료 시험에서 확인해야 한다. 이미지 예약금은 강제 청구 상한이 아니다. 제공사/사용자 코드 외부의 재시도까지 제어하지는 못한다.

확인일 2026-10-28. [GPT Image 1 Mini 종료 일정](https://developers.openai.com/api/docs/deprecations#2026-06-02-gpt-image-model-deprecations)은 **2026-12-01**이다. 후보를 자동 대체하지 않는다. [Images 편집 요청 규격](https://developers.openai.com/api/reference/resources/images/methods/edit), [GPT-5.4 Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini), [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4), [GPT Image 2](https://developers.openai.com/api/docs/models/gpt-image-2)를 근거로 요청·단가를 구성했다.
