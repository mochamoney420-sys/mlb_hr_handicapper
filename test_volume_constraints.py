import pandas as pd

from run_daily_predictions import apply_daily_hr_volume_constraints


def test_apply_daily_hr_volume_constraints_preserves_top_end_probabilities():
    frame = pd.DataFrame({
        'pred_hr_prob': [0.02, 0.015, 0.01, 0.005],
        'game_pk': [1, 1, 2, 2],
    })

    result = apply_daily_hr_volume_constraints(frame, game_count=1, avg_hr_per_game=2.3)

    assert result['pred_hr_prob'].max() >= 0.015
    assert result['pred_hr_prob'].iloc[0] >= result['pred_hr_prob'].iloc[1]
    assert result['pred_hr_prob'].max() <= 0.35
