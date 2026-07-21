#!/usr/bin/env python
"""Test the paid Odds API tier to verify it now works with batter_home_runs market."""

import os
import sys
sys.path.insert(0, '.')

# Ensure the API key from .env is loaded
from run_daily_predictions import fetch_hr_prop_odds, fetch_hr_prop_odds_raw

print("=" * 70)
print("TESTING PAID THE ODDS API TIER")
print("=" * 70)

# Test 1: Consensus devigged probabilities
print("\n1. Fetching Consensus Devigged Probabilities")
print("-" * 70)
try:
    odds_consensus = fetch_hr_prop_odds()
    if odds_consensus:
        print(f"✅ SUCCESS! Fetched consensus odds for {len(odds_consensus)} players")
        print(f"\nTop 10 players by probability:")
        sorted_odds = sorted(odds_consensus.items(), key=lambda x: x[1], reverse=True)
        for i, (player, prob) in enumerate(sorted_odds[:10], 1):
            print(f"  {i:2}. {player:<30} {prob:.1%}")
    else:
        print("⚠️  Empty result - API may not have populated odds yet")
except Exception as e:
    print(f"❌ ERROR: {e}")

# Test 2: Raw odds per sportsbook
print("\n2. Fetching Raw Odds Per Sportsbook")
print("-" * 70)
try:
    odds_raw = fetch_hr_prop_odds_raw()
    if odds_raw:
        print(f"✅ SUCCESS! Fetched raw odds for {len(odds_raw)} players")
        print(f"\nFirst player's odds across all books:")
        first_player = list(odds_raw.keys())[0]
        print(f"  Player: {first_player}")
        books = odds_raw[first_player]
        for book, american_odds in sorted(books.items()):
            # Convert American to implied probability
            if american_odds < 0:
                implied = abs(american_odds) / (abs(american_odds) + 100)
            else:
                implied = 100 / (american_odds + 100)
            print(f"    {book:<20} {american_odds:>6} → {implied:.1%} implied")
    else:
        print("⚠️  Empty result - API may not have populated odds yet")
except Exception as e:
    print(f"❌ ERROR: {e}")

print("\n" + "=" * 70)
print("If both tests passed, the paid Odds API is fully activated!")
print("All EV+ calculations and RLM detection will now use REAL market odds.")
print("=" * 70)
