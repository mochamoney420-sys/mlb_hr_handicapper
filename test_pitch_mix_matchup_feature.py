import pandas as pd

import run_daily_predictions as rdp


def test_pitch_mix_matchup_feature_uses_batter_pitch_type_damage():
    statcast_df = pd.DataFrame(
        [
            {"batter": 10, "pitcher": 1, "pitch_type": "FF", "launch_speed": 100, "events": "home_run"},
            {"batter": 10, "pitcher": 1, "pitch_type": "FF", "launch_speed": 100, "events": "single"},
            {"batter": 10, "pitcher": 1, "pitch_type": "SL", "launch_speed": 80, "events": "grounded_out"},
            {"batter": 11, "pitcher": 1, "pitch_type": "FF", "launch_speed": 80, "events": "grounded_out"},
            {"batter": 11, "pitcher": 1, "pitch_type": "FF", "launch_speed": 80, "events": "grounded_out"},
            {"batter": 11, "pitcher": 1, "pitch_type": "SL", "launch_speed": 100, "events": "home_run"},
        ]
    )
    rows_df = pd.DataFrame(
        [
            {"batter": 10, "pitcher": 1},
            {"batter": 11, "pitcher": 1},
        ]
    )

    feature = rdp.build_batter_pitch_mix_matchup_feature(
        statcast_df,
        rows_df,
        lookback_days=30,
        min_sample=1,
        usage_floor=0.05,
        damage_threshold=0.20,
    )

    assert feature.iloc[0] > feature.iloc[1]
    assert feature.iloc[0] > 0.5
    assert feature.iloc[1] < 0.5


def test_recent_batter_woba_and_pitcher_damage_proxies_are_signalized():
    statcast_df = pd.DataFrame(
        [
            {"batter": 10, "pitcher": 1, "estimated_woba_using_speedangle": 0.70, "launch_speed": 100, "events": "home_run", "game_date": "2026-08-01"},
            {"batter": 10, "pitcher": 1, "estimated_woba_using_speedangle": 0.60, "launch_speed": 95, "events": "single", "game_date": "2026-08-02"},
            {"batter": 11, "pitcher": 2, "estimated_woba_using_speedangle": 0.20, "launch_speed": 70, "events": "grounded_out", "game_date": "2026-08-02"},
            {"batter": 11, "pitcher": 2, "estimated_woba_using_speedangle": 0.15, "launch_speed": 75, "events": "grounded_out", "game_date": "2026-08-03"},
        ]
    )
    rows_df = pd.DataFrame([
        {"batter": 10, "pitcher": 1},
        {"batter": 11, "pitcher": 2},
    ])

    woba_feature = rdp.build_recent_batter_woba_proxy(statcast_df, rows_df, lookback_days=30)
    damage_feature = rdp.build_recent_pitcher_damage_proxy(statcast_df, rows_df, lookback_days=30)

    assert woba_feature.iloc[0] > woba_feature.iloc[1]
    assert damage_feature.iloc[0] > damage_feature.iloc[1]
