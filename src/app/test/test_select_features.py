"""Tests for runtime feature-count adjustment in select_features."""

from __future__ import annotations

import unittest

import pandas as pd

from app.workflow.tools.preprocessing.select_features import select_features


class SelectFeaturesTests(unittest.TestCase):
    def test_k_is_capped_to_available_numeric_features(self):
        data = pd.DataFrame(
            {
                "feature_a": [0.1, 0.2, 0.8, 0.9],
                "feature_b": [1, 2, 3, 4],
                "category": ["a", "a", "b", "b"],
                "fail": [0, 0, 1, 1],
            }
        )

        result = select_features(
            data=data,
            target_column="fail",
            method="correlation",
            k=10,
        )

        self.assertEqual(result["requested_k"], 10)
        self.assertEqual(result["effective_k"], 2)
        self.assertEqual(len(result["selected_features"]), 2)
        self.assertEqual(set(result["selected_features"]), {"feature_a", "feature_b"})
        self.assertEqual(result["columns"][-1], "fail")

    def test_valid_k_is_not_changed(self):
        data = pd.DataFrame(
            {
                "feature_a": [0.1, 0.2, 0.8, 0.9],
                "feature_b": [1, 2, 3, 4],
                "fail": [0, 0, 1, 1],
            }
        )

        result = select_features(
            data=data,
            target_column="fail",
            method="correlation",
            k=1,
        )

        self.assertEqual(result["requested_k"], 1)
        self.assertEqual(result["effective_k"], 1)
        self.assertEqual(len(result["selected_features"]), 1)


if __name__ == "__main__":
    unittest.main()
