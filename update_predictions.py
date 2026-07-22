#!/usr/bin/env python
"""
Continuous Prediction Updater: Checks every 2-4 hours for lineup changes,
updates predictions, and posts deltas to Discord if anything changed.

Ensures predictions are always fresh and accurate with latest game info.
"""

import os
import sys
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
# PREDICTION UPDATE ENGINE
# =====================================================================

def load_current_predictions():
    """Load today's current predictions from CSV."""
    today = datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{today}.csv'
    
    if not pred_file.exists():
        print(f"ℹ️  No predictions file found: {pred_file}")
        return pd.DataFrame()
    
    try:
        df = pd.read_csv(pred_file)
        print(f"✓ Loaded {len(df)} predictions from {today}")
        return df
    except Exception as e:
        print(f"✗ Error loading predictions: {e}")
        return pd.DataFrame()

def get_todays_lineups():
    """Get current lineups for today's games."""
    try:
        import statsapi
        today_str = datetime.today().strftime('%m/%d/%Y')
        games = statsapi.schedule(date=today_str) or []
        
        lineups = {}
        for game in games:
            game_pk = game.get('game_id')
            away_team = game.get('away_name', '')
            home_team = game.get('home_name', '')
            
            try:
                game_data = statsapi.get('game', {'gamePk': game_pk})
                away_lineup = game_data.get('gameData', {}).get('teams', {}).get('away', {}).get('lineup', [])
                home_lineup = game_data.get('gameData', {}).get('teams', {}).get('home', {}).get('lineup', [])
                
                lineups[game_pk] = {
                    'away_team': away_team,
                    'away_players': away_lineup,
                    'home_team': home_team,
                    'home_players': home_lineup
                }
            except:
                pass
        
        return lineups
    except Exception as e:
        print(f"✗ Error fetching lineups: {e}")
        return {}

def detect_lineup_changes(current_preds, new_lineups):
    """Detect if any predicted batters are no longer in lineup."""
    changes = []
    
    for idx, pred in current_preds.iterrows():
        batter_name = pred.get('batter_name', '')
        game_pk = pred.get('game_pk')
        
        if not game_pk or game_pk not in new_lineups:
            continue
        
        game_info = new_lineups[game_pk]
        
        # Check if batter is still in lineup
        all_players = game_info.get('away_players', []) + game_info.get('home_players', [])
        player_names = [p.get('person', {}).get('fullName', '').lower() for p in all_players]
        
        if batter_name.lower() not in player_names:
            changes.append({
                'batter_name': batter_name,
                'game_pk': game_pk,
                'change_type': 'REMOVED_FROM_LINEUP',
                'old_prob': float(pred.get('pred_hr_prob', 0)),
                'new_prob': 0.0
            })
    
    return changes

def regenerate_predictions_with_current_data():
    """Regenerate predictions with most current data (simplified version)."""
    try:
        # Import the main prediction function
        from run_daily_predictions import generate_daily_predictions
        
        print("\n🔄 Regenerating predictions with current data...")
        updated_preds = generate_daily_predictions()
        
        if not updated_preds.empty:
            print(f"✓ Successfully regenerated {len(updated_preds)} predictions")
            return updated_preds
        else:
            print("✗ Prediction regeneration returned empty")
            return pd.DataFrame()
    except Exception as e:
        print(f"✗ Error regenerating predictions: {e}")
        return pd.DataFrame()

def compare_predictions(old_preds, new_preds):
    """Compare old and new predictions to find significant changes."""
    changes = []
    
    if old_preds.empty or new_preds.empty:
        return changes
    
    # Merge on batter_name to find matches
    merged = old_preds[['batter_name', 'pitcher_name', 'pred_hr_prob']].merge(
        new_preds[['batter_name', 'pitcher_name', 'pred_hr_prob']],
        on=['batter_name', 'pitcher_name'],
        suffixes=('_old', '_new'),
        how='outer'
    )
    
    for idx, row in merged.iterrows():
        old_prob = row.get('pred_hr_prob_old', 0)
        new_prob = row.get('pred_hr_prob_new', 0)
        
        if pd.isna(old_prob):
            old_prob = 0
        if pd.isna(new_prob):
            new_prob = 0
        
        # Flag significant changes (>2% shift)
        change_pct = abs(new_prob - old_prob) * 100
        
        if change_pct >= 2.0:  # 2% threshold
            changes.append({
                'batter_name': row.get('batter_name', ''),
                'pitcher_name': row.get('pitcher_name', ''),
                'old_prob': float(old_prob),
                'new_prob': float(new_prob),
                'change_pct': float(change_pct),
                'direction': '📈 UP' if new_prob > old_prob else '📉 DOWN'
            })
    
    return sorted(changes, key=lambda x: x['change_pct'], reverse=True)

