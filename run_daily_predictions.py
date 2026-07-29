"""Run daily predictions for MLB HR model with Weather and Park Factors."""
# =====================================================================
# UNICODE & ENCODING FIX (Windows Console Support)
# =====================================================================
import io
import sys
import os
if sys.platform == 'win32':
    # Enable UTF-8 output on Windows
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    # Reconfigure stdout for UTF-8
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# =====================================================================
# SECTION 1: IMPORTS & ENV LOADING
# =====================================================================
import argparse
import atexit
import subprocess
import re
from datetime import datetime
from pathlib import Path
from itertools import combinations

# Load environment variables from .vscode/.env
env_file = Path(__file__).parent / '.vscode' / '.env'
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, val = line.split('=', 1)
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            key = key.strip()
            val = val.strip()

            # Discord keys must come from project env file so stale shell vars
            # do not keep pointing at deleted/rotated webhooks.
            if key.startswith('DISCORD_'):
                os.environ[key] = val
            else:
                os.environ.setdefault(key, val)
import time
import math
import json as _json
try:
    import requests
except ImportError:
    requests = None
import pandas as pd
import numpy as np
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
from threading import Thread
from queue import Queue

# =====================================================================
# TIMEOUT WRAPPER FOR STATCAST API CALLS
# =====================================================================
def statcast_with_timeout(start_dt, end_dt, timeout_seconds=30):
    """Call statcast() with a timeout to prevent indefinite hangs."""
    result_queue = Queue()
    error_queue = Queue()

    def fetch_data():
        try:
            data = statcast(start_dt=start_dt, end_dt=end_dt)
            result_queue.put(data)
        except Exception as e:
            error_queue.put(e)

    thread = Thread(target=fetch_data, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if not result_queue.empty():
        return result_queue.get()
    elif not error_queue.empty():
        raise error_queue.get()
    else:
        # Timeout occurred
        return None

# Import error tracking for silent failure detection
try:
    from error_tracking import get_tracker, log_error, log_warning
    ERROR_TRACKER = get_tracker()
except ImportError:
    print("Warning: error_tracking module not found; silent failure logging disabled")
    ERROR_TRACKER = None

# Import Baseball Savant integration
try:
    from src.baseball_savant import (
        check_lineups_morning, check_lineups_pregame,
        save_lineup_report, get_batted_balls_quality_metrics,
        get_todays_games, get_game_lineups
    )
except ImportError:
    print("Warning: baseball_savant module not available")
    check_lineups_morning = None
    get_game_lineups = None

# Import Professional Bettor features
try:
    from src.professional_bettors import (
        get_pitcher_platoon_splits, identify_platoon_mismatches,
        calculate_bullpen_fatigue_score, get_bullpen_quality_multiplier,
        get_umpire_strike_zone_profile, get_todays_umpires,
        calculate_density_altitude, get_gameday_conditions,
        detect_weather_extremes, get_sportsbook_comparison,
        find_optimal_pairings, generate_professional_recommendations,
        detect_breaking_pitch_vulnerability, identify_left_on_right_fade_opportunity,
        detect_reverse_split_anomaly
    )
    from src.stadium_info import get_stadium_elevation, STADIUM_INFO
except ImportError:
    print("Warning: professional_bettors module not available")
    get_pitcher_platoon_splits = None
    check_lineups_pregame = None
    save_lineup_report = None
    get_batted_balls_quality_metrics = None
    get_todays_games = None

try:
    from src.pa_physics_pipeline import apply_physics_pipeline_to_live
except ImportError:
    print("Warning: pa_physics_pipeline module not available")
    apply_physics_pipeline_to_live = None

try:
    from src.free_odds_sources import (
        load_free_odds_sources,
        build_devigged_probs_from_books,
    )
except ImportError:
    print("Warning: free_odds_sources module not available")
    load_free_odds_sources = None
    build_devigged_probs_from_books = None

# Import Ballpark Dimensions features
try:
    from src.ballpark_dimensions import (
        get_ballpark_factor, calculate_would_be_homers,
        get_porch_advantage_bonus, get_death_valley_penalty,
        calculate_park_adjustment_multiplier, get_stadium_info,
        BALLPARK_DATA
    )
except ImportError:
    print("Warning: ballpark_dimensions module not available")
    get_ballpark_factor = None
    BALLPARK_DATA = {}

try:
    from src.stadium_coordinates import STADIUM_COORDINATES
except ImportError:
    print("Warning: stadium_coordinates module not available")
    STADIUM_COORDINATES = {}

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
    'Comerica Park': 'DET', 'Minute Maid Park': 'HOU', 'Daikin Park': 'HOU', 'Kauffman Stadium': 'KC',
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

TEAM_ABBR_TO_STADIUM_KEY = {
    'ARI': 'Diamondbacks', 'ATL': 'Braves', 'BAL': 'Orioles', 'BOS': 'Red Sox', 'CHC': 'Cubs',
    'CWS': 'White Sox', 'CIN': 'Reds', 'CLE': 'Guardians', 'COL': 'Rockies', 'DET': 'Tigers',
    'HOU': 'Astros', 'KC': 'Royals', 'LAA': 'Angels', 'LAD': 'Dodgers', 'MIA': 'Marlins',
    'MIL': 'Brewers', 'MIN': 'Twins', 'NYM': 'Mets', 'NYY': 'Yankees', 'OAK': 'Athletics',
    'PHI': 'Phillies', 'PIT': 'Pirates', 'SD': 'Padres', 'SEA': 'Mariners', 'SF': 'Giants',
    'STL': 'Cardinals', 'TB': 'Rays', 'TEX': 'Rangers', 'TOR': 'Blue Jays', 'WSH': 'Nationals'
}

# Sportsbook tiers for RLM detection and sharp consensus weighting
SHARP_BOOKS = {'pinnacle', 'circasports', 'betonlineag', 'betus', 'betrivers', 'pointsbetusn', 'lowvig', 'bookmaker'}
SQUARE_BOOKS = {'fanduel', 'draftkings', 'betmgm', 'williamhill_us', 'barstool', 'unibet_us', 'mybookieag', 'bovada', 'caesars', 'wynnbet', 'betfred', 'superbook'}

# =====================================================================
# IMPROVEMENT 1-10: ELITE MODEL ENHANCEMENTS
# =====================================================================

def get_batter_hot_streak(df, batter_id, lookback_games=10):
    """IMPROVEMENT #2: Batter Hot/Cold Streaks (last 10 AB)
    Returns streak multiplier: >1.0 means hot, <1.0 means cold"""
    try:
        batter_data = df[df['batter'] == batter_id].sort_values('game_date').tail(lookback_games)
        if len(batter_data) < 3:
            return 1.0
        recent_hr_rate = batter_data['is_hr'].mean()
        season_avg = df[df['batter'] == batter_id]['is_hr'].mean()
        if season_avg > 0:
            streak_multiplier = recent_hr_rate / season_avg
            return max(0.5, min(3.0, streak_multiplier))  # Clip between 0.5x and 3.0x
        return 1.0
    except:
        return 1.0

def get_pitcher_recent_form(df, pitcher_id, lookback_games=5):
    """IMPROVEMENT #1: Pitcher Recent Form (last 3-5 games)
    Returns form multiplier: >1.0 means giving up HRs, <1.0 means strong"""
    try:
        pitcher_data = df[df['pitcher'] == pitcher_id].sort_values('game_date').tail(lookback_games)
        if len(pitcher_data) < 2:
            return 1.0
        recent_hr_rate = pitcher_data['is_hr'].mean()
        season_avg = df[df['pitcher'] == pitcher_id]['is_hr'].mean()
        if season_avg > 0:
            form_multiplier = recent_hr_rate / season_avg
            return max(0.4, min(2.5, form_multiplier))  # Clip between 0.4x and 2.5x
        return 1.0
    except:
        return 1.0
# HELPER: DYNAMIC LIVE WEATHER PARSER
# =====================================================================
def get_live_weather(lat, lon):
    """Fetches real-time localized metrics using Open-Meteo API.
    
    Returns:
        dict with: temp (°F), wind_speed (mph), wind_dir (°), humidity (%),
                   precipitation (in), pressure (mb)
    """
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}&current_weather=true"
            f"&temperature_unit=fahrenheit&windspeed_unit=mph"
            f"&timezone=America/Chicago"  # Use central time as default
        )
        res = requests.get(url, timeout=5).json()
        current = res.get('current_weather', {})
        
        # Try to fetch additional metrics from current API if available
        # Note: Some metrics may require hourly/daily endpoints, so we provide sensible defaults
        return {
            'temp': current.get('temperature', 70),
            'wind_speed': current.get('windspeed', 0),
            'wind_dir': current.get('winddirection', 0),
            'humidity': current.get('relative_humidity', 50),  # 50% default if unavailable
            'precipitation': current.get('precipitation', 0),  # 0 in default (no precip)
            'pressure': current.get('pressure_msl', 1013.25)  # 1013.25 mb sea-level default
        }
    except Exception:
        # Fallback to sensible defaults (60°F, moderate wind, 50% humidity, no precip, sea-level pressure)
        return {
            'temp': 70,
            'wind_speed': 0,
            'wind_dir': 0,
            'humidity': 50,
            'precipitation': 0,
            'pressure': 1013.25
        }


def _historical_weather_cache_path():
    data_dir = Path('data')
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / 'historical_weather_cache.json'


def _load_historical_weather_cache():
    fp = _historical_weather_cache_path()
    if not fp.exists():
        return {}
    try:
        payload = _json.loads(fp.read_text(encoding='utf-8'))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_historical_weather_cache(cache_payload):
    try:
        fp = _historical_weather_cache_path()
        fp.write_text(_json.dumps(cache_payload, indent=2), encoding='utf-8')
    except Exception:
        pass


def _get_stadium_coords_for_team(team_abbr):
    key = TEAM_ABBR_TO_STADIUM_KEY.get(str(team_abbr or '').upper(), '')
    if not key:
        return None
    rec = STADIUM_COORDINATES.get(key, {}) if isinstance(STADIUM_COORDINATES, dict) else {}
    lat = rec.get('latitude')
    lon = rec.get('longitude')
    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return None
    return lat, lon


