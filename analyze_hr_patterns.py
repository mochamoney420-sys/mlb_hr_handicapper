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

def _safe_name(value, default='Unknown'):
    if value is None or pd.isna(value):
        return default
    text = str(value).strip()
    return text if text else default


def _safe_float(value, default=0.0):
    val = pd.to_numeric(pd.Series([value]), errors='coerce').iloc[0]
    if pd.isna(val):
        return float(default)
    return float(val)


def _learning_hit_threshold(default=0.08):
    """Probability threshold used to classify a HR as predicted."""
    try:
        return float(np.clip(float(os.getenv('LEARNING_HR_HIT_THRESHOLD', str(default))), 0.01, 0.50))
    except Exception:
        return float(default)


def _build_hr_event_key(row):
    """Build a stable per-HR event key to avoid collapsing repeat HRs."""
    sv = row.get('sv_id')
    if sv is not None and not pd.isna(sv) and str(sv).strip():
        return str(sv).strip()

    game_pk = row.get('game_pk')
    batter = row.get('batter')
    pitcher = row.get('pitcher')
    inning = row.get('inning')
    half = row.get('inning_topbot')
    abn = row.get('at_bat_number')

    return f"{game_pk}:{batter}:{pitcher}:{inning}:{half}:{abn}:HR"


def _sanitize_pattern(pattern):
    clean = {}
    for key, value in pattern.items():
        if isinstance(value, (np.floating, np.integer)):
            value = float(value)
        if isinstance(value, float) and np.isnan(value):
            if key == 'model_prob':
                clean[key] = 0.0
            elif key in {'batter_name', 'pitcher_name'}:
                clean[key] = 'Unknown'
            else:
                clean[key] = None
            continue
        clean[key] = value

    clean['batter_name'] = _safe_name(clean.get('batter_name', 'Unknown'))
    clean['pitcher_name'] = _safe_name(clean.get('pitcher_name', 'Unknown'))
    clean['model_prob'] = _safe_float(clean.get('model_prob', 0.0), 0.0)
    return clean

