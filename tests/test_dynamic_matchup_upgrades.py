import math

import pandas as pd
import numpy as np
import pytest

import run_daily_predictions as rdp


def test_explicit_three_layer_baseline_physics_environment_is_bounded():
    live = pd.DataFrame({
        'batter': [101, 102],
        'bat_hr_rate': [0.042, 0.051],
        'bat_pa_count': [240, 310],
        'pitcher_avg_vaa_4seam': [-5.5, -3.8],
        'pitcher_induced_vertical_break': [16.5, 18.0],
        'hitter_avg_attack_angle': [12.0, 14.0],
        'pitcher_true_spin_efficiency_pct': [56.0, 52.0],
        'game_weather_temperature_f': [92.0, 76.0],
        'ballpark_elevation_ft': [5000.0, 120.0],
        'ballpark_hr_factor_3yr': [1.22, 1.08],
        'umpire_called_strike_percentage': [63.2, 66.1],
    })

    baseline = rdp.compute_baseline_hr_rate('101', live, tracking_db=None)
    physics = rdp.evaluate_ball_flight_physics(live)
    environment = rdp.calculate_environmental_scalar(live)

    assert len(baseline) == len(live)
    assert baseline.min() >= 0.0
    assert physics.min() >= 0.65
    assert physics.max() <= 1.35
    assert environment.min() >= 0.70
    assert environment.max() <= 1.40


def test_explicit_three_layer_probability_engine_handles_missing_inputs():
    live = pd.DataFrame(index=[0, 1, 2])

    baseline = rdp.compute_baseline_hr_rate('missing-hitter', live, tracking_db=None)
    physics = rdp.evaluate_ball_flight_physics(live)
    environment = rdp.calculate_environmental_scalar(live)

    assert len(baseline) == 3
    assert np.isfinite(physics).all()
    assert np.isfinite(environment).all()
    assert physics.min() >= 0.65
    assert environment.min() >= 0.70


def test_compute_micro_atmo_carry_adjustment_uses_real_density():
    out = rdp.compute_micro_atmo_carry_adjustment(
        pressure_mbar=1013.25,
        humidity_pct=45,
        temp_f=72,
        wind_speed_mph=12,
        wind_dir_deg=0,
        stadium_cf_bearing=5,
        roof_sealed=0,
    )

    assert set([
        'air_density_kg_m3',
        'drag_multiplier',
        'effective_wind_out_mph',
        'carry_adjustment_ft',
    ]).issubset(out)
    assert 1.0 < out['air_density_kg_m3'] < 1.3
    assert abs(out['effective_wind_out_mph']) > 0
    assert isinstance(out['carry_adjustment_ft'], float)


def test_compute_spray_geometry_overlap_sets_personalized_multiplier():
    history = pd.DataFrame([
        {
            'bb_type': 'fly_ball',
            'launch_speed': 102,
            'launch_angle': 25,
            'hc_x': 175,
            'hc_y': 145,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 108,
            'launch_angle': 30,
            'hc_x': 160,
            'hc_y': 160,
        },
        {
            'bb_type': 'line_drive',
            'launch_speed': 104,
            'launch_angle': 18,
            'hc_x': 90,
            'hc_y': 170,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 100,
            'launch_angle': 21,
            'hc_x': 145,
            'hc_y': 160,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 101,
            'launch_angle': 22,
            'hc_x': 170,
            'hc_y': 160,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 103,
            'launch_angle': 24,
            'hc_x': 120,
            'hc_y': 165,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 101,
            'launch_angle': 23,
            'hc_x': 118,
            'hc_y': 165,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 100,
            'launch_angle': 20,
            'hc_x': 125,
            'hc_y': 175,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 107,
            'launch_angle': 28,
            'hc_x': 135,
            'hc_y': 150,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 106,
            'launch_angle': 26,
            'hc_x': 130,
            'hc_y': 155,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 106,
            'launch_angle': 27,
            'hc_x': 80,
            'hc_y': 170,
        },
        {
            'bb_type': 'line_drive',
            'launch_speed': 100,
            'launch_angle': 20,
            'hc_x': 105,
            'hc_y': 175,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 99,
            'launch_angle': 24,
            'hc_x': 180,
            'hc_y': 150,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 110,
            'launch_angle': 30,
            'hc_x': 200,
            'hc_y': 130,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 105,
            'launch_angle': 26,
            'hc_x': 195,
            'hc_y': 145,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 102,
            'launch_angle': 25,
            'hc_x': 110,
            'hc_y': 170,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 101,
            'launch_angle': 24,
            'hc_x': 112,
            'hc_y': 168,
        },
        {
            'bb_type': 'fly_ball',
            'launch_speed': 104,
            'launch_angle': 26,
            'hc_x': 150,
            'hc_y': 165,
        },
    ])

    out = rdp.compute_spray_geometry_overlap(history, 'NYY')
    assert 0.8 <= out <= 1.35


