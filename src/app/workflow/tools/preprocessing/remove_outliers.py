"""
이상치 제거 Tool - 탐지된 인덱스에 해당하는 행을 제거합니다.
"""


def remove_outliers(
    data: object,
    outlier_indices: list = None,
) -> dict[str, object]:
    """
    이상치 인덱스에 해당하는 행을 제거합니다.

    Args:
        data: pandas DataFrame 또는 dict 컬럼-배열 형식
        outlier_indices: detect_outliers가 반환한 이상치 인덱스 목록

    Returns:
        {
            "data_without_outliers": DataFrame,
            "removed_indices": list,
            "shape": list,
            "columns": list,
        }
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

    if outlier_indices is None:
        raise ValueError("outlier_indices를 입력해주세요.")
    if not isinstance(outlier_indices, list):
        raise TypeError("outlier_indices는 list 형식이어야 합니다.")

    if not df.index.is_unique:
        raise ValueError(
            "DataFrame 인덱스가 중복되어 안전하게 행을 제거할 수 없습니다."
        )

    unique_indices = list(dict.fromkeys(outlier_indices))
    missing_indices = [
        index for index in unique_indices if index not in df.index
    ]
    if missing_indices:
        raise ValueError(f"데이터에 없는 인덱스입니다: {missing_indices}")

    removal_mask = df.index.isin(unique_indices)
    cleaned_data = df.loc[~removal_mask].copy()
    removed_indices = df.index[removal_mask].tolist()

    return {
        "data_without_outliers": cleaned_data,
        "removed_indices": removed_indices,
        "shape": list(cleaned_data.shape),
        "columns": list(cleaned_data.columns),
    }
