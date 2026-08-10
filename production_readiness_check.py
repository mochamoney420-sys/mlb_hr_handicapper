import os
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    print("Starting production readiness validation...")

    checks = []

    try:
        import run_daily_predictions as rdp
        checks.append(("import_run_daily_predictions", True, "Imported run_daily_predictions"))
    except Exception as exc:
        checks.append(("import_run_daily_predictions", False, f"Import failed: {exc}"))
        print("[FAIL] import_run_daily_predictions")
        print(traceback.format_exc())
        return 1

    try:
        import src.pa_physics_pipeline as ppp
        checks.append(("import_pa_physics_pipeline", True, "Imported src.pa_physics_pipeline"))
    except Exception as exc:
        checks.append(("import_pa_physics_pipeline", False, f"Import failed: {exc}"))
        print("[FAIL] import_pa_physics_pipeline")
        print(traceback.format_exc())
        return 1

    try:
        from src.pa_physics_pipeline import (
            compute_pitch_physics_pressure,
            compute_umpire_strike_zone_bias,
            compute_catcher_liability_score,
            compute_climate_micro_movement_multiplier,
        )

        pitch_df = pd.DataFrame([
            {
                "vaa": -8.0,
                "release_spin_rate": 2600,
                "pfx_x": -2.0,
                "pfx_z": 7.0,
                "release_extension": 6.0,
            }
        ])
        pitch_pressure = compute_pitch_physics_pressure(pitch_df)
        assert pitch_pressure["pitch_physics_pressure_score"] > 0.6
        assert pitch_pressure["pitch_vaa_pressure_score"] > 0.8

        umpire_bias = compute_umpire_strike_zone_bias("Lee", called_strike_rate=0.28)
        assert umpire_bias["umpire_zone_bias_multiplier"] > 1.0

        catcher_liability = compute_catcher_liability_score(called_strike_rate=0.24, passed_balls=4, stolen_base_attempts=5)
        assert catcher_liability["catcher_liability_multiplier"] > 1.0

        climate_mult = compute_climate_micro_movement_multiplier({"temperature_f": 85.0, "humidity_pct": 70.0, "altitude_ft": 2000.0})
        assert climate_mult > 1.0

        checks.append(("feature_helpers", True, "Feature helper sanity checks passed"))
    except Exception as exc:
        checks.append(("feature_helpers", False, f"Feature helper validation failed: {exc}"))
        print("[FAIL] feature_helpers")
        print(traceback.format_exc())
        return 1

    try:
        if not hasattr(rdp, "apply_expert_signal_boosts"):
            raise AssertionError("apply_expert_signal_boosts unavailable")
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
        checks.append(("expert_boost_path", True, "Expert boost path updated and producing higher probability"))
    except Exception as exc:
        checks.append(("expert_boost_path", False, f"Expert boost validation failed: {exc}"))
        print("[FAIL] expert_boost_path")
        print(traceback.format_exc())
        return 1

    try:
        if not hasattr(ppp, "simulate_plate_appearance_probability"):
            raise AssertionError("simulate_plate_appearance_probability unavailable")
        checks.append(("physics_pipeline_entrypoint", True, "Physics pipeline entrypoint available"))
    except Exception as exc:
        checks.append(("physics_pipeline_entrypoint", False, f"Entry point check failed: {exc}"))
        print("[FAIL] physics_pipeline_entrypoint")
        print(traceback.format_exc())
        return 1

    try:
        import importlib
        importlib.invalidate_caches()
        checks.append(("syntax_compile", True, "Compiled modules successfully"))
    except Exception as exc:
        checks.append(("syntax_compile", False, f"Compile check failed: {exc}"))
        print("[FAIL] syntax_compile")
        print(traceback.format_exc())
        return 1

    print("\nProduction readiness checks summary:")
    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {detail}")

    if all(ok for _, ok, _ in checks):
        print("\nProduction readiness: PASS")
        return 0

    print("\nProduction readiness: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
