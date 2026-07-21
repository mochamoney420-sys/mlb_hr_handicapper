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
import math
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
    from sklearn.model_selection import TimeSeriesSplit
except ImportError:
    RandomForestClassifier = None
    CalibratedClassifierCV = None
    TimeSeriesSplit = None
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

# Compass bearing (degrees from North) from home plate toward center field.
# Positive wind_out_component = wind blowing toward CF = helps HRs.
STADIUM_CF_BEARING = {
    'ARI': 330, 'ATL': 30,  'BAL': 90,  'BOS': 90,  'CHC': 30,
    'CWS': 5,   'CIN': 355, 'CLE': 5,   'COL': 335, 'DET': 5,
    'HOU': 25,  'KC': 5,    'LAA': 5,   'LAD': 348, 'MIA': 5,
    'MIL': 355, 'MIN': 5,   'NYM': 5,   'NYY': 5,   'OAK': 330,
    'PHI': 350, 'PIT': 320, 'SD': 310,  'SF': 60,   'SEA': 330,
    'STL': 5,   'TB': 5,    'TEX': 25,  'TOR': 10,  'WSH': 355
}

# Sportsbook tiers for RLM detection and sharp consensus weighting
SHARP_BOOKS = {'pinnacle', 'circasports', 'betonlineag', 'betus', 'betrivers', 'pointsbetusn', 'lowvig', 'bookmaker'}
SQUARE_BOOKS = {'fanduel', 'draftkings', 'betmgm', 'williamhill_us', 'barstool', 'unibet_us', 'mybookieag', 'bovada', 'caesars', 'wynnbet', 'betfred', 'superbook'}

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


