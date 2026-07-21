import os
from pathlib import Path
import pandas as pd
import numpy as np
from pybaseball import statcast


def download_statcast_data(start_date="2025-04-01", end_date="2026-10-01", cache_dir="cache"):
    os.makedirs(cache_dir, exist_ok=True)
    filename = Path(cache_dir) / f"statcast_{start_date}_{end_date}.csv"
    if filename.exists():
        return pd.read_csv(filename)

    df = statcast(start_dt=start_date, end_dt=end_date)
    df.to_csv(filename, index=False)
    return df


def _normalize_series(series):
    return pd.to_numeric(series, errors='coerce').fillna(0)


def add_batted_ball_flags(df):
    df = df.copy()
    df['launch_speed'] = _normalize_series(df.get('launch_speed'))
    df['launch_angle'] = _normalize_series(df.get('launch_angle'))
    df['is_hr'] = (df.get('events') == 'home_run').astype(int)
    df['is_barrel'] = (
        (df['launch_speed'] >= 98)
        & (df['launch_angle'] >= 8)
        & (df['launch_angle'] <= 40)
    ).astype(int)
    df['is_hard_hit'] = (df['launch_speed'] >= 95).astype(int)
    df['is_sweet_spot'] = (
        (df['launch_speed'] >= 90)
        & (df['launch_angle'].between(18, 32))
    ).astype(int)
    df['is_fly'] = df.get('bb_type', '').fillna('').str.lower() == 'fly_ball'
    df['is_ground'] = df.get('bb_type', '').fillna('').str.lower() == 'ground_ball'
    df['is_in_play'] = df['events'].notna().astype(int)
    df['estimated_woba'] = _normalize_series(df.get('estimated_woba_using_speedangle'))
    df['estimated_ba'] = _normalize_series(df.get('estimated_ba_using_speedangle'))
    return df


def add_rolling_metrics(df):
    df = df.copy()
    for window in [15, 30]:
        df[f'bat_barrel_{window}pa'] = df.groupby('batter')['is_barrel'].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )
        df[f'bat_hard_hit_{window}pa'] = df.groupby('batter')['is_hard_hit'].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )
        df[f'bat_sweet_spot_{window}pa'] = df.groupby('batter')['is_sweet_spot'].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )
        df[f'pit_hr_{window}pa'] = df.groupby('pitcher')['is_hr'].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )
        df[f'pit_barrel_allowed_{window}pa'] = df.groupby('pitcher')['is_barrel'].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )
        df[f'pit_hard_hit_allowed_{window}pa'] = df.groupby('pitcher')['is_hard_hit'].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )
    return df


def build_hr_feature_vectors(df):
    df = add_batted_ball_flags(df)
    df = add_rolling_metrics(df)

    batter_agg = df.groupby('batter').agg(
        bat_pa_count=('is_in_play', 'sum'),
        bat_hr_rate=('is_hr', 'mean'),
        bat_barrel_rate=('is_barrel', 'mean'),
        bat_hard_hit_rate=('is_hard_hit', 'mean'),
        bat_sweet_spot_rate=('is_sweet_spot', 'mean'),
        bat_15pa_barrel_rate=('bat_barrel_15pa', 'last'),
        bat_15pa_hard_hit_rate=('bat_hard_hit_15pa', 'last'),
        bat_15pa_sweet_spot_rate=('bat_sweet_spot_15pa', 'last'),
        bat_30pa_barrel_rate=('bat_barrel_30pa', 'last'),
        bat_30pa_hard_hit_rate=('bat_hard_hit_30pa', 'last'),
        bat_30pa_sweet_spot_rate=('bat_sweet_spot_30pa', 'last'),
        bat_avg_woba=('estimated_woba', 'mean'),
        bat_avg_ba=('estimated_ba', 'mean'),
    ).reset_index()

    pitcher_agg = df.groupby('pitcher').agg(
        pitch_pa_count=('is_in_play', 'sum'),
        pitch_hr_allowed_rate=('is_hr', 'mean'),
        pitch_barrel_allowed_rate=('is_barrel', 'mean'),
        pitch_hard_hit_allowed_rate=('is_hard_hit', 'mean'),
        pitch_15pa_hr_rate=('pit_hr_15pa', 'last'),
        pitch_15pa_barrel_allowed_rate=('pit_barrel_allowed_15pa', 'last'),
        pitch_15pa_hard_hit_allowed_rate=('pit_hard_hit_allowed_15pa', 'last'),
        pitch_30pa_hr_rate=('pit_hr_30pa', 'last'),
        pitch_30pa_barrel_allowed_rate=('pit_barrel_allowed_30pa', 'last'),
        pitch_30pa_hard_hit_allowed_rate=('pit_hard_hit_allowed_30pa', 'last'),
    ).reset_index()

    return batter_agg, pitcher_agg, df


def build_lineup_status(boxscore):
    lineup_rows = []
    for team_side in ['home', 'away']:
        side = boxscore.get(team_side, {})
        if not side:
            continue
        batting_order = side.get('battingOrder') or []
        players = side.get('players', {})
        for idx, pid in enumerate(batting_order, start=1):
            player = players.get(f'ID{pid}', {})
            lineup_rows.append({
                'team_side': team_side,
                'batting_order': idx,
                'player_id': pid,
                'player_name': player.get('person', {}).get('fullName', 'Unknown'),
                'status': player.get('status', 'Active'),
                'position': player.get('position', {}).get('abbreviation', ''),
            })
    return pd.DataFrame(lineup_rows)
