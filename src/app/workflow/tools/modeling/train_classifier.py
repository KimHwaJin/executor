"""
분류 모델 학습 Tool - 이진 분류 모델을 학습합니다. (logistic/RF/XGBoost/LGBM)
"""

def train_classifier(
        train_data: object=None,
        test_data: object=None,
        target_column: str=None,
        model_type: str="xgboost",
        positive_label: int=1,
) -> dict[str,object]:
    """
    이진 분류 모델을 학습합니다.
    
    Args:
        train_data: 학습용 pandas DataRame 또는 dict 컬럼-배열 형식
        test_data: 평가용 pandas DataFrame 또는 dict 컬럼-배열 형식
        target_column: target 컬럼명
        model_type: 모델 종류 (logistic, random_forest, xgboost, lightgbm)
        positive_label: 양성 값 (기본 1)
        
    Returns:
        {"model":object, "metrics":dict, "feature_importance":dict, "predictions":dict}
    """
    pass
