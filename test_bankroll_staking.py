import sys

import pandas as pd

sys.path.insert(0, '.')

from run_daily_predictions import _estimate_bet_stake_usd, _build_discord_top_keys


def test_estimate_bet_stake_usd_uses_bankroll_and_multiplier(monkeypatch):
    monkeypatch.setenv('BET_STAKE_BANKROLL_USD', '1000')
    monkeypatch.setenv('BET_STAKE_MIN_USD', '5')
    monkeypatch.setenv('BET_STAKE_MAX_USD', '50')
    monkeypatch.setenv('BET_STAKE_KELLY_MULTIPLIER', '0.25')

    stake = _estimate_bet_stake_usd(0.08)

    assert stake == 20.0


def test_estimate_bet_stake_usd_caps_at_max(monkeypatch):
    monkeypatch.setenv('BET_STAKE_BANKROLL_USD', '1000')
    monkeypatch.setenv('BET_STAKE_MIN_USD', '5')
    monkeypatch.setenv('BET_STAKE_MAX_USD', '50')
    monkeypatch.setenv('BET_STAKE_KELLY_MULTIPLIER', '0.25')

    stake = _estimate_bet_stake_usd(1.0)

    assert stake == 50.0


def test_build_discord_top_keys_handles_missing_columns():
    frame = pd.DataFrame({'hr_probability': [0.2]})

    assert _build_discord_top_keys(frame) == set()


def test_build_discord_top_keys_extracts_pairs():
    frame = pd.DataFrame({
        'batter_name': ['Alec Bohm'],
        'pitcher_name': ['Aaron Nola'],
    })

    assert _build_discord_top_keys(frame) == {('Alec Bohm', 'Aaron Nola')}
