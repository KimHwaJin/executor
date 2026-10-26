"""
target-feature 연관도 분석 Tool - target과 각 feature 간의 비선형 연관도(mutual_info, chi2, anova)를 계산합니다.
"""

def target_correlation(
        data: object,
        target_column: str = None,
        method: str = "mutual_info",
        positive_label: int = 1,
        top_n: int = 20,
        columns: list = None,
        output_dir: str = "artifacts/metrics",
) -> dict[str, object]:
    """
    target과 각 feature 간의 비선형 연관도를 계산합니다.
    (선형 상관은 correlation_analysis.py 사용)
    Args:
        data: pandas DataFrame 또는 dict 컬럼-배열 형식
        target_column: target 컬럼명
        method: 연관도 방법 ("mutual_info", "chi2", "anova")
        positive_label: 양성 값 (기본 1)
        top_n: 상위 N개 feature 반환
        columns: 분석할 feature 컬럼 리스트 (None이면 전체 수치형)
        output_dir: 통계 JSON을 저장할 디렉터리 (예: artifacts/metrics)
    Returns:
        {"scores": dict, "top_features": list, "ranked_features": list,
         "artifact_path": str}
    """
    import pandas as pd
    import numpy as np
    from pathlib import Path
    from uuid import uuid4
    import json

    # target을 이진 분류로 변환 (positive_label vs others)
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

    y = df[target_column].eq(positive_label).astype(int)

    # 분석 대상 feature 컬럼 선택
    if columns is None:
        feature_columns = [
            column
            for column in df.select_dtypes(include="number").columns
            if column != target_column
            and not pd.api.types.is_bool_dtype(df[column].dtype)
        ]
    else:
        if not isinstance(columns, list):
            raise TypeError("columns는 컬럼명으로 구성된 list이거나 None이어야 합니다.")
        missing_columns = [column for column in columns if column not in df.columns]
        if missing_columns:
            raise ValueError(f"데이터에 없는 columns입니다: {missing_columns}")
        feature_columns = columns

    if not feature_columns:
        raise ValueError("분석할 feature 컬럼이 없습니다.")

    # 결측치가 없는 행만 사용
    X = df[feature_columns].apply(pd.to_numeric, errors="coerce")
    valid = y.notna()
    X = X.loc[valid]
    y = y.loc[valid]

    method = (method or "mutual_info").lower()

    if method == "mutual_info":
        from sklearn.feature_selection import mutual_info_classif

        # 남은 결측치는  median으로 대체
        X_filled = X.fillna(X.median(numeric_only=True)).fillna(0)
        values = mutual_info_classif(X_filled, y, random_state=0)
        scores = {
            feature: float(score)
            for feature, score in zip(feature_columns, values)
        }
    elif method == "chi2":
        from sklearn.feature_selection import chi2
        from sklearn.preprocessing import MinMaxScaler

        # 결측치 대체
        X_filled = X.fillna(X.median(numeric_only=True)).fillna(0)

        #chi2 는 음수 불가 ->  MinMax scaling
        X_scaled = MinMaxScaler().fit_transform(X_filled)
        values, _ = chi2(X_scaled, y)
        scores = {
            feature: float(score)
            for feature, score in zip(feature_columns, values)
        }

    elif method == "anova":
        from scipy import stats
        scores = {}
        for feature in feature_columns:
            values = X[feature]
            normal_values = values[y == 0].dropna()
            failure_values = values[y == 1].dropna()
            if normal_values.empty or failure_values.empty:
                scores[feature] = None
                continue
            try:
                statistic, _ = stats.f_oneway(normal_values, failure_values)
                scores[feature] = None if np.isnan(statistic) else float(statistic)
            except ValueError:
                scores[feature] = None
    else:
        raise ValueError('method는 "mutual_info", "chi2", "anova" 중 하나여야 합니다.')

    ranked_features = sorted(
        (
            (feature, score)
            for feature, score in scores.items()
            if score is not None and not np.isnan(score)
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    try:
        top_n = int(top_n)
    except (TypeError, ValueError) as exc:
        raise ValueError("top_n은 정수로 변환 가능한 값이어야 합니다.") from exc
    if top_n < 1:
        raise ValueError("top_n은 1 이상이어야 합니다.")
    top_features = [feature for feature, _ in ranked_features[:top_n]]

    result = {
        "scores": scores,
        "top_features": top_features,
        "ranked_features":[
            {"feature": feature, "score":score} for feature, score in ranked_features
        ],
        "artifact_path": None,
    }

    if output_dir is not None:
        save_dir = Path(output_dir).expanduser().resolve()
        save_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = save_dir / f"target_correlation__{method}__{uuid4().hex}.json"
        result["artifact_path"] = str(artifact_path)
        artifact_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return result