def _fetch_historical_weather_for_team_date(team_abbr, date_str):
    coords = _get_stadium_coords_for_team(team_abbr)
    if coords is None or requests is None:
        return None

    lat, lon = coords
    try:
        url = (
            "https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat}&longitude={lon}"
            f"&start_date={date_str}&end_date={date_str}"
            "&daily=temperature_2m_mean,wind_speed_10m_mean,wind_direction_10m_dominant,"
            "relative_humidity_2m_mean,precipitation_sum,pressure_msl_mean"
            "&temperature_unit=fahrenheit&windspeed_unit=mph"
            "&timezone=America/New_York"
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code != 200:
            return None
        payload = resp.json() or {}
        daily = payload.get('daily', {}) or {}

        def _pick(name, default):
            try:
                vals = daily.get(name) or [default]
                return float(vals[0]) if vals and vals[0] is not None else float(default)
            except Exception:
                return float(default)

        return {
            'temp': _pick('temperature_2m_mean', 71.0),
            'wind_speed': _pick('wind_speed_10m_mean', 5.0),
            'wind_dir': _pick('wind_direction_10m_dominant', 0.0),
            'humidity': _pick('relative_humidity_2m_mean', 50.0),
            'precipitation': _pick('precipitation_sum', 0.0),
            'pressure': _pick('pressure_msl_mean', 1013.25),
        }
    except KeyboardInterrupt:
        return None
    except Exception:
        return None


def enrich_training_weather_from_history(pa_df):
    """Backfill recent historical weather for training rows using cached Open-Meteo archive."""
    if pa_df is None or pa_df.empty:
        return pa_df

    for col, default in [
        ('temp', 71.0),
        ('wind_speed', 5.0),
        ('wind_out_component', 0.0),
        ('humidity', 50.0),
        ('precipitation', 0.0),
        ('pressure', 1013.25),
    ]:
        if col not in pa_df.columns:
            pa_df[col] = default
        else:
            pa_df[col] = pd.to_numeric(pa_df[col], errors='coerce').fillna(default)

    if 'game_date' not in pa_df.columns or 'home_team' not in pa_df.columns:
        return pa_df

    game_dates = pd.to_datetime(pa_df['game_date'], errors='coerce')
    lookback_days = max(7, _safe_int(os.getenv('HIST_WEATHER_LOOKBACK_DAYS', '45'), 45) or 45)
    cutoff = datetime.today() - timedelta(days=lookback_days)
    target = pa_df[(game_dates.notna()) & (game_dates >= cutoff)].copy()
    if target.empty:
        return pa_df

    target['weather_date'] = pd.to_datetime(target['game_date'], errors='coerce').dt.strftime('%Y-%m-%d')
    target['team_abbr'] = target['home_team'].astype(str).str.upper()
    unique_keys = target[['weather_date', 'team_abbr']].dropna().drop_duplicates()
    if unique_keys.empty:
        return pa_df

    cache = _load_historical_weather_cache()
    max_fetch = max(20, _safe_int(os.getenv('HIST_WEATHER_MAX_FETCH_PER_RUN', '120'), 120) or 120)
    fetched = 0
    cache_hits = 0
    weather_by_key = {}

    for _, krow in unique_keys.iterrows():
        d = str(krow['weather_date'])
        t = str(krow['team_abbr']).upper()
        cache_key = f"{d}:{t}"

        rec = cache.get(cache_key)
        if isinstance(rec, dict):
            weather_by_key[cache_key] = rec
            cache_hits += 1
            continue

        if fetched >= max_fetch:
            continue
        fetched_rec = _fetch_historical_weather_for_team_date(t, d)
        if fetched_rec is None:
            continue
        cache[cache_key] = fetched_rec
        weather_by_key[cache_key] = fetched_rec
        fetched += 1

    if fetched > 0:
        _save_historical_weather_cache(cache)

    if not weather_by_key:
        return pa_df

    enriched_rows = 0
    date_series = pd.to_datetime(pa_df['game_date'], errors='coerce').dt.strftime('%Y-%m-%d')
    team_series = pa_df['home_team'].astype(str).str.upper()

    for _, krow in unique_keys.iterrows():
        d = str(krow['weather_date'])
        t = str(krow['team_abbr']).upper()
        rec = weather_by_key.get(f"{d}:{t}")
        if not rec:
            continue

        mask = date_series.eq(d) & team_series.eq(t)
        if not mask.any():
            continue

        temp = float(rec.get('temp', 71.0))
        wind_speed = float(rec.get('wind_speed', 5.0))
        wind_dir = float(rec.get('wind_dir', 0.0))
        humidity = float(rec.get('humidity', 50.0))
        precipitation = float(rec.get('precipitation', 0.0))
        pressure = float(rec.get('pressure', 1013.25))
        cf_bearing = STADIUM_CF_BEARING.get(t, 0)
        wind_out = round(wind_speed * math.cos(math.radians(wind_dir - cf_bearing)), 2)

        pa_df.loc[mask, 'temp'] = temp
        pa_df.loc[mask, 'wind_speed'] = wind_speed
        pa_df.loc[mask, 'wind_out_component'] = wind_out
        pa_df.loc[mask, 'humidity'] = humidity
        pa_df.loc[mask, 'precipitation'] = precipitation
        pa_df.loc[mask, 'pressure'] = pressure
        enriched_rows += int(mask.sum())

    print(
        "Historical weather enrichment: "
        f"rows={enriched_rows}, keys={len(weather_by_key)}/{len(unique_keys)}, "
        f"cache_hits={cache_hits}, fetched={fetched}"
    )
    return pa_df


def persist_daily_predictions(predictions_df, date_str=None):
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    Path('data').mkdir(parents=True, exist_ok=True)
    filename = Path('data') / f'predictions_{date_str}.csv'
    predictions_df.to_csv(filename, index=False)
    print(f"Saved daily prediction history: {filename}")
    return filename


def print_bet_ready_wagers(date_str=None, top_n=15):
    """Print only actionable wagers from today's predictions file.

    Safeguards:
    - Excludes rows with missing odds
    - Excludes rows outside configured American-odds range
    - Requires +EV and Kelly > 0
    """
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{date_str}.csv'
    if not pred_file.exists():
        print(f"NO BETS TODAY: missing predictions file ({pred_file}).")
        return pd.DataFrame()

    try:
        preds = pd.read_csv(pred_file)
    except Exception as exc:
        print(f"NO BETS TODAY: failed to read predictions file ({exc}).")
        return pd.DataFrame()

    if preds.empty:
        print("NO BETS TODAY: predictions file is empty.")
        return pd.DataFrame()

    for num_col in ['kelly_fraction', 'ev_percent', 'pred_hr_prob', 'edge_pct', 'best_market_odds_american']:
        if num_col in preds.columns:
            preds[num_col] = pd.to_numeric(preds[num_col], errors='coerce')

    def _env_int(name, default):
        try:
            return int(float(os.getenv(name, str(default))))
        except Exception:
            return int(default)

    def _env_float(name, default):
        try:
            return float(os.getenv(name, str(default)))
        except Exception:
            return float(default)

    # Conservative default range for HR props; override via env if needed.
    min_american = _env_int('BET_READY_MIN_AMERICAN_ODDS', -300)
    max_american = _env_int('BET_READY_MAX_AMERICAN_ODDS', 5000)

    if 'is_positive_ev' in preds.columns:
        preds['is_positive_ev_bool'] = preds['is_positive_ev'].astype(str).str.lower().eq('true')
    elif 'ev_percent' in preds.columns:
        preds['is_positive_ev_bool'] = pd.to_numeric(preds['ev_percent'], errors='coerce').fillna(0) > 0
    else:
        preds['is_positive_ev_bool'] = False

    if 'best_market_odds_american' in preds.columns:
        preds['has_market_odds'] = preds['best_market_odds_american'].astype(str).str.strip().ne('')
        preds.loc[preds['best_market_odds_american'].isna(), 'has_market_odds'] = False
    else:
        preds['has_market_odds'] = False

    preds['odds_in_sanity_range'] = (
        preds['best_market_odds_american'].notna() &
        (preds['best_market_odds_american'] >= min_american) &
        (preds['best_market_odds_american'] <= max_american)
    ) if 'best_market_odds_american' in preds.columns else False

    actionable = preds[
        preds['is_positive_ev_bool'] &
        preds['has_market_odds'] &
        preds['odds_in_sanity_range'] &
        (preds['kelly_fraction'].fillna(0) > 0)
    ].copy()

    if actionable.empty:
        with_odds_count = int(preds['has_market_odds'].sum())
        sane_odds_count = int(preds['odds_in_sanity_range'].sum())
        print(
            "NO BETS TODAY: no actionable +EV wagers "
            "(requires market odds, sane odds range, positive EV, and Kelly > 0)."
        )
        print(
            f"Diagnostics: rows={len(preds)}, with_odds={with_odds_count}, "
            f"sane_odds={sane_odds_count}, positive_ev={int(preds['is_positive_ev_bool'].sum())}, "
            f"odds_range=[{min_american},{max_american}]"
        )
        return actionable

    excluded_outlier_count = int((
        preds['is_positive_ev_bool'] & preds['has_market_odds'] & ~preds['odds_in_sanity_range']
    ).sum())

    keep_cols = [
        'batter_name', 'pitcher_name', 'pred_hr_prob', 'best_book',
        'best_market_odds_american', 'fair_odds_american',
        'ev_percent', 'edge_pct', 'kelly_fraction', 'game_time'
    ]
    keep_cols = [c for c in keep_cols if c in actionable.columns]
    report = actionable[keep_cols].sort_values(
        by=['ev_percent', 'kelly_fraction'],
        ascending=[False, False]
    ).head(max(1, int(top_n))).reset_index(drop=True)

    print("\nBET-READY WAGERS (+EV, ODDS-VALIDATED):")
    if excluded_outlier_count > 0:
        print(
            "Filtered out "
            f"{excluded_outlier_count} +EV rows with outlier market odds outside "
            f"[{min_american},{max_american}]."
        )
    print(report.to_string(index=False))
    return report


def print_conservative_bet_ready_wagers(date_str=None, top_n=10):
    """Print and persist a conservative shortlist of actionable wagers.

    This is a stricter filter than print_bet_ready_wagers and is intended
    to prioritize robustness over volume.
    """
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{date_str}.csv'
    if not pred_file.exists():
        print(f"No conservative shortlist: missing predictions file ({pred_file}).")
        return pd.DataFrame()

    try:
        preds = pd.read_csv(pred_file)
    except Exception as exc:
        print(f"No conservative shortlist: failed to read predictions file ({exc}).")
        return pd.DataFrame()

    if preds.empty:
        print("No conservative shortlist: predictions file is empty.")
        return pd.DataFrame()

    def _env_int(name, default):
        try:
            return int(float(os.getenv(name, str(default))))
        except Exception:
            return int(default)

    def _env_float(name, default):
        try:
            return float(os.getenv(name, str(default)))
        except Exception:
            return float(default)

    for c in [
        'pred_hr_prob', 'ev_percent', 'edge_pct', 'kelly_fraction',
        'best_market_odds_american', 'best_market_implied_prob'
    ]:
        if c in preds.columns:
            preds[c] = pd.to_numeric(preds[c], errors='coerce')

    if 'is_positive_ev' in preds.columns:
        preds['is_positive_ev_bool'] = preds['is_positive_ev'].astype(str).str.lower().eq('true')
    else:
        if 'ev_percent' in preds.columns:
            preds['is_positive_ev_bool'] = pd.to_numeric(preds['ev_percent'], errors='coerce').fillna(0) > 0
        else:
            preds['is_positive_ev_bool'] = False

    preds['model_reliability'] = preds.get('model_reliability', 'MEDIUM').astype(str).str.upper()
    preds['has_market_odds'] = preds.get('best_market_odds_american', pd.Series([np.nan] * len(preds))).notna()

    min_american = _env_int('CONSERVATIVE_MIN_AMERICAN_ODDS', -220)
    max_american = _env_int('CONSERVATIVE_MAX_AMERICAN_ODDS', 1200)
    min_prob = _env_float('CONSERVATIVE_MIN_PROB', 0.07)
    max_prob = _env_float('CONSERVATIVE_MAX_PROB', 0.40)
    min_ev_pct = _env_float('CONSERVATIVE_MIN_EV_PCT', 2.5)
    min_edge_pct = _env_float('CONSERVATIVE_MIN_EDGE_PCT', 4.0)
    min_kelly = _env_float('CONSERVATIVE_MIN_KELLY', 0.01)

    preds['odds_in_sanity_range'] = (
        preds['has_market_odds'] &
        (preds['best_market_odds_american'] >= min_american) &
        (preds['best_market_odds_american'] <= max_american)
    )

    high_conf = preds['model_reliability'].eq('HIGH')
    medium_conf = preds['model_reliability'].eq('MEDIUM') & (preds['pred_hr_prob'] <= 0.28)
    reliability_gate = high_conf | medium_conf

    shortlist = preds[
        preds['is_positive_ev_bool'] &
        preds['has_market_odds'] &
        preds['odds_in_sanity_range'] &
        reliability_gate &
        (preds['pred_hr_prob'] >= min_prob) &
        (preds['pred_hr_prob'] <= max_prob) &
        (preds['ev_percent'].fillna(0) >= min_ev_pct) &
        (preds['edge_pct'].fillna(0) >= min_edge_pct) &
        (preds['kelly_fraction'].fillna(0) >= min_kelly)
    ].copy()

    if shortlist.empty:
        print("No conservative shortlist: no rows passed strict risk filters.")
        return shortlist

    shortlist['conservative_score'] = (
        shortlist['kelly_fraction'].fillna(0) * 100
        + shortlist['ev_percent'].fillna(0)
        + shortlist['edge_pct'].fillna(0) / 2.0
    )

    keep_cols = [
        'batter_name', 'pitcher_name', 'pred_hr_prob', 'model_reliability',
        'best_book', 'best_market_odds_american', 'fair_odds_american',
        'edge_pct', 'ev_percent', 'kelly_fraction', 'conservative_score', 'game_time'
    ]
    keep_cols = [c for c in keep_cols if c in shortlist.columns]
    shortlist = shortlist.sort_values(
        ['conservative_score', 'ev_percent', 'kelly_fraction'],
        ascending=[False, False, False]
    ).head(max(1, int(top_n))).reset_index(drop=True)

    out_path = Path('data') / f'conservative_bet_ready_{date_str}.csv'
    shortlist.to_csv(out_path, index=False)

    print("\nCONSERVATIVE BET-READY SHORTLIST:")
    print(shortlist.to_string(index=False))
    print(f"Saved conservative shortlist: {out_path}")
    return shortlist


def _candidate_discord_webhooks(explicit_webhook=None):
    """Return normalized webhook candidates in priority order."""
    candidates = [
        explicit_webhook,
        os.getenv("DISCORD_MLB_WEBHOOK"),
        os.getenv("DISCORD_WEBHOOK_URL"),
        os.getenv("DISCORD_WEBHOOK"),
        os.getenv("DISCORD_MLB_WEBHOOK_BACKUP"),
        os.getenv("DISCORD_WEBHOOK_URL_BACKUP"),
    ]

    normalized = []
    for url in candidates:
        if not url:
            continue
        cleaned = str(url).strip().strip('"').strip("'")
        if not cleaned:
            continue
        normalized.append(cleaned)

    # Keep order while removing duplicates.
    return list(dict.fromkeys(normalized))


def send_discord_webhook(content=None, embeds=None, webhook_url=None, async_send=False, retries=3):
    """Send Discord webhook message.
    
    Args:
        async_send: If True, attempt async send but fall back to sync if needed.
        retries: Number of retry attempts for sync sends (default 3 for live HRs).
    """
    webhook_candidates = _candidate_discord_webhooks(explicit_webhook=webhook_url)
    if not webhook_candidates:
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
    
    # For live HRs, try async but with error tracking
    if async_send:
        import threading
        # Wrap with error tracking queue
        result_holder = {'success': False, 'error': None}
        thread = threading.Thread(
            target=_send_discord_async_tracked,
            args=(payload, webhook_candidates, result_holder),
            daemon=True
        )
        thread.start()
        # Don't wait, but return True immediately (async)
        return True

    # Synchronous send with retry
    for attempt in range(retries):
        result = _send_discord_sync(payload, webhook_candidates)
        if result:
            return True
        if attempt < retries - 1:
            time.sleep(0.5)  # Brief backoff between retries
    return False

def _send_discord_async_tracked(payload, webhook_candidates, result_holder):
    """Send Discord in background thread with error tracking."""
    try:
        success = _send_discord_sync(payload, webhook_candidates, silent=False)
        result_holder['success'] = success
    except Exception as e:
        result_holder['error'] = str(e)
        print(f"Async Discord send error: {e}")
        # Fallback: try one more time synchronously
        try:
            _send_discord_sync(payload, webhook_candidates, silent=True)
        except Exception:
            pass

def _send_discord_sync(payload, webhook_candidates, silent=False):
    """Synchronously send Discord webhook message."""
    for idx, candidate in enumerate(webhook_candidates):
        try:
            response = requests.post(candidate, json=payload, timeout=4)
            if response.status_code == 204:
                if not silent:
                    if idx > 0:
                        print("Discord notification sent successfully using backup webhook.")
                    else:
                        print("Discord notification sent successfully.")
                return True
            if response.status_code == 404:
                if not silent:
                    print(
                        f"Discord webhook returned 404 for candidate #{idx + 1}. "
                        "This webhook was likely deleted/rotated; trying next candidate if available."
                    )
                continue
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if not silent:
                    print(
                        "Discord rate-limited webhook call (429). "
                        f"Retry-After={retry_after}; trying next candidate if available."
                    )
                continue

            if not silent:
                print(
                    f"Discord webhook returned status {response.status_code}: "
                    f"{getattr(response, 'text', '')}"
                )
            return False
        except Exception as e:
            if not silent:
                print(f"Failed to send Discord notification via candidate #{idx + 1}: {e}")

    return False


def load_or_fetch_statcast(date_str):
    cache_dir = Path('cache')
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f'statcast_{date_str}.csv'
    empty_marker = cache_dir / f'statcast_{date_str}.empty'

    try:
        empty_ttl_minutes = int(float(os.getenv('STATCAST_EMPTY_CACHE_TTL_MINUTES', '30') or '30'))
    except Exception:
        empty_ttl_minutes = 30
    empty_ttl_minutes = max(5, empty_ttl_minutes)

    if empty_marker.exists():
        try:
            marker_age_sec = (datetime.now() - datetime.fromtimestamp(empty_marker.stat().st_mtime)).total_seconds()
            if marker_age_sec < (empty_ttl_minutes * 60):
                return pd.DataFrame()
        except Exception:
            pass

    if cache_file.exists():
        try:
            empty_marker.unlink(missing_ok=True)
            return pd.read_csv(cache_file)
        except Exception as exc:
            print(f"Cache read failed for {date_str}; refetching from Baseball Savant: {exc}")

    try:
        stats = statcast_with_timeout(start_dt=date_str, end_dt=date_str)
        if stats is None or stats.empty:
            try:
                empty_marker.write_text(datetime.now().isoformat(), encoding='utf-8')
            except Exception:
                pass
            print(f"No Statcast data available for {date_str} (cached {empty_ttl_minutes}m).")
            return pd.DataFrame()
        stats.to_csv(cache_file, index=False)
        empty_marker.unlink(missing_ok=True)
        return stats
    except Exception as exc:
        print(f"Failed to fetch Statcast data for {date_str}: {exc}")
        return pd.DataFrame()


def _load_actual_hr_outcomes(date_str):
    """Load actual HR outcomes for a date keyed by (game_pk, batter, pitcher)."""
    actual = load_or_fetch_statcast(date_str)
    if actual is None or actual.empty:
        return pd.DataFrame()

    need_cols = {'game_pk', 'batter', 'pitcher', 'events'}
    if not need_cols.issubset(set(actual.columns)):
        return pd.DataFrame()

    actual = actual.dropna(subset=['game_pk', 'batter', 'pitcher', 'events']).copy()
    actual['is_hr'] = (actual['events'] == 'home_run').astype(int)
    return actual.groupby(['game_pk', 'batter', 'pitcher'], as_index=False).agg(actual_hr=('is_hr', 'max'))


def calibrate_physics_blend_weight(days_lookback=30, default_weight=0.55):
    """Grid-search blend weight over recent predictions with physics diagnostics.

    INCREASED DEFAULT from 0.45 to 0.55: Physics simulation catches power spikes better
    by modeling actual PA context (exit velocity, launch angle, ballpark) vs pure stats.
    
    Returns best physics weight in [0.0, 1.0] that minimizes Brier score.
    """
    cutoff = datetime.today() - timedelta(days=days_lookback)
    candidates = [round(x, 2) for x in np.arange(0.0, 1.01, 0.05)]
    scores = {w: [] for w in candidates}
    eligible_days = 0

    for f in sorted(Path('data').glob('predictions_*.csv')):
        try:
            date_str = f.stem.replace('predictions_', '')
            file_date = datetime.strptime(date_str, '%Y-%m-%d')
            if file_date < cutoff:
                continue

            preds = pd.read_csv(f)
            if preds.empty:
                continue
            if 'pred_hr_prob' not in preds.columns or 'physics_hr_prob' not in preds.columns:
                continue

            actual = _load_actual_hr_outcomes(date_str)
            if actual.empty:
                continue

            merged = preds.merge(actual, on=['game_pk', 'batter', 'pitcher'], how='inner')
            if merged.empty:
                continue

            base_col = 'base_model_prob' if 'base_model_prob' in merged.columns else 'pred_hr_prob'
            merged['base_prob'] = pd.to_numeric(merged[base_col], errors='coerce').fillna(0.0)
            merged['physics_prob'] = pd.to_numeric(merged['physics_hr_prob'], errors='coerce').fillna(0.0)
            merged['actual_hr'] = pd.to_numeric(merged['actual_hr'], errors='coerce').fillna(0.0)

            eligible_days += 1
            for w in candidates:
                blended = ((1 - w) * merged['base_prob']) + (w * merged['physics_prob'])
                brier = ((blended - merged['actual_hr']) ** 2).mean()
                scores[w].append(float(brier))
        except Exception:
            continue

    if eligible_days == 0:
        print(f"Blend calibration: no eligible historical files in last {days_lookback} days; using default {default_weight:.2f}")
        return default_weight

    avg_scores = {w: (sum(v) / len(v) if v else 1e9) for w, v in scores.items()}
    best_weight = min(avg_scores, key=avg_scores.get)
    print(
        "Blend calibration: "
        f"best physics weight={best_weight:.2f} "
        f"(days={eligible_days}, avg_brier={avg_scores[best_weight]:.5f})"
    )
    return float(best_weight)


def resolve_probability_mode_and_weight():
    """Resolve runtime prediction mode and blend weight from env/config.

    HR_PROB_MODE:
      - base: model-only probabilities
      - physics: physics-only probabilities
      - blended: weighted blend (default)
      - auto: alias of blended

    HR_PHYSICS_BLEND_WEIGHT:
      - Optional float 0-1 override for blended mode.
      - If not set, auto-calibrated on recent history.
    """
    mode = str(os.getenv('HR_PROB_MODE', 'blended')).strip().lower()
    if mode == 'auto':
        mode = 'blended'
    if mode not in {'base', 'physics', 'blended'}:
        mode = 'blended'

    raw_weight = os.getenv('HR_PHYSICS_BLEND_WEIGHT')
    if raw_weight is not None and raw_weight != '':
        try:
            weight = max(0.0, min(1.0, float(raw_weight)))
            print(f"Blend weight override from HR_PHYSICS_BLEND_WEIGHT: {weight:.2f}")
        except Exception:
            weight = calibrate_physics_blend_weight(days_lookback=30, default_weight=0.45)
    else:
        weight = calibrate_physics_blend_weight(days_lookback=30, default_weight=0.45)

    return mode, weight


def resolve_kelly_multiplier(days_lookback=14, default_multiplier=0.60):
    """Resolve Kelly multiplier from env override or recent evaluation drift.

    INCREASED DEFAULT from 0.50 to 0.60: Model now using less conservative calibration,
    power spike detection, and stronger feedback weights. Higher Kelly fraction recommended.
    
    If KELLY_MULTIPLIER is set, use it (clipped to [0.10, 1.00]).
    Otherwise derive a conservative multiplier from recent Brier score quality.
    """
    raw = os.getenv('KELLY_MULTIPLIER')
    if raw is not None and str(raw).strip() != '':
        try:
            val = max(0.10, min(1.00, float(raw)))
            print(f"Kelly multiplier override from KELLY_MULTIPLIER: {val:.2f}")
            return float(val)
        except Exception:
            pass

    cutoff = datetime.today() - timedelta(days=days_lookback)
    briers = []
    for f in sorted(Path('data').glob('evaluation_*.csv')):
        try:
            d = datetime.strptime(f.stem.replace('evaluation_', ''), '%Y-%m-%d')
            if d < cutoff:
                continue
            df = pd.read_csv(f, usecols=['pred_hr_prob', 'actual_hr'])
            if df.empty:
                continue
            pred = pd.to_numeric(df['pred_hr_prob'], errors='coerce').fillna(0.0)
            actual = pd.to_numeric(df['actual_hr'], errors='coerce').fillna(0.0)
            brier = float(((pred - actual) ** 2).mean())
            if np.isfinite(brier):
                briers.append(brier)
        except Exception:
            continue

    if not briers:
        print(f"Kelly multiplier: no recent evaluations in last {days_lookback} days; using default {default_multiplier:.2f}")
        return float(default_multiplier)

    avg_brier = float(sum(briers) / len(briers))
    # Lower Brier = better calibration = can risk slightly more.
    # Sigmoid calibration allows higher multipliers than isotonic calibration used before.
    if avg_brier <= 0.08:
        mult = 0.75  # Increased from 0.60: excellent calibration
    elif avg_brier <= 0.12:
        mult = 0.65  # Increased from 0.50: good calibration  
    elif avg_brier <= 0.16:
        mult = 0.50  # Increased from 0.40: acceptable calibration
    else:
        mult = 0.40  # Increased from 0.30: poor calibration

    print(f"Kelly multiplier auto-adjusted from Brier trend: {mult:.2f} (days={len(briers)}, avg_brier={avg_brier:.4f})")
    return float(mult)


def _file_contains_text(path, needle):
    try:
        p = Path(path)
        if not p.exists():
            return False
        return needle in p.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return False


def run_model_self_check(days_lookback=30):
    """Check system health and detect/alert on silent failures."""
    print("\n" + "=" * 70)
    print("MODEL HEALTH CHECK & SILENT FAILURE DETECTION")
    print("=" * 70)

    issues_found = 0
    
    # 1. Check Discord webhook is configured
    webhook = os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("❌ ISSUE: Discord webhook not configured - HRs won't be sent!")
        issues_found += 1
    else:
        print("✅ Discord webhook configured and ready")

    # 2. Check physics module availability
    if apply_physics_pipeline_to_live is None:
        print("⚠️  Physics module not available (non-critical, using base model only)")
    else:
        print("✅ Physics simulation module loaded")

    # 3. Validate prediction files have required columns
    cutoff = datetime.today() - timedelta(days=days_lookback)
    pred_files = []
    bad_files = []
    for f in sorted(Path('data').glob('predictions_*.csv')):
        try:
            file_date = datetime.strptime(f.stem.replace('predictions_', ''), '%Y-%m-%d')
            if file_date < cutoff:
                continue
            pred_files.append(f)
            df = pd.read_csv(f, nrows=1)
            required = ['game_pk', 'batter', 'pitcher', 'pred_hr_prob']
            if not all(c in df.columns for c in required):
                bad_files.append(f.name)
        except Exception as e:
            bad_files.append(f"{f.name}: {e}")
            issues_found += 1

    if bad_files:
        print(f"❌ {len(bad_files)} prediction files invalid:")
        for fname in bad_files[:3]:
            print(f"   {fname}")
        issues_found += len(bad_files)
    else:
        print(f"✅ {len(pred_files)} prediction files valid")

    # 4. CHECK FOR SILENT FAILURES: Scan today's predictions for anomalies
    today_str = datetime.today().strftime('%Y-%m-%d')
    today_pred = Path('data') / f'predictions_{today_str}.csv'
    print("\n🔍 SCANNING FOR SILENT FAILURES...")
    
    silent_failures = []
    if today_pred.exists():
        try:
            today_preds_df = pd.read_csv(today_pred)
            
            if today_preds_df.empty:
                msg = f"Today's predictions CSV is EMPTY - model likely failed silently!"
                print(f"❌ {msg}")
                silent_failures.append(msg)
                issues_found += 1
            else:
                prob_col = today_preds_df['pred_hr_prob']
                zero_pct = (prob_col == 0).mean()
                nan_pct = prob_col.isna().mean()
                
                if zero_pct > 0.8:
                    msg = f"⚠️  {zero_pct*100:.0f}% of probabilities are exactly 0.0 - possible model failure"
                    print(msg)
                    silent_failures.append(msg)
                    issues_found += 1
                elif nan_pct > 0.5:
                    msg = f"⚠️  {nan_pct*100:.0f}% of probabilities are NaN - possible model failure"
                    print(msg)
                    silent_failures.append(msg)
                    issues_found += 1
                else:
                    print(f"✅ Today's predictions: {len(today_preds_df)} rows, mean={prob_col.mean():.3f}, max={prob_col.max():.3f}")
        except Exception as e:
            msg = f"Could not read today's predictions: {e}"
            print(f"❌ {msg}")
            silent_failures.append(msg)
            issues_found += 1
    else:
        print(f"ℹ️  Today's predictions not yet generated (normal if early in day)")

    # 5. Check evaluation pipeline (learning data)
    yesterday_str = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    eval_file = Path('data') / f'evaluation_{yesterday_str}.csv'
    if eval_file.exists():
        try:
            eval_df = pd.read_csv(eval_file)
            print(f"✅ Learning evaluation ready: {len(eval_df)} rows")
        except Exception as e:
            print(f"⚠️  Evaluation file corrupted: {e}")
    else:
        print(f"ℹ️  No evaluation for {yesterday_str} yet")

    # SUMMARY
    print("\n" + "=" * 70)
    if issues_found == 0:
        if not silent_failures:
            print("✅ ALL CHECKS PASSED - System is healthy")
            return True
        else:
            print(f"⚠️  WARNING: {len(silent_failures)} potential silent failures detected")
            return False
    else:
        print(f"❌ HEALTH CHECK FAILED - Found {issues_found} issue(s)")
        if silent_failures:
            print(f"\n🚨 SILENT FAILURES DETECTED:")
            for fail in silent_failures:
                print(f"   {fail}")
        return False


def send_morning_learning_summary(
    learning_result=None,
    missed_count=0,
    scale_pos_weight=None,
    physics_weight=None,
    kelly_multiplier=None,
):
    """Send a once-per-day Discord summary of what the model learned and changed."""
    today_str = datetime.today().strftime('%Y-%m-%d')
    marker = Path('data') / f'morning_learning_summary_sent_{today_str}.txt'

    if marker.exists() and os.getenv('FORCE_MORNING_LEARNING_SUMMARY', 'false').lower() != 'true':
        return False

    insights = (learning_result or {}).get('insights', {}) if isinstance(learning_result, dict) else {}
    if not isinstance(insights, dict):
        insights = {}

    # Fallback: if in-memory insights are missing, try today's saved learning report.
    if not insights:
        report_path = Path('data') / f"hr_learning_report_{today_str}.json"
        if report_path.exists():
            try:
                loaded = _json.loads(report_path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    insights = loaded
            except Exception:
                pass
    yesterday_str = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    eval_file = Path('data') / f'evaluation_{yesterday_str}.csv'
    eval_brier = None
    eval_rows = 0
    if eval_file.exists():
        try:
            eval_df = pd.read_csv(eval_file)
            eval_rows = len(eval_df)
            if 'brier_error' in eval_df.columns and not eval_df.empty:
                eval_brier = float(pd.to_numeric(eval_df['brier_error'], errors='coerce').mean())
        except Exception:
            pass

    if not insights:
        yesterday_eval_path = Path('data') / f"evaluation_{(datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')}.csv"
        if yesterday_eval_path.exists():
            try:
                from analyze_hr_patterns import build_learning_insights_from_evaluation
                eval_df = pd.read_csv(yesterday_eval_path)
                insights = build_learning_insights_from_evaluation(eval_df) or {}
            except Exception:
                insights = {}

    findings = []
    if insights:
        findings = [str(x) for x in insights.get('key_findings', [])[:3]]

    total_hrs = insights.get('total_hrs_analyzed') if insights else None
    accurate = insights.get('accurate_predictions') if insights else None
    missed = insights.get('missed_predictions') if insights else None

    try:
        total_hrs = int(total_hrs) if total_hrs is not None else None
    except Exception:
        total_hrs = None
    try:
        accurate = int(accurate) if accurate is not None else None
    except Exception:
        accurate = None
    try:
        missed = int(missed) if missed is not None else None
    except Exception:
        missed = None

    has_hr_summary = (
        total_hrs is not None and total_hrs > 0 and
        accurate is not None and missed is not None
    )

    lines = [f"**🧠 Morning Learning Summary — {today_str}**"]
    if has_hr_summary:
        lines.append(f"Yesterday reviewed: {total_hrs} HRs | Predicted: {accurate} | Missed: {missed}")
    else:
        lines.append("Yesterday reviewed: unavailable (no verified HR feedback loaded)")
    if eval_rows:
        brier_str = f"{eval_brier:.4f}" if eval_brier is not None else 'n/a'
        lines.append(f"Evaluation: {eval_rows} predictions scored | Brier: {brier_str}")
    else:
        lines.append("Evaluation: no completed evaluation file yet")

    if findings:
        lines.append("Learned:")
        for item in findings:
            lines.append(f"- {item}")
    else:
        lines.append("Learned: no verified HR feedback insights were available")

    lines.append("Model changes applied today:")
    lines.append(f"- Upweighted training rows from missed HR feedback: {int(missed_count)}")
    if scale_pos_weight is not None:
        lines.append(f"- Class imbalance control scale_pos_weight: {float(scale_pos_weight):.2f}")
    if physics_weight is not None:
        lines.append(f"- Auto blend weight for physics model: {float(physics_weight):.2f}")
    if kelly_multiplier is not None:
        lines.append(f"- Kelly staking multiplier: {float(kelly_multiplier):.2f}")

    sent = send_discord_webhook(content="\n".join(lines))
    if sent:
        Path('data').mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().isoformat(), encoding='utf-8')
    return sent


def backfill_physics_columns(days_lookback=30):
    """Backfill physics columns in recent prediction files using context heuristics.

    This enables earlier blend calibration on historical files that predate
    the physics pipeline persistence columns.
    """
    cutoff = datetime.today() - timedelta(days=days_lookback)
    updated = 0
    skipped = 0

    print(f"Backfilling physics columns for last {days_lookback} days...")
    for f in sorted(Path('data').glob('predictions_*.csv')):
        try:
            date_str = f.stem.replace('predictions_', '')
            file_date = datetime.strptime(date_str, '%Y-%m-%d')
            if file_date < cutoff:
                continue

            df = pd.read_csv(f)
            if df.empty or 'pred_hr_prob' not in df.columns:
                skipped += 1
                continue

            changed = False
            base_prob = pd.to_numeric(df.get('base_model_prob', df['pred_hr_prob']), errors='coerce').fillna(0.0)

            if 'physics_hr_prob' not in df.columns:
                temp = pd.to_numeric(df.get('temp', 71.0), errors='coerce').fillna(71.0)
                wind = pd.to_numeric(df.get('wind_speed', 5.0), errors='coerce').fillna(5.0)
                wind_out = pd.to_numeric(df.get('wind_out_component', 0.0), errors='coerce').fillna(0.0)
                park = pd.to_numeric(df.get('park_factor', 100.0), errors='coerce').fillna(100.0)
                platoon = pd.to_numeric(df.get('has_platoon_advantage', 0), errors='coerce').fillna(0.0)

                temp_boost = ((temp - 70.0) * 0.0015).clip(-0.03, 0.04)
                wind_boost = (wind_out * 0.0035 + wind * 0.0008).clip(-0.05, 0.06)
                park_boost = ((park - 100.0) * 0.0012).clip(-0.08, 0.08)
                platoon_boost = (platoon * 0.010).clip(0.0, 0.015)

                heur_mult = 1.0 + temp_boost + wind_boost + park_boost + platoon_boost
                physics_prob = np.clip(base_prob * heur_mult, 0.0, 1.0)
                df['physics_hr_prob'] = physics_prob
                changed = True

            defaults = {
                'base_model_prob': base_prob,
                'physics_per_pa_hr_prob': pd.to_numeric(df.get('physics_per_pa_hr_prob', 0.0), errors='coerce').fillna(0.0),
                'density_altitude_ft': pd.to_numeric(df.get('density_altitude_ft', 0.0), errors='coerce').fillna(0.0),
                'air_density_kg_m3': pd.to_numeric(df.get('air_density_kg_m3', 1.225), errors='coerce').fillna(1.225),
                'drag_multiplier': pd.to_numeric(df.get('drag_multiplier', 1.0), errors='coerce').fillna(1.0),
                'pitch_micro_matchup_score': pd.to_numeric(df.get('pitch_micro_matchup_score', 1.0), errors='coerce').fillna(1.0),
                'vaa_attack_angle_score': pd.to_numeric(df.get('vaa_attack_angle_score', 1.0), errors='coerce').fillna(1.0),
                'umpire_catcher_cascade': pd.to_numeric(df.get('umpire_catcher_cascade', 1.0), errors='coerce').fillna(1.0),
                'fatigue_index': pd.to_numeric(df.get('fatigue_index', 0.0), errors='coerce').fillna(0.0),
                'spin_decay_rpm': pd.to_numeric(df.get('spin_decay_rpm', 0.0), errors='coerce').fillna(0.0),
                'spin_decay_flag': pd.to_numeric(df.get('spin_decay_flag', 0.0), errors='coerce').fillna(0.0),
                'lineup_protection_woba_proxy': pd.to_numeric(df.get('lineup_protection_woba_proxy', 0.10), errors='coerce').fillna(0.10),
                'context_multiplier': pd.to_numeric(df.get('context_multiplier', 1.0), errors='coerce').fillna(1.0),
                'blend_weight_physics': pd.to_numeric(df.get('blend_weight_physics', 0.45), errors='coerce').fillna(0.45),
                'line_release_window_flag': pd.to_numeric(df.get('line_release_window_flag', 0), errors='coerce').fillna(0),
                'nrfi_under_drag_score': pd.to_numeric(df.get('nrfi_under_drag_score', 0.0), errors='coerce').fillna(0.0),
                'prob_edge_abs': pd.to_numeric(df.get('prob_edge_abs', 0.0), errors='coerce').fillna(0.0),
            }

            for col, val in defaults.items():
                if col not in df.columns:
                    df[col] = val
                    changed = True

            if 'probability_mode' not in df.columns:
                df['probability_mode'] = 'backfilled'
                changed = True

            if 'physics_delta' not in df.columns:
                df['physics_delta'] = pd.to_numeric(df['physics_hr_prob'], errors='coerce').fillna(base_prob) - base_prob
                changed = True

            if changed:
                df.to_csv(f, index=False)
                updated += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1

    print(f"Backfill complete: updated={updated}, skipped={skipped}")


def print_weekly_todo(days_lookback=7):
    """Print a prioritized next-week action list from live system signals."""
    today_str = datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{today_str}.csv'

    print("\n" + "=" * 70)
    print("NEXT-WEEK TODO (AUTO-GENERATED)")
    print("=" * 70)

    todos = []

    # 1) Market integration blockers
    if not os.getenv('ODDS_API_KEY'):
        todos.append(("INFO", "⚠️  ODDS_API_KEY not set. System works fine without it - predictions still generated."))
        todos.append(("OPTIONAL", "📊 To unlock live market EV and RLM features: Get API key at https://the-odds-api.com (requires upgrade to Standard plan for player props). Add ODDS_API_KEY to .vscode/.env."))
        todos.append(("OPTIONAL", "🆓 Alternative: Configure free-source odds ingest (FREE_ODDS_JSON_PATH / FREE_ODDS_CSV_PATH / FREE_ODDS_PUBLIC_URLS)."))
    else:
        try:
            probe = fetch_hr_prop_odds()
            if not probe:
                todos.append(("HIGH", "🔑 Odds API key present but returning no data. Check if account is deactivated or plan tier is insufficient. Visit https://the-odds-api.com/admin to verify account status."))
        except Exception as e:
            todos.append(("HIGH", f"🔑 Odds API error: {str(e)[:60]}. Check account status and plan tier at https://the-odds-api.com/admin"))

    webhook = os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        todos.append(("HIGH", "Configure DISCORD_MLB_WEBHOOK or DISCORD_WEBHOOK_URL for delivery alerts."))

    # 2) Calibration readiness
    cutoff = datetime.today() - timedelta(days=days_lookback)
    pred_files = []
    eval_files = []
    for f in Path('data').glob('predictions_*.csv'):
        try:
            d = datetime.strptime(f.stem.replace('predictions_', ''), '%Y-%m-%d')
            if d >= cutoff:
                pred_files.append(f)
        except Exception:
            pass
    for f in Path('data').glob('evaluation_*.csv'):
        try:
            d = datetime.strptime(f.stem.replace('evaluation_', ''), '%Y-%m-%d')
            if d >= cutoff:
                eval_files.append(f)
        except Exception:
            pass

    if len(pred_files) < 3 or len(eval_files) < 3:
        todos.append(("HIGH", "Calibration sample too small. Accumulate at least 3-5 days of prediction+evaluation files for stable blend tuning."))

    # 3) Pitcher name coverage quality
    try:
        df_live = get_today_matchups()
        if df_live is not None and not df_live.empty and 'pitcher_name' in df_live.columns:
            unknown_rate = (df_live['pitcher_name'].fillna('').str.contains('Unknown', case=False)).mean()
            if unknown_rate > 0.15:
                todos.append(("MEDIUM", f"Unknown pitcher rate is {unknown_rate:.0%}. Improve probable starter resolution in matchup builder."))
    except Exception:
        if pred_file.exists():
            try:
                df = pd.read_csv(pred_file)
                if not df.empty and 'pitcher_name' in df.columns:
                    unknown_rate = (df['pitcher_name'].fillna('').str.contains('Unknown', case=False)).mean()
                    if unknown_rate > 0.15:
                        todos.append(("MEDIUM", f"Unknown pitcher rate is {unknown_rate:.0%}. Improve probable starter resolution in matchup builder."))
            except Exception:
                pass

    # 4) Scheduling and monitoring reliability
    weekly_maint = Path('.github') / 'workflows' / 'weekly_maintenance.yml'
    monthly_maint = Path('.github') / 'workflows' / 'monthly_backfill.yml'

    has_weekly_self_check = (
        _file_contains_text(weekly_maint, '--self-check') and
        _file_contains_text(weekly_maint, 'Upload self-check artifacts')
    )
    if not has_weekly_self_check:
        todos.append(("MEDIUM", "Add a weekly cron/task to run --self-check and archive output for drift tracking."))

    # Kelly drift review can be automated once enough evaluation files exist.
    if len(eval_files) < 3:
        todos.append(("MEDIUM", "Build 3-5 recent evaluation files so auto Kelly/Brier drift controls can engage."))

    has_monthly_backfill = _file_contains_text(monthly_maint, '--backfill-physics --backfill-days 120')
    if not has_monthly_backfill:
        todos.append(("LOW", "Expand backfill window monthly (--backfill-physics --backfill-days 120) to keep calibration history rich."))

    priority_rank = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
    todos = sorted(todos, key=lambda t: priority_rank.get(t[0], 9))

    if not todos:
        print("No immediate next-week actions detected.")
        return

    for i, (p, text) in enumerate(todos, 1):
        print(f"{i}. [{p}] {text}")


def _normalize_player_key(name):
    return str(name or '').strip().lower()


def _american_to_decimal_safe(odds):
    try:
        o = float(odds)
    except Exception:
        return np.nan
    if o > 0:
        return 1.0 + (o / 100.0)
    if o < 0:
        return 1.0 + (100.0 / abs(o))
    return np.nan


def _load_hr_hitter_name_set(date_str):
    """Load actual HR hitters for one date from Statcast cache/feed."""
    try:
        sc = load_or_fetch_statcast(date_str)
    except Exception:
        return set()
    if sc is None or sc.empty or 'events' not in sc.columns:
        return set()

    hr = sc[sc['events'] == 'home_run'].copy()
    if hr.empty:
        return set()

    names = set()
    if 'player_name' in hr.columns:
        names |= {_normalize_player_key(x) for x in hr['player_name'].dropna().tolist() if str(x).strip()}
    if 'batter_name' in hr.columns:
        names |= {_normalize_player_key(x) for x in hr['batter_name'].dropna().tolist() if str(x).strip()}
    return names


def learn_parlay_pair_multipliers(days_back=60, top_n_per_day=20):
    """Learn global pairing multipliers from historical prediction files and actual HR outcomes."""
    data_dir = Path('data')
    pred_files = sorted(data_dir.glob('predictions_*.csv'))
    if not pred_files:
        return {
            'global_mult': 1.0,
            'cross_game_mult': 1.0,
            'same_game_mult': 1.0,
            'dual_ev_mult': 1.0,
            'training_days_used': 0,
        }

    today = datetime.today().date()
    cutoff = today - timedelta(days=max(1, int(days_back)))

    g_obs = g_exp = 0.0
    x_obs = x_exp = 0.0
    s_obs = s_exp = 0.0
    ev_obs = ev_exp = 0.0
    days_used = 0

    for fp in pred_files:
        stem = fp.stem
        if not stem.startswith('predictions_'):
            continue
        ds = stem.replace('predictions_', '').strip()
        try:
            d = datetime.strptime(ds, '%Y-%m-%d').date()
        except Exception:
            continue
        if d >= today or d < cutoff:
            continue

        try:
            preds = pd.read_csv(fp)
        except Exception:
            continue
        if preds.empty or 'batter_name' not in preds.columns or 'pred_hr_prob' not in preds.columns:
            continue

        hr_names = _load_hr_hitter_name_set(ds)
        if not hr_names:
            continue

        cols = ['batter_name', 'pred_hr_prob', 'game_pk']
        if 'ev_percent' in preds.columns:
            cols.append('ev_percent')
        sub = preds[cols].copy()
        sub['pred_hr_prob'] = pd.to_numeric(sub['pred_hr_prob'], errors='coerce').fillna(0.0)
        if 'ev_percent' not in sub.columns:
            sub['ev_percent'] = 0.0
        sub['ev_percent'] = pd.to_numeric(sub['ev_percent'], errors='coerce').fillna(0.0)
        sub = sub.sort_values('pred_hr_prob', ascending=False).head(max(8, int(top_n_per_day)))
        sub = sub[sub['pred_hr_prob'] > 0].copy()
        if len(sub) < 2:
            continue

        days_used += 1
        rows = sub.to_dict('records')
        for a, b in combinations(rows, 2):
            p1 = float(a.get('pred_hr_prob', 0.0))
            p2 = float(b.get('pred_hr_prob', 0.0))
            base = p1 * p2
            if base <= 0:
                continue
            n1 = _normalize_player_key(a.get('batter_name', ''))
            n2 = _normalize_player_key(b.get('batter_name', ''))
            hit = 1.0 if (n1 in hr_names and n2 in hr_names) else 0.0

            g_obs += hit
            g_exp += base

            same_game = str(a.get('game_pk', '')) == str(b.get('game_pk', ''))
            if same_game:
                s_obs += hit
                s_exp += base
            else:
                x_obs += hit
                x_exp += base

            if float(a.get('ev_percent', 0.0)) > 0 and float(b.get('ev_percent', 0.0)) > 0:
                ev_obs += hit
                ev_exp += base

    def _ratio(obs, exp, default=1.0, lo=0.7, hi=1.5):
        if exp <= 0:
            return default
        return float(np.clip(obs / exp, lo, hi))

    return {
        'global_mult': _ratio(g_obs, g_exp, default=1.0),
        'cross_game_mult': _ratio(x_obs, x_exp, default=1.0),
        'same_game_mult': _ratio(s_obs, s_exp, default=1.0),
        'dual_ev_mult': _ratio(ev_obs, ev_exp, default=1.0),
        'training_days_used': int(days_used),
    }


def build_learned_hr_pairings(live_df, days_back=60, candidate_n=36):
    """Build learned 2-leg HR parlay pairings (any game) from historical co-hit behavior."""
    if live_df is None or live_df.empty:
        return pd.DataFrame()

    req_cols = {'batter_name', 'pitcher_name', 'pred_hr_prob', 'game_pk'}
    if not req_cols.issubset(set(live_df.columns)):
        return pd.DataFrame()

    work = live_df.copy()
    work['pred_hr_prob'] = pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0)
    work['ev_percent'] = pd.to_numeric(work.get('ev_percent', 0.0), errors='coerce').fillna(0.0)
    work['signal_score'] = work['pred_hr_prob'] + np.maximum(work['ev_percent'], 0) / 250.0
    work = work.sort_values(['signal_score', 'pred_hr_prob'], ascending=False).head(max(12, int(candidate_n)))
    if len(work) < 2:
        return pd.DataFrame()

    multipliers = learn_parlay_pair_multipliers(days_back=max(14, int(days_back)), top_n_per_day=20)
    rows = []
    for a, b in combinations(work.to_dict('records'), 2):
        p1 = float(a.get('pred_hr_prob', 0.0))
        p2 = float(b.get('pred_hr_prob', 0.0))
        if p1 <= 0 or p2 <= 0:
            continue

        same_game = str(a.get('game_pk', '')) == str(b.get('game_pk', ''))
        base_combo = p1 * p2
        multi = float(multipliers.get('global_mult', 1.0))
        multi *= float(multipliers.get('same_game_mult', 1.0) if same_game else multipliers.get('cross_game_mult', 1.0))
        if float(a.get('ev_percent', 0.0)) > 0 and float(b.get('ev_percent', 0.0)) > 0:
            multi *= float(multipliers.get('dual_ev_mult', 1.0))

        learned_combo = min(0.95, max(0.0, base_combo * multi))

        o1 = _american_to_decimal_safe(a.get('best_market_odds_american', np.nan))
        o2 = _american_to_decimal_safe(b.get('best_market_odds_american', np.nan))
        parlay_decimal = (o1 * o2) if (pd.notna(o1) and pd.notna(o2)) else np.nan
        parlay_ev = (learned_combo * parlay_decimal - 1.0) if pd.notna(parlay_decimal) else np.nan

        rows.append({
            'pair_leg_1': a.get('batter_name', ''),
            'pair_leg_2': b.get('batter_name', ''),
            'leg1_game_pk': a.get('game_pk', ''),
            'leg2_game_pk': b.get('game_pk', ''),
            'pair_type': 'same_game' if same_game else 'cross_game',
            'leg1_prob': p1,
            'leg2_prob': p2,
            'base_combo_prob': base_combo,
            'learned_multiplier': multi,
            'combo_prob': learned_combo,
            'leg1_odds_american': a.get('best_market_odds_american', np.nan),
            'leg2_odds_american': b.get('best_market_odds_american', np.nan),
            'parlay_decimal': parlay_decimal,
            'parlay_ev': parlay_ev,
            'training_days_used': int(multipliers.get('training_days_used', 0)),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    out = out.sort_values(['parlay_ev', 'combo_prob'], ascending=[False, False], na_position='last').reset_index(drop=True)
    return out


def run_systematic_ev_operation(backfill_days=90):
    """Run end-to-end +EV workflow for scale and repeatability.

    Steps:
    1) Backfill historical diagnostics for calibration stability
    2) Self-check runtime mode/weight and data readiness
    3) Generate today's predictions with 10k simulation physics engine
    4) Print next-week operational priorities
    """
    print("\n" + "=" * 70)
    print("SYSTEMATIC +EV OPERATION")
    print("=" * 70)
    backfill_physics_columns(days_lookback=max(1, int(backfill_days)))
    run_model_self_check(days_lookback=max(1, int(min(backfill_days, 30))))
    generate_daily_predictions()
    print_weekly_todo(days_lookback=7)


HR_PROP_MARKET_KEY_CANDIDATES = [
    'batter_home_runs',
    'batter_home_runs_alternate',
]

SPORTSGAMEODDS_BASE_URL = 'https://api.sportsgameodds.com/v2'
SPORTSGAMEODDS_DEFAULT_LEAGUE = 'MLB'

# Cooldown guard for invalid Odds API key spam.
_ODDS_API_INVALID_KEY_UNTIL_TS = 0.0
_ODDS_API_INVALID_KEY_LAST_LOG_TS = 0.0


def _odds_provider_name():
    raw = str(os.getenv('ODDS_API_PROVIDER', 'sportsgameodds') or '').strip().lower()
    if raw in {'sgo', 'sportsgameodds', 'sportsgameodds_v2', 'sports_game_odds'}:
        return 'sportsgameodds'
    if raw in {'theoddsapi', 'the-odds-api', 'oddsapi'}:
        return 'theoddsapi'
    return 'sportsgameodds'


def _decimal_to_american(decimal_odds):
    try:
        d = float(decimal_odds)
        if not np.isfinite(d) or d <= 1.0:
            return None
        if d >= 2.0:
            return int(round((d - 1.0) * 100.0))
        return int(round(-100.0 / (d - 1.0)))
    except Exception:
        return None


def _extract_american_odds_value(value):
    """Extract American odds from flexible provider payload shapes."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            f = float(value)
            if not np.isfinite(f):
                return None
            # American odds are usually outside (-100, 100) and not zero.
            if f == 0:
                return None
            if abs(f) >= 100:
                return int(round(f))
        except Exception:
            return None

    if isinstance(value, str):
        s = value.strip().replace(',', '')
        if not s:
            return None
        try:
            f = float(s)
            if abs(f) >= 100 and f != 0:
                return int(round(f))
            if f > 1.0:
                return _decimal_to_american(f)
        except Exception:
            return None

    if isinstance(value, dict):
        # Common explicit American fields.
        for k in [
            'american', 'americanOdds', 'oddsAmerican', 'priceAmerican',
            'line', 'price', 'value', 'odds'
        ]:
            if k in value:
                out = _extract_american_odds_value(value.get(k))
                if out is not None:
                    return out

        # Decimal fallback fields.
        for k in ['decimal', 'oddsDecimal', 'priceDecimal']:
            if k in value:
                out = _decimal_to_american(value.get(k))
                if out is not None:
                    return out

    return None


def _normalize_book_key(raw_book):
    bk = str(raw_book or '').strip().lower()
    if not bk:
        return 'unknown_book'
    return bk.replace(' ', '_')


def _normalize_sgo_player_name(raw_entity):
    txt = str(raw_entity or '').strip()
    if not txt:
        return ''
    txt = txt.replace('_', ' ').replace('-', ' ')
    txt = ' '.join(tok for tok in txt.split() if tok)
    if not txt:
        return ''
    low = txt.lower()
    if low in {'home', 'away', 'all', 'game', 'team', 'teams'}:
        return ''
    # Strip common SGO suffixes like "_1_MLB" if they survive normalization.
    txt = re.sub(r'\s+\d+\s+mlb$', '', txt, flags=re.IGNORECASE)
    return txt


def _parse_sgo_event_start_time(event_obj):
    for key in ['startTime', 'startDate', 'startsAt', 'commence_time', 'commenceTime']:
        raw = str((event_obj or {}).get(key, '')).strip()
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except Exception:
            continue
    return None


def _get_hr_prop_market_key_candidates():
    """Return market key candidates, allowing env override for fast recovery.

    Use ODDS_API_HR_MARKETS="k1,k2,..." to override defaults without code edits.
    """
    raw = str(os.getenv('ODDS_API_HR_MARKETS', '') or '').strip()
    if not raw:
        return list(HR_PROP_MARKET_KEY_CANDIDATES)

    out = []
    for tok in raw.split(','):
        key = tok.strip()
        if key and key not in out:
            out.append(key)
    return out or list(HR_PROP_MARKET_KEY_CANDIDATES)


def _odds_api_invalid_market(status_code, body_text):
    """Return True when Odds API indicates an unsupported market key."""
    if int(status_code) != 422:
        return False
    body = str(body_text or '').lower()
    return ('invalid_market' in body) or ('invalid markets' in body) or ('markets not supported' in body)


def _odds_api_invalid_key(status_code, body_text):
    """Return True when Odds API indicates an invalid API key."""
    body = str(body_text or '').lower()
    return int(status_code) in {401, 403} and (
        'invalid_key' in body or 'api key is not valid' in body or 'invalid api key' in body
    )


def _odds_invalid_key_cooldown_active():
    try:
        return time.time() < float(_ODDS_API_INVALID_KEY_UNTIL_TS)
    except Exception:
        return False


def _set_odds_invalid_key_cooldown(reason_text=''):
    global _ODDS_API_INVALID_KEY_UNTIL_TS, _ODDS_API_INVALID_KEY_LAST_LOG_TS
    cooldown_seconds = max(60, int(str(os.getenv('ODDS_API_INVALID_KEY_COOLDOWN_SECONDS', '900')).strip() or '900'))
    now_ts = time.time()
    _ODDS_API_INVALID_KEY_UNTIL_TS = now_ts + cooldown_seconds
    # Log at most once per cooldown window.
    if (now_ts - float(_ODDS_API_INVALID_KEY_LAST_LOG_TS)) >= cooldown_seconds:
        _ODDS_API_INVALID_KEY_LAST_LOG_TS = now_ts
        mins = int(round(cooldown_seconds / 60.0))
        print(
            "Odds API key appears invalid; suppressing Odds API polling for "
            f"{mins}m to prevent log spam. {str(reason_text)[:140]}"
        )


def _parse_odds_event_commence_time(event_obj):
    """Parse Odds API commence_time into a datetime, or None if unavailable."""
    try:
        ts = str((event_obj or {}).get('commence_time', '')).strip()
        if not ts:
            return None
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


def _filter_current_slate_sgo_events(events_payload):
    """Keep only current slate events from Sportsgameodds payload."""
    events_payload = list(events_payload or [])
    if not events_payload:
        return []

    today_local = datetime.today().date()
    allowed_dates = {today_local, (today_local + timedelta(days=1))}
    kept = []
    stale_count = 0
    unknown_count = 0
    outside_count = 0

    for ev in events_payload:
        dt = _parse_sgo_event_start_time(ev)
        if dt is None:
            unknown_count += 1
            continue
        d = dt.date()
        if d < today_local:
            stale_count += 1
            continue
        if d not in allowed_dates:
            outside_count += 1
            continue
        kept.append(ev)

    strict = str(os.getenv('ODDS_ENFORCE_CURRENT_SLATE', 'true')).strip().lower() not in {'0', 'false', 'no'}
    print(
        f"SGO event filter: kept={len(kept)}/{len(events_payload)} "
        f"(stale={stale_count}, unknown_time={unknown_count}, outside_window={outside_count}, strict={strict})"
    )
    if not kept and unknown_count > 0 and stale_count == 0 and outside_count == 0:
        # Some SGO payload shapes omit explicit event time fields at top level.
        # In that case keep all events and rely on odds market filters.
        return events_payload
    if strict and stale_count > 0:
        raise RuntimeError(
            f"Stale SGO events detected ({stale_count}); aborting odds ingest because ODDS_ENFORCE_CURRENT_SLATE=true"
        )

    return kept


def _is_hr_market_sgo(odd_id, odd_obj):
    odd_id_l = str(odd_id or '').lower()
    stat_id_l = str((odd_obj or {}).get('statID') or (odd_obj or {}).get('statId') or '').lower()
    text = f"{odd_id_l} {stat_id_l}"
    # Target player anytime home-runs markets and exclude first-HR race props.
    if 'firsthomerun' in text or 'first_home_run' in text:
        return False
    return ('batting_homeruns' in text) or ('batting_homerun' in text)


def _extract_player_from_sgo_odd_id(odd_id):
    """Extract player token from oddID like batting_homeRuns-ALEC_BOHM_1_MLB-game-yn-yes."""
    s = str(odd_id or '').strip()
    if '-' not in s:
        return ''
    parts = s.split('-')
    if len(parts) < 2:
        return ''
    token = parts[1].strip()
    token = re.sub(r'_\d+_MLB$', '', token, flags=re.IGNORECASE)
    token = token.replace('_', ' ')
    return _normalize_sgo_player_name(token)


def _extract_hr_prop_player_book_odds_from_sgo(events_payload):
    """Extract {player_name: {book_key: american_odds}} from Sportsgameodds events payload."""
    out = {}

    def _merge_price(player_name, book_key, price):
        if not player_name or not book_key or price is None:
            return
        if player_name not in out:
            out[player_name] = {}

        existing = out[player_name].get(book_key)
        if existing is None:
            out[player_name][book_key] = price
            return

        # Prefer bettor-friendlier payout for the same book.
        try:
            existing_dec = (1 + (existing / 100.0)) if existing > 0 else (1 + (100.0 / abs(existing)))
            new_dec = (1 + (price / 100.0)) if price > 0 else (1 + (100.0 / abs(price)))
            if new_dec > existing_dec:
                out[player_name][book_key] = price
        except Exception:
            pass

    for ev in events_payload or []:
        odds_blob = ev.get('odds', {})
        if not isinstance(odds_blob, dict):
            continue

        for odd_id, odd_obj in odds_blob.items():
            if not isinstance(odd_obj, dict):
                continue
            if not _is_hr_market_sgo(odd_id, odd_obj):
                continue

            side = str(odd_obj.get('sideID') or odd_obj.get('sideId') or '').lower()
            if side in {'under', 'no'}:
                continue

            entity = (
                odd_obj.get('statEntityID') or odd_obj.get('statEntityId') or odd_obj.get('playerName')
                or odd_obj.get('participantID') or odd_obj.get('name')
            )
            player_name = _normalize_sgo_player_name(entity)
            if not player_name:
                player_name = _extract_player_from_sgo_odd_id(odd_id)
            if not player_name:
                continue

            by_book = odd_obj.get('byBookmaker')
            if by_book is None:
                by_book = odd_obj.get('bySportsbook')
            if by_book is None:
                by_book = odd_obj.get('sportsbooks')

            if isinstance(by_book, dict):
                for bk, quote in by_book.items():
                    price = _extract_american_odds_value(quote)
                    if price is None:
                        continue
                    _merge_price(player_name, _normalize_book_key(bk), price)
            elif isinstance(by_book, list):
                for row in by_book:
                    if not isinstance(row, dict):
                        continue
                    bk = row.get('bookmakerID') or row.get('bookmakerId') or row.get('bookID') or row.get('book')
                    price = _extract_american_odds_value(row)
                    if price is None:
                        continue
                    _merge_price(player_name, _normalize_book_key(bk), price)

    return out


def _fetch_hr_props_raw_from_sportsgameodds(api_key):
    """Fetch MLB HR props from Sportsgameodds v2 /events."""
    if _odds_invalid_key_cooldown_active():
        return {}

    if requests is None:
        return {}

    headers = {'x-api-key': str(api_key or '').strip()}
    params = {
        'leagueID': str(os.getenv('SPORTSGAMEODDS_LEAGUE_ID', SPORTSGAMEODDS_DEFAULT_LEAGUE)).strip() or 'MLB',
        'oddsAvailable': 'true',
        'limit': str(max(25, int(float(os.getenv('SPORTSGAMEODDS_LIMIT', '250'))))),
    }
    max_pages = max(1, int(float(os.getenv('SPORTSGAMEODDS_MAX_PAGES', '3'))))
    all_events = []
    cursor = None

    for _ in range(max_pages):
        call_params = dict(params)
        if cursor:
            call_params['cursor'] = cursor

        try:
            resp = requests.get(f"{SPORTSGAMEODDS_BASE_URL}/events", headers=headers, params=call_params, timeout=12)
        except Exception as e:
            print(f"Sportsgameodds fetch failed: {e}")
            break

        if resp.status_code != 200:
            body = (getattr(resp, 'text', '') or '')[:220]
            if _odds_api_invalid_key(resp.status_code, body):
                _set_odds_invalid_key_cooldown(reason_text=body)
                return {}
            print(f"Sportsgameodds returned {resp.status_code}: {body}")
            break

        payload = resp.json() or {}
        if isinstance(payload, dict) and payload.get('success') is False:
            err = str(payload.get('error', 'unknown_error'))
            if 'api key' in err.lower() or 'invalid' in err.lower():
                _set_odds_invalid_key_cooldown(reason_text=err)
                return {}
            print(f"Sportsgameodds error: {err[:180]}")
            break

        batch = payload.get('data') if isinstance(payload, dict) else payload
        batch = list(batch or [])
        all_events.extend(batch)

        next_cursor = payload.get('nextCursor') if isinstance(payload, dict) else None
        if not next_cursor:
            break
        cursor = str(next_cursor)

    if not all_events:
        return {}

    filtered = _filter_current_slate_sgo_events(all_events)
    parsed = _extract_hr_prop_player_book_odds_from_sgo(filtered)
    if parsed:
        n_pairs = sum(len(v) for v in parsed.values())
        print(f"Sportsgameodds HR props: {n_pairs} book-player pairs across {len(parsed)} players")
    else:
        print("Sportsgameodds returned no recognized HR prop outcomes in current slate.")
    return parsed


def _filter_current_slate_odds_events(events_payload, source_label='odds'):
    """Keep only current-slate odds events and reject stale historical events.

    Uses local "today" plus +1 UTC date allowance so late-night U.S. games
    (which can be next-day in UTC) are still accepted.
    """
    events_payload = list(events_payload or [])
    if not events_payload:
        return []

    strict = str(os.getenv('ODDS_ENFORCE_CURRENT_SLATE', 'true')).strip().lower() not in {'0', 'false', 'no'}
    today_local = datetime.today().date()
    allowed_dates = {today_local, (today_local + timedelta(days=1))}

    kept = []
    stale_count = 0
    unknown_count = 0
    future_outside_count = 0

    for ev in events_payload:
        dt = _parse_odds_event_commence_time(ev)
        if dt is None:
            unknown_count += 1
            continue

        ev_date = dt.date()
        if ev_date < today_local:
            stale_count += 1
            continue
        if ev_date not in allowed_dates:
            future_outside_count += 1
            continue
        kept.append(ev)

    print(
        f"Odds event filter ({source_label}): kept={len(kept)}/{len(events_payload)} "
        f"(stale={stale_count}, unknown_time={unknown_count}, outside_window={future_outside_count}, strict={strict})"
    )

    if strict and stale_count > 0:
        raise RuntimeError(
            f"Stale odds events detected ({stale_count}) in {source_label}; "
            f"aborting odds ingest because ODDS_ENFORCE_CURRENT_SLATE=true"
        )

    return kept


def _extract_hr_prop_player_book_odds(games_payload, market_keys=None):
    """Extract {player_name: {book_key: american_odds}} from The Odds API payload."""
    market_keys = set(market_keys or HR_PROP_MARKET_KEY_CANDIDATES)
    player_all_odds = {}
    for game in games_payload or []:
        for book in game.get('bookmakers', []):
            book_key = book.get('key', '')
            for market in book.get('markets', []):
                if market.get('key') not in market_keys:
                    continue
                for outcome in market.get('outcomes', []):
                    outcome_name = str(outcome.get('name', '')).strip()
                    # Event-level player props commonly use outcome.name as Over/Under
                    # and store the player in outcome.description.
                    player_name = str(outcome.get('description', '')).strip() or outcome_name
                    price = outcome.get('price')

                    if not player_name or price is None:
                        continue

                    # Keep only HR "to hit" side for Over/Under or Yes/No style markets.
                    side = outcome_name.lower()
                    if side in {'under', 'no'}:
                        continue

                    if player_name not in player_all_odds:
                        player_all_odds[player_name] = {}
                    player_all_odds[player_name][book_key] = price
    return player_all_odds


def _fetch_hr_props_raw_from_odds_api(api_key):
    """Fetch HR props from The Odds API with fallback to event-level endpoint."""
    if _odds_invalid_key_cooldown_active():
        return {}

    market_keys = _get_hr_prop_market_key_candidates()
    unsupported_markets = []
    aggregated = {}

    def _merge_player_book_odds(target, incoming):
        """Merge incoming {player: {book: odds}} preserving existing entries."""
        for name, books in (incoming or {}).items():
            if name not in target:
                target[name] = {}
            for bk, odds in (books or {}).items():
                if bk not in target[name]:
                    target[name][bk] = odds

    for market_key in market_keys:
        # Try top-level odds endpoint first.
        top_url = (
            f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
            f"?apiKey={api_key}&regions=us,us2&markets={market_key}"
            f"&oddsFormat=american&dateFormat=iso"
        )
        try:
            resp = requests.get(top_url, timeout=10)
            if resp.status_code == 200:
                top_payload = _filter_current_slate_odds_events(resp.json(), source_label='top-level')
                parsed = _extract_hr_prop_player_book_odds(top_payload, market_keys=[market_key])
                if parsed:
                    n_pairs = sum(len(v) for v in parsed.values())
                    print(f"Odds API ({market_key}): {n_pairs} book-player pairs across {len(parsed)} players")
                    _merge_player_book_odds(aggregated, parsed)
                    continue
                print(f"Odds API top-level endpoint returned no {market_key} outcomes; trying event-level endpoint.")
            else:
                body = (getattr(resp, 'text', '') or '')[:220]
                if _odds_api_invalid_key(resp.status_code, body):
                    _set_odds_invalid_key_cooldown(reason_text=body)
                    return aggregated
                if _odds_api_invalid_market(resp.status_code, body):
                    unsupported_markets.append(market_key)
                    print(f"Odds API market unsupported for this key/plan ({market_key}); trying next candidate.")
                    continue
                print(f"Odds API returned {resp.status_code} on top-level props endpoint ({market_key}): {body}")
        except Exception as e:
            print(f"Odds API top-level fetch failed ({market_key}): {e}")

        # Fallback: event-level props endpoint (works for some plan/market combinations).
        try:
            events_url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={api_key}&dateFormat=iso"
            events_resp = requests.get(events_url, timeout=10)
            if events_resp.status_code != 200:
                body = (getattr(events_resp, 'text', '') or '')[:220]
                if _odds_api_invalid_key(events_resp.status_code, body):
                    _set_odds_invalid_key_cooldown(reason_text=body)
                    return aggregated
                print(f"Odds API events list returned {events_resp.status_code}: {body}")
                continue

            events = _filter_current_slate_odds_events(events_resp.json() or [], source_label='events-list')
            if not events:
                print("Odds API event list is empty.")
                continue

            merged = {}
            event_422_count = 0
            event_req_count = 0
            for ev in events:
                ev_id = ev.get('id')
                if not ev_id:
                    continue
                event_req_count += 1
                ev_url = (
                    f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{ev_id}/odds"
                    f"?apiKey={api_key}&regions=us,us2&markets={market_key}"
                    f"&oddsFormat=american&dateFormat=iso"
                )
                ev_resp = requests.get(ev_url, timeout=10)
                if ev_resp.status_code != 200:
                    body = (getattr(ev_resp, 'text', '') or '')[:220]
                    if _odds_api_invalid_market(ev_resp.status_code, body):
                        event_422_count += 1
                    continue

                ev_payload = ev_resp.json() or {}
                chunk = _extract_hr_prop_player_book_odds([ev_payload], market_keys=[market_key])
                for name, books in chunk.items():
                    if name not in merged:
                        merged[name] = {}
                    merged[name].update(books)

            if merged:
                n_pairs = sum(len(v) for v in merged.values())
                print(f"Odds API event-level fallback ({market_key}): {n_pairs} book-player pairs across {len(merged)} players")
                _merge_player_book_odds(aggregated, merged)
                continue

            if event_req_count > 0 and event_422_count == event_req_count:
                if market_key not in unsupported_markets:
                    unsupported_markets.append(market_key)
                print(f"Odds API market unsupported at event-level ({market_key}); trying next candidate.")
                continue

            print(f"Odds API event-level fallback returned no {market_key} outcomes.")
        except Exception as e:
            print(f"Odds API event-level fallback failed ({market_key}): {e}")

    if unsupported_markets:
        print(
            "Odds API HR markets unsupported for current account/settings: "
            f"{', '.join(unsupported_markets)}. "
            "Set ODDS_API_HR_MARKETS to valid keys if your plan uses different names."
        )

    return aggregated


def _build_devigged_probs_from_raw_books(player_all_odds):
    """Build consensus devigged probabilities from per-book American odds."""
    player_probs = {}
    for name, book_odds in (player_all_odds or {}).items():
        weighted_probs = []
        for bk, odds in (book_odds or {}).items():
            try:
                odds = float(odds)
                raw_implied = abs(odds) / (abs(odds) + 100) if odds < 0 else 100 / (odds + 100)
                devigged = raw_implied * 0.952
                weight = 2 if bk in SHARP_BOOKS else 1
                weighted_probs.extend([devigged] * weight)
            except Exception:
                continue
        if weighted_probs:
            player_probs[name] = round(sum(weighted_probs) / len(weighted_probs), 4)
    return player_probs


def fetch_hr_prop_odds():
    """Fetch live HR prop lines and return {player_name: devigged_prob}.

    Provider is selected by ODDS_API_PROVIDER:
    - sportsgameodds (default)
    - theoddsapi
    """
    api_key = os.getenv('ODDS_API_KEY')
    provider = _odds_provider_name()
    if not api_key:
        if load_free_odds_sources is not None and build_devigged_probs_from_books is not None:
            raw_free = load_free_odds_sources()
            if raw_free:
                probs = build_devigged_probs_from_books(raw_free)
                print(f"Free source odds: {sum(len(v) for v in raw_free.values())} book-player pairs across {len(probs)} players")
                return probs
        return {}
    if _odds_invalid_key_cooldown_active():
        if load_free_odds_sources is not None and build_devigged_probs_from_books is not None:
            raw_free = load_free_odds_sources()
            if raw_free:
                return build_devigged_probs_from_books(raw_free)
        return {}
    try:
        if provider == 'theoddsapi':
            player_all_odds = _fetch_hr_props_raw_from_odds_api(api_key)
        else:
            player_all_odds = _fetch_hr_props_raw_from_sportsgameodds(api_key)
        if not player_all_odds:
            if load_free_odds_sources is not None and build_devigged_probs_from_books is not None:
                raw_free = load_free_odds_sources()
                if raw_free:
                    probs = build_devigged_probs_from_books(raw_free)
                    print(f"Free source odds fallback: {sum(len(v) for v in raw_free.values())} book-player pairs across {len(probs)} players")
                    return probs
            return {}

        probs = _build_devigged_probs_from_raw_books(player_all_odds)
        if probs:
            print(f"Odds provider active: {provider} ({len(probs)} players with devigged prices)")
        return probs
    except Exception as e:
        print(f"Odds fetch failed ({provider}): {e}")
        if load_free_odds_sources is not None and build_devigged_probs_from_books is not None:
            raw_free = load_free_odds_sources()
            if raw_free:
                probs = build_devigged_probs_from_books(raw_free)
                print(f"Free source odds fallback: {sum(len(v) for v in raw_free.values())} book-player pairs across {len(probs)} players")
                return probs
        return {}


def fetch_hr_prop_odds_raw():
    """Fetch HR prop lines from ALL sportsbooks. Returns {player: {book_key: american_odds}}.
    Used for RLM monitoring and per-book line movement tracking."""
    api_key = os.getenv('ODDS_API_KEY')
    provider = _odds_provider_name()
    if not api_key:
        if load_free_odds_sources is not None:
            return load_free_odds_sources()
        return {}
    if _odds_invalid_key_cooldown_active():
        if load_free_odds_sources is not None:
            return load_free_odds_sources()
        return {}
    try:
        if provider == 'theoddsapi':
            raw = _fetch_hr_props_raw_from_odds_api(api_key)
        else:
            raw = _fetch_hr_props_raw_from_sportsgameodds(api_key)
        if not raw:
            if load_free_odds_sources is not None:
                return load_free_odds_sources()
            return {}
        return raw
    except Exception as e:
        print(f"Odds raw fetch failed ({provider}): {e}")
        if load_free_odds_sources is not None:
            return load_free_odds_sources()
        return {}


def american_to_implied_prob(odds):
    odds = float(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def prob_to_fair_american(prob):
    p = max(1e-6, min(1 - 1e-6, float(prob)))
    if p >= 0.5:
        return -round((p / (1 - p)) * 100)
    return round(((1 - p) / p) * 100)


def is_line_release_window_et(now_utc=None):
    now_utc = now_utc or datetime.utcnow()
    now_et = now_utc - timedelta(hours=4)
    # Optimal window: 11:30 AM - 1:00 PM EST
    # Check if between 11:30 AM and 1:00 PM ET (before 14:00 to account for minute precision)
    hour = now_et.hour
    minute = now_et.minute
    
    # 11:30 AM to 1:00 PM means:
    # - Hour 11 with minute >= 30, OR
    # - Hour 12, OR  
    # - Hour 13 (1 PM) with minute < 60
    if hour == 11:
        return minute >= 30
    elif hour == 12:
        return True
    elif hour == 13:
        return minute < 60
    return False


def fetch_totals_market_pressure():
    """Fetch totals market and return under-bias pressure keyed by matchup name.

    Returns dict: "away @ home" -> score in [0, 1], where higher means heavier under bias.
    """
    api_key = os.getenv('ODDS_API_KEY')
    if not api_key:
        return {}
    if _odds_provider_name() != 'theoddsapi':
        # Totals pressure path is The-Odds-API-specific schema.
        return {}
    try:
        url = (
            f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
            f"?apiKey={api_key}&regions=us,us2&markets=totals"
            f"&oddsFormat=american&dateFormat=iso"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return {}

        pressure = {}
        for game in resp.json():
            away = str(game.get('away_team', '')).strip()
            home = str(game.get('home_team', '')).strip()
            key = f"{away} @ {home}"
            under_prices = []

            for book in game.get('bookmakers', []):
                for market in book.get('markets', []):
                    if market.get('key') != 'totals':
                        continue
                    for outcome in market.get('outcomes', []):
                        if str(outcome.get('name', '')).lower() == 'under' and outcome.get('price') is not None:
                            under_prices.append(float(outcome['price']))

            if under_prices:
                under_probs = [american_to_implied_prob(p) for p in under_prices]
                avg_under_prob = float(sum(under_probs) / len(under_probs))
                # 0.50 is neutral. >0.54 means meaningful under pressure.
                pressure[key] = max(0.0, min(1.0, (avg_under_prob - 0.50) / 0.12))

        return pressure
    except Exception:
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

    print(f"Loading historical metrics from cache and Baseball Savant (last {days_back} days)...")
    cached_count = 0
    fetched_count = 0
    for i in range(1, days_back + 1):
        target_date = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        cache_file = os.path.join(cache_dir, f"statcast_{target_date}.csv")

        if os.path.exists(cache_file):
            try:
                all_days_data.append(pd.read_csv(cache_file))
                cached_count += 1
                continue
            except Exception as e:
                print(f"  Warning: Could not read cache for {target_date}: {str(e)[:40]}")

        try:
            day_df = statcast_with_timeout(start_dt=target_date, end_dt=target_date)
            if day_df is not None and not day_df.empty:
                day_df.to_csv(cache_file, index=False)
                all_days_data.append(day_df)
                fetched_count += 1
        except Exception as e:
            print(f"  Warning: Could not fetch Statcast for {target_date}: {str(e)[:40]}")

    print(f"✅ Loaded {cached_count} days from cache, fetched {fetched_count} days from Baseball Savant")
    
    if not all_days_data:
        raise ValueError("Critical Error: No Statcast training data available from cache or Baseball Savant.")

    df = pd.concat(all_days_data, ignore_index=True)
    pitch_df = df.copy()
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
    if get_ballpark_factor is not None:
        pa_df['park_factor'] = pa_df.apply(
            lambda r: float(get_ballpark_factor(r.get('park_team', ''), r.get('stand', 'R')).get('park_factor', 1.0)) * 100.0,
            axis=1,
        )
    else:
        pa_df['park_factor'] = pa_df['park_team'].map(PARK_HR_FACTORS).fillna(100)
    
    # Fill baseline weather placeholders then enrich with historical weather backfill.
    pa_df['temp'] = 71.0
    pa_df['wind_speed'] = 5.0
    pa_df['wind_out_component'] = 0.0
    pa_df['humidity'] = 50.0
    pa_df['precipitation'] = 0.0
    pa_df['pressure'] = 1013.25
    pa_df = enrich_training_weather_from_history(pa_df)

    # Build Player Vector Profiles
    batter_stats = pa_df.groupby('batter').agg(
        bat_pa_count=('events', 'count'),
        bat_hr_rate=('is_hr', 'mean'),
        bat_barrel_rate=('is_barrel', 'mean'),
        bat_hard_hit_rate=('is_hard_hit', 'mean'),
        bat_sweet_spot_rate=('is_sweet_spot', 'mean'),
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
        bat_avg_exit_velocity=('launch_speed', lambda x: float(x[x > 0].mean()) if (x > 0).sum() > 0 else 88.0),
        bat_max_exit_velocity=('launch_speed', lambda x: float(x[x > 0].max()) if (x > 0).sum() > 0 else 102.0),
        bat_avg_launch_angle=('launch_angle', lambda x: float(pd.to_numeric(x, errors='coerce').dropna().mean()) if pd.to_numeric(x, errors='coerce').dropna().shape[0] > 0 else 12.0),
        bat_ev90=('launch_speed', lambda x: float(x[x > 0].quantile(0.90)) if (x > 0).sum() > 5 else 88.0),
        bat_iso_proxy=('is_xbh', 'mean'),
        bat_pulled_fly_count=('is_pulled_fly', 'sum')
    ).reset_index()
    batter_stats['bat_hr_fb_rate'] = batter_stats['bat_total_hr'] / batter_stats['bat_total_fb'].clip(lower=1)
    batter_stats['bat_pull_rate'] = batter_stats['bat_pulled_fly_count'] / batter_stats['bat_total_fb'].clip(lower=1)
    
    # Add wRC+ (Weighted Runs Created+) - normalized to league average of 100
    # wRC+ = 100 * [(HRs * 1.4 + SweetSpot% * 0.8 + AvgEV bonus) / league average]
    # Approximation: higher HR rate, sweet spot rate, and exit velocity = higher wRC+
    batter_stats['bat_wrc_plus'] = (
        100 * (
            (batter_stats['bat_hr_rate'] * 1.4 * 60) +  # HR component (scaled to league HR rate ~0.033)
            (batter_stats['bat_sweet_spot_rate'] * 0.8 * 100) +  # Sweet spot contact quality
            ((batter_stats['bat_avg_exit_velocity'] - 85) / 5 * 10)  # Exit velo bonus (normalized)
        ) / 60  # Normalize to league average ~100
    ).clip(lower=30, upper=200)  # Realistic wRC+ range
    
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
        pitch_sweet_spot_allowed_rate=('is_sweet_spot', 'mean'),
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
    pitcher_stats['pitch_fb_allowed_rate'] = pitcher_stats['pitch_total_fb'] / pitcher_stats['pitch_pa_count'].clip(lower=1)
    pitcher_stats['pitch_est_ip'] = pitcher_stats['pitch_pa_count'] / 4.3
    pitcher_stats['pitch_hr_per_9'] = (pitcher_stats['pitch_total_hr'] * 9) / pitcher_stats['pitch_est_ip'].clip(lower=1)
    _last_pit = pa_df.groupby('pitcher')['game_date'].max().reset_index()
    _last_pit['pitch_days_since_last_start'] = (today_date - _last_pit['game_date']).dt.days.clip(0, 30)
    pitcher_stats = pitcher_stats.merge(_last_pit[['pitcher', 'pitch_days_since_last_start']], on='pitcher', how='left')
    pitcher_stats['pitch_days_since_last_start'] = pitcher_stats['pitch_days_since_last_start'].fillna(5)

    return batter_stats, pitcher_stats, pa_df, pitch_df

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
        probable_home_pitcher = game.get('home_probable_pitcher', 'Unknown Pitcher')
        probable_away_pitcher = game.get('away_probable_pitcher', 'Unknown Pitcher')

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
            boxscore = statsapi.boxscore_data(game_id) or {}
            raw_game = None
            raw_teams = {}
            if not boxscore or not (boxscore.get('home', {}).get('battingOrder') or boxscore.get('away', {}).get('battingOrder')):
                raw_game = statsapi.get('game', {'gamePk': game_id}) or {}
                raw_teams = raw_game.get('liveData', {}).get('boxscore', {}).get('teams', {}) or {}
                if not boxscore:
                    boxscore = {}

            home_team_abbr = boxscore.get('home', {}).get('abbreviation', team_abbrev)
            away_team_abbr = boxscore.get('away', {}).get('abbreviation', '')
            if raw_game:
                home_team_abbr = raw_game.get('gameData', {}).get('teams', {}).get('home', {}).get('abbreviation', home_team_abbr)
                away_team_abbr = raw_game.get('gameData', {}).get('teams', {}).get('away', {}).get('abbreviation', away_team_abbr)
            if not home_team_abbr:
                try:
                    home_id = game.get('home_id')
                    if home_id:
                        home_team_abbr = statsapi.get('team', {'teamId': home_id}).get('teams', [{}])[0].get('abbreviation', team_abbrev)
                    else:
                        home_team_abbr = statsapi.lookup_team(game.get('home_name', ''))[0].get('abbreviation', team_abbrev)
                except Exception:
                    home_team_abbr = team_abbrev
            if not away_team_abbr:
                try:
                    away_id = game.get('away_id')
                    if away_id:
                        away_team_abbr = statsapi.get('team', {'teamId': away_id}).get('teams', [{}])[0].get('abbreviation', '')
                    else:
                        away_team_abbr = statsapi.lookup_team(game.get('away_name', ''))[0].get('abbreviation', '')
                except Exception:
                    away_team_abbr = ''

            for team_type in ['home', 'away']:
                opponent_type = 'away' if team_type == 'home' else 'home'
                team_info = boxscore.get(team_type, {})
                opp_info = boxscore.get(opponent_type, {})
                raw_team_info = raw_teams.get(team_type, {}) if raw_teams else {}
                raw_opp_info = raw_teams.get(opponent_type, {}) if raw_teams else {}

                if (not team_info or not opp_info) and (raw_team_info and raw_opp_info):
                    team_info = raw_team_info
                    opp_info = raw_opp_info
                elif not team_info or not opp_info:
                    continue

                pitchers = opp_info.get('pitchers') or []
                pitcher_id = pitchers[0] if pitchers else None
                p_throws = 'R'
                if pitcher_id:
                    pitcher_player_info = opp_info.get('players', {}).get(f"ID{pitcher_id}", {})
                    p_throws = pitcher_player_info.get('stats', {}).get('pitching', {}).get('pitchHand', 'R')
                    if not p_throws:
                        p_throws = 'R'

                batting_order = team_info.get('battingOrder') or team_info.get('batters') or []
                if not batting_order and get_game_lineups is not None:
                    fallback_lineups = get_game_lineups(game_id) or {}
                    side_key = f'{team_type}_players'
                    fallback_players = fallback_lineups.get(side_key, [])
                    batting_order = [str(p.get('id', '')).replace('ID', '') for p in fallback_players if p.get('is_batter')][:9]

                seen_batters = set()
                for order_idx, batter_id in enumerate(batting_order):
                    batter_id = str(batter_id).replace('ID', '')
                    if not batter_id or batter_id in seen_batters:
                        continue
                    seen_batters.add(batter_id)
                    batter_player_info = team_info.get('players', {}).get(f"ID{batter_id}", {})
                    if not batter_player_info and raw_team_info:
                        batter_player_info = raw_team_info.get('players', {}).get(f"ID{batter_id}", {})
                    batter_person = batter_player_info.get('person', {})
                    batter_name = batter_person.get('fullName', 'Unknown Batter')

                    b_stands = batter_player_info.get('batSide', {}).get('code')
                    if not b_stands:
                        b_stands = batter_player_info.get('stats', {}).get('batting', {}).get('batSide', 'R')
                    if not b_stands:
                        b_stands = 'R'

                    if get_ballpark_factor is not None:
                        park_factor_data = get_ballpark_factor(home_team_abbr, b_stands)
                        handed_park_factor = float(park_factor_data.get('park_factor', park_factor / 100.0)) * 100.0
                    else:
                        handed_park_factor = float(park_factor)

                    pitcher_player = opp_info.get('players', {}).get(f"ID{pitcher_id}", {}).get('person', {}) if pitcher_id else {}
                    pitcher_name = pitcher_player.get('fullName', 'Unknown Pitcher')

                    if not pitcher_name or pitcher_name == 'Unknown Pitcher':
                        pitcher_name = probable_away_pitcher if team_type == 'home' else probable_home_pitcher

                    matchups.append({
                        'game_pk': game_id,
                        'game_id': game_id,
                        'game_time': game_time_str,
                        'venue_id': venue_id,
                        'venue_name': venue_name,
                        'home_name': game.get('home_name', ''),
                        'away_name': game.get('away_name', ''),
                        'home_team': home_team_abbr,
                        'away_team': away_team_abbr,
                        'team_side': f"{team_type}_{game_id}",
                        'is_home_game': team_type == 'home',
                        'batter': batter_id,
                        'batter_name': batter_name,
                        'batter_hand': b_stands,
                        'pitcher': pitcher_id,
                        'pitcher_name': pitcher_name,
                        'pitcher_hand': p_throws,
                        'has_platoon_advantage': int(b_stands != p_throws),
                        'batting_order_slot': order_idx + 1,
                        'wind_out_component': wind_out_component,
                        'park_factor': handed_park_factor,
                        'temp': weather['temp'],
                        'wind_speed': weather['wind_speed'],
                        'humidity': weather.get('humidity', 50),
                        'precipitation': weather.get('precipitation', 0),
                        'pressure': weather.get('pressure', 1013.25)
                    })
        except Exception as e:
            print(f"Warning: failed to build matchups for game {game_id}: {e}")
            continue

    return pd.DataFrame(matchups)

# =====================================================================
# PITCHER DEGRADATION DETECTION FOR POWER SPIKE CATCHING
# =====================================================================

def detect_pitcher_degradation(statcast_df, days_lookback=7):
    """Detect pitchers showing HR/BB rate increase over recent starts.
    
    Identifies pitchers allowing more HRs or getting worse (degradation).
    Returns dict mapping pitcher_id -> degradation_score [0.0, 1.0].
    Higher score = more degradation = boost opponent HR predictions.
    """
    if statcast_df.empty:
        return {}
    
    try:
        # Recent starts (last N days)
        cutoff = datetime.today() - timedelta(days=days_lookback)
        recent = statcast_df[pd.to_datetime(statcast_df.get('game_date', ''), errors='coerce') >= cutoff].copy()
        
        if recent.empty:
            return {}
        
        # Group by pitcher, count HRs allowed in recent starts
        pitcher_stats = recent.groupby('pitcher').agg(
            pa_count=('pitcher', 'size'),
            hr_count=('home_run', 'sum'),
        ).reset_index()
        
        pitcher_stats['recent_hr_rate'] = pitcher_stats['hr_count'] / pitcher_stats['pa_count'].clip(lower=1)
        
        # Compare to longer-term average (30+ days)
        all_recent = statcast_df[
            pd.to_datetime(statcast_df.get('game_date', ''), errors='coerce') >= 
            (datetime.today() - timedelta(days=30))
        ].copy()
        
        if not all_recent.empty:
            pitcher_history = all_recent.groupby('pitcher').agg(
                pa_count_30d=('pitcher', 'size'),
                hr_count_30d=('home_run', 'sum'),
            ).reset_index()
            pitcher_history['hist_hr_rate'] = pitcher_history['hr_count_30d'] / pitcher_history['pa_count_30d'].clip(lower=1)
            
            pitcher_stats = pitcher_stats.merge(pitcher_history, on='pitcher', how='left')
            pitcher_stats['hist_hr_rate'] = pitcher_stats['hist_hr_rate'].fillna(0.08)
            
            # Degradation: recent HR rate > historical + threshold
            pitcher_stats['hr_rate_increase'] = pitcher_stats['recent_hr_rate'] - pitcher_stats['hist_hr_rate']
            pitcher_stats['degradation_score'] = (pitcher_stats['hr_rate_increase'] / 0.05).clip(0, 1.0)  # Normalize to [0,1]
        else:
            pitcher_stats['degradation_score'] = 0.0
        
        return dict(zip(pitcher_stats['pitcher'], pitcher_stats['degradation_score']))
    except Exception:
        return {}

# =====================================================================
# FEEDBACK WEIGHTING ENGINE (Adaptive Learning System)
# =====================================================================
# SECTION 4: INFERENCE MODEL PROCESSING
# =====================================================================
def build_feedback_weight_series(train_df, feedback_df):
    """Create per-row training weights from recent feedback, using game_pk as a fallback when batter/pitcher IDs are missing."""
    if train_df is None:
        return pd.Series([1.0], dtype=float)

    weights = pd.Series(1.0, index=train_df.index, dtype=float)
    if feedback_df is None or feedback_df.empty:
        return weights

    fb = feedback_df.copy()
    fb['actual_hr'] = pd.to_numeric(fb.get('actual_hr', 0), errors='coerce').fillna(0).astype(int)
    fb['pred_hr_prob'] = pd.to_numeric(fb.get('pred_hr_prob', 0), errors='coerce').fillna(0.0)
    fb['batter'] = pd.to_numeric(fb.get('batter', np.nan), errors='coerce')
    fb['pitcher'] = pd.to_numeric(fb.get('pitcher', np.nan), errors='coerce')
    fb['game_pk'] = pd.to_numeric(fb.get('game_pk', np.nan), errors='coerce')
    fb['event_date'] = pd.to_datetime(fb.get('event_date', None), errors='coerce')

    has_game_pk = 'game_pk' in train_df.columns
    has_batter = 'batter' in train_df.columns
    has_pitcher = 'pitcher' in train_df.columns

    for _, row in fb.iterrows():
        try:
            prob = float(row['pred_hr_prob'])
            actual = int(row['actual_hr'])
        except Exception:
            continue

        if prob <= 0:
            continue

        multiplier = 1.0
        if actual == 1 and prob < 0.10:
            multiplier = 3.5
        elif actual == 0 and prob > 0.20:
            multiplier = 0.7
        elif actual == 1 and prob < 0.20:
            multiplier = 1.8
        elif actual == 0 and prob < 0.05:
            multiplier = 1.1

        if pd.notna(row.get('event_date')):
            recency = apply_time_decay_weight(row['event_date'], datetime.today(), half_life_days=7)
            multiplier *= max(0.5, min(1.5, float(recency)))

        match_mask = pd.Series(False, index=train_df.index)
        if has_batter and has_pitcher and pd.notna(row['batter']) and pd.notna(row['pitcher']):
            match_mask |= (train_df['batter'] == row['batter']) & (train_df['pitcher'] == row['pitcher'])
        elif has_batter and pd.notna(row['batter']):
            match_mask |= (train_df['batter'] == row['batter'])
        elif has_pitcher and pd.notna(row['pitcher']):
            match_mask |= (train_df['pitcher'] == row['pitcher'])

        if has_game_pk and pd.notna(row['game_pk']):
            match_mask |= (train_df['game_pk'] == row['game_pk'])

        if match_mask.any():
            weights.loc[match_mask] *= multiplier

    return weights.clip(lower=0.5, upper=15.0)


def load_feedback_weights(train_df, days_lookback=30):
    """Load historical evaluation CSVs and apply ASYMMETRIC learning:
    Heavily prioritize learning from FAILURES (misses, false confidence)
    over learning from successes. Failures are 3-5x more impactful.
    
    Returns a weight array aligned with train_df's index."""
    weights = pd.Series(1.0, index=train_df.index)

    cutoff = datetime.today() - timedelta(days=days_lookback)
    recent_evals = []

    # Load structured evaluation CSVs
    for f in sorted(Path('data').glob('evaluation_*.csv')):
        try:
            file_date = datetime.strptime(f.stem.replace('evaluation_', ''), '%Y-%m-%d')
            if file_date >= cutoff:
                ev = pd.read_csv(f)
                ev['event_date'] = file_date.strftime('%Y-%m-%d')
                recent_evals.append(ev)
        except Exception:
            continue

    # Load live game feedback CSVs (real-time HR observations from the watcher bot)
    for f in sorted(Path('data').glob('live_feedback_*.csv')):
        try:
            file_date = datetime.strptime(f.stem.replace('live_feedback_', ''), '%Y-%m-%d')
            if file_date >= cutoff:
                fb = pd.read_csv(f)
                fb['event_date'] = file_date.strftime('%Y-%m-%d')
                fb['actual_hr'] = 1
                fb['pred_hr_prob'] = pd.to_numeric(fb.get('model_prob', 0), errors='coerce').fillna(0)
                keep_cols = [c for c in ['event_date', 'game_pk', 'inning', 'batter', 'pitcher', 'actual_hr', 'pred_hr_prob'] if c in fb.columns]
                recent_evals.append(fb[keep_cols])
        except Exception:
            continue

    if not recent_evals:
        return weights.values

    eval_df = pd.concat(recent_evals, ignore_index=True)
    eval_df['event_date'] = pd.to_datetime(eval_df.get('event_date', datetime.today().strftime('%Y-%m-%d')), errors='coerce')
    eval_df['game_pk'] = pd.to_numeric(eval_df.get('game_pk', np.nan), errors='coerce')
    eval_df['inning'] = eval_df.get('inning', '').astype(str).str.lower().str.strip()
    eval_df['batter'] = pd.to_numeric(eval_df.get('batter', None), errors='coerce')
    eval_df['pitcher'] = pd.to_numeric(eval_df.get('pitcher', None), errors='coerce')
    eval_df = eval_df.dropna(subset=['batter', 'pitcher', 'actual_hr', 'pred_hr_prob'])

    # De-duplicate same observed outcomes across evaluation + live feedback sources.
    before_dedupe = len(eval_df)
    if 'game_pk' in eval_df.columns:
        dedupe_cols = ['event_date', 'game_pk', 'inning', 'batter', 'pitcher', 'actual_hr']
    else:
        dedupe_cols = ['event_date', 'batter', 'pitcher', 'actual_hr']
    dedupe_cols = [c for c in dedupe_cols if c in eval_df.columns]
    eval_df = eval_df.sort_values(by=['event_date']).drop_duplicates(subset=dedupe_cols, keep='first')
    removed_dupes = before_dedupe - len(eval_df)

    # Recency decay: recent misses count more than old misses.
    ref_date = datetime.today()
    eval_df['recency_weight'] = eval_df['event_date'].apply(
        lambda d: apply_time_decay_weight(d, ref_date, half_life_days=max(5, int(days_lookback / 2)))
    )

    # Adaptive thresholds from recent calibration distribution.
    hr_rows = eval_df[eval_df['actual_hr'] == 1]
    non_hr_rows = eval_df[eval_df['actual_hr'] == 0]
    if not hr_rows.empty:
        missed_cutoff = float(np.clip(hr_rows['pred_hr_prob'].quantile(0.40), 0.08, 0.18))
        confident_hr_cutoff = float(np.clip(hr_rows['pred_hr_prob'].quantile(0.65), 0.15, 0.30))
    else:
        missed_cutoff = 0.10
        confident_hr_cutoff = 0.15
    if not non_hr_rows.empty:
        false_pos_cutoff = float(np.clip(non_hr_rows['pred_hr_prob'].quantile(0.90), 0.20, 0.35))
        skeptical_cutoff = float(np.clip(non_hr_rows['pred_hr_prob'].quantile(0.35), 0.03, 0.08))
    else:
        false_pos_cutoff = 0.25
        skeptical_cutoff = 0.05

    # ASYMMETRIC LEARNING: Learn from FAILURES >> SUCCESSES
    # Philosophy: Model already knows to predict correctly. Focus on what it MISSED.
    # Failures are 3-5x more impactful than successes.
    
    # Missed HRs / false positives use adaptive thresholds from recent outcomes.
    eval_df['missed_hr'] = ((eval_df['actual_hr'] == 1) & (eval_df['pred_hr_prob'] < missed_cutoff)).astype(int)
    eval_df['false_pos'] = ((eval_df['actual_hr'] == 0) & (eval_df['pred_hr_prob'] > false_pos_cutoff)).astype(int)
    eval_df['correct_skeptical'] = ((eval_df['actual_hr'] == 0) & (eval_df['pred_hr_prob'] < skeptical_cutoff)).astype(int)
    eval_df['correct_confident'] = ((eval_df['actual_hr'] == 1) & (eval_df['pred_hr_prob'] > confident_hr_cutoff)).astype(int)

    eval_df['missed_hr_w'] = eval_df['missed_hr'] * eval_df['recency_weight']
    eval_df['false_pos_w'] = eval_df['false_pos'] * eval_df['recency_weight']
    eval_df['correct_skeptical_w'] = eval_df['correct_skeptical'] * eval_df['recency_weight']
    eval_df['correct_confident_w'] = eval_df['correct_confident'] * eval_df['recency_weight']

    batter_feedback = eval_df.groupby('batter').agg(
        bat_missed=('missed_hr_w', 'sum'),
        bat_false_pos=('false_pos_w', 'sum'),
        bat_correct_skeptical=('correct_skeptical_w', 'sum'),
        bat_correct_confident=('correct_confident_w', 'sum')
    ).reset_index()
    pitcher_feedback = eval_df.groupby('pitcher').agg(
        pit_missed=('missed_hr_w', 'sum'),
        pit_false_pos=('false_pos_w', 'sum')
    ).reset_index()

    merged = train_df[['batter', 'pitcher']].copy().reset_index()
    merged = merged.merge(batter_feedback, on='batter', how='left')
    merged = merged.merge(pitcher_feedback, on='pitcher', how='left')
    merged = merged.fillna(0)

    # ASYMMETRIC WEIGHTING: Failures >> Successes (3-5x impact ratio)
    # 
    # FAILURES (aggressive upweighting):
    #   missed_hr: 3.0x (was 1.5x) - CRITICAL: missed HRs demand heavy learning
    #   pit_missed: 1.5x (was 0.6x) - pitcher degradation pattern learning
    #   false_pos: -0.5x (was -0.2x) - strong penalty for false confidence
    #   pit_false_pos: -0.4x (was -0.15x) - pitcher false confidence penalty
    #
    # SUCCESSES (minimal reinforcement):
    #   correct_skeptical: 0.0x (was 0.8x) - don't reinforce, already working
    #   correct_confident: 0.3x (was 1.2x) - tiny reinforcement only
    #
    # Result: Failures are ~10x more impactful than successes
    
    boost = (1.0 
             + (merged['bat_missed'] * 3.0)              # FAILURES: Very aggressive on misses
             + (merged['pit_missed'] * 1.5)              # FAILURES: Pitcher pattern
             - (merged['bat_false_pos'] * 0.5)           # FAILURES: Strong false confidence penalty
             - (merged['pit_false_pos'] * 0.4)           # FAILURES: Pitcher false confidence
             + (merged['bat_correct_skeptical'] * 0.0)   # SUCCESS: Don't reinforce (already works)
             + (merged['bat_correct_confident'] * 0.3))  # SUCCESS: Minimal reinforcement only
    boost = boost.clip(lower=0.5, upper=15.0)

    result = pd.Series(boost.values, index=merged['index'])
    base_weights = result.reindex(train_df.index).fillna(1.0)
    feedback_weights = build_feedback_weight_series(train_df, eval_df)
    final_weights = (base_weights * feedback_weights).clip(lower=0.5, upper=15.0).values
    missed_upweighted = int((final_weights > 2.0).sum())
    false_pos_penalized = int((final_weights < 1.0).sum())
    print(
        "Adaptive thresholds: "
        f"miss<{missed_cutoff:.3f}, false_pos>{false_pos_cutoff:.3f}, "
        f"skeptical<{skeptical_cutoff:.3f}, confident>{confident_hr_cutoff:.3f}; "
        f"deduped_rows={removed_dupes}"
    )
    print(f"✅ Asymmetric learning: {missed_upweighted} heavily upweighted (FAILURES), {false_pos_penalized} penalized (FALSE CONFIDENCE)")
    return final_weights


def _closed_loop_coeff_path():
    data_dir = Path('data')
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / 'closed_loop_coefficients.json'


def load_closed_loop_coefficients():
    """Load adaptive closed-loop coefficients with safe defaults."""
    defaults = {
        'temp_weight': 1.00,
        'wind_weight': 1.00,
        'spin_weight': 1.00,
        'fatigue_weight': 1.00,
        'pitch_intent_weight': 0.22,
        'last_updated': None,
        'samples_seen': 0,
    }
    path = _closed_loop_coeff_path()
    if not path.exists():
        return defaults
    try:
        payload = _json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict):
            return defaults
        merged = defaults.copy()
        merged.update(payload)
        for k in ['temp_weight', 'wind_weight', 'spin_weight', 'fatigue_weight', 'pitch_intent_weight']:
            merged[k] = float(merged.get(k, defaults[k]))
        merged['samples_seen'] = int(merged.get('samples_seen', 0))
        return merged
    except Exception:
        return defaults


def _save_closed_loop_coefficients(coeffs):
    try:
        path = _closed_loop_coeff_path()
        out = dict(coeffs or {})
        out['last_updated'] = datetime.now().isoformat()
        path.write_text(_json.dumps(out, indent=2), encoding='utf-8')
    except Exception:
        pass


def _load_cached_statcast_window(end_date, days_back=21):
    """Load a rolling Statcast cache window ending before end_date."""
    frames = []
    for i in range(1, max(2, int(days_back)) + 1):
        d = (end_date - timedelta(days=i)).strftime('%Y-%m-%d')
        fp = Path('cache') / f'statcast_{d}.csv'
        if not fp.exists():
            continue
        try:
            frames.append(pd.read_csv(fp))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    try:
        return pd.concat(frames, ignore_index=True)
    except Exception:
        return pd.DataFrame()


def _pitch_mix_vector(df):
    if df is None or df.empty or 'pitch_type' not in df.columns:
        return {}
    vc = df['pitch_type'].dropna().astype(str).value_counts(normalize=True)
    return {str(k): float(v) for k, v in vc.items()}


def _tv_distance(vec_a, vec_b):
    keys = set(vec_a.keys()) | set(vec_b.keys())
    if not keys:
        return 0.0
    return 0.5 * sum(abs(float(vec_a.get(k, 0.0)) - float(vec_b.get(k, 0.0))) for k in keys)



def apply_park_adjustment(prob, batter_hand, home_team, away_team):
    """IMPROVEMENT #3: Park-Adjusted Metrics
    Adjusts probability based on home park HR-friendliness"""
    try:
        home_factor = PARK_HR_FACTORS.get(home_team, 100) / 100.0
        multiplier = home_factor
        adjusted = prob * multiplier
        return max(0.01, min(0.99, adjusted))
    except:
        return prob


def calculate_confidence_interval(prob, sample_size=100):
    """IMPROVEMENT #4: Model Calibration - Confidence Intervals
    Returns (lower, upper) bounds at 95% confidence"""
    try:
        if sample_size < 5:
            return (max(0.01, prob - 0.1), min(0.99, prob + 0.1))
        std_error = math.sqrt((prob * (1 - prob)) / sample_size)
        z_score = 1.96
        lower = max(0.01, prob - (z_score * std_error))
        upper = min(0.99, prob + (z_score * std_error))
        return (lower, upper)
    except:
        return (prob * 0.8, prob * 1.2)


def validate_model_dataflow(train_df, live_df, required_features=None):
    """Validate that training and live frames carry the expected data columns for inference."""
    issues = []
    required_features = required_features or []

    if train_df is None:
        issues.append("Training dataframe is None")
    elif hasattr(train_df, 'columns'):
        missing_train = [col for col in required_features if col not in train_df.columns]
        if missing_train:
            issues.append(f"Training missing feature columns: {', '.join(missing_train)}")
        if 'is_hr' not in train_df.columns:
            issues.append("Training dataframe missing target column 'is_hr'")

    if live_df is None:
        return issues

    if hasattr(live_df, 'columns'):
        if getattr(live_df, 'empty', False) and len(live_df.columns) == 0:
            return issues

        missing_live = [col for col in required_features if col not in live_df.columns]
        if missing_live:
            issues.append(f"Live missing feature columns: {', '.join(missing_live)}")

    if hasattr(train_df, 'columns') and hasattr(live_df, 'columns'):
        for key in ['batter', 'pitcher']:
            if key in train_df.columns and key in live_df.columns:
                train_ids = pd.to_numeric(train_df[key], errors='coerce')
                live_ids = pd.to_numeric(live_df[key], errors='coerce')
                if live_ids.isna().any() and not train_ids.isna().all():
                    issues.append(f"Live dataframe contains missing {key} IDs")

    return issues


def get_batter_consistency(df, batter_id):
    """IMPROVEMENT #8: Volatility-Weighted Features
    Returns consistency score (0-1): higher = more consistent"""
    try:
        batter_data = df[df['batter'] == batter_id].copy()
        if batter_data.empty or len(batter_data) < 10:
            return 0.5

        if 'is_hr' not in batter_data.columns:
            if 'events' in batter_data.columns:
                batter_data['is_hr'] = batter_data['events'].fillna('').str.lower().eq('home_run').astype(int)
            elif 'event' in batter_data.columns:
                batter_data['is_hr'] = batter_data['event'].fillna('').str.lower().eq('home_run').astype(int)
            else:
                return 0.5

        if 'game_pk' not in batter_data.columns:
            return 0.5

        batter_data['game_pk'] = pd.to_numeric(batter_data['game_pk'], errors='coerce')
        batter_data = batter_data.dropna(subset=['game_pk'])
        if batter_data.empty:
            return 0.5

        game_hr_rates = batter_data.groupby('game_pk')['is_hr'].mean()
        if len(game_hr_rates) < 2:
            return 0.5

        std_dev = game_hr_rates.std()
        mean_hr = game_hr_rates.mean()
        if mean_hr > 0:
            cv = std_dev / mean_hr
            consistency = 1.0 / (1.0 + cv)
            return max(0.0, min(1.0, consistency))
        return 0.5
    except Exception:
        return 0.5


def apply_time_decay_weight(date_val, reference_date, half_life_days=14):
    """IMPROVEMENT #9: Time Decay on Training Data
    More recent data gets higher weight"""
    try:
        days_old = (reference_date - pd.to_datetime(date_val)).days
        if days_old < 0:
            return 1.0
        weight = 2.0 ** (-days_old / half_life_days)
        return max(0.1, min(1.0, weight))
    except:
        return 1.0


def _coerce_numeric_column(frame, column_name, default=0.0):
    """Return a numeric Series for a column, creating a default series when needed."""
    if frame is None:
        return pd.Series([default], dtype=float)

    if column_name in frame.columns:
        return pd.to_numeric(frame[column_name], errors='coerce').fillna(default)

    return pd.Series([default] * len(frame), index=frame.index, dtype=float)


def _ensure_discord_radar_columns(rankings_df):
    """Ensure ranking output always has the columns required for Discord sorting."""
    if rankings_df is None:
        return pd.DataFrame()

    work = rankings_df.copy()
    if 'hr_probability' not in work.columns:
        if 'pred_hr_prob' in work.columns:
            work['hr_probability'] = pd.to_numeric(work['pred_hr_prob'], errors='coerce').fillna(0.0)
        else:
            work['hr_probability'] = 0.0

    if 'physics_delta' not in work.columns:
        work['physics_delta'] = 0.0

    if 'ev_pct' not in work.columns and 'ev_percent' in work.columns:
        work['ev_pct'] = pd.to_numeric(work['ev_percent'], errors='coerce').fillna(0.0)

    return work


def _finalize_discord_radar_frame(rankings_df):
    """Normalize radar-style rankings so sort operations always have the expected columns."""
    if rankings_df is None:
        return pd.DataFrame()

    work = _ensure_discord_radar_columns(rankings_df).copy()
    work['hr_probability'] = _coerce_numeric_column(work, 'hr_probability', default=0.0)
    work['physics_delta'] = _coerce_numeric_column(work, 'physics_delta', default=0.0)
    work['physics_delta_abs'] = work['physics_delta'].abs()
    return work


def _select_thresholded_candidates(rankings_df, min_prob, max_rows):
    """Return only rows that meet the requested probability threshold."""
    if rankings_df is None or rankings_df.empty:
        return pd.DataFrame()

    work = rankings_df.copy()
    if 'hr_probability' not in work.columns:
        return pd.DataFrame(columns=work.columns)

    work['hr_probability'] = _coerce_numeric_column(work, 'hr_probability', default=0.0)
    qualified = work[work['hr_probability'] >= min_prob].copy()
    if qualified.empty:
        return pd.DataFrame(columns=work.columns)
    return qualified.head(max_rows).reset_index(drop=True)


def _prepare_discord_rankings(live_df):
    """Prepare a ranking frame for Discord output while preserving reliability labels."""
    if live_df is None:
        return pd.DataFrame()

    work = live_df.copy()
    if 'model_reliability' in work.columns:
        work['model_reliability'] = work['model_reliability'].fillna('MEDIUM').astype(str).str.upper()
    else:
        work['model_reliability'] = 'MEDIUM'

    if work.empty:
        work['physics_delta'] = []
        work['hr_probability'] = []
        work['ev_pct'] = []
        return work

    if 'physics_delta' not in work.columns:
        if {'physics_hr_prob', 'base_model_prob'}.issubset(set(work.columns)):
            work['physics_delta'] = (
                pd.to_numeric(work['physics_hr_prob'], errors='coerce').fillna(0.0)
                - pd.to_numeric(work['base_model_prob'], errors='coerce').fillna(0.0)
            )
        elif 'pred_hr_prob' in work.columns and 'base_model_prob' in work.columns:
            work['physics_delta'] = (
                pd.to_numeric(work['pred_hr_prob'], errors='coerce').fillna(0.0)
                - pd.to_numeric(work['base_model_prob'], errors='coerce').fillna(0.0)
            )
        else:
            work['physics_delta'] = 0.0

    rename_map = {'pred_hr_prob': 'hr_probability', 'ev_percent': 'ev_pct'}
    if 'model_reliability' not in work.columns:
        work['model_reliability'] = 'MEDIUM'
    return work.rename(columns=rename_map)


def estimate_model_reliability(pred_prob, consistency_score, sample_size):
    """IMPROVEMENT #4: Confidence/Reliability Level
    Returns: 'HIGH', 'MEDIUM', or 'LOW' confidence"""
    try:
        if pred_prob < 0.05 or pred_prob > 0.95:
            return 'LOW'

        if sample_size < 8:
            return 'LOW'

        if consistency_score < 0.25:
            return 'LOW'

        if consistency_score > 0.7 and sample_size > 25 and 0.12 < pred_prob < 0.88:
            return 'HIGH'

        if sample_size >= 12 and consistency_score >= 0.45:
            return 'MEDIUM'

        return 'LOW'
    except:
        return 'MEDIUM'


def apply_daily_hr_volume_constraints(preds_df, game_count=1, avg_hr_per_game=2.3):
    """Calibrate daily HR probabilities to a league-average volumetric cap.

    The model returns a ranking of probabilities for the full slate. Since HRs are rare,
    the summed probabilities should be constrained by the expected number of HRs in the
    games scheduled today. This function scales down the probabilities by a single factor
    so the expected total HR count matches the league-average expectation.
    """
    try:
        if preds_df is None:
            return preds_df
        work = preds_df.copy()
        if work.empty:
            return work

        work['pred_hr_prob'] = pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0)
        if work['pred_hr_prob'].le(0).all():
            return work

        expected_total_hr = max(0.01, float(game_count or 1) * float(avg_hr_per_game or 2.3))
        total_prob = float(work['pred_hr_prob'].sum())
        if total_prob <= 0:
            return work

        scaling_factor = min(1.0, max(0.05, expected_total_hr / total_prob))
        work['pred_hr_prob'] = np.clip(work['pred_hr_prob'] * scaling_factor, 0.0, 0.99)
        work['pred_hr_prob'] = work['pred_hr_prob'].round(6)
        return work
    except Exception:
        return preds_df


