#!/usr/bin/env python
"""
Automatic HR Pattern Learning: Analyze yesterday's actual home runs,
extract why they happened, and feed insights into today's model training.

Runs daily before model retraining to create an intelligent feedback loop.
"""

import os
import sys

# Fix UTF-8 encoding on Windows
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except:
        pass
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import json

# Load env
env_file = Path(__file__).parent / '.vscode' / '.env'
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, val = line.split('=', 1)
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            os.environ.setdefault(key.strip(), val.strip())

# =====================================================================
# HR PATTERN ANALYSIS ENGINE
# =====================================================================

def load_yesterdays_home_runs():
    """Load actual home runs from yesterday's live monitoring."""
    yesterday = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    feedback_file = Path('data') / f'live_feedback_{yesterday}.csv'
    
    if not feedback_file.exists():
        print(f"ℹ️  No home run feedback file found: {feedback_file}")
        return pd.DataFrame()
    
    try:
        hrs = pd.read_csv(feedback_file)
        print(f"📊 Found {len(hrs)} home runs from {yesterday}")
        return hrs
    except Exception as e:
        print(f"⚠️  Error loading feedback: {e}")
        return pd.DataFrame()

def load_training_data_for_analysis(days_back=60):
    """Load historical Statcast to analyze HR conditions."""
    all_days = []
    today = datetime.today()
    
    for i in range(1, days_back + 1):
        target_date = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        cache_file = Path('cache') / f'statcast_{target_date}.csv'
        
        if cache_file.exists():
            try:
                day_df = pd.read_csv(cache_file)
                all_days.append(day_df)
            except:
                continue
    
    if not all_days:
        return pd.DataFrame()
    
    df = pd.concat(all_days, ignore_index=True)
    return df

def extract_hr_patterns(actual_hrs, training_data):
    """Analyze the conditions under which yesterday's HRs occurred."""
    
    if actual_hrs.empty or training_data.empty:
        return [], {}
    
    patterns = []
    summary_stats = {}
    
    for idx, hr_row in actual_hrs.iterrows():
        batter_id = pd.to_numeric(hr_row.get('batter', None), errors='coerce')
        pitcher_id = pd.to_numeric(hr_row.get('pitcher', None), errors='coerce')
        batter_name = hr_row.get('batter_name', 'Unknown')
        pitcher_name = hr_row.get('pitcher_name', 'Unknown')
        
        if pd.isna(batter_id) or pd.isna(pitcher_id):
            continue
        
        # Find matching at-bats in training data
        matching_pas = training_data[
            (training_data['batter'] == batter_id) & 
            (training_data['pitcher'] == pitcher_id)
        ]
        
        if matching_pas.empty:
            # If no exact matchup in history, look at general player stats
            batter_pas = training_data[training_data['batter'] == batter_id]
            pitcher_pas = training_data[training_data['pitcher'] == pitcher_id]
            
            if batter_pas.empty or pitcher_pas.empty:
                continue
        else:
            batter_pas = training_data[training_data['batter'] == batter_id]
            pitcher_pas = training_data[training_data['pitcher'] == pitcher_id]
        
        # Extract key features that led to this HR
        hr_features = {
            'batter_name': batter_name,
            'pitcher_name': pitcher_name,
            'batter_id': int(batter_id),
            'pitcher_id': int(pitcher_id),
            'hr_date': hr_row.get('game_date', datetime.today().strftime('%Y-%m-%d')),
            'model_prob': float(hr_row.get('model_prob', 0)),
        }
        
        # Batter's recent form
        if not batter_pas.empty:
            batter_pas_sorted = batter_pas.sort_values('game_date', ascending=False).head(20)
            hr_features['batter_recent_avg_exit_velo'] = float(
                batter_pas_sorted['launch_speed'].dropna().mean() 
                if not batter_pas_sorted['launch_speed'].dropna().empty else 0
            )
            hr_features['batter_recent_barrel_rate'] = float(
                (batter_pas_sorted['launch_speed'] >= 98).mean()
            )
            hr_features['batter_hr_rate_recent'] = float(
                (batter_pas_sorted['events'] == 'home_run').mean()
            )
            hr_features['batter_pa_count_recent'] = int(len(batter_pas_sorted))
        
        # Pitcher's vulnerability
        if not pitcher_pas.empty:
            pitcher_pas_sorted = pitcher_pas.sort_values('game_date', ascending=False).head(20)
            hr_features['pitcher_recent_hr_allowed_rate'] = float(
                (pitcher_pas_sorted['events'] == 'home_run').mean()
            )
            hr_features['pitcher_recent_avg_exit_velo_allowed'] = float(
                pitcher_pas_sorted['launch_speed'].dropna().mean() 
                if not pitcher_pas_sorted['launch_speed'].dropna().empty else 0
            )
            hr_features['pitcher_pa_count_recent'] = int(len(pitcher_pas_sorted))
        
        # Game conditions (if available)
        hr_features['weather_temp'] = float(hr_row.get('temp', 71))
        hr_features['weather_wind_speed'] = float(hr_row.get('wind_speed', 5))
        hr_features['park_factor'] = float(hr_row.get('park_factor', 100))
        
        patterns.append(hr_features)
        
        # Aggregate statistics
        summary_stats[batter_name] = summary_stats.get(batter_name, 0) + 1
    
    return patterns, summary_stats