def test_compute_pitch_shape_matchup_edge_tracks_pitch_specific_shape():
    pitcher = pd.DataFrame([
        {
            'pitch_type': 'FF',
            'release_speed': 97.0,
            'pfx_x': 5.0,
            'pfx_z': 9.0,
        },
        {
            'pitch_type': 'FF',
            'release_speed': 98.0,
            'pfx_x': 4.0,
            'pfx_z': 8.0,
        },
        {
            'pitch_type': 'FF',
            'release_speed': 96.0,
            'pfx_x': 3.5,
            'pfx_z': 8.5,
        },
        {
            'pitch_type': 'SL',
            'release_speed': 84.0,
            'pfx_x': -9.0,
            'pfx_z': 6.0,
        },
        {
            'pitch_type': 'SL',
            'release_speed': 83.0,
            'pfx_x': -8.5,
            'pfx_z': 5.5,
        },
    ])

    batter = pd.DataFrame([
        {
            'pitch_type': 'FF',
            'release_speed': 96.0,
            'pfx_x': 5.0,
            'pfx_z': 8.5,
            'launch_speed': 101.0,
            'launch_angle': 25.0,
        },
        {
            'pitch_type': 'FF',
            'release_speed': 97.0,
            'pfx_x': 3.8,
            'pfx_z': 8.2,
            'launch_speed': 102.0,
            'launch_angle': 28.0,
        },
        {
            'pitch_type': 'FF',
            'release_speed': 98.0,
            'pfx_x': 4.5,
            'pfx_z': 9.5,
            'launch_speed': 100.0,
            'launch_angle': 26.0,
        },
        {
            'pitch_type': 'SL',
            'release_speed': 84.0,
            'pfx_x': -9.0,
            'pfx_z': 5.8,
            'launch_speed': 95.0,
            'launch_angle': 22.0,
        },
        {
            'pitch_type': 'SL',
            'release_speed': 83.5,
            'pfx_x': -8.2,
            'pfx_z': 6.2,
            'launch_speed': 97.0,
            'launch_angle': 20.0,
        },
    ])

    edge = rdp.compute_pitch_shape_matchup_edge(pitcher, batter)
    assert isinstance(edge, float)
    assert -1.0 <= edge <= 1.0


def test_resolve_probability_mode_and_weight_enforces_minimum_physics_blend(monkeypatch):
    monkeypatch.delenv('HR_PHYSICS_BLEND_WEIGHT', raising=False)
    monkeypatch.setenv('HR_PROB_MODE', 'blended')
    monkeypatch.setattr(rdp, 'calibrate_physics_blend_weight', lambda *args, **kwargs: 0.0)

    mode, weight = rdp.resolve_probability_mode_and_weight()

    assert mode == 'blended'
    assert weight >= 0.20
    assert weight == 0.20


def test_resolve_probability_mode_and_weight_honors_active_blend_override_even_in_base_mode(monkeypatch):
    monkeypatch.setenv('HR_PHYSICS_BLEND_WEIGHT', '0.25')
    monkeypatch.setenv('HR_PROB_MODE', 'base')
    monkeypatch.setattr(rdp, 'calibrate_physics_blend_weight', lambda *args, **kwargs: 0.0)

    mode, weight = rdp.resolve_probability_mode_and_weight()

    assert mode == 'blended'
    assert weight == pytest.approx(0.25)


def test_dampen_feedback_training_weights_uses_logarithmic_scaling():
    weights = pd.Series([1.0, 2.0, 5.0, 9.0], dtype=float)

    damped = rdp.dampen_feedback_training_weights(weights)

    assert damped.iloc[0] == pytest.approx(1.0, rel=1e-6)
    assert damped.iloc[1] == pytest.approx(1.0 + np.log(2.0), rel=1e-6)
    assert damped.iloc[2] < 5.0
    assert damped.iloc[3] < 9.0


def test_true_hr_signal_not_clipped_below_realistic_ceiling():
    live = pd.DataFrame({
        'model_reliability': ['HIGH', 'HIGH', 'MEDIUM', 'LOW'],
        'pred_hr_prob': [0.70, 0.62, 0.45, 0.28],
    })

    rel_upper = live['model_reliability'].astype(str).str.upper()
    rel_cap_high = 0.45
    rel_cap_medium = 0.34
    rel_cap_low = 0.24
    rel_cap = np.where(
        rel_upper == 'HIGH', rel_cap_high,
        np.where(rel_upper == 'MEDIUM', rel_cap_medium, rel_cap_low)
    )
    live['reliability_prob_cap'] = rel_cap
    live['pred_hr_prob'] = np.minimum(
        pd.to_numeric(live['pred_hr_prob'], errors='coerce').fillna(0.0), rel_cap
    )
    hard_conf_cap = 0.40
    live['pred_hr_prob'] = np.minimum(
        pd.to_numeric(live['pred_hr_prob'], errors='coerce').fillna(0.0), hard_conf_cap
    )

    assert live['pred_hr_prob'].iloc[0] >= 0.35
    assert live['pred_hr_prob'].iloc[1] >= 0.35
    assert live['pred_hr_prob'].max() >= 0.40