def _poisson_tail_probability(k, lambd):
    """Return P(X >= k) for X ~ Poisson(lambda)."""
    try:
        if lambd <= 0:
            return 0.0
        total = 0.0
        for i in range(int(k)):
            total += (lambd ** i) * math.exp(-lambd) / math.factorial(i)
        return max(0.0, min(1.0, 1.0 - total))
    except Exception:
        return 0.0


def apply_poisson_hr_filter(preds_df, k=5, p_threshold=0.05, min_game_prob=0.20):
    """Downgrade game-level HR predictions when the implied Poisson tail is implausibly rare."""
    try:
        if preds_df is None:
            return preds_df
        work = preds_df.copy()
        if work.empty:
            return work

        work['pred_hr_prob'] = pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0)
        if work['pred_hr_prob'].le(0).all():
            return work

        if 'game_pk' in work.columns:
            frames = []
            for _, group in work.groupby('game_pk', dropna=False):
                group = group.copy()
                game_total_prob = float(group['pred_hr_prob'].sum())
                if game_total_prob >= min_game_prob:
                    lambda_value = max(0.5, min(5.0, game_total_prob * 5.0))
                    tail_prob = _poisson_tail_probability(k, lambda_value)
                    if tail_prob < p_threshold:
                        group['pred_hr_prob'] = np.clip(group['pred_hr_prob'] * 0.75, 0.0, 0.99)
                frames.append(group)
            if frames:
                work = pd.concat(frames, ignore_index=True)
        else:
            game_total_prob = float(work['pred_hr_prob'].sum())
            if game_total_prob >= min_game_prob:
                lambda_value = max(0.5, game_total_prob * 10.0)
                tail_prob = _poisson_tail_probability(k, lambda_value)
                if tail_prob < p_threshold:
                    work['pred_hr_prob'] = np.clip(work['pred_hr_prob'] * 0.75, 0.0, 0.99)

        work['pred_hr_prob'] = work['pred_hr_prob'].round(6)
        return work
    except Exception:
        return preds_df