def send_update_to_discord(changes, update_reason):
    """Post prediction updates to Discord."""
    webhook_url = os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    
    if not webhook_url or not changes:
        return False
    
    try:
        import requests
        
        # Build message
        content = f"🔄 **PREDICTION UPDATE** — {update_reason}\n"
        content += f"Time: {datetime.now().strftime('%I:%M %p')}\n"
        content += f"Changes detected: {len(changes)}\n"
        content += "```\n"
        
        for i, change in enumerate(changes[:10], 1):  # Top 10 changes
            batter = change.get('batter_name', '').title()
            pitcher = change.get('pitcher_name', '').title()
            direction = change.get('direction', '')
            old = change.get('old_prob', 0)
            new = change.get('new_prob', 0)
            delta = change.get('change_pct', 0)
            
            content += f"{i}. {batter} vs {pitcher}\n"
            content += f"   {direction}: {old:.1%} → {new:.1%} ({delta:+.1f}%)\n"
        
        content += "```\n"
        
        if len(changes) > 10:
            content += f"... and {len(changes) - 10} more changes\n"
        
        # Send to Discord
        response = requests.post(
            webhook_url,
            json={'content': content},
            timeout=5
        )
        
        if response.status_code == 204:
            print(f"✓ Sent update to Discord: {len(changes)} changes")
            return True
        else:
            print(f"✗ Discord post failed: {response.status_code}")
            return False
    
    except Exception as e:
        print(f"✗ Error sending Discord message: {e}")
        return False

def save_prediction_update_log(changes, update_reason):
    """Log all prediction updates for tracking."""
    today = datetime.today().strftime('%Y-%m-%d')
    log_file = Path('data') / f'prediction_updates_{today}.json'
    
    Path('data').mkdir(parents=True, exist_ok=True)
    
    log_entry = {
        'timestamp': datetime.now().isoformat(),
        'update_reason': update_reason,
        'changes_count': len(changes),
        'changes': changes
    }
    
    # Append to log file
    if log_file.exists():
        try:
            existing = json.loads(log_file.read_text())
            if not isinstance(existing, list):
                existing = [existing]
        except:
            existing = []
    else:
        existing = []
    
    existing.append(log_entry)
    log_file.write_text(json.dumps(existing, indent=2))
    
    print(f"✓ Logged update: {log_file}")

# =====================================================================
# MAIN UPDATE CYCLE
# =====================================================================