def generate_learning_insights(patterns, summary_stats):
    """Create a learning report from yesterday's HR patterns."""
    
    if not patterns:
        return None
    
    insights = {
        'analysis_date': datetime.today().strftime('%Y-%m-%d'),
        'total_hrs_analyzed': len(patterns),
        'unique_batters': len(summary_stats),
        'missed_predictions': 0,
        'accurate_predictions': 0,
        'patterns': [],
        'key_findings': []
    }
    
    # Analyze each pattern
    for pattern in patterns:
        model_prob = pattern.get('model_prob', 0)
        
        # Categorize prediction accuracy
        if model_prob < 0.10:
            insights['missed_predictions'] += 1
            pattern['prediction_category'] = 'MISSED (low prob)'
        elif model_prob < 0.20:
            insights['missed_predictions'] += 1
            pattern['prediction_category'] = 'MISSED (medium prob)'
        else:
            insights['accurate_predictions'] += 1
            pattern['prediction_category'] = 'PREDICTED'
        
        insights['patterns'].append(pattern)
    
    # Extract key findings
    batter_hot = sorted(summary_stats.items(), key=lambda x: x[1], reverse=True)[:3]
    insights['key_findings'].append(f"🔥 Hottest batters: {', '.join([f'{name} ({count} HRs)' for name, count in batter_hot])}")
    
    missed = sum(1 for p in patterns if p['prediction_category'].startswith('MISSED'))
    insights['key_findings'].append(f"⚠️  Model missed {missed}/{len(patterns)} HRs (need to upweight these batters)")
    
    avg_exit_velo = np.mean([p.get('batter_recent_avg_exit_velo', 0) for p in patterns if p.get('batter_recent_avg_exit_velo')])
    if avg_exit_velo > 90:
        insights['key_findings'].append(f"💨 Yesterday's HR batters averaged {avg_exit_velo:.1f} mph exit velo (elite level)")
    
    avg_barrel_rate = np.mean([p.get('batter_recent_barrel_rate', 0) for p in patterns if 'batter_recent_barrel_rate' in p])
    if avg_barrel_rate > 0.15:
        insights['key_findings'].append(f"📍 Barrel rate was high ({avg_barrel_rate:.1%}) - quality of contact matters")
    
    return insights

def apply_insights_to_feedback_weights(patterns):
    """Convert HR insights into feedback weight adjustments for model retraining."""
    
    feedback_boost = {}
    
    for pattern in patterns:
        batter_id = pattern.get('batter_id')
        pitcher_id = pattern.get('pitcher_id')
        
        if not batter_id or not pitcher_id:
            continue
        
        # Create unique key for this matchup
        key = f"{int(batter_id)}_{int(pitcher_id)}"
        
        # Missed HRs get heavy boost (model learns to be more bullish)
        if pattern.get('prediction_category', '').startswith('MISSED'):
            feedback_boost[key] = 3.0  # Triple weight for missed HRs
        else:
            feedback_boost[key] = 1.5  # Slight boost for predicted ones (reinforce signal)
    
    return feedback_boost

