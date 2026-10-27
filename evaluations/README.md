# 실제 작업 기록

평가자는 실제 수작업과 앱 사용을 수행한다. 이 폴더는 결과를 기록·집계할 뿐 API를 호출하지 않는다.
진행 순서와 품질 기준은 [평가 계획](../docs/evaluation-protocol.md)을 따른다.

## 시작

```bash
cp evaluations/records.example.csv evaluations/records.csv
# records.csv에 실제 수행한 결과를 작성한다.
.venv/bin/python -m evaluations > evaluations/comparison.csv
```

다른 실험 회차는 `.venv/bin/python -m evaluations evaluations/records-v2.csv`처럼 별도 파일로 집계한다.
기록표는 비어 있다. 실험 전에 결과나 사람 점수를 채우지 않는다.

## 한 행에 적는 내용

- `case_id`: 같은 자료·시작 상태·완료 조건을 사용하는 비교 과제 ID. 최초 설정과 반복 제작을 구분한다.
- `method`: manual(AI 없는 수작업) 또는 service(실제 앱).
- `evaluator`: 실제 수행한 사람의 식별자. 같은 사람끼리 비교한다. 본인/동료/강사 관계는 notes에 적는다.
- `version`: 수작업 도구 이름·버전 또는 실제 앱 Git 커밋. 앱 커밋은 `git rev-parse --short HEAD`로 확인한다.
- `seconds`: 입력 시작부터 검수 후 완성까지 실제 측정한 초. 최초 스타일 설정을 포함하는 과제인지 notes에 적는다.
- `text_edits`, `image_edits`: 첫 초안 이후 문구 수정과 이미지 수정/재생성 회차. 없으면 실제 0을 적는다.
- `satisfaction`: 평가 계획의 1~5 만족도.
- `facts_ok`, `style_ok`, `product_ok`, `usable`: yes / no / unreviewed. 상품 보존이 적용되지 않을 때만 product_ok에 na.
- `notes`: 입력·원본/최종 결과의 보관 위치, 문제 문장·수정 내용·판정 근거·수행 순서·실제 게시 여부.

완료하지 못한 작업은 중단 시점까지 걸린 시간과 usable=no, 사유를 남긴다.
아직 수행하지 않은 작업을 0초로 입력하지 않는다. 미평가 시간을 API 시간으로 채우지 않는다.

## 집계 결과

- `comparable`: 양쪽 결과의 사실·스타일·상품 보존·사용 가능 판정을 통과한 비교.
- `quality_not_met`: 품질 조건 미충족. 전체 결과에는 남기고 시간 절감률은 계산하지 않는다.
- `awaiting_review`: 사람 판정 대기.
- `missing_pair`: 같은 과제·평가자의 다른 방식 기록이 없음.

시간 절감률 = (수작업 초 - 서비스 초) / 수작업 초 × 100. 음수이면 서비스가 더 오래 걸렸다는 뜻이다.
기록 전체는 원본 CSV에 보존하고 결과 CSV에는 버전·시간·수정·만족도·비교 상태를 출력한다.
CSV 집계는 수행 조건이 실제로 같았는지 대신 확인해 주지 않으므로 과제 자료와 notes를 함께 검토한다.

개인 기록은 Git에서 제외한다. 공개 보고서는 평가자·사진·비밀값을 확인한 뒤 필요한 수치만 별도로 공유한다.
[과거 기술 점검 결과](reports/pilot-2026-10-26/README.md)는 그대로 보존한다.
