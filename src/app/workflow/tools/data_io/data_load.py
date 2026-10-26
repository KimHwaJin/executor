def data_load(parquet_path: str = None):
    """Parquet 파일을 pandas DataFrame으로 불러옵니다.

    Args:
        parquet_path: 불러올 Parquet 파일의 경로입니다.

    Returns:
        불러온 pandas DataFrame입니다.
    """
    from pathlib import Path

    import pandas as pd

    if parquet_path is None:
        raise ValueError("parquet_path를 입력해주세요.")

    path = Path(parquet_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Parquet 파일을 찾을 수 없습니다: {path}")

    df = pd.read_parquet(path)
    print(f"shape: {df.shape}")

    return df
