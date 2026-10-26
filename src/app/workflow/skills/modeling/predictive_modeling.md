---
name: predictive_modeling
description: 지도학습 데이터를 분할하고 예측 모델을 학습·평가하는 workflow step을 설계할 때 사용하는 skill. split_dataset과 사용 가능한 학습 Tool의 선택, 실행 순서, target 전달과 모델 결과 수집을 결정한다.
---

# Predictive Modeling

## capabilities

- 학습·평가 데이터 분할
- 사용 가능한 분류 또는 회귀 학습 Tool 선택
- 모델과 평가 결과를 Workflow output으로 전달

## limitations

- Registry에 등록되지 않은 학습 Tool은 사용할 수 없음
- 지도학습 target과 모델 입력 전처리가 완료되어 있어야 함

## when_to_use

- 확정된 target을 예측하는 지도학습 모델을 만들고 평가할 때

## not_for

- target이 없는 비지도학습
- 데이터 진단이나 시각화만 필요한 경우

## typical_previous_skill

- `data_cleaning_pipeline`

## typical_next_skills

- 없음

## Tool list

| Tool | 실행 방식 | 실행 조건 | condition_tool | 역할 |
|---|---|---|---|---|
| `split_dataset` | 항상 실행 | 지도학습 데이터와 target이 준비된 경우 | 없음 | 학습·평가 데이터 생성 |
| `train_xgboost` | 항상 실행 | 분류 문제이고 xgboost 사용하려고 하는 경우 | 없음 | 분류 모델 학습과 평가 |

