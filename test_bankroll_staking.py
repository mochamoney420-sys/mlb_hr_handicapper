import sys

sys.path.insert(0, '.')

from run_daily_predictions import _estimate_bet_stake_usd


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
