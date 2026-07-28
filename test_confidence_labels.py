import unittest
from unittest.mock import patch

import pandas as pd

import run_daily_predictions as rdp
from run_daily_predictions import (
    apply_daily_hr_volume_constraints,
    apply_poisson_hr_filter,
    build_feedback_weight_series,
    estimate_model_reliability,
    get_batter_consistency,
    validate_model_dataflow,
)


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

    def test_fetch_historical_weather_returns_none_on_interrupt(self):
        with patch.object(rdp.requests, "get", side_effect=KeyboardInterrupt):
            self.assertIsNone(rdp._fetch_historical_weather_for_team_date("SEA", "2026-07-27"))

    def test_estimate_model_reliability_uses_medium_for_reasonable_signal(self):
        label = estimate_model_reliability(0.12, 0.6, 15)
        self.assertEqual(label, "MEDIUM")

    def test_apply_poisson_hr_filter_scales_outlier_game_probabilities(self):
        df = pd.DataFrame({"game_pk": [1, 1, 2], "pred_hr_prob": [0.20, 0.18, 0.05]})
        adjusted = apply_poisson_hr_filter(df, k=5, p_threshold=0.05, min_game_prob=0.2)
        self.assertLess(adjusted["pred_hr_prob"].iloc[0], 0.20)

    def test_apply_daily_hr_volume_constraints_scales_probabilities_down(self):
        df = pd.DataFrame({"pred_hr_prob": [0.20, 0.30, 0.10]})
        adjusted = apply_daily_hr_volume_constraints(df, game_count=1, avg_hr_per_game=0.1)
        self.assertLess(adjusted["pred_hr_prob"].iloc[0], 0.20)
        self.assertAlmostEqual(adjusted["pred_hr_prob"].sum(), 0.1, places=3)

    def test_build_feedback_weight_series_uses_game_pk_fallback_when_ids_missing(self):
        train_df = pd.DataFrame(
            {
                "game_pk": [1001, 1002],
                "batter": [11, 22],
                "pitcher": [33, 44],
            }
        )
        feedback_df = pd.DataFrame(
            {
                "event_date": ["2026-07-28"],
                "game_pk": [1002],
                "batter": [None],
                "pitcher": [None],
                "actual_hr": [1],
                "pred_hr_prob": [0.02],
            }
        )

        weights = build_feedback_weight_series(train_df, feedback_df)

        self.assertGreater(weights[1], 1.0)


if __name__ == "__main__":
    unittest.main()
