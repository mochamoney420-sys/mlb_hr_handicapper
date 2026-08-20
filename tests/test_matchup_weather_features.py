import pandas as pd

from run_daily_predictions import build_matchup_weather_features


def test_build_matchup_weather_features_adds_expected_columns():
    df = pd.DataFrame([
        {
            'bat_barrel_rate': 0.12,
            'bat_avg_exit_velocity': 95.0,
            'bat_pull_rate': 0.45,
            'bat_avg_launch_angle': 18.0,
            'bat_hr_fb_rate': 0.18,
            'bat_iso_proxy': 0.13,
            'pitch_avg_velocity': 90.0,
            'pitch_days_since_last_start': 6.0,
            'pitch_hr_allowed_rate': 0.06,
            'pitch_hr_fb_allowed_rate': 0.16,
            'pitch_fb_allowed_rate': 0.42,
            'temp': 84.0,
            'wind_out_component': 12.0,
            'pressure': 1008.0,
            'humidity': 40.0,
            'batting_order_slot': 4,
            'line_release_window_flag': 1,
        }
    ])

    out = build_matchup_weather_features(df)

    for col in [
        'ppci_dominance_score',
        'dynamic_matchup_grade',
        'pitch_arsenal_matchup_score',
        'micro_weather_score',
        'lineup_slot_pressure_score',
        'lineup_grab_window_score',
    ]:
        assert col in out.columns, f'missing {col}'

    assert out.loc[0, 'ppci_dominance_score'] > 0
    assert out.loc[0, 'micro_weather_score'] > 0
