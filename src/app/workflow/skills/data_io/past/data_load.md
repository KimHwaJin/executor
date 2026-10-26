---
name: data_load
description: 사용자 데이터 선택 조건으로 원천 데이터를 추출하고 데이터 유형별 wide 분석 형식으로 변환하는 workflow step을 설계할 때 사용하는 skill.
---

# Data Load

## capabilities

- 사용자 선택 조건을 `extract_data`에 전달
- NCE 데이터를 `transform_nce`로 wide 형식 변환
- WT 데이터를 `transform_wt`로 wide 형식 변환
- `DATA_MOCK=true`에서는 준비된 wide Parquet을 `data_load`로 직접 로드
- 변환한 데이터를 다음 Skill에 전달

## limitations

- 현재 로컬 환경에서는 mock wide Parquet을 사용
- 결합, 표본 추출, 품질 진단과 정제를 수행하지 않음

## when_to_use

- 분석할 NCE 또는 WT 데이터가 선택된 경우

## not_for

- 이미 메모리에 준비된 DataFrame만 사용하는 경우

## typical_previous_skill

- 없음

## typical_next_skills

- `dataset_preparation`
- `data_quality_check`

## Tool list

| Tool | 실행 방식 | 실행 조건 | condition_tool | 역할 |
|---|---|---|---|---|
| `extract_data` | 항상 실행 | 데이터 선택 조건이 확정된 경우 | 없음 | 원천 데이터 추출 |
| `transform_nce` | 항상 실행 | `data_type=nce` | 없음 | NCE wide 데이터 생성 |
| `transform_wt` | 항상 실행 | WT 계열 data type | 없음 | WT wide 데이터 생성 |
| `data_load` | 항상 실행 | `DATA_MOCK=true` | 없음 | wide mock Parquet 직접 로드 |
