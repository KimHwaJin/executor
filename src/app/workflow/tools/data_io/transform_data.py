"""Test stand-in for the customer's unified data transformation Tool.

This always loads the matching mock Parquet, ignoring upstream data; it does
not implement production extraction/pivot logic. Keep the function self-contained
so the notebook generator can embed this file without importing the API package.
"""


def transform_data(
    data_type: str,
    data: dict,
    transform_op: str | None = None,
    mock_data_dir: str = "/workspace/pv/data",
) -> dict[str, object]:
    """Return NCE or WT symbol data in the existing wide output contract.

    Args:
        data_type: Supported types are nce and wt_symbol.
        data: Input from extract_data.data; accepted for compatibility but ignored.
        transform_op: Operation label for testing; defaults to pivot for NCE
            and wt_fail_pivot for WT. No production pivot is performed.
        mock_data_dir: Directory containing the existing wide mock Parquet files.

    Returns:
        {"data": dict, "shape": list, "columns": list, "transform_info": dict}
    """
    from pathlib import Path

    import pandas as pd

    if not isinstance(data_type, str):
        raise TypeError("data_type must be a string")
    normalized_type = data_type.strip().lower()
    keys = ["alias_lot_id", "wf_id", "x", "y"]
    if normalized_type == "nce":
        expected_columns = keys + [
            "BLC(CELL) OPEN MASK MACRO.max_val", "DNW MASK MACRO.max_val",
            "ISO(PERI) + CUT MASK MACRO.max_val", "M4C MASK MACRO.max_val",
            "MBO MASK MACRO.max_val", "P + ADD MASK MACRO.max_val",
            "PMX MASK MACRO.max_val", "REP MASK MACRO.max_val",
            "SN OPEN MASK MACRO.max_val", "SNC PARTITION MASK MACRO.max_val",
        ]
        filename = "df_nce_wide_format.parquet"
        operation = "pivot" if transform_op is None else transform_op
    elif normalized_type == "wt_symbol":
        expected_columns = keys + ["PT1H.symbol", "PT1H.end_tm", "PT1H.p_f"]
        filename = "df_wt_symbol_wide_format.parquet"
        operation = "wt_fail_pivot" if transform_op is None else transform_op
    else:
        raise ValueError(f"Unsupported data_type: {data_type!r}; use nce or wt_symbol")

    frame = pd.read_parquet(Path(mock_data_dir) / filename)
    frame = frame.loc[:, expected_columns]
    return {
        "data": frame.to_dict(orient="list"),
        "shape": list(frame.shape),
        "columns": frame.columns.tolist(),
        "transform_info": {
            "applied": True,
            "transform_op": operation,
            "data_type": normalized_type,
            "steps": [f"load_mock_wide_{normalized_type}"],
            "mock": True,
        },
    }
