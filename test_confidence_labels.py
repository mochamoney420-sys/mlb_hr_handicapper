import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import run_daily_predictions as rdp
from analyze_hr_patterns import build_learning_insights_from_evaluation
from run_daily_predictions import (
    apply_daily_hr_volume_constraints,
    apply_monotonic_prob_calibration,
    apply_poisson_hr_filter,
    build_feedback_weight_series,
    estimate_model_reliability,
    get_batter_consistency,
    validate_model_dataflow,
    _prepare_discord_rankings,
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

    def test_apply_monotonic_prob_calibration_lifts_top_end_without_reordering(self):
        df = pd.DataFrame({"pred_hr_prob": [0.01, 0.02, 0.08, 0.15]})
        adjusted = apply_monotonic_prob_calibration(df, gamma=1.55, cap=0.14, top_signal_boost=0.015)

        self.assertGreater(adjusted["pred_hr_prob"].iloc[3], 0.10)
        self.assertGreater(adjusted["pred_hr_prob"].iloc[2], adjusted["pred_hr_prob"].iloc[1])
        self.assertEqual(adjusted["pred_hr_prob"].rank().tolist(), [1.0, 2.0, 3.0, 4.0])

    def test_apply_monotonic_prob_calibration_uses_looser_defaults(self):
        df = pd.DataFrame({"pred_hr_prob": [0.15]})
        adjusted = apply_monotonic_prob_calibration(df)

        self.assertGreater(adjusted["pred_hr_prob"].iloc[0], 0.16)

    def test_build_devigged_probs_from_raw_books_normalizes_vig(self):
        raw = {"A": {"draftkings": -110, "fanduel": -120, "betmgm": 100}}
        adjusted = rdp._build_devigged_probs_from_raw_books(raw)
        self.assertIn("A", adjusted)
        self.assertGreaterEqual(adjusted["A"], 0.30)
        self.assertLessEqual(adjusted["A"], 0.35)

    def test_validate_model_dataflow_does_not_flag_missing_live_df_when_not_ready(self):
        train_df = pd.DataFrame({"batter": [1], "pitcher": [2], "is_hr": [0]})
        issues = validate_model_dataflow(train_df, None, required_features=["bat_pa_count"])
        self.assertFalse(any("Live dataframe is None" in issue for issue in issues))

    def test_prepare_discord_rankings_preserves_model_reliability(self):
        live_df = pd.DataFrame(
            {
                "batter_name": ["A"],
                "pitcher_name": ["B"],
                "pred_hr_prob": [0.15],
                "edge_pct": [5.0],
                "kelly_fraction": [0.1],
                "ev_percent": [2.0],
                "game_time": ["7:00 PM"],
                "model_reliability": ["HIGH"],
            }
        )

        rankings = _prepare_discord_rankings(live_df)

        self.assertIn("model_reliability", rankings.columns)
        self.assertEqual(rankings["model_reliability"].iloc[0], "HIGH")

    def test_apply_expert_signal_boosts_increases_prob_for_favorable_matchup(self):
        live_df = pd.DataFrame(
            {
                "has_platoon_advantage": [1],
                "platoon_advantage_multiplier": [1.18],
                "bat_barrel_rate": [0.16],
                "bat_hard_hit_rate": [0.46],
                "bat_avg_exit_velocity": [95.0],
                "pitch_hr_per_9": [2.1],
                "pitch_hr_allowed_rate": [0.06],
                "park_factor": [110.0],
                "temp": [85.0],
                "wind_out_component": [8.0],
            }
        )

        boosted = rdp.apply_expert_signal_boosts(live_df, [0.08])

        self.assertGreater(boosted[0], 0.08)

    def test_prepare_discord_rankings_creates_physics_delta_when_missing(self):
        live_df = pd.DataFrame(
            {
                "batter_name": ["A"],
                "pitcher_name": ["B"],
                "pred_hr_prob": [0.18],
                "edge_pct": [5.0],
                "kelly_fraction": [0.1],
                "ev_percent": [2.0],
                "game_time": ["7:00 PM"],
                "base_model_prob": [0.10],
            }
        )

        rankings = _prepare_discord_rankings(live_df)

        self.assertIn("physics_delta", rankings.columns)
        self.assertAlmostEqual(rankings["physics_delta"].iloc[0], 0.08)

    def test_prepare_discord_rankings_builds_portfolio_action_score(self):
        live_df = pd.DataFrame(
            {
                "batter_name": ["A", "B"],
                "pitcher_name": ["X", "Y"],
                "pred_hr_prob": [0.18, 0.22],
                "edge_pct": [5.0, 12.0],
                "kelly_fraction": [0.02, 0.08],
                "ev_percent": [2.0, 8.0],
                "game_time": ["7:00 PM", "8:00 PM"],
                "specific_day_upside_score": [0.2, 0.7],
                "market_prob": [0.16, 0.18],
            }
        )

        rankings = _prepare_discord_rankings(live_df)

        self.assertIn("portfolio_action_score", rankings.columns)
        self.assertGreater(rankings["portfolio_action_score"].iloc[1], rankings["portfolio_action_score"].iloc[0])

    def test_ensure_discord_radar_columns_adds_prob_and_physics_defaults(self):
        radar = pd.DataFrame({"batter_name": ["A"]})

        ensured = rdp._ensure_discord_radar_columns(radar)

        self.assertIn("hr_probability", ensured.columns)
        self.assertIn("physics_delta", ensured.columns)
        self.assertEqual(ensured["hr_probability"].iloc[0], 0.0)

    def test_coerce_numeric_column_returns_default_series_when_missing(self):
        radar = pd.DataFrame({"batter_name": ["A"]})

        series = rdp._coerce_numeric_column(radar, "physics_delta", default=0.0)

        self.assertEqual(series.iloc[0], 0.0)

    def test_finalize_discord_radar_frame_adds_required_columns(self):
        radar = pd.DataFrame({"batter_name": ["A"]})

        finalized = rdp._finalize_discord_radar_frame(radar)

        self.assertIn("hr_probability", finalized.columns)
        self.assertIn("physics_delta", finalized.columns)
        self.assertIn("physics_delta_abs", finalized.columns)

    def test_select_thresholded_candidates_returns_empty_when_no_rows_meet_threshold(self):
        pool = pd.DataFrame({"batter_name": ["A", "B"], "hr_probability": [0.02, 0.03]})

        selected = rdp._select_thresholded_candidates(pool, min_prob=0.05, max_rows=5)

        self.assertTrue(selected.empty)

    def test_build_discord_snapshot_summary_reports_no_qualifying_picks(self):
        lines = rdp._build_discord_snapshot_summary(
            "2026-07-29",
            pd.DataFrame({"batter_name": ["A"]}),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            0.06,
            2,
            6,
        )

        self.assertTrue(any("No qualifying picks met" in line for line in lines))

    def test_send_morning_learning_summary_uses_repo_data_dir_for_reports(self):
        with TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            report_path = data_dir / "hr_learning_report_2026-07-29.json"
            report_path.write_text(
                '{"total_hrs_analyzed": 4, "accurate_predictions": 0, "missed_predictions": 4, "key_findings": ["test finding"]}',
                encoding="utf-8",
            )
            eval_path = data_dir / "evaluation_2026-07-28.csv"
            eval_path.write_text("brier_error\n0.12\n", encoding="utf-8")

            with patch.object(rdp, "_repo_data_dir", return_value=data_dir):
                with patch.object(rdp, "send_discord_webhook", return_value=True) as mock_send:
                    result = rdp.send_morning_learning_summary(missed_count=4)

            self.assertTrue(result)
            content = mock_send.call_args.kwargs["content"]
            self.assertIn("Yesterday reviewed: 4 HRs | Predicted: 0 | Missed: 4", content)
            self.assertIn("Evaluation: 1 predictions scored", content)
            self.assertIn("test finding", content)

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

    def test_build_learning_insights_from_evaluation_uses_verified_hr_rows(self):
        eval_df = pd.DataFrame(
            {
                "batter_name": ["A", "B"],
                "pitcher_name": ["X", "Y"],
                "pred_hr_prob": [0.02, 0.22],
                "actual_hr": [1, 1],
            }
        )

        insights = build_learning_insights_from_evaluation(eval_df)

        self.assertEqual(insights["total_hrs_analyzed"], 2)
        self.assertEqual(insights["missed_predictions"], 1)
        self.assertEqual(insights["accurate_predictions"], 1)


if __name__ == "__main__":
    unittest.main()