def save_learning_report(insights):
    """Persist learning insights to file for monitoring."""
    
    if not insights:
        return
    
    today = insights['analysis_date']
    report_file = Path('data') / f'hr_learning_report_{today}.json'
    
    # Convert patterns to serializable format
    insights_copy = insights.copy()
    insights_copy['patterns'] = [
        {k: float(v) if isinstance(v, (np.floating, np.integer)) else v 
         for k, v in p.items()}
        for p in insights_copy['patterns']
    ]
    
    report_file.write_text(json.dumps(insights_copy, indent=2))
    
    return report_file

def print_learning_report(insights):
    """Display human-readable learning insights."""
    
    if not insights:
        print("ℹ️  No home runs to analyze today")
        return
    
    print("\n" + "="*70)
    print("📚 YESTERDAY'S HOME RUN LEARNING ANALYSIS")
    print("="*70)
    
    print(f"\nAnalysis Date: {insights['analysis_date']}")
    print(f"Total HRs Analyzed: {insights['total_hrs_analyzed']}")
    print(f"Unique Batters: {insights['unique_batters']}")
    print(f"Missed by Model: {insights['missed_predictions']}")
    print(f"Predicted Correctly: {insights['accurate_predictions']}")
    
    print("\n🔍 KEY FINDINGS:")
    for finding in insights['key_findings']:
        print(f"   {finding}")
    
    print("\n📊 DETAILED PATTERNS:")
    for i, pattern in enumerate(insights['patterns'], 1):
        print(f"\n   [{i}] {pattern.get('batter_name', 'Unknown')} vs {pattern.get('pitcher_name', 'Unknown')}")
        print(f"       Category: {pattern.get('prediction_category', '?')}")
        print(f"       Model Prob: {pattern.get('model_prob', 0):.1%}")
        print(f"       Exit Velo: {pattern.get('batter_recent_avg_exit_velo', 0):.1f} mph")
        print(f"       Barrel Rate: {pattern.get('batter_recent_barrel_rate', 0):.1%}")
        if pattern.get('pitcher_recent_hr_allowed_rate'):
            print(f"       Pitcher HR Rate: {pattern.get('pitcher_recent_hr_allowed_rate', 0):.1%}")
    
    print("\n" + "="*70)

# =====================================================================
# MAIN ENTRY POINT
# =====================================================================

def analyze_yesterdays_hrs_and_learn():
    """Complete daily HR pattern learning pipeline."""
    
    print("\n🧠 AUTOMATIC HOME RUN PATTERN LEARNING")
    print("="*70)
    
    # Step 1: Load yesterday's actual HRs
    actual_hrs = load_yesterdays_home_runs()
    
    if actual_hrs.empty:
        print("ℹ️  No home runs to learn from today")
        return {}
    
    # Step 2: Load training data for analysis
    print("\n📥 Loading 60 days of Statcast data for pattern analysis...")
    training_data = load_training_data_for_analysis()
    
    if training_data.empty:
        print("⚠️  Could not load training data")
        return {}
    
    # Step 3: Extract patterns
    print("🔍 Analyzing HR conditions and patterns...")
    patterns, summary_stats = extract_hr_patterns(actual_hrs, training_data)
    
    if not patterns:
        print("⚠️  Could not extract patterns")
        return {}
    
    # Step 4: Generate insights
    print("💡 Generating learning insights...")
    insights = generate_learning_insights(patterns, summary_stats)
    
    # Step 5: Print report
    print_learning_report(insights)
    
    # Step 6: Save report
    report_file = save_learning_report(insights)
    if report_file:
        print(f"\n✅ Learning report saved: {report_file}")
    
    # Step 7: Extract feedback boost for model training
    print("\n🚀 Extracting feedback weights for model retraining...")
    feedback_boost = apply_insights_to_feedback_weights(patterns)
    print(f"   • {len(feedback_boost)} matchups flagged for weight adjustment")
    
    return {
        'insights': insights,
        'feedback_boost': feedback_boost,
        'patterns': patterns
    }

if __name__ == '__main__':
    result = analyze_yesterdays_hrs_and_learn()
    sys.exit(0 if result else 1)
