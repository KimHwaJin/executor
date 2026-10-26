"""
컬럼별 Boxplot EDA Tool
데이터프레임의 각 수치형 컬럼에 대해 Boxplot을 생성하고 이미지 파일로 저장합니다.
"""

def boxplot_eda(
    data: object,
    output_dir: str = "artifacts/plots",
    figsize: tuple | list = (6, 5),
    dpi: int = 150,
) -> dict[str, object]:
    """
    데이터프레임의 각 수치형 컬럼에 대해 Boxplot을 생성합니다.
    Args:
        data: pandas DataFrame (또는 dict 컬럼-배열 형식)
        output_dir: Boxplot 이미지를 저장할 디렉터리 (예: artifacts/plots)
        figsize: 그래프 크기. 기본 (6, 5)
        dpi: 저장 이미지 해상도. 기본 150

    Returns:
        {"boxplot_paths": list} - 생성된 Boxplot 이미지 파일 경로 리스트
    """
    import pandas as pd
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from pathlib import Path
    from uuid import uuid4
    import re

    # 한글 폰트 설정 (한글 라벨/타이틀 깨짐 방지)
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

    # dict 컬럼-배열 형식을 DataFrame으로 변환
    if data is None:
        raise ValueError("data를 입력해주세요.")
    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    numeric_columns = [
        column
        for column in df.select_dtypes(include="number").columns
        if not pd.api.types.is_bool_dtype(df[column].dtype)
    ]
    if not numeric_columns:
        raise ValueError("Boxplot을 생성할 수치형 컬럼이 없습니다.")

    # 결과 이미지 저장 경로 생성
    if output_dir is None:
        raise ValueError("output_dir를 입력해주세요.")

    save_dir = Path(output_dir).expanduser().resolve()
    save_dir.mkdir(parents=True, exist_ok=True)

    try:
        dpi = int(dpi)
    except (TypeError, ValueError) as exc:
        raise ValueError("dpi는 정수로 변환 가능한 값이어야 합니다.") from exc
    if dpi <= 0:
        raise ValueError("dpi는 1 이상이어야 합니다.")

    boxplot_paths = []

    for index, column in enumerate(numeric_columns, start=1):
        figure, axis = plt.subplots(figsize=figsize)
        try:
            values = df[column].dropna()

            if values.empty:
                axis.text(
                    0.5,
                    0.5,
                    "유효한 데이터가 없습니다.",
                    ha="center",
                    va="center",
                    transform=axis.transAxes,
                )
                axis.set_xticks([])
                axis.set_yticks([])
            else:
                axis.boxplot(
                    values.to_numpy(),
                    patch_artist=True,
                    boxprops={"facecolor": "#4C72B0", "alpha": 0.65},
                    medianprops={"color": "#C44E52", "linewidth": 2},
                )
                axis.set_xticks([1])
                axis.set_xticklabels([str(column)])

            axis.set_title(f"{column} Boxplot")
            axis.set_ylabel(str(column))
            figure.tight_layout()

            safe_column_name = re.sub(
                r"[^A-Za-z0-9_-]+",
                "_",
                str(column),
            ).strip("_")
            if not safe_column_name:
                safe_column_name = f"column_{index}"
            safe_column_name = safe_column_name[:80]

            artifact_id = uuid4().hex
            image_path = (
                save_dir
                / f"boxplot_eda__{safe_column_name}__{artifact_id}.png"
            )
            figure.savefig(
                image_path,
                dpi=dpi,
                bbox_inches="tight",
            )
            boxplot_paths.append(str(image_path))
        finally:
            plt.close(figure)

    return {"boxplot_paths": boxplot_paths}
