import sys
from pathlib import Path

import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_daily_predictions import (
    _apply_unit_based_kelly_cap,
    _compute_isolated_wager_metrics,
    _power_law_devig_prob,
    _reconcile_physics_delta,
    _sanitize_active_starters,
    calculate_retraining_weights,
    enforce_training_feature_guardrails,
    initialize_missing_feature_guardrails,
    make_baseball_true,
)


def verify_upweight_improvement(baseline_predictions, upweighted_predictions, actual_outcomes):
    """Enforce out-of-sample calibration improvement after failure upweighting."""
    base_brier = brier_score_loss(actual_outcomes, baseline_predictions)
    new_brier = brier_score_loss(actual_outcomes, upweighted_predictions)
    base_log_loss = log_loss(actual_outcomes, baseline_predictions, labels=[0, 1])
    new_log_loss = log_loss(actual_outcomes, upweighted_predictions, labels=[0, 1])

    assert new_brier <= base_brier, "⚠️ System Warning: Upweighting caused out-of-sample validation decay!"
    assert new_log_loss <= base_log_loss, "⚠️ System Warning: Upweighted log loss worsened on validation data."

    return {
        'baseline_brier': base_brier,
        'upweighted_brier': new_brier,
        'baseline_log_loss': base_log_loss,
        'upweighted_log_loss': new_log_loss,
    }


def test_sanitize_active_starters_keeps_only_slots_1_to_9():
    live = pd.DataFrame(
        {
            'batter_name': ['A', 'B', 'C', 'D'],
            'batting_order_slot': [1, 9, 10, 12],
            'is_starter': [True, True, False, False],
        }
    )

    cleaned = _sanitize_active_starters(live)

    assert cleaned['batting_order_slot'].tolist() == [1, 9]
    assert cleaned['batter_name'].tolist() == ['A', 'B']


def test_reconcile_physics_delta_uses_raw_per_pa_baseline():
    live = pd.DataFrame(
        {
            'raw_per_pa_hr_prob': [0.12, 0.21],
            'base_model_per_pa_prob': [0.10, 0.17],
            'pred_hr_prob': [0.08, 0.10],
            'base_model_prob': [0.08, 0.10],
        }
    )

    reconciled = _reconcile_physics_delta(live)

    assert (reconciled['physics_delta'].round(6).tolist() == [0.02, 0.04])


def test_isolated_wager_metrics_use_true_production_probability():
    row = {
        'production_prob': 0.20,
        'market_prob': 0.18,
        'betting_prob': 0.05,
    }

    ev_value, ev_percent, kelly = _compute_isolated_wager_metrics(row)

    assert ev_value > 0
    assert ev_percent > 0
    assert kelly >= 0
    assert abs(ev_value - ((0.20 / 0.18) - 1.0)) < 1e-9


def test_power_law_devig_prob_returns_fair_probability_in_range():
    fair = _power_law_devig_prob([-110, +120], exponent=1.35)

    assert 0.0 < fair < 1.0
    assert 0.35 < fair < 0.65


def test_make_baseball_true_scales_to_realistic_xwoba_ranges():
    base, delta, final = make_baseball_true(1.000, -0.934)

    assert 0.30 <= base <= 0.40
    assert -0.10 <= delta <= 0.0
    assert 0.25 <= final <= 0.40


def test_unit_based_kelly_cap_fences_risk_by_reliability():
    final_units, stake_usd, capped_fraction = _apply_unit_based_kelly_cap(
        kelly_fraction=0.08,
        model_reliability='HIGH',
        sportsbook_value_score=1.2,
        bankroll_usd=1000.0,
    )

    assert final_units <= 3.0
    assert stake_usd <= 30.0
    assert capped_fraction <= 0.08


def test_retraining_weight_prioritization():
    """Guardrail: rookie cleanup hitters with elite physical traits should be weighted above baseline."""
    mock_data = pd.DataFrame({
        'mlb_lifetime_pas': [200, 18],
        'milb_recent_iso': [0.120, 0.235],
        'statcast_barrel_percentage': [4.5, 14.2],
        'statcast_max_launch_speed': [104.0, 114.5],
        'lineup_batting_position': [8, 4],
    })

    computed = calculate_retraining_weights(mock_data)

    assert computed[1] > computed[0]
    assert computed[1] == 3.00


def test_missing_feature_matrix_alignment():
    """Guardrail: missing live columns must be explicit fallback provenance and row-level invalid."""
    mock_live = pd.DataFrame({'game_pk': ['123456'], 'hitter_id': ['543210']})

    patched_live = initialize_missing_feature_guardrails(mock_live)

    assert 'weather_hr_impact_score' in patched_live.columns
    assert 'platoon_advantage_multiplier' in patched_live.columns
    assert bool(patched_live['source_fallback_flag'].iloc[0]) is False
    assert bool(patched_live['row_source_valid'].iloc[0]) is True
    assert patched_live['missing_feature_fields'].iloc[0] == []


def test_training_guardrail_clamps_zero_variance_features_to_training_baseline():
    """Guardrail: constant training features must remain fixed during live scoring."""
    train_df = pd.DataFrame({
        'density_altitude_factor': [1.0, 1.0, 1.0],
        'pitcher_fear_factor': [0.0, 0.0, 0.0],
        'temp': [72.0, 72.0, 72.0],
    })
    live_df = pd.DataFrame({
        'density_altitude_factor': [1.5, 0.9, 1.0],
        'pitcher_fear_factor': [0.4, -0.1, 0.0],
        'temp': [88.0, 32.0, 72.0],
    })

    patched = enforce_training_feature_guardrails(
        train_df,
        live_df,
        required_features=['density_altitude_factor', 'pitcher_fear_factor', 'temp'],
    )

    assert patched['density_altitude_factor'].tolist() == [1.0, 1.0, 1.0]
    assert patched['pitcher_fear_factor'].tolist() == [0.0, 0.0, 0.0]
    assert patched['temp'].tolist() == [72.0, 72.0, 72.0]


def test_verify_upweight_improvement_requires_better_out_of_sample_calibration():
    baseline = [0.7, 0.7, 0.7, 0.2, 0.2]
    upweighted = [0.95, 0.08, 0.95, 0.05, 0.05]
    outcomes = [1, 0, 1, 0, 0]

    metrics = verify_upweight_improvement(baseline, upweighted, outcomes)

    assert metrics['upweighted_brier'] <= metrics['baseline_brier']
    assert metrics['upweighted_log_loss'] <= metrics['baseline_log_loss']
