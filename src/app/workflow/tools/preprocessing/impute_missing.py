"""
결측치 대체 Tool - 다양한 전략으로 결측치를 채웁니다.

strategy:
- mean: 수치형 컬럼 평균
- median: 수치형 컬럼 중앙값
- mode: 최빈값 (수치형/범주형 모두)
- constant: 지정된 fill_value
- ffill / bfill: 시계열/순서 데이터
- knn: KNN 기반 대체 (수치형)
- auto: 컬럼 타입에 따라 자동 선택 (수치형->median, 범주형->mode)
"""

def impute_missing(
        data: object,
        strategy: str = "auto",
        columns: list = None,
        fill_value: object=None,
        n_neighbors: int =5,
) -> dict[str,object]:
    """
    결측치를 지정된 전략으로 대체합니다.
    Args:
        data: pandas DataFrame 또는 dict 컬럼-배열 형식
        strategy: 대체 전략 ("auto", "mean", "median", "mode", "constant", "ffill", "bfill", "knn")
        columns: 대상 컬럼 목록 (None이면 결측치가 있는 모든 컬럼)
        fill_value: strategy="constant"일 때 사용할 값
        n_neighbors: strategy="knn"일 때 이웃 수
    Returns:
        {"imputed_data": dict, "shape": list, "columns": list,
         "imputation_summary": dict}
    """

    import pandas as pd
    import numpy as np

    # 대상 컬럼 결정
    # auto: 수치형-median, 범주형-mode
    if data is None:
        raise ValueError("data를 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        result = data.copy()
    elif isinstance(data, dict):
        result = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    strategy = str(strategy).lower()
    valid_strategies = {
        "auto",
        "mean",
        "median",
        "mode",
        "constant",
        "ffill",
        "bfill",
        "knn",
    }
    if strategy not in valid_strategies:
        raise ValueError(f"strategy는 {sorted(valid_strategies)} 중 하나여야 합니다.")

    if columns is None:
        selected_columns = [
            column for column in result.columns if result[column].isna().any()
        ]
    else:
        if not isinstance(columns, list):
            raise TypeError("columns는 컬럼명으로 구성된 list이거나 None이어야 합니다.")

        selected_columns = list(dict.fromkeys(columns))
        missing_columns = [
            column for column in selected_columns if column not in result.columns
        ]
        if missing_columns:
            raise ValueError(f"데이터에 없는 컬럼입니다: {missing_columns}")

    imputation_summary = {}

    if strategy == "knn" and selected_columns:
        from sklearn.impute import KNNImputer

        try:
            n_neighbors = int(n_neighbors)
        except (TypeError, ValueError) as exc:
            raise ValueError("n_neighbors는 정수로 변환 가능한 값이어야 합니다.") from exc

        if n_neighbors <= 0:
            raise ValueError("n_neighbors는 1 이상이어야 합니다.")

        non_numeric_columns = [
            column
            for column in selected_columns
            if not pd.api.types.is_numeric_dtype(result[column].dtype)
            or pd.api.types.is_bool_dtype(result[column].dtype)
        ]
        if non_numeric_columns:
            raise TypeError(
                f"knn 전략은 수치형 컬럼에만 사용할 수 있습니다: {non_numeric_columns}"
            )

        all_missing_columns = [
            column for column in selected_columns if result[column].isna().all()
        ]
        if all_missing_columns:
            raise ValueError(
                f"모든 값이 결측인 컬럼은 knn으로 대체할 수 없습니다: {all_missing_columns}"
            )

        numeric_values = result[selected_columns].to_numpy(
            dtype=float,
            na_value=np.nan,
        )
        if np.isinf(numeric_values).any():
            raise ValueError("knn 대상 컬럼에 무한대 값이 있습니다.")

        null_counts = {
            column: int(result[column].isna().sum())
            for column in selected_columns
        }
        imputer = KNNImputer(n_neighbors=n_neighbors)
        imputed_values = imputer.fit_transform(result[selected_columns])
        result.loc[:, selected_columns] = imputed_values

        for column in selected_columns:
            imputation_summary[column] = {
                "null_count": null_counts[column],
                "strategy_used": "knn",
                "fill_value": "knn_estimate",
            }

    else:
        for column in selected_columns:
            series = result[column]
            null_count = int(series.isna().sum())
            is_numeric = (
                pd.api.types.is_numeric_dtype(series.dtype)
                and not pd.api.types.is_bool_dtype(series.dtype)
            )
            column_strategy = strategy

            if strategy == "auto":
                column_strategy = "median" if is_numeric else "mode"

            used_fill_value = None

            if column_strategy in {"mean", "median"}:
                if not is_numeric:
                    raise TypeError(
                        f"{column_strategy} 전략은 수치형 컬럼에만 사용할 수 있습니다: {column}"
                    )

                if column_strategy == "mean":
                    used_fill_value = series.mean()
                else:
                    used_fill_value = series.median()

                if pd.isna(used_fill_value):
                    raise ValueError(
                        f"'{column}' 컬럼은 유효한 값이 없어 {column_strategy}으로 대체할 수 없습니다."
                    )
                result[column] = series.fillna(used_fill_value)

            elif column_strategy == "mode":
                modes = series.mode(dropna=True)
                if modes.empty:
                    raise ValueError(
                        f"'{column}' 컬럼은 유효한 값이 없어 mode로 대체할 수 없습니다."
                    )
                used_fill_value = modes.iloc[0]
                result[column] = series.fillna(used_fill_value)

            elif column_strategy == "constant":
                if fill_value is None:
                    raise ValueError("constant 전략은 fill_value가 필요합니다.")

                used_fill_value = fill_value
                if isinstance(series.dtype, pd.CategoricalDtype):
                    if fill_value not in series.cat.categories:
                        series = series.cat.add_categories([fill_value])
                result[column] = series.fillna(fill_value)

            elif column_strategy == "ffill":
                result[column] = series.ffill()

            elif column_strategy == "bfill":
                result[column] = series.bfill()

            imputation_summary[column] = {
                "null_count": null_count,
                "strategy_used": column_strategy,
                "fill_value": (
                    used_fill_value.item()
                    if isinstance(used_fill_value, np.generic)
                    else used_fill_value
                ),
            }

    return {
        "imputed_data": result.to_dict(orient="list"),
        "shape": list(result.shape),
        "columns": list(result.columns),
        "imputation_summary": imputation_summary,
    }
