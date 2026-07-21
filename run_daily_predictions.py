"""Run daily predictions for MLB HR model with Weather and Park Factors."""
# =====================================================================
# SECTION 1: IMPORTS
# =====================================================================
import argparse
import sys
from datetime import datetime
from pathlib import Path
import os
import time
import json as _json
try:
    import requests
except ImportError:
    requests = None
import pandas as pd
import statsapi
try:
    import xgboost as xgb
except ImportError:
    xgb = None
try:
    import lightgbm as lgb
except ImportError:
    lgb = None
try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.calibration import CalibratedClassifierCV
except ImportError:
    RandomForestClassifier = None
    CalibratedClassifierCV = None
from datetime import datetime, timedelta
from pybaseball import statcast

# Fix Pybaseball/Savant blocking by forcing a global browser user-agent header
import urllib.request
opener = urllib.request.build_opener()
opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')]
urllib.request.install_opener(opener)

if requests is None:
    class _RequestsFallback:
        @staticmethod
        def get(url, timeout=5):
            with urllib.request.urlopen(url, timeout=timeout) as response:
                text = response.read().decode('utf-8')
                class Resp:
                    def __init__(self, text, status_code):
                        self.text = text
                        self.status_code = status_code
                    def json(self):
                        return _json.loads(self.text)
                return Resp(text, response.getcode())

        @staticmethod
        def post(url, json=None, timeout=5):
            body = _json.dumps(json).encode('utf-8') if json is not None else None
            req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                class Resp:
                    def __init__(self, status_code):
                        self.status_code = status_code
                return Resp(response.getcode())

    requests = _RequestsFallback()

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


def persist_daily_predictions(predictions_df, date_str=None):
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    Path('data').mkdir(parents=True, exist_ok=True)
    filename = Path('data') / f'predictions_{date_str}.csv'
    predictions_df.to_csv(filename, index=False)
    print(f"Saved daily prediction history: {filename}")
    return filename


def send_discord_webhook(content=None, embeds=None, webhook_url=None):
    webhook_url = webhook_url or os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("Discord webhook not configured; skipping notification.")
        return False

    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    if not payload:
        print("Nothing to send to Discord; payload is empty.")
        return False

    try:
        response = requests.post(webhook_url, json=payload, timeout=5)
        if response.status_code == 204:
            print("Discord notification sent successfully.")
            return True
        print(f"Discord webhook returned status {response.status_code}: {getattr(response, 'text', '')}")
        return False
    except Exception as e:
        print(f"Failed to send Discord notification: {e}")
        return False


def load_or_fetch_statcast(date_str):
    cache_dir = Path('cache')
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f'statcast_{date_str}.csv'
    if cache_file.exists():
        return pd.read_csv(cache_file)

    try:
        stats = statcast(start_dt=date_str, end_dt=date_str)
        if stats is None or stats.empty:
            print(f"No Statcast data available for {date_str}.")
            return pd.DataFrame()
        stats.to_csv(cache_file, index=False)
        return stats
    except Exception as exc:
        print(f"Failed to fetch Statcast data for {date_str}: {exc}")
        return pd.DataFrame()


