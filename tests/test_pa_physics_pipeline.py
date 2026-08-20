import numpy as np
import pandas as pd

import run_daily_predictions as rdp
from src.pa_physics_pipeline import (
    compute_catcher_liability_score,
    compute_climate_micro_movement_multiplier,
    compute_pitch_physics_pressure,
    compute_umpire_strike_zone_bias,
)


def test_pitch_physics_pressure_rewards_steep_vaa_and_movement():
    pitch_df = pd.DataFrame(
        [{
            "vaa": -8.0,
            "release_spin_rate": 2600,
            "pfx_x": -2.0,
            "pfx_z": 7.0,
            "release_extension": 6.0,
        }]
    )

    result = compute_pitch_physics_pressure(pitch_df)

    assert result["pitch_physics_pressure_score"] > 0.6
    assert result["pitch_vaa_pressure_score"] > 0.8
    assert result["pitch_ssw_proxy"] > 0.0


def test_umpire_tight_zone_bias_increases_hitter_advantage():
    bias = compute_umpire_strike_zone_bias("Lee", called_strike_rate=0.28)

    assert bias["umpire_tight_zone_score"] > 0.5
    assert bias["umpire_zone_bias_multiplier"] > 1.0


def test_catcher_liability_score_rises_with_poor_framing_and_blocking():
    liability = compute_catcher_liability_score(called_strike_rate=0.24, passed_balls=4, stolen_base_attempts=5)

    assert liability["catcher_liability_score"] > 0.4
    assert liability["catcher_liability_multiplier"] > 1.0


def test_climate_micro_movement_multiplier_is_boosted_by_heat_and_humidity():
    env = {"temperature_f": 85.0, "humidity_pct": 70.0, "altitude_ft": 2000.0}

    multiplier = compute_climate_micro_movement_multiplier(env)

    assert multiplier > 1.0


def test_apply_expert_signal_boosts_uses_context_multipliers():
    live_df = pd.DataFrame([
        {
            "pitch_physics_pressure_score": 0.90,
            "umpire_zone_bias_multiplier": 1.16,
            "catcher_liability_multiplier": 1.18,
            "climate_micro_movement_multiplier": 1.12,
        }
    ])

    boosted = rdp.apply_expert_signal_boosts(live_df, np.array([0.10]))

    assert boosted[0] > 0.10
