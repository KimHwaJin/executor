"""
구간별 불량률 Tool - 수치형 feature를 구간화하여 구간별 불량률을 계산합니다.
"""

def failure_rate_by_bin(
        data:object = None,
        target_column: str = None,
        feature_column: str = None,
        n_bins: int = 10,
        bin_method: str = "eqaul_width",
        positive_label: int = 1,
        output_dir: str = "artifacts/metrics",
) -> dict[str, object]:
    """
    수치형 feature를 구간화하여 구간별 불량률을 계산합니다.
    Args:
        data: pandas DataFrame 또는 dict 컬럼-배열 형식
        target_column: 불량 여부 컬럼명
        feature_column: 구간화할 수치형 컬럼명
        n_bins: 구간 수 
        bin_method: 구간화 방식 ("equal_width", "quantile")
        positive_label: 불량(양성) 값 (기본 1)
        output_dir: 통계 JSON을 저장할 디렉터리 (예: artifacts/metrics)
    Returns:
        {"bin_edges": list, "bin_rates": list, "bin_counts": list, "trend": str,
         "artifact_path": str}
    """
    import pandas as pd
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
    if feature_column is None:
        raise ValueError("feature_column을 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    missing_columns = [
        column for column in [target_column, feature_column] if column not in df.columns
    ]
    if missing_columns:
        raise ValueError(f"데이터에 없는 컬럼입니다: {missing_columns}")

    # 결측치/무한대 제거
    try:
        n_bins = int(n_bins)
    except (TypeError, ValueError) as exc:
        raise ValueError("n_bins는 정수로 변환 가능한 값이어야 합니다.") from exc
    if n_bins < 1:
        raise ValueError("n_bins는 1 이상이어야 합니다.")

    feature = pd.to_numeric(df[feature_column], errors="coerce")
    target = df[target_column].eq(positive_label).astype(int)
    valid = feature.notna() & np.isfinite(feature.to_numpy())
    feature = feature[valid]
    target = target[valid]
    if feature.empty:
        raise ValueError("구간화할 유효한 수치형 데이터가 없습니다.")

    # 구간화
    method = (bin_method or "equal_width").lower()
    if method == "eqaul_width":
        method = "equal_width"

    if feature.nunique(dropna=True) == 1:
        bins = pd.Series(["all"], index=feature.index, dtype="object")
        bin_edges = [float(feature.iloc[0])]
    elif method == "equal_width":
        bins, edges = pd.cut(
            feature,
            bins=min(n_bins, feature.nunique(dropna=True)),
            retbins=True,
            duplicates="drop",
            include_lowest=True,
        )
        bin_edges = [float(value) for value in edges]
    elif method == "quantile":
        bins, edges = pd.qcut(
            feature,
            q=min(n_bins, feature.nunique(dropna=True)),
            retbins=True,
            duplicates="drop",
        )
        bin_edges = [float(value) for value in edges]
    else:
        raise ValueError('bin_method는 "equal_width" 또는 "quantile"이어야 합니다.')

    grouped = pd.DataFrame({"bin": bins.astype(str), "target": target}).groupby(
        "bin", sort=False
    )["target"]
    bin_counts = [int(value) for value in grouped.count().tolist()]
    bin_rates = [float(value) for value in grouped.mean().tolist()]

    # 추세 판단 (상관계수 기반)
    trend = "flat"
    if len(bin_rates) >= 2 and np.nanstd(bin_rates) > 0:
        corr = np.corrcoef(np.arange(len(bin_rates)), np.asarray(bin_rates))[0, 1]
        if corr >= 0.3:
            trend = "increasing"
        elif corr <= -0.3:
            trend = "decreasing"

    result = {
        "bin_edges": bin_edges,
        "bin_rates": bin_rates,
        "bin_counts": bin_counts,
        "trend": trend,
        "artifact_path": None,
    }

    if output_dir is not None:
        save_dir = Path(output_dir).expanduser().resolve()
        save_dir.mkdir(parents=True, exist_ok=True)
        safe_feature = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            str(feature_column),
        ).strip("_")
        if not safe_feature:
            safe_feature = "feature"
        safe_feature = safe_feature[:80]
        artifact_path = save_dir / f"failure_rate_by_bin__{safe_feature}__{uuid4().hex}.json"
        result["artifact_path"] = str(artifact_path)
        artifact_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return result
