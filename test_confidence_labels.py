import unittest

import pandas as pd

from run_daily_predictions import get_batter_consistency, validate_model_dataflow


class ConfidenceLabelTests(unittest.TestCase):
    def test_validate_model_dataflow_reports_missing_feature_columns(self):
        train_df = pd.DataFrame({"batter": [1], "pitcher": [2], "is_hr": [0]})
        live_df = pd.DataFrame({"batter": [1], "pitcher": [2], "bat_pa_count": [1]})

        issues = validate_model_dataflow(train_df, live_df, required_features=["bat_pa_count", "pitch_pa_count"])

        self.assertTrue(any("Training missing feature columns" in issue for issue in issues))
        self.assertTrue(any("Live missing feature columns" in issue for issue in issues))

    def test_get_batter_consistency_uses_events_when_is_hr_missing(self):
        df = pd.DataFrame(
            {
                "batter": [1] * 10 + [2] * 10,
                "game_pk": list(range(10)) + list(range(10, 20)),
                "events": ["single"] * 10 + ["home_run", "single", "single", "single", "single", "single", "single", "single", "single", "single"],
            }
        )

        batter_one_consistency = get_batter_consistency(df, 1)
        batter_two_consistency = get_batter_consistency(df, 2)

        self.assertGreater(batter_one_consistency, batter_two_consistency)
        self.assertGreaterEqual(batter_one_consistency, 0.45)
        self.assertLess(batter_two_consistency, 0.45)


if __name__ == "__main__":
    unittest.main()
