import pandas as pd


def test_load_frozen_pregame_predictions_uses_saved_predictions(tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    pred_path = data_dir / 'predictions_2026-08-20.csv'
    pd.DataFrame(
        [
            {
                'game_pk': 1,
                'batter': 101,
                'pitcher': 202,
                'batter_name': 'Jordan Walker',
                'pitcher_name': 'Bryce Elder',
                'pred_hr_prob': 0.12,
                'model_prob': 0.12,
            }
        ]
    ).to_csv(pred_path, index=False)

    monkeypatch.chdir(tmp_path)
    from analyze_hr_patterns import load_frozen_pregame_predictions

    out = load_frozen_pregame_predictions('2026-08-20')

    assert not out.empty
    assert out['pred_hr_prob'].iloc[0] == 0.12
    assert 'frozen_pregame_prediction' in out.columns
