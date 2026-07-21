#!/usr/bin/env python
"""Debug the Odds API connection to identify why it's returning 401."""

import os
import sys
sys.path.insert(0, '.')

# Load env vars from .vscode/.env first
env_file = os.path.join(os.path.dirname(__file__), '.vscode', '.env')
if os.path.exists(env_file):
    for line in open(env_file):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, val = line.split('=', 1)
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            os.environ.setdefault(key.strip(), val.strip())

import requests
import json

api_key = os.getenv('ODDS_API_KEY', 'NOT_SET')

print("=" * 70)
print("ODDS API DEBUGGING")
print("=" * 70)
print(f"\nAPI Key: {api_key[:10]}...{api_key[-10:]}")

# Test 1: Basic connectivity with sports endpoint
print("\n1. Testing basic API connectivity...")
print("-" * 70)
url_sports = f"https://api.the-odds-api.com/v4/sports?apiKey={api_key}"
try:
    resp = requests.get(url_sports, timeout=10)
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        sports = resp.json()
        print(f"✅ API is responding! Available sports: {len(sports)}")
        baseball_sports = [s for s in sports if 'baseball' in s.get('key', '').lower()]
        print(f"Baseball sports available: {len(baseball_sports)}")
        for sport in baseball_sports[:3]:
            print(f"  - {sport.get('key')}: {sport.get('title')}")
    else:
        print(f"❌ Status {resp.status_code}")
        print(f"Response: {resp.text[:500]}")
except Exception as e:
    print(f"❌ Error: {e}")

# Test 2: Try to get MLB odds with different parameters
print("\n2. Testing MLB odds endpoint...")
print("-" * 70)

test_configs = [
    {
        "name": "batter_home_runs market",
        "url": f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds?apiKey={api_key}&regions=us&markets=batter_home_runs&oddsFormat=american"
    },
    {
        "name": "all markets",
        "url": f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds?apiKey={api_key}&regions=us&oddsFormat=american"
    },
    {
        "name": "h2h market (basic test)",
        "url": f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds?apiKey={api_key}&regions=us&markets=h2h&oddsFormat=american"
    }
]

for config in test_configs:
    print(f"\n  Trying: {config['name']}")
    try:
        resp = requests.get(config['url'], timeout=10)
        print(f"    Status: {resp.status_code}", end="")
        if resp.status_code == 200:
            data = resp.json()
            print(f" ✅")
            print(f"    Games available: {len(data)}")
            if data:
                game = data[0]
                print(f"    First game bookmakers: {len(game.get('bookmakers', []))}")
                if game.get('bookmakers'):
                    first_bm = game['bookmakers'][0]
                    markets = first_bm.get('markets', [])
                    print(f"    Markets in first book: {[m.get('key') for m in markets[:3]]}")
        else:
            print(f" ❌")
            error_text = resp.text[:200]
            print(f"    Error: {error_text}")
    except Exception as e:
        print(f"    Error: {e}")

print("\n" + "=" * 70)
print("INTERPRETATION:")
print("=" * 70)
print("""
If Status 401 on all requests:
  → API key is invalid or account not active
  → Need to verify key at https://the-odds-api.com

If Status 200 on h2h but 401 on batter_home_runs:
  → Account tier doesn't include player props
  → Need to upgrade to Professional plan

If Status 200 on batter_home_runs:
  → API is working! Check if data exists for today's games
""")
