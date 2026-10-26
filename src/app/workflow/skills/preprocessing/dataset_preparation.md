---
name: dataset_preparation
description: 여러 데이터셋을 결합하거나 분석용 표본을 만드는 workflow step을 설계할 때 사용하는 skill. merge_data와 sample_data의 선택, 실행 순서, 결합 키 확인과 다음 step으로 전달할 데이터 흐름을 결정한다.
---

# Dataset Preparation

## capabilities

- 공통 키를 사용한 데이터셋 결합
- 대용량 데이터의 분석용 표본 추출
- 다음 Skill에 전달할 단일 준비 데이터 구성

## limitations

- 결측치 대체, 이상치 제거와 feature 변환을 수행하지 않음
- 서로 연결된 X/Y 데이터를 독립적으로 표본 추출하면 정합성이 깨질 수 있음

## when_to_use

- 둘 이상의 입력 데이터를 하나로 결합해야 하는 경우
- 전체 데이터 대신 대표 표본으로 분석할 경우

## not_for

- 데이터 품질 진단, 정제 또는 모델 학습

## typical_previous_skill

- `data_load`

## typical_next_skills

- `data_quality_check`
- `data_cleaning_pipeline`

## Tool list

| Tool | 실행 방식 | 실행 조건 | condition_tool | 역할 |
|---|---|---|---|---|
| `merge_data` | 조건부 실행 | 둘 이상의 데이터셋을 공통 키로 결합해야 할 때 | 없음 | 단일 분석 데이터 생성 |
| `sample_data` | 조건부 실행 | 표본 분석이 필요하거나 데이터가 큰 경우 | 없음 | 분석 비용을 줄일 표본 생성 |

