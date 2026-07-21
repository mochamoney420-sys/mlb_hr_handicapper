#!/usr/bin/env python
"""Quick test of Baseball Savant integration."""

import sys
import os
from pathlib import Path

# UTF-8 encoding fix for Windows
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent / 'src'))

from baseball_savant import get_todays_games, get_batting_orders_for_games

print("\nTesting Baseball Savant Integration...\n")

# Test 1: Get today's games
games = get_todays_games()
print(f"✓ Found {len(games)} games today")
for g in games[:5]:
    print(f"  {g['away_name']} @ {g['home_name']}")

# Test 2: Get batting orders
print("\nFetching batting orders...")
batting_orders = get_batting_orders_for_games()
print(f"✓ Got batting orders for {len(batting_orders)} games")

if batting_orders:
    first_game = list(batting_orders.values())[0]
    print(f"\nExample game: {first_game['away_team']} @ {first_game['home_team']}")
    print(f"Away batting order ({len(first_game['away_batting_order'])} batters):")
    for order in first_game['away_batting_order'][:5]:
        print(f"  {order['slot']}. {order['name']}")

print("\n✅ Baseball Savant integration working!")