def evaluate_saved_predictions(date_str=None):
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    prediction_file = Path('data') / f'predictions_{date_str}.csv'
    if not prediction_file.exists():
        print(f"No saved prediction file found for {date_str}. Expected: {prediction_file}")
        return pd.DataFrame()

    preds = pd.read_csv(prediction_file)
    if preds.empty:
        print(f"Saved prediction file is empty for {date_str}.")
        return pd.DataFrame()

    actual = load_or_fetch_statcast(date_str)
    if actual is None or actual.empty:
        print(f"Unable to load actual Statcast outcomes for {date_str}.")
        return pd.DataFrame()

    actual = actual.dropna(subset=['game_pk', 'batter', 'pitcher', 'events']).copy()
    actual['is_hr'] = (actual['events'] == 'home_run').astype(int)
    actual = actual.groupby(['game_pk', 'batter', 'pitcher'], as_index=False).agg(
        actual_hr=('is_hr', 'max'),
        plate_apps=('is_hr', 'size')
    )

    merged = preds.merge(actual, on=['game_pk', 'batter', 'pitcher'], how='left')
    merged['actual_hr'] = merged['actual_hr'].fillna(0).astype(int)
    merged['plate_apps'] = merged['plate_apps'].fillna(0).astype(int)

    merged['brier_error'] = (merged['pred_hr_prob'] - merged['actual_hr']) ** 2
    brier_score = merged['brier_error'].mean()
    overall_hr_rate = merged['actual_hr'].mean()
    top_10 = merged.sort_values(by='pred_hr_prob', ascending=False).head(10)
    top_10_hits = top_10['actual_hr'].sum()
    top_10_rate = top_10['actual_hr'].mean() if not top_10.empty else 0.0

    bucket_edges = [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 1.0]
    merged['prob_bucket'] = pd.cut(merged['pred_hr_prob'], bins=bucket_edges, include_lowest=True)
    calibration = merged.groupby('prob_bucket', dropna=False).agg(
        predictions=('pred_hr_prob', 'count'),
        avg_pred_prob=('pred_hr_prob', 'mean'),
        actual_rate=('actual_hr', 'mean')
    ).reset_index()

    print(f"\nEvaluation for {date_str}")
    print(f"Predictions evaluated: {len(merged)}")
    print(f"Actual HR occurrence rate: {overall_hr_rate:.3f}")
    print(f"Brier score: {brier_score:.4f}")
    print(f"Top 10 predictions HR rate: {top_10_rate:.3f} ({int(top_10_hits)} HRs)")
    print("\nTop 10 predictions with actual outcomes:")
    print(top_10[['batter_name', 'pitcher_name', 'pred_hr_prob', 'actual_hr', 'plate_apps']].to_string(index=False))
    print("\nCalibration by probability bucket:")
    print(calibration.to_string(index=False))

    eval_file = Path('data') / f'evaluation_{date_str}.csv'
    merged.to_csv(eval_file, index=False)
    print(f"Saved evaluation details: {eval_file}")

    top_rows = []
    for idx, row in top_10.head(3).iterrows():
        top_rows.append(f"{idx+1}. {row['batter_name']} vs {row['pitcher_name']} ({row['pred_hr_prob']:.2f})")

    embed = {
        "title": f"MLB HR Prediction Evaluation — {date_str}",
        "color": 1127128,
        "fields": [
            {"name": "Predictions evaluated", "value": str(len(merged)), "inline": True},
            {"name": "Actual HR rate", "value": f"{overall_hr_rate:.3f}", "inline": True},
            {"name": "Brier score", "value": f"{brier_score:.4f}", "inline": True},
            {"name": "Top 10 HR rate", "value": f"{top_10_rate:.3f} ({int(top_10_hits)} HRs)", "inline": True},
            {"name": "Top 3 predictions", "value": "\n".join(top_rows) or "No top predictions available", "inline": False}
        ],
        "footer": {"text": "MLB HR Handicapper evaluation summary"},
        "timestamp": datetime.now().isoformat()
    }

    if os.getenv("DISCORD_NOTIFY_EVAL", "false").lower() == "true":
        send_discord_webhook(embeds=[embed])

    return merged

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
    pa_df['is_barrel'] = ((pa_df['launch_speed'] >= 98) & 
                          (pa_df['launch_angle'] >= 26 - (pa_df['launch_speed'] - 98)) & 
                          (pa_df['launch_angle'] <= 30 + (pa_df['launch_speed'] - 98))).astype(int)

    pa_df['is_hard_hit'] = (pa_df['launch_speed'] >= 95).astype(int)
    pa_df['is_sweet_spot'] = ((pa_df['launch_speed'] >= 90) & pa_df['launch_angle'].between(18, 32)).astype(int)
    pa_df['is_fly'] = pa_df.get('bb_type', '').fillna('').str.lower() == 'fly_ball'
    pa_df['is_ground'] = pa_df.get('bb_type', '').fillna('').str.lower() == 'ground_ball'

    pa_df['game_date'] = pd.to_datetime(pa_df.get('game_date', None), errors='coerce')
    pa_df.sort_values(['batter', 'game_date', 'at_bat_number'], inplace=True)
    pa_df['bat_15pa_barrel_rate'] = pa_df.groupby('batter')['is_barrel'].transform(lambda x: x.shift().rolling(15, min_periods=1).mean())
    pa_df['bat_30pa_barrel_rate'] = pa_df.groupby('batter')['is_barrel'].transform(lambda x: x.shift().rolling(30, min_periods=1).mean())
    pa_df['bat_15pa_hard_hit_rate'] = pa_df.groupby('batter')['is_hard_hit'].transform(lambda x: x.shift().rolling(15, min_periods=1).mean())
    pa_df['bat_30pa_hard_hit_rate'] = pa_df.groupby('batter')['is_hard_hit'].transform(lambda x: x.shift().rolling(30, min_periods=1).mean())
    pa_df['bat_15pa_sweet_spot_rate'] = pa_df.groupby('batter')['is_sweet_spot'].transform(lambda x: x.shift().rolling(15, min_periods=1).mean())
    pa_df['bat_30pa_sweet_spot_rate'] = pa_df.groupby('batter')['is_sweet_spot'].transform(lambda x: x.shift().rolling(30, min_periods=1).mean())
    pa_df['bat_15pa_fb_rate'] = pa_df.groupby('batter')['is_fly'].transform(lambda x: x.shift().rolling(15, min_periods=1).mean())
    pa_df['bat_30pa_fb_rate'] = pa_df.groupby('batter')['is_fly'].transform(lambda x: x.shift().rolling(30, min_periods=1).mean())

    pa_df.sort_values(['pitcher', 'game_date', 'at_bat_number'], inplace=True)
    pa_df['pit_15pa_hr_rate'] = pa_df.groupby('pitcher')['is_hr'].transform(lambda x: x.shift().rolling(15, min_periods=1).mean())
    pa_df['pit_30pa_hr_rate'] = pa_df.groupby('pitcher')['is_hr'].transform(lambda x: x.shift().rolling(30, min_periods=1).mean())
    pa_df['pit_15pa_barrel_allowed_rate'] = pa_df.groupby('pitcher')['is_barrel'].transform(lambda x: x.shift().rolling(15, min_periods=1).mean())
    pa_df['pit_30pa_barrel_allowed_rate'] = pa_df.groupby('pitcher')['is_barrel'].transform(lambda x: x.shift().rolling(30, min_periods=1).mean())
    pa_df['pit_15pa_hard_hit_allowed_rate'] = pa_df.groupby('pitcher')['is_hard_hit'].transform(lambda x: x.shift().rolling(15, min_periods=1).mean())
    pa_df['pit_30pa_hard_hit_allowed_rate'] = pa_df.groupby('pitcher')['is_hard_hit'].transform(lambda x: x.shift().rolling(30, min_periods=1).mean())
    pa_df['pit_15pa_fb_allowed_rate'] = pa_df.groupby('pitcher')['is_fly'].transform(lambda x: x.shift().rolling(15, min_periods=1).mean())
    pa_df['pit_30pa_fb_allowed_rate'] = pa_df.groupby('pitcher')['is_fly'].transform(lambda x: x.shift().rolling(30, min_periods=1).mean())

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
        bat_hard_hit_rate=('is_hard_hit', 'mean'),
        bat_15pa_barrel_rate=('bat_15pa_barrel_rate', 'last'),
        bat_30pa_barrel_rate=('bat_30pa_barrel_rate', 'last'),
        bat_15pa_hard_hit_rate=('bat_15pa_hard_hit_rate', 'last'),
        bat_30pa_hard_hit_rate=('bat_30pa_hard_hit_rate', 'last'),
        bat_15pa_sweet_spot_rate=('bat_15pa_sweet_spot_rate', 'last'),
        bat_30pa_sweet_spot_rate=('bat_30pa_sweet_spot_rate', 'last'),
        bat_15pa_fb_rate=('bat_15pa_fb_rate', 'last'),
        bat_30pa_fb_rate=('bat_30pa_fb_rate', 'last')
    ).reset_index()

    pitcher_stats = pa_df.groupby('pitcher').agg(
        pitch_pa_count=('events', 'count'),
        pitch_hr_allowed_rate=('is_hr', 'mean'),
        pitch_barrel_allowed_rate=('is_barrel', 'mean'),
        pitch_hard_hit_allowed_rate=('is_hard_hit', 'mean'),
        pitch_15pa_hr_rate=('pit_15pa_hr_rate', 'last'),
        pitch_30pa_hr_rate=('pit_30pa_hr_rate', 'last'),
        pitch_15pa_barrel_allowed_rate=('pit_15pa_barrel_allowed_rate', 'last'),
        pitch_30pa_barrel_allowed_rate=('pit_30pa_barrel_allowed_rate', 'last'),
        pitch_15pa_hard_hit_allowed_rate=('pit_15pa_hard_hit_allowed_rate', 'last'),
        pitch_30pa_hard_hit_allowed_rate=('pit_30pa_hard_hit_allowed_rate', 'last'),
        pitch_15pa_fb_allowed_rate=('pit_15pa_fb_allowed_rate', 'last'),
        pitch_30pa_fb_allowed_rate=('pit_30pa_fb_allowed_rate', 'last')
    ).reset_index()

    return batter_stats, pitcher_stats, pa_df

