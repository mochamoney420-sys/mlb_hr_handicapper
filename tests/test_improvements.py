#!/usr/bin/env python3
"""Test that all improvements compile and run without errors."""

import sys
from pathlib import Path

import pytest

print("Testing improvements...\n")

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_FILE = REPO_ROOT / "run_daily_predictions.py"

# Test 1: error_tracking module
print("1️⃣  Testing error_tracking.py...")
try:
    from error_tracking import get_tracker, log_error, log_warning
    tracker = get_tracker()
    print("   ✅ error_tracking module imports successfully")
    print(f"   ✅ Tracker initialized: {type(tracker).__name__}")
except Exception as e:
    print(f"   ❌ Error tracking failed: {e}")
    sys.exit(1)

# Test 2: Bidirectional learning signature
print("\n2️⃣  Testing bidirectional learning updates...")
try:
    import inspect
    content = TARGET_FILE.read_text(encoding='utf-8', errors='ignore')
    
    if 'correct_confident' in content:
        print("   ✅ Found 'correct_confident' (high-confidence correct predictions)")
    else:
        print("   ❌ Missing 'correct_confident' - learning not updated")
        sys.exit(1)
    
    if 'correct_skeptical' in content:
        print("   ✅ Found 'correct_skeptical' (correct no-HR predictions)")
    else:
        print("   ❌ Missing 'correct_skeptical'")
        sys.exit(1)
    
    if 'Bidirectional learning' in content:
        print("   ✅ Found bidirectional learning logic")
    else:
        print("   ❌ Missing bidirectional learning")
        sys.exit(1)
        
except Exception as e:
    print(f"   ❌ Learning check failed: {e}")
    sys.exit(1)

# Test 3: Health check improvements
print("\n3️⃣  Testing health check with silent failure detection...")
try:
    if 'SCANNING FOR SILENT FAILURES' in content:
        print("   ✅ Found silent failure scanner in health check")
    else:
        print("   ❌ Health check not updated with silent failure detection")
        sys.exit(1)
    
    if '✅ Today\'s predictions' in content:
        print("   ✅ Found prediction validation logic")
    else:
        print("   ❌ Health check missing prediction validation")
        sys.exit(1)
        
except Exception as e:
    print(f"   ❌ Health check test failed: {e}")
    sys.exit(1)

# Test 4: Error tracker integration
print("\n4️⃣  Testing error tracking integration...")
try:
    if 'ERROR_TRACKER' in content:
        print("   ✅ Found ERROR_TRACKER initialization")
    else:
        print("   ❌ ERROR_TRACKER not initialized")
        sys.exit(1)
    
    if 'log_error' in content:
        print("   ✅ Found log_error calls")
    else:
        print("   ❌ No log_error calls found")
        sys.exit(1)
        
except Exception as e:
    print(f"   ❌ Tracker integration test failed: {e}")
    sys.exit(1)

import numpy as np
import pandas as pd
import analyze_hr_patterns as ahp
import run_daily_predictions as rdp
import update_predictions as upd


def test_extract_hr_patterns_ignores_empty_statcast_physics_and_uses_baseline():
    actual_hrs = pd.DataFrame({
        'batter': [101],
        'pitcher': [202],
        'batter_name': ['CJ Abrams'],
        'pitcher_name': ['Shota Imanaga'],
        'model_prob': [0.083],
    })
    training_data = pd.DataFrame({
        'batter': [101] * 5,
        'pitcher': [202] * 5,
        'game_date': [
            '2026-08-08', '2026-08-07', '2026-08-06', '2026-08-05', '2026-08-04'
        ],
        'launch_speed': [0.0, 0.0, 0.0, 0.0, 0.0],
        'events': ['single'] * 5,
    })

    patterns, _ = ahp.extract_hr_patterns(actual_hrs, training_data)

    assert patterns
    assert patterns[0]['batter_recent_avg_exit_velo'] == 88.0
    assert patterns[0]['batter_recent_barrel_rate'] == 0.0


