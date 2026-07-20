"""Run daily predictions for MLB HR model with Weather and Park Factors."""
# =====================================================================
# SECTION 1: IMPORTS
# =====================================================================
import mlb
import os
import time
import requests
import pandas as pd
import numpy as np
import statsapi
import xgboost as xgb
import mlbstatsapi
from datetime import datetime, timedelta
from pybaseball import statcast

# Fix Pybaseball/Savant blocking by forcing a global browser user-agent header
import urllib.request
opener = urllib.request.build_opener()
opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')]
urllib.request.install_opener(opener)

# =====================================================================
# HARDCODED STATIC LOOKUPS: PARK FACTORS (3-Year HR Multipliers)
# =====================================================================
# 100 is baseline. >100 favors hitters, <100 favors pitchers.
PARK_HR_FACTORS = {
    'ARI': 94,  'ATL': 102, 'BAL': 95,  'BOS': 92,  'CHC': 105,
    'CWS': 112, 'CIN': 128, 'CLE': 104, 'COL': 114, 'DET': 88,
    'HOU': 106, 'KC': 84,   'LAA': 108, 'LAD': 115, 'MIA': 85,
    'MIL': 113, 'MIN': 99,  'NYM': 90,  'NYY': 116, 'OAK': 82,
    'PHI': 114, 'PIT': 89,  'SD': 91,   'SF': 76,   'SEA': 92,
    'STL': 89,  'TB': 93,   'TEX': 105, 'TOR': 101, 'WSH': 103
}

# Map StatCast Venue strings to team abbreviations for matrix matching
VENUE_MAP = {
    'Chase Field': 'ARI', 'Truist Park': 'ATL', 'Oriole Park at Camden Yards': 'BAL',
    'Fenway Park': 'BOS', 'Wrigley Field': 'CHC', 'Guaranteed Rate Field': 'CWS',
    'Great American Ball Park': 'CIN', 'Progressive Field': 'CLE', 'Coors Field': 'COL',
    'Comerica Park': 'DET', 'Minute Maid Park': 'HOU', 'Kauffman Stadium': 'KC',
    'Angel Stadium': 'LAA', 'Dodger Stadium': 'LAD', 'LoanDepot Park': 'MIA',
    'American Family Field': 'MIL', 'Target Field': 'MIN', 'Citi Field': 'NYM',
    'Yankee Stadium': 'NYY', 'Oakland Coliseum': 'OAK', 'Citizens Bank Park': 'PHI',
    'PNC Park': 'PIT', 'Petco Park': 'SD', 'Oracle Park': 'SF', 'T-Mobile Park': 'SEA',
    'Busch Stadium': 'STL', 'Tropicana Field': 'TB', 'Globe Life Field': 'TEX',
    'Rogers Centre': 'TOR', 'Nationals Park': 'WSH'
}

