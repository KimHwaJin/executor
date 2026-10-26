"""두 데이터셋을 공통 컬럼으로 병합하는 Tool."""


def merge_data(
    left_data: object,
    right_data: object,
    join_columns: list = None,
    how: str = "inner",
    suffixes: tuple = ("_left", "_right"),
    validate: str = None,
) -> dict[str, object]:
    """두 DataFrame을 지정한 공통 컬럼을 기준으로 병합합니다.

    Args:
        left_data: 왼쪽 pandas DataFrame 또는 dict.
        right_data: 오른쪽 pandas DataFrame 또는 dict.
        join_columns: 양쪽 데이터에 공통으로 존재하는 병합 기준 컬럼 목록.
        how: 병합 방식("inner", "left", "right", "outer").
        suffixes: 이름이 겹치는 비기준 컬럼에 붙일 접미사 2개.
        validate: 키 관계 검증 방식. None 또는 "one_to_one",
            "one_to_many", "many_to_one", "many_to_many".

    Returns:
        {"merged_data": DataFrame, "merge_summary": dict}
    """
    import pandas as pd

    def to_dataframe(value: object, argument_name: str) -> pd.DataFrame:
        if value is None:
            raise ValueError(f"{argument_name}를 입력해주세요.")
        if isinstance(value, pd.DataFrame):
            return value.copy()
        if isinstance(value, dict):
            try:
                return pd.DataFrame(value)
            except ValueError as exc:
                raise ValueError(
                    f"{argument_name}를 DataFrame으로 변환할 수 없습니다: {exc}"
                ) from exc
        raise TypeError(
            f"{argument_name}는 pandas DataFrame 또는 dict 형식이어야 합니다."
        )

    left_df = to_dataframe(left_data, "left_data")
    right_df = to_dataframe(right_data, "right_data")

    if join_columns is None:
        raise ValueError("join_columns를 입력해주세요.")
    if not isinstance(join_columns, list):
        raise TypeError("join_columns는 컬럼명으로 구성된 list여야 합니다.")

    join_columns = list(dict.fromkeys(join_columns))
    if not join_columns:
        raise ValueError("join_columns에는 하나 이상의 컬럼명이 필요합니다.")
    if any(not isinstance(column, str) or not column for column in join_columns):
        raise TypeError("join_columns의 모든 값은 비어 있지 않은 문자열이어야 합니다.")

    missing_left_columns = [
        column for column in join_columns if column not in left_df.columns
    ]
    missing_right_columns = [
        column for column in join_columns if column not in right_df.columns
    ]
    if missing_left_columns:
        raise ValueError(
            f"left_data에 병합 기준 컬럼이 없습니다: {missing_left_columns}"
        )
    if missing_right_columns:
        raise ValueError(
            f"right_data에 병합 기준 컬럼이 없습니다: {missing_right_columns}"
        )

    how = str(how).lower()
    valid_how_values = {"inner", "left", "right", "outer"}
    if how not in valid_how_values:
        raise ValueError(f"how는 {sorted(valid_how_values)} 중 하나여야 합니다.")

    if not isinstance(suffixes, (tuple, list)) or len(suffixes) != 2:
        raise TypeError("suffixes는 두 문자열로 구성된 tuple 또는 list여야 합니다.")
    suffixes = tuple(suffixes)
    if any(not isinstance(suffix, str) for suffix in suffixes):
        raise TypeError("suffixes의 모든 값은 문자열이어야 합니다.")
    if suffixes[0] == suffixes[1]:
        raise ValueError("suffixes의 두 값은 서로 달라야 합니다.")

    valid_validate_values = {
        None,
        "one_to_one",
        "one_to_many",
        "many_to_one",
        "many_to_many",
    }
    if validate not in valid_validate_values:
        raise ValueError(
            "validate는 None, 'one_to_one', 'one_to_many', "
            "'many_to_one', 'many_to_many' 중 하나여야 합니다."
        )

    overlapping_columns = sorted(
        (set(left_df.columns) & set(right_df.columns)) - set(join_columns)
    )

    indicator_column = "__merge_status__"
    if indicator_column in left_df.columns or indicator_column in right_df.columns:
        raise ValueError(
            f"입력 데이터에 예약 컬럼 '{indicator_column}'이 존재합니다."
        )

    try:
        merged_data = pd.merge(
            left_df,
            right_df,
            on=join_columns,
            how=how,
            suffixes=suffixes,
            validate=validate,
            indicator=indicator_column,
            sort=False,
        )
    except pd.errors.MergeError as exc:
        raise ValueError(f"데이터 병합에 실패했습니다: {exc}") from exc

    merge_counts = merged_data[indicator_column].value_counts().to_dict()
    merged_data = merged_data.drop(columns=[indicator_column])

    left_rows = len(left_df)
    right_rows = len(right_df)
    merged_rows = len(merged_data)
    merge_summary = {
        "how": how,
        "join_columns": join_columns,
        "left_rows": left_rows,
        "right_rows": right_rows,
        "merged_rows": merged_rows,
        "matched_rows": int(merge_counts.get("both", 0)),
        "left_only_rows": int(merge_counts.get("left_only", 0)),
        "right_only_rows": int(merge_counts.get("right_only", 0)),
        "overlapping_columns": overlapping_columns,
        "suffixes": list(suffixes),
        "validate": validate,
        "left_duplicate_key_rows": int(
            left_df.duplicated(subset=join_columns, keep=False).sum()
        ),
        "right_duplicate_key_rows": int(
            right_df.duplicated(subset=join_columns, keep=False).sum()
        ),
        "left_missing_key_rows": int(left_df[join_columns].isna().any(axis=1).sum()),
        "right_missing_key_rows": int(
            right_df[join_columns].isna().any(axis=1).sum()
        ),
    }

    return {
        "merged_data": merged_data,
        "merge_summary": merge_summary,
    }
