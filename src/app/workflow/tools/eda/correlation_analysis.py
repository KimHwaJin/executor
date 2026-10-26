"""
상관관계 분석 Tool - 공정 파라미터 간 상관관계 및 불량과의 상관분석
"""

def correlation_analysis(
    data: object,
    target_col: str=None,
    method: str = "pearson",
    top_n: int =10,
    significance_level: float = 0.05,
) -> dict[str, object]:
    """
    공정 파라미터 간 상관관계 및 target과의 상관분석을 수행합니다.
    Args:
        data: pandas DataFrame (또는 dict 컬럼-배열 형식)
        target_col: 타겟 컬럼명 (불량 여부 등). None이면 파라미터 간 상관만 계산
        method: 상관계수 방법 ("pearson","spearman", "kendall")
        top_n: 상위 N개 상관관계 반환
        significance_level: 유의수준 (기본 0.05)
    Returns:
        {
            "correlation_matrix": dict - 상관행렬 (컬럼-배열 형식),
            "top_correlations": dict - 상위 상관관계 쌍 (컬럼-배열 형식),
            "significant_pairs": dict - 통계적으로 유의한 상관 쌍 (컬럼-배열 형식),
            "target_correlations": didct - target과의 상관관계 (컬럼-배열 형식, target_col 있을 때),
            "shape": list,
            "columns": list,
        }
    """
    import pandas as pd
    import numpy as np
    from scipy import stats

    # top_n 타입 방어: LLM/YAML에서 문자열로 전달될 수 있으므로 정수로 캐스팅
    try:
        top_n = int(top_n)
    except (TypeError, ValueError) as exc:
        raise ValueError("top_n은 정수로 변환 가능한 값이어야 합니다.") from exc

    if top_n < 0:
        raise ValueError("top_n은 0 이상이어야 합니다.")

    try:
        significance_level = float(significance_level)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "significance_level은 실수로 변환 가능한 값이어야 합니다."
        ) from exc

    if not 0 < significance_level <= 1:
        raise ValueError("significance_level은 0보다 크고 1 이하여야 합니다.")

    method = str(method).lower()
    valid_methods = {"pearson", "spearman", "kendall"}
    if method not in valid_methods:
        raise ValueError(f"method는 {sorted(valid_methods)} 중 하나여야 합니다.")

    if data is None:
        raise ValueError("data를 입력해주세요.")
    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    # 수치형 컬럼만 선택
    numeric_df = df.select_dtypes(include=[np.number, "bool"])
    if numeric_df.shape[1] == 0:
        raise ValueError("상관관계를 계산할 수치형 컬럼이 없습니다.")

    # target_col 검증
    if target_col is not None:
        if target_col not in df.columns:
            raise ValueError(f"데이터에 target_col '{target_col}'이 없습니다.")
        if target_col not in numeric_df.columns:
            raise TypeError("target_col은 수치형 컬럼이어야 합니다.")

    # 상관행렬 계산
    correlation_matrix = numeric_df.corr(method=method)

    # 상관행렬을 컬럼-배열 형식으로 변환
    correlation_matrix_dict = {
        column: [
            None if pd.isna(value) else float(value)
            for value in correlation_matrix[column]
        ]
        for column in correlation_matrix.columns
    }

    # 상위 상관관계 쌍 추출 (중복 제거)
    pair_records = []
    numeric_columns = list(numeric_df.columns)
    test_functions = {
        "pearson": stats.pearsonr,
        "spearman": stats.spearmanr,
        "kendall": stats.kendalltau,
    }

    for left_index, left_column in enumerate(numeric_columns):
        for right_column in numeric_columns[left_index + 1 :]:
            pair_data = numeric_df[[left_column, right_column]].dropna()
            correlation = correlation_matrix.loc[left_column, right_column]

            if (
                len(pair_data) < 2
                or pair_data[left_column].nunique() < 2
                or pair_data[right_column].nunique() < 2
            ):
                p_value = np.nan
            else:
                _, p_value = test_functions[method](
                    pair_data[left_column].to_numpy(dtype=float),
                    pair_data[right_column].to_numpy(dtype=float),
                )

            pair_records.append(
                {
                    "column_1": left_column,
                    "column_2": right_column,
                    "correlation": (
                        None if pd.isna(correlation) else float(correlation)
                    ),
                    "p_value": None if pd.isna(p_value) else float(p_value),
                    "significant": bool(
                        not pd.isna(p_value) and p_value < significance_level
                    ),
                }
            )

    # 절댓값 기준 정렬
    sorted_pairs = sorted(
        pair_records,
        key=lambda record: (
            record["correlation"] is not None,
            (
                abs(record["correlation"])
                if record["correlation"] is not None
                else -1
            ),
        ),
        reverse=True,
    )

    pair_columns = [
        "column_1",
        "column_2",
        "correlation",
        "p_value",
        "significant",
    ]

    def to_column_arrays(records: list) -> dict[str, list]:
        return {
            column: [record[column] for record in records]
            for column in pair_columns
        }

    top_correlations = to_column_arrays(sorted_pairs[:top_n])

    # 통계적 유의성 검정 (p-value)
    significant_pairs = to_column_arrays(
        [record for record in sorted_pairs if record["significant"]]
    )

    # target과의 상관관계
    target_columns = ["column", "correlation", "p_value", "significant"]
    target_correlations = {column: [] for column in target_columns}

    if target_col is not None:
        target_records = []
        for record in pair_records:
            if record["column_1"] == target_col:
                related_column = record["column_2"]
            elif record["column_2"] == target_col:
                related_column = record["column_1"]
            else:
                continue

            target_records.append(
                {
                    "column": related_column,
                    "correlation": record["correlation"],
                    "p_value": record["p_value"],
                    "significant": record["significant"],
                }
            )

        target_records.sort(
            key=lambda record: (
                record["correlation"] is not None,
                (
                    abs(record["correlation"])
                    if record["correlation"] is not None
                    else -1
                ),
            ),
            reverse=True,
        )
        target_correlations = {
            column: [record[column] for record in target_records]
            for column in target_columns
        }

    return {
        "correlation_matrix": correlation_matrix_dict,
        "top_correlations": top_correlations,
        "significant_pairs": significant_pairs,
        "target_correlations": target_correlations,
        "shape": list(df.shape),
        "columns": list(df.columns),
    }