def load_yesterdays_home_runs():
    """Load yesterday's complete home-run outcomes from Statcast and merge with any watcher feedback."""
    yesterday = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    feedback_file = Path('data') / f'live_feedback_{yesterday}.csv'
    evaluation_file = Path('data') / f'evaluation_{yesterday}.csv'
    statcast_file = Path('cache') / f'statcast_{yesterday}.csv'

    # Canonical source: yesterday evaluation file when available.
    # This keeps pattern-learning counts aligned with scoring metrics.
    if evaluation_file.exists():
        try:
            ev = pd.read_csv(evaluation_file)
            if not ev.empty and {'actual_hr', 'pred_hr_prob'}.issubset(ev.columns):
                ev = ev.copy()
                ev['actual_hr'] = pd.to_numeric(ev.get('actual_hr', 0), errors='coerce').fillna(0).astype(int)
                ev = ev[ev['actual_hr'] == 1].copy()
                if not ev.empty:
                    ev['date'] = yesterday
                    ev['model_prob'] = pd.to_numeric(ev.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0)
                    ev['batter_name'] = ev.get('batter_name', pd.Series(['Unknown'] * len(ev))).apply(_safe_name)
                    ev['pitcher_name'] = ev.get('pitcher_name', pd.Series(['Unknown'] * len(ev))).apply(_safe_name)
                    for col in ['game_pk', 'batter', 'pitcher']:
                        if col in ev.columns:
                            ev[col] = pd.to_numeric(ev[col], errors='coerce')
                    # Keep event-level HR rows whenever possible so multi-HR games are not collapsed.
                    event_dedupe_cols = [
                        c for c in [
                            'sv_id', 'play_id', 'event_id',
                            'game_pk', 'batter', 'pitcher', 'inning', 'inning_topbot', 'at_bat_number'
                        ]
                        if c in ev.columns
                    ]
                    if event_dedupe_cols:
                        ev = ev.sort_values(by=event_dedupe_cols).drop_duplicates(subset=event_dedupe_cols, keep='last')
                    ev['source'] = 'evaluation_canonical'
                    keep_cols = [c for c in ['date', 'game_pk', 'batter', 'pitcher', 'batter_name', 'pitcher_name', 'model_prob', 'source'] if c in ev.columns]
                    print(f"📊 Found {len(ev)} home runs from {yesterday} in evaluation")
                    return ev[keep_cols].reset_index(drop=True)
        except Exception as exc:
            print(f"⚠️  Error loading evaluation file: {exc}")

    hrs = pd.DataFrame()
    fb_enrichment = pd.DataFrame()

    if statcast_file.exists():
        try:
            statcast_df = pd.read_csv(statcast_file)
            if {'game_pk', 'batter', 'pitcher', 'events', 'game_date'}.issubset(statcast_df.columns):
                statcast_df = statcast_df.dropna(subset=['game_pk', 'batter', 'pitcher', 'events']).copy()
                statcast_df['is_hr'] = (statcast_df['events'] == 'home_run').astype(int)
                hr_rows = statcast_df[statcast_df['is_hr'] == 1].copy()
                if not hr_rows.empty:
                    hr_rows['event_key'] = hr_rows.apply(_build_hr_event_key, axis=1)
                    hr_rows = hr_rows.drop_duplicates(subset=['event_key'], keep='last')

                    if 'batter_name' not in hr_rows.columns:
                        if 'player_name' in hr_rows.columns:
                            hr_rows['batter_name'] = hr_rows['player_name']
                        else:
                            hr_rows['batter_name'] = np.nan
                    if 'pitcher_name' not in hr_rows.columns:
                        hr_rows['pitcher_name'] = np.nan

                hrs = hr_rows[['game_pk', 'batter', 'pitcher', 'batter_name', 'pitcher_name']].copy() if not hr_rows.empty else pd.DataFrame()
                if not hrs.empty:
                    hrs['actual_hr'] = 1
                    hrs['event_key'] = hr_rows['event_key'].values
                hrs['date'] = yesterday
                hrs['source'] = 'statcast'
                hrs['model_prob'] = np.nan
                print(f"📊 Found {len(hrs)} home runs from {yesterday} in Statcast")
        except Exception as exc:
            print(f"⚠️  Error loading Statcast file: {exc}")

    if feedback_file.exists():
        try:
            fb = pd.read_csv(feedback_file)
            if not fb.empty:
                fb = fb.copy()
                fb['date'] = fb.get('date', yesterday)
                fb['actual_hr'] = pd.to_numeric(fb.get('actual_hr', 1), errors='coerce').fillna(1).astype(int)
                fb['batter'] = pd.to_numeric(fb.get('batter', np.nan), errors='coerce')
                fb['pitcher'] = pd.to_numeric(fb.get('pitcher', np.nan), errors='coerce')
                fb['game_pk'] = pd.to_numeric(fb.get('game_pk', np.nan), errors='coerce')
                fb['batter_name'] = fb.get('batter_name', pd.Series(['Unknown'] * len(fb))).apply(_safe_name)
                fb['pitcher_name'] = fb.get('pitcher_name', pd.Series(['Unknown'] * len(fb))).apply(_safe_name)
                fb['model_prob'] = pd.to_numeric(fb.get('model_prob', np.nan), errors='coerce')

                fb_enrichment = fb[['game_pk', 'batter', 'pitcher', 'batter_name', 'pitcher_name', 'model_prob']].copy()
                fb_enrichment = fb_enrichment.dropna(subset=['game_pk', 'batter', 'pitcher'])
                fb_enrichment = fb_enrichment.sort_values(['game_pk', 'batter', 'pitcher']).drop_duplicates(
                    subset=['game_pk', 'batter', 'pitcher'],
                    keep='last'
                )

                if not hrs.empty:
                    fb_rows = fb[['date', 'batter_name', 'pitcher_name', 'batter', 'pitcher', 'game_pk', 'inning', 'model_prob', 'was_predicted', 'was_most_likely_homer', 'actual_hr']].dropna(subset=['batter_name', 'pitcher_name']).copy()
                    fb_rows['event_key'] = fb_rows.apply(
                        lambda r: f"{r.get('game_pk')}:{r.get('batter')}:{r.get('pitcher')}:{r.get('inning','')}:feedback",
                        axis=1
                    )
                    hrs = pd.concat([hrs, fb_rows], ignore_index=True)
                else:
                    hrs = fb[['date', 'batter_name', 'pitcher_name', 'batter', 'pitcher', 'game_pk', 'inning', 'model_prob', 'was_predicted', 'was_most_likely_homer', 'actual_hr']].dropna(subset=['batter_name', 'pitcher_name']).copy()
                    hrs['event_key'] = hrs.apply(
                        lambda r: f"{r.get('game_pk')}:{r.get('batter')}:{r.get('pitcher')}:{r.get('inning','')}:feedback",
                        axis=1
                    )
                print(f"🧾 Added {len(fb)} watcher feedback rows for {yesterday}")
        except Exception as exc:
            print(f"⚠️  Error loading feedback file: {exc}")

    if hrs.empty:
        print(f"ℹ️  No home run feedback file found: {feedback_file}")
        return pd.DataFrame()

    # Enrich Statcast-only rows with watcher names/probability where matchup keys overlap.
    if not fb_enrichment.empty and {'game_pk', 'batter', 'pitcher'}.issubset(hrs.columns):
        hrs = hrs.merge(
            fb_enrichment,
            on=['game_pk', 'batter', 'pitcher'],
            how='left',
            suffixes=('', '_fb')
        )
        hrs['batter_name'] = hrs.get('batter_name', pd.Series([np.nan] * len(hrs))).combine_first(hrs.get('batter_name_fb'))
        hrs['pitcher_name'] = hrs.get('pitcher_name', pd.Series([np.nan] * len(hrs))).combine_first(hrs.get('pitcher_name_fb'))
        hrs['model_prob'] = pd.to_numeric(
            hrs.get('model_prob', pd.Series([np.nan] * len(hrs))).combine_first(hrs.get('model_prob_fb')),
            errors='coerce'
        )
        hrs = hrs.drop(columns=[c for c in ['batter_name_fb', 'pitcher_name_fb', 'model_prob_fb'] if c in hrs.columns])

    # Fallback: use yesterday evaluation snapshot to recover model probabilities/names
    # for HR events that never reached live feedback.
    if evaluation_file.exists() and {'game_pk', 'batter', 'pitcher'}.issubset(hrs.columns):
        try:
            eval_df = pd.read_csv(evaluation_file)
            if not eval_df.empty:
                eval_df = eval_df.copy()
                eval_df['game_pk'] = pd.to_numeric(eval_df.get('game_pk', np.nan), errors='coerce')
                eval_df['batter'] = pd.to_numeric(eval_df.get('batter', np.nan), errors='coerce')
                eval_df['pitcher'] = pd.to_numeric(eval_df.get('pitcher', np.nan), errors='coerce')
                eval_df['pred_hr_prob'] = pd.to_numeric(eval_df.get('pred_hr_prob', np.nan), errors='coerce')
                eval_df['batter_name'] = eval_df.get('batter_name', pd.Series(['Unknown'] * len(eval_df))).apply(_safe_name)
                eval_df['pitcher_name'] = eval_df.get('pitcher_name', pd.Series(['Unknown'] * len(eval_df))).apply(_safe_name)

                eval_enrichment = eval_df[
                    ['game_pk', 'batter', 'pitcher', 'pred_hr_prob', 'batter_name', 'pitcher_name']
                ].dropna(subset=['game_pk', 'batter', 'pitcher'])
                eval_enrichment = eval_enrichment.sort_values(['game_pk', 'batter', 'pitcher']).drop_duplicates(
                    subset=['game_pk', 'batter', 'pitcher'],
                    keep='last'
                )

                hrs = hrs.merge(
                    eval_enrichment,
                    on=['game_pk', 'batter', 'pitcher'],
                    how='left',
                    suffixes=('', '_eval')
                )

                hrs['model_prob'] = pd.to_numeric(
                    hrs.get('model_prob', pd.Series([np.nan] * len(hrs))).combine_first(hrs.get('pred_hr_prob')),
                    errors='coerce'
                )
                hrs['batter_name'] = hrs.get('batter_name', pd.Series([np.nan] * len(hrs))).combine_first(
                    hrs.get('batter_name_eval')
                )
                hrs['pitcher_name'] = hrs.get('pitcher_name', pd.Series([np.nan] * len(hrs))).combine_first(
                    hrs.get('pitcher_name_eval')
                )

                hrs = hrs.drop(columns=[c for c in ['pred_hr_prob', 'batter_name_eval', 'pitcher_name_eval'] if c in hrs.columns])
        except Exception as exc:
            print(f"⚠️  Error enriching from evaluation file: {exc}")

    # Fall back to stable ID-based labels when names are unavailable.
    if 'batter_name' in hrs.columns:
        hrs['batter_name'] = hrs['batter_name'].apply(_safe_name)
        batter_id_label = hrs.get('batter', pd.Series([np.nan] * len(hrs))).apply(
            lambda x: f"Batter_{int(x)}" if pd.notna(x) else 'Unknown'
        )
        hrs['batter_name'] = hrs['batter_name'].where(hrs['batter_name'] != 'Unknown', batter_id_label)
    if 'pitcher_name' in hrs.columns:
        hrs['pitcher_name'] = hrs['pitcher_name'].apply(_safe_name)
        pitcher_id_label = hrs.get('pitcher', pd.Series([np.nan] * len(hrs))).apply(
            lambda x: f"Pitcher_{int(x)}" if pd.notna(x) else 'Unknown'
        )
        hrs['pitcher_name'] = hrs['pitcher_name'].where(hrs['pitcher_name'] != 'Unknown', pitcher_id_label)

    if 'model_prob' in hrs.columns:
        hrs['model_prob'] = pd.to_numeric(hrs['model_prob'], errors='coerce').fillna(0.0)

    if 'event_key' in hrs.columns:
        return hrs.drop_duplicates(subset=['event_key'], keep='last')
    return hrs.drop_duplicates(subset=['game_pk', 'batter', 'pitcher'], keep='last')

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
        batter_name = _safe_name(hr_row.get('batter_name', 'Unknown'))
        pitcher_name = _safe_name(hr_row.get('pitcher_name', 'Unknown'))

        if pd.isna(batter_id) or pd.isna(pitcher_id):
            continue
        
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
            batter_pas = matching_pas
            pitcher_pas = matching_pas
        
        # Extract key features that led to this HR
        hr_features = {
            'batter_name': batter_name,
            'pitcher_name': pitcher_name,
            'batter_id': int(batter_id),
            'pitcher_id': int(pitcher_id),
            'hr_date': hr_row.get('game_date', datetime.today().strftime('%Y-%m-%d')),
            'model_prob': _safe_float(hr_row.get('model_prob', 0), 0.0),
        }
        
        # Batter's recent form
        if not batter_pas.empty:
            batter_pas_sorted = batter_pas.sort_values('game_date', ascending=False).head(20)
            valid_ev = pd.to_numeric(batter_pas_sorted.get('launch_speed', pd.Series([np.nan] * len(batter_pas_sorted))), errors='coerce').dropna()
            valid_ev = valid_ev[valid_ev > 70.0]
            if valid_ev.empty:
                hr_features['batter_recent_avg_exit_velo'] = 88.0
                hr_features['batter_recent_barrel_rate'] = 0.0
            else:
                hr_features['batter_recent_avg_exit_velo'] = float(valid_ev.mean())
                hr_features['batter_recent_barrel_rate'] = float((valid_ev >= 98).mean())
            hr_features['batter_hr_rate_recent'] = float(
                (batter_pas_sorted['events'] == 'home_run').mean()
            ) if not batter_pas_sorted.empty else 0.0
            hr_features['batter_pa_count_recent'] = int(len(batter_pas_sorted))
        
        # Pitcher's vulnerability
        if not pitcher_pas.empty:
            pitcher_pas_sorted = pitcher_pas.sort_values('game_date', ascending=False).head(20)
            valid_ev = pd.to_numeric(pitcher_pas_sorted.get('launch_speed', pd.Series([np.nan] * len(pitcher_pas_sorted))), errors='coerce').dropna()
            valid_ev = valid_ev[valid_ev > 70.0]
            hr_features['pitcher_recent_hr_allowed_rate'] = float(
                (pitcher_pas_sorted['events'] == 'home_run').mean()
            )
            hr_features['pitcher_recent_avg_exit_velo_allowed'] = float(
                valid_ev.mean() if not valid_ev.empty else 88.0
            )
            hr_features['pitcher_pa_count_recent'] = int(len(pitcher_pas_sorted))
        
        # Game conditions (if available)
        hr_features['weather_temp'] = _safe_float(hr_row.get('temp', 71), 71.0)
        hr_features['weather_wind_speed'] = _safe_float(hr_row.get('wind_speed', 5), 5.0)
        hr_features['park_factor'] = _safe_float(hr_row.get('park_factor', 100), 100.0)
        
        patterns.append(hr_features)
        
        # Aggregate statistics
        summary_stats[batter_name] = summary_stats.get(batter_name, 0) + 1
    
    return patterns, summary_stats

