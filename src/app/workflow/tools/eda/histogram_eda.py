"""
컬럼별 히스토그램 EDA tool
데이터프레임의 각 컬럼에 대해 히스토그램(수치형) 또는 카운트플롯(범주형)을 생성합니다.
생성된 이미지는 지정된 경로에 저장합니다.
"""

def histogram_eda(
    data: object,
    columns: list = None,
    bins: int = 30,
    output_dir: str = "artifacts/plots",
    figsize: tuple | list = (8, 5),
    dpi: int = 150,
) -> dict[str, object]:
    """
    데이터프레임의 각 컬럼에 대해 히스토그램/카운트플롯을 생성합니다.
    Args:
        data: pandas DataFrame (또는 dict 컬럼-배열 형식)
        columns: 플롯을 생성할 컬럼 목록. None이면 모든 수치형/범주형 컬럼.
        bins: 히스토그램 bin 수 (수치형 컬럼에만 적용)
        output_dir: 히스토그램 이미지를 저장할 디렉터리 (예: artifacts/plots)
        figsize: 그래프 크기. 기본 (8, 5)
        dpi: 저장 이미지 해상도. 기본 150

    Returns:
        {"histogram_paths": list} - 생성된 히스토그램 이미지 파일 경로 리스트
    """
    import pandas as pd
    import matplotlib.pyplot as plt
    import seaborn as sns

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

    try:
        bins = int(bins)
    except (TypeError, ValueError) as exc:
        raise ValueError("bins는 정수로 변환 가능한 값이어야 합니다.") from exc
    if bins <= 0:
        raise ValueError("bins는 1 이상이어야 합니다.")

    # 대상 컬럼 선택
    if columns is None:
        numeric_columns = list(df.select_dtypes(include="number").columns)
        categorical_columns = list(
            df.select_dtypes(
                include=["object", "category", "string", "bool"]
            ).columns
        )
        selected_columns = numeric_columns + [
            column
            for column in categorical_columns
            if column not in numeric_columns
        ]
    else:
        if not isinstance(columns, list):
            raise TypeError(
                "columns는 컬럼명으로 구성된 list이거나 None이어야 합니다."
            )
        missing_columns = [
            column for column in columns if column not in df.columns
        ]
        if missing_columns:
            raise ValueError(f"데이터에 없는 컬럼입니다: {missing_columns}")
        selected_columns = columns

    if not selected_columns:
        raise ValueError("플롯을 생성할 컬럼이 없습니다.")

    # 결과 이미지 저장 경로 생성
    if output_dir is None:
        raise ValueError("output_dir를 입력해주세요.")

    save_dir = Path(output_dir).expanduser().resolve()
    save_dir.mkdir(parents=True, exist_ok=True)

    plot_paths = []

    try:
        dpi = int(dpi)
    except (TypeError, ValueError) as exc:
        raise ValueError("dpi는 정수로 변환 가능한 값이어야 합니다.") from exc
    if dpi <= 0:
        raise ValueError("dpi는 1 이상이어야 합니다.")

    for index, column in enumerate(selected_columns, start=1):
        figure, axis = plt.subplots(figsize=figsize)
        try:
            series = df[column]

            if (
                pd.api.types.is_numeric_dtype(series.dtype)
                and not pd.api.types.is_bool_dtype(series.dtype)
            ):
                sns.histplot(series.dropna(), bins=bins, ax=axis)
                axis.set_title(f"{column} Histogram")
                axis.set_ylabel("Frequency")
            else:
                plot_series = series.astype("string").fillna("<NA>")
                category_order = plot_series.value_counts().index
                sns.countplot(
                    x=plot_series,
                    order=category_order,
                    ax=axis,
                )
                axis.set_title(f"{column} Count Plot")
                axis.set_ylabel("Count")
                axis.tick_params(axis="x", rotation=45)

            axis.set_xlabel(str(column))
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
                / f"histogram_eda__{safe_column_name}__{artifact_id}.png"
            )
            figure.savefig(
                image_path,
                dpi=dpi,
                bbox_inches="tight",
            )
            plot_paths.append(str(image_path))
        finally:
            plt.close(figure)

    return {"histogram_paths": plot_paths}
