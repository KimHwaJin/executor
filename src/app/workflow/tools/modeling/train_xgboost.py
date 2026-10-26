"""XGBoost 분류 모델 학습 Tool."""


def train_xgboost(
    train_data: object = None,
    test_data: object = None,
    target_column: str = None,
    output_dir: str = "artifacts/models",
) -> dict[str, object]:
    """분할된 학습·테스트 데이터로 XGBoost 분류 모델을 학습합니다.

    Args:
        train_data: target 컬럼을 포함한 학습용 pandas DataFrame 또는 dict.
        test_data: target 컬럼을 포함한 테스트용 pandas DataFrame 또는 dict.
        target_column: 예측 대상 컬럼명.
        output_dir: 모델 파일을 저장할 디렉터리 (예: artifacts/models).

    Returns:
        {"model": model, "metrics": dict, "model_path": str}
    """
    import numpy as np
    import pandas as pd
    from pathlib import Path
    from uuid import uuid4
    from sklearn.metrics import accuracy_score, f1_score
    from sklearn.preprocessing import LabelEncoder
    from xgboost import XGBClassifier

    def to_dataframe(value: object, argument_name: str) -> pd.DataFrame:
        if value is None:
            raise ValueError(f"{argument_name}를 입력해주세요.")
        if isinstance(value, pd.DataFrame):
            return value.copy()
        if isinstance(value, dict):
            try:
                return pd.DataFrame(value)
            except ValueError as exc:
                raise ValueError(
                    f"{argument_name}를 DataFrame으로 변환할 수 없습니다: {exc}"
                ) from exc
        raise TypeError(
            f"{argument_name}는 pandas DataFrame 또는 dict 형식이어야 합니다."
        )

    train_df = to_dataframe(train_data, "train_data")
    test_df = to_dataframe(test_data, "test_data")

    if train_df.empty:
        raise ValueError("train_data가 비어 있습니다.")
    if test_df.empty:
        raise ValueError("test_data가 비어 있습니다.")
    if target_column is None:
        raise ValueError("target_column을 입력해주세요.")
    if output_dir is None:
        raise ValueError("output_dir를 입력해주세요.")

    for data_name, frame in (("train_data", train_df), ("test_data", test_df)):
        if target_column not in frame.columns:
            raise ValueError(
                f"{data_name}에 target_column '{target_column}'이 없습니다."
            )
        if frame[target_column].isna().any():
            raise ValueError(f"{data_name}의 target_column에 결측치가 있습니다.")

    feature_columns = [
        column for column in train_df.columns if column != target_column
    ]
    if not feature_columns:
        raise ValueError("학습에 사용할 feature 컬럼이 없습니다.")

    missing_test_columns = [
        column for column in feature_columns if column not in test_df.columns
    ]
    if missing_test_columns:
        raise ValueError(
            f"test_data에 학습 feature 컬럼이 없습니다: {missing_test_columns}"
        )

    invalid_feature_columns = []
    for column in feature_columns:
        train_is_numeric = (
            pd.api.types.is_numeric_dtype(train_df[column].dtype)
            or pd.api.types.is_bool_dtype(train_df[column].dtype)
        )
        test_is_numeric = (
            pd.api.types.is_numeric_dtype(test_df[column].dtype)
            or pd.api.types.is_bool_dtype(test_df[column].dtype)
        )
        if not train_is_numeric or not test_is_numeric:
            invalid_feature_columns.append(column)

    if invalid_feature_columns:
        raise TypeError(
            "feature 컬럼은 학습·테스트 데이터 모두에서 수치형 또는 "
            f"boolean이어야 합니다: {invalid_feature_columns}"
        )

    x_train = train_df[feature_columns].copy()
    x_test = test_df[feature_columns].copy()

    train_feature_values = x_train.to_numpy(dtype=float, na_value=np.nan)
    test_feature_values = x_test.to_numpy(dtype=float, na_value=np.nan)
    if np.isinf(train_feature_values).any():
        raise ValueError("train_data의 feature 컬럼에 무한대 값이 있습니다.")
    if np.isinf(test_feature_values).any():
        raise ValueError("test_data의 feature 컬럼에 무한대 값이 있습니다.")

    label_encoder = LabelEncoder()
    y_train = label_encoder.fit_transform(train_df[target_column])
    class_count = len(label_encoder.classes_)
    if class_count < 2:
        raise ValueError(
            "train_data의 target_column에는 서로 다른 클래스가 2개 이상 필요합니다."
        )

    try:
        y_test = label_encoder.transform(test_df[target_column])
    except ValueError as exc:
        raise ValueError(
            "test_data의 target_column에 학습 데이터에 없던 클래스가 있습니다."
        ) from exc

    model = XGBClassifier(
        n_estimators=100,
        max_depth=6,
        learning_rate=0.1,
        random_state=42,
        n_jobs=1,
        tree_method="hist",
        eval_metric="logloss" if class_count == 2 else "mlogloss",
    )
    model.fit(x_train, y_train)

    predictions = model.predict(x_test)
    f1_average = "binary" if class_count == 2 else "macro"

    def to_python_value(value: object) -> object:
        return value.item() if isinstance(value, np.generic) else value

    label_mapping = {
        class_index: to_python_value(class_label)
        for class_index, class_label in enumerate(label_encoder.classes_)
    }
    model.dtest_label_mapping_ = label_mapping
    model.dtest_feature_columns_ = feature_columns

    metrics = {
        "accuracy": float(accuracy_score(y_test, predictions)),
        "f1_score": float(
            f1_score(
                y_test,
                predictions,
                average=f1_average,
                zero_division=0,
            )
        ),
        "f1_average": f1_average,
        "train_rows": len(x_train),
        "test_rows": len(x_test),
        "target_column": target_column,
        "feature_columns": feature_columns,
        "label_mapping": label_mapping,
    }

    save_dir = Path(output_dir).expanduser().resolve()
    save_dir.mkdir(parents=True, exist_ok=True)
    artifact_id = uuid4().hex
    model_path = (
        save_dir / f"train_xgboost__model__{artifact_id}.json"
    )
    model.save_model(str(model_path))

    return {
        "model": model,
        "metrics": metrics,
        "model_path": str(model_path),
    }
