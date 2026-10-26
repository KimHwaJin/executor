"""
불량/정상 그룹 비교 Tool - 불량/정상 그룹 간 수치 feature 분포를 통계적으로 비교합니다.
"""

def compare_failure_normal(
        data: object,
        target_column: str = None,
        feature_columns: list = None,
        positive_label: int = 1,
        output_dir: str = "artifacts/metrics",
) -> dict[str, object]:
    """
    불량/정상 그룹 간 수치 feature 분포를 통계적으로 비교합니다.
    Args:
        data: pnadas DataFrame 또는 dict 컬럼-배열 형식
        target_column: 불량 여부 컬럼명
        feature_columns: 비교할 수치형 컬럼 리스트 (None이면 전체 수치형)
        positive_label: 불량(양성) 값 (기본 1)
        output_dir: 통계 JSON을 저장할 디렉터리 (예: artifacts/metrics)
    Returns:
        {"feature_stats": dict, "mannwhitney_pvalues": dict, "effect_sizes": dict,
         "artifact_path": str}
    """
    import pandas as pd
    import numpy as np
    from scipy import stats
    from pathlib import Path
    from uuid import uuid4
    import json

    # target을 이진 분류로 변환
    if data is None:
        raise ValueError("data를 입력해주세요.")
    if target_column is None:
        raise ValueError("target_column을 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    if target_column not in df.columns:
        raise ValueError(f"데이터에 없는 target_column입니다: {target_column}")

    target = df[target_column].eq(positive_label)

    # feature 컬럼 선택
    if feature_columns is None:
        feature_columns = [
            column
            for column in df.select_dtypes(include="number").columns
            if column != target_column
            and not pd.api.types.is_bool_dtype(df[column].dtype)
        ]
    else:
        if not isinstance(feature_columns, list):
            raise TypeError("feature_columns는 컬럼명으로 구성된 list이거나 None이어야 합니다.")
        missing_columns = [column for column in feature_columns if column not in df.columns]
        if missing_columns:
            raise ValueError(f"데이터에 없는 feature_columns입니다: {missing_columns}")

    if not feature_columns:
        raise ValueError("비교할 수치형 feature 컬럼이 없습니다.")

    feature_stats = {}
    mannwhitney_pvalues = {}
    effect_sizes = {}

    # 결측치가 없는 행만 사용
    for column in feature_columns:
        numeric_feature = pd.to_numeric(df[column], errors="coerce")
        valid = numeric_feature.notna() & target.notna()
        failure_values = numeric_feature[valid & target]
        normal_values = numeric_feature[valid & ~target]

        failure_count = int(failure_values.count())
        normal_count = int(normal_values.count())
        feature_stats[column] = {
            "failure_count": failure_count,
            "normal_count": normal_count,
            "failure_mean": None if failure_count == 0 else float(failure_values.mean()),
            "normal_mean": None if normal_count == 0 else float(normal_values.mean()),
            "failure_median": None if failure_count == 0 else float(failure_values.median()),
            "normal_median": None if normal_count == 0 else float(normal_values.median()),
            "failure_std": None if failure_count <= 1 else float(failure_values.std(ddof=1)),
            "normal_std": None if normal_count <= 1 else float(normal_values.std(ddof=1)),
        }

        # Mann-Whitney U 검정
        if failure_count > 0 and normal_count > 0:
            try:
                _, pvalue = stats.mannwhitneyu(
                    failure_values,
                    normal_values,
                    alternative="two-sided",
                )
                mannwhitney_pvalues[column] = float(pvalue)
            except ValueError:
                mannwhitney_pvalues[column] = None
        else:
            mannwhitney_pvalues[column] = None

        # Cohen's d 효과 크기
        if failure_count > 1 and normal_count > 1:
            failure_var = failure_values.var(ddof=1)
            normal_var = normal_values.var(ddof=1)
            pooled_std = np.sqrt(
                ((failure_count - 1) * failure_var + (normal_count - 1) * normal_var)
                / (failure_count + normal_count - 2)
            )
            effect_sizes[column] = (
                None
                if pooled_std == 0 or np.isnan(pooled_std)
                else float((failure_values.mean() - normal_values.mean()) / pooled_std)
            )
        else:
            effect_sizes[column] = None

    result = {
        "feature_stats": feature_stats,
        "mannwhitney_pvalues": mannwhitney_pvalues,
        "effect_sizes": effect_sizes,
        "artifact_path": None,
    }

    if output_dir is not None:
        save_dir = Path(output_dir).expanduser().resolve()
        save_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = save_dir / f"compare_failure_normal__summary__{uuid4().hex}.json"
        result["artifact_path"] = str(artifact_path)
        artifact_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return result
