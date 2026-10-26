"""
데이터 샘플링 Tool - 랜덤, 층화, 시스템 샘플링
"""

def sample_data(
    data: object,
    sample_size: int | float = None,
    method: str = "random",
    stratify_column: str = None,
    random_state: int = 42,
) -> dict[str, object]:
    """
    대용량 데이터셋에서 샘플을 추출합니다.
    Args:
        data: pandas DataFrame (또는 dict 컬럼-배열 형식)
        sample_size: 샘플 크기 또는 비율
        method: 샘플링 방법 ("random", "stratified", "systematic")
        stratify_column: 층화 샘플링 기준 컬럼
        random_state: 재현성을 위한 시드

    Returns:
        {"sampled_data": DataFrame, "sample_info": dict}
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

    original_size = len(df)
    if original_size == 0:
        raise ValueError("빈 데이터에서는 샘플을 추출할 수 없습니다.")

    method = str(method).lower()
    valid_methods = {"random", "stratified", "systematic"}
    if method not in valid_methods:
        raise ValueError(f"method는 {sorted(valid_methods)} 중 하나여야 합니다.")

    if sample_size is None:
        raise ValueError("sample_size를 입력해주세요.")
    if isinstance(sample_size, (bool, np.bool_)):
        raise TypeError("sample_size는 정수 행 개수 또는 실수 비율이어야 합니다.")

    if isinstance(sample_size, (int, np.integer)):
        sample_count = int(sample_size)
        if sample_count <= 0 or sample_count > original_size:
            raise ValueError(
                f"정수 sample_size는 1 이상 {original_size} 이하여야 합니다."
            )
    elif isinstance(sample_size, (float, np.floating)):
        sample_ratio = float(sample_size)
        if not 0 < sample_ratio <= 1:
            raise ValueError("실수 sample_size는 0보다 크고 1 이하여야 합니다.")
        sample_count = int(np.ceil(original_size * sample_ratio))
    else:
        raise TypeError("sample_size는 정수 행 개수 또는 실수 비율이어야 합니다.")

    try:
        random_state = int(random_state)
    except (TypeError, ValueError) as exc:
        raise ValueError("random_state는 정수로 변환 가능한 값이어야 합니다.") from exc

    if method == "random":
        sampled_data = df.sample(
            n=sample_count,
            replace=False,
            random_state=random_state,
        ).copy()

    elif method == "stratified":
        from sklearn.model_selection import train_test_split

        if stratify_column is None:
            raise ValueError("stratified 방식은 stratify_column이 필요합니다.")
        if stratify_column not in df.columns:
            raise ValueError(f"데이터에 stratify_column '{stratify_column}'이 없습니다.")
        if df[stratify_column].isna().any():
            raise ValueError("stratify_column에 결측치가 있어 층화 샘플링할 수 없습니다.")

        if sample_count == original_size:
            sampled_data = df.sample(
                n=sample_count,
                replace=False,
                random_state=random_state,
            ).copy()
        else:
            try:
                sampled_data, _ = train_test_split(
                    df,
                    train_size=sample_count,
                    random_state=random_state,
                    shuffle=True,
                    stratify=df[stratify_column],
                )
                sampled_data = sampled_data.copy()
            except ValueError as exc:
                raise ValueError(f"층화 샘플링에 실패했습니다: {exc}") from exc

    else:
        step = original_size / sample_count
        positions = np.floor(np.arange(sample_count) * step).astype(int)
        sampled_data = df.iloc[positions].copy()

    sample_info = {
        "original_size": original_size,
        "sampled_size": len(sampled_data),
        "sample_ratio": round(len(sampled_data) / original_size, 4),
        "requested_sample_size": sample_size,
        "method": method,
        "random_state": random_state,
    }
    if method == "stratified":
        sample_info["stratify_column"] = stratify_column

    return {
        "sampled_data": sampled_data,
        "sample_info": sample_info,
    }
