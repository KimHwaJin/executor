"""Generate deterministic wide-format mock NCE and WT symbol datasets."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_DATA_DIR = PROJECT_ROOT / "mock_data"
NCE_OUTPUT = MOCK_DATA_DIR / "df_nce_wide_format.parquet"
WT_OUTPUT = MOCK_DATA_DIR / "df_wt_symbol_wide_format.parquet"

KEY_COLUMNS = ["alias_lot_id", "wf_id", "x", "y"]
NCE_VALUE_COLUMNS = [
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
WT_VALUE_COLUMNS = ["PT1H.symbol", "PT1H.end_tm", "PT1H.p_f"]


def _make_keys(row_count: int) -> pd.DataFrame:
    row_number = np.arange(row_count)
    wafer_position = row_number % 2_500
    die_position = wafer_position % 100
    return pd.DataFrame(
        {
            "alias_lot_id": ["TE2AM92"]
            + [f"TE2AM{92 + (number // 2500):02d}" for number in row_number[1:]],
            "wf_id": [f"{(position // 100) + 1:02d}" for position in wafer_position],
            "x": die_position % 10 + 1,
            "y": die_position // 10 + 1,
        }
    )


def generate_mock_wide_data(row_count: int = 10_000, seed: int = 20260904) -> None:
    """Write matching NCE/WT wide datasets with stable, analysis-ready schemas."""
    rng = np.random.default_rng(seed)
    keys = _make_keys(row_count)

    nce = keys.copy()
    latent_signal = rng.normal(0.0, 1.0, row_count)
    for index, column in enumerate(NCE_VALUE_COLUMNS):
        nce[column] = np.round(
            2.0 + index * 0.12 + latent_signal * 0.08 + rng.normal(0, 0.12, row_count),
            6,
        )

    failure_score = latent_signal + rng.normal(0, 0.75, row_count)
    failure = (failure_score > 1.0).astype("int8")
    symbols = np.where(
        failure == 1,
        rng.choice(["EB", "EC", "ED"], row_count),
        rng.choice(["PASS", "OK"], row_count),
    )
    timestamps = pd.Timestamp("2026-08-01 05:49:17") + pd.to_timedelta(
        np.arange(row_count), unit="min"
    )

    wt = keys.copy()
    wt["PT1H.symbol"] = symbols
    wt["PT1H.end_tm"] = timestamps
    wt["PT1H.p_f"] = failure

    # Keep the first row identical to the schema example in the data contract.
    keys.loc[0, ["wf_id", "x", "y"]] = ["01", 11, 21]
    nce.loc[0, KEY_COLUMNS] = keys.loc[0, KEY_COLUMNS]
    wt.loc[0, KEY_COLUMNS] = keys.loc[0, KEY_COLUMNS]
    wt.loc[0, WT_VALUE_COLUMNS] = ["EB", pd.Timestamp("2026-08-01 05:49:17"), 0]

    MOCK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    nce.to_parquet(NCE_OUTPUT, index=False)
    wt.to_parquet(WT_OUTPUT, index=False)


if __name__ == "__main__":
    generate_mock_wide_data()
