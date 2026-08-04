import pandas as pd
import run_daily_predictions as rdp

statcast_df = pd.DataFrame([
    {"batter": 10, "pitcher": 1, "estimated_woba_using_speedangle": 0.70, "launch_speed": 100, "events": "home_run", "game_date": "2026-08-01"},
    {"batter": 10, "pitcher": 1, "estimated_woba_using_speedangle": 0.60, "launch_speed": 95, "events": "single", "game_date": "2026-08-02"},
    {"batter": 11, "pitcher": 2, "estimated_woba_using_speedangle": 0.20, "launch_speed": 70, "events": "grounded_out", "game_date": "2026-08-02"},
    {"batter": 11, "pitcher": 2, "estimated_woba_using_speedangle": 0.15, "launch_speed": 75, "events": "grounded_out", "game_date": "2026-08-03"},
])
rows_df = pd.DataFrame([{"batter": 10, "pitcher": 1}, {"batter": 11, "pitcher": 2}])
print(rdp.build_recent_batter_woba_proxy(statcast_df, rows_df, lookback_days=30))
print(rdp.build_recent_pitcher_damage_proxy(statcast_df, rows_df, lookback_days=30))
