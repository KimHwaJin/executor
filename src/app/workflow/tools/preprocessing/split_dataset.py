"""
데이터셋 분할 Tool - 학습/검증/테스트 셋으로 분할합니다.

분할 방식:
- random: 무작위 분할 (train_test_split)
- stratified: 층화 추출 (분류 문제에서 클래스 비율 유지)
- kfold: K-Fold 교차 검증용 분할
- timeseries: 시계열 순서 유지 분할
"""

def split_dataset(
    data: object,
    target_column: str = None,
    test_size: float = 0.2,
    val_size: float = 0.0,
    method: str = "random",
    n_splits: int = 5,
    random_state: int = 42,
    shuffle: bool = True,
) -> dict[str, object]:
    """
    데이터셋을 학습/검증/테스트 셋으로 분할합니다.
    Args:
        data: pandas DataFrame 또는 dict 컬럼-배열 형식
        target_column: 타겟 컬럼명 (stratified 분할 시 필수)
        test_size: 테스트 셋 비율(0~1)
        val_size: 검증 셋 비율 (0~1, 0이면 검증 셋 생성 안 함)
        method: 분할 방식 ("random", "stratified", "kfold", "timeseries")
        n_splits: kfold 방식 시 fold 수
        random_state: 재현성을 위한 시드
        shuffle: 셔플 여부 (tiemseries에서는 무시)
    Returns:
        모든 분할 방식에서 동일한 키를 갖는 결과 dict.
        ``val_data``는 검증 셋이 없으면 ``None``, ``folds``는 kfold 방식이
        아니면 빈 list입니다.
    """
    import pandas as pd
    import numpy as np

    if data is None:
        raise ValueError("data를 입력해주세요.")

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif isinstance(data, dict):
        df = pd.DataFrame(data)
    else:
        raise TypeError("data는 pandas DataFrame 또는 dict 형식이어야 합니다.")

    method = str(method).lower()
    valid_methods = {"random", "stratified", "kfold", "timeseries"}
    if method not in valid_methods:
        raise ValueError(f"method는 {sorted(valid_methods)} 중 하나여야 합니다.")

    n_total = len(df)
    if n_total < 2:
        raise ValueError("데이터 분할을 위해 최소 2개의 행이 필요합니다.")

    try:
        random_state = int(random_state)
    except (TypeError, ValueError) as exc:
        raise ValueError("random_state는 정수로 변환 가능한 값이어야 합니다.") from exc

    def to_column_dict(frame: pd.DataFrame) -> dict[str, list]:
        return frame.to_dict(orient="list")

    def validate_target() -> None:
        if target_column is None:
            raise ValueError(f"{method} 방식은 target_column이 필요합니다.")
        if target_column not in df.columns:
            raise ValueError(f"데이터에 target_column '{target_column}'이 없습니다.")
        if df[target_column].isna().any():
            raise ValueError("target_column에 결측치가 있어 층화 분할할 수 없습니다.")

    if method == "kfold":
        from sklearn.model_selection import KFold, StratifiedKFold

        try:
            n_splits = int(n_splits)
        except (TypeError, ValueError) as exc:
            raise ValueError("n_splits는 정수로 변환 가능한 값이어야 합니다.") from exc

        if n_splits < 2 or n_splits > n_total:
            raise ValueError(f"n_splits는 2 이상 {n_total} 이하여야 합니다.")

        splitter_random_state = random_state if shuffle else None
        if target_column is None:
            splitter = KFold(
                n_splits=n_splits,
                shuffle=shuffle,
                random_state=splitter_random_state,
            )
            split_iterator = splitter.split(df)
            kfold_type = "kfold"
        else:
            validate_target()
            splitter = StratifiedKFold(
                n_splits=n_splits,
                shuffle=shuffle,
                random_state=splitter_random_state,
            )
            split_iterator = splitter.split(df, df[target_column])
            kfold_type = "stratified_kfold"

        folds = []
        first_train_positions = None
        first_test_positions = None

        try:
            for fold_number, (train_positions, test_positions) in enumerate(
                split_iterator,
                start=1,
            ):
                if first_train_positions is None:
                    first_train_positions = train_positions
                    first_test_positions = test_positions

                folds.append(
                    {
                        "fold": fold_number,
                        "train_indices": df.index[train_positions].tolist(),
                        "test_indices": df.index[test_positions].tolist(),
                    }
                )
        except ValueError as exc:
            raise ValueError(f"kfold 분할에 실패했습니다: {exc}") from exc

        train_df = df.iloc[first_train_positions].copy()
        test_df = df.iloc[first_test_positions].copy()

        return {
            "train_data": to_column_dict(train_df),
            "test_data": to_column_dict(test_df),
            "val_data": None,
            "split_summary": {
                "method": method,
                "kfold_type": kfold_type,
                "total_rows": n_total,
                "n_splits": n_splits,
                "shuffle": bool(shuffle),
                "random_state": random_state,
            },
            "folds": folds,
        }

    try:
        test_size = float(test_size)
        val_size = float(val_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("test_size와 val_size는 실수로 변환 가능해야 합니다.") from exc

    if not 0 < test_size < 1:
        raise ValueError("test_size는 0보다 크고 1보다 작아야 합니다.")
    if not 0 <= val_size < 1:
        raise ValueError("val_size는 0 이상 1 미만이어야 합니다.")
    if test_size + val_size >= 1:
        raise ValueError("test_size와 val_size의 합은 1보다 작아야 합니다.")

    if method == "timeseries":
        test_count = int(np.ceil(n_total * test_size))
        validation_count = int(np.ceil(n_total * val_size)) if val_size else 0
        train_count = n_total - test_count - validation_count

        if train_count <= 0:
            raise ValueError("지정한 비율로는 학습 데이터가 남지 않습니다.")

        train_df = df.iloc[:train_count].copy()
        if validation_count:
            val_df = df.iloc[train_count : train_count + validation_count].copy()
        else:
            val_df = None
        test_df = df.iloc[train_count + validation_count :].copy()

    else:
        from sklearn.model_selection import train_test_split

        if method == "stratified":
            validate_target()
            if not shuffle:
                raise ValueError("stratified 방식은 shuffle=True가 필요합니다.")
            stratify_values = df[target_column]
        else:
            stratify_values = None

        try:
            remaining_df, test_df = train_test_split(
                df,
                test_size=test_size,
                random_state=random_state if shuffle else None,
                shuffle=shuffle,
                stratify=stratify_values,
            )

            if val_size:
                relative_val_size = val_size / (1 - test_size)
                remaining_stratify = (
                    remaining_df[target_column]
                    if method == "stratified"
                    else None
                )
                train_df, val_df = train_test_split(
                    remaining_df,
                    test_size=relative_val_size,
                    random_state=random_state if shuffle else None,
                    shuffle=shuffle,
                    stratify=remaining_stratify,
                )
            else:
                train_df = remaining_df
                val_df = None
        except ValueError as exc:
            raise ValueError(f"{method} 분할에 실패했습니다: {exc}") from exc

    split_summary = {
        "method": method,
        "total_rows": n_total,
        "train_rows": len(train_df),
        "test_rows": len(test_df),
        "val_rows": len(val_df) if val_df is not None else 0,
        "test_size": test_size,
        "val_size": val_size,
        "shuffle": False if method == "timeseries" else bool(shuffle),
        "random_state": random_state,
    }

    output = {
        "train_data": to_column_dict(train_df),
        "test_data": to_column_dict(test_df),
        "val_data": to_column_dict(val_df) if val_df is not None else None,
        "split_summary": split_summary,
        "folds": [],
    }

    return output
