"""
feature별 불량 분포 시각화 Tool - feature별 불량/정상 분포를 박스/바이올린/히스토그램/KDE로 시각화합니다.
"""

def plot_failure_distribution_by_feature(
        data:object = None,
        target_column: str = None,
        feature_column: str = None,
        plot_type: str = "boxplot",
        positive_label: int = 1,
        output_dir: str = "artifacts/plots",
        figsize: tuple = (8, 6),
        dpi: int = 150,
) -> dict[str, object]:
    """
    feature별 불량/정상 분포를 박스/바이올린/히스토그램/KDE로 시각화합니다.
    Args:
        data: pandas DataFrame 또는 dict 컬럼-배열 형식
        target_column: 불량 여부 컬럼명
        feature_column: 시각화할 feature 컬럼명
        plot_type: 플롯 종류 ("boxplot", "violin", "histogram", "kde")
        positive_label: 불량(양성) 값 (기본 1)
        output_dir: 플롯 이미지를 저장할 디렉터리 (예: artifacts/plots)
        figsize: 그래프 크기. 기본 (8, 6)
        dpi: 저장 이미지 해상도. 기본 150
    Returns:
        {"plot_path": str, "failure_stats": dict, "normal_stats": dict}
    """

    import pandas as pd
    import numpy as np
    import matplotlib.pyplot as plt
    import seaborn as sns
    from matplotlib import font_manager
    from pathlib import Path
    import re
    import uuid

    # 한글 폰트 설정
    for font_name in [
        "Malgun Gothic",
        "AppleGothic",
        "NanumGothic",
        "Noto Sans CJK KR",
    ]:
        try:
            font_manager.findfont(font_name, fallback_to_default=False)
            plt.rcParams["font.family"] = font_name
            break
        except ValueError:
            continue
    plt.rcParams["axes.unicode_minus"] = False

    # 입력 검증
    if data is None:
        raise ValueError("data를 입력해주세요.")
    if target_column is None:
        raise ValueError("target_column을 입력해주세요.")
    if feature_column is None:
        raise ValueError("feature_column을 입력해주세요.")
    if output_dir is None:
        raise ValueError("output_dir를 입력해주세요.")

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

    try:
        dpi = int(dpi)
    except (TypeError, ValueError) as exc:
        raise ValueError("dpi는 정수로 변환 가능한 값이어야 합니다.") from exc
    if dpi < 1:
        raise ValueError("dpi는 1 이상이어야 합니다.")

    # target을 이진 분류로 변환
    plot_data = pd.DataFrame(
        {
            "feature": pd.to_numeric(df[feature_column], errors="coerce"),
            "class": np.where(df[target_column].eq(positive_label), "failure", "normal"),
        }
    ).dropna(subset=["feature"])
    if plot_data.empty:
        raise ValueError("시각화할 유효한 feature 값이 없습니다.")

    failure_values = plot_data.loc[plot_data["class"] == "failure", "feature"]
    normal_values = plot_data.loc[plot_data["class"] == "normal", "feature"]
    failure_stats = failure_values.describe().to_dict() if not failure_values.empty else {}
    normal_stats = normal_values.describe().to_dict() if not normal_values.empty else {}
    failure_stats = {key: float(value) for key, value in failure_stats.items()}
    normal_stats = {key: float(value) for key, value in normal_stats.items()}

    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    safe_feature = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(feature_column),
    ).strip("_")
    if not safe_feature:
        safe_feature = "feature"
    safe_feature = safe_feature[:80]
    method = (plot_type or "boxplot").lower()
    plot_path = output_path / f"plot_failure_distribution_by_feature__{safe_feature}__{method}__{uuid.uuid4().hex}.png"

    # 시각화 (서브디렉토리 없이 평면 구조)
    figure, axis = plt.subplots(figsize=figsize)
    try:
        # if plot_type == "boxplot":
        if method == "boxplot":
            sns.boxplot(data=plot_data, x="class", y="feature", ax=axis)
        # if plot_type == "violin":
        elif method == "violin":
            sns.violinplot(data=plot_data, x="class", y="feature", ax=axis, cut=0)
        # if plot_type == "histogram":
        elif method == "histogram":
            sns.histplot(
                data=plot_data,
                x="feature",
                hue="class",
                element="step",
                stat="density",
                common_norm=False,
                ax=axis,
            )
        # if plot_type == "kde":
        elif method == "kde":
            if failure_values.nunique(dropna=True) < 2 or normal_values.nunique(dropna=True) < 2:
                raise ValueError("kde 플롯에는 각 클래스별로 서로 다른 값이 2개 이상 필요합니다.")
            sns.kdeplot(data=plot_data, x="feature", hue="class", common_norm=False, ax=axis)
        else:
            raise ValueError('plot_type은 "boxplot", "violin", "histogram", "kde" 중 하나여야 합니다.')

        axis.set_title(f"{feature_column} 불량/정상 분포")
        axis.set_xlabel(str(feature_column))
        axis.set_ylabel(str(feature_column) if method in {"boxplot", "violin"} else "density")
        figure.tight_layout()
        figure.savefig(plot_path, dpi=dpi, bbox_inches="tight")
    finally:
        plt.close(figure)

    return {
        "plot_path": str(plot_path),
        "failure_stats": failure_stats,
        "normal_stats": normal_stats,
    }