def fetch_hr_prop_odds():
    """Fetch live HR prop lines from The Odds API. Returns {player_name: devigged_prob}.
    Set ODDS_API_KEY in .env to enable real market edge calculation."""
    api_key = os.getenv('ODDS_API_KEY')
    if not api_key:
        return {}
    try:
        url = (
            f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
            f"?apiKey={api_key}&regions=us,us2&markets=batter_home_runs"
            f"&oddsFormat=american&dateFormat=iso"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print(f"Odds API returned {resp.status_code}")
            return {}

        # Collect all book lines per player
        player_all_odds = {}  # name -> {book_key: american_odds}
        for game in resp.json():
            for book in game.get('bookmakers', []):
                book_key = book.get('key', '')
                for market in book.get('markets', []):
                    if market.get('key') != 'batter_home_runs':
                        continue
                    for outcome in market.get('outcomes', []):
                        name = outcome.get('name', '').strip()
                        price = outcome.get('price')
                        if not name or price is None:
                            continue
                        if name not in player_all_odds:
                            player_all_odds[name] = {}
                        player_all_odds[name][book_key] = price

        # Consensus devigged probability — sharp books weighted 2x
        player_probs = {}
        for name, book_odds in player_all_odds.items():
            weighted_probs = []
            for bk, odds in book_odds.items():
                raw_implied = abs(odds) / (abs(odds) + 100) if odds < 0 else 100 / (odds + 100)
                devigged = raw_implied * 0.952
                weight = 2 if bk in SHARP_BOOKS else 1
                weighted_probs.extend([devigged] * weight)
            if weighted_probs:
                player_probs[name] = round(sum(weighted_probs) / len(weighted_probs), 4)

        n_pairs = sum(len(v) for v in player_all_odds.values())
        print(f"Odds API: {n_pairs} book-player pairs across {len(player_probs)} players")
        return player_probs
    except Exception as e:
        print(f"Odds API fetch failed: {e}")
        return {}


def fetch_hr_prop_odds_raw():
    """Fetch HR prop lines from ALL sportsbooks. Returns {player: {book_key: american_odds}}.
    Used for RLM monitoring and per-book line movement tracking."""
    api_key = os.getenv('ODDS_API_KEY')
    if not api_key:
        return {}
    try:
        url = (
            f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
            f"?apiKey={api_key}&regions=us,us2&markets=batter_home_runs"
            f"&oddsFormat=american&dateFormat=iso"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return {}
        raw = {}
        for game in resp.json():
            for book in game.get('bookmakers', []):
                book_key = book.get('key', '')
                for market in book.get('markets', []):
                    if market.get('key') != 'batter_home_runs':
                        continue
                    for outcome in market.get('outcomes', []):
                        name = outcome.get('name', '').strip()
                        price = outcome.get('price')
                        if not name or price is None:
                            continue
                        if name not in raw:
                            raw[name] = {}
                        raw[name][book_key] = price
        return raw
    except Exception as e:
        print(f"Odds raw fetch failed: {e}")
        return {}


def save_odds_snapshot(odds_raw, date_str=None):
    """Append a timestamped per-book odds snapshot to a JSONL file."""
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    Path('data').mkdir(parents=True, exist_ok=True)
    entry = {'timestamp': datetime.now().isoformat(), 'odds': odds_raw}
    with open(Path('data') / f'odds_snapshots_{date_str}.jsonl', 'a') as f:
        f.write(_json.dumps(entry) + '\n')


def detect_rlm(current_odds, previous_odds, watch_batters):
    """Detect reverse line movement or sharp/public divergence on watched batters.
    Returns list of (batter_name, sharp_move, square_move, signal) tuples."""
    alerts = []
    for batter in watch_batters:
        curr = current_odds.get(batter)
        prev = previous_odds.get(batter)
        if not curr or not prev:
            continue

        sharp_moves, square_moves, all_moves = [], [], []
        for book, price in curr.items():
            if book not in prev:
                continue
            move = price - prev[book]
            all_moves.append(move)
            if book in SHARP_BOOKS:
                sharp_moves.append(move)
            elif book in SQUARE_BOOKS:
                square_moves.append(move)

        if not all_moves:
            continue

        max_move = max(abs(m) for m in all_moves)
        if max_move < 3:
            continue

        sharp_avg = sum(sharp_moves) / len(sharp_moves) if sharp_moves else None
        square_avg = sum(square_moves) / len(square_moves) if square_moves else None

        if sharp_avg is not None and square_avg is not None and abs(sharp_avg) > 2 and abs(square_avg) > 2:
            if sharp_avg * square_avg < 0:  # moving in opposite directions
                signal = f"RLM — sharps: {sharp_avg:+.1f} / public: {square_avg:+.1f} across {len(all_moves)} books"
                alerts.append((batter, sharp_avg, square_avg, signal))
        elif max_move >= 8:
            avg_move = sum(all_moves) / len(all_moves)
            signal = f"STEAM — {avg_move:+.1f} pts avg across {len(all_moves)} books"
            alerts.append((batter, avg_move, avg_move, signal))

    return alerts


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
    pa_df['release_speed'] = pd.to_numeric(pa_df.get('release_speed', 0), errors='coerce').fillna(0)
    pa_df['is_fly'] = pa_df.get('bb_type', '').fillna('').str.lower() == 'fly_ball'
    pa_df['is_ground'] = pa_df.get('bb_type', '').fillna('').str.lower() == 'ground_ball'
    pa_df['is_xbh'] = pa_df['events'].isin(['double', 'triple', 'home_run']).astype(int)
    pa_df['bat_spray_angle'] = pd.to_numeric(
        pa_df['spray_angle'] if 'spray_angle' in pa_df.columns else pd.Series(0, index=pa_df.index),
        errors='coerce'
    ).fillna(0)
    pa_df['is_pulled_fly'] = (
        pa_df['is_fly'] & (
            ((pa_df['stand'] == 'R') & (pa_df['bat_spray_angle'] < -15)) |
            ((pa_df['stand'] == 'L') & (pa_df['bat_spray_angle'] > 15))
        )
    ).astype(int)

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
    pa_df['wind_out_component'] = 0.0  # no directional wind data for historical training rows

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
        bat_30pa_fb_rate=('bat_30pa_fb_rate', 'last'),
        bat_total_hr=('is_hr', 'sum'),
        bat_total_fb=('is_fly', 'sum'),
        bat_ev90=('launch_speed', lambda x: float(x[x > 0].quantile(0.90)) if (x > 0).sum() > 5 else 88.0),
        bat_iso_proxy=('is_xbh', 'mean'),
        bat_pulled_fly_count=('is_pulled_fly', 'sum')
    ).reset_index()
    batter_stats['bat_hr_fb_rate'] = batter_stats['bat_total_hr'] / batter_stats['bat_total_fb'].clip(lower=1)
    batter_stats['bat_pull_rate'] = batter_stats['bat_pulled_fly_count'] / batter_stats['bat_total_fb'].clip(lower=1)
    today_date = pd.Timestamp(datetime.today().date())
    _last_bat = pa_df.groupby('batter')['game_date'].max().reset_index()
    _last_bat['bat_days_since_last_game'] = (today_date - _last_bat['game_date']).dt.days.clip(0, 30)
    batter_stats = batter_stats.merge(_last_bat[['batter', 'bat_days_since_last_game']], on='batter', how='left')
    batter_stats['bat_days_since_last_game'] = batter_stats['bat_days_since_last_game'].fillna(7)

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
        pitch_30pa_fb_allowed_rate=('pit_30pa_fb_allowed_rate', 'last'),
        pitch_total_hr=('is_hr', 'sum'),
        pitch_total_fb=('is_fly', 'sum'),
        pitch_avg_velocity=('release_speed', lambda x: float(x[x > 70].mean()) if (x > 70).sum() > 0 else 92.0)
    ).reset_index()
    pitcher_stats['pitch_hr_fb_allowed_rate'] = pitcher_stats['pitch_total_hr'] / pitcher_stats['pitch_total_fb'].clip(lower=1)
    _last_pit = pa_df.groupby('pitcher')['game_date'].max().reset_index()
    _last_pit['pitch_days_since_last_start'] = (today_date - _last_pit['game_date']).dt.days.clip(0, 30)
    pitcher_stats = pitcher_stats.merge(_last_pit[['pitcher', 'pitch_days_since_last_start']], on='pitcher', how='left')
    pitcher_stats['pitch_days_since_last_start'] = pitcher_stats['pitch_days_since_last_start'].fillna(5)

    return batter_stats, pitcher_stats, pa_df

