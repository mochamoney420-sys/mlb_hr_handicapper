#!/usr/bin/env python
"""
Pre-game Lineup Checker: Runs 2-3 hours before first MLB game of the day.
Verifies final lineups are set and alerts to any scratches/injuries.
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from baseball_savant import (
    check_lineups_pregame,
    save_lineup_report,
    get_todays_games,
    print_lineup_summary
)

def run_pregame_check():
    """Execute pre-game lineup verification."""
    print("\n" + "="*70)
    print("⚾ PRE-GAME LINEUP VERIFICATION")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)
    
    games = get_todays_games()
    if not games:
        print("No games scheduled for today.")
        return False
    
    print(f"Found {len(games)} game(s) today.\n")
    
    # Find earliest first pitch
    first_pitches = [g.get('game_datetime') for g in games if g.get('game_datetime')]
    if first_pitches:
        first_pitch = min(first_pitches)
        now = datetime.now()
        try:
            fp_time = datetime.fromisoformat(first_pitch.replace('Z', '+00:00'))
            hours_to_first_pitch = (fp_time - now).total_seconds() / 3600
            print(f"First pitch: {first_pitch}")
            print(f"Time until first pitch: {hours_to_first_pitch:.1f} hours\n")
        except:
            pass
    
    # Check lineups
    try:
        lineups = check_lineups_pregame()
        if lineups:
            print_lineup_summary(lineups)
            save_lineup_report(lineups, "_pregame_check")
            return True
        else:
            print("⚠️ No lineups found within 6 hours of first pitch.")
            return False
    except Exception as e:
        print(f"❌ Error checking lineups: {e}")
        return False


def schedule_pregame_check():
    """Set up Windows Task Scheduler for pre-game checks."""
    import subprocess
    
    today = datetime.now().strftime('%Y-%m-%d')
    games = get_todays_games()
    
    if not games:
        print("No games today, no need to schedule pre-game check.")
        return
    
    # Find earliest first pitch and schedule 2.5 hours before
    first_pitches = [g.get('game_datetime') for g in games if g.get('game_datetime')]
    if first_pitches:
        first_pitch = min(first_pitches)
        try:
            fp_time = datetime.fromisoformat(first_pitch.replace('Z', '+00:00'))
            check_time = fp_time - timedelta(hours=2.5)
            
            # Format for Task Scheduler
            time_str = check_time.strftime('%H:%M')
            
            task_name = f"MLB_PreGame_Check_{today}"
            script_path = Path(__file__).resolve()
            
            cmd = (
                f'SchTasks /Create /TN "{task_name}" /TR '
                f'"python {script_path}" /SC ONCE /ST {time_str} /F'
            )
            
            try:
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if result.returncode == 0:
                    print(f"✅ Pre-game check scheduled for {time_str} (2.5 hours before first pitch)")
                else:
                    print(f"⚠️ Could not schedule: {result.stderr}")
            except Exception as e:
                print(f"⚠️ Scheduling failed: {e}")
        except:
            print("⚠️ Could not parse first pitch time")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Pre-game lineup checker")
    parser.add_argument("--schedule", action="store_true", help="Schedule this script to run before first pitch")
    parser.add_argument("--run", action="store_true", help="Run pre-game check now")
    
    args = parser.parse_args()
    
    if args.schedule:
        schedule_pregame_check()
    elif args.run:
        success = run_pregame_check()
        sys.exit(0 if success else 1)
    else:
        # Default: run the check
        success = run_pregame_check()
        sys.exit(0 if success else 1)
