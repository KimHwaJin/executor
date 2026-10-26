"""
그룹별 불량률 시각화 Tool - 범주형 그룹별 불량률을 바 플롯으로 시각화합니다.
"""

def plot_failure_by_group(
        data: object = None,
        target_column: str = None,
        group_column: str = None,
        positive_label: int = 1,
        top_k: int = 20,
        output_dir: str = "artifacts/plots",
        figsize: tuple = (8, 6),
        dpi: int = 150,
) -> dict[str, object]:
    """
    범주형 그룹별 불량률을 바 플롯으로 시각화합니다.
    Args:
        data: pandas DataFrame 또는 dict 컬럼-배열 형식
        target_column: 불량 여부 컬럼명
        group_column: 그룹화할 범주형 컬럼명
        positive_label: 불량(양성) 값 (기본 1)
        top_k: 표시할 상위 그룹 수
        output_dir: 플롯 이미지를 저장할 디렉터리 (예: artifacts/plots)
        figsize: 그래프 크기. 기본 (8,6)
        dpi: 저장 이미지 해상도. 기본 150
    Returns:
        {"plot_path": str, "group_rates": dict}
    """

    import pandas as pd
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from pathlib import Path
    import uuid
    import re

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

    # 입력 검정
    if data is None:
        raise ValueError("data를 입력해주세요.")
    if target_column is None:
        raise ValueError("target_column을 입력해주세요.")
    if group_column is None:
        raise ValueError("group_column을 입력해주세요.")
    if output_dir is None:
        raise ValueError("output_dir를 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    missing_columns = [
        column for column in [target_column, group_column] if column not in df.columns
    ]
    if missing_columns:
        raise ValueError(f"데이터에 없는 컬럼입니다: {missing_columns}")

    try:
        top_k = int(top_k)
        dpi = int(dpi)
    except (TypeError, ValueError) as exc:
        raise ValueError("top_k와 dpi는 정수로 변환 가능한 값이어야 합니다.") from exc
    if top_k < 1:
        raise ValueError("top_k는 1 이상이어야 합니다.")
    if dpi < 1:
        raise ValueError("dpi는 1 이상이어야 합니다.")

    # target을 이진 분류로 변환
    target = df[target_column].eq(positive_label).astype(int)

    # 그룹별 불량률 계산
    group_data = pd.DataFrame(
        {
            "group": df[group_column],
            "target": target,
        }
    ).dropna(subset=["group"])
    if group_data.empty:
        raise ValueError("그룹별 불량률을 계산할 유효한 데이터가 없습니다.")

    grouped = group_data.groupby("group", dropna=True)["target"]
    rates = grouped.mean()

    # 전체 불량률 (기준선)
    overall_rate = float(group_data["target"].mean())

    # 불량률 기준 내림차순 정렬 후 상위 top_k
    selected_rates = rates.sort_values(ascending=False).head(top_k)
    group_rates = {str(group): float(rate) for group, rate in selected_rates.items()}

    # 시각화 (서브디렉토리 없이 평면 구조)
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    safe_group = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(group_column),
    ).strip("_")
    if not safe_group:
        safe_group = "group"
    safe_group = safe_group[:80]
    plot_path = output_path / f"plot_failure_by_group__{safe_group}__{uuid.uuid4().hex}.png"

    figure, axis = plt.subplots(figsize=figsize)
    try:
        bars = axis.bar(
            [str(group) for group in selected_rates.index],
            selected_rates.to_numpy(),
            color="#4C72B0",
            alpha=0.85,
        )
        axis.set_title(f"{group_column}별 불량률")
        axis.set_xlabel(str(group_column))
        axis.set_ylabel("불량률")
        axis.set_ylim(0, max(1.0, float(selected_rates.max()) * 1.15))
        axis.tick_params(axis="x", rotation=45)

        # 전체 불량률 기준선
        axis.axhline(
            overall_rate,
            color="#C44E52",
            linestyle="--",
            linewidth=1.5,
            label=f"전체 불량률 {overall_rate:.3f}",
        )
        axis.legend()

        # 값 표시
        for bar in bars:
            height = bar.get_height()
            axis.text(
                bar.get_x() + bar.get_width() / 2,
                height,
                f"{height:.3f}",
                ha="center",
                va="bottom",
                fontsize=9,
            )

        figure.tight_layout()
        figure.savefig(plot_path, dpi=dpi, bbox_inches="tight")
    finally:
        plt.close(figure)

    return {"plot_path": str(plot_path), "group_rates": group_rates}
