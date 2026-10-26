"""Temporary WT transformation Tool backed by analysis-ready mock data."""

def transform_wt(
    data: dict | None = None,
    transform_op: str = "wt_fail_pivot",
    keep_type: str = "last",
    pivot: bool = True,
    index: list | None = None,
    meta_cols: list | None = None,
    aggfunc: str = "last",
    **kwargs: object,
) -> dict[str, object]:
    """Return WT symbol data in the agreed wide analysis schema.

    Wide input from ``extract_data`` is passed through. Other local input
    triggers the deterministic WT wide mock fallback.
    """
    from pathlib import Path

    import pandas as pd

    expected_columns = [
        "alias_lot_id", "wf_id", "x", "y",
        "PT1H.symbol", "PT1H.end_tm", "PT1H.p_f",
    ]
    frame = pd.DataFrame(data) if data else pd.DataFrame()
    used_mock_fallback = not set(expected_columns).issubset(frame.columns)
    if used_mock_fallback:
        mock_path = Path("/workspace/pv/data/df_wt_symbol_wide_format.parquet")
        frame = pd.read_parquet(mock_path)
    frame = frame.loc[:, expected_columns]

    return {
        "data": frame.to_dict(orient="list"),
        "shape": list(frame.shape),
        "columns": frame.columns.tolist(),
        "transform_info": {
            "applied": True,
            "transform_op": transform_op,
            "data_type": "wt_symbol",
            "steps": ["load_mock_wide_wt"] if used_mock_fallback else ["validate_wide_wt"],
            "mock": True,
            "options": {"keep_type": keep_type, "pivot": pivot, "index": index, "meta_cols": meta_cols, "aggfunc": aggfunc, **kwargs},
        },
    }