# =====================================================================
# SECTION 3: DAILY LIVE LINEUPS FETCHING
# =====================================================================
def get_today_matchups():
    today_str = datetime.today().strftime('%Y-%m-%d')
    schedule = statsapi.schedule(date=today_str)
    print(f"Schedule: {len(schedule)} games for {today_str}")
    matchups = []

    for game in schedule:
        if game.get('status') in ['Cancelled', 'Postponed']:
            continue

        game_id = game.get('game_pk') or game.get('game_id')
        if not game_id:
            continue

        # Parse game start time from UTC ISO datetime
        game_time_str = ''
        raw_dt = game.get('game_datetime', '')
        if raw_dt:
            try:
                gdt = datetime.strptime(raw_dt[:19], '%Y-%m-%dT%H:%M:%S')
                gdt_et = gdt - timedelta(hours=4)  # UTC to EDT
                hr = gdt_et.hour % 12 or 12
                ampm = 'PM' if gdt_et.hour >= 12 else 'AM'
                game_time_str = f"{hr}:{gdt_et.minute:02d} {ampm}"
            except Exception:
                game_time_str = game.get('game_time', '')
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
            weather = {'temp': 71, 'wind_speed': 5, 'wind_dir': 0}
        cf_bearing = STADIUM_CF_BEARING.get(team_abbrev, 0)
        _wind_angle = math.radians(weather.get('wind_dir', 0) - cf_bearing)
        wind_out_component = round(weather.get('wind_speed', 0) * math.cos(_wind_angle), 2)

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
                        'game_time': game_time_str,
                        'batter': batter_id,
                        'batter_name': batter_name,
                        'pitcher': pitcher_id,
                        'pitcher_name': pitcher_name,
                        'has_platoon_advantage': int(b_stands != p_throws),
                        'batting_order_slot': order_idx + 1,
                        'wind_out_component': wind_out_component,
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
def load_feedback_weights(train_df, days_lookback=30):
    """Load historical evaluation CSVs and upweight training rows for
    batters/pitchers the model consistently missed. Returns a weight
    array aligned with train_df's index."""
    weights = pd.Series(1.0, index=train_df.index)

    cutoff = datetime.today() - timedelta(days=days_lookback)
    recent_evals = []

    # Load structured evaluation CSVs
    for f in sorted(Path('data').glob('evaluation_*.csv')):
        try:
            file_date = datetime.strptime(f.stem.replace('evaluation_', ''), '%Y-%m-%d')
            if file_date >= cutoff:
                recent_evals.append(pd.read_csv(f))
        except Exception:
            continue

    # Load live game feedback CSVs (real-time HR observations from the watcher bot)
    for f in sorted(Path('data').glob('live_feedback_*.csv')):
        try:
            file_date = datetime.strptime(f.stem.replace('live_feedback_', ''), '%Y-%m-%d')
            if file_date >= cutoff:
                fb = pd.read_csv(f)
                fb['actual_hr'] = 1
                fb['pred_hr_prob'] = pd.to_numeric(fb.get('model_prob', 0), errors='coerce').fillna(0)
                recent_evals.append(fb[['batter', 'pitcher', 'actual_hr', 'pred_hr_prob']])
        except Exception:
            continue

    if not recent_evals:
        return weights.values

    eval_df = pd.concat(recent_evals, ignore_index=True)
    eval_df['batter'] = pd.to_numeric(eval_df.get('batter', None), errors='coerce')
    eval_df['pitcher'] = pd.to_numeric(eval_df.get('pitcher', None), errors='coerce')
    eval_df = eval_df.dropna(subset=['batter', 'pitcher', 'actual_hr', 'pred_hr_prob'])

    # Missed HR: batter actually hit HR but model gave low probability
    eval_df['missed_hr'] = ((eval_df['actual_hr'] == 1) & (eval_df['pred_hr_prob'] < 0.15)).astype(int)
    # False positive: high probability but no HR
    eval_df['false_pos'] = ((eval_df['actual_hr'] == 0) & (eval_df['pred_hr_prob'] > 0.25)).astype(int)

    batter_feedback = eval_df.groupby('batter').agg(
        bat_missed=('missed_hr', 'sum'),
        bat_false_pos=('false_pos', 'sum')
    ).reset_index()
    pitcher_feedback = eval_df.groupby('pitcher').agg(
        pit_missed=('missed_hr', 'sum')
    ).reset_index()

    merged = train_df[['batter', 'pitcher']].copy().reset_index()
    merged = merged.merge(batter_feedback, on='batter', how='left')
    merged = merged.merge(pitcher_feedback, on='pitcher', how='left')
    merged = merged.fillna(0)

    # Upweight missed HRs, slightly downweight chronic false positives
    boost = 1.0 + (merged['bat_missed'] * 0.6) + (merged['pit_missed'] * 0.4) - (merged['bat_false_pos'] * 0.1)
    boost = boost.clip(lower=0.5, upper=6.0)

    result = pd.Series(boost.values, index=merged['index'])
    return result.reindex(train_df.index).fillna(1.0).values


