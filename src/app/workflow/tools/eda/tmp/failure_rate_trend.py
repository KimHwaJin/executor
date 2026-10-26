"""
불량률 추이 Tool - 시간/lot 순서에 따른 불량률  추이를 계산합니다.
"""

def failure_rate_trend(
        data: object = None,
        target_column: str = None,
        time_column: str = None,
        freq: str = "D",
        positive_label: int = 1,
        output_dir: str = "artifacts/metrics"
) -> dict[str, object]:
    """
    시간/lot 순서에 따른 불량률 추이를 계산합니다.
    Args:
        data: pandas DataFrame 또는 dict 컬럼-배열 형식
        target_column: 불량 여부 컬럼명
        time_column: 시간/순서 컬럼명
        freq: 리샘플링 주기 ("D", "W", "M" 등)
        positive_label: 불량(양성) 값 (기본 1)
        output_dir: 통계 JSON을 저장할 디렉터리 (예: artifacts/metrics)
    Returns:
        {"trend_index": list, "trend_rates": list, "trend_counts": list,
         "artifact_path": str}
    """

    import pandas as  pd
    import numpy as np
    from pathlib import Path
    from uuid import uuid4
    import json
    import re

    # target을 이진 분류로 변환
    if data is None:
        raise ValueError("data를 입력해주세요.")
    if target_column is None:
        raise ValueError("target_column을 입력해주세요.")
    if time_column is None:
        raise ValueError("time_column을 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    missing_columns = [
        column for column in [target_column, time_column] if column not in df.columns
    ]
    if missing_columns:
        raise ValueError(f"데이터에 없는 컬럼입니다: {missing_columns}")

    target = df[target_column].eq(positive_label).astype(int)

    # time_column 처리
    time_values = df[time_column]

    # datetime으로 변환 시도
    parsed_time = pd.to_datetime(time_values, errors="coerce")
    valid_datetime_ratio = float(parsed_time.notna().mean()) if len(parsed_time) else 0.0

    # datetime인 경우 freq 기반 리샘플링
    if valid_datetime_ratio >= 0.8:
        trend_data = pd.DataFrame(
            {
                "time": parsed_time,
                "target": target,
            }
        ).dropna(subset=["time"])
        if trend_data.empty:
            raise ValueError("유효한 time_column 값이 없습니다.")

        trend_data = trend_data.sort_values("time").set_index("time")
        grouped = trend_data["target"].resample(freq)
        trend_index = [str(value) for value in grouped.mean().index.tolist()]
        trend_rates = [
            None if pd.isna(value) else float(value)
            for value in grouped.mean().tolist()
        ]
        trend_counts = [int(value) for value in grouped.count().tolist()]
    else:

        # 숫자 순서인 경우: freq를 그룹 크기로 해석 (정수 변환 시도)
        order_values = pd.to_numeric(time_values, errors="coerce")
        trend_data = pd.DataFrame(
            {
                "order": order_values,
                "target": target,
            }
        ).dropna(subset=["order"])
        if trend_data.empty:
            raise ValueError("time_column을 datetime 또는 숫자 순서로 해석할 수 없습니다.")

        try:
            group_size = int(freq)
        except (TypeError, ValueError):
            group_size = 50
        if group_size < 1:
            raise ValueError("숫자 순서 기준 freq는 1 이상이어야 합니다.")

        # 정렬 후 그룹화
        trend_data = trend_data.sort_values("order").reset_index(drop=True)
        trend_data["_group"] = trend_data.index // group_size
        grouped = trend_data.groupby("_group")["target"]
        trend_index = [str(value) for value in grouped.mean().index.tolist()]
        trend_rates = [float(value) for value in grouped.mean().tolist()]
        trend_counts = [int(value) for value in grouped.count().tolist()]
        
    result = {
        "trend_index": trend_index,
        "trend_rates": trend_rates,
        "trend_counts": trend_counts,
        "artifact_path": None,
    }

    if output_dir is not None:
        save_dir = Path(output_dir).expanduser().resolve()
        save_dir.mkdir(parents=True, exist_ok=True)
        safe_time = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            str(time_column),
        ).strip("_")
        if not safe_time:
            safe_time = "time"
        safe_time = safe_time[:80]
        artifact_path = save_dir / f"failure_rate_trend__{safe_time}__{uuid4().hex}.json"
        result["artifact_path"] = str(artifact_path)
        artifact_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return result
