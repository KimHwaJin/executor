"""
이상치 탐지 Tool - IQR, Z-score, Isolation Forest
"""

def detect_outliers(
        data: object,
        method: str = "iqr",
        columns: list = None,
) -> dict[str, object]:
    """
    다양한 방법으로 이상치를 탐지합니다.
    Args:
        data: pandas DataFrame (또는 dict 컬럼-배열 형식)
        method: 탐지 방법 ("iqr", "zscore", "isolation_forest")
        columns: 탐지할 컬럼 (선택사항)
    Returns:
        {"outlier_indices": list, "outlier_report": dict}
    """

    import pandas as pd
    import numpy as np

    if data is None:
        raise ValueError("data를 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    method = str(method).lower()
    valid_methods = {"iqr", "zscore", "isolation_forest"}
    if method not in valid_methods:
        raise ValueError(f"method는 {sorted(valid_methods)} 중 하나여야 합니다.")

    if columns is None:
        selected_columns = [
            column
            for column in df.select_dtypes(include=[np.number]).columns
            if not pd.api.types.is_bool_dtype(df[column].dtype)
        ]
    else:
        if not isinstance(columns, list):
            raise TypeError("columns는 컬럼명으로 구성된 list이거나 None이어야 합니다.")

        selected_columns = list(dict.fromkeys(columns))
        missing_columns = [
            column for column in selected_columns if column not in df.columns
        ]
        if missing_columns:
            raise ValueError(f"데이터에 없는 컬럼입니다: {missing_columns}")

        non_numeric_columns = [
            column
            for column in selected_columns
            if not pd.api.types.is_numeric_dtype(df[column].dtype)
            or pd.api.types.is_bool_dtype(df[column].dtype)
        ]
        if non_numeric_columns:
            raise TypeError(
                f"이상치 탐지 컬럼은 수치형이어야 합니다: {non_numeric_columns}"
            )

    if not selected_columns:
        raise ValueError("이상치를 탐지할 수치형 컬럼이 없습니다.")

    outlier_mask = pd.Series(False, index=df.index, dtype=bool)
    column_report = {}

    if method == "iqr":
        for column in selected_columns:
            series = df[column]
            finite_series = series.replace([np.inf, -np.inf], np.nan)
            first_quartile = finite_series.quantile(0.25)
            third_quartile = finite_series.quantile(0.75)
            iqr = third_quartile - first_quartile
            lower_bound = first_quartile - 1.5 * iqr
            upper_bound = third_quartile + 1.5 * iqr

            column_mask = (
                (series < lower_bound)
                | (series > upper_bound)
                | np.isinf(series)
            ).fillna(False)
            outlier_mask |= column_mask

            column_report[column] = {
                "lower_bound": (
                    None if pd.isna(lower_bound) else float(lower_bound)
                ),
                "upper_bound": (
                    None if pd.isna(upper_bound) else float(upper_bound)
                ),
                "outlier_count": int(column_mask.sum()),
            }

    elif method == "zscore":
        for column in selected_columns:
            series = df[column]
            finite_series = series.replace([np.inf, -np.inf], np.nan)
            mean = finite_series.mean()
            standard_deviation = finite_series.std(ddof=0)

            if pd.isna(standard_deviation) or standard_deviation == 0:
                column_mask = pd.Series(False, index=df.index, dtype=bool)
            else:
                z_scores = (finite_series - mean).abs() / standard_deviation
                column_mask = (z_scores > 3).fillna(False)

            column_mask |= np.isinf(series)
            outlier_mask |= column_mask

            column_report[column] = {
                "mean": None if pd.isna(mean) else float(mean),
                "standard_deviation": (
                    None
                    if pd.isna(standard_deviation)
                    else float(standard_deviation)
                ),
                "outlier_count": int(column_mask.sum()),
            }

    elif method == "isolation_forest" and len(df) > 0:
        from sklearn.ensemble import IsolationForest

        model_data = df[selected_columns].replace([np.inf, -np.inf], np.nan)
        model_data = model_data.fillna(model_data.median()).fillna(0.0)

        model = IsolationForest(
            n_estimators=100,
            contamination="auto",
            random_state=42,
        )
        predictions = model.fit_predict(model_data)
        outlier_mask = pd.Series(predictions == -1, index=df.index, dtype=bool)

        infinite_rows = np.isinf(
            df[selected_columns].to_numpy(dtype=float, na_value=np.nan)
        ).any(axis=1)
        outlier_mask |= pd.Series(infinite_rows, index=df.index, dtype=bool)

    outlier_indices = df.index[outlier_mask].tolist()
    outlier_count = int(outlier_mask.sum())
    row_count = len(df)

    outlier_report = {
        "method": method,
        "columns": selected_columns,
        "row_count": row_count,
        "outlier_count": outlier_count,
        "outlier_rate": round(outlier_count / row_count, 4) if row_count else 0.0,
        "column_report": column_report,
    }

    if method == "isolation_forest":
        outlier_report["parameters"] = {
            "n_estimators": 100,
            "contamination": "auto",
            "random_state": 42,
        }

    return {
        "outlier_indices": outlier_indices,
        "outlier_report": outlier_report,
    }
