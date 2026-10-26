"""Temporary NCE transformation Tool backed by analysis-ready mock data."""

def transform_nce(
    data: dict | None = None,
    transform_op: str = "pivot",
    index: list | None = None,
    columns: str = "process",
    values: list | None = None,
    aggfunc: str = "last",
    **kwargs: object,
) -> dict[str, object]:
    """Return NCE data in the agreed wide analysis schema.

    Wide input from ``extract_data`` is preserved, including its row limit.
    Other input triggers the deterministic wide NCE mock fallback.
    """
    from pathlib import Path

    import pandas as pd

    expected_columns = [
        "alias_lot_id", "wf_id", "x", "y",
        "BLC(CELL) OPEN MASK MACRO.max_val",
        "DNW MASK MACRO.max_val",
        "ISO(PERI) + CUT MASK MACRO.max_val",
        "M4C MASK MACRO.max_val",
        "MBO MASK MACRO.max_val",
        "P + ADD MASK MACRO.max_val",
        "PMX MASK MACRO.max_val",
        "REP MASK MACRO.max_val",
        "SN OPEN MASK MACRO.max_val",
        "SNC PARTITION MASK MACRO.max_val",
    ]
    frame = pd.DataFrame(data) if data else pd.DataFrame()
    used_mock_fallback = not set(expected_columns).issubset(frame.columns)
    if used_mock_fallback:
        mock_path = Path("/workspace/pv/data/df_nce_wide_format.parquet")
        frame = pd.read_parquet(mock_path)
    frame = frame.loc[:, expected_columns]

    return {
        "data": frame.to_dict(orient="list"),
        "shape": list(frame.shape),
        "columns": frame.columns.tolist(),
        "transform_info": {
            "applied": True,
            "transform_op": transform_op,
            "data_type": "nce",
            "steps": ["load_mock_wide_nce"] if used_mock_fallback else ["validate_wide_nce"],
            "mock": True,
            "options": {"index": index, "columns": columns, "values": values, "aggfunc": aggfunc, **kwargs},
        },
    }
