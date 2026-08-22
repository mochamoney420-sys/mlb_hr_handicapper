import pandas as pd

from run_daily_predictions import build_cluster_matchup_probabilities, execute_calibrated_probability_pipeline


def test_build_cluster_matchup_probabilities():
    live = pd.DataFrame([
        {
            'batter': 1001,
            'pitcher': 2001,
            'stand': 'R',
            'p_throws': 'R',
            'projected_pas': 4.1,
            'park_factor': 100.0,
            'temp': 82.0,
            'wind_out_component': 12.0,
        }
    ])

    cluster_df = pd.DataFrame([
        {'batter_id': 1001, 'pitcher_id': 2001, 'batter_hand': 'R', 'pitcher_hand': 'R', 'pa': 600, 'hr': 14},
        {'batter_id': 1001, 'pitcher_id': 2001, 'batter_hand': 'R', 'pitcher_hand': 'L', 'pa': 500, 'hr': 13},
        {'batter_id': 1001, 'pitcher_id': 2001, 'batter_hand': 'L', 'pitcher_hand': 'R', 'pa': 550, 'hr': 11},
        {'batter_id': 1001, 'pitcher_id': 2001, 'batter_hand': 'L', 'pitcher_hand': 'L', 'pa': 520, 'hr': 10},
    ])

    out = build_cluster_matchup_probabilities(live, cluster_df)

    assert 'cluster_platoon_prob' in out.columns
    assert out.loc[0, 'cluster_platoon_prob'] > 0.0


def test_execute_calibrated_probability_pipeline_guardrails():
    mock_live = pd.DataFrame([{
        'batter': 1001,
        'hitter_id': 1001,
        'pitcher': 2001,
        'pitcher_throws': 'R',
        'pitcher_hr_9': 2.20,
        'ballpark_hr_factor_3yr': 1.28,
        'engine_environmental_scalar': 1.18,
        'lineup_batting_position': 1,
        'projected_pas': 4.5,
    }])

    processed = execute_calibrated_probability_pipeline(mock_live)
    final_p = float(processed['production_prob'].iloc[0])

    assert 0.03 < final_p < 0.12
    assert processed['pred_hr_prob'].iloc[0] == processed['production_prob'].iloc[0]
