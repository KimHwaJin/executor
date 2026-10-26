"""
분류 모델 평가 Tool - 학습된 분류 모델을 평가합니다 (precision/recall/f1/AUC)
"""

def evaluate_model(
        model: object = None,
        test_data: object = None,
        target_column: str = None,
        threshold: float = 0.5,
        positive_label: int = 1,
) -> dict[str, object]:
    """
    학습된 분류 모델을 평가합니다.
    
    Args:
        model: 학습된 분류 모델 객체
        test_data: 평가용 pandas DataFrame 또는 dict 컬럼-배열 형식
        target_column: target 컬럼명
        threshold: 분류 임계값 (기본 0.5)
        positive_label: 양성 값 (기본 1)
        
    Returns:
        {"accuracy": float, "precision": float, "recall": float, "f1": float, "roc_auc": float, "pr_auc": float, "confusion_matrix": list, "classification_report": dict}
    """
    pass