def build_learning_insights_from_evaluation(eval_df):
    """Build a learning report from yesterday's evaluation file when live feedback is absent."""
    if eval_df is None or eval_df.empty:
        return None

    eval_df = eval_df.copy()
    eval_df['actual_hr'] = pd.to_numeric(eval_df.get('actual_hr', 0), errors='coerce').fillna(0).astype(int)
    eval_df['pred_hr_prob'] = pd.to_numeric(eval_df.get('pred_hr_prob', 0), errors='coerce').fillna(0.0)

    # Prefer the outcome date if present; fallback to yesterday.
    analysis_date = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    for date_col in ['date', 'event_date', 'game_date']:
        if date_col in eval_df.columns:
            try:
                dvals = pd.to_datetime(eval_df[date_col], errors='coerce').dropna()
                if not dvals.empty:
                    analysis_date = dvals.iloc[0].strftime('%Y-%m-%d')
                    break
            except Exception:
                pass

    patterns = []
    summary_stats = {}
    hit_threshold = _learning_hit_threshold(0.08)
    for _, row in eval_df[eval_df['actual_hr'] == 1].iterrows():
        model_prob = _safe_float(row.get('pred_hr_prob', 0.0), 0.0)
        pattern = {
            'batter_name': _safe_name(row.get('batter_name', 'Unknown')),
            'pitcher_name': _safe_name(row.get('pitcher_name', 'Unknown')),
            'model_prob': model_prob,
            'prediction_category': 'PREDICTED' if model_prob >= hit_threshold else 'MISSED',
        }
        patterns.append(pattern)
        summary_stats[str(pattern['batter_name'])] = summary_stats.get(str(pattern['batter_name']), 0) + 1

    if not patterns:
        return None

    insights = {
        'analysis_date': analysis_date,
        'total_hrs_analyzed': len(patterns),
        'unique_batters': len(summary_stats),
        'missed_predictions': int(sum(1 for p in patterns if p.get('model_prob', 0) < hit_threshold)),
        'accurate_predictions': int(sum(1 for p in patterns if p.get('model_prob', 0) >= hit_threshold)),
        'patterns': patterns,
        'key_findings': [],
    }

    insights['key_findings'].append(
        f"⚠️  Model missed {insights['missed_predictions']}/{insights['total_hrs_analyzed']} HRs "
        f"(threshold={hit_threshold:.2f})"
    )
    return insights