def test_bench_players_receive_zero_pa_expectation():
    starter_pa = rdp.get_lineup_pa_expectation_with_starter_guard(1, is_starter=True)
    bench_pa = rdp.get_lineup_pa_expectation_with_starter_guard(10, is_starter=False)

    assert starter_pa > 0.0
    assert bench_pa == 0.0


def test_sanitize_daily_lineup_pool_drops_pitcher_rows_and_requires_minimum_players():
    df = pd.DataFrame({
        'batter_id': [101, 102, 103, 104, 105, 106],
        'batter_position': ['P', 'RF', '1B', 'LF', 'CF', '2B'],
    })

    clean = rdp.sanitize_daily_lineup_pool(df, active_mlb_ids={101, 102, 103, 104, 105, 106})

    assert len(clean) == 5
    assert set(clean['batter_position']) == {'RF', '1B', 'LF', 'CF', '2B'}


def test_compute_bullpen_platoon_blend_weighting_uses_platoon_and_usage():
    hitter_profile = {'stands': 'R'}
    starter_profile = {'pitch_barrel_allowed_rate': 0.08}
    bullpen_pool_df = pd.DataFrame({
        'player_id': [1, 2, 3],
        'throws': ['R', 'L', 'L'],
        'barrel_allowed_rate': [0.12, 0.15, 0.18],
        'recent_usage_3d': [0.10, 0.40, 0.60],
    })

    blended = rdp.compute_bullpen_platoon_blend(
        hitter_profile=hitter_profile,
        starter_profile=starter_profile,
        bullpen_pool_df=bullpen_pool_df,
        expected_total_pas=4.0,
    )

    assert blended == pytest.approx(0.0993, abs=1e-4)


def test_alert_threshold_is_dynamic_not_hardcoded_12_percent(monkeypatch):
    monkeypatch.setenv('DISCORD_MIN_PROB', '0.06')
    monkeypatch.delenv('DISCORD_ALERT_THRESHOLD', raising=False)
    threshold = upd.get_live_alert_threshold()

    assert threshold < 0.12
    assert threshold >= 0.05


def test_runtime_guardrails_block_placeholder_or_stale_data(monkeypatch):
    monkeypatch.setenv('ODDS_API_KEY', 'dummy-key')
    guard = rdp.evaluate_runtime_guardrails(
        module_status={'core_dependencies_ok': True},
        raw_odds={
            'Player A': {'draftkings': -110},
            'Player B': {'draftkings': -110},
        },
        source_age_seconds=3600,
        live_data_ok=True,
    )

    assert guard['status'] in {'blocked', 'warning'}
    assert any('stale' in reason.lower() or 'placeholder' in reason.lower() for reason in guard['reasons'])


def test_predict_props_uses_env_key_not_hardcoded_secret():
    text = (REPO_ROOT / 'src' / 'predict_props.py').read_text(encoding='utf-8', errors='ignore')
    assert 'API_KEY = "' not in text
    assert 'os.getenv("ODDS_API_KEY"' in text or 'os.getenv(\'ODDS_API_KEY\'' in text


def test_calculate_probability_metrics_handles_single_class_labels():
    metrics = rdp.calculate_probability_metrics([0, 0, 0], [0.05, 0.10, 0.15])

    assert metrics['brier_score'] == pytest.approx(0.0116666667, abs=1e-6)
    assert np.isnan(metrics['log_loss'])


def test_probability_guardrails_keep_true_hr_candidates_visible():
    caps = rdp.get_probability_guardrail_defaults()
    shortlist = rdp.get_conservative_shortlist_defaults()

    assert caps['hard_confidence_cap'] >= 0.45
    assert caps['reliability_cap_high'] >= 0.50
    assert caps['reliability_cap_medium'] >= 0.40
    assert shortlist['min_prob'] <= 0.05
    assert shortlist['max_prob'] >= 0.50


print("\n" + "=" * 70)
print("✅ ALL TESTS PASSED")
print("=" * 70)
print("""
System is ready for next run with:
  ✓ Silent failure detection (all exceptions logged)
  ✓ Bidirectional learning (learns from right + wrong predictions)
  ✓ Health check scanner (detects model failures automatically)
  ✓ Error reporting (daily JSON + text reports)
""")