def run_post_mortem_backpropagation(days_lookback=7):
    """Closed-loop post-mortem layer that rewrites adaptive coefficients.

    The loop inspects recent prediction misses, re-queries game Statcast slices,
    diagnoses likely failure modes, and nudges weight coefficients for next runs.
    """
    coeffs = load_closed_loop_coefficients()
    cutoff = datetime.today() - timedelta(days=max(1, int(days_lookback)))

    eval_files = []
    for fp in sorted(Path('data').glob('evaluation_*.csv')):
        try:
            d = datetime.strptime(fp.stem.replace('evaluation_', ''), '%Y-%m-%d')
            if d >= cutoff:
                eval_files.append((d, fp))
        except Exception:
            continue

    if not eval_files:
        return {
            'coefficients': coeffs,
            'samples': 0,
            'false_pos': 0,
            'diagnostics': {},
        }

    def _num_series(df, col, default):
        if col in df.columns:
            s = df[col]
        else:
            s = pd.Series([default] * len(df), index=df.index)
        return pd.to_numeric(s, errors='coerce').fillna(default)

    false_pos_rows = []
    missed_rows = []
    for d, fp in eval_files:
        try:
            ev = pd.read_csv(fp)
            if ev.empty:
                continue
            ev['pred_hr_prob'] = _num_series(ev, 'pred_hr_prob', 0.0)
            ev['actual_hr'] = _num_series(ev, 'actual_hr', 0.0).astype(int)
            false_pos_rows.append(ev[(ev['pred_hr_prob'] >= 0.18) & (ev['actual_hr'] == 0)].copy())
            missed_rows.append(ev[(ev['pred_hr_prob'] <= 0.10) & (ev['actual_hr'] == 1)].copy())
        except Exception:
            continue

    false_pos = pd.concat(false_pos_rows, ignore_index=True) if false_pos_rows else pd.DataFrame()
    missed = pd.concat(missed_rows, ignore_index=True) if missed_rows else pd.DataFrame()

    n_false = len(false_pos)
    n_missed = len(missed)
    total_samples = n_false + n_missed
    if total_samples == 0:
        return {
            'coefficients': coeffs,
            'samples': 0,
            'false_pos': 0,
            'diagnostics': {},
        }

    temp_false_rate = 0.0
    wind_false_rate = 0.0
    spin_miss_rate = 0.0
    fatigue_false_rate = 0.0

    if not false_pos.empty:
        t = _num_series(false_pos, 'temp', 72.0)
        w = _num_series(false_pos, 'wind_out_component', 0.0)
        f_idx = _num_series(false_pos, 'circadian_disruption_index', 0.0)
        v_fat = _num_series(false_pos, 'visual_fatigue_modifier', 1.0)
        temp_false_rate = float((t >= 84).mean())
        wind_false_rate = float((w >= 6).mean())
        fatigue_false_rate = float(((f_idx >= 10) | (v_fat <= 0.96)).mean())

    if not missed.empty:
        spin_flag = _num_series(missed, 'spin_decay_flag', 0.0)
        spin_rpm = _num_series(missed, 'spin_decay_rpm', 0.0)
        spin_miss_rate = float(((spin_flag > 0) | (spin_rpm >= 140)).mean())

    # Deep diagnosis via exact-game Statcast slices.
    bat_speed_drop_hits = 0
    pitch_mix_shift_hits = 0
    wind_shift_proxy_hits = 0
    diagnosis_sample = false_pos.head(60)
    date_cache = {}

    for _, r in diagnosis_sample.iterrows():
        try:
            gpk = pd.to_numeric(r.get('game_pk', np.nan), errors='coerce')
            batter = pd.to_numeric(r.get('batter', np.nan), errors='coerce')
            pitcher = pd.to_numeric(r.get('pitcher', np.nan), errors='coerce')
            pred_date = str(r.get('prediction_timestamp', '') or '')[:10]
            if len(pred_date) != 10:
                continue
            if pred_date not in date_cache:
                date_cache[pred_date] = load_or_fetch_statcast(pred_date)
            day_sc = date_cache.get(pred_date)
            if day_sc is None or day_sc.empty:
                continue

            game_slice = day_sc[
                (pd.to_numeric(day_sc.get('game_pk', np.nan), errors='coerce') == float(gpk)) &
                (pd.to_numeric(day_sc.get('batter', np.nan), errors='coerce') == float(batter)) &
                (pd.to_numeric(day_sc.get('pitcher', np.nan), errors='coerce') == float(pitcher))
            ].copy()
            if game_slice.empty:
                continue

            # Bat-speed drop diagnosis.
            game_ev = pd.to_numeric(game_slice.get('launch_speed', np.nan), errors='coerce').dropna()
            if not game_ev.empty:
                hist = _load_cached_statcast_window(datetime.strptime(pred_date, '%Y-%m-%d'), days_back=21)
                hist_ev = pd.Series(dtype=float)
                if not hist.empty:
                    hist_ev = pd.to_numeric(
                        hist[pd.to_numeric(hist.get('batter', np.nan), errors='coerce') == float(batter)].get('launch_speed', np.nan),
                        errors='coerce'
                    ).dropna()
                if len(hist_ev) >= 20 and game_ev.mean() <= (hist_ev.mean() - 1.5):
                    bat_speed_drop_hits += 1

            # Pitch-mix shift diagnosis.
            hist2 = _load_cached_statcast_window(datetime.strptime(pred_date, '%Y-%m-%d'), days_back=21)
            if not hist2.empty:
                p_hist = hist2[pd.to_numeric(hist2.get('pitcher', np.nan), errors='coerce') == float(pitcher)]
                game_mix = _pitch_mix_vector(game_slice)
                hist_mix = _pitch_mix_vector(p_hist)
                if _tv_distance(game_mix, hist_mix) >= 0.22:
                    pitch_mix_shift_hits += 1

            # Wind-vector shift proxy: favorable pregame wind but weak in-game contact.
            pre_wind = float(pd.to_numeric(r.get('wind_out_component', 0.0), errors='coerce'))
            hard_hit_rate = float((pd.to_numeric(game_slice.get('launch_speed', 0), errors='coerce') >= 95).mean())
            if pre_wind >= 6.0 and hard_hit_rate <= 0.10:
                wind_shift_proxy_hits += 1
        except Exception:
            continue

    sample_n = max(1, len(diagnosis_sample))
    bat_speed_drop_rate = bat_speed_drop_hits / sample_n
    pitch_mix_shift_rate = pitch_mix_shift_hits / sample_n
    wind_shift_proxy_rate = wind_shift_proxy_hits / sample_n

    # Reward-function backprop: adjust coefficients for next day.
    coeffs['temp_weight'] = float(np.clip(coeffs['temp_weight'] * (1.0 - 0.015 * temp_false_rate), 0.88, 1.12))
    coeffs['wind_weight'] = float(np.clip(coeffs['wind_weight'] * (1.0 - 0.015 * max(wind_false_rate, wind_shift_proxy_rate)), 0.88, 1.15))
    coeffs['spin_weight'] = float(np.clip(coeffs['spin_weight'] * (1.0 + 0.015 * max(spin_miss_rate, pitch_mix_shift_rate)), 0.88, 1.20))
    coeffs['fatigue_weight'] = float(np.clip(coeffs['fatigue_weight'] * (1.0 + 0.015 * max(fatigue_false_rate, bat_speed_drop_rate)), 0.88, 1.20))
    coeffs['samples_seen'] = int(coeffs.get('samples_seen', 0)) + int(total_samples)
    _save_closed_loop_coefficients(coeffs)

    report = {
        'as_of': datetime.now().isoformat(),
        'samples': int(total_samples),
        'false_pos': int(n_false),
        'missed_hr': int(n_missed),
        'diagnostics': {
            'temp_false_rate': round(temp_false_rate, 4),
            'wind_false_rate': round(wind_false_rate, 4),
            'spin_miss_rate': round(spin_miss_rate, 4),
            'fatigue_false_rate': round(fatigue_false_rate, 4),
            'bat_speed_drop_rate': round(bat_speed_drop_rate, 4),
            'pitch_mix_shift_rate': round(pitch_mix_shift_rate, 4),
            'wind_shift_proxy_rate': round(wind_shift_proxy_rate, 4),
        },
        'coefficients': {
            'temp_weight': round(coeffs['temp_weight'], 6),
            'wind_weight': round(coeffs['wind_weight'], 6),
            'spin_weight': round(coeffs['spin_weight'], 6),
            'fatigue_weight': round(coeffs['fatigue_weight'], 6),
            'pitch_intent_weight': round(coeffs['pitch_intent_weight'], 6),
        },
    }
    try:
        out_path = Path('data') / f"closed_loop_report_{datetime.today().strftime('%Y-%m-%d')}.json"
        out_path.write_text(_json.dumps(report, indent=2), encoding='utf-8')
    except Exception:
        pass
    return report


