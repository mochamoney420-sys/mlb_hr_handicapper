import pandas as pd
import run_daily_predictions as rdp


def test_print_conservative_bet_ready_wagers_falls_back_when_strict_filters_empty(tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    predictions = pd.DataFrame({
        'batter_name': ['A'],
        'pitcher_name': ['B'],
        'pred_hr_prob': [0.05],
        'ev_percent': [1.2],
        'edge_pct': [2.0],
        'kelly_fraction': [0.008],
        'model_reliability': ['MEDIUM'],
        'game_time': ['10:00 AM'],
    })
    predictions.to_csv(data_dir / 'predictions_2026-08-04.csv', index=False)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CONSERVATIVE_MIN_PROB', '0.07')
    monkeypatch.setenv('CONSERVATIVE_MIN_EV_PCT', '2.5')
    monkeypatch.setenv('CONSERVATIVE_MIN_EDGE_PCT', '4.0')
    monkeypatch.setenv('CONSERVATIVE_MIN_KELLY', '0.01')

    shortlist = rdp.print_conservative_bet_ready_wagers('2026-08-04', top_n=5)

    assert not shortlist.empty
    assert shortlist.iloc[0]['batter_name'] == 'A'


def test_prepare_discord_rankings_adds_missing_ev_column():
    frame = pd.DataFrame({'batter_name': ['A'], 'pitcher_name': ['B'], 'pred_hr_prob': [0.12]})
    ranked = rdp._prepare_discord_rankings(frame)

    assert 'ev_pct' in ranked.columns
    assert ranked['ev_pct'].iloc[0] == 0.0


def test_prepare_discord_rankings_does_not_create_duplicate_sort_keys():
    frame = pd.DataFrame({
        'batter_name': ['A'],
        'pitcher_name': ['B'],
        'pred_hr_prob': [0.12],
        'ev_percent': [8.0],
        'kelly_fraction': [0.03],
        'model_reliability': ['MEDIUM'],
    })
    ranked = rdp._prepare_discord_rankings(frame)

    assert ranked.columns.is_unique
    ranked_sorted = ranked.sort_values(
        by=['portfolio_action_score', 'hr_probability', 'ev_pct', 'kelly_fraction'],
        ascending=[False, False, False, False],
    )
    assert len(ranked_sorted) == 1


def test_build_portfolio_action_score_handles_empty_radar():
    radar = pd.DataFrame(columns=['batter_name', 'pitcher_name'])
    radar = rdp._finalize_discord_radar_frame(radar)
    score = rdp._build_portfolio_action_score(radar)

    assert score.empty


def test_prepare_discord_rankings_adds_slate_z_score_and_relative_rank():
    frame = pd.DataFrame({
        'batter_name': ['A', 'B', 'C', 'D', 'E', 'F'],
        'pitcher_name': ['P1', 'P2', 'P3', 'P4', 'P5', 'P6'],
        'pred_hr_prob': [0.01, 0.02, 0.02, 0.03, 0.04, 0.12],
        'ev_percent': [0.0, 0.0, 0.0, 0.0, 0.0, 10.5],
        'kelly_fraction': [0.0, 0.0, 0.0, 0.0, 0.0, 0.06],
        'model_reliability': ['MEDIUM'] * 6,
    })
    ranked = rdp._prepare_discord_rankings(frame)

    assert 'slate_z_score' in ranked.columns
    assert 'relative_slate_rank' in ranked.columns
    assert ranked['slate_z_score'].iloc[-1] > 2.0
    assert ranked['relative_slate_rank'].iloc[-1] > 0.80


def test_dynamic_discord_gate_allows_high_slate_anomaly_below_static_min_prob():
    frame = pd.DataFrame({
        'batter_name': ['A', 'B', 'C'],
        'pitcher_name': ['P1', 'P2', 'P3'],
        'pred_hr_prob': [0.018, 0.024, 0.086],
        'market_prob': [0.045, 0.051, 0.078],
        'ev_percent': [0.0, 0.0, 8.7],
        'kelly_fraction': [0.01, 0.015, 0.04],
        'model_reliability': ['MEDIUM', 'MEDIUM', 'HIGH'],
    })
    ranked = rdp._prepare_discord_rankings(frame)
    dynamic_gate = (
        (ranked['hr_probability'] >= 0.04)
        | (ranked['slate_z_score'] >= 2.0)
    )

    assert ranked.loc[dynamic_gate, 'batter_name'].tolist() == ['C']
