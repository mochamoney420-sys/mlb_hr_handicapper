#!/usr/bin/env python
"""
End-of-Day HR Pattern Learner: Runs after all games finish (typically 10-11 PM ET).
Analyzes today's home runs and prepares insights for tomorrow's model training.
"""

import os
import sys
import time
import statsapi
from datetime import datetime, timedelta
from pathlib import Path

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
# WAIT FOR GAMES TO END
# =====================================================================

def check_all_games_complete(max_attempts=60):
    """
    Monitor today's games until all are complete.
    Returns True when all games are finished or after max_attempts.
    
    Typical baseball day:
    - First pitch: 1:10 PM ET
    - Last game end: ~10:30 PM ET
    - Total duration: ~9 hours
    
    Check every 5 minutes starting at 8 PM ET.
    """
    
    today_str = datetime.today().strftime('%m/%d/%Y')
    print(f"\n🏟️  WAITING FOR ALL GAMES TO COMPLETE")
    print("="*70)
    print(f"Monitoring date: {today_str}")
    print("Will check every 5 minutes until all games finish...")
    print("Typical wait time: 8-10 PM ET\n")
    
    for attempt in range(1, max_attempts + 1):
        try:
            games = statsapi.schedule(date=today_str) or []
            
            if not games:
                print(f"[{datetime.now().strftime('%H:%M')}] Attempt {attempt}: No games today, skipping")
                return True
            
            total_games = len(games)
            complete_games = sum(1 for g in games if g['status'] == 'Final' or g['status'].startswith('Game Over'))
            
            print(f"[{datetime.now().strftime('%H:%M')}] Games: {complete_games}/{total_games} complete", end='')
            
            if complete_games == total_games:
                print(" ✅ ALL GAMES COMPLETE!")
                return True
            
            print(f" (waiting...)")
            
            # Check every 5 minutes
            time.sleep(300)
            
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M')}] Check error: {e}")
            time.sleep(300)
    
    print(f"\n⏰ Max wait time reached ({max_attempts * 5} minutes)")
    print("Proceeding with pattern analysis anyway...\n")
    return False

def run_end_of_day_learning():
    """Run HR pattern analysis after games complete."""
    
    print("\n" + "="*70)
    print("🌙 END-OF-DAY HOME RUN PATTERN LEARNING")
    print("="*70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Step 1: Wait for games
    check_all_games_complete()
    
    # Step 2: Run pattern analysis
    print("\n📊 Analyzing today's home runs for pattern learning...")
    print("-"*70)
    
    try:
        from analyze_hr_patterns import analyze_yesterdays_hrs_and_learn
        
        # Note: "yesterday" from tomorrow's perspective = today's games
        # So this will analyze today's HRs when run tonight
        result = analyze_yesterdays_hrs_and_learn()
        
        if result and result.get('insights'):
            insights = result['insights']
            total_hrs = insights['total_hrs_analyzed']
            
            print("\n" + "="*70)
            print("✅ PATTERN LEARNING COMPLETE")
            print("="*70)
            print(f"Home Runs Analyzed: {total_hrs}")
            print(f"Accurate Predictions: {insights['accurate_predictions']}")
            print(f"Missed Predictions: {insights['missed_predictions']}")
            print(f"Report saved: data/hr_learning_report_{insights['analysis_date']}.json")
            
            print("\n🧠 These patterns will automatically improve tomorrow's model training:")
            for finding in insights.get('key_findings', []):
                print(f"   • {finding}")
            
            print("\n" + "="*70)
            print("Tomorrow's model training will incorporate these insights ✨")
            print("="*70)
            
            return True
        else:
            print("⚠️  No home runs to analyze today")
            return False
            
    except ImportError:
        print("❌ analyze_hr_patterns module not found")
        return False
    except Exception as e:
        print(f"❌ Analysis failed: {e}")
        import traceback
        traceback.print_exc()
        return False

# =====================================================================
# SCHEDULER SETUP
# =====================================================================

def schedule_end_of_day_runner():
    """Create Windows Task Scheduler job for nightly pattern learning."""
    import subprocess
    
    script_path = Path(__file__).resolve()
    workspace_dir = script_path.parent
    
    ps_cmd = f"""
$TaskName = "MLB_HR_EndOfDayLearning"
$TaskPath = "\\MLB_HR_Handicapper\\"

# Check if task exists
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($task) {{
    Write-Host "Task already exists. Updating..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}}

# Create trigger: Every day at 8 PM ET
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00pm

# Create action: Run Python script
$action = New-ScheduledTaskAction -Execute 'C:\\Users\\bobby\\AppData\\Local\\Programs\\Python\\Python314\\python.exe' -Argument '{script_path}' -WorkingDirectory '{workspace_dir}'

# Create task with high priority
$task = New-ScheduledTask -Action $action -Trigger $trigger -TaskName $TaskName -Description "MLB HR Model: End-of-day pattern learning (runs after games finish)" -RunLevel Highest

# Register task
Register-ScheduledTask -TaskPath $TaskPath -InputObject $task -Force
Write-Host "✅ End-of-day learning scheduled (8:00 PM daily)"
"""
    
    try:
        result = subprocess.run(
            ['powershell', '-Command', ps_cmd],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("✅ End-of-day learner scheduled")
            print("   • Runs at 8:00 PM ET daily")
            print("   • Waits for all games to finish")
            print("   • Analyzes day's home runs")
            print("   • Insights feed tomorrow's training")
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
        # One-time setup: create scheduled task
        print("Setting up end-of-day automatic learning...")
        schedule_end_of_day_runner()
    else:
        # Run learning immediately
        success = run_end_of_day_learning()
        sys.exit(0 if success else 1)