def generate_daily_predictions():
    b_stats, p_stats, raw_pa = get_advanced_hr_metrics(days_back=60)

    # Drop columns from raw_pa that also exist in b_stats/p_stats to avoid
    # pandas creating _x/_y suffixes on merge (rolling features live in both).
    _b_drop = [c for c in b_stats.columns if c in raw_pa.columns and c != 'batter']
    _p_drop = [c for c in p_stats.columns if c in raw_pa.columns and c != 'pitcher']
    _drop_all = list(set(_b_drop + _p_drop))
    train_df = raw_pa.drop(columns=_drop_all, errors='ignore').merge(b_stats, on='batter', how='inner')
    train_df = train_df.merge(p_stats, on='pitcher', how='inner')

    # Auto-evaluate yesterday's predictions to feed the learning loop
    yesterday_str = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    if (Path('data') / f'predictions_{yesterday_str}.csv').exists() and \
       not (Path('data') / f'evaluation_{yesterday_str}.csv').exists():
        print(f"Auto-evaluating {yesterday_str} predictions for learning feedback...")
        try:
            evaluate_saved_predictions(yesterday_str)
        except Exception as _e:
            print(f"Auto-evaluation skipped: {_e}")

    sample_weights = load_feedback_weights(train_df)
    missed_count = int((sample_weights > 1.2).sum())
    print(f"Feedback weights loaded — {missed_count} training rows upweighted from past misses.")
    
    # Updated Matrix including Weather, Park Factors, and recent performance vectors
    features = [
        'has_platoon_advantage',
        'bat_pa_count', 'bat_hr_rate', 'bat_barrel_rate', 'bat_hard_hit_rate',
        'bat_hr_fb_rate', 'bat_pull_rate', 'bat_ev90', 'bat_iso_proxy', 'bat_days_since_last_game',
        'bat_15pa_barrel_rate', 'bat_30pa_barrel_rate',
        'bat_15pa_hard_hit_rate', 'bat_30pa_hard_hit_rate',
        'bat_15pa_sweet_spot_rate', 'bat_30pa_sweet_spot_rate',
        'bat_15pa_fb_rate', 'bat_30pa_fb_rate',
        'pitch_pa_count', 'pitch_hr_allowed_rate', 'pitch_barrel_allowed_rate',
        'pitch_hard_hit_allowed_rate', 'pitch_hr_fb_allowed_rate', 'pitch_days_since_last_start',
        'pitch_avg_velocity',
        'pitch_15pa_hr_rate', 'pitch_30pa_hr_rate',
        'pitch_15pa_barrel_allowed_rate', 'pitch_30pa_barrel_allowed_rate',
        'pitch_15pa_hard_hit_allowed_rate', 'pitch_30pa_hard_hit_allowed_rate',
        'pitch_15pa_fb_allowed_rate', 'pitch_30pa_fb_allowed_rate',
        'park_factor', 'temp', 'wind_speed', 'wind_out_component'
    ]
    
    X_train = train_df[features]
    y_train = train_df['is_hr']

    cv_splitter = TimeSeriesSplit(n_splits=3) if TimeSeriesSplit is not None else 3
    base_models = []
    model_names = []

    if xgb is not None:
        base_models.append(xgb.XGBClassifier(
            n_estimators=150, max_depth=5, learning_rate=0.04,
            eval_metric='logloss'
        ))
        model_names.append('XGBoost')

    if lgb is not None:
        base_models.append(lgb.LGBMClassifier(
            n_estimators=150, max_depth=5, learning_rate=0.04, verbose=-1
        ))
        model_names.append('LightGBM')

    if not base_models:
        if RandomForestClassifier is not None:
            base_models.append(RandomForestClassifier(n_estimators=150, max_depth=5, random_state=42))
            model_names.append('RandomForest')
        else:
            raise ImportError("Missing required ML package: install xgboost, lightgbm, or scikit-learn.")

    trained_models = []
    for m in base_models:
        if CalibratedClassifierCV is not None:
            m = CalibratedClassifierCV(m, cv=cv_splitter, method='isotonic')
        try:
            m.fit(X_train, y_train, sample_weight=sample_weights)
        except TypeError:
            m.fit(X_train, y_train)
        trained_models.append(m)

    print(f"Ensemble trained: {', '.join(model_names)} (TimeSeriesSplit CV)")
    try:
        _base = trained_models[0]
        _fi = None
        if hasattr(_base, 'calibrated_classifiers_'):
            _est = _base.calibrated_classifiers_[0].estimator
            if hasattr(_est, 'feature_importances_'):
                _fi = _est.feature_importances_
        elif hasattr(_base, 'feature_importances_'):
            _fi = _base.feature_importances_
        if _fi is not None:
            _fi_series = pd.Series(_fi, index=features).sort_values(ascending=False).head(10)
            print("\nTop 10 Feature Importances:")
            for _fname, _fval in _fi_series.items():
                print(f"  {_fname:<40} {_fval:.4f}")
    except Exception:
        pass
    
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
    live['bat_hr_fb_rate'] = live['bat_hr_fb_rate'].fillna(0.12)
    live['pitch_hr_fb_allowed_rate'] = live['pitch_hr_fb_allowed_rate'].fillna(0.12)
    live['bat_ev90'] = live['bat_ev90'].fillna(88.0)
    live['bat_iso_proxy'] = live['bat_iso_proxy'].fillna(0.08)
    live['bat_days_since_last_game'] = live['bat_days_since_last_game'].fillna(1)
    live['pitch_days_since_last_start'] = live['pitch_days_since_last_start'].fillna(5)
    live['pitch_avg_velocity'] = live['pitch_avg_velocity'].fillna(92.0)
    live['wind_out_component'] = live['wind_out_component'].fillna(0.0)
    live['bat_pull_rate'] = live['bat_pull_rate'].fillna(0.38)
    live['game_time'] = live['game_time'].fillna('') if 'game_time' in live.columns else ''

    X_live = live[features]
    all_probs = [m.predict_proba(X_live)[:, 1] for m in trained_models]
    probs = sum(all_probs) / len(all_probs)

    # Batting order PA multiplier: top of order gets more plate appearances per game
    _ORDER_FACTOR = {1: 1.08, 2: 1.07, 3: 1.06, 4: 1.05, 5: 1.04, 6: 1.0, 7: 0.98, 8: 0.96, 9: 0.94}
    order_slots = live.get('batting_order_slot', pd.Series([5] * len(live))).fillna(5).astype(int).clip(1, 9)
    order_factors = order_slots.map(_ORDER_FACTOR).fillna(1.0).values
    probs = (probs * order_factors).clip(0, 1)

    # Kelly criterion: half-Kelly bet sizing vs configurable market baseline
    _market_american = float(os.getenv('MARKET_HR_ODDS', '-120'))
    _dec_odds = 1 + 100 / abs(_market_american) if _market_american < 0 else 1 + _market_american / 100
    _b = _dec_odds - 1
    _market_prob = float(os.getenv('MARKET_HR_BASELINE', '0.09'))

    def _kelly(p):
        edge = p * _b - (1 - p)
        return max(round(edge / _b * 0.5, 4), 0.0) if edge > 0 else 0.0

    live['pred_hr_prob'] = probs
    live['edge_pct'] = ((live['pred_hr_prob'] - _market_prob) / _market_prob * 100).round(1)
    live['kelly_fraction'] = live['pred_hr_prob'].apply(_kelly)
    live['model_name'] = '+'.join(model_names)
    live['prediction_timestamp'] = datetime.now().isoformat()

    # Apply real market odds if ODDS_API_KEY is configured
    market_odds = fetch_hr_prop_odds()
    if market_odds:
        def _match_odds(bname):
            bname_lower = bname.lower()
            for key, prob in market_odds.items():
                if bname_lower in key.lower() or key.lower() in bname_lower:
                    return prob
                if bname.split()[-1].lower() in key.lower():
                    return prob
            return None
        live['market_prob'] = live['batter_name'].apply(_match_odds)
        matched = live['market_prob'].notna().sum()
        print(f"Odds matched: {matched}/{len(live)} batters have real market lines")
        live['edge_pct'] = live.apply(
            lambda r: round((r['pred_hr_prob'] - r['market_prob']) / r['market_prob'] * 100, 1)
            if pd.notna(r['market_prob']) else r['edge_pct'], axis=1
        )
        def _kelly_real(row):
            mp = row.get('market_prob', None)
            if mp is None or pd.isna(mp) or mp <= 0 or mp >= 1:
                return row['kelly_fraction']
            b = (1 - mp) / mp
            edge = row['pred_hr_prob'] * b - (1 - row['pred_hr_prob'])
            return max(round(edge / b * 0.5, 4), 0.0) if edge > 0 else 0.0
        live['kelly_fraction'] = live.apply(_kelly_real, axis=1)

    persist_daily_predictions(live[['game_pk', 'game_time', 'batter', 'batter_name', 'pitcher', 'pitcher_name',
                                    'has_platoon_advantage', 'park_factor', 'temp', 'wind_speed',
                                    'pred_hr_prob', 'edge_pct', 'kelly_fraction',
                                    'model_name', 'prediction_timestamp']])

    # Sort and present elite values
    rankings = live[['batter_name', 'pitcher_name', 'pred_hr_prob', 'edge_pct', 'kelly_fraction', 'game_time']].rename(
        columns={'pred_hr_prob': 'hr_probability'})
    top_5 = rankings.sort_values(by='hr_probability', ascending=False).head(5).reset_index(drop=True)

    print("\nTop 5 Daily Projected HR Probabilities:")
    print(top_5.to_string(index=False))

    # =====================================================================
    # DISCORD WEBHOOK INTEGRATION
    # =====================================================================
    target_date = datetime.today().strftime('%Y-%m-%d')
    WEBHOOK_URL = os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    if not WEBHOOK_URL:
        print("Discord webhook not configured — skipping notification. Set DISCORD_MLB_WEBHOOK to enable.")
        return top_5 if 'top_5' in locals() else pd.DataFrame()

    if 'top_5' in locals() or 'top_5' in globals():
        table_rows = []
        for _, row in top_5.iterrows():
            pct = f"{row['hr_probability'] * 100:.1f}%"
            edge = f"{row['edge_pct']:+.0f}%" if pd.notna(row.get('edge_pct')) else 'N/A'
            gtime = str(row.get('game_time', '')).strip()[:8]
            table_rows.append(f"| {row['batter_name'][:14]:<14} | {row['pitcher_name'][:14]:<14} | {gtime:<8} | {pct:<6} | {edge:<6} |")

        table_str = "\n".join(table_rows)

        message_content = (
            f"**\u26be Today's Top 5 MLB HR Predictions ({target_date})**\n"
            "```\n"
            f"| {'Batter':<14} | {'Pitcher':<14} | {'Time ET':<8} | {'Prob':<6} | {'Edge':<6} |\n"
            f"|{'-'*16}|{'-'*16}|{'-'*10}|{'-'*8}|{'-'*8}|\n"
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
        print("No predictions available to send to Discord.")

# ==========================================
# RLM WATCHER
# ==========================================
def monitor_odds_rlm():
    """Continuously watch all sportsbook lines for RLM and steam moves on today's top picks."""
    WEBHOOK_URL = os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_MLB_WEBHOOK not set")
    if not os.getenv('ODDS_API_KEY'):
        raise RuntimeError("ODDS_API_KEY not set — required for RLM monitoring")

    today_str = datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{today_str}.csv'
    if not pred_file.exists():
        print("No predictions file found for today. Run predictions first.")
        return

    preds = pd.read_csv(pred_file)
    watch_batters = preds.nlargest(15, 'pred_hr_prob')['batter_name'].tolist()
    print(f"RLM watcher started — {len(watch_batters)} batters tracked")
    print(f"Watching: {', '.join(watch_batters[:5])}...")

    # Load last snapshot if exists
    prev_odds = {}
    snapshot_file = Path('data') / f'odds_snapshots_{today_str}.jsonl'
    if snapshot_file.exists():
        try:
            lines = snapshot_file.read_text().strip().splitlines()
            if lines:
                prev_odds = _json.loads(lines[-1]).get('odds', {})
                print(f"Loaded prior snapshot with {len(prev_odds)} players")
        except Exception:
            pass

    while True:
        try:
            current_odds = fetch_hr_prop_odds_raw()
            if not current_odds:
                print(f"[{datetime.now().strftime('%H:%M')}] No odds available yet, retrying in 15 min...")
                time.sleep(900)
                continue

            save_odds_snapshot(current_odds, today_str)

            if prev_odds:
                alerts = detect_rlm(current_odds, prev_odds, watch_batters)
                for batter, sharp_move, square_move, signal in alerts:
                    batter_row = preds[preds['batter_name'].str.lower() == batter.lower()]
                    model_prob_str = f"{float(batter_row['pred_hr_prob'].iloc[0]) * 100:.1f}%" if not batter_row.empty else 'N/A'
                    msg = (
                        f"\u26a1 **LINE MOVE ALERT — {batter}**\n"
                        f"Model HR prob: {model_prob_str}\n"
                        f"Signal: {signal}"
                    )
                    send_discord_webhook(content=msg)
                    print(msg)

            # Print current market snapshot for top picks
            print(f"\n[{datetime.now().strftime('%H:%M')}] {len(current_odds)} players tracked across sportsbooks")
            for batter in watch_batters[:5]:
                books = current_odds.get(batter, {})
                if books:
                    sharp = {k: v for k, v in books.items() if k in SHARP_BOOKS}
                    square = {k: v for k, v in books.items() if k in SQUARE_BOOKS}
                    sharp_str = f"sharp: {list(sharp.values())[0]:+d}" if sharp else ''
                    square_str = f"public: {min(square.values(), key=abs):+d}" if square else ''
                    print(f"  {batter[:25]:<25} {sharp_str:<15} {square_str}  ({len(books)} books)")

            prev_odds = current_odds
            time.sleep(900)
        except Exception as e:
            print(f"RLM monitor error: {e}")
            time.sleep(60)


# ==========================================
# NEW FEATURE: LIVE HOME RUN WATCHER
# ==========================================
def log_live_hr_feedback(batter_name, pitcher_name, game_pk, inning_half, num_inning):
    """Check today's predictions for the HR batter, log outcome to live_feedback CSV,
    and return (model_prob, was_predicted, was_top5) for Discord annotation."""
    today_str = datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{today_str}.csv'

    model_prob = None
    batter_id = None
    pitcher_id = None
    was_predicted = False
    was_top5 = False

    if pred_file.exists():
        try:
            preds = pd.read_csv(pred_file)
            match = preds[preds['batter_name'].str.lower().str.strip() == batter_name.lower().strip()]
            if not match.empty:
                row = match.iloc[0]
                model_prob = float(row['pred_hr_prob'])
                batter_id = row.get('batter')
                pitcher_id = row.get('pitcher')
                was_predicted = model_prob >= 0.15
                top5_names = preds.nlargest(5, 'pred_hr_prob')['batter_name'].str.lower().str.strip().tolist()
                was_top5 = batter_name.lower().strip() in top5_names
        except Exception:
            pass

    feedback_row = {
        'date': today_str,
        'timestamp': datetime.now().isoformat(),
        'batter_name': batter_name,
        'pitcher_name': pitcher_name,
        'batter': batter_id,
        'pitcher': pitcher_id,
        'game_pk': game_pk,
        'inning': f'{inning_half} {num_inning}',
        'model_prob': model_prob if model_prob is not None else '',
        'was_predicted': was_predicted,
        'was_top5': was_top5,
        'actual_hr': 1
    }

    feedback_file = Path('data') / f'live_feedback_{today_str}.csv'
    Path('data').mkdir(parents=True, exist_ok=True)
    fb_df = pd.DataFrame([feedback_row])
    if feedback_file.exists():
        fb_df.to_csv(feedback_file, mode='a', header=False, index=False)
    else:
        fb_df.to_csv(feedback_file, index=False)

    return model_prob, was_predicted, was_top5


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

                    # Log live outcome against today's predictions for learning feedback
                    _model_prob, _was_predicted, _was_top5 = None, False, False
                    if batter_name:
                        try:
                            _model_prob, _was_predicted, _was_top5 = log_live_hr_feedback(
                                batter_name, pitcher_name, game_id, inning_half, num_inning
                            )
                        except Exception:
                            pass

                    message_lines = [
                        "\U0001f6a8 **LIVE HOME RUN ALERT** \U0001f6a8",
                        f"\U0001f3df\ufe0f *{game_display}* ({inning_half} {num_inning})",
                        f"\u26be {description}"
                    ]
                    if batter_name:
                        message_lines.append(f"\U0001f464 Batter: {batter_name}")
                    if pitcher_name:
                        message_lines.append(f"\U0001f3af Pitcher: {pitcher_name}")
                    if _model_prob is not None:
                        prob_str = f"{_model_prob * 100:.1f}%"
                        if _was_top5:
                            message_lines.append(f"\u2705 **Model called it!** (Prob: {prob_str}) — Top 5 pick")
                        elif _was_predicted:
                            message_lines.append(f"\u2705 Model predicted this (Prob: {prob_str})")
                        else:
                            message_lines.append(f"\u26a0\ufe0f Model missed (had: {prob_str}) — logged for retraining")
                    else:
                        message_lines.append(f"\u26a0\ufe0f Not in today's predictions — logged for retraining")

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
    parser.add_argument("--rlm", action="store_true", help="Monitor all sportsbooks for reverse line movement on today's picks")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate saved predictions against actual results")
    parser.add_argument("--eval-date", type=str, help="Date to evaluate predictions for, format YYYY-MM-DD")
    parser.add_argument("--notify-eval", action="store_true", help="Send evaluation summary to Discord if webhook is configured")

    args = parser.parse_args()

    if args.live:
        monitor_live_home_runs()
        return

    if args.rlm:
        monitor_odds_rlm()
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
