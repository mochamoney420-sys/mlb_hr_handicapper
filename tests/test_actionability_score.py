import sys

import pandas as pd

sys.path.insert(0, '.')

from run_daily_predictions import _estimate_actionability_score


def test_actionability_score_prefers_quality_signals():
    row = {
        'pred_hr_prob': 0.17,
        'ev_percent': 12.0,
        'edge_pct': 8.0,
        'kelly_fraction': 0.04,
        'model_reliability': 'HIGH',
        'batter_pitch_mix_matchup_score': 0.8,
        'recent_batter_woba_proxy': 0.75,
        'recent_pitcher_damage_proxy': 0.7,
    }

    score = _estimate_actionability_score(row)

    assert score > 20.0
