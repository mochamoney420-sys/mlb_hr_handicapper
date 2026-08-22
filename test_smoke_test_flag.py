import importlib.util
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parent / 'run_daily_predictions.py'
    spec = importlib.util.spec_from_file_location('run_daily_predictions', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_pipeline_arguments_supports_smoke_test_flag():
    module = load_module()
    args = module.parse_pipeline_arguments(['--smoke-test'])

    assert args.smoke_test is True


def test_initialize_missing_feature_guardrails_fills_live_schema():
    module = load_module()
    df = module.pd.DataFrame({'batter': [1], 'pitcher': [2]})

    guarded = module.initialize_missing_feature_guardrails(df)

    required = [
        'platoon_advantage_multiplier', 'weather_extremes_multiplier',
        'ballpark_park_factor', 'catcher_liability_multiplier',
        'umpire_strike_to_ball_ratio', 'umpire_called_strike_percentage',
        'umpire_runs_created_per_game', 'weather_hr_impact_score',
        'weather_hr_penalty_score', 'ppci_dominance_score',
        'dynamic_matchup_grade', 'pitch_arsenal_matchup_score',
        'micro_weather_score', 'lineup_slot_pressure_score',
        'lineup_grab_window_score', 'split_advantage_score',
        'flyball_pitcher_target_score', 'hot_streak_contact_score',
        'arsenal_vulnerability_score', 'game_total_context_score',
        'breaking_pitch_vulnerability', 'left_on_right_fade_score',
        'reverse_split_anomaly_score', 'porch_advantage_bonus',
        'death_valley_penalty', 'would_be_hr_differential',
        'bullpen_quality_score_home', 'bullpen_quality_score_away',
        'opp_bullpen_xfip_degradation', 'umpire_strike_zone_impact',
        'density_altitude_factor', 'sportsbook_value_score',
        'pitcher_fear_factor', 'is_elite_power_batter'
    ]

    for column in required:
        assert column in guarded.columns
        assert guarded[column].notna().all()

    assert guarded['row_source_valid'].all()


def test_execute_live_scoring_validation_checks_initializes_missing_feature_stubs():
    module = load_module()
    df = module.pd.DataFrame({'batter': [1], 'pitcher': [2]})

    validated = module.execute_live_scoring_validation_checks(df)

    for column in [
        'arsenal_vulnerability_score', 'ballpark_park_factor', 'death_valley_penalty',
        'dynamic_matchup_grade', 'pitch_arsenal_matchup_score', 'platoon_advantage_multiplier',
        'ppci_dominance_score', 'sportsbook_value_score', 'umpire_strike_to_ball_ratio',
        'weather_extremes_multiplier', 'would_be_hr_differential'
    ]:
        assert column in validated.columns
        assert validated[column].notna().all()

    assert validated['row_source_valid'].all()


def test_run_targeted_live_inference_builds_probability_outputs():
    module = load_module()
    df = module.pd.DataFrame({
        'batter': [1, 2],
        'pitcher': [10, 11],
        'batter_name': ['A', 'B'],
        'pitcher_name': ['X', 'Y'],
        'projected_pas': [4.1, 4.1],
    })

    scored = module.run_targeted_live_inference(df)

    assert 'pred_hr_prob' in scored.columns
    assert 'production_prob' in scored.columns
    assert scored['pred_hr_prob'].notna().all()
    assert scored['pred_hr_prob'].between(0.0, 1.0).all()
