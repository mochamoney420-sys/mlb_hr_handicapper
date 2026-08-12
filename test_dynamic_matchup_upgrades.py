import math

import pandas as pd
import numpy as np

import run_daily_predictions as rdp


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
