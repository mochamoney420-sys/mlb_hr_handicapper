import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, '.')

from run_daily_predictions import apply_expert_signal_boosts, apply_professional_filter_stack


class SpecificDayUpsideTests(unittest.TestCase):
    def test_upside_score_is_higher_for_professional_edge_matchup(self):
        df = pd.DataFrame([
            {
                'has_platoon_advantage': 0,
                'platoon_advantage_multiplier': 1.0,
                'bat_barrel_rate': 0.08,
                'bat_hard_hit_rate': 0.35,
                'bat_avg_exit_velocity': 88.0,
                'pitch_hr_per_9': 1.1,
                'pitch_hr_allowed_rate': 0.04,
                'park_factor': 100.0,
                'temp': 72.0,
                'wind_out_component': 0.0,
                'weather_extremes_multiplier': 1.0,
                'density_altitude_factor': 1.0,
                'porch_advantage_bonus': 1.0,
                'is_elite_power_batter': 0,
                'split_advantage_score': 0.0,
                'flyball_pitcher_target_score': 0.0,
                'hot_streak_contact_score': 0.0,
                'arsenal_vulnerability_score': 0.0,
                'game_total_context_score': 0.0,
            },
            {
                'has_platoon_advantage': 1,
                'platoon_advantage_multiplier': 1.25,
                'bat_barrel_rate': 0.15,
                'bat_hard_hit_rate': 0.50,
                'bat_avg_exit_velocity': 96.0,
                'pitch_hr_per_9': 1.7,
                'pitch_hr_allowed_rate': 0.06,
                'park_factor': 120.0,
                'temp': 87.0,
                'wind_out_component': 8.0,
                'weather_extremes_multiplier': 1.35,
                'density_altitude_factor': 1.25,
                'porch_advantage_bonus': 1.15,
                'is_elite_power_batter': 1,
                'split_advantage_score': 0.9,
                'flyball_pitcher_target_score': 0.95,
                'hot_streak_contact_score': 0.9,
                'arsenal_vulnerability_score': 0.85,
                'game_total_context_score': 0.9,
            },
        ])

        base_probs = np.array([0.10, 0.10])
        adjusted = apply_expert_signal_boosts(df, base_probs)

        self.assertGreater(adjusted[1], adjusted[0])
        self.assertGreater(df.loc[1, 'specific_day_upside_score'], df.loc[0, 'specific_day_upside_score'])
        self.assertGreater(df.loc[1, 'upside_boost_multiplier'], df.loc[0, 'upside_boost_multiplier'])

    def test_professional_filter_stack_orders_market_environment_matchup_and_power(self):
        df = pd.DataFrame([
            {
                'is_in_lineup': 0,
                'is_healthy': 1,
                'market_available': 1,
                'weather_ok': 1,
                'park_ok': 1,
                'has_platoon_advantage': 1,
                'pitch_arsenal_matchup_score': 0.90,
                'bat_barrel_rate': 0.15,
                'bat_hard_hit_rate': 0.50,
                'bat_avg_exit_velocity': 96.0,
                'is_elite_power_batter': 1,
                'best_market_odds_american': -120,
                'market_prob': 0.54,
                'ev_percent': 12.5,
            },
            {
                'is_in_lineup': 1,
                'is_healthy': 1,
                'market_available': 1,
                'weather_ok': 1,
                'park_ok': 1,
                'has_platoon_advantage': 1,
                'pitch_arsenal_matchup_score': 0.90,
                'bat_barrel_rate': 0.15,
                'bat_hard_hit_rate': 0.50,
                'bat_avg_exit_velocity': 96.0,
                'is_elite_power_batter': 1,
                'best_market_odds_american': -120,
                'market_prob': 0.54,
                'ev_percent': 12.5,
            },
        ])

        out = apply_professional_filter_stack(df)

        self.assertFalse(out.loc[0, 'professional_filter_pass'])
        self.assertEqual(out.loc[0, 'daily_filter_stage'], 'market')
        self.assertTrue(out.loc[1, 'professional_filter_pass'])
        self.assertEqual(out.loc[1, 'daily_filter_stage'], 'ready')


if __name__ == '__main__':
    unittest.main()