# =====================================================================
# HELPER: DYNAMIC LIVE WEATHER PARSER
# =====================================================================
def get_live_weather(lat, lon):
    """Fetches real-time localized metrics using Open-Meteo API."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}&current_weather=true"
            f"&temperature_unit=fahrenheit&windspeed_unit=mph"
        )
        res = requests.get(url, timeout=5).json()
        current = res.get('current_weather', {})
        return {
            'temp': current.get('temperature', 70),
            'wind_speed': current.get('windspeed', 0),
            'wind_dir': current.get('winddirection', 0)
        }
    except Exception:
        return {'temp': 70, 'wind_speed': 0, 'wind_dir': 0}

# =====================================================================
# SECTION 2: ADAPTIVE HISTORICAL FEATURES SOURCING
# =====================================================================
def get_advanced_hr_metrics(days_back=60):
    cache_dir = "cache"
    os.makedirs(cache_dir, exist_ok=True)
    all_days_data = []
    today = datetime.today()

    print(f"Syncing historical metrics from the last {days_back} days...")
    for i in range(1, days_back + 1):
        target_date = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        cache_file = os.path.join(cache_dir, f"statcast_{target_date}.csv")

        if os.path.exists(cache_file):
            all_days_data.append(pd.read_csv(cache_file))
            continue

        try:
            day_df = statcast(start_dt=target_date, end_dt=target_date)
            if day_df is not None and not day_df.empty:
                day_df.to_csv(cache_file, index=False)
                all_days_data.append(day_df)
            time.sleep(1)
        except Exception:
            continue

    if not all_days_data:
        raise ValueError("Critical Error: Missing training baseline vectors.")

    df = pd.concat(all_days_data, ignore_index=True)
    pa_df = df.dropna(subset=['events']).drop_duplicates(subset=['game_pk', 'batter', 'at_bat_number']).copy()
    pa_df['has_platoon_advantage'] = (pa_df['stand'] != pa_df['p_throws']).astype(int)

    pa_df['launch_speed'] = pd.to_numeric(pa_df['launch_speed'], errors='coerce').fillna(0)
    pa_df['launch_angle'] = pd.to_numeric(pa_df['launch_angle'], errors='coerce').fillna(0)
    pa_df['is_hr'] = (pa_df['events'] == 'home_run').astype(int)

    # Calculate Barrel Profile
    pa_df['is_barrel'] = ((pa_df['launch_speed'] >= 98) & 
                          (pa_df['launch_angle'] >= 26 - (pa_df['launch_speed'] - 98)) & 
                          (pa_df['launch_angle'] <= 30 + (pa_df['launch_speed'] - 98))).astype(int)

    # Map historical environment attributes
    pa_df['park_team'] = pa_df['home_team'] # Statcast uses team abbreviations directly
    pa_df['park_factor'] = pa_df['park_team'].map(PARK_HR_FACTORS).fillna(100)
    
    # Fill baseline weather placeholders for historical data where missing
    pa_df['temp'] = 71.0
    pa_df['wind_speed'] = 5.0

    # Build Player Vector Profiles
    batter_stats = pa_df.groupby('batter').agg(
        bat_pa_count=('events', 'count'),
        bat_hr_rate=('is_hr', 'mean'),
        bat_barrel_rate=('is_barrel', 'mean'),
        bat_hard_hit_rate=('launch_speed', lambda x: (x >= 95).mean())
    ).reset_index()

    pitcher_stats = pa_df.groupby('pitcher').agg(
        pitch_pa_count=('events', 'count'),
        pitch_hr_allowed_rate=('is_hr', 'mean'),
        pitch_barrel_allowed_rate=('is_barrel', 'mean'),
        pitch_hard_hit_allowed_rate=('launch_speed', lambda x: (x >= 95).mean())
    ).reset_index()

    return batter_stats, pitcher_stats, pa_df

# =====================================================================
# SECTION 3: DAILY LIVE LINEUPS FETCHING
# =====================================================================
def get_today_matchups():
    today_str = datetime.today().strftime('%Y-%m-%d')
    schedule = statsapi.schedule(date=today_str)
    matchups = []

    for game in schedule:
        if game.get('status') in ['Cancelled', 'Postponed']:
            continue
        
        game_id = game.get('game_pk')
        venue_name = game.get('venue_name', '')
        team_abbrev = VENUE_MAP.get(venue_name, 'Unknown')
        park_factor = PARK_HR_FACTORS.get(team_abbrev, 100)

        # Pull geolocation data via venue metadata or fallback to baseline coordinate mappings
        venue_id = game.get('venue_id', 0)
        venue_data = statsapi.get('venue', {'venueIds': str(venue_id)})
        try:
            coords = venue_data['venues'][0]['location']
            lat, lon = coords['latitude'], coords['longitude']
            weather = get_live_weather(lat, lon)
        except Exception:
            weather = {'temp': 71, 'wind_speed': 5}

        try:
            boxscore = statsapi.boxscore_data(game_id)
            for team_type in ['home', 'away']:
                opponent_type = 'away' if team_type == 'home' else 'home'
                
                pitcher_id = boxscore['teams'][opponent_type]['pitchers'][0] if boxscore['teams'][opponent_type]['pitchers'] else None
                p_throws = 'R'
                if pitcher_id:
                    pitcher_info = statsapi.get('people', {'personIds': str(pitcher_id)})
                    pitcher_person = pitcher_info.get('people', [{}])[0]
                    p_throws = pitcher_person.get('pitchHand', {}).get('code', 'R')
                
                batting_order = boxscore['teams'][team_type]['battingOrder']
                for order_idx, batter_id in enumerate(batting_order):
                    batter_info = statsapi.get('people', {'personIds': str(batter_id)})
                    batter_person = batter_info.get('people', [{}])[0]
                    b_stands = batter_person.get('batSide', {}).get('code', 'R')
                    
                    matchups.append({
                        'game_pk': game_id,
                        'batter': batter_id,
                        'batter_name': boxscore['teams'][team_type]['players'][f"ID{batter_id}"]['person']['fullName'],
                        'pitcher': pitcher_id,
                        'pitcher_name': boxscore['teams'][opponent_type]['players'][f"ID{pitcher_id}"]['person']['fullName'] if pitcher_id else "Unknown",
                        'has_platoon_advantage': int(b_stands != p_throws),
                        'park_factor': park_factor,
                        'temp': weather['temp'],
                        'wind_speed': weather['wind_speed']
                    })
        except Exception:
            continue

    return pd.DataFrame(matchups)

# =====================================================================
# SECTION 4: INFERENCE MODEL PROCESSING
# =====================================================================
def generate_daily_predictions():
    b_stats, p_stats, raw_pa = get_advanced_hr_metrics(days_back=60)
    
    train_df = raw_pa.merge(b_stats, on='batter', how='inner')
    train_df = train_df.merge(p_stats, on='pitcher', how='inner')
    
    # Updated Matrix including Weather and Park Vectors
    features = [
        'has_platoon_advantage', 'bat_pa_count', 'bat_hr_rate', 'bat_barrel_rate', 
        'bat_hard_hit_rate', 'pitch_pa_count', 'pitch_hr_allowed_rate', 
        'pitch_barrel_allowed_rate', 'pitch_hard_hit_allowed_rate',
        'park_factor', 'temp', 'wind_speed'
    ]
    
    X_train = train_df[features]
    y_train = train_df['is_hr']
    
    model = xgb.XGBClassifier(n_estimators=150, max_depth=5, learning_rate=0.04, eval_metric='logloss')
    model.fit(X_train, y_train)
    
    live_matchups = get_today_matchups()
    if live_matchups.empty:
        print("No games or lineups available for today.")
        return pd.DataFrame()

    # Join live matchups with player vectors where available (use inner to ensure features exist)
    live = live_matchups.merge(b_stats, on='batter', how='left')
    live = live.merge(p_stats, on='pitcher', how='left')

    # Fill missing numeric features with reasonable baselines
    for col in ['bat_pa_count', 'bat_hr_rate', 'bat_barrel_rate', 'bat_hard_hit_rate']:
        if col in live.columns:
            live[col] = live[col].fillna(0)
    for col in ['pitch_pa_count', 'pitch_hr_allowed_rate', 'pitch_barrel_allowed_rate', 'pitch_hard_hit_allowed_rate']:
        if col in live.columns:
            live[col] = live[col].fillna(live[col].mean() if not live[col].isna().all() else 0)

    live['park_factor'] = live['park_factor'].fillna(100)
    live['temp'] = live['temp'].fillna(71.0)
    live['wind_speed'] = live['wind_speed'].fillna(5.0)

    X_live = live[features]
    probs = model.predict_proba(X_live)[:, 1]
    live['pred_hr_prob'] = probs
    # Sort and present elite values
    rankings = live[['batter_name', 'pitcher_name', 'pred_hr_prob']].rename(columns={'pred_hr_prob': 'hr_probability'})
    top_5 = rankings.sort_values(by='hr_probability', ascending=False).head(5).reset_index(drop=True)

    print("\nTop 5 Daily Projected HR Probabilities:")
    print(top_5.to_string(index=False))

    # =====================================================================
    # DISCORD WEBHOOK INTEGRATION
    # =====================================================================
    WEBHOOK_URL = os.getenv("DISCORD_MLB_WEBHOOK", "YOUR_DISCORD_WEBHOOK_URL_HERE")

    if WEBHOOK_URL != "YOUR_DISCORD_WEBHOOK_URL_HERE":
        # Format rows for markdown table presentation
        table_rows = []
        for _, row in top_5.iterrows():
            pct = f"{row['hr_probability'] * 100:.1f}%"
            table_rows.append(f"| {row['batter_name'][:18]:<18} | {row['pitcher_name'][:18]:<18} | {pct:<6} |")

        table_str = "\n".join(table_rows)

        # Build a structured Discord code-block message
        message_content = (
            "**⚾ Today's Top 5 MLB Home Run Predictions**\n"
            "```\n"
            f"| {'Batter':<18} | {'Pitcher':<18} | {'Prob':<6} |\n"
            f"|{'-'*20}|{'-'*20}|{'-'*8}|\n"
            f"{table_str}\n"
            "```"
        )

        try:
            response = requests.post(WEBHOOK_URL, json={"content": message_content}, timeout=5)
            if response.status_code == 204:
                print("Successfully sent predictions to Discord.")
            else:
                print(f"Discord returned unexpected status: {response.status_code}")
        except Exception as e:
            print(f"Failed to transmit data to Discord webhook: {e}")

    # Return sorted predictions
    return live.sort_values('pred_hr_prob', ascending=False).reset_index(drop=True)

if __name__ == "__main__":
    generate_daily_predictions()
