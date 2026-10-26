"""통계 또는 모델 기반으로 중요한 feature를 선택하는 Tool."""


def select_features(
    data: object,
    target_column: str = None,
    method: str = "correlation",
    k: int = 10,
) -> dict[str, object]:
    """target과 관련성이 높은 수치형 feature를 선택합니다.

    Args:
        data: target 컬럼을 포함한 pandas DataFrame 또는 dict.
        target_column: 예측 대상 컬럼명.
        method: 선택 방법("correlation", "mutual_info", "rfe").
        k: 선택할 feature 개수.

    Returns:
        {
            "selected_data": DataFrame,
            "shape": list,
            "columns": list,
            "selected_features": list,
            "feature_scores": dict,
            "requested_k": int,
            "effective_k": int,
        }
    """
    import numpy as np
    import pandas as pd

    if data is None:
        raise ValueError("data를 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        try:
            df = pd.DataFrame(data)
        except ValueError as exc:
            raise ValueError(
                f"data를 DataFrame으로 변환할 수 없습니다: {exc}"
            ) from exc
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    if df.empty:
        raise ValueError("data가 비어 있습니다.")
    if target_column is None:
        raise ValueError("target_column을 입력해주세요.")
    if target_column not in df.columns:
        raise ValueError(f"data에 target_column '{target_column}'이 없습니다.")
    if df[target_column].isna().any():
        raise ValueError("target_column에 결측치가 있습니다.")

    method = str(method).lower()
    valid_methods = {"correlation", "mutual_info", "rfe"}
    if method not in valid_methods:
        raise ValueError(f"method는 {sorted(valid_methods)} 중 하나여야 합니다.")

    if isinstance(k, (bool, np.bool_)):
        raise TypeError("k는 1 이상의 정수여야 합니다.")
    try:
        requested_k = int(k)
    except (TypeError, ValueError) as exc:
        raise TypeError("k는 1 이상의 정수여야 합니다.") from exc
    if requested_k <= 0:
        raise ValueError("k는 1 이상이어야 합니다.")

    feature_columns = [
        column
        for column in df.columns
        if column != target_column
        and (
            pd.api.types.is_numeric_dtype(df[column].dtype)
            or pd.api.types.is_bool_dtype(df[column].dtype)
        )
    ]
    if not feature_columns:
        raise ValueError("선택할 수 있는 수치형 또는 boolean feature가 없습니다.")
    available_count = len(feature_columns)
    effective_k = min(requested_k, available_count)

    features = df[feature_columns].copy()
    feature_values = features.to_numpy(dtype=float, na_value=np.nan)
    if np.isnan(feature_values).any():
        raise ValueError(
            "feature 컬럼에 결측치가 있습니다. 먼저 impute_missing을 실행해주세요."
        )
    if np.isinf(feature_values).any():
        raise ValueError("feature 컬럼에 무한대 값이 있습니다.")

    target = df[target_column]
    target_unique_count = int(target.nunique(dropna=True))
    is_classification = (
        pd.api.types.is_bool_dtype(target.dtype)
        or not pd.api.types.is_numeric_dtype(target.dtype)
        or target_unique_count <= 20
    )

    if method == "correlation":
        if not pd.api.types.is_numeric_dtype(target.dtype):
            raise TypeError(
                "correlation 방법은 수치형 target_column에서만 사용할 수 있습니다."
            )

        target_values = target.to_numpy(dtype=float, na_value=np.nan)
        if np.isinf(target_values).any():
            raise ValueError("target_column에 무한대 값이 있습니다.")

        scores = {}
        for column in feature_columns:
            correlation = features[column].corr(target)
            scores[column] = 0.0 if pd.isna(correlation) else abs(float(correlation))

    elif method == "mutual_info":
        from sklearn.feature_selection import (
            mutual_info_classif,
            mutual_info_regression,
        )
        from sklearn.preprocessing import LabelEncoder

        if is_classification:
            encoded_target = LabelEncoder().fit_transform(target)
            calculated_scores = mutual_info_classif(
                features,
                encoded_target,
                random_state=42,
            )
        else:
            if not pd.api.types.is_numeric_dtype(target.dtype):
                raise TypeError(
                    "회귀 mutual_info 방법은 수치형 target_column이 필요합니다."
                )
            calculated_scores = mutual_info_regression(
                features,
                target.to_numpy(dtype=float),
                random_state=42,
            )

        scores = {
            column: float(score)
            for column, score in zip(feature_columns, calculated_scores)
        }

    else:
        from sklearn.feature_selection import RFE
        from sklearn.linear_model import LinearRegression, LogisticRegression
        from sklearn.preprocessing import LabelEncoder

        if is_classification:
            encoded_target = LabelEncoder().fit_transform(target)
            if len(np.unique(encoded_target)) < 2:
                raise ValueError(
                    "분류 target_column에는 서로 다른 클래스가 2개 이상 필요합니다."
                )
            estimator = LogisticRegression(max_iter=1000, random_state=42)
            rfe_target = encoded_target
        else:
            if not pd.api.types.is_numeric_dtype(target.dtype):
                raise TypeError("회귀 RFE는 수치형 target_column이 필요합니다.")
            estimator = LinearRegression()
            rfe_target = target.to_numpy(dtype=float)

        selector = RFE(
            estimator=estimator,
            n_features_to_select=effective_k,
            step=1,
        )
        selector.fit(features, rfe_target)

        selected_by_rfe = [
            column
            for column, selected in zip(feature_columns, selector.support_)
            if selected
        ]
        coefficients = np.asarray(selector.estimator_.coef_)
        if coefficients.ndim == 1:
            coefficient_scores = np.abs(coefficients)
        else:
            coefficient_scores = np.mean(np.abs(coefficients), axis=0)

        scores = {
            column: float(score)
            for column, score in zip(selected_by_rfe, coefficient_scores)
        }

    selected_features = sorted(
        scores,
        key=lambda column: (-scores[column], feature_columns.index(column)),
    )[:effective_k]
    feature_scores = {
        column: scores[column] for column in selected_features
    }

    selected_data = df[selected_features + [target_column]].copy()

    return {
        "selected_data": selected_data,
        "shape": list(selected_data.shape),
        "columns": list(selected_data.columns),
        "selected_features": selected_features,
        "feature_scores": feature_scores,
        "requested_k": requested_k,
        "effective_k": effective_k,
    }