def compute_pitcher_intent_fear_factor(statcast_df, elite_batters):
    """Track pitcher intent via rolling out-of-zone % vs elite power hitters."""
    if statcast_df is None or statcast_df.empty or not elite_batters:
        return {}
    if 'zone' not in statcast_df.columns or 'pitcher' not in statcast_df.columns:
        return {}
    try:
        work = statcast_df.copy()
        work['batter'] = pd.to_numeric(work.get('batter', np.nan), errors='coerce')
        work['pitcher'] = pd.to_numeric(work.get('pitcher', np.nan), errors='coerce')
        work['zone'] = pd.to_numeric(work.get('zone', np.nan), errors='coerce')
        work['game_pk'] = pd.to_numeric(work.get('game_pk', np.nan), errors='coerce')
        work = work.dropna(subset=['batter', 'pitcher', 'zone', 'game_pk'])
        work = work[work['batter'].isin(set(float(x) for x in elite_batters))].copy()
        if work.empty:
            return {}

        work['is_out_zone'] = ((work['zone'] < 1) | (work['zone'] > 9)).astype(int)
        pg = work.groupby(['pitcher', 'game_pk'], as_index=False).agg(out_zone_rate=('is_out_zone', 'mean'))
        if pg.empty:
            return {}

        baseline = float(pg['out_zone_rate'].mean())
        fear = {}
        for pid, g in pg.groupby('pitcher'):
            last3 = g.sort_values('game_pk').tail(3)
            rate = float(last3['out_zone_rate'].mean())
            # Above-baseline out-of-zone behavior => greater intentional avoidance.
            fear[str(int(pid))] = float(np.clip((rate - baseline) / 0.20, 0.0, 1.0))
        return fear
    except Exception:
        return {}


def run_adversarial_debate_layer(live_df, rounds=5000):
    """Dual-agent adversarial layer (Optimist vs Pessimist/Sportsbook)."""
    if live_df is None or live_df.empty:
        return live_df

    rounds = max(500, int(rounds))
    out = live_df.copy()

    bat_barrel = pd.to_numeric(out.get('bat_barrel_rate', 0.0), errors='coerce').fillna(0.0)
    bat_hh = pd.to_numeric(out.get('bat_hard_hit_rate', 0.0), errors='coerce').fillna(0.0)
    park = pd.to_numeric(out.get('park_factor', 100.0), errors='coerce').fillna(100.0)
    temp = pd.to_numeric(out.get('temp', 72.0), errors='coerce').fillna(72.0)
    wind_out = pd.to_numeric(out.get('wind_out_component', 0.0), errors='coerce').fillna(0.0)
    pitch_hr9 = pd.to_numeric(out.get('pitch_hr_per_9', 1.1), errors='coerce').fillna(1.1)
    pitch_hr_allowed = pd.to_numeric(out.get('pitch_hr_allowed_rate', 0.04), errors='coerce').fillna(0.04)
    umpire_impact = pd.to_numeric(out.get('umpire_strike_zone_impact', 1.0), errors='coerce').fillna(1.0)
    bullpen_home = pd.to_numeric(out.get('bullpen_quality_score_home', 50.0), errors='coerce').fillna(50.0)
    bullpen_away = pd.to_numeric(out.get('bullpen_quality_score_away', 50.0), errors='coerce').fillna(50.0)
    fear = pd.to_numeric(out.get('pitcher_fear_factor', 0.0), errors='coerce').fillna(0.0)

    optimist = (
        ((bat_barrel - 0.08) / 0.08) * 0.90
        + ((bat_hh - 0.35) / 0.20) * 0.75
        + ((park - 100.0) / 20.0) * 0.55
        + ((temp - 72.0) / 18.0) * 0.25
        + (wind_out / 10.0) * 0.30
        + ((pitch_hr9 - 1.1) / 0.6) * 0.55
    )

    pessim = (
        ((0.05 - pitch_hr_allowed) / 0.03) * 0.65
        + ((umpire_impact - 1.0) / 0.08) * 0.35
        + (((bullpen_home + bullpen_away) / 2.0 - 50.0) / 20.0) * 0.30
        + (fear * 1.10)
    )

    debate_logit = optimist - pessim
    p_opt = 1.0 / (1.0 + np.exp(-debate_logit))
    opt_wins = np.round(p_opt * rounds).astype(int)
    pes_wins = rounds - opt_wins
    margin = (opt_wins - pes_wins) / float(rounds)
    mult = np.clip(1.0 + (margin * 0.22), 0.82, 1.18)

    out['optimist_score'] = optimist
    out['pessimist_score'] = pessim
    out['debate_rounds'] = rounds
    out['optimist_wins'] = opt_wins
    out['pessimist_wins'] = pes_wins
    out['adversarial_margin'] = margin
    out['adversarial_multiplier'] = mult
    return out


# =====================================================================
# PROFESSIONAL-GRADE MONTE CARLO SIMULATION ENGINE
# =====================================================================

def project_batting_order_pa(batting_order_slot, avg_game_length_innings=9):
    """
    Project how many plate appearances a batter gets based on lineup position.
    
    Algorithm:
    - Average game = 9 innings per team
    - Roughly 3 PAs per batter per 9 innings (0.33 PA/inning)
    - Adjust by batting order: top of order gets more PAs
    
    Returns: expected PA count (float)
    """
    # Base PA calculation: 9 innings * 3 batters/inning = ~27 PAs per team across 9 hitters
    # Per batter: 27/9 = 3 PAs baseline
    base_pa = avg_game_length_innings * 0.33
    
    # Position-based modifiers (empirical from 2023-2025 MLB)
    position_factors = {
        1: 1.15,  # Lead-off sees most at-bats
        2: 1.12,
        3: 1.10,
        4: 1.08,  # Cleanup
        5: 1.05,
        6: 1.02,
        7: 0.98,
        8: 0.95,
        9: 0.88   # Pitcher's spot (or 9-hole hitter gets fewer)
    }
    
    position = int(batting_order_slot) if pd.notna(batting_order_slot) else 5
    position = max(1, min(position, 9))
    
    multiplier = position_factors.get(position, 1.0)
    return base_pa * multiplier


def monte_carlo_hr_simulation(single_pa_prob, num_simulations=10000, avg_pas=3.0):
    """
    Run Monte Carlo simulation to calculate the probability of ≥1 home run
    given a player's single-PA home run probability.
    
    Binomial approach: 
    - Each PA has probability p of being a home run
    - After N PAs, what's P(HR_count ≥ 1)?
    - Equivalently: 1 - P(0 home runs) = 1 - (1-p)^N
    
    For precision with rare events, we simulate:
    - 10,000 simulated games
    - Each game: draw N (from distribution around avg_pas)
    - Each PA: Bernoulli(p) for HR
    
    Returns: float, simulated probability of ≥1 HR
    """
    if single_pa_prob <= 0 or single_pa_prob >= 1:
        return single_pa_prob  # Edge case: if p=0 or p=1, HR prob = p
    
    # Use exact binomial formula for speed (more accurate for rare events)
    # P(≥1 HR | p, N) = 1 - (1-p)^N
    # Then average over distribution of N
    
    # Distribution of PAs: normal around avg_pas with std 0.5
    pas_distribution = np.random.normal(avg_pas, 0.5, num_simulations)
    pas_distribution = np.maximum(pas_distribution, 1).astype(int)  # Minimum 1 PA
    
    # Binomial probability: P(at least 1 HR)
    # = 1 - P(no HRs) = 1 - (1-p)^N
    prob_no_hr = (1 - single_pa_prob) ** pas_distribution
    prob_at_least_one = 1 - prob_no_hr
    
    # Average across simulated games
    simulated_prob = prob_at_least_one.mean()
    
    return float(simulated_prob)


def calculate_ev_premium(model_prob, market_prob, market_odds_american=None):
    """
    Calculate Expected Value (EV) for a moneyline-style player prop bet.
    
    Formula:
    EV = (P_model * Decimal_Odds - 1) - (1 - P_model) * 1
       = P_model * Decimal_Odds - 1
    
    If EV > 0, the bet is +EV (profitable in long run).
    
    Also converts American odds to decimal:
    - Negative odds: Decimal = 1 + 100/|American|
    - Positive odds: Decimal = 1 + American/100
    
    Returns: (ev_value, decimal_odds, ev_percent)
    """
    if market_prob is None or pd.isna(market_prob) or market_prob <= 0 or market_prob >= 1:
        return (0.0, 0.0, 0.0)
    
    # If American odds provided, convert to decimal
    if market_odds_american is not None and not pd.isna(market_odds_american):
        if market_odds_american < 0:
            decimal_odds = 1 + 100 / abs(market_odds_american)
        else:
            decimal_odds = 1 + market_odds_american / 100
    else:
        # Reverse from implied probability: Decimal = 1 / market_prob
        decimal_odds = 1 / market_prob
    
    # EV in dollars (per $1 bet)
    ev_value = model_prob * decimal_odds - 1
    
    # EV as percentage
    ev_percent = ev_value * 100
    
    return (ev_value, decimal_odds, ev_percent)