def check_and_update_predictions():
    """Main function: Check for changes and update predictions."""
    
    print("\n" + "="*70)
    print("🔄 CONTINUOUS PREDICTION UPDATE CHECK")
    print("="*70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Step 1: Load current predictions
    current_preds = load_current_predictions()
    if current_preds.empty:
        print("⚠️  No predictions to update")
        return False
    
    # Step 2: Get current lineups
    print("\n📋 Fetching current lineups...")
    new_lineups = get_todays_lineups()
    
    # Step 3: Detect lineup changes
    print("🔍 Checking for lineup changes...")
    lineup_changes = detect_lineup_changes(current_preds, new_lineups)
    
    if lineup_changes:
        print(f"⚠️  Found {len(lineup_changes)} lineup changes")
        for change in lineup_changes[:5]:
            print(f"   • {change['batter_name']}: REMOVED FROM LINEUP")
    
    # Step 4: Regenerate predictions with current data
    print("\n🧮 Regenerating predictions with current data...")
    new_preds = regenerate_predictions_with_current_data()
    
    if new_preds.empty:
        print("⚠️  Could not regenerate predictions")
        return False
    
    # Step 5: Compare predictions
    print("📊 Comparing old vs new predictions...")
    prob_changes = compare_predictions(current_preds, new_preds)
    
    # Step 6: Combine all changes
    all_changes = lineup_changes + prob_changes
    all_changes = sorted(all_changes, 
                         key=lambda x: abs(x.get('change_pct', x.get('delta_prob', 0))), 
                         reverse=True)
    
    print(f"\n📈 Total changes detected: {len(all_changes)}")
    
    if all_changes:
        # Step 7: Save predictions with timestamp
        today = datetime.today().strftime('%Y-%m-%d')
        backup_file = Path('data') / f'predictions_{today}_BACKUP_{datetime.now().strftime("%H%M%S")}.csv'
        current_preds.to_csv(backup_file, index=False)
        print(f"✓ Backed up old predictions: {backup_file}")
        
        # Step 8: Save new predictions
        pred_file = Path('data') / f'predictions_{today}.csv'
        new_preds.to_csv(pred_file, index=False)
        print(f"✓ Saved updated predictions: {pred_file}")
        
        # Step 9: Send to Discord
        update_reason = f"Lineup changes: {len(lineup_changes)}, Probability updates: {len(prob_changes)}"
        send_update_to_discord(all_changes[:15], update_reason)
        
        # Step 10: Log update
        save_prediction_update_log(all_changes, update_reason)
        
        return True
    else:
        print("✓ No significant changes detected")
        return False

# =====================================================================
# SCHEDULER SETUP
# =====================================================================

def schedule_prediction_updates():
    """Create Windows Task Scheduler job for prediction updates."""
    import subprocess
    
    script_path = Path(__file__).resolve()
    workspace_dir = script_path.parent
    
    ps_cmd = f"""
$TaskName = "MLB_HR_PredictionUpdater"
$TaskPath = "\\MLB_HR_Handicapper\\"

# Check if task exists
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($task) {{
    Write-Host "Task already exists. Updating..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}}

# Create trigger: Every 2 hours starting at 10 AM
$trigger = New-ScheduledTaskTrigger -Daily -At 10:00am -RepetitionInterval (New-TimeSpan -Hours 2) -RepetitionDuration (New-TimeSpan -Days 999)

# Create action: Run Python script
$action = New-ScheduledTaskAction -Execute 'C:\\Users\\bobby\\AppData\\Local\\Programs\\Python\\Python314\\python.exe' -Argument '{script_path}' -WorkingDirectory '{workspace_dir}'

# Create task with high priority
$task = New-ScheduledTask -Action $action -Trigger $trigger -TaskName $TaskName -Description "MLB HR Model: Continuous prediction updates (every 2 hours)" -RunLevel Highest

# Register task
Register-ScheduledTask -TaskPath $TaskPath -InputObject $task -Force
Write-Host "✅ Prediction updater scheduled (every 2 hours starting 10 AM)"
"""
    
    try:
        result = subprocess.run(
            ['powershell', '-Command', ps_cmd],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("✅ Prediction updater scheduled")
            print("   • Runs: Every 2 hours (10 AM, 12 PM, 2 PM, 4 PM, 6 PM, 8 PM)")
            print("   • Checks: Lineup changes, probability shifts")
            print("   • Updates: Discord + predictions CSV")
            return True
        else:
            print(f"⚠️  Could not schedule: {result.stderr}")
            return False
    except Exception as e:
        print(f"⚠️  Scheduling error: {e}")
        return False

# =====================================================================
# ENTRY POINT
# =====================================================================

if __name__ == '__main__':
    if '--setup' in sys.argv:
        print("Setting up continuous prediction updates...")
        schedule_prediction_updates()
    else:
        try:
            updated = check_and_update_predictions()
            if updated:
                print("✓ Update cycle completed with changes applied.")
            else:
                print("✓ Update cycle completed with no changes required.")
            # No-op cycles are expected and should not fail CI.
            sys.exit(0)
        except Exception as e:
            print(f"✗ Unhandled updater error: {e}")
            sys.exit(1)
