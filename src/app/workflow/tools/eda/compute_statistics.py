""" 
컬럼별 통계량 계산 Tool
데이터프레임의 각 컬럼에 대해 기본 통계량을 계산합니다.
수치형 컬럼은 describe(), 범주형 컬럼은 count/unique/top/freq를 반환합니다.
통계량 dict를 반환하고, output_dir이 지정되면 JSON 파일로 저장합니다.
"""

def compute_statistics(
    data: object,
    columns: list = None,
    output_dir: str | None = None,
) -> dict[str, object]:
    """
    데이터프레임의 각 컬럼에 대해 기본 통계량을 계산
    Args: 
        data: pandas DataFrame 객체 또는 dict 컬럼-배열 형식
        columns: 통계량을 계산할 컬럼 목록. None이면 모든 수치형/범주형 컬럼.
        output_dir: 통계 JSON 저장 경로. None이면 파일을 저장하지 않습니다.
    Returns:
        {"statistics": dict} - 컬럼별 통계량 (수치형: describe, 범주형: count/unique/top/freq)
    """
    import pandas as pd

    # dict 컬럼-배열 형식을 Dataframe으로 변환

    if data is None:
        raise ValueError("data를 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError(
            "data는 pandas DataFrame 또는 dict 형식이어야 합니다."
        )

    if columns is None:
        numeric_columns = list(df.select_dtypes(include="number").columns)
        categorical_columns = list(
            df.select_dtypes(include=["object", "category", "string", "bool"]).columns
        )
        selected_columns = numeric_columns + [
            column for column in categorical_columns if column not in numeric_columns
        ]
    else:
        if not isinstance(columns, list):
            raise TypeError("columns는 컬럼명으로 구성된 list이거나 None이어야 합니다.")

        missing_columns = [column for column in columns if column not in df.columns]
        if missing_columns:
            raise ValueError(f"데이터에 없는 컬럼입니다: {missing_columns}")
        selected_columns = columns

    statistics = {}

    for column in selected_columns:
        series = df[column]

        if pd.api.types.is_numeric_dtype(series.dtype) and not pd.api.types.is_bool_dtype(
            series.dtype
        ):
            statistics[column] = series.describe().to_dict()
            continue

        non_null_series = series.dropna()
        value_counts = non_null_series.value_counts()
        statistics[column] = {
            "count": int(non_null_series.count()),
            "unique": int(non_null_series.nunique()),
            "top": value_counts.index[0] if not value_counts.empty else None,
            "freq": int(value_counts.iloc[0]) if not value_counts.empty else None,
        }

    result = {"statistics": statistics}
    if output_dir is not None:
        from pathlib import Path
        from uuid import uuid4

        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        # pandas normalizes numpy scalars, timestamps and NaN to JSON values.
        (directory / f"compute_statistics__{uuid4().hex}.json").write_text(
            pd.Series(result).to_json(force_ascii=False, indent=2),
            encoding="utf-8",
        )
    return result
