#!/usr/bin/env python
"""Test lineup extraction directly."""

import sys
import os
from pathlib import Path

# UTF-8 encoding fix for Windows
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent / 'src'))

print("Starting test...")

try:
    from baseball_savant import check_lineups_morning
    print("✓ Imported check_lineups_morning")
except Exception as e:
    print(f"❌ Failed to import: {e}")
    sys.exit(1)

print("\n" + "="*70)
print("TESTING MORNING LINEUP CHECK")
print("="*70)

try:
    lineups = check_lineups_morning()
    print(f"\n✅ Got lineups for {len(lineups)} games")
    
    if lineups:
        first_game_id = list(lineups.keys())[0]
        first_game = lineups[first_game_id]
        
        away_batters = [p['name'] for p in first_game['away_players'] if p['is_batter']]
        home_batters = [p['name'] for p in first_game['home_players'] if p['is_batter']]
        
        print(f"\n✓ Confirmed {len(away_batters)} away batters, {len(home_batters)} home batters in first game")
        
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