def generate_learning_insights(patterns, summary_stats):
    """Create a learning report from yesterday's HR patterns."""
    
    if not patterns:
        return None
    
    insights = {
        'analysis_date': (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d'),
        'total_hrs_analyzed': len(patterns),
        'unique_batters': len(summary_stats),
        'missed_predictions': 0,
        'accurate_predictions': 0,
        'patterns': [],
        'key_findings': []
    }
    
    hit_threshold = _learning_hit_threshold(0.08)
    near_hit_floor = max(0.01, hit_threshold - 0.03)

    # Analyze each pattern
    for pattern in patterns:
        model_prob = pattern.get('model_prob', 0)
        
        # Categorize prediction accuracy
        if model_prob < near_hit_floor:
            insights['missed_predictions'] += 1
            pattern['prediction_category'] = 'MISSED (low prob)'
        elif model_prob < hit_threshold:
            insights['missed_predictions'] += 1
            pattern['prediction_category'] = 'MISSED (near hit threshold)'
        else:
            insights['accurate_predictions'] += 1
            pattern['prediction_category'] = 'PREDICTED'
        
        insights['patterns'].append(pattern)
    
    # Extract key findings
    batter_hot = sorted(summary_stats.items(), key=lambda x: x[1], reverse=True)[:3]
    insights['key_findings'].append(f"🔥 Hottest batters: {', '.join([f'{name} ({count} HRs)' for name, count in batter_hot])}")
    
    missed = sum(1 for p in patterns if p['prediction_category'].startswith('MISSED'))
    insights['key_findings'].append(
        f"⚠️  Model missed {missed}/{len(patterns)} HRs "
        f"(threshold={hit_threshold:.2f})"
    )
    
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
        
        # Limit the extreme weight boost on uncalibrated player profiles.
        if pattern.get('prediction_category', '').startswith('MISSED'):
            recent_barrel = float(pattern.get('batter_recent_barrel_rate', 0.0) or 0.0)
            recent_pa = int(pattern.get('batter_pa_count_recent', 0) or 0)
            if recent_barrel < 0.04 or recent_pa < 5:
                feedback_boost[key] = 1.75
            else:
                feedback_boost[key] = 3.0
        else:
            feedback_boost[key] = 1.5
    
    return feedback_boost

def save_learning_report(insights):
    """Persist learning insights to file for monitoring."""
    
    if not insights:
        return
    
    today = insights['analysis_date']
    report_file = Path('data') / f'hr_learning_report_{today}.json'
    
    # Convert patterns to serializable format and strip NaN literals.
    insights_copy = insights.copy()
    insights_copy['patterns'] = [_sanitize_pattern(p) for p in insights_copy['patterns']]

    report_file.write_text(json.dumps(insights_copy, indent=2, allow_nan=False), encoding='utf-8')
    
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

    # If canonical evaluation rows were loaded, build report directly so
    # learning counts stay aligned with evaluation metrics.
    if not actual_hrs.empty and 'source' in actual_hrs.columns:
        src = actual_hrs['source'].astype(str).str.lower().fillna('')
        if src.str.contains('evaluation').all() or src.str.contains('fallback').all():
            eval_like = actual_hrs.copy()
            eval_like['actual_hr'] = 1
            eval_like['pred_hr_prob'] = pd.to_numeric(eval_like.get('model_prob', 0.0), errors='coerce').fillna(0.0)
            insights = build_learning_insights_from_evaluation(eval_like)
            if insights:
                report_file = save_learning_report(insights)
                if report_file:
                    print(f"✅ Learning report saved from canonical evaluation source: {report_file}")
                print_learning_report(insights)
                return {'insights': insights, 'feedback_boost': {}, 'patterns': insights.get('patterns', [])}
    
    if actual_hrs.empty:
        yesterday = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
        eval_file = Path('data') / f'evaluation_{yesterday}.csv'
        if eval_file.exists():
            try:
                eval_df = pd.read_csv(eval_file)
                insights = build_learning_insights_from_evaluation(eval_df)
                if insights:
                    report_file = save_learning_report(insights)
                    if report_file:
                        print(f"✅ Learning report saved from evaluation fallback: {report_file}")
                    print_learning_report(insights)
                    return {'insights': insights, 'feedback_boost': {}, 'patterns': []}
            except Exception as exc:
                print(f"⚠️  Evaluation fallback failed: {exc}")
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