# =====================================================================
# SECTION 3: DAILY LIVE LINEUPS FETCHING
# =====================================================================
def get_today_matchups():
    today_str = datetime.today().strftime('%Y-%m-%d')
    schedule = statsapi.schedule(date=today_str)
    print(f"DEBUG: schedule returned {len(schedule)} games for {today_str}")
    matchups = []

    for game in schedule:
        if game.get('status') in ['Cancelled', 'Postponed']:
            print(f"DEBUG: skipping cancelled/postponed game {game.get('game_id') or game.get('game_pk')}")
            continue
        
        game_id = game.get('game_pk') or game.get('game_id')
        if not game_id:
            print(f"DEBUG: missing game_id/game_pk for schedule entry: {game}")
            continue
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
            if not boxscore:
                continue

            for team_type in ['home', 'away']:
                opponent_type = 'away' if team_type == 'home' else 'home'
                team_info = boxscore.get(team_type, {})
                opp_info = boxscore.get(opponent_type, {})
                if not team_info or not opp_info:
                    continue

                pitchers = opp_info.get('pitchers') or []
                pitcher_id = pitchers[0] if pitchers else None
                p_throws = 'R'
                if pitcher_id:
                    pitcher_player_info = opp_info.get('players', {}).get(f"ID{pitcher_id}", {})
                    p_throws = pitcher_player_info.get('stats', {}).get('pitching', {}).get('pitchHand', 'R')
                    if not p_throws:
                        p_throws = 'R'

                batting_order = team_info.get('battingOrder') or []
                for order_idx, batter_id in enumerate(batting_order):
                    batter_player_info = team_info.get('players', {}).get(f"ID{batter_id}", {})
                    batter_person = batter_player_info.get('person', {})
                    batter_name = batter_person.get('fullName', 'Unknown Batter')

                    b_stands = batter_player_info.get('batSide', {}).get('code')
                    if not b_stands:
                        b_stands = batter_player_info.get('stats', {}).get('batting', {}).get('batSide', 'R')
                    if not b_stands:
                        b_stands = 'R'

                    pitcher_player = opp_info.get('players', {}).get(f"ID{pitcher_id}", {}).get('person', {}) if pitcher_id else {}
                    pitcher_name = pitcher_player.get('fullName', 'Unknown Pitcher')

                    matchups.append({
                        'game_pk': game_id,
                        'batter': batter_id,
                        'batter_name': batter_name,
                        'pitcher': pitcher_id,
                        'pitcher_name': pitcher_name,
                        'has_platoon_advantage': int(b_stands != p_throws),
                        'park_factor': park_factor,
                        'temp': weather['temp'],
                        'wind_speed': weather['wind_speed']
                    })
        except Exception as e:
            print(f"Warning: failed to build matchups for game {game_id}: {e}")
            continue

    return pd.DataFrame(matchups)

