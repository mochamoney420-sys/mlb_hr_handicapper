import pandas as pd
from datetime import datetime

from run_daily_predictions import _ensure_handedness_columns, _resolve_historical_season_window


def test_ensure_handedness_columns_fills_from_stand_and_p_throws():
    df = pd.DataFrame({
        'stand': ['L', 'R', None],
        'p_throws': ['R', 'L', 'R'],
    })

    out = _ensure_handedness_columns(df)

    assert out['batter_hand'].tolist() == ['L', 'R', 'R']
    assert out['pitcher_hand'].tolist() == ['R', 'L', 'R']


def test_resolve_historical_season_window_defaults_to_current_year():
    start_year, end_year = _resolve_historical_season_window(today=datetime(2026, 8, 10))

    assert end_year == 2026
    assert start_year <= end_year
