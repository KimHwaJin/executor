"""Current extract_data -> transform_data flow using actual local Parquet fixtures."""

import unittest
from pathlib import Path
import shutil
import tempfile

from agent_config import PROJECT_ROOT
from app.workflow.tools.data_io.extract_data import extract_data
from app.workflow.tools.data_io.transform_data import transform_data


class MockDataIoTest(unittest.TestCase):
    query = {
        "lot_cd": "6E2",
        "process": ["ALL"],
        "query_mode": "period",
        "start_dt": "2026-08-01",
        "end_dt": "2026-08-01",
        "wafer_list": None,
        "title": None,
        "symbol": None,
        "bin": None,
        "device": None,
        "owner": None,
        "program": None,
        "equip": None,
        "limit": 3,
    }

    def setUp(self):
        self.temporary_root = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary_root.name) / "data"
        data_dir.mkdir()
        for filename in (
            "df_nce_wide_format.parquet",
            "df_wt_symbol_wide_format.parquet",
        ):
            shutil.copyfile(PROJECT_ROOT / "mock_data" / filename, data_dir / filename)
        self.mock_data_dir = str(data_dir)

    def tearDown(self):
        self.temporary_root.cleanup()

    def test_extract_then_transform_nce_returns_wide_data(self):
        extracted = extract_data(data_type="nce", **self.query)
        transformed = transform_data(data_type=extracted["data_type"], data=extracted["data"],
                                     mock_data_dir=self.mock_data_dir)

        self.assertEqual(extracted["query_params"], self.query)
        self.assertEqual(extracted["data"], {})
        self.assertTrue(extracted["metadata"]["placeholder"])
        self.assertEqual(transformed["shape"], [10_000, 14])
        self.assertIn("BLC(CELL) OPEN MASK MACRO.max_val", transformed["data"])
        self.assertNotIn("process", transformed["data"])
        self.assertNotIn("max_val", transformed["data"])

    def test_extract_then_transform_wt_returns_wide_data(self):
        extracted = extract_data(data_type="wt_symbol", **self.query)
        transformed = transform_data(
            data_type=extracted["data_type"], data=extracted["data"],
            mock_data_dir=self.mock_data_dir,
        )

        self.assertEqual(extracted["data"], {})
        self.assertEqual(transformed["shape"], [10_000, 7])
        self.assertEqual(transformed["data"]["PT1H.symbol"][0], "EB")
        self.assertEqual(transformed["data"]["PT1H.p_f"][0], 0)
        self.assertNotIn("process", transformed["data"])

    def test_extract_placeholder_does_not_validate_customer_arguments(self):
        result = extract_data(data_type="future_type", **{**self.query, "limit": 0})

        self.assertEqual(result["data"], {})
        self.assertEqual(result["data_type"], "future_type")
        self.assertEqual(result["query_params"]["limit"], 0)


if __name__ == "__main__":
    unittest.main()
