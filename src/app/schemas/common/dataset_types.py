"""Allowed source dataset types, independent of prediction targets/columns."""
from typing import Literal, get_args

DatasetType = Literal["kgsd", "nce", "pkt", "tdbi", "wis", "wt_bin_para", "wt_item_para", "wt_symbol"]
DATASET_TYPES = get_args(DatasetType)


def normalize_dataset_type(value):
    if not isinstance(value, str) or value.strip().lower() not in DATASET_TYPES:
        raise ValueError("data_type must be one of: " + ", ".join(DATASET_TYPES))
    return value.strip().lower()