def generate_daily_predictions():
    # =====================================================================
    # PHASE 0: LEARN FROM YESTERDAY'S HOME RUNS (Automatic Pattern Analysis)
    # =====================================================================
    print("\n" + "="*70)
    print("PHASE 0: ANALYZING YESTERDAY'S HOME RUNS FOR PATTERN LEARNING")
    print("="*70)
    learning_result = {}
    try:
        from analyze_hr_patterns import analyze_yesterdays_hrs_and_learn
        learning_result = analyze_yesterdays_hrs_and_learn()
        if learning_result and learning_result.get('insights'):
            insights = learning_result['insights']
            print(f"\n✅ Learning Complete: {insights['total_hrs_analyzed']} HRs analyzed")
            print(f"   • Model accuracy: {insights['accurate_predictions']}/{insights['total_hrs_analyzed']} predicted")
            print(f"   • Missed predictions: {insights['missed_predictions']} (will upweight in training)")
    except ImportError:
        print("⚠️  analyze_hr_patterns module not found, skipping pattern learning")
        if ERROR_TRACKER:
            log_warning("pattern_learning", "analyze_hr_patterns import", "Module not found - non-critical")
    except Exception as e:
        print(f"⚠️  HR pattern learning failed: {e}")
        if ERROR_TRACKER:
            log_error("pattern_learning", "analyze_yesterdays_hrs_and_learn", e, "WARNING")
    
    # =====================================================================
    # PHASE 0.5: BASEBALL SAVANT LINEUP VERIFICATION
    # =====================================================================
    print("\n" + "="*70)
    print("PHASE 0.5: VERIFYING LINEUPS FROM BASEBALL SAVANT")
    print("="*70)
    try:
        if check_lineups_morning is not None:
            morning_lineups = check_lineups_morning()
            if morning_lineups:
                save_lineup_report(morning_lineups, "_morning_check")
                print(f"\n✅ Lineup verification complete: {len(morning_lineups)} games confirmed")
                print("   • All lineups from Baseball Savant verified")
                print("   • Report saved for prediction matching")
            else:
                print("⚠️  No games found for today")
        else:
            print("⚠️  baseball_savant module not available, skipping lineup check")
    except Exception as e:
        print(f"⚠️  Lineup verification failed: {e}")
        if ERROR_TRACKER:
            log_error("lineup_check", "check_lineups_morning", e, "WARNING")
    
    # =====================================================================
    # PHASE 1: LOAD TRAINING DATA
    # =====================================================================
    print("\n" + "="*70)
    print("PHASE 1: LOADING TRAINING DATA")
    print("="*70)
    try:
        b_stats, p_stats, raw_pa, pitch_statcast_df = get_advanced_hr_metrics(days_back=60)
        print(f"✅ Training data loaded: {len(raw_pa)} plate appearances")
    except Exception as e:
        if ERROR_TRACKER:
            log_error("data_loading", "get_advanced_hr_metrics", e, "CRITICAL")
        raise
    
    # Store raw Statcast data for professional bettor feature calculations
    statcast_df = pitch_statcast_df.copy()

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
            if ERROR_TRACKER:
                log_error("evaluation", f"evaluate_saved_predictions({yesterday_str})", _e, "WARNING")

    sample_weights = load_feedback_weights(train_df)
    missed_count = int((sample_weights > 1.2).sum())
    print(f"Feedback weights loaded — {missed_count} training rows upweighted from past misses.")

    # Closed-loop reward backprop layer: rewrite adaptive coefficients from recent outcomes.
    closed_loop_report = run_post_mortem_backpropagation(days_lookback=7)
    adaptive_coeffs = load_closed_loop_coefficients()
    if closed_loop_report.get('samples', 0) > 0:
        print(
            "Closed-loop backprop updated coefficients: "
            f"temp={adaptive_coeffs.get('temp_weight', 1.0):.3f}, "
            f"wind={adaptive_coeffs.get('wind_weight', 1.0):.3f}, "
            f"spin={adaptive_coeffs.get('spin_weight', 1.0):.3f}, "
            f"fatigue={adaptive_coeffs.get('fatigue_weight', 1.0):.3f} "
            f"(samples={closed_loop_report.get('samples', 0)})"
        )
    
    # Feature list for MODEL TRAINING (only features that exist in train_df)
    features_train = [
        # Batter Features (core power + batted ball profile)
        'bat_pa_count', 'bat_hr_rate', 'bat_barrel_rate', 'bat_hard_hit_rate', 'bat_sweet_spot_rate',
        'bat_hr_fb_rate', 'bat_pull_rate', 'bat_ev90', 'bat_iso_proxy', 'bat_days_since_last_game',
        'bat_avg_exit_velocity', 'bat_max_exit_velocity', 'bat_avg_launch_angle',
        'bat_15pa_barrel_rate', 'bat_30pa_barrel_rate',
        'bat_15pa_hard_hit_rate', 'bat_30pa_hard_hit_rate',
        'bat_15pa_sweet_spot_rate', 'bat_30pa_sweet_spot_rate',
        'bat_15pa_fb_rate', 'bat_30pa_fb_rate',
        'bat_wrc_plus',  # NEW: Overall offensive value normalized to league average
        'has_platoon_advantage',
        
        # Pitcher vulnerability features (HR/9, FB%, hard-hit, barrels)
        'pitch_pa_count', 'pitch_hr_allowed_rate', 'pitch_barrel_allowed_rate',
        'pitch_hard_hit_allowed_rate', 'pitch_sweet_spot_allowed_rate', 'pitch_hr_fb_allowed_rate', 'pitch_days_since_last_start',
        'pitch_fb_allowed_rate', 'pitch_hr_per_9',
        'pitch_avg_velocity',
        'pitch_15pa_hr_rate', 'pitch_30pa_hr_rate',
        'pitch_15pa_barrel_allowed_rate', 'pitch_30pa_barrel_allowed_rate',
        'pitch_15pa_hard_hit_allowed_rate', 'pitch_30pa_hard_hit_allowed_rate',
        'pitch_15pa_fb_allowed_rate', 'pitch_30pa_fb_allowed_rate',
        
        # Stadium & Weather Features
        'park_factor', 'temp', 'wind_speed', 'wind_out_component',
        'humidity', 'precipitation', 'pressure',  # NEW: Enhanced weather tracking
    ]
    
    # Ensure all required weather columns exist in training data
    for weather_col, default_val in [('humidity', 50.0), ('precipitation', 0.0), ('pressure', 1013.25)]:
        if weather_col not in train_df.columns:
            train_df[weather_col] = default_val
        else:
            train_df[weather_col] = train_df[weather_col].fillna(default_val)

    # Validate training data first; live dataframe is assembled later in the flow.
    dataflow_issues = validate_model_dataflow(
        train_df,
        live_df=pd.DataFrame(),
        required_features=features_train,
    )
    if dataflow_issues:
        print("⚠️ Dataflow validation warnings:")
        for issue in dataflow_issues:
            print(f"  - {issue}")
    
    # Feature list for LIVE PREDICTIONS (includes calculated multipliers)
    features_live = features_train + [
        # Platoon & Matchup Features (Advanced Handedness) - CALCULATED FOR LIVE ONLY
        'has_platoon_advantage', 'platoon_advantage_multiplier',
        'breaking_pitch_vulnerability', 'left_on_right_fade_score',
        'reverse_split_anomaly_score',
        
        # Ballpark Dimensions Features (CALCULATED FOR LIVE ONLY)
        'ballpark_park_factor', 'porch_advantage_bonus',
        'death_valley_penalty', 'would_be_hr_differential',
        
        # Professional Bettor Features (CALCULATED FOR LIVE ONLY)
        'bullpen_quality_score_home', 'bullpen_quality_score_away',
        'umpire_strike_zone_impact', 'density_altitude_factor',
        'weather_extremes_multiplier', 'sportsbook_value_score'
    ]
    
    X_train = train_df[features_train]
    y_train = train_df['is_hr']

    positive_count = int(pd.to_numeric(y_train, errors='coerce').fillna(0).sum())
    negative_count = int(len(y_train) - positive_count)
    scale_pos_weight = round(max(1.0, min(50.0, negative_count / max(positive_count, 1))), 2)
    print(
        "Class imbalance control: "
        f"positive={positive_count}, negative={negative_count}, scale_pos_weight={scale_pos_weight:.2f}"
    )

    cv_splitter = TimeSeriesSplit(n_splits=3) if TimeSeriesSplit is not None else 3
    base_models = []
    model_names = []

    if xgb is not None:
        base_models.append(xgb.XGBClassifier(
            n_estimators=150, max_depth=5, learning_rate=0.04,
            eval_metric='logloss', scale_pos_weight=scale_pos_weight
        ))
        model_names.append('XGBoost')

    if lgb is not None:
        base_models.append(lgb.LGBMClassifier(
            n_estimators=150, max_depth=5, learning_rate=0.04,
            scale_pos_weight=scale_pos_weight, verbose=-1
        ))
        model_names.append('LightGBM')

    # IMPROVEMENT #7: Ensemble Diversity - Add Random Forest (different approach: bagging vs boosting)
    if RandomForestClassifier is not None:
        base_models.append(RandomForestClassifier(
            n_estimators=200, max_depth=8, min_samples_split=10, random_state=42
        ))
        model_names.append('RandomForest')

    # IMPROVEMENT #7: Add Logistic Regression (captures linear relationships)
    # Wrapped in pipeline with SimpleImputer to handle NaN values
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.impute import SimpleImputer
        
        lr_pipeline = Pipeline([
            ('imputer', SimpleImputer(strategy='median')),
            ('lr', LogisticRegression(max_iter=1000, class_weight='balanced', random_state=42))
        ])
        base_models.append(lr_pipeline)
        model_names.append('LogisticRegression+Imputer')
    except ImportError:
        pass

    # IMPROVEMENT #7: Add Neural Network (captures non-linear interactions)
    # Also wrapped with imputer for NaN handling
    try:
        from sklearn.neural_network import MLPClassifier
        from sklearn.pipeline import Pipeline
        from sklearn.impute import SimpleImputer
        
        nn_pipeline = Pipeline([
            ('imputer', SimpleImputer(strategy='median')),
            ('nn', MLPClassifier(hidden_layer_sizes=(128, 64, 32), max_iter=500, random_state=42, early_stopping=True))
        ])
        base_models.append(nn_pipeline)
        model_names.append('NeuralNetwork+Imputer')
    except ImportError:
        pass

    if not base_models:
        raise ImportError("Missing required ML package: install xgboost, lightgbm, or scikit-learn.")

    trained_models = []
    for m in base_models:
        if CalibratedClassifierCV is not None:
            # FIXED: Use 'sigmoid' (Platt scaling) instead of 'isotonic' for rare event calibration
            # Isotonic is too aggressive for HR prediction (rare event with low base rate)
            # Sigmoid preserves probability mass better, avoiding probability collapse
            m = CalibratedClassifierCV(m, cv=cv_splitter, method='sigmoid')
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
            _fi_series = pd.Series(_fi, index=features_train).sort_values(ascending=False).head(10)
            print("\nTop 10 Feature Importances:")
            for _fname, _fval in _fi_series.items():
                print(f"  {_fname:<40} {_fval:.4f}")
    except Exception:
        pass
    
    live_matchups = get_today_matchups()
    if live_matchups.empty:
        print("No games or lineups available for today.")
        return pd.DataFrame()

    for _id_col in ['batter', 'pitcher']:
        if _id_col in live_matchups.columns:
            live_matchups[_id_col] = pd.to_numeric(live_matchups[_id_col], errors='coerce').astype('Int64')
        if _id_col in b_stats.columns:
            b_stats[_id_col] = pd.to_numeric(b_stats[_id_col], errors='coerce').astype('Int64') if _id_col == 'batter' else b_stats.get(_id_col, b_stats[_id_col])
        if _id_col in p_stats.columns:
            p_stats[_id_col] = pd.to_numeric(p_stats[_id_col], errors='coerce').astype('Int64') if _id_col == 'pitcher' else p_stats.get(_id_col, p_stats[_id_col])

    # Join live matchups with player vectors where available (use inner to ensure features exist)
    live = live_matchups.merge(b_stats, on='batter', how='left')
    live = live.merge(p_stats, on='pitcher', how='left')

    live_dataflow_issues = validate_model_dataflow(
        train_df,
        live,
        required_features=features_live,
    )
    if live_dataflow_issues:
        print("ℹ️ Live dataflow validation:")
        for issue in live_dataflow_issues:
            print(f"  - {issue}")

    # Fill missing numeric features with reasonable baselines
    for col in ['bat_pa_count', 'bat_hr_rate', 'bat_barrel_rate', 'bat_hard_hit_rate', 'bat_sweet_spot_rate']:
        if col in live.columns:
            live[col] = live[col].fillna(0)
    if 'bat_wrc_plus' in live.columns:
        live['bat_wrc_plus'] = live['bat_wrc_plus'].fillna(100)  # League average
    for col in ['pitch_pa_count', 'pitch_hr_allowed_rate', 'pitch_barrel_allowed_rate', 'pitch_hard_hit_allowed_rate', 'pitch_sweet_spot_allowed_rate']:
        if col in live.columns:
            live[col] = live[col].fillna(live[col].mean() if not live[col].isna().all() else 0)

    live['park_factor'] = live['park_factor'].fillna(100)
    live['temp'] = live['temp'].fillna(71.0)
    live['wind_speed'] = live['wind_speed'].fillna(5.0)
    live['humidity'] = live['humidity'].fillna(50.0)  # League average humidity
    live['precipitation'] = live['precipitation'].fillna(0.0)  # No precip by default
    live['pressure'] = live['pressure'].fillna(1013.25)  # Sea-level standard pressure
    live['bat_hr_fb_rate'] = live['bat_hr_fb_rate'].fillna(0.12)
    live['pitch_hr_fb_allowed_rate'] = live['pitch_hr_fb_allowed_rate'].fillna(0.12)
    live['bat_ev90'] = live['bat_ev90'].fillna(88.0)
    live['bat_avg_exit_velocity'] = live['bat_avg_exit_velocity'].fillna(88.0)
    live['bat_max_exit_velocity'] = live['bat_max_exit_velocity'].fillna(102.0)
    live['bat_avg_launch_angle'] = live['bat_avg_launch_angle'].fillna(12.0)
    live['bat_iso_proxy'] = live['bat_iso_proxy'].fillna(0.08)
    live['bat_days_since_last_game'] = live['bat_days_since_last_game'].fillna(1)
    live['bat_wrc_plus'] = live['bat_wrc_plus'].fillna(100)  # League average for unknown batters
    live['pitch_days_since_last_start'] = live['pitch_days_since_last_start'].fillna(5)
    live['pitch_avg_velocity'] = live['pitch_avg_velocity'].fillna(92.0)
    live['pitch_fb_allowed_rate'] = live['pitch_fb_allowed_rate'].fillna(0.35)
    live['pitch_hr_per_9'] = live['pitch_hr_per_9'].fillna(1.10)
    live['wind_out_component'] = live['wind_out_component'].fillna(0.0)
    live['bat_pull_rate'] = live['bat_pull_rate'].fillna(0.38)
    live['game_time'] = live['game_time'].fillna('') if 'game_time' in live.columns else ''
    live['home_team'] = live['home_team'].fillna('') if 'home_team' in live.columns else ''
    live['away_team'] = live['away_team'].fillna('') if 'away_team' in live.columns else ''
    live['batter_hand'] = live['batter_hand'].fillna('R') if 'batter_hand' in live.columns else 'R'
    live['pitcher_hand'] = live['pitcher_hand'].fillna('R') if 'pitcher_hand' in live.columns else 'R'
    
    # =====================================================================
    # PROFESSIONAL BETTOR FEATURES CALCULATION
    # =====================================================================
    
    # 1. Platoon Advantage Multiplier & Advanced Handedness Analysis (Pitcher L/R vs Batter L/R)
    live['platoon_advantage_multiplier'] = 1.0
    live['breaking_pitch_vulnerability'] = 1.0
    live['left_on_right_fade_score'] = 1.0
    live['reverse_split_anomaly_score'] = 1.0
    
    if get_pitcher_platoon_splits is not None and not statcast_df.empty:
        for idx, row in live.iterrows():
            try:
                pitcher_id = row.get('pitcher')
                batter_id = row.get('batter')
                batter_hand = row.get('batter_hand', 'R')
                
                if pitcher_id and batter_id:
                    # Main platoon advantage (includes sightline, breaking ball vulnerability, reverse splits)
                    mult = identify_platoon_mismatches(batter_id, pitcher_id, batter_hand, statcast_df)
                    live.at[idx, 'platoon_advantage_multiplier'] = mult
                    
                    # Breaking pitch vulnerability (slider breaks into power zone)
                    bp_vuln = detect_breaking_pitch_vulnerability(pitcher_id, batter_hand, statcast_df)
                    live.at[idx, 'breaking_pitch_vulnerability'] = bp_vuln
                    
                    # Left-on-right fade opportunity (RHP without changeup vs LHH)
                    if batter_hand == 'L':
                        lor_fade = identify_left_on_right_fade_opportunity(pitcher_id, statcast_df)
                        live.at[idx, 'left_on_right_fade_score'] = lor_fade
                    
                    # Reverse split anomaly (same-handed guy crushing it)
                    is_anomaly, anomaly_mult = detect_reverse_split_anomaly(pitcher_id, statcast_df)
                    if is_anomaly:
                        live.at[idx, 'reverse_split_anomaly_score'] = anomaly_mult
            except Exception:
                pass
    
    # 2. Bullpen Quality Scores
    live['bullpen_quality_score_home'] = 50.0  # Neutral default
    live['bullpen_quality_score_away'] = 50.0
    if get_pitcher_platoon_splits is not None:  # Use as marker for professional module availability
        for idx, row in live.iterrows():
            try:
                home_team = row.get('home_team', '')
                away_team = row.get('away_team', '')
                
                if home_team:
                    live.at[idx, 'bullpen_quality_score_home'] = calculate_bullpen_fatigue_score(home_team, datetime.today(), statcast_df)
                if away_team:
                    live.at[idx, 'bullpen_quality_score_away'] = calculate_bullpen_fatigue_score(away_team, datetime.today(), statcast_df)
            except Exception:
                pass
    
    # 3. Umpire Strike Zone Impact
    live['umpire_strike_zone_impact'] = 1.0
    if get_todays_umpires is not None:
        try:
            umpires = get_todays_umpires()
            for idx, row in live.iterrows():
                game_id = row.get('game_id')
                if game_id and game_id in umpires:
                    profile = umpires[game_id].get('profile', {})
                    live.at[idx, 'umpire_strike_zone_impact'] = profile.get('impact', 1.0)
        except Exception:
            pass
    
    # 4. Density Altitude Factor
    live['density_altitude_factor'] = 1.0
    if calculate_density_altitude is not None:
        for idx, row in live.iterrows():
            try:
                temp = row.get('temp', 70)
                elevation = row.get('elevation', 0)
                humidity = row.get('humidity', 50)
                
                if elevation == 0:
                    # Try to get from stadium info
                    game_id = row.get('game_id')
                    venue_id = row.get('venue_id')
                    if venue_id:
                        elevation = STADIUM_INFO.get(venue_id, {}).get('elevation', 0)
                
                da_calc = calculate_density_altitude(float(temp), float(elevation), float(humidity))
                live.at[idx, 'density_altitude_factor'] = da_calc.get('ball_carry_factor', 1.0)
            except Exception:
                pass
    
    # 5. Weather Extremes Multiplier
    live['weather_extremes_multiplier'] = 1.0
    if detect_weather_extremes is not None:
        for idx, row in live.iterrows():
            try:
                temp = row.get('temp', 70)
                wind = row.get('wind_speed', 0)
                humidity = row.get('humidity', 50)
                
                conditions = {'temperature': float(temp), 'wind_speed': float(wind), 'humidity': float(humidity)}
                mult = detect_weather_extremes(conditions)
                live.at[idx, 'weather_extremes_multiplier'] = mult
            except Exception:
                pass
    
    # 6. Market Micro-Structure Features
    live['line_release_window_flag'] = 1 if is_line_release_window_et() else 0
    live['nrfi_under_drag_score'] = 0.0
    live['sportsbook_value_score'] = 1.0

    _under_pressure = fetch_totals_market_pressure()
    if _under_pressure:
        def _under_drag(row):
            key = f"{str(row.get('away_name', '')).strip()} @ {str(row.get('home_name', '')).strip()}"
            return float(_under_pressure.get(key, 0.0))

        live['nrfi_under_drag_score'] = live.apply(_under_drag, axis=1)
        # Under pressure can create slightly inflated HR prices in props.
        live['sportsbook_value_score'] = (1.0 + (live['nrfi_under_drag_score'] * 0.10)).clip(1.0, 1.12)
    
    # =====================================================================
    # SECTION: BALLPARK DIMENSIONS FEATURES
    # =====================================================================
    live['ballpark_park_factor'] = 1.0
    live['porch_advantage_bonus'] = 1.0
    live['death_valley_penalty'] = 1.0
    live['would_be_hr_differential'] = 0.0
    
    if get_ballpark_factor is not None:
        for idx, row in live.iterrows():
            try:
                batter_hand = row.get('batter_hand', 'R')
                home_team = row.get('home_team')
                
                # Both teams hit in the home park; handedness controls the split.
                park_data = get_ballpark_factor(home_team, batter_hand)
                
                live.at[idx, 'ballpark_park_factor'] = park_data.get('park_factor', 1.0)
                live.at[idx, 'park_factor'] = float(park_data.get('park_factor', 1.0)) * 100.0
                
                # Porch advantage: detect short porch + recent warning-track fly balls
                recent_fb_dist = row.get('recent_flyball_distance')  # Optional
                porch_bonus = get_porch_advantage_bonus(
                    home_team,
                    batter_hand,
                    recent_fb_dist
                )
                live.at[idx, 'porch_advantage_bonus'] = porch_bonus
                
                # Death valley penalty
                exit_velo = row.get('bat_ev90', 90)
                penalty = get_death_valley_penalty(
                    home_team,
                    exit_velo
                )
                live.at[idx, 'death_valley_penalty'] = penalty
                
                # Would-Be HR differential (simplified - uses park factor difference)
                # In production, could pull from actual Statcast "would-be" calculation
                park_chars = park_data.get('characteristics', [])
                if 'short_porch' in park_chars or 'short_rf_porch' in park_chars:
                    live.at[idx, 'would_be_hr_differential'] = 0.10  # +10% for short porch
                elif 'death_valley' in park_chars or 'deep_cf' in park_chars:
                    live.at[idx, 'would_be_hr_differential'] = -0.08  # -8% for death valley
                elif 'lh_inflated' in park_chars and batter_hand.upper() == 'L':
                    live.at[idx, 'would_be_hr_differential'] = 0.08  # +8% for LHH in LH-friendly park
                elif 'rh_inflated' in park_chars and batter_hand.upper() == 'R':
                    live.at[idx, 'would_be_hr_differential'] = 0.08  # +8% for RHH in RH-friendly park
                    
            except Exception:
                pass
    
    # =====================================================================
    # FILL ALL PROFESSIONAL FEATURES WITH DEFAULTS
    # =====================================================================
    professional_features = [
        'platoon_advantage_multiplier', 'breaking_pitch_vulnerability',
        'left_on_right_fade_score', 'reverse_split_anomaly_score',
        'ballpark_park_factor', 'porch_advantage_bonus',
        'death_valley_penalty', 'would_be_hr_differential',
        'bullpen_quality_score_home', 'bullpen_quality_score_away',
        'umpire_strike_zone_impact', 'density_altitude_factor',
        'weather_extremes_multiplier', 'sportsbook_value_score'
    ]
    
    for col in professional_features:
        if col not in live.columns:
            if 'score' in col or 'multiplier' in col or 'impact' in col or 'factor' in col or 'value' in col:
                live[col] = 1.0
            else:
                live[col] = 50.0
        else:
            if 'score' in col or 'multiplier' in col or 'impact' in col or 'factor' in col or 'value' in col:
                live[col] = live[col].fillna(1.0)
            else:
                live[col] = live[col].fillna(50.0)

    # Pitcher intent pivot variable: out-of-zone avoidance against elite power bats.
    elite_power = set(
        b_stats[
            (pd.to_numeric(b_stats.get('bat_iso_proxy', 0.0), errors='coerce').fillna(0.0) >= 0.17) |
            (pd.to_numeric(b_stats.get('bat_wrc_plus', 100.0), errors='coerce').fillna(100.0) >= 130) |
            (pd.to_numeric(b_stats.get('bat_hr_rate', 0.0), errors='coerce').fillna(0.0) >= 0.05)
        ]['batter'].dropna().astype(float).tolist()
    ) if 'batter' in b_stats.columns else set()
    fear_map = compute_pitcher_intent_fear_factor(statcast_df, elite_power)
    live['pitcher_fear_factor'] = pd.to_numeric(
        live.get('pitcher', pd.Series([np.nan] * len(live))).apply(
            lambda x: fear_map.get(str(int(x)), 0.0) if pd.notna(x) else 0.0
        ),
        errors='coerce'
    ).fillna(0.0)
    live['is_elite_power_batter'] = pd.to_numeric(live.get('batter', np.nan), errors='coerce').isin(elite_power).astype(int)

    X_live = live[features_train]
    all_probs = [m.predict_proba(X_live)[:, 1] for m in trained_models]
    probs = sum(all_probs) / len(all_probs)

    # =====================================================================
    # PROFESSIONAL UPGRADE 1: PA Projection + Monte Carlo Simulation
    # =====================================================================
    # Project batting order-based PA count for each batter
    order_slots = live.get('batting_order_slot', pd.Series([5] * len(live))).fillna(5).astype(int).clip(1, 9)
    projected_pas = order_slots.apply(project_batting_order_pa)
    live['projected_pas'] = projected_pas
    
    # Run Monte Carlo: convert single-PA probability to game-level probability
    simulated_probs = pd.Series([
        monte_carlo_hr_simulation(p, avg_pas=pa) 
        for p, pa in zip(probs, projected_pas)
    ], index=range(len(probs)))
    
    # Use simulated (game-level) probabilities as our model prediction
    probs = simulated_probs.values
    base_model_probs = probs.copy()
    
    # =====================================================================
    # IMPROVEMENTS #1-3: PITCHER FORM, BATTER STREAKS, PARK ADJUSTMENT
    # =====================================================================
    print("Applying elite enhancements: pitcher form, batter streaks, park factors...")
    
    # IMPROVEMENT #1: Apply pitcher recent form tracking
    pitcher_form_boosts = np.ones(len(live))
    for idx, pitcher_id in enumerate(live.get('pitcher', pd.Series()).values):
        if pd.notna(pitcher_id) and pitcher_id in statcast_df['pitcher'].values:
            form_mult = get_pitcher_recent_form(statcast_df, pitcher_id, lookback_games=5)
            pitcher_form_boosts[idx] = form_mult
    
    # IMPROVEMENT #2: Apply batter hot/cold streaks
    batter_streak_boosts = np.ones(len(live))
    for idx, batter_id in enumerate(live.get('batter', pd.Series()).values):
        if pd.notna(batter_id) and batter_id in statcast_df['batter'].values:
            streak_mult = get_batter_hot_streak(statcast_df, batter_id, lookback_games=10)
            batter_streak_boosts[idx] = streak_mult
    
    # IMPROVEMENT #3: Apply park-adjusted metrics
    park_adjustments = np.ones(len(live))
    for idx, row in live.iterrows():
        home_team = row.get('home_team', '')
        batter_hand = row.get('stand', 'R')
        park_adj = apply_park_adjustment(1.0, batter_hand, home_team, '')
        park_adjustments[idx] = park_adj
    
    # Apply combined boosts
    combined_boosts = pitcher_form_boosts * batter_streak_boosts * park_adjustments
    base_model_probs = np.clip(base_model_probs * combined_boosts, 0.0, 1.0)
    
    elite_enhancements_applied = (pitcher_form_boosts != 1.0).sum() + (batter_streak_boosts != 1.0).sum()
    print(f"✅ Elite enhancements applied: {elite_enhancements_applied} matchups boosted/adjusted")
    
    # POWER SPIKE DETECTION: Boost probabilities for batters with recent hot streaks
    # Check if batter has recent high HR rate or exit velocity surge
    if 'bat_hr_rate_recent' in live.columns and 'bat_avg_exit_velocity' in live.columns:
        hot_streak_multiplier = np.ones(len(live))
        
        # Power spike detection: Recent HR rate > 0.08 (league avg ~0.03) signals hot batter
        hot_batters = pd.to_numeric(live.get('bat_hr_rate_recent', 0), errors='coerce') > 0.08
        high_exit_velo = pd.to_numeric(live.get('bat_avg_exit_velocity', 0), errors='coerce') > 90.0
        
        # Boost by 1.15x if both hot streak indicators present
        hot_streak_multiplier[hot_batters & high_exit_velo] = 1.15
        hot_streak_multiplier[hot_batters] = 1.10
        
        base_model_probs = np.clip(base_model_probs * hot_streak_multiplier, 0.0, 1.0)
        probs = base_model_probs.copy()
    
    # PITCHER DEGRADATION BOOST: Increase probabilities when pitcher is showing decline
    # This catches power spike events where pitcher suddenly gets hit harder
    pitcher_degradation = detect_pitcher_degradation(statcast_df, days_lookback=7)
    if pitcher_degradation:
        degradation_boosts = live.get('pitcher', pd.Series()).map(pitcher_degradation).fillna(0.0)
        # Boost formula: if pitcher has 50% degradation score, add 8% to probability
        degradation_boosts = np.clip(degradation_boosts * 0.16, 0.0, 0.12)  # Max boost 12%
        base_model_probs = np.clip(base_model_probs * (1.0 + degradation_boosts), 0.0, 1.0)
        print(f"Pitcher degradation boosts applied: {(degradation_boosts > 0).sum()} matchups")
    
    live['base_model_prob'] = base_model_probs
    physics_probs = base_model_probs.copy()

    # =====================================================================
    # PROFESSIONAL UPGRADE 1.5: FULL PA PHYSICS + CONTEXT PIPELINE
    # =====================================================================
    if apply_physics_pipeline_to_live is not None and not statcast_df.empty:
        try:
            live = apply_physics_pipeline_to_live(live, statcast_df)
            if 'physics_hr_prob' in live.columns:
                _physics_series = pd.to_numeric(live['physics_hr_prob'], errors='coerce')
                _base_series = pd.Series(base_model_probs, index=_physics_series.index)
                physics_probs = _physics_series.fillna(_base_series).values
        except BaseException as _physics_err:
            print(f"Physics pipeline skipped: {_physics_err}")

    # Runtime probability mode selection and blend calibration.
    prob_mode, physics_weight = resolve_probability_mode_and_weight()
    if prob_mode == 'base':
        probs = base_model_probs
        print("Probability mode: base (ML model only)")
    elif prob_mode == 'physics':
        probs = np.clip(physics_probs, 0.0, 1.0)
        print("Probability mode: physics (PA simulation only)")
    else:
        probs = np.clip((base_model_probs * (1 - physics_weight)) + (physics_probs * physics_weight), 0.0, 1.0)
        print(
            "Probability mode: blended "
            f"(physics={physics_weight:.2f}, base={1 - physics_weight:.2f})"
        )

    # Guard against extreme physics-driven jumps on already-high baseline hitters.
    physics_uplift_cap = np.where(
        base_model_probs >= 0.35,
        0.07,
        np.where(base_model_probs >= 0.20, 0.10, 0.13)
    )
    probs = np.minimum(probs, base_model_probs + physics_uplift_cap)

    live['physics_hr_prob'] = physics_probs
    live['blend_weight_physics'] = physics_weight if prob_mode == 'blended' else 0.0
    live['probability_mode'] = prob_mode
    live['physics_delta'] = probs - base_model_probs
    live['physics_uplift_cap'] = physics_uplift_cap

    def _live_num(col, default):
        if col in live.columns:
            s = live[col]
        else:
            s = pd.Series([default] * len(live), index=live.index)
        return pd.to_numeric(s, errors='coerce').fillna(default)

    # Closed-loop adaptive coefficient layer (reward-function backprop application).
    temp_signal = ((_live_num('temp', 72.0) - 75.0) / 20.0).clip(0.0, 1.0)
    wind_signal = (_live_num('wind_out_component', 0.0) / 12.0).clip(0.0, 1.0)
    spin_signal = (
        _live_num('spin_decay_flag', 0.0)
        + (_live_num('spin_decay_rpm', 0.0) / 300.0)
    ).clip(0.0, 1.0)
    fatigue_signal = (
        (_live_num('circadian_disruption_index', 0.0) / 24.0)
        + (1.0 - _live_num('visual_fatigue_modifier', 1.0))
    ).clip(0.0, 1.0)

    temp_w = float(adaptive_coeffs.get('temp_weight', 1.0))
    wind_w = float(adaptive_coeffs.get('wind_weight', 1.0))
    spin_w = float(adaptive_coeffs.get('spin_weight', 1.0))
    fatigue_w = float(adaptive_coeffs.get('fatigue_weight', 1.0))

    adaptive_mult = (
        1.0
        + ((temp_w - 1.0) * temp_signal)
        + ((wind_w - 1.0) * wind_signal)
        + ((spin_w - 1.0) * spin_signal)
        + ((fatigue_w - 1.0) * fatigue_signal)
    ).clip(0.75, 1.30)
    probs = np.clip(probs * adaptive_mult.values, 0.0, 1.0)
    live['adaptive_feedback_multiplier'] = adaptive_mult.values

    # Pitcher intent suppression: elite hitters see fewer hittable strikes from fearful pitchers.
    intent_coeff = float(adaptive_coeffs.get('pitch_intent_weight', 0.22))
    env_prime = (
        ((_live_num('park_factor', 100.0) - 100.0) / 25.0)
        + (_live_num('wind_out_component', 0.0) / 12.0)
        + ((_live_num('temp', 72.0) - 72.0) / 20.0)
    ).clip(0.0, 1.2)
    fear = _live_num('pitcher_fear_factor', 0.0)
    elite_flag = _live_num('is_elite_power_batter', 0.0)
    intent_suppression = (1.0 - (intent_coeff * fear * env_prime * elite_flag)).clip(0.70, 1.00)
    probs = np.clip(probs * intent_suppression.values, 0.0, 1.0)
    live['pitcher_intent_suppression'] = intent_suppression.values

    # Multi-agent adversarial synthesis (Optimist vs Pessimist) before final betting math.
    try:
        debate_rounds = max(500, int(str(os.getenv('ADVERSARIAL_DEBATE_ROUNDS', '5000')).strip() or '5000'))
    except Exception:
        debate_rounds = 5000
    live = run_adversarial_debate_layer(live, rounds=debate_rounds)
    probs = np.clip(probs * pd.to_numeric(live.get('adversarial_multiplier', 1.0), errors='coerce').fillna(1.0).values, 0.0, 1.0)

    # Cap stacked post-model boosts so one row cannot dominate the card from layered multipliers.
    stacked_multiplier_cap = np.where(
        base_model_probs >= 0.35,
        1.18,
        np.where(base_model_probs >= 0.20, 1.28, 1.42)
    )
    safe_base = np.clip(base_model_probs, 1e-6, 1.0)
    observed_multiplier = probs / safe_base
    capped_multiplier = np.minimum(observed_multiplier, stacked_multiplier_cap)
    probs = np.clip(safe_base * capped_multiplier, 0.0, 1.0)
    live['stacked_boost_multiplier_raw'] = observed_multiplier
    live['stacked_boost_multiplier_cap'] = stacked_multiplier_cap
    live['stacked_boost_multiplier_final'] = capped_multiplier

    # =====================================================================
    # PROFESSIONAL UPGRADE 2: Kelly Criterion with Simulated Probabilities
    # =====================================================================
    # Batting order PA multiplier is now baked into simulation, but keep market baseline for EV calc
    _market_american = float(os.getenv('MARKET_HR_ODDS', '-120'))
    _dec_odds = 1 + 100 / abs(_market_american) if _market_american < 0 else 1 + _market_american / 100
    _b = _dec_odds - 1
    _market_prob = float(os.getenv('MARKET_HR_BASELINE', '0.09'))

    kelly_multiplier = resolve_kelly_multiplier(days_lookback=14, default_multiplier=0.50)

    send_morning_learning_summary(
        learning_result=learning_result,
        missed_count=missed_count,
        scale_pos_weight=scale_pos_weight,
        physics_weight=physics_weight if prob_mode == 'blended' else 0.0,
        kelly_multiplier=kelly_multiplier,
    )

    def _kelly(p):
        edge = p * _b - (1 - p)
        return max(round(edge / _b * kelly_multiplier, 4), 0.0) if edge > 0 else 0.0

    live['pred_hr_prob'] = probs
    live = apply_daily_hr_volume_constraints(
        live,
        game_count=max(1, int(len(live.get('game_pk').dropna().unique()) // 9)) if 'game_pk' in live.columns else 1,
        avg_hr_per_game=2.3,
    )
    live = apply_poisson_hr_filter(live, k=5, p_threshold=0.05, min_game_prob=0.20)
    live['edge_pct'] = ((live['pred_hr_prob'] - _market_prob) / _market_prob * 100).round(1)
    
    # =====================================================================
    # IMPROVEMENT #4: Model Calibration & Confidence Intervals
    # IMPROVEMENT #10: Uncertainty Quantification for Discord Alerts
    # =====================================================================
    print("Calculating confidence intervals and reliability levels...")
    
    confidence_lower = []
    confidence_upper = []
    reliability_levels = []
    consistency_scores = []
    
    for idx, row in live.iterrows():
        batter_id = row.get('batter', None)
        pred_prob = row['pred_hr_prob']
        
        # Get batter consistency (0-1 scale, higher = more consistent)
        if pd.notna(batter_id) and batter_id in statcast_df['batter'].values:
            consistency = get_batter_consistency(statcast_df, batter_id)
        else:
            consistency = 0.5
        consistency_scores.append(consistency)
        
        # Calculate confidence interval
        lower, upper = calculate_confidence_interval(pred_prob, sample_size=100)
        confidence_lower.append(lower)
        confidence_upper.append(upper)
        
        # Estimate reliability level
        reliability = estimate_model_reliability(pred_prob, consistency, sample_size=100)
        reliability_levels.append(reliability)
    
    live['confidence_lower_95pct'] = confidence_lower
    live['confidence_upper_95pct'] = confidence_upper
    live['model_reliability'] = reliability_levels
    live['batter_consistency_score'] = consistency_scores

    # Reliability-aware hard ceilings to prevent unsupported top-end inflation.
    rel_upper = live['model_reliability'].astype(str).str.upper()
    rel_cap = np.where(rel_upper == 'HIGH', 0.62, np.where(rel_upper == 'MEDIUM', 0.52, 0.42))
    live['reliability_prob_cap'] = rel_cap
    live['pred_hr_prob'] = np.minimum(pd.to_numeric(live['pred_hr_prob'], errors='coerce').fillna(0.0), rel_cap)
    
    high_conf_count = sum(1 for r in reliability_levels if r == 'HIGH')
    print(f"✅ Calibration complete: {high_conf_count}/{len(live)} predictions HIGH confidence")
    live['kelly_fraction'] = live['pred_hr_prob'].apply(_kelly)
    live['kelly_multiplier'] = kelly_multiplier
    live['model_name'] = '+'.join(model_names)
    live['prediction_timestamp'] = datetime.now().isoformat()

    # Initialize EV columns (default case: no market odds available yet)
    if 'ev_value' not in live.columns:
        live['ev_value'] = 0.0
    if 'ev_percent' not in live.columns:
        live['ev_percent'] = 0.0
    if 'is_positive_ev' not in live.columns:
        live['is_positive_ev'] = False

    # Apply real market odds if ODDS_API_KEY is configured
    market_odds_raw = fetch_hr_prop_odds_raw()
    market_odds = _build_devigged_probs_from_raw_books(market_odds_raw)

    def _match_raw_best_line(bname):
        if not market_odds_raw:
            return None, None
        bname_lower = str(bname).lower().strip()
        best_book = None
        best_odds = None
        best_decimal = -1.0

        for key, book_map in market_odds_raw.items():
            key_lower = key.lower().strip()
            if not (bname_lower in key_lower or key_lower in bname_lower or bname_lower.split()[-1] in key_lower):
                continue
            for bk, odds in (book_map or {}).items():
                try:
                    o = float(odds)
                    dec = (1 + (o / 100.0)) if o > 0 else (1 + (100.0 / abs(o)))
                    if dec > best_decimal:
                        best_decimal = dec
                        best_odds = int(round(o))
                        best_book = bk
                except Exception:
                    continue
        return best_book, best_odds

    def _match_raw_book_lines(bname):
        if not market_odds_raw:
            return {}
        bname_lower = str(bname).lower().strip()
        matched_books = {}
        for key, book_map in market_odds_raw.items():
            key_lower = str(key).lower().strip()
            if not (bname_lower in key_lower or key_lower in bname_lower or bname_lower.split()[-1] in key_lower):
                continue
            for bk, odds in (book_map or {}).items():
                try:
                    o = float(odds)
                    if np.isfinite(o):
                        cur = matched_books.get(str(bk))
                        if cur is None:
                            matched_books[str(bk)] = o
                        else:
                            cur_dec = (1 + (cur / 100.0)) if cur > 0 else (1 + (100.0 / abs(cur)))
                            new_dec = (1 + (o / 100.0)) if o > 0 else (1 + (100.0 / abs(o)))
                            if new_dec > cur_dec:
                                matched_books[str(bk)] = o
                except Exception:
                    continue
        return matched_books

    if market_odds or market_odds_raw:
        def _match_odds(bname):
            bname_lower = bname.lower()
            for key, prob in market_odds.items():
                if bname_lower in key.lower() or key.lower() in bname_lower:
                    return prob
                if bname.split()[-1].lower() in key.lower():
                    return prob
            return None
        live['market_prob'] = live['batter_name'].apply(_match_odds) if market_odds else np.nan

        raw_matches = live['batter_name'].apply(_match_raw_best_line)
        raw_book_lines = live['batter_name'].apply(_match_raw_book_lines)
        live['best_book'] = raw_matches.apply(lambda x: x[0])
        live['best_market_odds_american'] = raw_matches.apply(lambda x: x[1])
        live['best_market_implied_prob'] = live['best_market_odds_american'].apply(
            lambda x: american_to_implied_prob(x) if pd.notna(x) else np.nan
        )
        live['matched_book_count'] = raw_book_lines.apply(lambda d: int(len(d)) if isinstance(d, dict) else 0)

        def _book_dispersion(d):
            if not isinstance(d, dict) or not d:
                return 0.0
            probs = [american_to_implied_prob(v) for v in d.values()]
            probs = [p for p in probs if p is not None and np.isfinite(p)]
            if len(probs) < 2:
                return 0.0
            return float(max(probs) - min(probs))

        def _arbitrage_value_pct(d):
            if not isinstance(d, dict) or not d:
                return 0.0
            implied = []
            for v in d.values():
                p = american_to_implied_prob(v)
                if p is not None and np.isfinite(p):
                    implied.append(float(p))
            if len(implied) < 2:
                return 0.0
            best_implied = float(min(implied))
            median_implied = float(np.median(implied))
            if median_implied <= 0:
                return 0.0
            return float(max(0.0, ((median_implied - best_implied) / median_implied) * 100.0))

        live['book_prob_dispersion'] = raw_book_lines.apply(_book_dispersion)
        live['arbitrage_value_pct'] = raw_book_lines.apply(_arbitrage_value_pct)

        # Prefer devigged market prob where available, else implied from best line.
        live['market_prob'] = pd.to_numeric(live['market_prob'], errors='coerce')
        live['market_prob'] = live['market_prob'].fillna(live['best_market_implied_prob'])

        medium_no_odds_mask = (
            live['model_reliability'].astype(str).str.upper().eq('MEDIUM')
            & live['market_prob'].isna()
        )
        if medium_no_odds_mask.any():
            medium_cap = float(os.getenv('MEDIUM_NO_ODDS_PROB_CAP', '0.33'))
            live.loc[medium_no_odds_mask, 'pred_hr_prob'] = np.minimum(
                pd.to_numeric(live.loc[medium_no_odds_mask, 'pred_hr_prob'], errors='coerce').fillna(0.0),
                medium_cap
            )
            live.loc[medium_no_odds_mask, 'reliability_prob_cap'] = np.minimum(
                pd.to_numeric(live.loc[medium_no_odds_mask, 'reliability_prob_cap'], errors='coerce').fillna(medium_cap),
                medium_cap
            )

        matched = live['market_prob'].notna().sum()
        print(f"Odds matched: {matched}/{len(live)} batters have market lines")
        
        # =====================================================================
        # PROFESSIONAL UPGRADE 3: Advanced EV+ Filtering & True Expected Value
        # =====================================================================
        def calculate_row_ev(row):
            """Calculate true EV for this specific batter using market odds."""
            model_p = row['pred_hr_prob']
            market_p = row.get('market_prob', None)
            
            if market_p is None or pd.isna(market_p) or market_p <= 0 or market_p >= 1:
                return 0.0, 0.0
            
            # Decimal odds from implied probability
            decimal_odds = 1 / market_p
            
            # EV = (Model Prob × Decimal Odds - 1) - (1 - Model Prob) × (Overhead/Vig)
            # Simplified: EV = Model Prob × Decimal Odds - 1
            ev_value = model_p * decimal_odds - 1
            ev_percent = ev_value * 100
            
            return ev_value, ev_percent
        
        live[['ev_value', 'ev_percent']] = live.apply(
            lambda r: pd.Series(calculate_row_ev(r)), axis=1
        )

        live['fair_odds_american'] = live['pred_hr_prob'].apply(prob_to_fair_american)
        live['prob_edge_abs'] = (live['pred_hr_prob'] - live['market_prob']).fillna(0.0)

        elite_edge_abs = float(os.getenv('EV_EDGE_TRIGGER_ABS', '0.03'))
        elite_ev_pct = float(os.getenv('EV_TRIGGER_PCT', '10.0'))
        arb_min_books = max(2, int(float(os.getenv('ARB_MIN_BOOKS', '3'))))
        arb_min_value_pct = float(os.getenv('ARB_MIN_VALUE_PCT', '3.0'))
        live['elite_ev_signal'] = (
            (live['prob_edge_abs'] >= elite_edge_abs) &
            (live['ev_percent'] >= elite_ev_pct)
        )
        live['positive_ev_arbitrage'] = (
            (live['ev_percent'] > 0) &
            (live['arbitrage_value_pct'] >= arb_min_value_pct) &
            (live['matched_book_count'] >= arb_min_books)
        )
        live['arbitrage_score'] = (
            (live['ev_percent'].clip(lower=0.0) * 0.50) +
            (live['arbitrage_value_pct'].clip(lower=0.0) * 0.35) +
            (live['prob_edge_abs'].clip(lower=0.0) * 100.0 * 0.15)
        )
        live['release_window_sniper_signal'] = (
            (live.get('line_release_window_flag', 0) == 1) & live['elite_ev_signal']
        )

        # Blend sportsbook value score with observed edge and under-drag regime.
        _sv = 1.0 + live['prob_edge_abs'].clip(lower=0.0, upper=0.12)
        _nrfi = 1.0 + pd.to_numeric(live.get('nrfi_under_drag_score', 0.0), errors='coerce').fillna(0.0) * 0.08
        _arb = 1.0 + (live['arbitrage_value_pct'].clip(lower=0.0, upper=12.0) / 100.0) * 0.55
        live['sportsbook_value_score'] = (_sv * _nrfi * _arb).clip(1.0, 1.35)
        
        # Filter for +EV opportunities (profitable bets only)
        live['is_positive_ev'] = live['ev_percent'] > 0
        
        # Recalculate edge % using real market probability
        live['edge_pct'] = live.apply(
            lambda r: round((r['pred_hr_prob'] - r['market_prob']) / r['market_prob'] * 100, 1)
            if pd.notna(r['market_prob']) else r['edge_pct'], axis=1
        )
        
        # Kelly with real market odds
        def _kelly_real(row):
            mp = row.get('market_prob', None)
            if mp is None or pd.isna(mp) or mp <= 0 or mp >= 1:
                return row['kelly_fraction']
            b = (1 - mp) / mp
            edge = row['pred_hr_prob'] * b - (1 - row['pred_hr_prob'])
            return max(round(edge / b * kelly_multiplier, 4), 0.0) if edge > 0 else 0.0
        live['kelly_fraction'] = live.apply(_kelly_real, axis=1)
    else:
        # No real odds: mark EV as zero
        medium_mask = live['model_reliability'].astype(str).str.upper().eq('MEDIUM')
        if medium_mask.any():
            medium_cap = float(os.getenv('MEDIUM_NO_ODDS_PROB_CAP', '0.33'))
            live.loc[medium_mask, 'pred_hr_prob'] = np.minimum(
                pd.to_numeric(live.loc[medium_mask, 'pred_hr_prob'], errors='coerce').fillna(0.0),
                medium_cap
            )
            live.loc[medium_mask, 'reliability_prob_cap'] = np.minimum(
                pd.to_numeric(live.loc[medium_mask, 'reliability_prob_cap'], errors='coerce').fillna(medium_cap),
                medium_cap
            )

        live['ev_value'] = 0.0
        live['ev_percent'] = 0.0
        live['is_positive_ev'] = False
        live['best_book'] = None
        live['best_market_odds_american'] = np.nan
        live['best_market_implied_prob'] = np.nan
        live['fair_odds_american'] = live['pred_hr_prob'].apply(prob_to_fair_american)
        live['prob_edge_abs'] = 0.0
        live['elite_ev_signal'] = False
        live['matched_book_count'] = 0
        live['book_prob_dispersion'] = 0.0
        live['arbitrage_value_pct'] = 0.0
        live['positive_ev_arbitrage'] = False
        live['arbitrage_score'] = 0.0
        live['release_window_sniper_signal'] = False

    physics_output_defaults = {
        'physics_hr_prob': 0.0,
        'physics_per_pa_hr_prob': 0.0,
        'adaptive_feedback_multiplier': 1.0,
        'pitcher_fear_factor': 0.0,
        'pitcher_intent_suppression': 1.0,
        'is_elite_power_batter': 0,
        'optimist_score': 0.0,
        'pessimist_score': 0.0,
        'debate_rounds': 5000,
        'optimist_wins': 2500,
        'pessimist_wins': 2500,
        'adversarial_margin': 0.0,
        'adversarial_multiplier': 1.0,
        'density_altitude_ft': 0.0,
        'air_density_kg_m3': 1.225,
        'drag_multiplier': 1.0,
        'pitch_micro_matchup_score': 1.0,
        'pitch_arsenal_matchup_score': 1.0,
        'vaa_attack_angle_score': 1.0,
        'umpire_catcher_cascade': 1.0,
        'umpire_zone_drift_score': 1.0,
        'umpire_hotzone_overlap': 0.0,
        'fatigue_index': 0.0,
        'circadian_disruption_index': 0.0,
        'visual_fatigue_modifier': 1.0,
        'travel_distance_miles': 0.0,
        'rest_day_count': 1.0,
        'spin_decay_rpm': 0.0,
        'spin_decay_flag': 0.0,
        'release_pos_x_std_15': 0.0,
        'release_pos_z_std_15': 0.0,
        'release_extension_decay_ft': 0.0,
        'spin_velocity_ratio_decay': 0.0,
        'primary_weapon_vulnerable_pitch_count': 0.0,
        'bullpen_exposure_multiplier': 1.0,
        'lineup_protection_woba_proxy': 0.10,
        'context_multiplier': 1.0,
        'ballistic_hr_proxy_prob': 0.0,
        'ballistic_carry_distance_ft': 0.0,
        'ballistic_barrier_distance_ft': 370.0,
        'ballistic_carry_gap_ft': 0.0,
        'ballistic_multiplier': 1.0,
        'arbitrage_value_pct': 0.0,
        'book_prob_dispersion': 0.0,
        'matched_book_count': 0,
        'positive_ev_arbitrage': 0,
        'arbitrage_score': 0.0,
    }
    for _col, _default in physics_output_defaults.items():
        if _col not in live.columns:
            live[_col] = _default
        else:
            live[_col] = pd.to_numeric(live[_col], errors='coerce').fillna(_default)

    persist_daily_predictions(live[['game_pk', 'game_time', 'batter', 'batter_name', 'pitcher', 'pitcher_name',
                                    'has_platoon_advantage', 'park_factor', 'temp', 'wind_speed',
                                    'pred_hr_prob', 'edge_pct', 'kelly_fraction', 'ev_value', 'ev_percent',
                                    'kelly_multiplier',
                                    'base_model_prob', 'physics_delta', 'blend_weight_physics', 'probability_mode',
                                    'physics_uplift_cap',
                                    'reliability_prob_cap',
                                    'adaptive_feedback_multiplier', 'pitcher_fear_factor', 'pitcher_intent_suppression',
                                    'is_elite_power_batter', 'optimist_score', 'pessimist_score', 'debate_rounds',
                                    'optimist_wins', 'pessimist_wins', 'adversarial_margin', 'adversarial_multiplier',
                                    'stacked_boost_multiplier_raw', 'stacked_boost_multiplier_cap',
                                    'stacked_boost_multiplier_final',
                                    'best_book', 'best_market_odds_american', 'best_market_implied_prob',
                                    'fair_odds_american', 'prob_edge_abs', 'elite_ev_signal',
                                    'arbitrage_value_pct', 'book_prob_dispersion', 'matched_book_count',
                                    'positive_ev_arbitrage', 'arbitrage_score',
                                    'release_window_sniper_signal', 'line_release_window_flag', 'nrfi_under_drag_score',
                                    'physics_hr_prob', 'physics_per_pa_hr_prob', 'density_altitude_ft',
                                    'air_density_kg_m3', 'drag_multiplier', 'pitch_micro_matchup_score',
                                    'pitch_arsenal_matchup_score', 'vaa_attack_angle_score', 'umpire_catcher_cascade', 'umpire_zone_drift_score',
                                    'umpire_hotzone_overlap', 'fatigue_index', 'circadian_disruption_index', 'visual_fatigue_modifier',
                                    'travel_distance_miles', 'rest_day_count',
                                    'ballistic_hr_proxy_prob', 'ballistic_carry_distance_ft',
                                    'ballistic_barrier_distance_ft', 'ballistic_carry_gap_ft', 'ballistic_multiplier',
                                    'spin_decay_rpm', 'spin_decay_flag', 'release_pos_x_std_15', 'release_pos_z_std_15',
                                    'release_extension_decay_ft', 'spin_velocity_ratio_decay', 'primary_weapon_vulnerable_pitch_count',
                                    'bullpen_exposure_multiplier',
                                    'lineup_protection_woba_proxy', 'context_multiplier',
                                    'confidence_lower_95pct', 'confidence_upper_95pct', 'model_reliability', 'batter_consistency_score',
                                    'model_name', 'prediction_timestamp']])

    def _env_int(name, default):
        try:
            return int(float(os.getenv(name, str(default))))
        except Exception:
            return int(default)

    def _env_float(name, default):
        try:
            return float(os.getenv(name, str(default)))
        except Exception:
            return float(default)

    # Sort and present elite values
    rankings = _prepare_discord_rankings(
        live[['batter_name', 'pitcher_name', 'pred_hr_prob', 'edge_pct', 'kelly_fraction', 'ev_percent', 'game_time', 'model_reliability']]
    )

    discord_top_prob_n = max(10, _env_int('DISCORD_TOP_PROB_COUNT', 30))
    discord_top_ev_n = max(3, _env_int('DISCORD_TOP_EV_COUNT', 12))
    discord_rows_per_message = max(5, _env_int('DISCORD_ROWS_PER_MESSAGE', 10))
    discord_min_prob = _env_float('DISCORD_MIN_PROB', 0.06)
    discord_radar_n = max(8, _env_int('DISCORD_RADAR_COUNT', 20))
    discord_window_1_hours = max(1, _env_int('DISCORD_WINDOW_1_HOURS', 2))
    discord_window_2_hours = max(discord_window_1_hours + 1, _env_int('DISCORD_WINDOW_2_HOURS', 6))

    # Top probabilities for reporting/Discord delivery.
    prob_pool = rankings.sort_values(by='hr_probability', ascending=False).reset_index(drop=True)
    top_prob = _select_thresholded_candidates(prob_pool, discord_min_prob, discord_top_prob_n)
    if top_prob.empty:
        print(f"No predictions met the minimum Discord confidence threshold ({discord_min_prob * 100:.0f}%).")

    if 'physics_delta' not in live.columns:
        live['physics_delta'] = 0.0

    radar = _prepare_discord_rankings(
        live[[
            'batter_name', 'pitcher_name', 'pred_hr_prob', 'edge_pct',
            'kelly_fraction', 'ev_percent', 'game_time', 'model_reliability', 'physics_delta'
        ]]
    ).copy()
    radar = _finalize_discord_radar_frame(radar)
    radar = radar[radar['hr_probability'] >= max(0.03, discord_min_prob * 0.7)]

    top_keys = set(zip(top_prob['batter_name'].astype(str), top_prob['pitcher_name'].astype(str)))
    radar = radar[
        ~radar.apply(lambda r: (str(r['batter_name']), str(r['pitcher_name'])) in top_keys, axis=1)
    ]
    radar = pd.DataFrame(radar).copy()
    radar['hr_probability'] = _coerce_numeric_column(radar, 'hr_probability', default=0.0)
    radar['physics_delta'] = _coerce_numeric_column(radar, 'physics_delta', default=0.0)
    radar['physics_delta_abs'] = radar['physics_delta'].abs()
    radar = radar.sort_values(
        by=['physics_delta_abs', 'hr_probability'],
        ascending=[False, False]
    ).head(discord_radar_n).reset_index(drop=True)

    def _annotate_time_windows(df):
        if df is None or df.empty:
            return df

        now = datetime.now()
        out = df.copy()

        def _minutes_until(value):
            try:
                raw = str(value or '').strip()
                if not raw:
                    return 99999
                t = datetime.strptime(raw, '%I:%M %p').time()
                target = datetime.combine(now.date(), t)
                mins = int((target - now).total_seconds() // 60)
                # Treat times that already passed as next-day starts if far enough behind.
                if mins < -90:
                    mins += 24 * 60
                return mins
            except Exception:
                return 99999

        def _window_label(mins):
            if mins == 99999:
                return 'Unknown'
            if mins <= (discord_window_1_hours * 60):
                return f'<= {discord_window_1_hours}h'
            if mins <= (discord_window_2_hours * 60):
                return f'<= {discord_window_2_hours}h'
            return 'Later'

        out['__minutes_until_start'] = out['game_time'].apply(_minutes_until)
        out['start_window'] = out['__minutes_until_start'].apply(_window_label)
        out = out.sort_values(
            by=['__minutes_until_start', 'hr_probability', 'ev_pct', 'kelly_fraction'],
            ascending=[True, False, False, False]
        ).reset_index(drop=True)
        return out

    top_prob = _annotate_time_windows(top_prob)
    radar = _annotate_time_windows(radar)

    # Show largest movers caused by physics/context simulation.
    if 'physics_delta' in live.columns:
        movers = live[['batter_name', 'pitcher_name', 'physics_delta', 'base_model_prob', 'pred_hr_prob']].copy()
        movers = movers.sort_values(by='physics_delta', key=lambda s: s.abs(), ascending=False).head(5)
        print("\nTop 5 Physics-Driven Movers (absolute delta vs base model):")
        for _, mr in movers.iterrows():
            print(
                f"  {mr['batter_name']} vs {mr['pitcher_name']} | "
                f"delta={mr['physics_delta']:+.3f} | "
                f"base={mr['base_model_prob']:.3f} -> final={mr['pred_hr_prob']:.3f}"
            )

    if 'elite_ev_signal' in live.columns:
        elite = live[live['elite_ev_signal'] == True].copy()
        if not elite.empty:
            elite_view = elite[['batter_name', 'pitcher_name', 'pred_hr_prob', 'best_market_odds_american', 'fair_odds_american', 'ev_percent', 'best_book']]
            elite_view = elite_view.sort_values('ev_percent', ascending=False).head(10)
            print("\nElite +EV Discrepancy Signals:")
            print(elite_view.to_string(index=False))

    if 'positive_ev_arbitrage' in live.columns:
        arb = live[live['positive_ev_arbitrage'] == True].copy()
        if not arb.empty:
            arb_view = arb[[
                'batter_name', 'pitcher_name', 'best_book', 'best_market_odds_american',
                'ev_percent', 'arbitrage_value_pct', 'matched_book_count', 'arbitrage_score'
            ]].sort_values(['arbitrage_score', 'ev_percent'], ascending=[False, False]).head(10)
            print("\nTop Positive EV Arbitrage Signals:")
            print(arb_view.to_string(index=False))

    if {'circadian_disruption_index', 'visual_fatigue_modifier'}.issubset(set(live.columns)):
        circadian_view = live[['batter_name', 'pitcher_name', 'circadian_disruption_index', 'visual_fatigue_modifier', 'travel_distance_miles', 'rest_day_count', 'pred_hr_prob']].copy()
        circadian_view = circadian_view.sort_values(['circadian_disruption_index', 'travel_distance_miles'], ascending=[False, False]).head(5)
        if not circadian_view.empty:
            print("\nTop 5 Circadian/Travel Fatigue Flags:")
            print(circadian_view.to_string(index=False))

    if {'umpire_zone_drift_score', 'umpire_hotzone_overlap'}.issubset(set(live.columns)):
        umpire_view = live[['batter_name', 'pitcher_name', 'umpire_zone_drift_score', 'umpire_hotzone_overlap', 'umpire_catcher_cascade', 'pred_hr_prob']].copy()
        umpire_view = umpire_view.sort_values(['umpire_zone_drift_score', 'umpire_hotzone_overlap'], ascending=[False, False]).head(5)
        if not umpire_view.empty:
            print("\nTop 5 Umpire Zone Drift Boosts:")
            print(umpire_view.to_string(index=False))

    pair_df = build_learned_hr_pairings(
        live,
        days_back=max(14, int(os.getenv('PARLAY_LEARN_DAYS', '60'))),
        candidate_n=max(12, int(os.getenv('PARLAY_CANDIDATE_BATTERS', '36'))),
    )
    if not pair_df.empty:
        print("\nTop Learned 2-Leg HR Pairings (Any Game):")
        pair_view = pair_df[[
            'pair_leg_1', 'pair_leg_2', 'pair_type',
            'combo_prob', 'parlay_ev', 'learned_multiplier', 'training_days_used'
        ]].head(10)
        print(pair_view.to_string(index=False))
        try:
            pair_path = Path('data') / f"parlay_candidates_{datetime.today().strftime('%Y-%m-%d')}.csv"
            pair_df.to_csv(pair_path, index=False)
            print(f"Saved learned pairing candidates: {pair_path}")
        except Exception:
            pass
    
    # Also identify +EV only picks (if odds available)
    top_ev = pd.DataFrame()
    if 'is_positive_ev' in live.columns:
        positive_ev = live[live['is_positive_ev'] == True].copy()
        if not positive_ev.empty:
            top_ev = _prepare_discord_rankings(
                positive_ev[['batter_name', 'pitcher_name', 'pred_hr_prob', 'edge_pct', 'kelly_fraction', 'ev_percent', 'game_time', 'model_reliability']]
            )
            top_ev = top_ev.sort_values(by='ev_pct', ascending=False).head(discord_top_ev_n).reset_index(drop=True)
            print(f"\n✅ +EV PREMIUM PICKS (Expected Value > 0%):")
            print(top_ev.to_string(index=False))
    top_ev = _annotate_time_windows(top_ev)

    print(f"\nMost Likely Homers (≥{discord_min_prob*100:.0f}% confidence) - {len(top_prob)} candidates:")
    print(top_prob.to_string(index=False))
    print(f"\nRadar Coverage: {len(radar)} additional candidates")

    # =====================================================================
    # DISCORD WEBHOOK INTEGRATION
    # =====================================================================
    target_date = datetime.today().strftime('%Y-%m-%d')
    if not _candidate_discord_webhooks():
        print("Discord webhook not configured — skipping notification. Set DISCORD_MLB_WEBHOOK to enable.")
        return live

    def _post_pick_table_batches(df, title):
        if df is None or df.empty:
            return True

        table_rows = []
        for _, row in df.iterrows():
            pct = f"{row['hr_probability'] * 100:.1f}%"
            edge = f"{row.get('edge_pct', 0):+.0f}%" if pd.notna(row.get('edge_pct')) else 'N/A'
            ev_str = f"{row.get('ev_pct', 0):+.1f}%" if pd.notna(row.get('ev_pct', None)) else 'N/A'
            kelly = f"{float(row.get('kelly_fraction', 0) or 0):.3f}" if pd.notna(row.get('kelly_fraction')) else 'N/A'
            gtime = str(row.get('game_time', '')).strip()[:8]
            win = str(row.get('start_window', 'Later'))[:10]
            
            # IMPROVEMENT #10: Add confidence emoji icon
            confidence = str(row.get('model_reliability', 'MEDIUM')).upper()
            conf_emoji = '🔴' if confidence == 'HIGH' else '🟡' if confidence == 'MEDIUM' else '🟢'
            
            table_rows.append(
                f"| {str(row['batter_name'])[:12]:<12} | {str(row['pitcher_name'])[:12]:<12} | {gtime:<8} | {win:<10} | {pct:<6} | {conf_emoji} | {edge:<7} | {ev_str:<7} | {kelly:<6} |"
            )

        chunks = [
            table_rows[i:i + discord_rows_per_message]
            for i in range(0, len(table_rows), discord_rows_per_message)
        ]

        for idx, chunk_rows in enumerate(chunks, start=1):
            part_suffix = f" (Part {idx}/{len(chunks)})" if len(chunks) > 1 else ""
            table_str = "\n".join(chunk_rows)
            message_content = (
                f"**{title} ({target_date}){part_suffix}**\n"
                "```\n"
                f"| {'Batter':<12} | {'Pitcher':<12} | {'Time ET':<8} | {'Window':<10} | {'Prob':<6} | Conf | {'Edge':<7} | {'EV%':<7} | {'Kelly':<6} |\n"
                f"|{'-'*14}|{'-'*14}|{'-'*10}|{'-'*12}|{'-'*8}|{'-'*6}|{'-'*9}|{'-'*9}|{'-'*8}|\n"
                f"{table_str}\n"
                "```\n"
                "Legend: 🔴=HIGH confidence  🟡=MEDIUM confidence  🟢=LOW confidence"
            )
            if not send_discord_webhook(content=message_content):
                return False

        return True

    summary_lines = [
        f"\u26be MLB HR MODEL SNAPSHOT ({target_date})",
        f"Candidates ranked: {len(rankings)}",
        f"Most Likely Homers: {len(top_prob)} candidates ≥{discord_min_prob*100:.0f}% confidence",
        f"Delivered radar picks: {len(radar)}",
        f"Delivered +EV picks: {len(top_ev)}",
        f"Time windows: <= {discord_window_1_hours}h, <= {discord_window_2_hours}h, later",
    ]
    if not send_discord_webhook(content="\n".join(summary_lines)):
        print("Failed to transmit Discord summary after trying configured candidates.")

    if top_prob.empty:
        print("No predictions available to send to Discord.")
        return live

    sent_prob = _post_pick_table_batches(top_prob, f"⚾ Most Likely Homers ({len(top_prob)} candidates ≥{discord_min_prob*100:.0f}%)")
    sent_ev = True
    sent_radar = _post_pick_table_batches(radar, f"\U0001f535 HR Radar Picks (Physics Movers & Sleepers) ({len(radar)})")
    if not top_ev.empty:
        sent_ev = _post_pick_table_batches(top_ev, f"\u2705 +EV HR Picks (Top {len(top_ev)})")

    if not sent_prob or not sent_ev or not sent_radar:
        print("Failed to transmit one or more Discord pick tables after trying configured candidates.")

    return live

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
    and return (model_prob, was_predicted, was_most_likely_homer, model_rank) for Discord annotation."""
    today_str = datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{today_str}.csv'

    model_prob = None
    batter_id = None
    pitcher_id = None
    was_predicted = False
    was_most_likely_homer = False
    model_rank = None

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
                # Most Likely Homers: all batters model thinks have >= 12% chance (not fixed top 5)
                most_likely_threshold = 0.12
                most_likely_names = preds[preds['pred_hr_prob'] >= most_likely_threshold]['batter_name'].str.lower().str.strip().tolist()
                was_most_likely_homer = batter_name.lower().strip() in most_likely_names
                model_rank = int((preds['pred_hr_prob'] > model_prob).sum() + 1)
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
        'was_most_likely_homer': was_most_likely_homer,
        'actual_hr': 1
    }

    feedback_file = Path('data') / f'live_feedback_{today_str}.csv'
    Path('data').mkdir(parents=True, exist_ok=True)
    fb_df = pd.DataFrame([feedback_row])
    if feedback_file.exists():
        fb_df.to_csv(feedback_file, mode='a', header=False, index=False)
    else:
        fb_df.to_csv(feedback_file, index=False)

    return model_prob, was_predicted, was_most_likely_homer, model_rank


def _live_hr_processed_path(date_str=None):
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    return Path('data') / f'live_hr_processed_{date_str}.json'


def load_processed_home_run_events(date_str=None):
    path = _live_hr_processed_path(date_str)
    if not path.exists():
        return set()
    try:
        payload = _json.loads(path.read_text(encoding='utf-8'))
        if isinstance(payload, list):
            return set(str(x) for x in payload)
    except Exception:
        pass
    return set()


def save_processed_home_run_events(processed_ids, date_str=None):
    path = _live_hr_processed_path(date_str)
    Path('data').mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(_json.dumps(sorted(str(x) for x in processed_ids), indent=2), encoding='utf-8')
    except Exception:
        pass


def _normalize_half_inning_label(value):
    text = str(value or '').strip().lower()
    if text.startswith('t'):
        return 'top'
    if text.startswith('b'):
        return 'bottom'
    return text or 'unknown'


def _build_fallback_hr_event_id(game_id, inning, half_inning, at_bat_idx, batter_id, pitcher_id):
    return f"{game_id}:{inning}:{_normalize_half_inning_label(half_inning)}:{at_bat_idx}:{batter_id}:{pitcher_id}:HR"


def _build_fallback_hr_event_id_from_statcast_row(row):
    game_id = _safe_int(row.get('game_pk'))
    inning = _safe_int(row.get('inning'))
    half_inning = row.get('inning_topbot') or row.get('half_inning') or row.get('inning_half') or ''
    at_bat_idx = _safe_int(row.get('at_bat_number')) or _safe_int(row.get('atBatIndex')) or ''
    batter_id = _safe_int(row.get('batter'))
    pitcher_id = _safe_int(row.get('pitcher'))
    if game_id is None or inning is None or batter_id is None or pitcher_id is None:
        return None
    return _build_fallback_hr_event_id(game_id, inning, half_inning, at_bat_idx, batter_id, pitcher_id)


def backfill_unprocessed_today_home_runs(processed_home_runs, webhook_url):
    """Reconcile today's Statcast HR feed against processed IDs and send any missed alerts."""
    today_str = datetime.today().strftime('%Y-%m-%d')
    try:
        sc = load_or_fetch_statcast(today_str)
    except Exception as e:
        print(f"Live HR backfill skipped: {e}")
        return 0

    if sc is None or sc.empty or 'events' not in sc.columns:
        return 0

    hr_rows = sc[sc['events'] == 'home_run'].copy()
    if hr_rows.empty:
        return 0

    sent_count = 0
    hr_rows['fallback_event_id'] = hr_rows.apply(_build_fallback_hr_event_id_from_statcast_row, axis=1)
    hr_rows = hr_rows[hr_rows['fallback_event_id'].notna()].copy()

    for _, row in hr_rows.iterrows():
        fallback_event_id = str(row.get('fallback_event_id'))
        if fallback_event_id in processed_home_runs:
            continue

        batter_name = str(row.get('batter_name') or row.get('player_name') or row.get('batter') or 'Unknown Batter')
        pitcher_name = str(row.get('pitcher_name') or row.get('pitcher') or 'Unknown Pitcher')
        inning_half = str(row.get('inning_topbot') or row.get('half_inning') or row.get('inning_half') or '')
        num_inning = _safe_int(row.get('inning')) or 0
        game_id = _safe_int(row.get('game_pk'))

        # Reuse learning feedback so the catch-up event is indistinguishable from live detection.
        model_prob, was_predicted, was_most_likely_homer, model_rank = log_live_hr_feedback(
            batter_name, pitcher_name, game_id, inning_half, num_inning
        )

        alert_ts = datetime.now().strftime('%Y-%m-%d %I:%M:%S %p ET')
        message_lines = [
            "🚨 **LIVE HOME RUN ALERT** 🚨",
            f"⏰ Time: {alert_ts}",
            f"🏟️ *{row.get('home_team','Home')} @ {row.get('away_team','Away')}* ({inning_half} {num_inning})",
            "⚾ Backfill reconciliation caught a missed HR event",
        ]
        if batter_name:
            message_lines.append(f"👤 Batter: {batter_name}")
        if pitcher_name:
            message_lines.append(f"🎯 Pitcher: {pitcher_name}")
        if model_prob is not None:
            prob_str = f"{model_prob * 100:.1f}%"
            if was_most_likely_homer:
                message_lines.append(f"✅ **Model called it!** (Prob: {prob_str}) — Most Likely Homer")
            elif model_rank is not None:
                message_lines.append(f"📊 Model rank: #{model_rank} (Prob: {prob_str})")
            elif was_predicted:
                message_lines.append(f"✅ Model signaled HR risk (Prob: {prob_str})")
            else:
                message_lines.append(f"⚠️ Model missed (had: {prob_str}) — logged for retraining")

        sent = send_discord_webhook(content="\n".join(message_lines), webhook_url=webhook_url, async_send=False)
        if sent:
            processed_home_runs.add(fallback_event_id)
            processed_home_runs.add(f"{game_id}:{row.get('inning')}:{_normalize_half_inning_label(inning_half)}:{row.get('at_bat_number') or row.get('atBatIndex') or ''}:{_safe_int(row.get('batter'))}:{_safe_int(row.get('pitcher'))}:HR")
            sent_count += 1
            print(f"✅ Backfill HR alert CONFIRMED for {batter_name} vs {pitcher_name} (play {fallback_event_id})")
        else:
            print(f"⚠️ Backfill failed to send HR alert for {batter_name} vs {pitcher_name} (play {fallback_event_id})")

    if sent_count > 0:
        save_processed_home_run_events(processed_home_runs)
    return sent_count


def write_live_monitor_status(status):
    path = Path('data') / 'live_monitor_status.json'
    Path('data').mkdir(parents=True, exist_ok=True)
    safe_status = dict(status or {})
    safe_status['updated_at'] = datetime.now().isoformat()
    try:
        path.write_text(_json.dumps(safe_status, indent=2), encoding='utf-8')
    except Exception:
        pass


def _safe_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def _normalize_player_name(name):
    return str(name or '').strip().lower()


def _best_line_from_book_map(book_map):
    """Return (best_book, best_american) using max decimal payout logic."""
    best_book = None
    best_american = None
    best_decimal = -1.0
    for bk, odds in (book_map or {}).items():
        o = _safe_float(odds)
        if o is None:
            continue
        dec = (1 + (o / 100.0)) if o > 0 else (1 + (100.0 / abs(o)))
        if dec > best_decimal:
            best_decimal = dec
            best_american = int(round(o))
            best_book = str(bk)
    return best_book, best_american


def load_live_power_profile(date_str=None):
    """Load today's predictions into a power profile index keyed by batter id and name."""
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{date_str}.csv'
    if not pred_file.exists():
        return {'by_id': {}, 'by_name': {}}

    try:
        preds = pd.read_csv(pred_file)
    except Exception:
        return {'by_id': {}, 'by_name': {}}

    by_id = {}
    by_name = {}
    for _, row in preds.iterrows():
        batter_id = _safe_int(row.get('batter'))
        batter_name = str(row.get('batter_name', '')).strip()
        profile = {
            'batter_id': batter_id,
            'batter_name': batter_name,
            'pred_hr_prob': _safe_float(row.get('pred_hr_prob'), 0.0) or 0.0,
            'hard_hit_rate': _safe_float(row.get('bat_15pa_hard_hit_rate'), 0.0) or 0.0,
            'avg_ev': _safe_float(row.get('bat_avg_exit_velocity'), 0.0) or 0.0,
        }
        if batter_id is not None:
            by_id[batter_id] = profile
        if batter_name:
            by_name[_normalize_player_name(batter_name)] = profile

    return {'by_id': by_id, 'by_name': by_name}


def build_pitch_count_fastball_tendency_lookup(days_back=45, min_sample=25):
    """Build pitcher count-specific four-seam tendency lookup from cached Statcast files."""
    cache_dir = Path('cache')
    if not cache_dir.exists():
        return {}

    file_paths = []
    today = datetime.today().date()
    for i in range(1, max(2, int(days_back)) + 1):
        ds = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        fp = cache_dir / f'statcast_{ds}.csv'
        if fp.exists():
            file_paths.append(fp)

    if not file_paths:
        return {}

    parts = []
    for fp in file_paths:
        try:
            df = pd.read_csv(fp, usecols=['pitcher', 'balls', 'strikes', 'pitch_type'])
            parts.append(df)
        except Exception:
            continue

    if not parts:
        return {}

    pitches = pd.concat(parts, ignore_index=True)
    pitches['pitcher'] = pd.to_numeric(pitches.get('pitcher'), errors='coerce')
    pitches['balls'] = pd.to_numeric(pitches.get('balls'), errors='coerce')
    pitches['strikes'] = pd.to_numeric(pitches.get('strikes'), errors='coerce')
    pitches = pitches.dropna(subset=['pitcher', 'balls', 'strikes', 'pitch_type']).copy()
    if pitches.empty:
        return {}

    pitches['pitcher'] = pitches['pitcher'].astype(int)
    pitches['count_key'] = pitches['balls'].astype(int).astype(str) + '-' + pitches['strikes'].astype(int).astype(str)
    pitches = pitches[pitches['count_key'].isin({'2-0', '3-1'})].copy()
    if pitches.empty:
        return {}

    pitches['is_four_seam'] = pitches['pitch_type'].astype(str).isin({'FF', 'FA'}).astype(int)
    agg = pitches.groupby(['pitcher', 'count_key'], as_index=False).agg(
        four_seam_rate=('is_four_seam', 'mean'),
        sample_size=('is_four_seam', 'count'),
    )
    agg = agg[agg['sample_size'] >= max(1, int(min_sample))]

    lookup = {}
    for _, row in agg.iterrows():
        lookup[(int(row['pitcher']), str(row['count_key']))] = {
            'four_seam_rate': float(row['four_seam_rate']),
            'sample_size': int(row['sample_size'])
        }
    return lookup


def _get_next_hitters_live(play_by_play, half_inning, current_batter_id, lookahead_slots=5):
    """Return upcoming batter candidates from live boxscore batting order."""
    live_data = (play_by_play or {}).get('liveData', {})
    boxscore = live_data.get('boxscore', {})
    teams = boxscore.get('teams', {})
    offense_key = 'away' if str(half_inning).lower() == 'top' else 'home'
    team_box = teams.get(offense_key, {})
    batting_order = team_box.get('battingOrder', []) or []
    players = team_box.get('players', {}) or {}

    ids = [_safe_int(x) for x in batting_order if _safe_int(x) is not None]
    if not ids:
        return []

    current_batter_id = _safe_int(current_batter_id)
    if current_batter_id is None or current_batter_id not in ids:
        start_idx = 0
    else:
        start_idx = ids.index(current_batter_id) + 1

    out = []
    for step in range(max(1, int(lookahead_slots))):
        bid = ids[(start_idx + step) % len(ids)]
        p = players.get(f'ID{bid}', {})
        full_name = str((p.get('person') or {}).get('fullName', '')).strip()
        if full_name:
            out.append({'batter_id': bid, 'batter_name': full_name})
    return out


def _rank_next_power_hitters(candidates, power_profile, top_n=3):
    """Rank upcoming hitters by today's HR model probability."""
    scored = []
    by_id = power_profile.get('by_id', {})
    by_name = power_profile.get('by_name', {})

    for c in candidates or []:
        bid = _safe_int(c.get('batter_id'))
        bname = str(c.get('batter_name', '')).strip()
        prof = by_id.get(bid) or by_name.get(_normalize_player_name(bname)) or {}
        scored.append({
            'batter_id': bid,
            'batter_name': bname,
            'pred_hr_prob': float(prof.get('pred_hr_prob', 0.0)),
            'hard_hit_rate': float(prof.get('hard_hit_rate', 0.0)),
            'avg_ev': float(prof.get('avg_ev', 0.0)),
        })

    scored.sort(key=lambda x: x.get('pred_hr_prob', 0.0), reverse=True)
    return scored[:max(1, int(top_n))]


def _pitcher_recent_release_series(all_plays, pitcher_id):
    """Extract chronological releasePosZ values for one pitcher from live game plays."""
    pitcher_id = _safe_int(pitcher_id)
    if pitcher_id is None:
        return []

    series = []
    for play in all_plays or []:
        pid = _safe_int(((play.get('matchup') or {}).get('pitcher') or {}).get('id'))
        if pid != pitcher_id:
            continue
        for pe in play.get('playEvents', []) or []:
            if not pe.get('isPitch', False):
                continue
            release_z = _safe_float((((pe.get('pitchData') or {}).get('coordinates') or {}).get('releasePosZ')))
            if release_z is not None:
                series.append(release_z)
    return series


def _recent_hard_hit_lineout_streak(all_plays, pitcher_id):
    """Count consecutive hard-hit lineouts allowed by pitcher (most recent backward)."""
    pitcher_id = _safe_int(pitcher_id)
    if pitcher_id is None:
        return 0

    streak = 0
    for play in reversed(all_plays or []):
        pid = _safe_int(((play.get('matchup') or {}).get('pitcher') or {}).get('id'))
        if pid != pitcher_id:
            continue

        result = play.get('result', {}) or {}
        event = str(result.get('event', '')).lower()
        event_type = str(result.get('eventType', '')).lower()
        is_lineout = ('lineout' in event) or ('lineout' in event_type)
        if not is_lineout:
            break

        launch_speed = None
        for pe in reversed(play.get('playEvents', []) or []):
            ls = _safe_float(((pe.get('hitData') or {}).get('launchSpeed')))
            if ls is not None:
                launch_speed = ls
                break

        if launch_speed is not None and launch_speed >= 95.0:
            streak += 1
            continue
        break

    return streak


def _extract_best_live_odds(current_odds_raw):
    """Convert raw odds map to normalized best-line map keyed by normalized player name."""
    out = {}
    for player_name, book_map in (current_odds_raw or {}).items():
        best_book, best_american = _best_line_from_book_map(book_map)
        if best_american is None:
            continue
        out[_normalize_player_name(player_name)] = {
            'player_name': str(player_name),
            'best_book': best_book,
            'best_american': int(best_american)
        }
    return out


def _is_risp_no_outs(play):
    matchup = (play or {}).get('matchup', {}) or {}
    count = (play or {}).get('count', {}) or {}
    outs = _safe_int(count.get('outs'), 0) or 0
    has_risp = bool(matchup.get('postOnSecond') or matchup.get('postOnThird'))
    return has_risp and outs == 0


def _detect_release_axis_tilt_signal(game, play_by_play, power_profile, seen_keys):
    """Detect release height drop over recent pitch window and emit one alert per pitcher-game."""
    alerts = []
    threshold_inches = _safe_float(os.getenv('LIVE_RELEASE_DROP_INCHES', '2.5'), 2.5) or 2.5
    threshold_ft = threshold_inches / 12.0
    window = max(6, _safe_int(os.getenv('LIVE_RELEASE_WINDOW_PITCHES', '10'), 10) or 10)
    next_hitters_n = max(1, _safe_int(os.getenv('LIVE_RELEASE_NEXT_HITTERS', '3'), 3) or 3)
    lookahead_slots = max(next_hitters_n, _safe_int(os.getenv('LIVE_RELEASE_LOOKAHEAD_SLOTS', '5'), 5) or 5)

    all_plays = ((play_by_play or {}).get('liveData', {}).get('plays', {}).get('allPlays', []) or [])
    if not all_plays:
        return alerts

    last_play = all_plays[-1]
    matchup = last_play.get('matchup', {}) or {}
    pitcher = matchup.get('pitcher', {}) or {}
    pitcher_id = _safe_int(pitcher.get('id'))
    pitcher_name = str(pitcher.get('fullName', 'Unknown Pitcher'))
    if pitcher_id is None:
        return alerts

    rz = _pitcher_recent_release_series(all_plays, pitcher_id)
    if len(rz) < window:
        return alerts

    tail = rz[-window:]
    first_half = tail[:window // 2]
    second_half = tail[window // 2:]
    if not first_half or not second_half:
        return alerts

    drop_ft = (sum(first_half) / len(first_half)) - (sum(second_half) / len(second_half))
    if drop_ft <= threshold_ft:
        return alerts

    game_id = game.get('game_pk') or game.get('game_id')
    key = f"release:{game_id}:{pitcher_id}"
    if key in seen_keys:
        return alerts
    seen_keys.add(key)

    half_inning = (last_play.get('about', {}) or {}).get('halfInning', '')
    current_batter_id = ((matchup.get('batter') or {}).get('id'))
    candidates = _get_next_hitters_live(play_by_play, half_inning, current_batter_id, lookahead_slots=lookahead_slots)
    ranked = _rank_next_power_hitters(candidates, power_profile, top_n=next_hitters_n)

    alerts.append({
        'type': 'release_axis_tilt',
        'game_id': game_id,
        'pitcher_id': pitcher_id,
        'pitcher_name': pitcher_name,
        'drop_inches': drop_ft * 12.0,
        'window': window,
        'targets': ranked,
        'game_display': f"{game.get('away_name','Away')} @ {game.get('home_name','Home')}",
    })
    return alerts


def _detect_predictable_count_signal(game, play_by_play, power_profile, tendency_lookup, seen_keys):
    """Detect 2-0 / 3-1 predictable count four-seam tendency windows."""
    alerts = []
    rate_threshold = _safe_float(os.getenv('LIVE_COUNT_FASTBALL_RATE', '0.82'), 0.82) or 0.82
    lookahead_slots = max(3, _safe_int(os.getenv('LIVE_COUNT_LOOKAHEAD_SLOTS', '5'), 5) or 5)
    next_hitters_n = max(1, _safe_int(os.getenv('LIVE_COUNT_NEXT_HITTERS', '2'), 2) or 2)

    all_plays = ((play_by_play or {}).get('liveData', {}).get('plays', {}).get('allPlays', []) or [])
    if not all_plays:
        return alerts

    # Evaluate only recent plays to keep loop lightweight.
    for play in all_plays[-4:]:
        matchup = play.get('matchup', {}) or {}
        pitcher = matchup.get('pitcher', {}) or {}
        batter = matchup.get('batter', {}) or {}
        pitcher_id = _safe_int(pitcher.get('id'))
        batter_id = _safe_int(batter.get('id'))
        pitcher_name = str(pitcher.get('fullName', 'Unknown Pitcher'))
        batter_name = str(batter.get('fullName', 'Unknown Batter'))
        if pitcher_id is None:
            continue

        for pe in play.get('playEvents', []) or []:
            if not pe.get('isPitch', False):
                continue
            count = pe.get('count', {}) or {}
            balls = _safe_int(count.get('balls'))
            strikes = _safe_int(count.get('strikes'))
            if balls is None or strikes is None:
                continue
            count_key = f"{balls}-{strikes}"
            if count_key not in {'2-0', '3-1'}:
                continue

            tendency = tendency_lookup.get((pitcher_id, count_key))
            if not tendency:
                continue
            fs_rate = float(tendency.get('four_seam_rate', 0.0))
            if fs_rate < rate_threshold:
                continue
            if not _is_risp_no_outs(play):
                continue

            game_id = game.get('game_pk') or game.get('game_id')
            about = play.get('about', {}) or {}
            at_bat_idx = about.get('atBatIndex', '')
            key = f"count:{game_id}:{pitcher_id}:{batter_id}:{count_key}:{at_bat_idx}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            half_inning = about.get('halfInning', '')
            candidates = _get_next_hitters_live(play_by_play, half_inning, batter_id, lookahead_slots=lookahead_slots)
            ranked = _rank_next_power_hitters(candidates, power_profile, top_n=next_hitters_n)

            alerts.append({
                'type': 'predictable_count',
                'game_id': game_id,
                'pitcher_id': pitcher_id,
                'pitcher_name': pitcher_name,
                'batter_name': batter_name,
                'count_key': count_key,
                'four_seam_rate': fs_rate,
                'sample_size': int(tendency.get('sample_size', 0)),
                'targets': ranked,
                'game_display': f"{game.get('away_name','Away')} @ {game.get('home_name','Home')}",
            })

    return alerts


def _detect_live_odds_inversion_signal(game, play_by_play, power_profile, best_odds_now, best_odds_prev, seen_keys):
    """Detect rapid in-play odds spikes after hard-hit lineout clusters."""
    alerts = []
    if not best_odds_now or not best_odds_prev:
        return alerts

    min_jump = _safe_int(os.getenv('LIVE_ODDS_SPIKE_MIN_JUMP', '200'), 200) or 200
    from_max = _safe_int(os.getenv('LIVE_ODDS_SPIKE_FROM_MAX', '500'), 500) or 500
    to_min = _safe_int(os.getenv('LIVE_ODDS_SPIKE_TO_MIN', '650'), 650) or 650
    next_hitters_n = max(1, _safe_int(os.getenv('LIVE_ODDS_NEXT_HITTERS', '3'), 3) or 3)
    lookahead_slots = max(next_hitters_n, _safe_int(os.getenv('LIVE_ODDS_LOOKAHEAD_SLOTS', '5'), 5) or 5)

    all_plays = ((play_by_play or {}).get('liveData', {}).get('plays', {}).get('allPlays', []) or [])
    if not all_plays:
        return alerts

    last_play = all_plays[-1]
    matchup = last_play.get('matchup', {}) or {}
    pitcher = matchup.get('pitcher', {}) or {}
    pitcher_id = _safe_int(pitcher.get('id'))
    pitcher_name = str(pitcher.get('fullName', 'Unknown Pitcher'))
    if pitcher_id is None:
        return alerts

    streak = _recent_hard_hit_lineout_streak(all_plays, pitcher_id)
    if streak < 2:
        return alerts

    half_inning = (last_play.get('about', {}) or {}).get('halfInning', '')
    current_batter_id = ((matchup.get('batter') or {}).get('id'))
    candidates = _get_next_hitters_live(play_by_play, half_inning, current_batter_id, lookahead_slots=lookahead_slots)
    ranked = _rank_next_power_hitters(candidates, power_profile, top_n=next_hitters_n)

    game_id = game.get('game_pk') or game.get('game_id')
    by_name = power_profile.get('by_name', {})
    for hitter in ranked:
        hname = str(hitter.get('batter_name', ''))
        norm = _normalize_player_name(hname)
        now_obj = best_odds_now.get(norm)
        prev_obj = best_odds_prev.get(norm)
        if not now_obj or not prev_obj:
            continue

        now_odds = _safe_int(now_obj.get('best_american'))
        prev_odds = _safe_int(prev_obj.get('best_american'))
        if now_odds is None or prev_odds is None:
            continue
        if prev_odds <= 0 or now_odds <= 0:
            continue
        if not (prev_odds <= from_max and now_odds >= to_min and (now_odds - prev_odds) >= min_jump):
            continue

        prof = by_name.get(norm, {})
        hh_rate = float(prof.get('hard_hit_rate', hitter.get('hard_hit_rate', 0.0)) or 0.0)
        avg_ev = float(prof.get('avg_ev', hitter.get('avg_ev', 0.0)) or 0.0)
        if hh_rate < 0.35 and avg_ev < 92.0:
            continue

        key = f"odds:{game_id}:{pitcher_id}:{norm}:{prev_odds}:{now_odds}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        alerts.append({
            'type': 'odds_inversion',
            'game_id': game_id,
            'pitcher_id': pitcher_id,
            'pitcher_name': pitcher_name,
            'batter_name': hname,
            'prev_odds': prev_odds,
            'now_odds': now_odds,
            'jump': now_odds - prev_odds,
            'hard_hit_lineout_streak': streak,
            'best_book': str(now_obj.get('best_book') or ''),
            'game_display': f"{game.get('away_name','Away')} @ {game.get('home_name','Home')}",
        })

    return alerts


def _build_live_reprice_predictions(game, play_by_play, power_profile, best_odds_now=None, best_odds_prev=None):
    """Build fresh live HR predictions when pitcher quality appears to be declining.

    This is a re-pricing layer, not a new training run: it adjusts today's baseline
    batter probabilities using live pitcher degradation signals and returns the next
    hitters most likely to benefit.
    """
    all_plays = ((play_by_play or {}).get('liveData', {}).get('plays', {}).get('allPlays', []) or [])
    if not all_plays:
        return None

    last_play = all_plays[-1]
    matchup = last_play.get('matchup', {}) or {}
    pitcher = matchup.get('pitcher', {}) or {}
    pitcher_id = _safe_int(pitcher.get('id'))
    pitcher_name = str(pitcher.get('fullName', 'Unknown Pitcher'))
    if pitcher_id is None:
        return None

    release_series = _pitcher_recent_release_series(all_plays, pitcher_id)
    if len(release_series) < 8:
        return None

    window = min(len(release_series), max(8, _safe_int(os.getenv('LIVE_REPRICE_WINDOW_PITCHES', '10'), 10) or 10))
    tail = release_series[-window:]
    half = max(1, window // 2)
    first_avg = sum(tail[:half]) / len(tail[:half])
    second_avg = sum(tail[half:]) / len(tail[half:]) if tail[half:] else first_avg
    drop_inches = max(0.0, (first_avg - second_avg) * 12.0)
    hard_hit_streak = _recent_hard_hit_lineout_streak(all_plays, pitcher_id)

    # Only reprice when we have a tangible live deterioration signal.
    if drop_inches < _safe_float(os.getenv('LIVE_REPRICE_MIN_DROP_INCHES', '1.5'), 1.5):
        return None

    half_inning = (last_play.get('about', {}) or {}).get('halfInning', '')
    current_batter_id = ((matchup.get('batter') or {}).get('id'))
    lookahead_slots = max(4, _safe_int(os.getenv('LIVE_REPRICE_LOOKAHEAD_SLOTS', '6'), 6) or 6)
    target_n = max(3, _safe_int(os.getenv('LIVE_REPRICE_TARGETS', '4'), 4) or 4)
    candidates = _get_next_hitters_live(play_by_play, half_inning, current_batter_id, lookahead_slots=lookahead_slots)
    ranked = _rank_next_power_hitters(candidates, power_profile, top_n=target_n)
    if not ranked:
        return None

    # Convert decline into a conservative uplift factor for live probabilities.
    decline_factor = 1.0
    decline_factor += min(0.18, drop_inches / 20.0)
    decline_factor += min(0.10, hard_hit_streak * 0.03)
    decline_factor = min(1.30, max(1.0, decline_factor))

    game_id = game.get('game_pk') or game.get('game_id')
    game_display = f"{game.get('away_name','Away')} @ {game.get('home_name','Home')}"
    reprice_rows = []
    for row in ranked:
        base_prob = float(row.get('pred_hr_prob', 0.0))
        live_prob = min(0.95, base_prob * decline_factor)
        best_odds = None
        best_book = None
        odds_momentum = 0.0
        if best_odds_now:
            norm = _normalize_player_name(row.get('batter_name', ''))
            odds_obj = best_odds_now.get(norm)
            if odds_obj:
                best_odds = odds_obj.get('best_american')
                best_book = odds_obj.get('best_book')
            if best_odds_prev:
                prev_obj = best_odds_prev.get(norm)
                if prev_obj and prev_obj.get('best_american') is not None and best_odds is not None:
                    prev_odds = _safe_float(prev_obj.get('best_american'))
                    now_odds = _safe_float(best_odds)
                    if prev_odds is not None and now_odds is not None:
                        odds_momentum = max(0.0, (now_odds - prev_odds) / 1000.0)

        live_prob = min(0.97, live_prob + min(0.08, odds_momentum))

        reprice_rows.append({
            'batter_name': row.get('batter_name', ''),
            'base_prob': base_prob,
            'live_prob': live_prob,
            'delta_prob': live_prob - base_prob,
            'odds_momentum': odds_momentum,
            'best_book': best_book or '',
            'best_market_odds_american': best_odds,
            'hard_hit_rate': row.get('hard_hit_rate', 0.0),
            'avg_ev': row.get('avg_ev', 0.0),
        })

    reprice_rows.sort(key=lambda x: (x['live_prob'], x['odds_momentum'], x['delta_prob']), reverse=True)
    return {
        'type': 'live_reprice',
        'game_id': game_id,
        'game_display': game_display,
        'pitcher_id': pitcher_id,
        'pitcher_name': pitcher_name,
        'drop_inches': drop_inches,
        'hard_hit_streak': hard_hit_streak,
        'decline_factor': decline_factor,
        'targets': reprice_rows[:target_n],
    }


def save_live_reprice_snapshot(reprice_alert, date_str=None):
    """Persist live repriced HR candidates for later review."""
    if not reprice_alert:
        return None

    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    out_path = Path('data') / f'live_reprice_predictions_{date_str}.csv'
    Path('data').mkdir(parents=True, exist_ok=True)

    rows = []
    for target in reprice_alert.get('targets') or []:
        rows.append({
            'timestamp': datetime.now().isoformat(),
            'game_id': reprice_alert.get('game_id', ''),
            'game_display': reprice_alert.get('game_display', ''),
            'pitcher_name': reprice_alert.get('pitcher_name', ''),
            'pitcher_id': reprice_alert.get('pitcher_id', ''),
            'drop_inches': reprice_alert.get('drop_inches', ''),
            'hard_hit_streak': reprice_alert.get('hard_hit_streak', 0),
            'decline_factor': reprice_alert.get('decline_factor', 1.0),
            'batter_name': target.get('batter_name', ''),
            'base_prob': target.get('base_prob', 0.0),
            'live_prob': target.get('live_prob', 0.0),
            'delta_prob': target.get('delta_prob', 0.0),
            'odds_momentum': target.get('odds_momentum', 0.0),
            'best_book': target.get('best_book', ''),
            'best_market_odds_american': target.get('best_market_odds_american', ''),
            'hard_hit_rate': target.get('hard_hit_rate', 0.0),
            'avg_ev': target.get('avg_ev', 0.0),
        })

    if not rows:
        return None

    df = pd.DataFrame(rows)
    if out_path.exists():
        df.to_csv(out_path, mode='a', header=False, index=False)
    else:
        df.to_csv(out_path, index=False)
    return out_path


def _format_micro_signal_message(alert):
    ts = datetime.now().strftime('%Y-%m-%d %I:%M:%S %p ET')
    atype = alert.get('type')
    game_display = alert.get('game_display', 'Game in progress')

    if atype == 'release_axis_tilt':
        lines = [
            "⚡ **MICRO-SIGNAL: RELEASE AXIS TILT**",
            f"⏰ Time: {ts}",
            f"🏟️ {game_display}",
            f"🎯 Pitcher: {alert.get('pitcher_name', 'Unknown')}",
            f"📉 Release drop: {float(alert.get('drop_inches', 0.0)):.2f} in over last {int(alert.get('window', 10))} pitches",
        ]
        targets = alert.get('targets') or []
        if targets:
            lines.append("🔥 Next power hitters:")
            for t in targets:
                lines.append(f"• {t.get('batter_name','Unknown')} ({float(t.get('pred_hr_prob', 0.0))*100:.1f}% HR)")
        return "\n".join(lines)

    if atype == 'odds_inversion':
        return "\n".join([
            "⚡ **MICRO-SIGNAL: LIVE ODDS INVERSION**",
            f"⏰ Time: {ts}",
            f"🏟️ {game_display}",
            f"🎯 Pitcher stress: {alert.get('pitcher_name', 'Unknown')} ({int(alert.get('hard_hit_lineout_streak', 0))} hard-hit lineouts)",
            f"👤 Batter: {alert.get('batter_name', 'Unknown')}",
            f"📈 Odds spike: {int(alert.get('prev_odds', 0)):+d} → {int(alert.get('now_odds', 0)):+d} (Δ {int(alert.get('jump', 0)):+d})",
            f"🏦 Book: {alert.get('best_book', 'N/A')}",
        ])

    if atype == 'predictable_count':
        lines = [
            "⚡ **MICRO-SIGNAL: PREDICTABLE COUNT WINDOW**",
            f"⏰ Time: {ts}",
            f"🏟️ {game_display}",
            f"🎯 Pitcher: {alert.get('pitcher_name', 'Unknown')}",
            f"🧮 Count: {alert.get('count_key', 'N/A')} (RISP, 0 outs)",
            f"📌 Four-seam tendency: {float(alert.get('four_seam_rate', 0.0))*100:.1f}% (n={int(alert.get('sample_size', 0))})",
            f"👤 Current batter: {alert.get('batter_name', 'Unknown')}",
        ]
        targets = alert.get('targets') or []
        if targets:
            lines.append("🔥 Next power hitters:")
            for t in targets:
                lines.append(f"• {t.get('batter_name','Unknown')} ({float(t.get('pred_hr_prob', 0.0))*100:.1f}% HR)")
        return "\n".join(lines)

    if atype == 'live_reprice':
        lines = [
            "⚡ **LIVE HR REPRICE**",
            f"⏰ Time: {ts}",
            f"🏟️ {game_display}",
            f"🎯 Pitcher: {alert.get('pitcher_name', 'Unknown')}",
            f"📉 Release drop: {float(alert.get('drop_inches', 0.0)):.2f} in",
            f"🧪 Decline factor: x{float(alert.get('decline_factor', 1.0)):.2f}",
        ]
        targets = alert.get('targets') or []
        if targets:
            lines.append("🔥 Repriced HR candidates:")
            for t in targets:
                live_prob = float(t.get('live_prob', 0.0)) * 100
                delta = float(t.get('delta_prob', 0.0)) * 100
                odds_text = ''
                if pd.notna(t.get('best_market_odds_american')) and t.get('best_market_odds_american') is not None:
                    odds_text = f" | {int(t.get('best_market_odds_american')):+d}"
                lines.append(
                    f"• {t.get('batter_name','Unknown')} -> {live_prob:.1f}% ({delta:+.1f} pts){odds_text}"
                )
        return "\n".join(lines)

    return ""


def monitor_live_home_runs():
    """Loop indefinitely, checking live game data for home runs and alerting Discord."""
    WEBHOOK_URL = os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_MLB_WEBHOOK or DISCORD_WEBHOOK_URL not set; configure env var or GitHub secret")

    if not _claim_live_monitor_pidfile():
        return

    current_pid = os.getpid()
    atexit.register(_release_live_monitor_pidfile, current_pid)

    try:
        print("🚀 Monitoring started: Waiting for live MLB home run events...")
        print(f"📡 Discord webhook: {WEBHOOK_URL[:30]}...{WEBHOOK_URL[-10:] if len(WEBHOOK_URL) > 40 else ''}")
        print("⚡ Micro-signal engine enabled: release-axis tilt, odds inversion, predictable count windows")
        processed_home_runs = load_processed_home_run_events()
        micro_alert_keys = set()

        power_profile = load_live_power_profile()
        tendency_lookup = build_pitch_count_fastball_tendency_lookup(
            days_back=max(14, _safe_int(os.getenv('LIVE_COUNT_LOOKBACK_DAYS', '45'), 45) or 45),
            min_sample=max(10, _safe_int(os.getenv('LIVE_COUNT_MIN_SAMPLE', '25'), 25) or 25),
        )
        if tendency_lookup:
            print(f"Loaded count tendency lookup: {len(tendency_lookup)} pitcher-count patterns")
        else:
            print("Count tendency lookup unavailable (insufficient cached pitch data).")

        odds_poll_seconds = max(3, _safe_int(os.getenv('LIVE_ODDS_POLL_SECONDS', '3'), 3) or 3)  # Reduced from 5s to 3s
        monitor_sleep_seconds = max(2, _safe_int(os.getenv('LIVE_MONITOR_POLL_SECONDS', '2'), 2) or 2)  # Reduced from 5s to 2s for faster HR detection
        heartbeat_enabled = str(os.getenv('LIVE_HEARTBEAT_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
        heartbeat_minutes = max(1, _safe_int(os.getenv('LIVE_HEARTBEAT_MINUTES', '10'), 10) or 10)
        heartbeat_every_seconds = heartbeat_minutes * 60
        backfill_every_seconds = max(30, _safe_int(os.getenv('LIVE_HR_BACKFILL_SECONDS', '60'), 60) or 60)
        last_heartbeat_ts = 0.0
        last_backfill_ts = 0.0
        last_odds_poll_ts = 0.0
        best_odds_prev = {}
        best_odds_now = {}

        # Catch up immediately on any HRs that occurred before the monitor started.
        backfill_sent = backfill_unprocessed_today_home_runs(processed_home_runs, WEBHOOK_URL)
        if backfill_sent:
            print(f"Backfill reconciliation sent {backfill_sent} missed HR alert(s) at startup")
        last_backfill_ts = time.time()
        _send_live_monitor_startup_report(
            WEBHOOK_URL,
            current_pid,
            processed_home_runs,
            backfill_sent,
            monitor_sleep_seconds,
            odds_poll_seconds,
        )

        while True:
            try:
                today_str = datetime.today().strftime('%m/%d/%Y')
                games = statsapi.schedule(date=today_str) or []
                in_progress_games = 0
                detected_this_loop = 0
                sent_this_loop = 0
                micro_signals_this_loop = 0

                now_ts = time.time()
                if (now_ts - last_backfill_ts) >= backfill_every_seconds:
                    try:
                        backfill_sent = backfill_unprocessed_today_home_runs(processed_home_runs, WEBHOOK_URL)
                        if backfill_sent:
                            print(f"Backfill reconciliation sent {backfill_sent} missed HR alert(s)")
                        last_backfill_ts = now_ts
                    except Exception as backfill_err:
                        print(f"Live HR backfill failed: {backfill_err}")

                if (now_ts - last_odds_poll_ts) >= odds_poll_seconds:
                    try:
                        odds_raw = fetch_hr_prop_odds_raw()
                        if odds_raw:
                            best_odds_prev = best_odds_now
                            best_odds_now = _extract_best_live_odds(odds_raw)
                        last_odds_poll_ts = now_ts
                    except Exception as odds_err:
                        print(f"Live odds poll failed: {odds_err}")

                for game in games:
                    status_parts = [
                        str(game.get('status', '') or ''),
                        str(game.get('detailed_state', '') or ''),
                        str(game.get('game_status', '') or ''),
                    ]
                    status_text = " ".join(status_parts).lower()
                    is_live_state = any(
                        token in status_text
                        for token in [
                            'in progress',
                            'manager challenge',
                            'review',
                            'warmup',
                            'delayed',
                            'live',
                        ]
                    )
                    if not is_live_state:
                        continue
                    in_progress_games += 1
                    game_id = game.get('game_pk') or game.get('game_id')
                    if not game_id:
                        continue
                    play_by_play = statsapi.get('game', {'gamePk': game_id}) or {}
                    all_plays = play_by_play.get('liveData', {}).get('plays', {}).get('allPlays', [])

                    # Fast-path HR scanner: check only recent + scoring plays first so Discord alerts fire quickly.
                    recent_play_window = max(4, _safe_int(os.getenv('LIVE_HR_RECENT_PLAY_WINDOW', '18'), 18) or 18)
                    recent_idx_start = max(0, len(all_plays) - recent_play_window)
                    candidate_play_indexes = set(range(recent_idx_start, len(all_plays)))
                    scoring_indexes = (
                        play_by_play.get('liveData', {})
                        .get('linescore', {})
                        .get('scoringPlays', [])
                        or []
                    )
                    for _idx in scoring_indexes:
                        try:
                            i = int(_idx)
                            if 0 <= i < len(all_plays):
                                candidate_play_indexes.add(i)
                        except Exception:
                            continue

                    for play_idx in sorted(candidate_play_indexes):
                        play = all_plays[play_idx]
                        result = play.get('result', {})
                        about = play.get('about', {})
                        matchup = play.get('matchup', {})
                        event_name = str(result.get('event', '')).lower().strip()
                        event_type = str(result.get('eventType', '')).lower().strip()
                        event_desc = str(result.get('description', '')).lower()

                        is_hr = any([
                            event_name in ('home run', 'home_run', 'homerun', 'hr'),
                            event_type in ('home_run', 'home run', 'homerun', 'hr'),
                            'home run' in event_name,
                            'home run' in event_type,
                            'home run' in event_desc,
                            'solo home run' in event_desc,
                            '2-run home run' in event_desc,
                            '3-run home run' in event_desc,
                            'grand slam' in event_desc,
                        ])
                        if not is_hr:
                            continue

                        event_id = about.get('playId')
                        fallback_event_id = _build_fallback_hr_event_id(
                            game_id,
                            about.get('inning', ''),
                            about.get('halfInning', ''),
                            about.get('atBatIndex', ''),
                            matchup.get('batter', {}).get('id') or '',
                            matchup.get('pitcher', {}).get('id') or '',
                        )
                        if not event_id:
                            event_id = fallback_event_id

                        if event_id in processed_home_runs or fallback_event_id in processed_home_runs:
                            continue
                        detected_this_loop += 1

                        description = result.get('description', 'A home run was hit!')
                        inning_half = about.get('halfInning', '')
                        num_inning = about.get('inning', '')
                        batter_name = matchup.get('batter', {}).get('fullName') or ''
                        pitcher_name = matchup.get('pitcher', {}).get('fullName') or ''
                        game_display = f"{game.get('away_name','Away')} @ {game.get('home_name','Home')}"
                        alert_ts = datetime.now().strftime('%Y-%m-%d %I:%M:%S %p ET')

                        _model_prob, _was_predicted, _was_most_likely_homer, _model_rank = None, False, False, None
                        if batter_name:
                            try:
                                _model_prob, _was_predicted, _was_most_likely_homer, _model_rank = log_live_hr_feedback(
                                    batter_name, pitcher_name, game_id, inning_half, num_inning
                                )
                            except Exception:
                                pass

                        message_lines = [
                            "🚨 **LIVE HOME RUN ALERT** 🚨",
                            f"⏰ Time: {alert_ts}",
                            f"🏟️ *{game_display}* ({inning_half} {num_inning})",
                            f"⚾ {description}"
                        ]
                        if batter_name:
                            message_lines.append(f"👤 Batter: {batter_name}")
                        if pitcher_name:
                            message_lines.append(f"🎯 Pitcher: {pitcher_name}")
                        if _model_prob is not None:
                            prob_str = f"{_model_prob * 100:.1f}%"
                            if _was_most_likely_homer:
                                message_lines.append(f"✅ **Model called it!** (Prob: {prob_str}) — Most Likely Homer")
                            elif _model_rank is not None:
                                message_lines.append(
                                    f"📊 Model rank: #{_model_rank} (Prob: {prob_str}) — not in Most Likely Homers"
                                )
                            elif _was_predicted:
                                message_lines.append(f"✅ Model signaled HR risk (Prob: {prob_str})")
                            else:
                                message_lines.append(f"⚠️ Model missed (had: {prob_str}) — logged for retraining")
                        else:
                            message_lines.append("⚠️ Not in today's predictions — logged for retraining")

                        sent = send_discord_webhook(content="\n".join(message_lines), webhook_url=WEBHOOK_URL, async_send=False)
                        if sent:
                            print(f"✅ Live HR alert CONFIRMED for {batter_name} vs {pitcher_name} in {game_display} (play {event_id})")
                            processed_home_runs.add(event_id)
                            processed_home_runs.add(fallback_event_id)
                            save_processed_home_run_events(processed_home_runs)
                            sent_this_loop += 1
                        else:
                            print(f"⚠️  FAILED to send HR alert for {batter_name} vs {pitcher_name} (play {event_id}) — will retry next loop")

                    # Secondary micro-signal pass after immediate HR alert handling.
                    for alert in _detect_release_axis_tilt_signal(game, play_by_play, power_profile, micro_alert_keys):
                        msg = _format_micro_signal_message(alert)
                        if msg and send_discord_webhook(content=msg, webhook_url=WEBHOOK_URL):
                            micro_signals_this_loop += 1
                            sent_this_loop += 1
                            print(f"Micro signal sent: {alert.get('type')} ({alert.get('game_display','')})")

                    for alert in _detect_predictable_count_signal(game, play_by_play, power_profile, tendency_lookup, micro_alert_keys):
                        msg = _format_micro_signal_message(alert)
                        if msg and send_discord_webhook(content=msg, webhook_url=WEBHOOK_URL):
                            micro_signals_this_loop += 1
                            sent_this_loop += 1
                            print(f"Micro signal sent: {alert.get('type')} ({alert.get('game_display','')})")

                    for alert in _detect_live_odds_inversion_signal(
                        game,
                        play_by_play,
                        power_profile,
                        best_odds_now,
                        best_odds_prev,
                        micro_alert_keys,
                    ):
                        msg = _format_micro_signal_message(alert)
                        if msg and send_discord_webhook(content=msg, webhook_url=WEBHOOK_URL):
                            micro_signals_this_loop += 1
                            sent_this_loop += 1
                            print(f"Micro signal sent: {alert.get('type')} ({alert.get('game_display','')})")

                    reprice_alert = _build_live_reprice_predictions(game, play_by_play, power_profile, best_odds_now, best_odds_prev)
                    if reprice_alert:
                        reprice_key = f"reprice:{reprice_alert.get('game_id')}:{reprice_alert.get('pitcher_id')}"
                        if reprice_key not in micro_alert_keys:
                            micro_alert_keys.add(reprice_key)
                            save_live_reprice_snapshot(reprice_alert)
                            msg = _format_micro_signal_message(reprice_alert)
                            if msg and send_discord_webhook(content=msg, webhook_url=WEBHOOK_URL):
                                micro_signals_this_loop += 1
                                sent_this_loop += 1
                                print(f"Live reprice sent: {reprice_alert.get('pitcher_name')} ({reprice_alert.get('game_display','')})")

                if detected_this_loop > 0 and detected_this_loop > sent_this_loop:
                    print(f"⚠️  HR detection gap: {detected_this_loop} detected but {sent_this_loop} sent Discord alerts")

                if heartbeat_enabled and in_progress_games > 0 and (now_ts - last_heartbeat_ts) >= heartbeat_every_seconds:
                    hb_lines = [
                        "💓 **LIVE MONITOR HEARTBEAT**",
                        f"⏱ Time: {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p ET')}",
                        f"🎮 In-progress games: {in_progress_games}",
                        f"⚾ Processed HR events today: {len(processed_home_runs)}",
                        f"📈 Loop summary: detected={detected_this_loop}, sent={sent_this_loop}, micro={micro_signals_this_loop}",
                        f"🔁 Poll rates: monitor={monitor_sleep_seconds}s, odds={odds_poll_seconds}s",
                    ]
                    hb_ok = send_discord_webhook(content="\n".join(hb_lines), webhook_url=WEBHOOK_URL, async_send=False)
                    if hb_ok:
                        last_heartbeat_ts = now_ts
                        print(f"Heartbeat sent ({heartbeat_minutes}m cadence)")
                    else:
                        print("⚠️ Heartbeat send failed; will retry next cycle")

                discord_success_rate = (sent_this_loop / detected_this_loop * 100) if detected_this_loop > 0 else 0
                write_live_monitor_status({
                    'mode': 'live_hr_monitor',
                    'in_progress_games': in_progress_games,
                    'detected_events_this_loop': detected_this_loop,
                    'sent_events_this_loop': sent_this_loop,
                    'discord_success_rate_pct': round(discord_success_rate, 1),
                    'micro_signals_this_loop': micro_signals_this_loop,
                    'processed_event_count': len(processed_home_runs),
                    'odds_players_tracked': len(best_odds_now),
                    'live_monitor_poll_seconds': monitor_sleep_seconds,
                    'live_odds_poll_seconds': odds_poll_seconds,
                    'updated_at': datetime.now().isoformat(),
                })
                time.sleep(monitor_sleep_seconds)
            except Exception as e:
                print("Error checking live feeds:", e)
                write_live_monitor_status({
                    'mode': 'live_hr_monitor',
                    'error': str(e),
                    'processed_event_count': len(processed_home_runs),
                })
                time.sleep(2)
    finally:
        _release_live_monitor_pidfile(expected_pid=current_pid)


def _is_pid_running(pid):
    """Best-effort monitor liveness check with process identity verification."""
    try:
        pid = int(pid)
    except Exception:
        return False

    if pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                [
                    "wmic", "process", "where", f"processid={pid}",
                    "get", "ProcessId,CommandLine", "/FORMAT:LIST"
                ],
                capture_output=True,
                text=True,
                timeout=5
            )
            out = (proc.stdout or "")
            if str(pid) not in out:
                return False
            lower = out.lower()
            return ('run_daily_predictions.py' in lower) and ('--live' in lower)
        except Exception:
            return False

    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _live_monitor_pid_file():
    return Path('data') / 'live_monitor.pid'


def _live_monitor_log_file():
    return Path('data') / 'live_monitor.log'


def _rotate_live_monitor_log_if_needed():
    """Rotate the live monitor log before launch so it cannot grow forever."""
    log_path = _live_monitor_log_file()
    if not log_path.exists():
        return

    try:
        max_mb = max(1, _safe_int(os.getenv('LIVE_MONITOR_LOG_MAX_MB', '2'), 2) or 2)
        backup_count = max(1, _safe_int(os.getenv('LIVE_MONITOR_LOG_BACKUPS', '3'), 3) or 3)
        max_bytes = max_mb * 1024 * 1024
        if log_path.stat().st_size < max_bytes:
            return

        for idx in range(backup_count, 0, -1):
            src = log_path.with_name(f"{log_path.name}.{idx}")
            dst = log_path.with_name(f"{log_path.name}.{idx + 1}")
            if not src.exists():
                continue
            if idx == backup_count:
                src.unlink(missing_ok=True)
            else:
                src.replace(dst)

        log_path.replace(log_path.with_name(f"{log_path.name}.1"))
        print(f"Rotated live monitor log at {max_mb} MB with {backup_count} backup(s)")
    except Exception as e:
        print(f"⚠️ Could not rotate live monitor log: {e}")


def _release_live_monitor_pidfile(expected_pid=None):
    pid_file = _live_monitor_pid_file()
    try:
        if not pid_file.exists():
            return
        current_text = pid_file.read_text(encoding='utf-8').strip()
        current_pid = int(current_text)
        if expected_pid is not None and current_pid != int(expected_pid):
            return
        pid_file.unlink(missing_ok=True)
    except Exception:
        pass


def _claim_live_monitor_pidfile():
    """Return True when this process successfully becomes the active live monitor."""
    pid_file = _live_monitor_pid_file()
    Path('data').mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text(encoding='utf-8').strip())
            if existing_pid != current_pid and _is_pid_running(existing_pid):
                print(f"Live monitor already active under PID {existing_pid}; exiting duplicate watcher.")
                return False
        except Exception:
            pass

    try:
        pid_file.write_text(str(current_pid), encoding='utf-8')
        return True
    except Exception as e:
        print(f"Could not write live monitor PID file: {e}")
        return False


def _read_live_monitor_log_tail(max_lines=20):
    try:
        log_path = _live_monitor_log_file()
        if not log_path.exists():
            return ''
        lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
        return "\n".join(lines[-max(1, int(max_lines)):])
    except Exception:
        return ''


def _send_live_monitor_startup_report(webhook_url, current_pid, processed_home_runs, backfill_sent, monitor_sleep_seconds, odds_poll_seconds):
    """Send a one-time startup sanity report so ops can verify the watcher is alive."""
    lines = [
        "🟢 **LIVE MONITOR STARTED**",
        f"⏱ Time: {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p ET')}",
        f"🆔 PID: {current_pid}",
        f"⚾ Processed HR events loaded: {len(processed_home_runs)}",
        f"🔁 Backfill alerts sent at startup: {int(backfill_sent or 0)}",
        f"📡 Poll rates: monitor={monitor_sleep_seconds}s, odds={odds_poll_seconds}s",
    ]
    ok = send_discord_webhook(content="\n".join(lines), webhook_url=webhook_url, async_send=False)
    if ok:
        print("Startup sanity report sent")
    else:
        print("⚠️ Startup sanity report failed")
    return ok


def launch_live_monitor_background():
    """Launch one background live monitor process unless already running."""
    pid_file = _live_monitor_pid_file()
    log_file = _live_monitor_log_file()
    Path('data').mkdir(parents=True, exist_ok=True)

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text(encoding='utf-8').strip())
            if _is_pid_running(existing_pid):
                print(f"Live monitor already running (PID {existing_pid}).")
                return
        except Exception:
            pass
        _release_live_monitor_pidfile()

    try:
        _rotate_live_monitor_log_if_needed()
        with log_file.open('a', encoding='utf-8') as log_handle:
            log_handle.write(f"\n[{datetime.now().isoformat()}] Launching live monitor background process\n")
            log_handle.flush()
        child = subprocess.Popen(
            [sys.executable, __file__, "--live"],
            stdout=log_file.open('a', encoding='utf-8'),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        )
        time.sleep(2)
        exit_code = child.poll()
        if exit_code is not None:
            _release_live_monitor_pidfile(expected_pid=child.pid)
            tail = _read_live_monitor_log_tail(max_lines=25)
            print(f"⚠️ Live monitor exited immediately with code {exit_code}.")
            if tail:
                print("Recent live monitor log:")
                print(tail)
            return
        print(f"✅ Live monitor launched in background (PID {child.pid}). Log: {log_file}")
    except Exception as e:
        print(f"⚠️ Could not start live monitor: {e}")


def pull_games(date_str):
    """Fetch and return Statcast data for a given date (YYYY-MM-DD)."""
    print(f"Initiating Statcast pitch metric ingestion tracking for: {date_str}")
    try:
        df = statcast_with_timeout(start_dt=date_str, end_dt=date_str)
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
    parser.add_argument("--self-check", action="store_true", help="Print active mode/weight and physics calibration readiness")
    parser.add_argument("--backfill-physics", action="store_true", help="Backfill physics columns in recent predictions files")
    parser.add_argument("--weekly-todo", action="store_true", help="Print prioritized next-week action list from current system status")
    parser.add_argument("--systematic-ev", action="store_true", help="Run full +EV operation (backfill, self-check, predict, weekly todo)")
    parser.add_argument("--bet-ready", action="store_true", help="Print only actionable wagers (+EV with odds and Kelly > 0)")
    parser.add_argument("--backfill-days", type=int, default=30, help="Lookback window for self-check/backfill commands")

    args = parser.parse_args()

    if args.live:
        monitor_live_home_runs()
        return

    if args.self_check:
        run_model_self_check(days_lookback=max(1, int(args.backfill_days)))
        return

    if args.backfill_physics:
        backfill_physics_columns(days_lookback=max(1, int(args.backfill_days)))
        run_model_self_check(days_lookback=max(1, int(args.backfill_days)))
        return

    if args.systematic_ev:
        run_systematic_ev_operation(backfill_days=max(1, int(args.backfill_days)))
        if args.bet_ready:
            print_bet_ready_wagers()
            print_conservative_bet_ready_wagers()
        launch_live_monitor_background()
        return

    if args.weekly_todo:
        print_weekly_todo(days_lookback=max(1, int(args.backfill_days)))
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

    if args.bet_ready:
        generate_daily_predictions()
        print_bet_ready_wagers()
        print_conservative_bet_ready_wagers()
        launch_live_monitor_background()
        return

    generate_daily_predictions()
    print_conservative_bet_ready_wagers()
    
    # Pre-game lineup check (2-3 hours before first pitch)
    try:
        print("\n" + "="*70)
        print("PRE-GAME LINEUP CHECK — Confirming final lineups before games")
        print("="*70)
        if check_lineups_pregame is not None:
            pregame_lineups = check_lineups_pregame()
            if pregame_lineups:
                save_lineup_report(pregame_lineups, "_pregame_check")
                print(f"\n✅ Pre-game lineup check complete: {len(pregame_lineups)} games verified")
            else:
                print("⚠️  No games within 6 hours of first pitch")
        else:
            print("⚠️  baseball_savant module not available for pre-game check")
    except Exception as e:
        print(f"⚠️  Pre-game lineup check failed: {e}")
    
    # Spawn (or reuse) background live monitor process to catch home runs throughout the day.
    print("\n📡 Ensuring live home run monitor is running in background...")
    launch_live_monitor_background()


if __name__ == "__main__":
    main()
