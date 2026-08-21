import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_daily_predictions import (
    _compute_isolated_wager_metrics,
    _reconcile_physics_delta,
    _sanitize_active_starters,
)


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
