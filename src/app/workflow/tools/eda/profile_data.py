"""
데이터 프로파일링 Tool - 기본 통계 및 품질 분석
"""

def profile_data(
    data: object,
) -> dict[str, object]:
    """
    데이터셋의 기본 통계 및 품질을 분석합니다.
    Args:
        data: pandas DataFrame (또는 dict 컬럼-배열 형식)
    Returns:
        {"profile": dict} - row_count, missing_rate 등
    """

    import pandas as pd

    if data is None:
        raise ValueError("data를 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    row_count = len(df)
    column_count = len(df.columns)
    total_cell_count = row_count * column_count

    if total_cell_count == 0:
        missing_rate = 0.0
    else:
        missing_count = int(df.isna().sum().sum())
        missing_rate = missing_count / total_cell_count

    return {
        "profile": {
            "row_count": row_count,
            "column_count": column_count,
            "missing_rate": round(missing_rate, 4),
        }
    }