# =====================================================================
# SECTION 4: INFERENCE MODEL PROCESSING
# =====================================================================
def generate_daily_predictions():
    b_stats, p_stats, raw_pa = get_advanced_hr_metrics(days_back=60)
    
    train_df = raw_pa.merge(b_stats, on='batter', how='inner')
    train_df = train_df.merge(p_stats, on='pitcher', how='inner')
    
    # Updated Matrix including Weather, Park Factors, and recent performance vectors
    features = [
        'has_platoon_advantage',
        'bat_pa_count', 'bat_hr_rate', 'bat_barrel_rate', 'bat_hard_hit_rate',
        'bat_15pa_barrel_rate', 'bat_30pa_barrel_rate',
        'bat_15pa_hard_hit_rate', 'bat_30pa_hard_hit_rate',
        'bat_15pa_sweet_spot_rate', 'bat_30pa_sweet_spot_rate',
        'bat_15pa_fb_rate', 'bat_30pa_fb_rate',
        'pitch_pa_count', 'pitch_hr_allowed_rate', 'pitch_barrel_allowed_rate',
        'pitch_hard_hit_allowed_rate', 'pitch_15pa_hr_rate', 'pitch_30pa_hr_rate',
        'pitch_15pa_barrel_allowed_rate', 'pitch_30pa_barrel_allowed_rate',
        'pitch_15pa_hard_hit_allowed_rate', 'pitch_30pa_hard_hit_allowed_rate',
        'pitch_15pa_fb_allowed_rate', 'pitch_30pa_fb_allowed_rate',
        'park_factor', 'temp', 'wind_speed'
    ]
    
    X_train = train_df[features]
    y_train = train_df['is_hr']
    
    if xgb is not None:
        model = xgb.XGBClassifier(
            n_estimators=150,
            max_depth=5,
            learning_rate=0.04,
            eval_metric='logloss',
            use_label_encoder=False
        )
    elif lgb is not None:
        model = lgb.LGBMClassifier(
            n_estimators=150,
            max_depth=5,
            learning_rate=0.04
        )
    elif RandomForestClassifier is not None:
        model = RandomForestClassifier(
            n_estimators=150,
            max_depth=5,
            random_state=42
        )
    else:
        raise ImportError("Missing required ML package: install xgboost, lightgbm, or scikit-learn.")

    if CalibratedClassifierCV is not None:
        model = CalibratedClassifierCV(model, cv=3, method='sigmoid')

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
    live['model_name'] = getattr(getattr(model, 'base_estimator_', model), '__class__', model).__name__
    live['prediction_timestamp'] = datetime.now().isoformat()

    persist_daily_predictions(live[['game_pk', 'batter', 'batter_name', 'pitcher', 'pitcher_name',
                                    'has_platoon_advantage', 'park_factor', 'temp', 'wind_speed',
                                    'pred_hr_prob', 'model_name', 'prediction_timestamp']])

    # Sort and present elite values
    rankings = live[['batter_name', 'pitcher_name', 'pred_hr_prob']].rename(columns={'pred_hr_prob': 'hr_probability'})
    top_5 = rankings.sort_values(by='hr_probability', ascending=False).head(5).reset_index(drop=True)

    print("\nTop 5 Daily Projected HR Probabilities:")
    print(top_5.to_string(index=False))

    # =====================================================================
    # DISCORD WEBHOOK INTEGRATION
    # =====================================================================
    target_date = datetime.today().strftime('%Y-%m-%d')
    WEBHOOK_URL = os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_MLB_WEBHOOK or DISCORD_WEBHOOK_URL not set; configure env var or GitHub secret")

    # Assuming 'top_5' is generated by your model evaluation step above
    # representation check to prevent crashes if top_5 isn't initialized yet
    if 'top_5' in locals() or 'top_5' in globals():
        if WEBHOOK_URL != "https://discord.com/api/webhooks/1525525618861543654/jBzZ7vTarJs-j2apC7Ws2M29cF5aaJ9-0JkvdyyK9aJUJRziU9MXqfHyzx0roW4HVHIZ":
            # Format rows for markdown table presentation
            table_rows = []
            for _, row in top_5.iterrows():
                pct = f"{row['hr_probability'] * 100:.1f}%"
                table_rows.append(f"| {row['batter_name'][:18]:<18} | {row['pitcher_name'][:18]:<18} | {pct:<6} |")

            table_str = "\n".join(table_rows)

            # Build a structured Discord code-block message
            message_content = (
                f"**⚾ Today's Top 5 MLB Home Run Predictions ({target_date})**\n"
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
    else:
        print("Model tracking skipped: 'top_5' data framework is empty or not yet generated.")

# ==========================================
# NEW FEATURE: LIVE HOME RUN WATCHER
# ==========================================
def monitor_live_home_runs():
    """Loop indefinitely, checking live game data for home runs and alerting Discord."""
    WEBHOOK_URL = os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_MLB_WEBHOOK or DISCORD_WEBHOOK_URL not set; configure env var or GitHub secret")

    print("🚀 Monitoring started: Waiting for live MLB home run events...")
    processed_home_runs = set()

    while True:
        try:
            today_str = datetime.today().strftime('%m/%d/%Y')
            games = statsapi.schedule(date=today_str) or []
            for game in games:
                if game.get('status') != 'In Progress':
                    continue
                game_id = game.get('game_pk') or game.get('game_id')
                if not game_id:
                    continue
                play_by_play = statsapi.get('game', {'gamePk': game_id}) or {}
                all_plays = play_by_play.get('liveData', {}).get('plays', {}).get('allPlays', [])
                for play in all_plays:
                    result = play.get('result', {})
                    event_id = play.get('about', {}).get('playId')
                    if not event_id:
                        continue

                    event_name = str(result.get('event', '')).lower()
                    if event_name not in ('home run', 'home_run', 'homerun', 'hr'):
                        continue
                    if event_id in processed_home_runs:
                        continue

                    description = result.get('description', 'A home run was hit!')
                    inning_half = play.get('about', {}).get('halfInning', '')
                    num_inning = play.get('about', {}).get('inning', '')
                    batter_name = play.get('matchup', {}).get('batter', {}).get('fullName') or ''
                    pitcher_name = play.get('matchup', {}).get('pitcher', {}).get('fullName') or ''
                    game_display = f"{game.get('away_name','Away')} @ {game.get('home_name','Home')}"

                    message_lines = [
                        "🚨 **LIVE HOME RUN ALERT** 🚨",
                        f"🏟️ *{game_display}* ({inning_half} {num_inning})",
                        f"⚾ {description}"
                    ]
                    if batter_name:
                        message_lines.append(f"👤 Batter: {batter_name}")
                    if pitcher_name:
                        message_lines.append(f"🎯 Pitcher: {pitcher_name}")

                    payload = {"content": "\n".join(message_lines)}

                    try:
                        response = requests.post(WEBHOOK_URL, json=payload, timeout=5)
                        if response.status_code == 204:
                            print(f"Live HR alert sent for play {event_id} in {game_display}.")
                        else:
                            print(f"Live HR webhook returned status {response.status_code}: {response.text if hasattr(response, 'text') else ''}")
                    except Exception as e:
                        print("Webhook post failed:", e)
                    finally:
                        processed_home_runs.add(event_id)
            time.sleep(30)
        except Exception as e:
            print("Error checking live feeds:", e)
            time.sleep(10)


def pull_games(date_str):
    """Fetch and return Statcast data for a given date (YYYY-MM-DD)."""
    print(f"Initiating Statcast pitch metric ingestion tracking for: {date_str}")
    try:
        df = statcast(start_dt=date_str, end_dt=date_str)
        return df
    except Exception as e:
        print(f"Error fetching data via pybaseball module: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="MLB Daily HR Handicapper CLI Core Process")
    parser.add_argument("--today", action="store_true", help="Ingest Statcast data files for today's active games")
    parser.add_argument("--date", type=str, help="Ingest Statcast records using explicit format: YYYY-MM-DD")
    parser.add_argument("--live", action="store_true", help="Launch real-time Discord home run notifications watch script")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate saved predictions against actual results")
    parser.add_argument("--eval-date", type=str, help="Date to evaluate predictions for, format YYYY-MM-DD")
    parser.add_argument("--notify-eval", action="store_true", help="Send evaluation summary to Discord if webhook is configured")

    args = parser.parse_args()

    if args.live:
        monitor_live_home_runs()
        return

    if args.today:
        date_str = datetime.today().strftime('%Y-%m-%d')
        pull_games(date_str)
        return

    if args.date and not args.evaluate:
        pull_games(args.date)
        return

    if args.evaluate:
        if args.notify_eval:
            os.environ["DISCORD_NOTIFY_EVAL"] = "true"
        evaluate_saved_predictions(args.eval_date or args.date)
        return

    generate_daily_predictions()


if __name__ == "__main__":
    main()
