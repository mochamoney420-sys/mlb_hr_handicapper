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
            {"batter": 10, "pitcher": 1, "estimated_woba_using_speedangle": 0.70, "launch_speed": 100, "launch_angle": 30, "events": "home_run", "game_date": "2026-08-01"},
            {"batter": 10, "pitcher": 1, "estimated_woba_using_speedangle": 0.60, "launch_speed": 95, "launch_angle": 22, "events": "single", "game_date": "2026-08-02"},
            {"batter": 11, "pitcher": 2, "estimated_woba_using_speedangle": 0.20, "launch_speed": 70, "launch_angle": 8, "events": "grounded_out", "game_date": "2026-08-02"},
            {"batter": 11, "pitcher": 2, "estimated_woba_using_speedangle": 0.15, "launch_speed": 75, "launch_angle": 10, "events": "grounded_out", "game_date": "2026-08-03"},
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


def test_recent_pitcher_damage_proxy_handles_missing_launch_metrics_without_na_cast_crash():
    statcast_df = pd.DataFrame(
        [
            {"batter": 10, "pitcher": 1, "launch_speed": 101.0, "launch_angle": 31.0, "events": "home_run", "game_date": "2026-08-01"},
            {"batter": 10, "pitcher": 1, "launch_speed": None, "launch_angle": None, "events": "single", "game_date": "2026-08-02"},
            {"batter": 11, "pitcher": 2, "launch_speed": 72.0, "launch_angle": 9.0, "events": "grounded_out", "game_date": "2026-08-02"},
            {"batter": 11, "pitcher": 2, "launch_speed": 94.0, "launch_angle": 14.0, "events": "home_run", "game_date": "2026-08-03"},
        ]
    )
    rows_df = pd.DataFrame([
        {"batter": 10, "pitcher": 1},
        {"batter": 11, "pitcher": 2},
    ])

    damage_feature = rdp.build_recent_pitcher_damage_proxy(statcast_df, rows_df, lookback_days=30)

    assert damage_feature.notna().all()
    assert len(damage_feature) == 2
    assert damage_feature.iloc[0] >= 0.0
    assert damage_feature.iloc[1] >= 0.0
