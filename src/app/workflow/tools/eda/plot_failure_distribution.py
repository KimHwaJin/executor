"""
불량 분포 시각화 Tool - 불량/정상 클래스 분포를 시각화합니다. (target 분포만 확인. 불량 비율 확인)
"""

def plot_failure_distribution(
        data: object,
        target_column: str = None,
        positive_label: int = 1,
        output_dir: str = "artifacts/plots",
        figsize: tuple = (8, 6),
        dpi: int = 150,
) -> dict[str, object]:
    """
    불량/정상 클래스 분포를 시각화합니다.
    Args:
        data: pandas DataFrame 또는 dict 컬럼-배열 형식
        positive_label: 불량(양성) 값 (기본 1)
        output_dir: 플롯 이미지를 저장할 디렉터리 (예: artifacts/plots)
        figsize: 그래프 크기. 기본 (8, 6)
        dpi: 저장 이미지 해상도. 기본 150
    Returns:
        {"plot_path": str, "class_counts": dict, "class_ratio": dict}
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

    # 입력 검증
    if data is None:
        raise ValueError("data를 입력해주세요.")
    if target_column is None:
        raise ValueError("target_column을 입력해주세요.")
    if output_dir is None:
        raise ValueError("output_dir를 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    if target_column not in df.columns:
        raise ValueError(f"데이터에 없는 target_column입니다: {target_column}")

    try:
        dpi = int(dpi)
    except (TypeError, ValueError) as exc:
        raise ValueError("dpi는 정수로 변환 가능한 값이어야 합니다.") from exc
    if dpi < 1:
        raise ValueError("dpi는 1 이상이어야 합니다.")

    # target을 이진 분류로 변환
    target = df[target_column].eq(positive_label)

    # 클래스별 카운트 
    class_counts = {
        "normal": int((~target).sum()),
        "failure": int(target.sum()),
    }
    total_count = sum(class_counts.values())
    class_ratio = {
        key: (0.0 if total_count == 0 else float(value / total_count))
        for key, value in class_counts.items()
    }

    # 시각화 (서브디렉토리 없이 평면 구조)
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    safe_target = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(target_column),
    ).strip("_")
    if not safe_target:
        safe_target = "target"
    safe_target = safe_target[:80]
    plot_path = output_path / f"plot_failure_distribution__{safe_target}__{uuid.uuid4().hex}.png"

    figure, axes = plt.subplots(1, 2, figsize=figsize)
    try:

        # (A) 막대 그래프
        labels = ["normal", "failure"]
        values = [class_counts[label] for label in labels]
        axes[0].bar(labels, values, color=["#4C72B0", "#C44E52"], alpha=0.85)
        axes[0].set_title("클래스별 개수")
        axes[0].set_ylabel("count")
        for index, value in enumerate(values):
            axes[0].text(index, value, str(value), ha="center", va="bottom")

        # (B) 파이 차트
        if total_count > 0:
            axes[1].pie(
                values,
                labels=labels,
                autopct="%1.1f%%",
                colors=["#4C72B0", "#C44E52"],
                startangle=90,
            )
        else:
            axes[1].text(0.5, 0.5, "데이터 없음", ha="center", va="center")
        axes[1].set_title("클래스 비율")

        figure.tight_layout()
        figure.savefig(plot_path, dpi=dpi, bbox_inches="tight")
    finally:
        plt.close(figure)

    return {
        "plot_path": str(plot_path),
        "class_counts": class_counts,
        "class_ratio": class_ratio,
    }
