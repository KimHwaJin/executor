"""Tests for the stable split_dataset output contract."""

from __future__ import annotations

import unittest
from types import ModuleType
from unittest.mock import patch

import numpy as np
import pandas as pd

from app.workflow.tools.preprocessing.split_dataset import split_dataset


class SplitDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = pd.DataFrame(
            {
                "feature": list(range(10)),
                "target": [0, 1] * 5,
            }
        )

    @staticmethod
    def _mock_sklearn():
        model_selection = ModuleType("sklearn.model_selection")

        def train_test_split(frame, *, test_size, **_kwargs):
            test_count = max(1, int(np.ceil(len(frame) * test_size)))
            return frame.iloc[:-test_count], frame.iloc[-test_count:]

        class KFold:
            def __init__(self, *, n_splits, **_kwargs):
                self.n_splits = n_splits

            def split(self, frame, *_args):
                positions = np.arange(len(frame))
                for test_positions in np.array_split(positions, self.n_splits):
                    train_positions = np.setdiff1d(positions, test_positions)
                    yield train_positions, test_positions

        model_selection.train_test_split = train_test_split
        model_selection.KFold = KFold
        model_selection.StratifiedKFold = KFold
        sklearn = ModuleType("sklearn")
        sklearn.model_selection = model_selection
        return patch.dict(
            "sys.modules",
            {
                "sklearn": sklearn,
                "sklearn.model_selection": model_selection,
            },
        )

    def test_random_split_returns_all_registered_outputs(self):
        with self._mock_sklearn():
            result = split_dataset(self.data, target_column="target")

        self.assertEqual(
            set(result),
            {"train_data", "test_data", "val_data", "split_summary", "folds"},
        )
        self.assertIsNone(result["val_data"])
        self.assertEqual(result["folds"], [])

    def test_kfold_split_returns_all_registered_outputs(self):
        with self._mock_sklearn():
            result = split_dataset(
                self.data,
                target_column="target",
                method="kfold",
                n_splits=2,
            )

        self.assertEqual(
            set(result),
            {"train_data", "test_data", "val_data", "split_summary", "folds"},
        )
        self.assertIsNone(result["val_data"])
        self.assertEqual(len(result["folds"]), 2)


if __name__ == "__main__":
    unittest.main()
