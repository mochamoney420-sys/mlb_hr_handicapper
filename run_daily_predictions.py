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
import json
import hashlib
import pickle
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
    from sklearn.metrics import log_loss
    from sklearn.linear_model import LogisticRegression, SGDClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.isotonic import IsotonicRegression
except ImportError:
    RandomForestClassifier = None
    CalibratedClassifierCV = None
    TimeSeriesSplit = None
    log_loss = None
    LogisticRegression = None
    SGDClassifier = None
    StandardScaler = None
    IsotonicRegression = None
try:
    from sklearn.frozen import FrozenEstimator
except Exception:
    FrozenEstimator = None
from datetime import datetime, timedelta
from pybaseball import statcast
from threading import Thread
from queue import Queue


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
    from src.advanced_math import calculate_platoon_cluster_prob
except ImportError:
    print("Warning: advanced_math module not available")
    calculate_platoon_cluster_prob = None

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

RETRACTABLE_ROOF_VENUES = {
    'Chase Field',
    'Daikin Park',
    'T-Mobile Park',
    'American Family Field',
    'Rogers Centre',
    'Marlins Park',
    'LoanDepot Park',
    'Globe Life Field',
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


def get_lineup_slot_pa_expectation(batting_order_slot):
    """Expected plate appearances by lineup slot for a typical 9-inning game."""
    slot_map = {
        1: 4.75,
        2: 4.62,
        3: 4.48,
        4: 4.34,
        5: 4.22,
        6: 4.08,
        7: 3.95,
        8: 3.86,
        9: 3.78,
    }
    try:
        slot = int(batting_order_slot)
    except Exception:
        slot = 5
    return float(slot_map.get(max(1, min(9, slot)), 4.22))


def _load_roof_status_overrides():
    """Optional manual roof-status overrides from env JSON mapping venue -> status."""
    raw = str(os.getenv('ROOF_STATUS_OVERRIDES_JSON', '') or '').strip()
    if not raw:
        return {}
    try:
        payload = _json.loads(raw)
        if isinstance(payload, dict):
            return {str(k).strip(): str(v).strip().lower() for k, v in payload.items()}
    except Exception:
        pass
    return {}


def infer_roof_status(venue_name, raw_game_payload=None, weather=None):
    """Infer retractable-roof status and whether outdoor weather should be neutralized."""
    venue = str(venue_name or '').strip()
    overrides = _load_roof_status_overrides()
    if venue in overrides:
        status = overrides[venue]
        sealed = status in {'closed', 'sealed', 'indoors', 'indoor'}
        return status, int(sealed)

    if venue not in RETRACTABLE_ROOF_VENUES:
        return 'open_air', 0

    payload_text = ''
    if raw_game_payload:
        try:
            payload_text = _json.dumps(raw_game_payload).lower()
        except Exception:
            payload_text = str(raw_game_payload).lower()
    if 'roof closed' in payload_text or 'closed roof' in payload_text:
        return 'closed', 1
    if 'roof open' in payload_text or 'open roof' in payload_text:
        return 'open', 0

    weather = weather or {}
    precip = float(pd.to_numeric(weather.get('precipitation', 0.0), errors='coerce')) if weather is not None else 0.0
    wind = float(pd.to_numeric(weather.get('wind_speed', 0.0), errors='coerce')) if weather is not None else 0.0
    temp = float(pd.to_numeric(weather.get('temp', 72.0), errors='coerce')) if weather is not None else 72.0

    if precip >= 0.05 or wind >= 16 or temp <= 48 or temp >= 95:
        return 'closed_heuristic', 1
    return 'open_heuristic', 0


def bullpen_xfip_degradation_index(home_bullpen_score, away_bullpen_score, is_home_game):
    """Proxy index for expected bullpen degradation faced by a batter.

    Higher value means the opposing bullpen is weaker/more fatigued, which raises late-game HR risk.
    """
    h = float(pd.to_numeric(home_bullpen_score, errors='coerce'))
    a = float(pd.to_numeric(away_bullpen_score, errors='coerce'))
    if np.isnan(h):
        h = 50.0
    if np.isnan(a):
        a = 50.0

    opp = a if bool(is_home_game) else h
    # Convert 0-100 style bullpen quality/fatigue score into a centered degradation signal.
    # Positive when bullpen profile projects more run environment late.
    centered = (opp - 50.0) / 50.0
    return float(np.clip(centered, -1.0, 1.0))


def build_bullpen_fatigue_multiplier_map(
    statcast_df,
    lookback_days=14,
    top_n_arms=3,
    fatigue_pitch_count=20,
    boost_multiplier=1.10,
):
    """Return team -> bullpen fatigue multiplier based on trailing 48h arm usage.

    Rule: if the top N high-leverage relievers (usage-ranked over recent games)
    threw at least `fatigue_pitch_count` pitches on each of the last two days,
    mark bullpen as fatigued and return `boost_multiplier` for opposing hitters.
    """
    default_map = {}
    debug_info = {
        'teams_flagged': 0,
        'teams_seen': 0,
    }
    if statcast_df is None or statcast_df.empty:
        return default_map, debug_info

    work = statcast_df.copy()
    if 'pitcher' not in work.columns:
        return default_map, debug_info

    # Build stable date axis.
    date_col = 'game_date' if 'game_date' in work.columns else None
    if date_col is None:
        return default_map, debug_info
    work['_game_date'] = pd.to_datetime(work[date_col], errors='coerce').dt.date
    work = work[work['_game_date'].notna()].copy()
    if work.empty:
        return default_map, debug_info

    cutoff = (datetime.today() - timedelta(days=max(2, int(lookback_days)))).date()
    work = work[work['_game_date'] >= cutoff].copy()
    if work.empty:
        return default_map, debug_info

    # Resolve pitcher's defending team as robustly as possible.
    if 'pitching_team' in work.columns:
        work['_pitching_team'] = work['pitching_team'].astype(str).str.upper().str.strip()
    elif {'inning_topbot', 'home_team', 'away_team'}.issubset(set(work.columns)):
        itb = work['inning_topbot'].astype(str).str.lower().str.strip()
        work['_pitching_team'] = np.where(itb.str.startswith('top'), work['home_team'], work['away_team'])
        work['_pitching_team'] = work['_pitching_team'].astype(str).str.upper().str.strip()
    elif 'posteam' in work.columns:
        # In Statcast, posteam is offense; use opposite side where possible is unavailable here,
        # so fallback to empty and skip unreliable rows.
        work['_pitching_team'] = ''
    else:
        work['_pitching_team'] = ''

    work['pitcher'] = pd.to_numeric(work['pitcher'], errors='coerce')
    work = work[(work['pitcher'].notna()) & (work['_pitching_team'].astype(str).str.len() >= 2)].copy()
    if work.empty:
        return default_map, debug_info

    # Count pitches per pitcher per day.
    daily = work.groupby(['_pitching_team', 'pitcher', '_game_date'], as_index=False).size()
    daily = daily.rename(columns={'size': 'pitch_count'})

    # Estimate "high-leverage" bullpen arms as most-used relievers over lookback.
    usage = daily.groupby(['_pitching_team', 'pitcher'], as_index=False).agg(
        total_pitches=('pitch_count', 'sum'),
        active_days=('pitch_count', 'count'),
        avg_pitches_day=('pitch_count', 'mean'),
    )

    # Starter filter: relievers typically average fewer pitches/day than starters.
    reliever_pool = usage[(usage['active_days'] >= 3) & (usage['avg_pitches_day'] <= 35)].copy()
    if reliever_pool.empty:
        return default_map, debug_info

    reliever_pool = reliever_pool.sort_values(['_pitching_team', 'total_pitches'], ascending=[True, False])

    today = datetime.today().date()
    d1 = today - timedelta(days=1)
    d2 = today - timedelta(days=2)
    daily_lookup = {
        (str(r['_pitching_team']), float(r['pitcher']), r['_game_date']): int(r['pitch_count'])
        for _, r in daily.iterrows()
    }

    team_map = {}
    for team, grp in reliever_pool.groupby('_pitching_team'):
        top_grp = grp.head(max(1, int(top_n_arms)))
        if len(top_grp) < max(1, int(top_n_arms)):
            team_map[str(team)] = 1.0
            continue

        exhausted_count = 0
        for _, arm in top_grp.iterrows():
            pid = float(arm['pitcher'])
            p1 = int(daily_lookup.get((str(team), pid, d1), 0))
            p2 = int(daily_lookup.get((str(team), pid, d2), 0))
            if p1 >= int(fatigue_pitch_count) and p2 >= int(fatigue_pitch_count):
                exhausted_count += 1

        fatigued = exhausted_count >= max(1, int(top_n_arms))
        team_map[str(team)] = float(boost_multiplier if fatigued else 1.0)

    debug_info['teams_seen'] = int(len(team_map))
    debug_info['teams_flagged'] = int(sum(1 for v in team_map.values() if float(v) > 1.0))
    return team_map, debug_info


def _clv_tracker_path(date_str=None):
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    return Path('data') / f'clv_tracker_{date_str}.csv'


def _record_clv_entry(date_str, batter_name, open_odds_american, model_prob, source='live_ev_alert'):
    path = _clv_tracker_path(date_str)
    Path('data').mkdir(parents=True, exist_ok=True)
    row = {
        'timestamp': datetime.now().isoformat(),
        'batter_name': str(batter_name or ''),
        'open_odds_american': _safe_float(open_odds_american, np.nan),
        'model_prob': _safe_float(model_prob, np.nan),
        'source': str(source),
    }
    df = pd.DataFrame([row])
    if path.exists():
        df.to_csv(path, mode='a', header=False, index=False)
    else:
        df.to_csv(path, index=False)


def _compute_clv_implied_delta(entry_odds, current_odds):
    e = _safe_float(entry_odds)
    c = _safe_float(current_odds)
    if e is None or c is None:
        return np.nan
    try:
        return float(american_to_implied_prob(c) - american_to_implied_prob(e))
    except Exception:
        return np.nan


def _load_odds_snapshots(date_str=None):
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    snapshot_file = Path('data') / f'odds_snapshots_{date_str}.jsonl'
    if not snapshot_file.exists():
        return []

    snapshots = []
    try:
        for line in snapshot_file.read_text(encoding='utf-8').splitlines():
            line = str(line or '').strip()
            if not line:
                continue
            payload = _json.loads(line)
            ts_raw = payload.get('timestamp')
            odds_raw = payload.get('odds', {})
            ts = pd.to_datetime(ts_raw, errors='coerce')
            if pd.isna(ts) or not isinstance(odds_raw, dict):
                continue
            snapshots.append({
                'timestamp': ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts,
                'odds': odds_raw,
            })
    except Exception:
        return []

    snapshots.sort(key=lambda x: x.get('timestamp') or datetime.min)
    return snapshots


def _load_latest_odds_snapshot_payload(date_str=None):
    """Return the newest raw odds payload captured for the day, if available."""
    candidates = []
    if date_str is not None:
        candidates.append(str(date_str))
    else:
        candidates.append(datetime.today().strftime('%Y-%m-%d'))

    seen = set()
    for candidate_date in candidates:
        snapshots = _load_odds_snapshots(candidate_date)
        if snapshots:
            payload = snapshots[-1].get('odds', {})
            if isinstance(payload, dict) and len(payload) > 1:
                return payload
        seen.add(candidate_date)

    data_dir = Path('data')
    for snapshot_file in sorted(data_dir.glob('odds_snapshots_*.jsonl'), reverse=True):
        date_token = snapshot_file.stem.replace('odds_snapshots_', '')
        if date_token in seen:
            continue
        try:
            snapshots = _load_odds_snapshots(date_token)
            if not snapshots:
                continue
            payload = snapshots[-1].get('odds', {})
            if isinstance(payload, dict) and len(payload) > 1:
                return payload
        except Exception:
            continue
    return {}


def _is_placeholder_raw_odds_payload(raw_odds):
    if not isinstance(raw_odds, dict) or len(raw_odds) < 2:
        return True

    normalized_players = []
    total_pairs = 0
    for player, book_map in raw_odds.items():
        normalized_players.append(_normalize_player_name(player))
        if isinstance(book_map, dict):
            total_pairs += sum(1 for _, odds in book_map.items() if _safe_float(odds) is not None)

    if total_pairs < 2:
        return True

    placeholder_tokens = {'player a', 'player b', 'player c', 'unknown', 'n/a', 'null'}
    if all(name in placeholder_tokens or not name for name in normalized_players):
        return True

    return False


def _resolve_snapshot_book_map(snapshot_odds, batter_name):
    norm = _normalize_player_name(batter_name)
    if not norm:
        return {}

    direct = snapshot_odds.get(batter_name)
    if isinstance(direct, dict):
        return direct

    exact = snapshot_odds.get(norm)
    if isinstance(exact, dict):
        return exact

    for k, v in (snapshot_odds or {}).items():
        if not isinstance(v, dict):
            continue
        kn = _normalize_player_name(k)
        if kn == norm or (kn and (norm in kn or kn in norm)):
            return v
    return {}


def _infer_close_line_from_snapshots(snapshots, batter_name, game_dt):
    if not snapshots or game_dt is None:
        return None, None, None

    lookback_minutes = max(10, _env_int('CLV_CLOSE_LOOKBACK_MINUTES', 90))
    post_start_minutes = max(1, _env_int('CLV_CLOSE_POSTSTART_MINUTES', 5))

    pre_start = game_dt - timedelta(minutes=lookback_minutes)
    post_end = game_dt + timedelta(minutes=post_start_minutes)

    pre_candidates = []
    post_candidates = []
    for snap in snapshots:
        ts = snap.get('timestamp')
        if ts is None or ts < pre_start or ts > post_end:
            continue
        book_map = _resolve_snapshot_book_map(snap.get('odds', {}), batter_name)
        if not book_map:
            continue
        book, line = _best_line_from_book_map(book_map)
        if line is None:
            continue
        rec = (ts, book, int(line))
        if ts <= game_dt:
            pre_candidates.append(rec)
        else:
            post_candidates.append(rec)

    if pre_candidates:
        ts, book, line = sorted(pre_candidates, key=lambda x: x[0])[-1]
        return line, book, ts
    if post_candidates:
        ts, book, line = sorted(post_candidates, key=lambda x: x[0])[0]
        return line, book, ts
    return None, None, None


def _snapshot_market_consensus(
    snapshots,
    batter_name,
    min_snapshots=2,
    min_books=2,
    max_implied_spread=0.08,
):
    """Score how consistent market pricing is across snapshots/books for one batter."""
    if not snapshots or not batter_name:
        return {
            'snapshot_points': 0,
            'snapshot_books': 0,
            'snapshot_implied_spread': np.nan,
            'snapshot_consensus_ok': False,
        }

    implied_points = []
    books_seen = set()
    for snap in snapshots:
        book_map = _resolve_snapshot_book_map(snap.get('odds', {}), batter_name)
        if not isinstance(book_map, dict) or not book_map:
            continue

        for bk, odds in book_map.items():
            p = american_to_implied_prob(odds)
            if p is None or not np.isfinite(p):
                continue
            implied_points.append(float(p))
            books_seen.add(str(bk).strip().lower())

    spread = np.nan
    if implied_points:
        spread = float(max(implied_points) - min(implied_points))

    consensus_ok = (
        len(implied_points) >= max(1, int(min_snapshots))
        and len(books_seen) >= max(1, int(min_books))
        and np.isfinite(spread)
        and spread <= float(max_implied_spread)
    )

    return {
        'snapshot_points': int(len(implied_points)),
        'snapshot_books': int(len(books_seen)),
        'snapshot_implied_spread': spread,
        'snapshot_consensus_ok': bool(consensus_ok),
    }


def audit_clv_performance(date_str=None, lookback_days=30):
    """Audit CLV quality by reconciling entry lines vs pregame close from snapshots."""
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    tracker_path = _clv_tracker_path(date_str)
    if not tracker_path.exists():
        print(f"CLV audit skipped: no tracker file for {date_str}.")
        return pd.DataFrame()

    try:
        clv_df = pd.read_csv(tracker_path)
    except Exception as exc:
        print(f"CLV audit failed to read tracker: {exc}")
        return pd.DataFrame()

    if clv_df.empty:
        print(f"CLV audit skipped: tracker file empty for {date_str}.")
        return pd.DataFrame()

    pred_file = Path('data') / f'predictions_{date_str}.csv'
    game_time_by_name = {}
    if pred_file.exists():
        try:
            preds = pd.read_csv(pred_file)
            for _, r in preds.iterrows():
                nm = _normalize_player_name(r.get('batter_name'))
                if nm:
                    game_time_by_name[nm] = r.get('game_time', '')
        except Exception:
            pass

    snapshots = _load_odds_snapshots(date_str)

    records = []
    updates = []
    for idx, row in clv_df.iterrows():
        batter_name = str(row.get('batter_name', '')).strip()
        norm = _normalize_player_name(batter_name)
        entry = _safe_float(row.get('open_odds_american'))
        if entry is None:
            continue

        close_line = _safe_float(row.get('close_odds_american'))
        close_ts = str(row.get('close_timestamp', '') or '').strip()
        close_book = str(row.get('close_book', '') or '').strip()

        game_dt = _parse_game_time_for_date(date_str, game_time_by_name.get(norm))
        if close_line is None and game_dt is not None:
            inferred_line, inferred_book, inferred_ts = _infer_close_line_from_snapshots(snapshots, batter_name, game_dt)
            if inferred_line is not None:
                close_line = float(inferred_line)
                close_book = str(inferred_book or '')
                close_ts = inferred_ts.isoformat() if hasattr(inferred_ts, 'isoformat') else str(inferred_ts)
                updates.append((idx, close_line, close_book, close_ts))

        clv_delta = _compute_clv_implied_delta(entry, close_line)
        records.append({
            'date': date_str,
            'batter_name': batter_name,
            'entry_timestamp': row.get('timestamp', ''),
            'entry_odds_american': entry,
            'close_odds_american': close_line,
            'close_timestamp': close_ts,
            'close_book': close_book,
            'model_prob': _safe_float(row.get('model_prob'), np.nan),
            'clv_implied_delta': clv_delta,
            'has_close_line': int(close_line is not None and np.isfinite(close_line)),
            'source': str(row.get('source', '')),
        })

    if updates:
        if 'close_book' not in clv_df.columns:
            clv_df['close_book'] = ''
        if 'close_odds_american' not in clv_df.columns:
            clv_df['close_odds_american'] = np.nan
        if 'close_clv_implied_delta' not in clv_df.columns:
            clv_df['close_clv_implied_delta'] = np.nan
        if 'close_timestamp' not in clv_df.columns:
            clv_df['close_timestamp'] = ''

        for idx, close_line, close_book, close_ts in updates:
            clv_df.at[idx, 'close_odds_american'] = close_line
            clv_df.at[idx, 'close_book'] = close_book
            clv_df.at[idx, 'close_timestamp'] = close_ts
            clv_df.at[idx, 'close_clv_implied_delta'] = _compute_clv_implied_delta(
                clv_df.at[idx, 'open_odds_american'],
                close_line,
            )
        try:
            clv_df.to_csv(tracker_path, index=False)
        except Exception:
            pass

    audit_df = pd.DataFrame(records)
    if audit_df.empty:
        print(f"CLV audit found no valid entries for {date_str}.")
        return audit_df

    Path('data').mkdir(parents=True, exist_ok=True)
    audit_path = Path('data') / f'clv_audit_{date_str}.csv'
    audit_df.to_csv(audit_path, index=False)

    valid = audit_df[audit_df['has_close_line'] == 1].copy()
    coverage = float(len(valid) / max(1, len(audit_df)))
    avg_delta = float(pd.to_numeric(valid.get('clv_implied_delta', np.nan), errors='coerce').mean()) if not valid.empty else np.nan
    med_delta = float(pd.to_numeric(valid.get('clv_implied_delta', np.nan), errors='coerce').median()) if not valid.empty else np.nan
    hit_rate = float((pd.to_numeric(valid.get('clv_implied_delta', np.nan), errors='coerce') > 0).mean()) if not valid.empty else np.nan

    rolling_vals = []
    cutoff = datetime.today() - timedelta(days=max(1, int(lookback_days)))
    for fp in sorted(Path('data').glob('clv_audit_*.csv')):
        try:
            d = datetime.strptime(fp.stem.replace('clv_audit_', ''), '%Y-%m-%d')
            if d < cutoff:
                continue
            tmp = pd.read_csv(fp)
            vals = pd.to_numeric(tmp.get('clv_implied_delta', np.nan), errors='coerce').dropna()
            rolling_vals.extend(vals.tolist())
        except Exception:
            continue
    rolling_30d_avg = float(np.mean(rolling_vals)) if rolling_vals else np.nan

    summary = {
        'date': date_str,
        'entries': int(len(audit_df)),
        'entries_with_close': int(len(valid)),
        'close_coverage': coverage,
        'avg_clv_implied_delta': avg_delta,
        'median_clv_implied_delta': med_delta,
        'positive_clv_rate': hit_rate,
        'rolling_lookback_days': int(lookback_days),
        'rolling_avg_clv_implied_delta': rolling_30d_avg,
        'updated_at': datetime.now().isoformat(),
    }
    summary_path = Path('data') / f'clv_audit_summary_{date_str}.json'
    try:
        summary_path.write_text(_json.dumps(summary, indent=2), encoding='utf-8')
    except Exception:
        pass

    print(
        "CLV AUDIT: "
        f"entries={summary['entries']}, close_coverage={summary['close_coverage']*100:.1f}%, "
        f"avg_delta={summary['avg_clv_implied_delta'] if pd.notna(summary['avg_clv_implied_delta']) else float('nan'):.5f}, "
        f"positive_rate={summary['positive_clv_rate'] if pd.notna(summary['positive_clv_rate']) else float('nan'):.2%}"
    )
    print(f"Saved CLV audit: {audit_path}")
    print(f"Saved CLV summary: {summary_path}")

    return audit_df


def _parse_game_time_for_date(date_str, game_time_value):
    """Best-effort parse of prediction game-time text into a naive datetime."""
    raw = str(game_time_value or '').strip()
    if not raw:
        return None

    candidates = [raw]
    cleaned = raw.replace('ET', '').replace('EST', '').replace('EDT', '').strip()
    if cleaned and cleaned != raw:
        candidates.append(cleaned)
    if date_str:
        candidates.append(f"{date_str} {raw}")
        candidates.append(f"{date_str} {cleaned}")

    for c in candidates:
        try:
            ts = pd.to_datetime(c, errors='coerce')
            if pd.notna(ts):
                if hasattr(ts, 'to_pydatetime'):
                    return ts.to_pydatetime()
                return ts
        except Exception:
            continue
    return None


def _update_clv_tracker_snapshot(date_str, latest_best_odds_by_batter, game_time_by_batter=None):
    """Update current odds and near-first-pitch CLV snapshots for tracked entries."""
    path = _clv_tracker_path(date_str)
    if not path.exists():
        return

    try:
        clv_df = pd.read_csv(path)
    except Exception:
        return

    if clv_df.empty:
        return

    now_ts = datetime.now()
    game_time_by_batter = game_time_by_batter or {}

    if 'current_odds_american' not in clv_df.columns:
        clv_df['current_odds_american'] = np.nan
    if 'current_clv_implied_delta' not in clv_df.columns:
        clv_df['current_clv_implied_delta'] = np.nan
    if 'close_odds_american' not in clv_df.columns:
        clv_df['close_odds_american'] = np.nan
    if 'close_clv_implied_delta' not in clv_df.columns:
        clv_df['close_clv_implied_delta'] = np.nan
    if 'close_timestamp' not in clv_df.columns:
        clv_df['close_timestamp'] = ''

    for idx, row in clv_df.iterrows():
        batter_name = str(row.get('batter_name', '')).strip()
        norm_name = _normalize_player_name(batter_name)
        latest_odds = latest_best_odds_by_batter.get(norm_name)
        if latest_odds is None:
            continue

        clv_df.at[idx, 'current_odds_american'] = latest_odds
        clv_df.at[idx, 'current_clv_implied_delta'] = _compute_clv_implied_delta(
            row.get('open_odds_american'),
            latest_odds,
        )

        close_odds = _safe_float(row.get('close_odds_american'))
        if close_odds is not None:
            continue

        game_dt = _parse_game_time_for_date(date_str, game_time_by_batter.get(norm_name))
        if game_dt is None:
            continue

        # Capture closing-line snapshot exactly at first-pitch minus ~1 minute.
        # Small grace window handles scheduler jitter and API latency.
        close_window_start = game_dt - timedelta(minutes=1)
        close_window_end = game_dt + timedelta(minutes=2)
        if close_window_start <= now_ts <= close_window_end:
            clv_df.at[idx, 'close_odds_american'] = latest_odds
            clv_df.at[idx, 'close_clv_implied_delta'] = _compute_clv_implied_delta(
                row.get('open_odds_american'),
                latest_odds,
            )
            clv_df.at[idx, 'close_timestamp'] = now_ts.isoformat()

    try:
        clv_df.to_csv(path, index=False)
    except Exception:
        return

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


def _morning_bet_alert_state_path(date_str=None):
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    return Path('data') / f'morning_bet_alert_sent_{date_str}.json'


def _market_release_window_active_et():
    """Return True when current ET time is inside configured market-release window."""
    now_et = datetime.utcnow() - timedelta(hours=4)
    start_hour = _env_int('MORNING_ALERT_START_HOUR_ET', 9)
    end_hour = _env_int('MORNING_ALERT_END_HOUR_ET', 11)
    end_minute = _env_int('MORNING_ALERT_END_MINUTE_ET', 0)

    cur_min = (now_et.hour * 60) + now_et.minute
    start_min = (start_hour * 60)
    end_min = (end_hour * 60) + end_minute
    return start_min <= cur_min <= end_min


def _has_morning_bet_alert_been_sent(date_str=None):
    path = _morning_bet_alert_state_path(date_str)
    if not path.exists():
        return False
    try:
        payload = _json.loads(path.read_text(encoding='utf-8'))
        return bool(payload.get('sent', False))
    except Exception:
        return False


def _mark_morning_bet_alert_sent(date_str=None, payload=None):
    path = _morning_bet_alert_state_path(date_str)
    Path('data').mkdir(parents=True, exist_ok=True)
    body = {
        'sent': True,
        'sent_at': datetime.now().isoformat(),
    }
    if isinstance(payload, dict):
        body.update(payload)
    try:
        path.write_text(_json.dumps(body, indent=2), encoding='utf-8')
    except Exception:
        pass


def send_morning_bet_now_alert(live_df, date_str=None):
    """Send a once-per-day morning Discord alert with actionable pre-correction bets."""
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    enabled = str(os.getenv('MORNING_BET_ALERT_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
    if not enabled:
        return False

    if not _candidate_discord_webhooks():
        return False

    if _has_morning_bet_alert_been_sent(date_str):
        return False

    if not _market_release_window_active_et():
        return False

    if live_df is None or live_df.empty:
        return False

    picks = live_df.copy()
    for c in ['pred_hr_prob', 'market_prob', 'ev_percent', 'kelly_fraction', 'best_market_odds_american']:
        if c in picks.columns:
            picks[c] = pd.to_numeric(picks[c], errors='coerce')

    min_odds = _env_int('MORNING_BET_ALERT_MIN_AMERICAN_ODDS', 300)
    min_edge_abs = _env_float('MORNING_BET_ALERT_MIN_EDGE_ABS', 0.02)
    min_ev_pct = _env_float('MORNING_BET_ALERT_MIN_EV_PCT', 2.0)
    min_kelly = _env_float('MORNING_BET_ALERT_MIN_KELLY', 0.005)
    top_n = max(3, _env_int('MORNING_BET_ALERT_TOP_N', 8))

    picks['edge_abs'] = (picks.get('pred_hr_prob', 0.0) - picks.get('market_prob', np.nan))
    picks['has_odds'] = picks.get('best_market_odds_american', np.nan).notna()

    actionable = picks[
        picks['has_odds'] &
        (picks['best_market_odds_american'] >= min_odds) &
        (picks['edge_abs'] >= min_edge_abs) &
        (picks.get('ev_percent', 0.0).fillna(0.0) >= min_ev_pct) &
        (picks.get('kelly_fraction', 0.0).fillna(0.0) >= min_kelly)
    ].copy()

    actionable = actionable.sort_values(
        by=['ev_percent', 'edge_abs', 'kelly_fraction', 'pred_hr_prob'],
        ascending=[False, False, False, False]
    ).head(top_n)

    if actionable.empty:
        message = (
            f"⏰ **MARKET RELEASE CHECK ({date_str})**\n"
            "No qualifying +EV HR bets right now under morning risk gates.\n"
            f"Filters: odds>=+{min_odds}, edge>={min_edge_abs*100:.1f} pts, "
            f"EV>={min_ev_pct:.1f}%, Kelly>={min_kelly:.3f}."
        )
        sent = send_discord_webhook(content=message)
        if sent:
            _mark_morning_bet_alert_sent(date_str, {
                'qualifying_rows': 0,
                'min_odds': min_odds,
                'min_edge_abs': min_edge_abs,
                'min_ev_pct': min_ev_pct,
                'min_kelly': min_kelly,
            })
        return bool(sent)

    lines = [
        f"🚨 **BET NOW — MARKET RELEASE WINDOW ({date_str})**",
        "Lines are still inefficient; action before correction:",
    ]
    for _, row in actionable.iterrows():
        stake_usd = float(row.get('stake_usd', _estimate_bet_stake_usd(row.get('kelly_fraction', 0.0))) or 0.0)
        lines.append(
            f"- {row.get('batter_name','')} vs {row.get('pitcher_name','')} | "
            f"{int(row.get('best_market_odds_american')):+d} ({row.get('best_book','n/a')}) | "
            f"model={float(row.get('pred_hr_prob', 0.0))*100:.1f}% | "
            f"mkt={float(row.get('market_prob', 0.0))*100:.1f}% | "
            f"EV={float(row.get('ev_percent', 0.0)):+.1f}% | "
            f"Kelly={float(row.get('kelly_fraction', 0.0)):.3f} | "
            f"Stake=${stake_usd:.2f}"
        )

    sent = send_discord_webhook(content="\n".join(lines))
    if sent:
        _mark_morning_bet_alert_sent(date_str, {
            'qualifying_rows': int(len(actionable)),
            'min_odds': min_odds,
            'min_edge_abs': min_edge_abs,
            'min_ev_pct': min_ev_pct,
            'min_kelly': min_kelly,
            'top_n': top_n,
            'top_names': actionable['batter_name'].astype(str).tolist(),
        })
    return bool(sent)


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
        'ev_percent', 'edge_pct', 'kelly_fraction', 'stake_usd', 'actionability_score', 'game_time'
    ]
    keep_cols = [c for c in keep_cols if c in actionable.columns]
    actionable['stake_usd'] = actionable['kelly_fraction'].apply(_estimate_bet_stake_usd)
    actionable['actionability_score'] = actionable.apply(_estimate_actionability_score, axis=1)
    min_actionability = _env_float('BET_READY_MIN_ACTIONABILITY_SCORE', 12.0)
    actionable = actionable[actionable['actionability_score'] >= min_actionability].copy()
    report = actionable[keep_cols].sort_values(
        by=['actionability_score', 'ev_percent', 'kelly_fraction'],
        ascending=[False, False, False]
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
    if 'best_market_odds_american' not in preds.columns:
        preds['best_market_odds_american'] = np.nan
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
        shortlist = preds[
            reliability_gate &
            (preds['pred_hr_prob'] >= max(min_prob * 0.5, 0.02)) &
            (preds['pred_hr_prob'] <= max_prob)
        ].copy()
        if shortlist.empty:
            print("No conservative shortlist: no rows passed strict risk filters.")
            return shortlist
        print("Strict conservative filters returned no rows; falling back to a broader model-driven shortlist.")

    shortlist_base = shortlist.copy()
    shortlist['conservative_score'] = (
        shortlist['kelly_fraction'].fillna(0) * 100
        + shortlist['ev_percent'].fillna(0)
        + shortlist['edge_pct'].fillna(0) / 2.0
    )
    shortlist['stake_usd'] = shortlist['kelly_fraction'].apply(_estimate_bet_stake_usd)
    shortlist['actionability_score'] = shortlist.apply(_estimate_actionability_score, axis=1)
    min_actionability = _env_float('CONSERVATIVE_MIN_ACTIONABILITY_SCORE', 18.0)
    shortlist = shortlist[shortlist['actionability_score'] >= min_actionability].copy()
    if shortlist.empty and not shortlist_base.empty:
        shortlist = shortlist_base.copy()
        shortlist['conservative_score'] = (
            shortlist['kelly_fraction'].fillna(0) * 100
            + shortlist['ev_percent'].fillna(0)
            + shortlist['edge_pct'].fillna(0) / 2.0
        )
        shortlist['stake_usd'] = shortlist['kelly_fraction'].apply(_estimate_bet_stake_usd)
        shortlist['actionability_score'] = shortlist.apply(_estimate_actionability_score, axis=1)
        print("No rows survived the conservative actionability threshold; keeping the best-ranked fallback rows.")

    keep_cols = [
        'batter_name', 'pitcher_name', 'pred_hr_prob', 'model_reliability',
        'best_book', 'best_market_odds_american', 'fair_odds_american',
        'edge_pct', 'ev_percent', 'kelly_fraction', 'stake_usd', 'actionability_score', 'conservative_score', 'game_time'
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


def build_single_straight_market_discrepancies(preds_df, top_n=12):
    """Build a shortlist where model math materially disagrees with market prices.

    This targets single straight wagers only (one batter prop per row), not parlays.
    """
    if preds_df is None or preds_df.empty:
        return pd.DataFrame()

    work = preds_df.copy()

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
        'pred_hr_prob', 'market_prob', 'best_market_implied_prob', 'prob_edge_abs',
        'market_edge_pct', 'ev_percent', 'kelly_fraction', 'best_market_odds_american', 'matched_book_count'
    ]:
        if c in work.columns:
            work[c] = pd.to_numeric(work[c], errors='coerce')

    if 'model_reliability' not in work.columns:
        work['model_reliability'] = 'MEDIUM'
    else:
        work['model_reliability'] = work['model_reliability'].fillna('MEDIUM').astype(str).str.upper()

    if 'market_prob' not in work.columns:
        work['market_prob'] = np.nan
    if 'best_market_implied_prob' in work.columns:
        work['market_prob'] = pd.to_numeric(work['market_prob'], errors='coerce').fillna(
            pd.to_numeric(work['best_market_implied_prob'], errors='coerce')
        )

    if 'prob_edge_abs' not in work.columns:
        work['prob_edge_abs'] = (
            pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0)
            - pd.to_numeric(work.get('market_prob', np.nan), errors='coerce')
        ).fillna(0.0)

    if 'market_edge_pct' not in work.columns:
        mp = pd.to_numeric(work.get('market_prob', np.nan), errors='coerce')
        work['market_edge_pct'] = (
            (pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0) - mp) / mp * 100.0
        ).replace([np.inf, -np.inf], np.nan).fillna(0.0)

    if 'matched_book_count' not in work.columns:
        work['matched_book_count'] = 0
    work['matched_book_count'] = pd.to_numeric(work['matched_book_count'], errors='coerce').fillna(0).astype(int)

    if 'is_positive_ev' in work.columns:
        work['is_positive_ev_bool'] = work['is_positive_ev'].astype(str).str.lower().eq('true')
    else:
        work['is_positive_ev_bool'] = pd.to_numeric(work.get('ev_percent', 0.0), errors='coerce').fillna(0.0) > 0.0

    min_edge_abs = _env_float('STRAIGHT_DISCREPANCY_MIN_EDGE_ABS', 0.03)
    min_ev_pct = _env_float('STRAIGHT_DISCREPANCY_MIN_EV_PCT', 3.0)
    min_kelly = _env_float('STRAIGHT_DISCREPANCY_MIN_KELLY', 0.01)
    min_prob = _env_float('STRAIGHT_DISCREPANCY_MIN_PROB', 0.05)
    max_prob = _env_float('STRAIGHT_DISCREPANCY_MAX_PROB', 0.45)
    min_books = _env_int('STRAIGHT_DISCREPANCY_MIN_MATCHED_BOOKS', 1)
    min_american = _env_int('STRAIGHT_DISCREPANCY_MIN_AMERICAN_ODDS', -300)
    max_american = _env_int('STRAIGHT_DISCREPANCY_MAX_AMERICAN_ODDS', 5000)

    reliability_ok = work['model_reliability'].isin(['HIGH', 'MEDIUM'])
    odds_ok = (
        pd.to_numeric(work.get('best_market_odds_american', np.nan), errors='coerce').notna()
        & (pd.to_numeric(work.get('best_market_odds_american', np.nan), errors='coerce') >= min_american)
        & (pd.to_numeric(work.get('best_market_odds_american', np.nan), errors='coerce') <= max_american)
    )

    shortlist = work[
        reliability_ok
        & work['market_prob'].notna()
        & odds_ok
        & (pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0) >= min_prob)
        & (pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0) <= max_prob)
        & (pd.to_numeric(work.get('prob_edge_abs', 0.0), errors='coerce').fillna(0.0) >= min_edge_abs)
        & (pd.to_numeric(work.get('ev_percent', 0.0), errors='coerce').fillna(0.0) >= min_ev_pct)
        & (pd.to_numeric(work.get('kelly_fraction', 0.0), errors='coerce').fillna(0.0) >= min_kelly)
        & (work['matched_book_count'] >= min_books)
        & work['is_positive_ev_bool']
    ].copy()

    if shortlist.empty:
        return shortlist

    shortlist['wager_type'] = 'single_straight'
    shortlist['discrepancy_score'] = (
        shortlist['prob_edge_abs'].clip(lower=0.0) * 100.0 * 0.55
        + shortlist['ev_percent'].clip(lower=0.0) * 0.30
        + shortlist['kelly_fraction'].clip(lower=0.0) * 100.0 * 0.15
    )
    shortlist['stake_usd'] = shortlist['kelly_fraction'].apply(_estimate_bet_stake_usd)

    keep_cols = [
        'wager_type', 'batter_name', 'pitcher_name', 'game_time', 'model_reliability',
        'pred_hr_prob', 'market_prob', 'prob_edge_abs', 'market_edge_pct',
        'best_book', 'best_market_odds_american', 'fair_odds_american',
        'ev_percent', 'kelly_fraction', 'stake_usd', 'matched_book_count', 'discrepancy_score'
    ]
    keep_cols = [c for c in keep_cols if c in shortlist.columns]
    shortlist = shortlist[keep_cols].sort_values(
        by=['discrepancy_score', 'ev_percent', 'kelly_fraction'],
        ascending=[False, False, False]
    ).head(max(1, int(top_n))).reset_index(drop=True)
    return shortlist


def print_single_straight_market_discrepancies(date_str=None, top_n=12):
    """Print, persist, and return single straight wagers with strong model-vs-market gaps."""
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{date_str}.csv'
    if not pred_file.exists():
        print(f"No discrepancy shortlist: missing predictions file ({pred_file}).")
        return pd.DataFrame()

    try:
        preds = pd.read_csv(pred_file)
    except Exception as exc:
        print(f"No discrepancy shortlist: failed to read predictions file ({exc}).")
        return pd.DataFrame()

    shortlist = build_single_straight_market_discrepancies(preds, top_n=top_n)
    if shortlist.empty:
        print("No single-straight discrepancy shortlist: no rows passed discrepancy filters.")
        return shortlist

    out_path = Path('data') / f'straight_market_discrepancies_{date_str}.csv'
    shortlist.to_csv(out_path, index=False)
    print("\nSINGLE STRAIGHT MARKET DISCREPANCIES:")
    print(shortlist.to_string(index=False))
    print(f"Saved discrepancy shortlist: {out_path}")
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


def _split_discord_content(content, limit=2000):
    """Split Discord message content into <=limit chunks.

    Prefers splitting on newline boundaries, then spaces, and finally hard cuts.
    """
    text = str(content or '')
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text
    soft_floor = max(200, int(limit * 0.60))
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind('\n', 0, limit + 1)
        if split_at < soft_floor:
            split_at = remaining.rfind(' ', 0, limit + 1)
        if split_at <= 0:
            split_at = limit

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
        chunks.append(chunk)
        remaining = remaining[len(chunk):].lstrip('\n ').rstrip()

    return [c for c in chunks if c]


def _build_discord_payloads(content=None, embeds=None):
    """Build one or more Discord payloads honoring content size limits."""
    content_chunks = _split_discord_content(content, limit=2000) if content else []
    if not content_chunks:
        payload = {}
        if embeds:
            payload['embeds'] = embeds
        return [payload] if payload else []

    payloads = []
    for idx, chunk in enumerate(content_chunks):
        payload = {'content': chunk}
        # Attach embeds only on the first message to avoid duplicate cards.
        if embeds and idx == 0:
            payload['embeds'] = embeds
        payloads.append(payload)

    if len(payloads) > 1:
        print(f"Discord content auto-chunked into {len(payloads)} messages.")
    return payloads


def _send_discord_payloads_with_retry(payloads, webhook_candidates, retries=3, silent=False):
    """Send all payload chunks, retrying each chunk independently."""
    for payload in payloads:
        sent = False
        for attempt in range(max(1, int(retries))):
            sent = _send_discord_sync(payload, webhook_candidates, silent=silent)
            if sent:
                break
            if attempt < (max(1, int(retries)) - 1):
                time.sleep(0.5)
        if not sent:
            return False
    return True


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

    payloads = _build_discord_payloads(content=content, embeds=embeds)

    if not payloads:
        print("Nothing to send to Discord; payload is empty.")
        return False
    
    # For live HRs, try async but with error tracking
    if async_send:
        import threading
        # Wrap with error tracking queue
        result_holder = {'success': False, 'error': None}
        thread = threading.Thread(
            target=_send_discord_async_tracked,
            args=(payloads, webhook_candidates, result_holder, retries),
            daemon=True
        )
        thread.start()
        # Don't wait, but return True immediately (async)
        return True

    # Synchronous send with per-chunk retry.
    return _send_discord_payloads_with_retry(
        payloads,
        webhook_candidates,
        retries=retries,
        silent=False,
    )

def _send_discord_async_tracked(payloads, webhook_candidates, result_holder, retries=3):
    """Send Discord in background thread with error tracking."""
    try:
        success = _send_discord_payloads_with_retry(
            payloads,
            webhook_candidates,
            retries=retries,
            silent=False,
        )
        result_holder['success'] = success
    except Exception as e:
        result_holder['error'] = str(e)
        print(f"Async Discord send error: {e}")
        # Fallback: try one more time synchronously
        try:
            _send_discord_payloads_with_retry(
                payloads,
                webhook_candidates,
                retries=1,
                silent=True,
            )
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


def calculate_probability_metrics(y_true, probs):
    """Evaluate probabilistic binary predictions using log-loss and Brier score."""
    labels = pd.to_numeric(pd.Series(y_true), errors='coerce').fillna(0).astype(int).to_numpy()
    probs_array = np.clip(pd.to_numeric(pd.Series(probs), errors='coerce').fillna(0.0).to_numpy(), 1e-6, 1 - 1e-6)

    if len(labels) != len(probs_array):
        raise ValueError("y_true and probs must be the same length")

    if log_loss is None:
        logloss_value = float('nan')
    else:
        logloss_value = float(log_loss(labels, probs_array))

    brier_value = float(np.mean((probs_array - labels) ** 2))
    return {'log_loss': logloss_value, 'brier_score': brier_value}


def calculate_ensemble_weights(y_true, per_model_probs, model_names=None):
    """Compute log-loss-aware ensemble weights (better models get more voting power)."""
    labels = pd.to_numeric(pd.Series(y_true), errors='coerce').fillna(0).astype(int).to_numpy()
    names = list(model_names or [f"model_{i}" for i in range(len(per_model_probs))])

    if not per_model_probs:
        return np.array([], dtype=float), []

    scores = []
    for idx, probs in enumerate(per_model_probs):
        probs_arr = np.clip(pd.to_numeric(pd.Series(probs), errors='coerce').fillna(0.0).to_numpy(), 1e-6, 1 - 1e-6)
        if len(probs_arr) != len(labels):
            continue
        metrics = calculate_probability_metrics(labels, probs_arr)
        ll = metrics.get('log_loss', np.nan)
        if not np.isfinite(ll):
            ll = 1.5
        # Inverse-loss weighting strongly penalizes flat/underperforming models.
        inv = 1.0 / max(ll, 1e-6)
        scores.append((idx, names[idx], float(ll), float(inv)))

    if not scores:
        w = np.array([1.0 / len(per_model_probs)] * len(per_model_probs), dtype=float)
        return w, []

    inv_values = np.array([s[3] for s in scores], dtype=float)
    inv_values = np.power(inv_values, 1.35)
    inv_values = np.clip(inv_values, 1e-8, np.inf)
    weight_values = inv_values / inv_values.sum()

    out_weights = np.zeros(len(per_model_probs), dtype=float)
    diagnostics = []
    for (row_idx, name, ll, _), w in zip(scores, weight_values):
        out_weights[row_idx] = float(w)
        diagnostics.append({'model': name, 'log_loss': float(ll), 'weight': float(w)})

    if out_weights.sum() <= 0:
        out_weights = np.array([1.0 / len(per_model_probs)] * len(per_model_probs), dtype=float)
    else:
        out_weights = out_weights / out_weights.sum()

    return out_weights, diagnostics


def weighted_ensemble_probabilities(per_model_probs, weights):
    if not per_model_probs:
        return np.array([], dtype=float)
    probs_stack = np.vstack([np.asarray(p, dtype=float).reshape(-1) for p in per_model_probs])
    w = np.asarray(weights, dtype=float).reshape(-1)
    if len(w) != probs_stack.shape[0] or np.sum(w) <= 0:
        w = np.array([1.0 / probs_stack.shape[0]] * probs_stack.shape[0], dtype=float)
    w = w / np.sum(w)
    return np.average(probs_stack, axis=0, weights=w)


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
            file_date = _extract_date_from_prefixed_stem(f.stem, 'predictions_')
            if file_date is None:
                continue
            date_str = file_date.strftime('%Y-%m-%d')
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
            file_date = _extract_date_from_prefixed_stem(f.stem, 'predictions_')
            if file_date is None:
                continue
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
    today_prob_mean = None
    today_prob_q90 = None
    today_prob_max = None
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
                    today_prob_mean = float(prob_col.mean())
                    today_prob_q90 = float(prob_col.quantile(0.90))
                    today_prob_max = float(prob_col.max())
                    print(
                        f"✅ Today's predictions: {len(today_preds_df)} rows, "
                        f"mean={today_prob_mean:.3f}, q90={today_prob_q90:.3f}, max={today_prob_max:.3f}"
                    )
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

    # 6. Check recent closed-loop miss-rate trend for underprediction collapse.
    try:
        miss_rate_alert_threshold = float(np.clip(float(os.getenv('SELF_CHECK_MISS_RATE_ALERT_THRESHOLD', '0.85')), 0.50, 1.0))
    except Exception:
        miss_rate_alert_threshold = 0.85
    try:
        miss_rate_min_samples = max(20, int(float(os.getenv('SELF_CHECK_MISS_RATE_MIN_SAMPLES', '40'))))
    except Exception:
        miss_rate_min_samples = 40
    try:
        recovery_mean_threshold = float(np.clip(float(os.getenv('SELF_CHECK_RECOVERY_MEAN_THRESHOLD', '0.040')), 0.0, 1.0))
    except Exception:
        recovery_mean_threshold = 0.04
    try:
        recovery_q90_threshold = float(np.clip(float(os.getenv('SELF_CHECK_RECOVERY_Q90_THRESHOLD', '0.055')), 0.0, 1.0))
    except Exception:
        recovery_q90_threshold = 0.055

    try:
        latest_closed_loop = sorted(Path('data').glob('closed_loop_report_*.json'))
        if latest_closed_loop:
            newest_report = latest_closed_loop[-1]
            report = json.loads(newest_report.read_text(encoding='utf-8'))
            samples = int(report.get('samples', 0) or 0)
            missed_hr = int(report.get('missed_hr', 0) or 0)
            miss_rate = (missed_hr / samples) if samples > 0 else 0.0
            print(
                "Closed-loop check: "
                f"samples={samples}, missed_hr={missed_hr}, miss_rate={miss_rate:.1%}"
            )
            if samples >= miss_rate_min_samples and miss_rate >= miss_rate_alert_threshold:
                current_recovered = (
                    today_prob_mean is not None
                    and today_prob_q90 is not None
                    and today_prob_mean >= recovery_mean_threshold
                    and today_prob_q90 >= recovery_q90_threshold
                )
                if current_recovered:
                    print(
                        "ℹ️  Closed-loop miss-rate is high, but today's predictions have recovered; "
                        "treating this as stale historical drift rather than an active failure"
                    )
                else:
                    msg = (
                        f"⚠️  High miss-rate detected in closed-loop report ({miss_rate:.1%} over {samples} samples) - "
                        "possible probability suppression"
                    )
                    print(msg)
                    silent_failures.append(msg)
                    issues_found += 1
        else:
            print("ℹ️  No closed-loop report found yet")
    except Exception as e:
        print(f"⚠️  Could not evaluate closed-loop miss trend: {e}")

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


def _repo_data_dir():
    """Return the repository data directory regardless of the current working directory."""
    repo_root = Path(__file__).resolve().parent
    candidate = repo_root / 'data'
    if candidate.exists():
        return candidate
    return Path('data')


def send_morning_learning_summary(
    learning_result=None,
    missed_count=0,
    scale_pos_weight=None,
    physics_weight=None,
    kelly_multiplier=None,
):
    """Send a once-per-day Discord summary of what the model learned and changed."""
    today_str = datetime.today().strftime('%Y-%m-%d')
    data_dir = _repo_data_dir()
    marker = data_dir / f'morning_learning_summary_sent_{today_str}.txt'

    if marker.exists() and os.getenv('FORCE_MORNING_LEARNING_SUMMARY', 'false').lower() != 'true':
        return False

    insights = (learning_result or {}).get('insights', {}) if isinstance(learning_result, dict) else {}
    if not isinstance(insights, dict):
        insights = {}

    # Fallback: if in-memory insights are missing, try today's saved learning report,
    # then the most recent prior report if the current-day file is not available yet.
    if not insights:
        report_candidates = [
            data_dir / f"hr_learning_report_{today_str}.json",
            data_dir / f"hr_learning_report_{(datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')}.json",
        ]
        report_candidates.extend(sorted(data_dir.glob('hr_learning_report_*.json'), reverse=True))
        for report_path in report_candidates:
            if not getattr(report_path, 'exists', lambda: False)():
                continue
            try:
                loaded = _json.loads(report_path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    insights = loaded
                    break
            except Exception:
                continue
    yesterday_str = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')

    # Prefer the raw Statcast event count for yesterday's HR total.
    verified_hr_total = None
    try:
        sc_df = load_or_fetch_statcast(yesterday_str)
        if sc_df is not None and not sc_df.empty and 'events' in sc_df.columns:
            verified_hr_total = int((sc_df['events'] == 'home_run').sum())
    except Exception:
        pass
    if verified_hr_total is None:
        feedback_file = data_dir / f'live_feedback_{yesterday_str}.csv'
        if feedback_file.exists():
            try:
                fb_df = pd.read_csv(feedback_file)
                if not fb_df.empty:
                    actual_hr_series = pd.to_numeric(fb_df.get('actual_hr', 1), errors='coerce').fillna(1)
                    verified_hr_total = int((actual_hr_series == 1).sum())
            except Exception:
                pass

    eval_file = data_dir / f'evaluation_{yesterday_str}.csv'
    eval_brier = None
    eval_rows = 0
    eval_candidates = [eval_file]
    if not eval_file.exists():
        eval_candidates.extend(sorted(data_dir.glob('evaluation_*.csv'), reverse=True))
    for candidate in eval_candidates:
        if not getattr(candidate, 'exists', lambda: False)():
            continue
        try:
            eval_df = pd.read_csv(candidate)
            eval_rows = len(eval_df)
            if 'brier_error' in eval_df.columns and not eval_df.empty:
                eval_brier = float(pd.to_numeric(eval_df['brier_error'], errors='coerce').mean())
            eval_file = candidate
            break
        except Exception:
            continue

    if not insights:
        yesterday_eval_path = data_dir / f"evaluation_{(datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')}.csv"
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
        if verified_hr_total is not None:
            lines.append(
                f"Yesterday HRs: {verified_hr_total} | Above 10% model score: {accurate} | Below 10%: {missed}"
            )
        else:
            lines.append(
                f"Yesterday HRs: unavailable | Above 10% model score: {accurate} | Below 10%: {missed}"
            )
    else:
        lines.append("Yesterday reviewed: unavailable (no verified HR feedback loaded)")
    if eval_rows:
        brier_str = f"{eval_brier:.4f}" if eval_brier is not None else 'n/a'
        lines.append(f"Evaluation: {eval_rows} predictions scored | Brier: {brier_str}")
    elif eval_file.exists():
        lines.append("Evaluation: 1 predictions scored")
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
        data_dir.mkdir(parents=True, exist_ok=True)
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
            file_date = _extract_date_from_prefixed_stem(f.stem, 'predictions_')
            if file_date is None:
                continue
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
            d = _extract_date_from_prefixed_stem(f.stem, 'predictions_')
            if d is None:
                continue
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
    work['matched_book_count'] = pd.to_numeric(work.get('matched_book_count', 0), errors='coerce').fillna(0).astype(int)
    work['book_prob_dispersion'] = pd.to_numeric(work.get('book_prob_dispersion', np.nan), errors='coerce')
    if 'market_consensus_ok' in work.columns:
        work['market_consensus_ok'] = work['market_consensus_ok'].fillna(False).astype(bool)
    else:
        work['market_consensus_ok'] = False

    min_leg_prob = max(0.01, min(0.95, _env_float('PARLAY_MIN_LEG_PROB', 0.10)))
    min_books_per_leg = max(1, _env_int('PARLAY_MIN_MATCHED_BOOKS', 2))
    max_leg_dispersion = max(0.0, _env_float('PARLAY_MAX_BOOK_PROB_DISPERSION', 0.10))
    min_parlay_edge_pct = _env_float('PARLAY_EDGE_MIN_PCT', 8.0)
    same_game_corr = max(0.0, min(0.75, _env_float('SGP_BATTER_CORRELATION', 0.25)))

    work['signal_score'] = work['pred_hr_prob'] + np.maximum(work['ev_percent'], 0) / 250.0
    work = work.sort_values(['signal_score', 'pred_hr_prob'], ascending=False).head(max(12, int(candidate_n)))
    work = work[work['pred_hr_prob'] >= min_leg_prob].copy()
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

        corr_boost = 0.0
        if same_game:
            corr_boost = same_game_corr * math.sqrt(max(0.0, p1 * (1.0 - p1) * p2 * (1.0 - p2)))
        true_combo_prob = min(0.95, max(0.0, learned_combo + corr_boost))

        offer = _best_common_book_parlay_offer(
            a.get('raw_book_lines_json', '{}'),
            b.get('raw_book_lines_json', '{}'),
        )

        o1 = _american_to_decimal_safe(offer.get('sportsbook_leg1_odds_american', np.nan))
        o2 = _american_to_decimal_safe(offer.get('sportsbook_leg2_odds_american', np.nan))
        parlay_decimal = (o1 * o2) if (pd.notna(o1) and pd.notna(o2)) else np.nan
        if pd.isna(parlay_decimal):
            parlay_decimal = _safe_float(offer.get('sportsbook_parlay_decimal'), np.nan)
        sportsbook_implied = _safe_float(offer.get('sportsbook_parlay_implied_prob'), np.nan)
        if (sportsbook_implied is None or not np.isfinite(sportsbook_implied)) and pd.notna(parlay_decimal) and parlay_decimal > 0:
            sportsbook_implied = float(1.0 / parlay_decimal)
        parlay_ev = (true_combo_prob * parlay_decimal - 1.0) if pd.notna(parlay_decimal) else np.nan
        parlay_edge_pct = (parlay_ev * 100.0) if pd.notna(parlay_ev) else np.nan

        leg_a_ok = (
            bool(a.get('market_consensus_ok', False))
            and int(a.get('matched_book_count', 0)) >= min_books_per_leg
            and _safe_float(a.get('book_prob_dispersion'), np.inf) <= max_leg_dispersion
        )
        leg_b_ok = (
            bool(b.get('market_consensus_ok', False))
            and int(b.get('matched_book_count', 0)) >= min_books_per_leg
            and _safe_float(b.get('book_prob_dispersion'), np.inf) <= max_leg_dispersion
        )
        pair_market_consensus_ok = bool(leg_a_ok and leg_b_ok)

        parlay_edge_signal = (
            pair_market_consensus_ok
            and int(offer.get('sportsbook_common_books', 0)) >= 1
            and pd.notna(parlay_edge_pct)
            and float(parlay_edge_pct) >= float(min_parlay_edge_pct)
        )

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
            'corr_boost': corr_boost,
            'true_combo_prob': true_combo_prob,
            'leg1_odds_american': offer.get('sportsbook_leg1_odds_american', np.nan),
            'leg2_odds_american': offer.get('sportsbook_leg2_odds_american', np.nan),
            'sportsbook_book': offer.get('sportsbook_book'),
            'sportsbook_common_books': offer.get('sportsbook_common_books', 0),
            'parlay_decimal': parlay_decimal,
            'sportsbook_parlay_implied_prob': sportsbook_implied,
            'parlay_ev': parlay_ev,
            'parlay_edge_pct': parlay_edge_pct,
            'pair_market_consensus_ok': pair_market_consensus_ok,
            'parlay_edge_signal': bool(parlay_edge_signal),
            'training_days_used': int(multipliers.get('training_days_used', 0)),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    out = out.sort_values(
        ['parlay_edge_signal', 'parlay_edge_pct', 'true_combo_prob'],
        ascending=[False, False, False],
        na_position='last'
    ).reset_index(drop=True)
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
_ODDS_API_RATE_LIMIT_UNTIL_TS = 0.0
_ODDS_API_RATE_LIMIT_LAST_LOG_TS = 0.0
_ODDS_API_REQUEST_HISTORY = []


def _odds_provider_name():
    raw = str(os.getenv('ODDS_API_PROVIDER', 'sportsgameodds') or '').strip().lower()
    if raw in {'auto', 'automatic', 'fallback'}:
        return 'auto'
    if raw in {'sgo', 'sportsgameodds', 'sportsgameodds_v2', 'sports_game_odds'}:
        return 'sportsgameodds'
    if raw in {'theoddsapi', 'the-odds-api', 'oddsapi'}:
        return 'theoddsapi'
    if raw in {'sharpapi', 'sharp_api', 'sharp-api'}:
        return 'sharpapi'
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


def _rate_limit_cooldown_active():
    try:
        return time.time() < float(_ODDS_API_RATE_LIMIT_UNTIL_TS)
    except Exception:
        return False


def _set_rate_limit_cooldown(reason_text='', retry_after=None):
    global _ODDS_API_RATE_LIMIT_UNTIL_TS, _ODDS_API_RATE_LIMIT_LAST_LOG_TS
    now_ts = time.time()
    try:
        retry_after = float(retry_after) if retry_after is not None else None
    except Exception:
        retry_after = None
    if retry_after is not None and retry_after > 0:
        cooldown_seconds = max(30, int(round(retry_after)))
    else:
        cooldown_seconds = max(30, int(str(os.getenv('ODDS_API_RATE_LIMIT_COOLDOWN_SECONDS', '120')).strip() or '120'))
    _ODDS_API_RATE_LIMIT_UNTIL_TS = now_ts + cooldown_seconds
    if (now_ts - float(_ODDS_API_RATE_LIMIT_LAST_LOG_TS)) >= cooldown_seconds:
        _ODDS_API_RATE_LIMIT_LAST_LOG_TS = now_ts
        print(
            "Odds provider rate-limited; backing off for "
            f"{int(round(cooldown_seconds))}s. {str(reason_text)[:140]}"
        )


def _parse_retry_after_seconds(resp):
    try:
        retry_after = resp.headers.get('Retry-After') or resp.headers.get('retry-after')
        if not retry_after:
            return None
        try:
            return max(1, int(float(retry_after)))
        except Exception:
            return None
    except Exception:
        return None


def _compute_odds_request_wait_seconds(history, *, max_per_minute=500, now_ts=None):
    """Return how long to wait before the next odds request to stay under a per-minute cap."""
    if not history:
        return 0.0

    now_ts = now_ts if now_ts is not None else time.time()
    window_seconds = 60.0
    trimmed = [ts for ts in history if now_ts - float(ts) <= window_seconds]
    if len(trimmed) < max_per_minute:
        return 0.0

    oldest = min(trimmed)
    wait_for_reset = window_seconds - (now_ts - float(oldest))
    return max(0.0, wait_for_reset + 0.1)


def _throttle_odds_requests(*, max_per_minute=None, now_ts=None):
    """Pause briefly when the odds provider request history is approaching the minute cap."""
    global _ODDS_API_REQUEST_HISTORY
    if max_per_minute is None:
        max_per_minute = max(1, int(str(os.getenv('ODDS_API_MAX_PER_MINUTE', '500')).strip() or '500'))

    now_ts = now_ts if now_ts is not None else time.time()
    _ODDS_API_REQUEST_HISTORY = [ts for ts in _ODDS_API_REQUEST_HISTORY if now_ts - float(ts) <= 60.0]
    wait_seconds = _compute_odds_request_wait_seconds(_ODDS_API_REQUEST_HISTORY, max_per_minute=max_per_minute, now_ts=now_ts)
    if wait_seconds > 0:
        time.sleep(wait_seconds)
        now_ts = time.time()
        _ODDS_API_REQUEST_HISTORY = [ts for ts in _ODDS_API_REQUEST_HISTORY if now_ts - float(ts) <= 60.0]
    _ODDS_API_REQUEST_HISTORY.append(time.time())


def _get_odds_http_response(resp, timeout_seconds, *, url, provider, headers=None, params=None):
    if _rate_limit_cooldown_active():
        return None, None, 'rate_limited'
    _throttle_odds_requests()
    try:
        response = requests.get(url, headers=headers, params=params, timeout=timeout_seconds)
    except Exception as e:
        return None, e, 'request_error'
    if response.status_code == 429:
        retry_after = _parse_retry_after_seconds(response)
        _set_rate_limit_cooldown(reason_text=(getattr(response, 'text', '') or '')[:140], retry_after=retry_after)
        return response, None, 'rate_limited'
    return response, None, 'ok'


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
    if _odds_invalid_key_cooldown_active() or _rate_limit_cooldown_active():
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
            resp, exc, status = _get_odds_http_response(
                None,
                12,
                url=f"{SPORTSGAMEODDS_BASE_URL}/events",
                provider='sportsgameodds',
                headers=headers,
                params=call_params,
            )
            if status != 'ok':
                if status == 'rate_limited':
                    break
                print(f"Sportsgameodds fetch failed: {exc}")
                break
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


def _fetch_hr_props_raw_from_sharpapi(api_key):
    """Fetch MLB HR props from sharpapi.io /odds endpoint (line=0.5 main-line props)."""
    if _odds_invalid_key_cooldown_active() or _rate_limit_cooldown_active():
        return {}
    if requests is None:
        return {}

    headers = {'X-API-Key': str(api_key or '').strip()}
    base_url = 'https://api.sharpapi.io/api/v1/odds'
    limit = 200
    max_pages = max(1, int(float(os.getenv('SHARPAPI_MAX_PAGES', '10'))))

    raw_rows = []
    offset = 0
    cursor = None
    for _ in range(max_pages):
        params = {
            'sport': 'baseball',
            'league': 'MLB',
            'market': 'player_home_runs',
            'is_main_line': 'true',
            'is_active': 'true',
            'limit': str(limit),
        }
        if cursor:
            params['cursor'] = cursor
        else:
            params['offset'] = str(offset)
        try:
            resp = requests.get(base_url, headers=headers, params=params, timeout=15)
        except Exception as exc:
            print(f"SharpAPI fetch error: {exc}")
            break

        if resp.status_code in (401, 403):
            body = (resp.text or '')[:200]
            _set_odds_invalid_key_cooldown(reason_text=body)
            print(f"SharpAPI invalid key ({resp.status_code}): {body}")
            return {}
        if resp.status_code == 429:
            _set_rate_limit_cooldown()
            break
        if resp.status_code != 200:
            print(f"SharpAPI returned {resp.status_code}: {resp.text[:200]}")
            break

        payload = resp.json()
        batch = payload.get('data') or []
        raw_rows.extend(batch)
        pagination = payload.get('pagination', {})
        cursor = pagination.get('next_cursor')
        if not pagination.get('has_more') or not cursor:
            break
        offset += limit  # fallback; cursor takes priority

    if not raw_rows:
        return {}

    # Build {player: {book: american_odds}} keeping best (lowest absolute) odds per book.
    out: dict = {}
    today_local = datetime.today().date()
    allowed_dates = {today_local, today_local + timedelta(days=1)}
    for row in raw_rows:
        if not row.get('is_active', True):
            continue
        # Only keep the "over" (hit at least 1 HR) side; under odds are not HR props.
        if str(row.get('selection_type', 'over')).lower() not in {'over', 'yes', ''}:
            continue
        player = str(row.get('player_name') or row.get('selection') or '').strip()
        book = str(row.get('sportsbook') or 'unknown').strip().lower()
        odds_raw = row.get('odds_american')
        if not player or odds_raw is None:
            continue
        try:
            odds_i = int(round(float(odds_raw)))
        except Exception:
            continue
        # HR over props must be positive American odds (underdogs); negative = under/favorite side.
        if odds_i <= 0 or odds_i < 100 or odds_i > 15000:
            continue
        # Date guard — reject stale props
        start_raw = row.get('event_start_time', '')
        if start_raw:
            try:
                ev_date = datetime.fromisoformat(start_raw.replace('Z', '+00:00')).date()
                if ev_date not in allowed_dates:
                    continue
            except Exception:
                pass
        out.setdefault(player, {})[book] = odds_i

    if out:
        n_pairs = sum(len(v) for v in out.values())
        print(f"SharpAPI HR props: {n_pairs} book-player pairs across {len(out)} players")
    else:
        print("SharpAPI returned no recognized HR prop outcomes in current slate.")
    return out


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
    if _odds_invalid_key_cooldown_active() or _rate_limit_cooldown_active():
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
            resp, exc, status = _get_odds_http_response(
                None,
                10,
                url=top_url,
                provider='theoddsapi',
            )
            if status != 'ok':
                if status == 'rate_limited':
                    return aggregated
                print(f"Odds API top-level fetch failed ({market_key}): {exc}")
                continue
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
            events_resp, exc, status = _get_odds_http_response(
                None,
                10,
                url=events_url,
                provider='theoddsapi',
            )
            if status != 'ok':
                if status == 'rate_limited':
                    return aggregated
                print(f"Odds API events list failed: {exc}")
                continue
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
                ev_resp, exc, status = _get_odds_http_response(
                    None,
                    10,
                    url=ev_url,
                    provider='theoddsapi',
                )
                if status != 'ok':
                    if status == 'rate_limited':
                        return aggregated
                    continue
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


def _get_odds_cache_path():
    configured = str(os.getenv('ODDS_CACHE_FILE', '') or '').strip()
    if configured:
        return Path(configured)
    return Path('data') / 'latest_hr_prop_odds_cache.json'


def _load_cached_hr_prop_odds_payload(cache_path=None):
    """Return cached odds payload and age in seconds, if available."""
    path = Path(cache_path or _get_odds_cache_path())
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(payload, dict) and 'raw' in payload:
            raw = payload.get('raw')
            timestamp = payload.get('timestamp')
            try:
                ts = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
                age_seconds = (datetime.now(ts.tzinfo) - ts).total_seconds()
            except Exception:
                age_seconds = None
            return raw, age_seconds
    except Exception:
        return None, None
    return None, None


def _save_cached_hr_prop_odds_payload(payload, cache_path=None):
    """Persist odds payload to disk for fallback use."""
    path = Path(cache_path or _get_odds_cache_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'timestamp': datetime.now().isoformat(),
        'raw': payload,
    }
    path.write_text(json.dumps(record, indent=2), encoding='utf-8')


def _build_devigged_probs_from_raw_books(player_all_odds):
    """Build consensus no-vig probabilities from per-book American odds."""
    player_probs = {}
    for name, book_odds in (player_all_odds or {}).items():
        implied_probs = []
        for bk, odds in (book_odds or {}).items():
            try:
                odds = float(odds)
                raw_implied = abs(odds) / (abs(odds) + 100) if odds < 0 else 100 / (odds + 100)
                if raw_implied > 0 and np.isfinite(raw_implied):
                    implied_probs.append(float(raw_implied))
            except Exception:
                continue
        if not implied_probs:
            continue

        # For one-sided player props across books, probabilities should not be
        # normalized across books (that creates artificial ~1/N baselines).
        # Use a robust central estimate of implied probability instead.
        if len(implied_probs) == 1:
            final_prob = float(implied_probs[0])
        else:
            final_prob = float(np.median(implied_probs))
        player_probs[name] = round(float(np.clip(final_prob, 0.0, 1.0)), 4)
    return player_probs


def fetch_hr_prop_odds():
    """Fetch live HR prop lines and return {player_name: devigged_prob}.

    The base model should primarily rely on free sources such as Baseball Savant,
    Statcast, and local snapshots. The paid odds API is treated as an optional
    overlay for live market odds rather than a prerequisite for model generation.

    Provider is selected by ODDS_API_PROVIDER:
    - sportsgameodds (default)
    - theoddsapi
    """
    api_key = os.getenv('ODDS_API_KEY')
    provider = _odds_provider_name()
    use_free_fallback = str(os.getenv('ODDS_USE_FREE_FALLBACK', 'true')).strip().lower() not in {'0', 'false', 'no'}
    prefer_free_sources = str(os.getenv('ODDS_PREFER_FREE_SOURCES', 'true')).strip().lower() not in {'0', 'false', 'no'}

    def _try_free_fallback(label=''):
        if load_free_odds_sources is not None and build_devigged_probs_from_books is not None:
            raw_free = load_free_odds_sources()
            if raw_free:
                probs = build_devigged_probs_from_books(raw_free)
                if label:
                    print(f"{label}: {sum(len(v) for v in raw_free.values())} book-player pairs across {len(probs)} players")
                else:
                    print(f"Free source odds fallback: {sum(len(v) for v in raw_free.values())} book-player pairs across {len(probs)} players")
                return probs
        return {}

    free_probs = _try_free_fallback('Free source odds') if prefer_free_sources else {}
    if free_probs:
        return free_probs

    if not api_key:
        return free_probs

    if _odds_invalid_key_cooldown_active() or _rate_limit_cooldown_active():
        cached_raw, cached_age = _load_cached_hr_prop_odds_payload()
        if cached_raw:
            print(f"Using cached odds payload during cooldown ({cached_age} seconds old)")
            probs = _build_devigged_probs_from_raw_books(cached_raw)
            if probs:
                return probs
        if use_free_fallback:
            return _try_free_fallback('Rate-limit fallback')
        return {}

    def _try_provider(provider_name):
        if provider_name == 'theoddsapi':
            return _fetch_hr_props_raw_from_odds_api(api_key)
        if provider_name == 'sportsgameodds':
            return _fetch_hr_props_raw_from_sportsgameodds(api_key)
        if provider_name == 'sharpapi':
            return _fetch_hr_props_raw_from_sharpapi(api_key)
        return {}

    try:
        candidates = []
        if provider in {'auto', 'fallback'}:
            candidates = ['sharpapi', 'sportsgameodds', 'theoddsapi']
        else:
            candidates = [provider]

        last_empty = None
        for candidate in candidates:
            player_all_odds = _try_provider(candidate)
            if player_all_odds:
                _save_cached_hr_prop_odds_payload(player_all_odds)
                probs = _build_devigged_probs_from_raw_books(player_all_odds)
                if probs:
                    print(f"Odds provider active: {candidate} ({len(probs)} players with devigged prices)")
                    return probs
                last_empty = candidate
            else:
                last_empty = candidate

        cached_raw, cached_age = _load_cached_hr_prop_odds_payload()
        if cached_raw:
            print(f"Using cached odds payload ({cached_age} seconds old)")
            probs = _build_devigged_probs_from_raw_books(cached_raw)
            if probs:
                return probs
        if use_free_fallback:
            return _try_free_fallback(f'Provider fallback after {last_empty or provider}')
        return {}
    except Exception as e:
        print(f"Odds fetch failed ({provider}): {e}")
        if use_free_fallback:
            return _try_free_fallback('Provider error fallback')
        return {}


def fetch_hr_prop_odds_raw():
    """Fetch HR prop lines from ALL sportsbooks. Returns {player: {book_key: american_odds}}.
    Used for RLM monitoring and per-book line movement tracking.

    Free-source odds are preferred first. The paid odds API is used only as an
    overlay when the free sources are absent or stale.
    """
    api_key = os.getenv('ODDS_API_KEY')
    provider = _odds_provider_name()
    use_free_fallback = str(os.getenv('ODDS_USE_FREE_FALLBACK', 'true')).strip().lower() not in {'0', 'false', 'no'}
    prefer_free_sources = str(os.getenv('ODDS_PREFER_FREE_SOURCES', 'true')).strip().lower() not in {'0', 'false', 'no'}

    if prefer_free_sources and load_free_odds_sources is not None:
        raw_free = load_free_odds_sources()
        if raw_free:
            return raw_free

    if not api_key:
        if load_free_odds_sources is not None:
            return load_free_odds_sources()
        return {}
    if _odds_invalid_key_cooldown_active() or _rate_limit_cooldown_active():
        cached_raw, cached_age = _load_cached_hr_prop_odds_payload()
        if cached_raw and not _is_placeholder_raw_odds_payload(cached_raw):
            print(f"Using cached raw odds payload during cooldown ({cached_age} seconds old)")
            return cached_raw
        if cached_raw:
            print("Ignoring placeholder raw odds cache during cooldown")
        snapshot_raw = _load_latest_odds_snapshot_payload()
        if snapshot_raw:
            print("Using latest snapshot raw odds payload during cooldown")
            return snapshot_raw
        if use_free_fallback and load_free_odds_sources is not None:
            return load_free_odds_sources()
        return {}
    try:
        if provider == 'theoddsapi':
            raw = _fetch_hr_props_raw_from_odds_api(api_key)
        elif provider == 'sharpapi':
            raw = _fetch_hr_props_raw_from_sharpapi(api_key)
        else:
            raw = _fetch_hr_props_raw_from_sportsgameodds(api_key)
        if raw and _is_placeholder_raw_odds_payload(raw):
            print("Ignoring placeholder raw odds payload from provider")
            raw = {}
        if not raw:
            cached_raw, cached_age = _load_cached_hr_prop_odds_payload()
            if cached_raw and not _is_placeholder_raw_odds_payload(cached_raw):
                print(f"Using cached raw odds payload ({cached_age} seconds old)")
                return cached_raw
            if cached_raw:
                print("Ignoring placeholder raw odds cache")
            snapshot_raw = _load_latest_odds_snapshot_payload()
            if snapshot_raw:
                print("Using latest snapshot raw odds payload")
                return snapshot_raw
            if load_free_odds_sources is not None:
                return load_free_odds_sources()
            return {}
        if not _is_placeholder_raw_odds_payload(raw):
            _save_cached_hr_prop_odds_payload(raw)
        return raw
    except Exception as e:
        print(f"Odds raw fetch failed ({provider}): {e}")
        if use_free_fallback and load_free_odds_sources is not None:
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
    start_hour = _safe_int(os.getenv('MARKET_RELEASE_START_HOUR_ET', '9'), 9) or 9
    end_hour = _safe_int(os.getenv('MARKET_RELEASE_END_HOUR_ET', '11'), 11) or 11
    end_minute = _safe_int(os.getenv('MARKET_RELEASE_END_MINUTE_ET', '0'), 0) or 0

    current_minutes = now_et.hour * 60 + now_et.minute
    start_minutes = int(start_hour) * 60
    end_minutes = int(end_hour) * 60 + int(end_minute)

    return start_minutes <= current_minutes <= end_minutes


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


def _book_map_consensus_implied_prob(book_map, preferred_books=None):
    """Return (median_implied_prob, count) from a sportsbook map.

    Uses implied probability, not raw American-odds points, to keep math stable
    across positive/negative line ranges.
    """
    if not isinstance(book_map, dict) or not book_map:
        return None, 0

    preferred = None
    if preferred_books is not None:
        preferred = {str(b).strip().lower() for b in preferred_books if str(b).strip()}

    implied_probs = []
    for bk, odds in book_map.items():
        bk_norm = str(bk).strip().lower()
        if preferred is not None and bk_norm not in preferred:
            continue
        o = _safe_float(odds)
        if o is None or not np.isfinite(o):
            continue
        try:
            p = float(american_to_implied_prob(o))
        except Exception:
            continue
        if np.isfinite(p) and 0.0 < p < 1.0:
            implied_probs.append(p)

    if not implied_probs:
        return None, 0
    return float(np.median(implied_probs)), int(len(implied_probs))


def _book_map_mean_implied_delta(curr_map, prev_map, preferred_books=None):
    """Return (mean_delta, n_books, agreement_ratio) in implied-probability space."""
    if not isinstance(curr_map, dict) or not isinstance(prev_map, dict):
        return None, 0, 0.0

    preferred = None
    if preferred_books is not None:
        preferred = {str(b).strip().lower() for b in preferred_books if str(b).strip()}

    deltas = []
    for bk, curr_odds in curr_map.items():
        bk_norm = str(bk).strip().lower()
        if preferred is not None and bk_norm not in preferred:
            continue
        if bk not in prev_map:
            continue
        prev_odds = prev_map.get(bk)
        try:
            curr_p = float(american_to_implied_prob(curr_odds))
            prev_p = float(american_to_implied_prob(prev_odds))
        except Exception:
            continue
        if not (np.isfinite(curr_p) and np.isfinite(prev_p)):
            continue
        deltas.append(curr_p - prev_p)

    if not deltas:
        return None, 0, 0.0

    pos = sum(1 for d in deltas if d > 0)
    neg = sum(1 for d in deltas if d < 0)
    agreement = max(pos, neg) / max(1, len(deltas))
    return float(np.mean(deltas)), int(len(deltas)), float(agreement)


def detect_rlm(current_odds, previous_odds, watch_batters):
    """Detect reverse line movement or sharp/public divergence on watched batters.
    Returns list of (batter_name, sharp_move, square_move, signal) tuples."""
    alerts = []
    rlm_min_books = max(1, _env_int('LINE_MOVE_MIN_BOOKS', 2))
    rlm_min_sharp_delta_pts = max(0.10, _env_float('RLM_MIN_SHARP_DELTA_PTS', 1.0))
    rlm_min_divergence_pts = max(0.20, _env_float('RLM_MIN_DIVERGENCE_PTS', 1.8))
    steam_min_mean_delta_pts = max(0.20, _env_float('STEAM_MIN_MEAN_DELTA_PTS', 1.2))
    steam_min_agreement = float(np.clip(_env_float('STEAM_MIN_BOOK_AGREEMENT', 0.65), 0.50, 1.0))

    for batter in watch_batters:
        curr = current_odds.get(batter)
        prev = previous_odds.get(batter)
        if not curr or not prev:
            continue

        sharp_delta, sharp_n, _ = _book_map_mean_implied_delta(curr, prev, preferred_books=SHARP_BOOKS)
        square_delta, square_n, _ = _book_map_mean_implied_delta(curr, prev, preferred_books=SQUARE_BOOKS)
        all_delta, all_n, all_agreement = _book_map_mean_implied_delta(curr, prev, preferred_books=None)

        if all_delta is None or all_n < rlm_min_books:
            continue

        sharp_pts = (sharp_delta or 0.0) * 100.0
        square_pts = (square_delta or 0.0) * 100.0
        all_pts = all_delta * 100.0

        if sharp_delta is not None and square_delta is not None and sharp_n >= rlm_min_books and square_n >= rlm_min_books:
            divergence_pts = (sharp_delta - square_delta) * 100.0
            if (
                (sharp_delta * square_delta) < 0
                and abs(sharp_pts) >= rlm_min_sharp_delta_pts
                and abs(divergence_pts) >= rlm_min_divergence_pts
            ):
                signal = (
                    f"RLM — sharp Δ {sharp_pts:+.2f} pts ({sharp_n} books) | "
                    f"public Δ {square_pts:+.2f} pts ({square_n} books) | "
                    f"divergence {divergence_pts:+.2f} pts"
                )
                alerts.append((batter, sharp_pts, square_pts, signal))
                continue

        if abs(all_pts) >= steam_min_mean_delta_pts and all_agreement >= steam_min_agreement:
            direction = 'shortening' if all_pts > 0 else 'drifting'
            signal = (
                f"STEAM — consensus implied Δ {all_pts:+.2f} pts across {all_n} books "
                f"({all_agreement*100:.0f}% agreement, {direction})"
            )
            alerts.append((batter, all_pts, all_pts, signal))

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
    prob_metrics = calculate_probability_metrics(merged['actual_hr'], merged['pred_hr_prob'])
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
    print(f"Log-loss: {prob_metrics['log_loss']:.4f}")
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
            {"name": "Log-loss", "value": f"{prob_metrics['log_loss']:.4f}", "inline": True},
            {"name": "Top 10 HR rate", "value": f"{top_10_rate:.3f} ({int(top_10_hits)} HRs)", "inline": True},
            {"name": "Top 3 predictions", "value": "\n".join(top_rows) or "No top predictions available", "inline": False}
        ],
        "footer": {"text": "MLB HR Handicapper evaluation summary"},
        "timestamp": datetime.now().isoformat()
    }

    if os.getenv("DISCORD_NOTIFY_EVAL", "false").lower() == "true":
        send_discord_webhook(embeds=[embed])

    return merged


def _nightly_accuracy_marker_path(target_date):
    return Path('data') / f'nightly_accuracy_summary_sent_{target_date}.txt'


def _compute_nightly_accuracy_grade(merged_df):
    """Return nightly grade diagnostics from evaluated predictions."""
    if merged_df is None or merged_df.empty:
        return {
            'grade': 'N/A',
            'score': 0.0,
            'brier': np.nan,
            'log_loss': np.nan,
            'actual_rate': np.nan,
            'pred_rate': np.nan,
            'calibration_gap': np.nan,
            'top10_rate': np.nan,
        }

    work = merged_df.copy()
    p = pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0).clip(0.0, 1.0)
    y = pd.to_numeric(work.get('actual_hr', 0.0), errors='coerce').fillna(0.0).clip(0.0, 1.0)

    brier = float(np.mean((p - y) ** 2)) if len(work) else np.nan
    try:
        log_loss_val = float(calculate_probability_metrics(y, p).get('log_loss', np.nan))
    except Exception:
        log_loss_val = np.nan

    actual_rate = float(y.mean()) if len(work) else np.nan
    pred_rate = float(p.mean()) if len(work) else np.nan
    calibration_gap = float(abs(pred_rate - actual_rate)) if np.isfinite(pred_rate) and np.isfinite(actual_rate) else np.nan

    top10 = work.assign(_p=p.values).sort_values('_p', ascending=False).head(10)
    top10_rate = float(pd.to_numeric(top10.get('actual_hr', 0.0), errors='coerce').fillna(0.0).mean()) if not top10.empty else np.nan

    score = 100.0
    if np.isfinite(brier):
        if brier > 0.12:
            score -= 35
        elif brier > 0.09:
            score -= 25
        elif brier > 0.07:
            score -= 15
        elif brier > 0.05:
            score -= 8

    if np.isfinite(calibration_gap):
        if calibration_gap > 0.12:
            score -= 30
        elif calibration_gap > 0.08:
            score -= 20
        elif calibration_gap > 0.05:
            score -= 12
        elif calibration_gap > 0.03:
            score -= 6

    if np.isfinite(top10_rate) and np.isfinite(actual_rate) and top10_rate < actual_rate:
        score -= 10

    score = float(np.clip(score, 0.0, 100.0))
    if score >= 90:
        grade = 'A'
    elif score >= 80:
        grade = 'B'
    elif score >= 70:
        grade = 'C'
    elif score >= 60:
        grade = 'D'
    else:
        grade = 'F'

    return {
        'grade': grade,
        'score': score,
        'brier': brier,
        'log_loss': log_loss_val,
        'actual_rate': actual_rate,
        'pred_rate': pred_rate,
        'calibration_gap': calibration_gap,
        'top10_rate': top10_rate,
    }


def send_nightly_accuracy_summary(date_str=None, webhook_url=None, predicted_threshold=None):
    """Send end-of-night accuracy summary with full HR list and predicted counts."""
    target_date = date_str or (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
    marker = _nightly_accuracy_marker_path(target_date)

    if marker.exists() and os.getenv('FORCE_NIGHTLY_ACCURACY_SUMMARY', 'false').lower() != 'true':
        return False

    threshold = float(predicted_threshold if predicted_threshold is not None else _env_float('NIGHTLY_PREDICTED_THRESHOLD', 0.15))
    threshold = float(np.clip(threshold, 0.01, 0.50))

    merged = evaluate_saved_predictions(target_date)
    if merged is None or merged.empty:
        print(f"Nightly summary skipped: no evaluation data for {target_date}")
        return False

    hrs = merged[merged['actual_hr'] == 1].copy()
    if hrs.empty:
        lines = [
            f"**🌙 Nightly HR Accuracy — {target_date}**",
            "Total HRs: 0",
            "Predicted by model: 0",
            "No home runs were recorded in the scored outcomes.",
        ]
        sent = send_discord_webhook(content="\n".join(lines), webhook_url=webhook_url, async_send=False)
        if sent:
            Path('data').mkdir(parents=True, exist_ok=True)
            marker.write_text(datetime.now().isoformat(), encoding='utf-8')
        return sent

    hrs['pred_hr_prob'] = pd.to_numeric(hrs.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0)
    hrs = hrs.sort_values('pred_hr_prob', ascending=False).reset_index(drop=True)
    hrs['predicted_flag'] = hrs['pred_hr_prob'] >= threshold

    grade_diag = _compute_nightly_accuracy_grade(merged)

    total_hrs = int(len(hrs))
    predicted_count = int(hrs['predicted_flag'].sum())
    hit_rate = (predicted_count / total_hrs) if total_hrs > 0 else 0.0

    lines = [
        f"**🌙 Nightly HR Accuracy — {target_date}**",
        f"Nightly Grade: {grade_diag.get('grade', 'N/A')} ({grade_diag.get('score', 0.0):.1f}/100)",
        (
            f"Brier {grade_diag.get('brier', np.nan):.4f} | "
            f"LogLoss {grade_diag.get('log_loss', np.nan):.4f} | "
            f"Cal Gap {grade_diag.get('calibration_gap', np.nan)*100:.2f} pts"
        ),
        f"Total HRs: {total_hrs}",
        f"Predicted by model (>= {threshold * 100:.0f}%): {predicted_count}/{total_hrs} ({hit_rate * 100:.1f}%)",
        "Full HR list:",
    ]

    for idx, row in hrs.iterrows():
        batter = str(row.get('batter_name', 'Unknown'))
        pitcher = str(row.get('pitcher_name', 'Unknown'))
        prob = float(row.get('pred_hr_prob', 0.0) or 0.0)
        status = 'PREDICTED' if bool(row.get('predicted_flag')) else 'MISSED'
        lines.append(f"{idx + 1}. {batter} vs {pitcher} — {prob * 100:.1f}% ({status})")

    sent = send_discord_webhook(content="\n".join(lines), webhook_url=webhook_url, async_send=False)
    if sent:
        Path('data').mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().isoformat(), encoding='utf-8')
    return sent

# =====================================================================
# SECTION 2: ADAPTIVE HISTORICAL FEATURES SOURCING
# =====================================================================
def load_historical_statcast_seasons(
    start_year=None,
    end_year=None,
    hist_dir='data/historical',
    min_year=2019,
):
    """Load multi-year Statcast seasons from parquet cache, fetching missing years on first run.

    Files are stored as data/historical/statcast_{year}.parquet.
    Fetching one full season via pybaseball takes ~5-15 minutes and runs once per year.
    Set HIST_SEASONS_ENABLED=false to disable (default: enabled when cache exists).
    """
    enabled = str(os.getenv('HIST_SEASONS_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
    if not enabled:
        return pd.DataFrame()

    try:
        import pyarrow  # noqa: F401
    except ImportError:
        print("ℹ️ Historical seasons disabled: install pyarrow for parquet support (pip install pyarrow).")
        return pd.DataFrame()

    Path(hist_dir).mkdir(parents=True, exist_ok=True)
    today = datetime.today()
    if end_year is None:
        end_year = today.year - 1  # previous complete season
    lookback_seasons = max(1, int(os.getenv('HIST_SEASONS_LOOKBACK', '2')))
    if start_year is None:
        start_year = max(min_year, int(os.getenv('HIST_SEASONS_START_YEAR', str(end_year - (lookback_seasons - 1)))))
    start_year = max(min_year, int(start_year))
    hist_rows_per_year_cap = max(0, int(os.getenv('HIST_ROWS_PER_YEAR', '200000')))

    def _sanitize_for_concat(df_in):
        """Convert extension dtypes to numpy/object to avoid concat mask blowups."""
        if df_in is None or df_in.empty:
            return df_in
        df_out = df_in.copy()
        for col in df_out.columns:
            s = df_out[col]
            if pd.api.types.is_extension_array_dtype(s.dtype):
                if str(s.dtype) == 'boolean':
                    df_out[col] = s.fillna(False).astype(bool)
                elif pd.api.types.is_numeric_dtype(s.dtype):
                    df_out[col] = pd.to_numeric(s, errors='coerce')
                else:
                    df_out[col] = s.astype('object')
        return df_out

    def _trim_year_frame(df_in, keep_rows):
        """Keep the most recent rows from a season frame for low-memory fallback."""
        if df_in is None or df_in.empty or len(df_in) <= keep_rows:
            return df_in
        if 'game_date' in df_in.columns:
            tmp = df_in.copy()
            tmp['game_date'] = pd.to_datetime(tmp['game_date'], errors='coerce')
            tmp = tmp.sort_values('game_date')
            return tmp.tail(keep_rows).copy()
        return df_in.tail(keep_rows).copy()

    def _apply_year_row_cap(df_in):
        """Apply an always-on per-season cap for runtime/memory control."""
        if hist_rows_per_year_cap <= 0:
            return df_in
        return _trim_year_frame(df_in, hist_rows_per_year_cap)

    frames = []
    for year in range(start_year, end_year + 1):
        pq_path = Path(hist_dir) / f'statcast_{year}.parquet'
        if pq_path.exists():
            try:
                df_yr = pd.read_parquet(pq_path)
                df_yr = _sanitize_for_concat(df_yr)
                _pre_cap = len(df_yr)
                df_yr = _apply_year_row_cap(df_yr)
                frames.append(df_yr)
                if len(df_yr) < _pre_cap:
                    print(f"  Historical {year}: capped to {len(df_yr):,} rows for speed")
                print(f"  Historical {year}: {len(df_yr):,} rows loaded from cache")
                continue
            except Exception as exc:
                print(f"  Historical {year}: cache corrupt, re-fetching ({exc})")

        # One-time fetch — this is slow (~10-15 min per season) but runs once.
        print(f"  Historical {year}: fetching from Baseball Savant (one-time, ~10-15 min)...")
        try:
            chunks = []
            start_dt = f"{year}-03-01"
            end_dt = f"{year}-11-30"
            season_df = statcast_with_timeout(start_dt=start_dt, end_dt=end_dt, timeout_seconds=1800)
            if season_df is not None and not season_df.empty:
                season_df.to_parquet(pq_path, index=False)
                season_df = _sanitize_for_concat(season_df)
                _pre_cap = len(season_df)
                season_df = _apply_year_row_cap(season_df)
                frames.append(season_df)
                if len(season_df) < _pre_cap:
                    print(f"  Historical {year}: capped to {len(season_df):,} rows for speed")
                print(f"  Historical {year}: {len(season_df):,} rows fetched and cached")
            else:
                print(f"  Historical {year}: no data returned")
        except Exception as exc:
            print(f"  Historical {year}: fetch failed ({exc})")

    if not frames:
        return pd.DataFrame()

    try:
        hist = pd.concat(frames, ignore_index=True)
    except MemoryError:
        fallback_rows = max(50000, int(os.getenv('HIST_FALLBACK_ROWS_PER_YEAR', '150000')))
        print(
            "  Warning: low memory while combining historical seasons; "
            f"retrying with last {fallback_rows:,} rows per year"
        )
        trimmed_frames = [_trim_year_frame(_sanitize_for_concat(f), fallback_rows) for f in frames]
        trimmed_frames = [f for f in trimmed_frames if f is not None and not f.empty]
        if not trimmed_frames:
            return pd.DataFrame()
        hist = pd.concat(trimmed_frames, ignore_index=True)
    print(f"✅ Historical seasons {start_year}-{end_year}: {len(hist):,} total rows loaded")
    return hist


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

    recent_df = pd.concat(all_days_data, ignore_index=True)

    # Merge multi-year historical data when available, with time-decay weighting.
    hist_df = load_historical_statcast_seasons()
    if not hist_df.empty:
        # Assign sample weights: recent rows = 1.0, historical rows decay by year.
        hist_df['game_date'] = pd.to_datetime(hist_df.get('game_date', pd.NaT), errors='coerce')
        today_ts = pd.Timestamp(today.date())
        days_old = (today_ts - hist_df['game_date']).dt.days.clip(lower=0).fillna(730)
        # Half-life 180 days so last-season data retains ~50% weight.
        hist_df['_sample_weight'] = np.exp(-np.log(2) * days_old / 180.0).clip(lower=0.10, upper=1.0)
        recent_df['_sample_weight'] = 1.0
        df = pd.concat([recent_df, hist_df], ignore_index=True)
        # Deduplicate: keep the more recent copy of any (game_pk, batter, pitcher) combination.
        for _id_col in ['game_pk', 'batter', 'pitcher']:
            if _id_col in df.columns:
                df[_id_col] = pd.to_numeric(df[_id_col], errors='coerce')
        df = df.sort_values('_sample_weight', ascending=False).drop_duplicates(
            subset=['game_pk', 'batter', 'pitcher'], keep='first'
        ) if {'game_pk', 'batter', 'pitcher'}.issubset(df.columns) else df
        print(f"✅ Combined dataset: {len(df):,} rows (recent + {len(hist_df):,} historical after dedup)")
    else:
        df = recent_df
        df['_sample_weight'] = 1.0

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

    # Rolling Statcast velocity windows: short-form trend vs longer baseline.
    recent_cutoff = pd.Timestamp(datetime.today().date()) - pd.Timedelta(days=14)
    baseline_cutoff = pd.Timestamp(datetime.today().date()) - pd.Timedelta(days=365)
    recent_slice = pa_df[pa_df['game_date'] >= recent_cutoff].copy()
    baseline_slice = pa_df[pa_df['game_date'] >= baseline_cutoff].copy()

    recent_batter = recent_slice.groupby('batter').agg(
        bat_barrel_rate_14d=('is_barrel', 'mean'),
        bat_hard_hit_rate_14d=('is_hard_hit', 'mean'),
    ).reset_index() if not recent_slice.empty else pd.DataFrame(columns=['batter', 'bat_barrel_rate_14d', 'bat_hard_hit_rate_14d'])

    baseline_batter = baseline_slice.groupby('batter').agg(
        bat_barrel_rate_365d=('is_barrel', 'mean'),
        bat_hard_hit_rate_365d=('is_hard_hit', 'mean'),
    ).reset_index() if not baseline_slice.empty else pd.DataFrame(columns=['batter', 'bat_barrel_rate_365d', 'bat_hard_hit_rate_365d'])

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

    # HR rate split by opposing pitcher handedness (RHP vs LHP).
    pa_df['p_throws'] = pa_df.get('p_throws', pd.Series(['R'] * len(pa_df), index=pa_df.index)).fillna('R')
    _vs_rhp = pa_df[pa_df['p_throws'] == 'R'].groupby('batter', as_index=False).agg(
        bat_hr_rate_vs_rhp=('is_hr', 'mean'), bat_pa_vs_rhp=('is_hr', 'count'))
    _vs_lhp = pa_df[pa_df['p_throws'] == 'L'].groupby('batter', as_index=False).agg(
        bat_hr_rate_vs_lhp=('is_hr', 'mean'), bat_pa_vs_lhp=('is_hr', 'count'))
    batter_stats = batter_stats.merge(_vs_rhp[['batter', 'bat_hr_rate_vs_rhp']], on='batter', how='left')
    batter_stats = batter_stats.merge(_vs_lhp[['batter', 'bat_hr_rate_vs_lhp']], on='batter', how='left')
    batter_stats['bat_hr_rate_vs_rhp'] = batter_stats['bat_hr_rate_vs_rhp'].fillna(batter_stats['bat_hr_rate'])
    batter_stats['bat_hr_rate_vs_lhp'] = batter_stats['bat_hr_rate_vs_lhp'].fillna(batter_stats['bat_hr_rate'])

    # Days since batter last hit a HR (recency/drought signal).
    _today_ts = pd.Timestamp(datetime.today().date())
    _last_hr = pa_df[pa_df['is_hr'] == 1].groupby('batter')['game_date'].max().reset_index()
    _last_hr['bat_days_since_last_hr'] = (_today_ts - _last_hr['game_date']).dt.days.clip(0, 60)
    batter_stats = batter_stats.merge(_last_hr[['batter', 'bat_days_since_last_hr']], on='batter', how='left')
    batter_stats['bat_days_since_last_hr'] = batter_stats['bat_days_since_last_hr'].fillna(30)

    batter_stats = batter_stats.merge(recent_batter, on='batter', how='left')

    batter_stats = batter_stats.merge(baseline_batter, on='batter', how='left')
    batter_stats['bat_barrel_rate_14d'] = pd.to_numeric(batter_stats.get('bat_barrel_rate_14d', np.nan), errors='coerce').fillna(batter_stats['bat_barrel_rate'])
    batter_stats['bat_hard_hit_rate_14d'] = pd.to_numeric(batter_stats.get('bat_hard_hit_rate_14d', np.nan), errors='coerce').fillna(batter_stats['bat_hard_hit_rate'])
    batter_stats['bat_barrel_rate_365d'] = pd.to_numeric(batter_stats.get('bat_barrel_rate_365d', np.nan), errors='coerce').fillna(batter_stats['bat_barrel_rate'])
    batter_stats['bat_hard_hit_rate_365d'] = pd.to_numeric(batter_stats.get('bat_hard_hit_rate_365d', np.nan), errors='coerce').fillna(batter_stats['bat_hard_hit_rate'])
    batter_stats['bat_barrel_trend_delta'] = (batter_stats['bat_barrel_rate_14d'] - batter_stats['bat_barrel_rate_365d']).clip(-0.25, 0.25)
    batter_stats['bat_hard_hit_trend_delta'] = (batter_stats['bat_hard_hit_rate_14d'] - batter_stats['bat_hard_hit_rate_365d']).clip(-0.35, 0.35)
    
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

    recent_pitch = recent_slice.groupby('pitcher').agg(
        pitch_barrel_allowed_14d=('is_barrel', 'mean'),
        pitch_hard_hit_allowed_14d=('is_hard_hit', 'mean'),
    ).reset_index() if not recent_slice.empty else pd.DataFrame(columns=['pitcher', 'pitch_barrel_allowed_14d', 'pitch_hard_hit_allowed_14d'])
    baseline_pitch = baseline_slice.groupby('pitcher').agg(
        pitch_barrel_allowed_365d=('is_barrel', 'mean'),
        pitch_hard_hit_allowed_365d=('is_hard_hit', 'mean'),
    ).reset_index() if not baseline_slice.empty else pd.DataFrame(columns=['pitcher', 'pitch_barrel_allowed_365d', 'pitch_hard_hit_allowed_365d'])
    pitcher_stats = pitcher_stats.merge(recent_pitch, on='pitcher', how='left')
    pitcher_stats = pitcher_stats.merge(baseline_pitch, on='pitcher', how='left')
    pitcher_stats['pitch_barrel_allowed_14d'] = pd.to_numeric(pitcher_stats.get('pitch_barrel_allowed_14d', np.nan), errors='coerce').fillna(pitcher_stats['pitch_barrel_allowed_rate'])
    pitcher_stats['pitch_hard_hit_allowed_14d'] = pd.to_numeric(pitcher_stats.get('pitch_hard_hit_allowed_14d', np.nan), errors='coerce').fillna(pitcher_stats['pitch_hard_hit_allowed_rate'])
    pitcher_stats['pitch_barrel_allowed_365d'] = pd.to_numeric(pitcher_stats.get('pitch_barrel_allowed_365d', np.nan), errors='coerce').fillna(pitcher_stats['pitch_barrel_allowed_rate'])
    pitcher_stats['pitch_hard_hit_allowed_365d'] = pd.to_numeric(pitcher_stats.get('pitch_hard_hit_allowed_365d', np.nan), errors='coerce').fillna(pitcher_stats['pitch_hard_hit_allowed_rate'])
    pitcher_stats['pitch_barrel_trend_delta'] = (pitcher_stats['pitch_barrel_allowed_14d'] - pitcher_stats['pitch_barrel_allowed_365d']).clip(-0.25, 0.25)
    pitcher_stats['pitch_hard_hit_trend_delta'] = (pitcher_stats['pitch_hard_hit_allowed_14d'] - pitcher_stats['pitch_hard_hit_allowed_365d']).clip(-0.35, 0.35)
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

        # Resolve probable pitcher names to MLBAM IDs for Statcast feature lookup.
        def _resolve_pitcher_id(name):
            if not name or name in ('Unknown Pitcher', 'TBD', ''):
                return None
            try:
                results = statsapi.lookup_player(name, gameType='R')
                if results:
                    return int(results[0]['id'])
            except Exception:
                pass
            return None
        probable_home_pitcher_id = _resolve_pitcher_id(probable_home_pitcher)
        probable_away_pitcher_id = _resolve_pitcher_id(probable_away_pitcher)
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

            roof_status, roof_sealed = infer_roof_status(venue_name, raw_game_payload=raw_game, weather=weather)
            if roof_sealed:
                # Indoors/sealed roof neutralizes outdoor wind effects for carry physics.
                wind_out_component = 0.0

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
                if not batting_order:
                    # Data-availability fail-safe: use available roster player IDs as emergency batting-order proxy.
                    team_players = team_info.get('players', {}) if isinstance(team_info, dict) else {}
                    if not team_players and raw_team_info:
                        team_players = raw_team_info.get('players', {}) or {}
                    roster_ids = []
                    for k in (team_players or {}).keys():
                        sid = str(k).replace('ID', '').strip()
                        if sid.isdigit():
                            roster_ids.append(sid)
                    batting_order = roster_ids[:9]

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

                    # Fall back to pre-resolved probable pitcher ID when boxscore hasn't populated yet.
                    if not pitcher_id:
                        pitcher_id = probable_away_pitcher_id if team_type == 'home' else probable_home_pitcher_id

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
                        'lineup_pa_expectation': get_lineup_slot_pa_expectation(order_idx + 1),
                        'wind_out_component': wind_out_component,
                        'park_factor': handed_park_factor,
                        'temp': weather['temp'],
                        'wind_speed': weather['wind_speed'],
                        'humidity': weather.get('humidity', 50),
                        'precipitation': weather.get('precipitation', 0),
                        'pressure': weather.get('pressure', 1013.25),
                        'roof_status': roof_status,
                        'roof_sealed': roof_sealed,
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
# Bidirectional learning: update weights from both misses and correct predictions
# so the system adapts to failures without over-reinforcing already-correct behavior.
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


def apply_rolling_training_window(train_df):
    """Filter training rows to a rolling time window to reduce drift/latency.

    Uses `TRAINING_ROLLING_WINDOW_DAYS` (default 365). Set to 0 to disable.
    """
    if train_df is None or getattr(train_df, 'empty', True) or 'game_date' not in train_df.columns:
        return train_df

    rolling_days = max(0, _env_int('TRAINING_ROLLING_WINDOW_DAYS', 365))
    if rolling_days <= 0:
        return train_df

    work = train_df.copy()
    work['game_date'] = pd.to_datetime(work.get('game_date', pd.NaT), errors='coerce')
    max_game_date = work['game_date'].max()
    if pd.isna(max_game_date):
        return train_df

    cutoff = max_game_date - pd.Timedelta(days=rolling_days)
    keep_mask = work['game_date'].isna() | (work['game_date'] >= cutoff)
    filtered = work.loc[keep_mask].copy()
    dropped = int(len(work) - len(filtered))
    print(
        "Rolling training window: "
        f"days={rolling_days}, kept={len(filtered):,}, dropped={dropped:,}, cutoff={cutoff.date()}"
    )
    return filtered


def build_recent_minibatch_weights(train_df):
    """Upweight the most recent mini-batch to capture short-term form changes.

    Uses `TRAINING_MINI_BATCH_DAYS` (default 7) and
    `TRAINING_MINI_BATCH_MULTIPLIER` (default 1.20).
    """
    if train_df is None or getattr(train_df, 'empty', True) or 'game_date' not in train_df.columns:
        return np.ones(len(train_df) if train_df is not None else 0, dtype=float)

    mini_days = max(1, _env_int('TRAINING_MINI_BATCH_DAYS', 7))
    mini_mult = float(np.clip(_env_float('TRAINING_MINI_BATCH_MULTIPLIER', 1.20), 1.0, 2.5))

    game_dates = pd.to_datetime(train_df.get('game_date', pd.NaT), errors='coerce')
    max_game_date = game_dates.max()
    if pd.isna(max_game_date):
        return np.ones(len(train_df), dtype=float)

    cutoff = max_game_date - pd.Timedelta(days=mini_days)
    recent_mask = game_dates >= cutoff
    weights = np.ones(len(train_df), dtype=float)
    if recent_mask.any() and mini_mult > 1.0:
        weights[np.asarray(recent_mask.fillna(False), dtype=bool)] = mini_mult
    print(
        "Mini-batch recency weights: "
        f"days={mini_days}, multiplier={mini_mult:.2f}, boosted_rows={int(recent_mask.sum()):,}"
    )
    return weights


def _online_sidecar_state_path():
    Path('data').mkdir(parents=True, exist_ok=True)
    return Path('data') / 'online_sidecar_state.pkl'


def _load_online_sidecar_state(feature_names):
    path = _online_sidecar_state_path()
    if not path.exists():
        return None
    try:
        state = pickle.loads(path.read_bytes())
        saved_features = list(state.get('feature_names', []))
        if saved_features != list(feature_names):
            return None
        return state
    except Exception:
        return None


def _save_online_sidecar_state(state):
    try:
        _online_sidecar_state_path().write_bytes(pickle.dumps(state))
    except Exception as exc:
        print(f"Online sidecar state save skipped: {exc}")


def _build_online_sidecar_state(feature_names, alpha):
    if SGDClassifier is None or StandardScaler is None:
        return None
    model = SGDClassifier(
        loss='log_loss',
        penalty='elasticnet',
        alpha=float(np.clip(alpha, 1e-6, 1e-2)),
        l1_ratio=0.10,
        learning_rate='optimal',
        random_state=42,
        average=True,
    )
    scaler = StandardScaler(with_mean=True, with_std=True)
    return {
        'feature_names': list(feature_names),
        'model': model,
        'scaler': scaler,
        'medians': {},
        'update_count': 0,
        'initialized': False,
        'last_update_utc': None,
    }


def update_online_sidecar_model(train_df, feature_names, sample_weights):
    """Incrementally update optional online sidecar model from recent mini-batch."""
    enabled = str(os.getenv('ONLINE_SIDECAR_ENABLED', 'false')).strip().lower() in {'1', 'true', 'yes'}
    if not enabled:
        return None, {'enabled': False, 'applied': False, 'reason': 'disabled'}
    if SGDClassifier is None or StandardScaler is None:
        return None, {'enabled': True, 'applied': False, 'reason': 'sklearn_missing'}
    if train_df is None or getattr(train_df, 'empty', True) or 'is_hr' not in train_df.columns:
        return None, {'enabled': True, 'applied': False, 'reason': 'no_training_rows'}

    if any(c not in train_df.columns for c in feature_names):
        return None, {'enabled': True, 'applied': False, 'reason': 'missing_features'}

    mini_days = max(1, _env_int('ONLINE_SIDECAR_BATCH_DAYS', 7))
    min_rows = max(100, _env_int('ONLINE_SIDECAR_MIN_ROWS', 800))
    alpha = float(np.clip(_env_float('ONLINE_SIDECAR_ALPHA', 0.0005), 1e-6, 1e-2))

    work = train_df.copy()
    work['game_date'] = pd.to_datetime(work.get('game_date', pd.NaT), errors='coerce')
    max_game_date = work['game_date'].max()
    if pd.isna(max_game_date):
        return None, {'enabled': True, 'applied': False, 'reason': 'missing_game_date'}

    cutoff = max_game_date - pd.Timedelta(days=mini_days)
    batch_mask = work['game_date'] >= cutoff
    batch_rows = int(batch_mask.sum())
    if batch_rows < min_rows:
        return None, {
            'enabled': True,
            'applied': False,
            'reason': 'insufficient_batch_rows',
            'batch_rows': batch_rows,
            'required_rows': min_rows,
        }

    state = _load_online_sidecar_state(feature_names)
    if state is None:
        state = _build_online_sidecar_state(feature_names, alpha)
    if state is None:
        return None, {'enabled': True, 'applied': False, 'reason': 'state_init_failed'}

    work_nodup = work.loc[:, ~work.columns.duplicated(keep='first')]
    x_full = work_nodup.reindex(columns=feature_names).apply(pd.to_numeric, errors='coerce')
    medians = x_full.median(numeric_only=True).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    state['medians'] = medians.to_dict()
    x_batch = x_full.loc[batch_mask].fillna(medians)
    y_batch = pd.to_numeric(work.loc[batch_mask, 'is_hr'], errors='coerce').fillna(0).astype(int)

    w_arr = np.asarray(sample_weights, dtype=float).reshape(-1)
    if len(w_arr) != len(work):
        w_batch = np.ones(len(x_batch), dtype=float)
    else:
        w_batch = np.clip(w_arr[np.asarray(batch_mask, dtype=bool)], 0.25, 20.0)

    scaler = state['scaler']
    scaler.partial_fit(x_batch.values)
    x_scaled = scaler.transform(x_batch.values)

    model = state['model']
    if bool(state.get('initialized', False)):
        model.partial_fit(x_scaled, y_batch.values, sample_weight=w_batch)
    else:
        model.partial_fit(x_scaled, y_batch.values, classes=np.array([0, 1]), sample_weight=w_batch)
        state['initialized'] = True

    state['update_count'] = int(state.get('update_count', 0)) + 1
    state['last_update_utc'] = datetime.utcnow().isoformat()
    _save_online_sidecar_state(state)

    diagnostics = {
        'enabled': True,
        'applied': True,
        'batch_days': mini_days,
        'batch_rows': batch_rows,
        'positive_rate': float(y_batch.mean()) if len(y_batch) else 0.0,
        'update_count': int(state.get('update_count', 0)),
    }
    print(
        "Online sidecar update: "
        f"rows={batch_rows:,}, pos_rate={diagnostics['positive_rate']:.4f}, "
        f"updates={diagnostics['update_count']}"
    )
    return state, diagnostics


def apply_online_sidecar_blend(base_probs, live_features, feature_names, sidecar_state=None):
    """Apply conservative bounded blend from optional online sidecar model."""
    enabled = str(os.getenv('ONLINE_SIDECAR_ENABLED', 'false')).strip().lower() in {'1', 'true', 'yes'}
    blend_enabled = str(os.getenv('ONLINE_SIDECAR_BLEND_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
    if not enabled or not blend_enabled:
        return np.asarray(base_probs), None, {'enabled': enabled, 'applied': False, 'reason': 'disabled'}

    if sidecar_state is None:
        sidecar_state = _load_online_sidecar_state(feature_names)
    if not sidecar_state or not bool(sidecar_state.get('initialized', False)):
        return np.asarray(base_probs), None, {'enabled': True, 'applied': False, 'reason': 'missing_state'}

    min_updates = max(1, _env_int('ONLINE_SIDECAR_MIN_UPDATES', 3))
    update_count = int(sidecar_state.get('update_count', 0))
    if update_count < min_updates:
        return np.asarray(base_probs), None, {
            'enabled': True,
            'applied': False,
            'reason': 'insufficient_updates',
            'update_count': update_count,
            'min_updates': min_updates,
        }

    blend_weight = float(np.clip(_env_float('ONLINE_SIDECAR_WEIGHT', 0.12), 0.0, 0.25))
    max_delta = float(np.clip(_env_float('ONLINE_SIDECAR_MAX_DELTA', 0.03), 0.0, 0.10))
    if blend_weight <= 0 or max_delta <= 0:
        return np.asarray(base_probs), None, {'enabled': True, 'applied': False, 'reason': 'zero_blend'}

    medians = pd.Series(sidecar_state.get('medians', {}), dtype=float)
    live_nodup = live_features.loc[:, ~live_features.columns.duplicated(keep='first')]
    x_live = live_nodup.reindex(columns=feature_names).apply(pd.to_numeric, errors='coerce')
    if not medians.empty:
        x_live = x_live.fillna(medians)
    x_live = x_live.fillna(0.0)

    scaler = sidecar_state.get('scaler')
    model = sidecar_state.get('model')
    if scaler is None or model is None:
        return np.asarray(base_probs), None, {'enabled': True, 'applied': False, 'reason': 'invalid_state'}

    x_scaled = scaler.transform(x_live.values)
    sidecar_probs = model.predict_proba(x_scaled)[:, 1]
    sidecar_probs = np.clip(np.asarray(sidecar_probs), 0.0, 1.0)
    base_arr = np.clip(np.asarray(base_probs), 0.0, 1.0)

    delta = np.clip(sidecar_probs - base_arr, -max_delta, max_delta)
    blended = np.clip(base_arr + (blend_weight * delta), 0.0, 1.0)
    diag = {
        'enabled': True,
        'applied': True,
        'blend_weight': blend_weight,
        'max_delta': max_delta,
        'update_count': update_count,
        'mean_abs_delta': float(np.mean(np.abs(delta))) if len(delta) else 0.0,
    }
    print(
        "Online sidecar blend: "
        f"weight={blend_weight:.3f}, max_delta={max_delta:.3f}, "
        f"mean_abs_delta={diag['mean_abs_delta']:.4f}, updates={update_count}"
    )
    return blended, sidecar_probs, diag


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


def build_pregame_primary_weapon_vulnerability_lookup(
    statcast_df,
    lookback_days=45,
    min_sample=40,
    usage_floor=0.12,
):
    """Build pregame pitcher lookup for vulnerable primary weapons from recent pitch mix.

    This replaces in-game count assumptions with a morning-safe proxy:
    count how many heavily used pitch types are allowing elevated damaging contact.
    """
    empty_debug = {'pitchers_seen': 0, 'pitchers_flagged': 0}
    if statcast_df is None or statcast_df.empty:
        return {}, empty_debug

    required = {'pitcher', 'pitch_type'}
    if not required.issubset(set(statcast_df.columns)):
        return {}, empty_debug

    df = statcast_df.copy()
    try:
        if 'game_date' in df.columns:
            df['game_date'] = pd.to_datetime(df['game_date'], errors='coerce')
            cutoff = pd.Timestamp(datetime.today().date()) - pd.Timedelta(days=max(14, int(lookback_days)))
            df = df[df['game_date'] >= cutoff]
    except Exception:
        pass

    if df.empty:
        return {}, empty_debug

    df['pitcher'] = pd.to_numeric(df['pitcher'], errors='coerce')
    df = df.dropna(subset=['pitcher', 'pitch_type']).copy()
    if df.empty:
        return {}, empty_debug

    if 'launch_speed' in df.columns:
        launch_speed = pd.to_numeric(df['launch_speed'], errors='coerce').fillna(0.0)
        hard_hit = launch_speed >= 95.0
    else:
        hard_hit = pd.Series([False] * len(df), index=df.index)

    if 'events' in df.columns:
        is_hr = df['events'].astype(str).eq('home_run')
    else:
        is_hr = pd.Series([False] * len(df), index=df.index)

    if 'estimated_woba_using_speedangle' in df.columns:
        xwoba = pd.to_numeric(df['estimated_woba_using_speedangle'], errors='coerce').fillna(0.0)
        loud_xwoba = xwoba >= 0.600
    else:
        loud_xwoba = pd.Series([False] * len(df), index=df.index)

    df['damaging_contact'] = (hard_hit | is_hr | loud_xwoba).astype(int)

    by_pitch = df.groupby(['pitcher', 'pitch_type'], as_index=False).agg(
        sample_size=('pitch_type', 'count'),
        damage_rate=('damaging_contact', 'mean'),
    )
    if by_pitch.empty:
        return {}, empty_debug

    by_pitch['pitcher_total'] = by_pitch.groupby('pitcher')['sample_size'].transform('sum')
    by_pitch['usage_rate'] = by_pitch['sample_size'] / by_pitch['pitcher_total'].clip(lower=1)

    vulnerable = by_pitch[
        (by_pitch['sample_size'] >= max(10, int(min_sample)))
        & (by_pitch['usage_rate'] >= float(usage_floor))
        & (by_pitch['damage_rate'] >= 0.38)
    ].copy()

    if vulnerable.empty:
        return {}, {'pitchers_seen': int(by_pitch['pitcher'].nunique()), 'pitchers_flagged': 0}

    lookup = {
        int(pid): int(cnt)
        for pid, cnt in vulnerable.groupby('pitcher')['pitch_type'].nunique().to_dict().items()
    }
    debug = {
        'pitchers_seen': int(by_pitch['pitcher'].nunique()),
        'pitchers_flagged': int(len(lookup)),
    }
    return lookup, debug


def _tv_distance(vec_a, vec_b):
    keys = set(vec_a.keys()) | set(vec_b.keys())
    if not keys:
        return 0.0
    return 0.5 * sum(abs(float(vec_a.get(k, 0.0)) - float(vec_b.get(k, 0.0))) for k in keys)


def build_batter_recent_hr_features(statcast_df, rows_df, short_days=7, long_days=14):
    """Build recent HR count and rate features for batters over short and long windows."""
    if rows_df is None or getattr(rows_df, 'empty', True):
        return pd.DataFrame(index=getattr(rows_df, 'index', pd.RangeIndex(0)))
    empty_result = pd.DataFrame({
        'batter_hr_last_7d': 0.0,
        'batter_hr_last_14d': 0.0,
        'batter_hr_rate_7d': 0.0,
        'batter_hr_rate_14d': 0.0,
    }, index=rows_df.index)
    if statcast_df is None or getattr(statcast_df, 'empty', True) or 'batter' not in statcast_df.columns:
        return empty_result

    work = statcast_df.copy()
    try:
        work['game_date'] = pd.to_datetime(work['game_date'], errors='coerce')
        today = pd.Timestamp(datetime.today().date())
        work = work[work['game_date'] < today].copy()
    except Exception:
        pass

    work['batter'] = pd.to_numeric(work['batter'], errors='coerce')
    work = work.dropna(subset=['batter']).copy()
    if work.empty:
        return empty_result

    if 'events' in work.columns:
        work['is_hr'] = work['events'].astype(str).str.lower().eq('home_run').astype(int)
    else:
        work['is_hr'] = 0

    today_ts = pd.Timestamp(datetime.today().date())
    cut_short = today_ts - pd.Timedelta(days=short_days)
    cut_long = today_ts - pd.Timedelta(days=long_days)

    short_df = work[work['game_date'] >= cut_short]
    long_df = work[work['game_date'] >= cut_long]

    short_agg = short_df.groupby('batter', as_index=False).agg(
        batter_hr_last_7d=('is_hr', 'sum'),
        batter_pa_7d=('is_hr', 'count'),
    )
    long_agg = long_df.groupby('batter', as_index=False).agg(
        batter_hr_last_14d=('is_hr', 'sum'),
        batter_pa_14d=('is_hr', 'count'),
    )

    def _score(row):
        try:
            batter_id = int(row.get('batter', np.nan))
        except Exception:
            return pd.Series({'batter_hr_last_7d': 0.0, 'batter_hr_last_14d': 0.0,
                              'batter_hr_rate_7d': 0.033, 'batter_hr_rate_14d': 0.033})
        s = short_agg[short_agg['batter'] == batter_id]
        l = long_agg[long_agg['batter'] == batter_id]
        hr7 = float(s['batter_hr_last_7d'].iloc[0]) if not s.empty else 0.0
        pa7 = float(s['batter_pa_7d'].iloc[0]) if not s.empty else 0.0
        hr14 = float(l['batter_hr_last_14d'].iloc[0]) if not l.empty else 0.0
        pa14 = float(l['batter_pa_14d'].iloc[0]) if not l.empty else 0.0
        rate7 = hr7 / pa7 if pa7 >= 5 else 0.033
        rate14 = hr14 / pa14 if pa14 >= 10 else 0.033
        return pd.Series({'batter_hr_last_7d': hr7, 'batter_hr_last_14d': hr14,
                          'batter_hr_rate_7d': rate7, 'batter_hr_rate_14d': rate14})

    return rows_df.apply(_score, axis=1).astype(float)


def build_recent_batter_woba_proxy(statcast_df, rows_df, lookback_days=45, min_sample=4):
    """Create a recent batter wOBA proxy from Statcast xwOBA-style signals."""
    if rows_df is None or getattr(rows_df, 'empty', True):
        return pd.Series([0.0] * 0, dtype=float)
    if statcast_df is None or getattr(statcast_df, 'empty', True):
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)
    if 'batter' not in statcast_df.columns or 'estimated_woba_using_speedangle' not in statcast_df.columns:
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)

    work = statcast_df.copy()
    try:
        if 'game_date' in work.columns:
            work['game_date'] = pd.to_datetime(work['game_date'], errors='coerce')
            cutoff = pd.Timestamp(datetime.today().date()) - pd.Timedelta(days=max(14, int(lookback_days)))
            work = work[work['game_date'] >= cutoff]
    except Exception:
        pass

    work['batter'] = pd.to_numeric(work['batter'], errors='coerce')
    work = work.dropna(subset=['batter']).copy()
    if work.empty:
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)

    xwoba = pd.to_numeric(work['estimated_woba_using_speedangle'], errors='coerce').fillna(0.0)
    work['xwoba'] = xwoba
    bat_summary = work.groupby('batter', as_index=False).agg(
        sample_size=('xwoba', 'count'),
        recent_xwoba=('xwoba', 'mean'),
    )
    if int(min_sample) > 1:
        bat_summary = bat_summary[bat_summary['sample_size'] >= max(1, int(min_sample))].copy()
    if bat_summary.empty:
        bat_summary = work.groupby('batter', as_index=False).agg(
            sample_size=('xwoba', 'count'),
            recent_xwoba=('xwoba', 'mean'),
        )

    def _score_for_row(row):
        try:
            batter_id = int(row.get('batter', np.nan))
        except Exception:
            return 0.0
        if pd.isna(batter_id):
            return 0.0
        match = bat_summary[bat_summary['batter'] == batter_id]
        if match.empty:
            return 0.0
        return float(np.clip(match['recent_xwoba'].iloc[0], 0.0, 1.0))

    return rows_df.apply(_score_for_row, axis=1).astype(float)


def build_recent_pitcher_damage_proxy(statcast_df, rows_df, lookback_days=45, min_sample=4):
    """Create a recent pitcher damage proxy from recent barrel/hard-hit/HR allowed rate."""
    if rows_df is None or getattr(rows_df, 'empty', True):
        return pd.Series([0.0] * 0, dtype=float)
    if statcast_df is None or getattr(statcast_df, 'empty', True):
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)
    if 'pitcher' not in statcast_df.columns:
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)

    work = statcast_df.copy()
    try:
        if 'game_date' in work.columns:
            work['game_date'] = pd.to_datetime(work['game_date'], errors='coerce')
            cutoff = pd.Timestamp(datetime.today().date()) - pd.Timedelta(days=max(14, int(lookback_days)))
            work = work[work['game_date'] >= cutoff]
    except Exception:
        pass

    work['pitcher'] = pd.to_numeric(work['pitcher'], errors='coerce')
    work = work.dropna(subset=['pitcher']).copy()
    if work.empty:
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)

    if 'launch_speed' in work.columns:
        hard_hit = pd.to_numeric(work['launch_speed'], errors='coerce').fillna(0.0) >= 95.0
    else:
        hard_hit = pd.Series([False] * len(work), index=work.index)
    if 'events' in work.columns:
        is_hr = work['events'].astype(str).str.lower().eq('home_run')
    else:
        is_hr = pd.Series([False] * len(work), index=work.index)
    damage = (hard_hit.astype(int) + is_hr.astype(int)).clip(0, 1)
    work['damage_flag'] = damage

    pit_summary = work.groupby('pitcher', as_index=False).agg(
        sample_size=('damage_flag', 'count'),
        recent_damage_rate=('damage_flag', 'mean'),
    )
    if int(min_sample) > 1:
        pit_summary = pit_summary[pit_summary['sample_size'] >= max(1, int(min_sample))].copy()
    if pit_summary.empty:
        pit_summary = work.groupby('pitcher', as_index=False).agg(
            sample_size=('damage_flag', 'count'),
            recent_damage_rate=('damage_flag', 'mean'),
        )

    def _score_for_row(row):
        try:
            pitcher_id = int(row.get('pitcher', np.nan))
        except Exception:
            return 0.0
        if pd.isna(pitcher_id):
            return 0.0
        match = pit_summary[pit_summary['pitcher'] == pitcher_id]
        if match.empty:
            return 0.0
        return float(np.clip(match['recent_damage_rate'].iloc[0], 0.0, 1.0))

    return rows_df.apply(_score_for_row, axis=1).astype(float)


def build_batter_pitch_mix_matchup_feature(
    statcast_df,
    rows_df,
    lookback_days=45,
    min_sample=10,
    usage_floor=0.12,
    damage_threshold=0.38,
):
    """Create a dedicated batter-vs-pitcher pitch-mix matchup feature.

    This evaluates how often the batter has produced damaging contact against the
    pitch types the pitcher uses most frequently, giving the model an explicit
    matchup signal instead of relying only on indirect proxies.
    """
    if rows_df is None or getattr(rows_df, 'empty', True):
        return pd.Series([0.0] * 0, dtype=float)

    if statcast_df is None or getattr(statcast_df, 'empty', True):
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)

    required = {'batter', 'pitcher', 'pitch_type'}
    if not required.issubset(set(statcast_df.columns)):
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)

    work = statcast_df.copy()
    try:
        if 'game_date' in work.columns:
            work['game_date'] = pd.to_datetime(work['game_date'], errors='coerce')
            cutoff = pd.Timestamp(datetime.today().date()) - pd.Timedelta(days=max(14, int(lookback_days)))
            work = work[work['game_date'] >= cutoff]
    except Exception:
        pass

    if work.empty:
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)

    work['batter'] = pd.to_numeric(work['batter'], errors='coerce')
    work['pitcher'] = pd.to_numeric(work['pitcher'], errors='coerce')
    work = work.dropna(subset=['batter', 'pitcher', 'pitch_type']).copy()
    if work.empty:
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)

    if 'launch_speed' in work.columns:
        hard_hit = pd.to_numeric(work['launch_speed'], errors='coerce').fillna(0.0) >= 95.0
    else:
        hard_hit = pd.Series([False] * len(work), index=work.index)

    if 'events' in work.columns:
        is_hr = work['events'].astype(str).str.lower().eq('home_run')
    else:
        is_hr = pd.Series([False] * len(work), index=work.index)

    if 'estimated_woba_using_speedangle' in work.columns:
        xwoba = pd.to_numeric(work['estimated_woba_using_speedangle'], errors='coerce').fillna(0.0)
        loud_xwoba = xwoba >= 0.600
    else:
        loud_xwoba = pd.Series([False] * len(work), index=work.index)

    work['damaging_contact'] = (hard_hit | is_hr | loud_xwoba).astype(int)

    by_matchup = work.groupby(['batter', 'pitcher', 'pitch_type'], as_index=False).agg(
        sample_size=('pitch_type', 'count'),
        damage_rate=('damaging_contact', 'mean'),
    )
    if by_matchup.empty:
        return pd.Series([0.0] * len(rows_df), index=rows_df.index, dtype=float)

    by_matchup['pitcher_total'] = by_matchup.groupby(['batter', 'pitcher'])['sample_size'].transform('sum')
    by_matchup['usage_rate'] = by_matchup['sample_size'] / by_matchup['pitcher_total'].clip(lower=1)

    vulnerable = by_matchup[
        (by_matchup['sample_size'] >= max(1, int(min_sample)))
        & (by_matchup['usage_rate'] >= float(usage_floor))
        & (by_matchup['damage_rate'] >= float(damage_threshold))
    ].copy()

    def _score_for_row(row):
        try:
            batter_id = int(row.get('batter', np.nan))
            pitcher_id = int(row.get('pitcher', np.nan))
        except Exception:
            return 0.0
        if pd.isna(batter_id) or pd.isna(pitcher_id):
            return 0.0

        subset = vulnerable[(vulnerable['batter'] == batter_id) & (vulnerable['pitcher'] == pitcher_id)]
        if subset.empty:
            return 0.0
        weighted_damage = subset['usage_rate'].mul(subset['damage_rate'])
        if weighted_damage.empty:
            return 0.0
        score = float(np.clip(weighted_damage.mean(), 0.0, 1.0))
        return score

    return rows_df.apply(_score_for_row, axis=1).astype(float)


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


def _extract_positive_class_probabilities(model, features_frame):
    """Return robust positive-class probabilities for binary HR classification.

    Uses predict_proba when available and always extracts class-1 probabilities.
    Falls back to predict output only when predict_proba is unavailable.
    """
    if hasattr(model, 'predict_proba'):
        proba = model.predict_proba(features_frame)
        proba_arr = np.asarray(proba)
        if proba_arr.ndim == 2 and proba_arr.shape[1] >= 2:
            return np.clip(proba_arr[:, 1], 0.0, 1.0)
        if proba_arr.ndim == 1:
            return np.clip(proba_arr, 0.0, 1.0)

    # Fallback for estimators without predict_proba.
    raw_pred = np.asarray(model.predict(features_frame))
    if raw_pred.ndim == 2 and raw_pred.shape[1] >= 2:
        return np.clip(raw_pred[:, 1], 0.0, 1.0)
    return np.clip(raw_pred.reshape(-1), 0.0, 1.0)


def _downsample_majority_class(
    X,
    y,
    sample_weights,
    negative_to_positive_ratio=3.0,
    strategy='recent',
    random_state=42,
):
    """Downsample the majority class to a target negative:positive ratio."""
    if X is None or y is None:
        return X, y, sample_weights, {'applied': False, 'reason': 'missing_inputs'}

    y_series = pd.to_numeric(pd.Series(y), errors='coerce').fillna(0).astype(int)
    pos_idx = np.where(y_series.values == 1)[0]
    neg_idx = np.where(y_series.values == 0)[0]

    pos_count = int(len(pos_idx))
    neg_count = int(len(neg_idx))
    if pos_count <= 0 or neg_count <= 0:
        return X, y, sample_weights, {
            'applied': False,
            'reason': 'single_class',
            'pos_count': pos_count,
            'neg_count': neg_count,
        }

    ratio = float(np.clip(negative_to_positive_ratio, 1.0, 10.0))
    target_neg = int(min(neg_count, max(pos_count, round(pos_count * ratio))))
    if target_neg >= neg_count:
        return X, y, sample_weights, {
            'applied': False,
            'reason': 'already_within_ratio',
            'pos_count': pos_count,
            'neg_count': neg_count,
            'target_neg': target_neg,
        }

    strategy_key = str(strategy or 'recent').strip().lower()
    if strategy_key == 'random':
        rng = np.random.RandomState(int(random_state))
        kept_neg = np.sort(rng.choice(neg_idx, size=target_neg, replace=False))
    else:
        kept_neg = np.sort(neg_idx)[-target_neg:]

    keep_idx = np.sort(np.concatenate([pos_idx, kept_neg]))

    if isinstance(X, pd.DataFrame):
        X_out = X.iloc[keep_idx].copy()
    else:
        X_out = np.asarray(X)[keep_idx]

    if isinstance(y, pd.Series):
        y_out = y.iloc[keep_idx].copy()
    else:
        y_out = pd.Series(np.asarray(y)[keep_idx]).copy()

    w_arr = np.asarray(sample_weights) if sample_weights is not None else np.ones(len(y_series), dtype=float)
    w_out = w_arr[keep_idx]

    return X_out, y_out, w_out, {
        'applied': True,
        'strategy': strategy_key,
        'pos_count': pos_count,
        'neg_count': neg_count,
        'target_neg': int(target_neg),
        'rows_after': int(len(keep_idx)),
        'neg_to_pos_after': float(target_neg / max(1, pos_count)),
    }


def _apply_prior_probability_correction(probabilities, real_pos_rate, sampled_pos_rate):
    """Map downsampled probabilities back toward real-population prevalence.

    Uses odds correction: odds_real = odds_sample * w,
    where w = prior_odds_real / prior_odds_sample.
    Equivalent form:
      p_real = p_sample / (p_sample + (1 - p_sample) / w)
    """
    p = np.clip(np.asarray(probabilities, dtype=float), 1e-6, 1 - 1e-6)
    r = float(real_pos_rate)
    s = float(sampled_pos_rate)

    if not np.isfinite(r) or not np.isfinite(s):
        return p, {
            'applied': False,
            'reason': 'invalid_rates',
            'weight_factor': 1.0,
        }
    if r <= 0 or r >= 1 or s <= 0 or s >= 1:
        return p, {
            'applied': False,
            'reason': 'rates_out_of_bounds',
            'weight_factor': 1.0,
        }

    prior_odds_real = r / (1.0 - r)
    prior_odds_sample = s / (1.0 - s)
    if prior_odds_sample <= 0:
        return p, {
            'applied': False,
            'reason': 'invalid_sample_odds',
            'weight_factor': 1.0,
        }

    w = float(np.clip(prior_odds_real / prior_odds_sample, 1e-4, 1e4))
    corrected = p / (p + ((1.0 - p) / w))
    corrected = np.clip(corrected, 0.0, 1.0)
    return corrected, {
        'applied': True,
        'weight_factor': w,
        'real_pos_rate': r,
        'sampled_pos_rate': s,
    }


def print_x_today_diagnostics(live_df, feature_cols, tag='X_today', sample_rows=5):
    """Print strict diagnostics to catch static, NaN-heavy, or degenerate live feature payloads."""
    if live_df is None or live_df.empty:
        print(f"{tag} diagnostics: live frame is empty")
        return

    expected = list(feature_cols or [])
    missing = [c for c in expected if c not in live_df.columns]
    if missing:
        preview = ', '.join(missing[:12])
        print(f"{tag} diagnostics: missing feature columns ({len(missing)}): {preview}")

    payload = live_df.reindex(columns=expected)
    if payload.empty:
        print(f"{tag} diagnostics: payload matrix is empty after reindex")
        return

    numeric_payload = payload.apply(pd.to_numeric, errors='coerce')
    total_cells = max(1, int(numeric_payload.shape[0] * numeric_payload.shape[1]))
    nan_cells = int(numeric_payload.isna().sum().sum())
    nan_pct = (nan_cells / total_cells) * 100.0
    all_zero_rows = int((numeric_payload.fillna(0.0).abs().sum(axis=1) == 0.0).sum())

    nunique_by_col = numeric_payload.nunique(dropna=True)
    static_cols = nunique_by_col[nunique_by_col <= 1].index.tolist()
    varying_cols = int((nunique_by_col > 1).sum())

    row_signature = numeric_payload.fillna(-9999.0).round(6).astype(str).agg('|'.join, axis=1)
    unique_rows = int(row_signature.nunique())
    repeated_rows = int(len(row_signature) - unique_rows)

    print(
        f"{tag} diagnostics: rows={len(payload)}, cols={payload.shape[1]}, "
        f"nan_pct={nan_pct:.1f}%, varying_cols={varying_cols}, "
        f"static_cols={len(static_cols)}, unique_rows={unique_rows}, repeated_rows={repeated_rows}, "
        f"all_zero_rows={all_zero_rows}"
    )
    if static_cols:
        print(f"{tag} diagnostics: static columns sample -> {', '.join(static_cols[:10])}")

    preview_cols = [
        c for c in ['batter_name', 'pitcher_name', 'temp', 'wind_speed', 'wind_out_component', 'park_factor']
        if c in live_df.columns
    ] + [c for c in expected if c not in {'batter_name', 'pitcher_name'}][:4]
    preview_cols = list(dict.fromkeys(preview_cols))[:10]
    if preview_cols:
        print(f"{tag} preview ({min(sample_rows, len(live_df))} rows):")
        print(live_df[preview_cols].head(sample_rows).to_string(index=False))


def print_discord_payload_diagnostics(top_prob, radar, top_ev):
    """Sanity-check Discord payload values before webhook dispatch."""
    frames = {
        'top_prob': top_prob,
        'radar': radar,
        'top_ev': top_ev,
    }
    for label, frame in frames.items():
        if frame is None or frame.empty:
            print(f"Discord payload diagnostics ({label}): empty")
            continue

        probs = pd.to_numeric(frame.get('hr_probability', pd.Series([0.0] * len(frame))), errors='coerce').fillna(0.0)
        unique_probs = int(probs.round(6).nunique())
        print(
            f"Discord payload diagnostics ({label}): rows={len(frame)}, "
            f"min_prob={probs.min():.4f}, max_prob={probs.max():.4f}, "
            f"unique_prob_values={unique_probs}"
        )
        preview_cols = [c for c in ['batter_name', 'pitcher_name', 'hr_probability', 'model_reliability', 'edge_pct'] if c in frame.columns]
        if preview_cols:
            print(frame[preview_cols].head(3).to_string(index=False))


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

    if 'ev_pct' not in work.columns:
        if 'ev_percent' in work.columns:
            work['ev_pct'] = pd.to_numeric(work['ev_percent'], errors='coerce').fillna(0.0)
        else:
            work['ev_pct'] = 0.0

    if 'portfolio_action_score' not in work.columns:
        work['portfolio_action_score'] = _build_portfolio_action_score(work)

    return work


def _build_portfolio_action_score(frame):
    """Return a robust portfolio action score series for Discord/radar ranking."""
    if frame is None:
        return pd.Series(dtype=float)

    if not isinstance(frame, pd.DataFrame):
        return pd.Series([0.0], dtype=float)

    if frame.empty:
        return pd.Series(dtype=float, index=frame.index)

    hr_prob = _coerce_numeric_column(frame, 'hr_probability', default=0.0)
    ev_pct = _coerce_numeric_column(frame, 'ev_pct', default=0.0)
    physics_delta_abs = _coerce_numeric_column(frame, 'physics_delta_abs', default=0.0)
    return (hr_prob * 100.0) + (ev_pct * 0.5) + (physics_delta_abs * 0.1)


def _finalize_discord_radar_frame(rankings_df):
    """Normalize radar-style rankings so sort operations always have the expected columns."""
    if rankings_df is None:
        return pd.DataFrame()

    work = _ensure_discord_radar_columns(rankings_df).copy()
    work['hr_probability'] = _coerce_numeric_column(work, 'hr_probability', default=0.0)
    work['physics_delta'] = _coerce_numeric_column(work, 'physics_delta', default=0.0)
    work['ev_pct'] = _coerce_numeric_column(work, 'ev_pct', default=0.0)
    work['physics_delta_abs'] = work['physics_delta'].abs()
    work['portfolio_action_score'] = _build_portfolio_action_score(work)
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


def _build_discord_top_keys(frame):
    """Safely build a set of batter/pitcher key pairs for filtering Discord rows."""
    if frame is None or getattr(frame, 'empty', False):
        return set()

    if not isinstance(frame, pd.DataFrame):
        return set()

    batter_col = 'batter_name' if 'batter_name' in frame.columns else None
    pitcher_col = 'pitcher_name' if 'pitcher_name' in frame.columns else None
    if not batter_col or not pitcher_col:
        return set()

    return {
        (str(row.get(batter_col, '')), str(row.get(pitcher_col, '')))
        for _, row in frame.iterrows()
    }


def _build_discord_snapshot_summary(target_date, rankings, top_prob, radar, top_ev, discord_min_prob, discord_window_1_hours, discord_window_2_hours):
    """Build a single Discord summary that matches the actual pick tables being sent."""
    lines = [f"⚾ MLB HR MODEL SNAPSHOT ({target_date})", f"Candidates ranked: {len(rankings)}"]

    if top_prob.empty and radar.empty and top_ev.empty:
        lines.append(f"No qualifying picks met the minimum confidence threshold ({discord_min_prob * 100:.0f}%).")
    else:
        lines.append(f"Most Likely Homers: {len(top_prob)} candidates ≥{discord_min_prob * 100:.0f}% confidence")
        lines.append(f"Delivered radar picks: {len(radar)}")
        lines.append(f"Delivered +EV picks: {len(top_ev)}")

    lines.append(f"Time windows: <= {discord_window_1_hours}h, <= {discord_window_2_hours}h, later")
    return lines


def _confidence_grade_emoji(confidence):
    """Return a distinct emoji marker for each confidence tier."""
    confidence = str(confidence or '').upper()
    if confidence == 'HIGH':
        return '🔵'
    if confidence == 'MEDIUM':
        return '🟠'
    return '🟣'


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
        work['portfolio_action_score'] = []
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

    pred_prob = _coerce_numeric_column(work, 'pred_hr_prob', default=0.0)
    if 'hr_probability' in work.columns and 'pred_hr_prob' not in work.columns:
        pred_prob = _coerce_numeric_column(work, 'hr_probability', default=0.0)

    edge_pct = _coerce_numeric_column(work, 'edge_pct', default=0.0)
    ev_pct = _coerce_numeric_column(work, 'ev_percent', default=0.0)
    if 'ev_pct' in work.columns and 'ev_percent' not in work.columns:
        ev_pct = _coerce_numeric_column(work, 'ev_pct', default=0.0)

    kelly = _coerce_numeric_column(work, 'kelly_fraction', default=0.0)
    upside = _coerce_numeric_column(work, 'specific_day_upside_score', default=0.0)
    market_prob = _coerce_numeric_column(work, 'market_prob', default=np.nan)
    prob_edge_abs = (pred_prob - market_prob).fillna(0.0) if 'market_prob' in work.columns else pred_prob * 0.0

    out = work.copy()
    if 'pred_hr_prob' in out.columns:
        if 'hr_probability' in out.columns:
            out = out.drop(columns=['pred_hr_prob'])
        else:
            out = out.rename(columns={'pred_hr_prob': 'hr_probability'})
    if 'ev_percent' in out.columns:
        if 'ev_pct' in out.columns:
            out = out.drop(columns=['ev_percent'])
        else:
            out = out.rename(columns={'ev_percent': 'ev_pct'})

    if 'model_reliability' not in out.columns:
        out['model_reliability'] = 'MEDIUM'

    out['hr_probability'] = pred_prob
    out['edge_pct'] = edge_pct
    out['ev_pct'] = ev_pct
    out['kelly_fraction'] = kelly
    out['specific_day_upside_score'] = upside
    out['portfolio_action_score'] = (
        (pred_prob * 100.0) * 0.45
        + (edge_pct * 0.55)
        + (ev_pct * 0.35)
        + (kelly * 100.0 * 0.20)
        + (upside * 100.0 * 0.20)
        + (prob_edge_abs * 100.0 * 0.25)
    )
    return out


def estimate_model_reliability(pred_prob, consistency_score, sample_size):
    """IMPROVEMENT #4: Confidence/Reliability Level
    Returns: 'HIGH', 'MEDIUM', or 'LOW' confidence"""
    try:
        p = float(pd.to_numeric(pred_prob, errors='coerce'))
        c = float(pd.to_numeric(consistency_score, errors='coerce'))
        n = int(pd.to_numeric(sample_size, errors='coerce'))

        if not np.isfinite(p):
            return 'LOW'
        if not np.isfinite(c):
            c = 0.5

        # HR props are rare-event probabilities, so confidence cutoffs must be
        # calibrated in the low-probability regime (not 12%+ as in older logic).
        if p < 0.008:
            return 'LOW'

        if n < 15:
            return 'LOW'

        if c < 0.20:
            return 'LOW'

        if c >= 0.62 and n >= 80 and p >= 0.028:
            return 'HIGH'

        # Allow MEDIUM for strong current signal even on smaller batter sample windows.
        if c >= 0.55 and n >= 15 and p >= 0.08:
            return 'MEDIUM'

        if c >= 0.40 and n >= 35 and p >= 0.016:
            return 'MEDIUM'

        return 'LOW'
    except Exception:
        return 'MEDIUM'


def apply_daily_hr_volume_constraints(preds_df, game_count=1, avg_hr_per_game=2.3):
    """Calibrate daily HR probabilities without collapsing the entire slate.

    The model returns a ranking of probabilities for the full slate. Since HRs are rare,
    the summed probabilities should stay within a reasonable daily volume envelope, but
    global scaling should not flatten the top-end picks into near-zero values. This version
    preserves the ranking and only applies a mild shrinkage factor when the slate is
    materially over-forecasted.
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
        if scaling_factor >= 0.98:
            return work

        scaled = work['pred_hr_prob'] * scaling_factor
        if scaled.max() <= 0:
            return work

        # Preserve the ranking and keep the top-end candidates above a usable alert threshold.
        # The old behavior was too aggressive for alerting; this keeps the slate conservative
        # without erasing the top picks entirely.
        top_cap = max(0.12, min(0.35, scaled.max() * 1.05))
        work['pred_hr_prob'] = np.clip(scaled, 0.0, top_cap)
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


def apply_monotonic_prob_calibration(preds_df, gamma=1.35, cap=0.18, top_signal_boost=0.025):
    """Lift the upper tail of probabilities without changing their ordering.

    This uses a monotonic transform that stretches small probabilities upward in a
    controlled way, making the strongest candidates more actionable while keeping
    the full slate from exploding.
    """
    try:
        if preds_df is None:
            return preds_df
        work = preds_df.copy()
        if work.empty:
            return work

        probs = pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0)
        if probs.empty:
            return work

        calibrated = probs.copy()
        positive_mask = calibrated > 0.0
        if positive_mask.any():
            base_vals = 1.0 - np.power(1.0 - calibrated[positive_mask], gamma)
            calibrated[positive_mask] = np.clip(base_vals, 0.0, cap)

            if top_signal_boost > 0:
                max_prob = max(float(probs[positive_mask].max()), 1e-6)
                signal_scale = np.clip((probs[positive_mask] / max_prob), 0.0, 1.0)
                boost = top_signal_boost * signal_scale
                calibrated[positive_mask] = np.clip(calibrated[positive_mask] + boost, 0.0, cap)

        work['pred_hr_prob'] = calibrated.round(6)
        return work
    except Exception:
        return preds_df


def apply_recent_calibration_correction(
    preds_df,
    days_lookback=7,
    alpha=0.55,
    min_rows=600,
    min_files=3,
    min_multiplier=0.85,
    max_multiplier=1.45,
):
    """Apply a conservative scalar correction from recent evaluated calibration bias.

    The correction uses recent evaluation files to estimate mean(actual)/mean(pred), then
    shrinks that ratio toward 1.0 via `alpha` to avoid overreaction.
    """
    try:
        if preds_df is None:
            return preds_df, {'applied': False, 'reason': 'preds_none'}
        work = preds_df.copy()
        if work.empty:
            return work, {'applied': False, 'reason': 'preds_empty'}

        cutoff = datetime.today() - timedelta(days=max(1, int(days_lookback)))
        eval_files = []
        for fp in sorted(Path('data').glob('evaluation_*.csv')):
            try:
                d = datetime.strptime(fp.stem.replace('evaluation_', ''), '%Y-%m-%d')
                if d >= cutoff:
                    eval_files.append(fp)
            except Exception:
                continue

        if len(eval_files) < max(1, int(min_files)):
            return work, {
                'applied': False,
                'reason': 'insufficient_files',
                'files': len(eval_files),
            }

        parts = []
        for fp in eval_files:
            try:
                ev = pd.read_csv(fp, usecols=['pred_hr_prob', 'actual_hr'])
                if ev is not None and not ev.empty:
                    parts.append(ev)
            except Exception:
                continue

        if not parts:
            return work, {'applied': False, 'reason': 'no_eval_rows'}

        ev_all = pd.concat(parts, ignore_index=True)
        if len(ev_all) < max(100, int(min_rows)):
            return work, {
                'applied': False,
                'reason': 'insufficient_rows',
                'rows': int(len(ev_all)),
            }

        pred_mean = float(pd.to_numeric(ev_all['pred_hr_prob'], errors='coerce').fillna(0.0).mean())
        actual_mean = float(pd.to_numeric(ev_all['actual_hr'], errors='coerce').fillna(0.0).mean())

        if pred_mean <= 1e-6:
            return work, {'applied': False, 'reason': 'pred_mean_zero'}

        raw_ratio = actual_mean / pred_mean
        correction = 1.0 + ((raw_ratio - 1.0) * float(np.clip(alpha, 0.0, 1.0)))
        correction = float(np.clip(correction, min_multiplier, max_multiplier))

        probs = pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0)
        work['pred_hr_prob'] = np.clip(probs * correction, 0.0, 0.99).round(6)
        work['recent_calibration_multiplier'] = correction

        diagnostics = {
            'applied': True,
            'days': int(days_lookback),
            'files': int(len(eval_files)),
            'rows': int(len(ev_all)),
            'pred_mean': pred_mean,
            'actual_mean': actual_mean,
            'raw_ratio': float(raw_ratio),
            'correction': float(correction),
        }
        return work, diagnostics
    except Exception as exc:
        return preds_df, {'applied': False, 'reason': f'error:{exc}'}


def apply_recent_empirical_calibration(
    preds_df,
    days_lookback=14,
    blend=0.75,
    min_rows=500,
    min_files=3,
    min_unique_probs=30,
    max_delta=0.30,
):
    """Apply shape-aware probability recalibration from recent scored outcomes.

    Unlike scalar correction, this can bend overconfident upper tails downward while
    preserving monotonic ordering through isotonic regression.
    """
    try:
        if preds_df is None:
            return preds_df, {'applied': False, 'reason': 'preds_none'}
        work = preds_df.copy()
        if work.empty:
            return work, {'applied': False, 'reason': 'preds_empty'}
        if IsotonicRegression is None:
            return work, {'applied': False, 'reason': 'isotonic_unavailable'}

        cutoff = datetime.today() - timedelta(days=max(1, int(days_lookback)))
        eval_files = []
        for fp in sorted(Path('data').glob('evaluation_*.csv')):
            try:
                d = datetime.strptime(fp.stem.replace('evaluation_', ''), '%Y-%m-%d')
                if d >= cutoff:
                    eval_files.append(fp)
            except Exception:
                continue

        if len(eval_files) < max(1, int(min_files)):
            return work, {'applied': False, 'reason': 'insufficient_files', 'files': len(eval_files)}

        parts = []
        for fp in eval_files:
            try:
                ev = pd.read_csv(fp, usecols=['pred_hr_prob', 'actual_hr'])
                if ev is not None and not ev.empty:
                    parts.append(ev)
            except Exception:
                continue

        if not parts:
            return work, {'applied': False, 'reason': 'no_eval_rows'}

        ev_all = pd.concat(parts, ignore_index=True)
        ev_all['pred_hr_prob'] = pd.to_numeric(ev_all.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0).clip(0.0, 0.99)
        ev_all['actual_hr'] = pd.to_numeric(ev_all.get('actual_hr', 0.0), errors='coerce').fillna(0.0).clip(0.0, 1.0)
        ev_all = ev_all.dropna(subset=['pred_hr_prob', 'actual_hr'])

        if len(ev_all) < max(100, int(min_rows)):
            return work, {'applied': False, 'reason': 'insufficient_rows', 'rows': int(len(ev_all))}

        y = ev_all['actual_hr'].astype(float).values
        if np.unique(y).size < 2:
            return work, {'applied': False, 'reason': 'single_class_eval'}

        x = ev_all['pred_hr_prob'].astype(float).values
        if np.unique(np.round(x, 6)).size < max(5, int(min_unique_probs)):
            return work, {'applied': False, 'reason': 'insufficient_unique_probs'}

        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds='clip')
        iso.fit(x, y)

        base_probs = pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0).clip(0.0, 0.99)
        iso_probs = pd.Series(iso.predict(base_probs.values), index=work.index).clip(0.0, 0.99)

        blend = float(np.clip(blend, 0.0, 1.0))
        max_delta = float(np.clip(max_delta, 0.02, 0.60))
        delta = (iso_probs - base_probs).clip(-max_delta, max_delta)
        calibrated = (base_probs + (blend * delta)).clip(0.0, 0.99)

        work['pred_hr_prob'] = calibrated.round(6)
        work['empirical_calibration_applied'] = 1
        work['empirical_calibration_blend'] = blend
        work['empirical_calibration_max_delta'] = max_delta
        work['empirical_calibration_mean_abs_delta'] = float(np.mean(np.abs(delta.values))) if len(delta) else 0.0

        diagnostics = {
            'applied': True,
            'days': int(days_lookback),
            'files': int(len(eval_files)),
            'rows': int(len(ev_all)),
            'blend': float(blend),
            'max_delta': float(max_delta),
            'mean_abs_delta': float(np.mean(np.abs(delta.values))) if len(delta) else 0.0,
            'pred_mean_before': float(base_probs.mean()) if len(base_probs) else 0.0,
            'pred_mean_after': float(calibrated.mean()) if len(calibrated) else 0.0,
            'actual_mean_eval': float(np.mean(y)) if len(y) else 0.0,
        }
        return work, diagnostics
    except Exception as exc:
        return preds_df, {'applied': False, 'reason': f'error:{exc}'}


def apply_daily_probability_anchor(
    preds_df,
    target_mean=0.095,
    strength=0.90,
    min_scale=0.20,
    max_scale=1.10,
    final_cap=0.30,
):
    """Rescale today's probabilities toward a realistic slate-level mean.

    This prevents systematic overconfidence when stacked transforms inflate the
    entire distribution, while preserving ranking order.
    """
    try:
        if preds_df is None:
            return preds_df, {'applied': False, 'reason': 'preds_none'}
        work = preds_df.copy()
        if work.empty:
            return work, {'applied': False, 'reason': 'preds_empty'}

        probs = pd.to_numeric(work.get('pred_hr_prob', 0.0), errors='coerce').fillna(0.0).clip(0.0, 0.99)
        current_mean = float(probs.mean()) if len(probs) else 0.0
        if current_mean <= 1e-9:
            return work, {'applied': False, 'reason': 'mean_zero'}

        target_mean = float(np.clip(target_mean, 0.03, 0.20))
        strength = float(np.clip(strength, 0.0, 1.0))
        min_scale = float(np.clip(min_scale, 0.05, 2.0))
        max_scale = float(np.clip(max_scale, min_scale, 3.0))
        final_cap = float(np.clip(final_cap, 0.12, 0.60))

        raw_scale = target_mean / current_mean
        shrunk_scale = 1.0 + ((raw_scale - 1.0) * strength)
        applied_scale = float(np.clip(shrunk_scale, min_scale, max_scale))

        anchored = (probs * applied_scale).clip(0.0, final_cap)
        work['pred_hr_prob'] = anchored.round(6)
        work['daily_anchor_scale'] = applied_scale
        work['daily_anchor_target_mean'] = target_mean
        work['daily_anchor_pre_mean'] = current_mean
        work['daily_anchor_post_mean'] = float(anchored.mean()) if len(anchored) else 0.0
        work['daily_anchor_final_cap'] = final_cap

        return work, {
            'applied': True,
            'target_mean': target_mean,
            'pre_mean': current_mean,
            'post_mean': float(anchored.mean()) if len(anchored) else 0.0,
            'raw_scale': float(raw_scale),
            'applied_scale': applied_scale,
            'final_cap': final_cap,
        }
    except Exception as exc:
        return preds_df, {'applied': False, 'reason': f'error:{exc}'}


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

    all_eval_rows = []
    for d, fp in eval_files:
        try:
            ev = pd.read_csv(fp)
            if ev.empty:
                continue
            ev['pred_hr_prob'] = _num_series(ev, 'pred_hr_prob', 0.0)
            ev['actual_hr'] = _num_series(ev, 'actual_hr', 0.0).astype(int)
            all_eval_rows.append(ev)
        except Exception:
            continue

    eval_all = pd.concat(all_eval_rows, ignore_index=True) if all_eval_rows else pd.DataFrame()
    if eval_all.empty:
        return {
            'coefficients': coeffs,
            'samples': 0,
            'false_pos': 0,
            'diagnostics': {},
        }

    hr_rows = eval_all[eval_all['actual_hr'] == 1]
    non_hr_rows = eval_all[eval_all['actual_hr'] == 0]

    if not hr_rows.empty:
        missed_cutoff = float(np.clip(hr_rows['pred_hr_prob'].quantile(0.45), 0.06, 0.20))
    else:
        missed_cutoff = 0.10
    if not non_hr_rows.empty:
        false_pos_cutoff = float(np.clip(non_hr_rows['pred_hr_prob'].quantile(0.92), 0.14, 0.32))
    else:
        false_pos_cutoff = 0.20

    false_pos = eval_all[(eval_all['pred_hr_prob'] >= false_pos_cutoff) & (eval_all['actual_hr'] == 0)].copy()
    missed = eval_all[(eval_all['pred_hr_prob'] <= missed_cutoff) & (eval_all['actual_hr'] == 1)].copy()

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
            'missed_cutoff': round(missed_cutoff, 4),
            'false_pos_cutoff': round(false_pos_cutoff, 4),
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


def build_matchup_weather_features(df):
    """Construct a compact set of matchup and weather features from available columns."""
    if df is None:
        return pd.DataFrame()

    out = df.copy()

    def _safe_num(series, default):
        if isinstance(series, (pd.Series, pd.Index, np.ndarray, list, tuple)):
            return pd.to_numeric(series, errors='coerce').fillna(default)
        return pd.Series([default] * len(out), index=out.index, dtype=float)

    bat_barrel = _safe_num(out.get('bat_barrel_rate', 0.08), 0.08)
    bat_ev = _safe_num(out.get('bat_avg_exit_velocity', 88.0), 88.0)
    bat_pull = _safe_num(out.get('bat_pull_rate', 0.38), 0.38)
    bat_launch = _safe_num(out.get('bat_avg_launch_angle', 12.0), 12.0)
    bat_hr_fb = _safe_num(out.get('bat_hr_fb_rate', 0.12), 0.12)
    pitch_velo = _safe_num(out.get('pitch_avg_velocity', 92.0), 92.0)
    pitch_days = _safe_num(out.get('pitch_days_since_last_start', 5.0), 5.0)
    pitch_hr_allowed = _safe_num(out.get('pitch_hr_allowed_rate', 0.04), 0.04)
    pitch_hr_fb_allowed = _safe_num(out.get('pitch_hr_fb_allowed_rate', 0.12), 0.12)
    pitch_fb_allowed = _safe_num(out.get('pitch_fb_allowed_rate', 0.35), 0.35)
    temp = _safe_num(out.get('temp', 72.0), 72.0)
    wind_out = _safe_num(out.get('wind_out_component', 0.0), 0.0)
    pressure = _safe_num(out.get('pressure', 1013.25), 1013.25)
    humidity = _safe_num(out.get('humidity', 50.0), 50.0)
    lineup_slot = _safe_num(out.get('batting_order_slot', 5.0), 5.0)
    release_flag = _safe_num(out.get('line_release_window_flag', 0.0), 0.0)

    ppci = (
        1.0
        + ((bat_barrel - 0.08) / 0.08) * 0.20
        + ((bat_ev - 88.0) / 12.0) * 0.16
        + ((bat_hr_fb - 0.12) / 0.08) * 0.14
        + ((pitch_hr_allowed - 0.04) / 0.03) * 0.18
        + ((pitch_hr_fb_allowed - 0.12) / 0.08) * 0.12
        + ((pitch_velo - 92.0) / 12.0) * 0.05
    )
    dynamic = (
        1.0
        + ((bat_pull - 0.38) / 0.20) * 0.08
        + ((bat_launch - 12.0) / 16.0) * 0.07
        + ((pitch_fb_allowed - 0.35) / 0.15) * 0.09
        + ((pitch_days - 5.0) / 7.0) * 0.04
    )
    arsenal = (
        1.0
        + ((bat_barrel - 0.08) / 0.08) * 0.11
        + ((bat_ev - 88.0) / 12.0) * 0.09
        + ((pitch_hr_allowed - 0.04) / 0.03) * 0.10
        + ((pitch_velo - 92.0) / 10.0) * 0.04
    )
    micro = (
        1.0
        + ((temp - 72.0) / 28.0) * 0.06
        + (wind_out / 8.0) * 0.05
        + np.maximum(0.0, (1013.25 - pressure) / 15.0) * 0.02
        + np.maximum(0.0, (humidity - 45.0) / 30.0) * 0.01
    )
    lineup_pressure = (
        1.0
        + np.clip((6.0 - lineup_slot) / 5.0, 0.0, 1.0) * 0.12
        + release_flag * 0.08
        + np.clip((bat_hr_fb - 0.12) / 0.08, 0.0, 1.0) * 0.05
    )
    lineup_grab = (
        1.0
        + release_flag * 0.10
        + np.clip((bat_ev - 88.0) / 12.0, 0.0, 1.0) * 0.04
        + np.clip((pitch_days - 5.0) / 10.0, 0.0, 1.0) * 0.03
    )

    out['ppci_dominance_score'] = np.clip(ppci, 0.75, 1.55)
    out['dynamic_matchup_grade'] = np.clip(dynamic, 0.80, 1.35)
    out['pitch_arsenal_matchup_score'] = np.clip(arsenal, 0.80, 1.28)
    out['micro_weather_score'] = np.clip(micro, 0.90, 1.45)
    out['lineup_slot_pressure_score'] = np.clip(lineup_pressure, 0.90, 1.35)
    out['lineup_grab_window_score'] = np.clip(lineup_grab, 0.95, 1.30)

    # Explicit upside signals inspired by professional hitter's-edge research.
    split_edge = np.clip((out.get('has_platoon_advantage', 0) * 0.18) + (out.get('platoon_advantage_multiplier', 1.0) - 1.0) * 0.35, 0.0, 1.0)
    flyball_target = np.clip(
        ((pitch_hr_fb_allowed - 0.12) / 0.08) * 0.42
        + ((pitch_fb_allowed - 0.35) / 0.15) * 0.24
        + ((bat_hr_fb - 0.12) / 0.08) * 0.18
        + ((bat_pull - 0.38) / 0.20) * 0.16,
        0.0,
        1.0,
    )
    hot_streak = np.clip(
        ((bat_barrel - 0.08) / 0.08) * 0.35
        + ((bat_ev - 88.0) / 12.0) * 0.30
        + ((bat_hr_fb - 0.12) / 0.08) * 0.20
        + ((bat_launch - 12.0) / 16.0) * 0.15,
        0.0,
        1.0,
    )
    arsenal_vuln = np.clip(
        ((pitch_hr_allowed - 0.04) / 0.03) * 0.30
        + ((pitch_velo - 92.0) / 12.0) * 0.15
        + ((pitch_hr_fb_allowed - 0.12) / 0.08) * 0.30
        + ((pitch_fb_allowed - 0.35) / 0.15) * 0.25,
        0.0,
        1.0,
    )
    total_context = np.clip(
        ((temp - 72.0) / 18.0) * 0.25
        + (wind_out / 10.0) * 0.20
        + np.maximum(0.0, (1013.25 - pressure) / 12.0) * 0.15
        + np.maximum(0.0, (humidity - 45.0) / 30.0) * 0.10
        + np.clip((out.get('park_factor', 100.0) - 100.0) / 20.0, 0.0, 1.0) * 0.30,
        0.0,
        1.0,
    )

    out['split_advantage_score'] = np.clip(split_edge, 0.0, 1.0)
    out['flyball_pitcher_target_score'] = np.clip(flyball_target, 0.0, 1.0)
    out['hot_streak_contact_score'] = np.clip(hot_streak, 0.0, 1.0)
    out['arsenal_vulnerability_score'] = np.clip(arsenal_vuln, 0.0, 1.0)
    out['game_total_context_score'] = np.clip(total_context, 0.0, 1.0)

    # If the slate provides an actual total, blend it into the context score.
    if 'game_total' in out.columns:
        total_val = pd.to_numeric(out['game_total'], errors='coerce').fillna(8.5)
        out['game_total_context_score'] = np.clip(
            0.6 * out['game_total_context_score'] + 0.4 * ((total_val - 7.0) / 5.0).clip(0.0, 1.0),
            0.0,
            1.0,
        )
    return out


def build_cluster_matchup_probabilities(live_df, cluster_df=None):
    """Build a cluster-based matchup probability column for live rows."""
    if live_df is None:
        return pd.DataFrame()

    out = live_df.copy()
    if calculate_platoon_cluster_prob is None:
        out['cluster_platoon_prob'] = 0.0
        return out

    if cluster_df is None or (isinstance(cluster_df, pd.DataFrame) and cluster_df.empty):
        cluster_df = pd.DataFrame([
            {'batter_id': None, 'pitcher_id': None, 'batter_hand': 'R', 'pitcher_hand': 'R', 'pa': 65000, 'hr': 1950},
            {'batter_id': None, 'pitcher_id': None, 'batter_hand': 'R', 'pitcher_hand': 'L', 'pa': 76000, 'hr': 2500},
            {'batter_id': None, 'pitcher_id': None, 'batter_hand': 'L', 'pitcher_hand': 'R', 'pa': 58000, 'hr': 1900},
            {'batter_id': None, 'pitcher_id': None, 'batter_hand': 'L', 'pitcher_hand': 'L', 'pa': 72000, 'hr': 2300},
        ])

    def _normalise_hand(value):
        if pd.isna(value):
            return None
        hand = str(value).strip().upper()
        return hand if hand in {'L', 'R'} else None

    def _wind_direction(row):
        explicit = str(row.get('wind_direction', '') or '').strip().lower()
        if explicit:
            return explicit
        try:
            wind_out = float(row.get('wind_out_component', 0.0) or 0.0)
        except Exception:
            wind_out = 0.0
        return 'outward' if wind_out > 0 else 'neutral'

    probs = []
    for _, row in out.iterrows():
        batter_hand = _normalise_hand(row.get('stand', row.get('batter_hand', None)))
        pitcher_hand = _normalise_hand(row.get('p_throws', row.get('pitcher_hand', None)))
        if batter_hand is None or pitcher_hand is None:
            probs.append(0.0)
            continue

        try:
            batter_id = row.get('batter', None)
            pitcher_id = row.get('pitcher', None)
            prob = calculate_platoon_cluster_prob(
                cluster_df,
                batter_id=int(float(batter_id)) if pd.notna(batter_id) else None,
                pitcher_id=int(float(pitcher_id)) if pd.notna(pitcher_id) else None,
                batter_hand=batter_hand,
                pitcher_hand=pitcher_hand,
                league_hr_pa=0.031,
                min_pa=100,
                projected_pa=float(row.get('projected_pas', 4.1) or 4.1),
                park_factor_hr=max(float(row.get('park_factor', 100.0) or 100.0) / 100.0, 0.5),
                temperature=float(row.get('temp', 72.0) or 72.0),
                wind_speed=float(row.get('wind_speed', row.get('wind_out_component', 0.0)) or 0.0),
                wind_direction=_wind_direction(row),
            )
            probs.append(float(np.clip(prob or 0.0, 0.0, 1.0)))
        except Exception:
            probs.append(0.0)

    out['cluster_platoon_prob'] = probs
    return out


def apply_expert_signal_boosts(live_df, base_probs):
    """Boost probabilities using matchup, contact-quality, and environment signals."""
    if live_df is None:
        return np.asarray(base_probs, dtype=float)

    probs = np.asarray(base_probs, dtype=float).reshape(-1)
    if len(probs) != len(live_df):
        probs = np.resize(probs, len(live_df))

    if len(live_df) == 0:
        return probs

    def _series_or_default(column, default):
        if column in live_df.columns:
            return pd.to_numeric(live_df[column], errors='coerce').fillna(default).astype(float)
        return pd.Series([default] * len(live_df), index=live_df.index, dtype=float)

    platoon = _series_or_default('has_platoon_advantage', 0.0)
    platoon_mult = _series_or_default('platoon_advantage_multiplier', 1.0)
    barrel_rate = _series_or_default('bat_barrel_rate', 0.0)
    hard_hit_rate = _series_or_default('bat_hard_hit_rate', 0.0)
    exit_velo = _series_or_default('bat_avg_exit_velocity', 88.0)
    pitch_hr9 = _series_or_default('pitch_hr_per_9', 1.1)
    pitch_hr_allowed = _series_or_default('pitch_hr_allowed_rate', 0.04)
    park_factor = _series_or_default('park_factor', 100.0)
    temp = _series_or_default('temp', 72.0)
    wind_out = _series_or_default('wind_out_component', 0.0)
    weather_extremes = _series_or_default('weather_extremes_multiplier', 1.0)
    density_altitude = _series_or_default('density_altitude_factor', 1.0)
    porch_bonus = _series_or_default('porch_advantage_bonus', 1.0)
    elite_flag = _series_or_default('is_elite_power_batter', 0.0)
    split_adv = _series_or_default('split_advantage_score', 0.0)
    flyball_target = _series_or_default('flyball_pitcher_target_score', 0.0)
    hot_streak = _series_or_default('hot_streak_contact_score', 0.0)
    arsenal_vuln = _series_or_default('arsenal_vulnerability_score', 0.0)
    game_total_ctx = _series_or_default('game_total_context_score', 0.0)

    platoon_score = np.clip(((platoon_mult - 1.0) / 0.25) + (platoon * 0.35), 0.0, 1.0)
    contact_score = np.clip(
        ((barrel_rate - 0.08) / 0.10) * 0.45
        + ((hard_hit_rate - 0.35) / 0.25) * 0.35
        + ((exit_velo - 88.0) / 12.0) * 0.20,
        0.0,
        1.0,
    )
    pitcher_score = np.clip(
        ((pitch_hr9 - 1.1) / 1.2) * 0.55
        + ((pitch_hr_allowed - 0.04) / 0.03) * 0.45,
        0.0,
        1.0,
    )
    park_weather_score = np.clip(
        ((park_factor - 100.0) / 20.0) * 0.35
        + ((temp - 72.0) / 18.0) * 0.25
        + (wind_out / 10.0) * 0.20
        + ((weather_extremes - 1.0) / 0.20) * 0.10
        + ((density_altitude - 1.0) / 0.25) * 0.10,
        0.0,
        1.0,
    )
    porch_score = np.clip((porch_bonus - 1.0) / 0.15, 0.0, 1.0)

    specific_day_upside_score = np.clip(
        (platoon_score * 0.20)
        + (contact_score * 0.18)
        + (pitcher_score * 0.16)
        + (park_weather_score * 0.12)
        + (porch_score * 0.08)
        + (elite_flag * 0.06)
        + (split_adv * 0.14)
        + (flyball_target * 0.12)
        + (hot_streak * 0.10)
        + (arsenal_vuln * 0.10)
        + (game_total_ctx * 0.08),
        0.0,
        1.0,
    )
    signal_multiplier = 1.0 + specific_day_upside_score
    upside_boost_multiplier = np.clip(1.0 + specific_day_upside_score, 0.0, 1.6)

    live_df['specific_day_upside_score'] = specific_day_upside_score
    live_df['upside_boost_multiplier'] = upside_boost_multiplier

    return np.clip(probs * upside_boost_multiplier, 0.0, 1.0)


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


def build_expected_pa_distribution(expected_pa, spread=0.70, min_pa=1, max_pa=8):
    """Build a lightweight discrete PA distribution around expected PA count."""
    try:
        mu = float(expected_pa)
    except Exception:
        mu = 4.0
    if not np.isfinite(mu):
        mu = 4.0

    sigma = max(0.30, float(spread))
    lower = max(int(min_pa), int(math.floor(mu)) - 1)
    upper = min(int(max_pa), int(math.ceil(mu)) + 2)
    if lower > upper:
        lower = upper = int(np.clip(round(mu), min_pa, max_pa))

    dist = {}
    for pa in range(lower, upper + 1):
        z = (pa - mu) / sigma
        weight = math.exp(-0.5 * (z ** 2))
        dist[int(pa)] = float(weight)

    total = float(sum(dist.values()))
    if total <= 0:
        fallback_pa = int(np.clip(round(mu), min_pa, max_pa))
        return {fallback_pa: 1.0}

    return {k: float(v / total) for k, v in dist.items()}


def adjust_expected_pa_for_context(base_pa, team_implied_total=np.nan, game_total=np.nan):
    """Adjust PA expectation using available Vegas scoring context."""
    pa = float(base_pa) if pd.notna(base_pa) else 4.0
    pa = float(np.clip(pa, 2.0, 6.5))

    team_total = pd.to_numeric(pd.Series([team_implied_total]), errors='coerce').iloc[0]
    if pd.isna(team_total):
        g_total = pd.to_numeric(pd.Series([game_total]), errors='coerce').iloc[0]
        if pd.notna(g_total):
            team_total = float(g_total) / 2.0

    if pd.notna(team_total):
        # 4.2 is a neutral baseline implied runs per team.
        total_delta = float(np.clip((float(team_total) - 4.2) / 2.0, -0.35, 0.45))
        pa *= (1.0 + (0.35 * total_delta))

    return float(np.clip(pa, 2.0, 6.8))


def calculate_game_hr_probability(hr_rate_per_pa, expected_pa_distribution):
    """Calculate game-level HR probability from HR/PA and PA distribution via Poisson."""
    p_pa = float(hr_rate_per_pa)
    if not np.isfinite(p_pa):
        return 0.0
    if p_pa <= 0:
        return 0.0
    if p_pa >= 1:
        return 1.0

    final_game_prob = 0.0
    for pa, pa_prob in expected_pa_distribution.items():
        lam = p_pa * float(pa)
        prob_one_or_more_hrs = 1.0 - math.exp(-lam)
        final_game_prob += prob_one_or_more_hrs * float(pa_prob)

    return float(np.clip(final_game_prob, 0.0, 1.0))


def monte_carlo_hr_simulation(single_pa_prob, num_simulations=10000, avg_pas=3.0):
    """Backward-compatible wrapper; now uses analytic Poisson weighting."""
    spread = float(np.clip(_env_float('PA_DISTRIBUTION_STD', 0.70), 0.30, 1.20))
    pa_dist = build_expected_pa_distribution(avg_pas, spread=spread)
    return calculate_game_hr_probability(single_pa_prob, pa_dist)


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
    train_df = build_matchup_weather_features(train_df)
    train_df['batter_pitch_mix_matchup_score'] = build_batter_pitch_mix_matchup_feature(
        statcast_df,
        train_df[['batter', 'pitcher']],
        lookback_days=max(21, _env_int('PREGAME_PITCH_MIX_LOOKBACK_DAYS', 45)),
        min_sample=max(3, _env_int('PREGAME_PITCH_MIX_MIN_SAMPLE', 10)),
        usage_floor=float(np.clip(_env_float('PREGAME_PITCH_MIX_USAGE_FLOOR', 0.12), 0.05, 0.4)),
        damage_threshold=float(np.clip(_env_float('PREGAME_PITCH_MIX_DAMAGE_THRESHOLD', 0.38), 0.15, 0.70)),
    )
    train_df['recent_batter_woba_proxy'] = build_recent_batter_woba_proxy(
        statcast_df,
        train_df[['batter', 'pitcher']],
        lookback_days=max(21, _env_int('RECENT_BATTER_WOBA_LOOKBACK_DAYS', 45)),
        min_sample=max(2, _env_int('RECENT_BATTER_WOBA_MIN_SAMPLE', 4)),
    )
    train_df['recent_pitcher_damage_proxy'] = build_recent_pitcher_damage_proxy(
        statcast_df,
        train_df[['batter', 'pitcher']],
        lookback_days=max(21, _env_int('RECENT_PITCHER_DAMAGE_LOOKBACK_DAYS', 45)),
        min_sample=max(2, _env_int('RECENT_PITCHER_DAMAGE_MIN_SAMPLE', 4)),
    )
    _train_recent_hr = build_batter_recent_hr_features(statcast_df, train_df[['batter', 'pitcher']])
    for _rh_col in _train_recent_hr.columns:
        train_df[_rh_col] = _train_recent_hr[_rh_col].values

    professional_default_cols = {
        'platoon_advantage_multiplier': 1.0,
        'breaking_pitch_vulnerability': 1.0,
        'left_on_right_fade_score': 1.0,
        'reverse_split_anomaly_score': 1.0,
        'ballpark_park_factor': 1.0,
        'porch_advantage_bonus': 1.0,
        'death_valley_penalty': 1.0,
        'would_be_hr_differential': 0.0,
        'bullpen_quality_score_home': 50.0,
        'bullpen_quality_score_away': 50.0,
        'umpire_strike_zone_impact': 1.0,
        'density_altitude_factor': 1.0,
        'weather_extremes_multiplier': 1.0,
        'sportsbook_value_score': 1.0,
        'opp_bullpen_xfip_degradation': 0.0,
        'opp_bullpen_fatigue_multiplier': 1.0,
        'umpire_strike_to_ball_ratio': 1.0,
        'umpire_runs_created_per_game': 4.40,
        'lineup_pa_expectation': 4.22,
        'roof_sealed': 0,
        'pitcher_fear_factor': 0.0,
        'is_elite_power_batter': 0,
    }
    for col, default in professional_default_cols.items():
        if col not in train_df.columns:
            train_df[col] = default
        else:
            train_df[col] = pd.to_numeric(train_df[col], errors='coerce').fillna(default)

    # Rolling mini-batch training: keep a bounded historical window for stability and speed.
    train_df = apply_rolling_training_window(train_df)

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
    recent_minibatch_weights = build_recent_minibatch_weights(train_df)
    sample_weights = np.clip(np.asarray(sample_weights) * recent_minibatch_weights, 0.5, 15.0)
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
        'bat_barrel_trend_delta', 'bat_hard_hit_trend_delta',
        'bat_wrc_plus',  # NEW: Overall offensive value normalized to league average
        'has_platoon_advantage',
        'lineup_pa_expectation',
        
        # Pitcher vulnerability features (HR/9, FB%, hard-hit, barrels)
        'pitch_pa_count', 'pitch_hr_allowed_rate', 'pitch_barrel_allowed_rate',
        'pitch_hard_hit_allowed_rate', 'pitch_sweet_spot_allowed_rate', 'pitch_hr_fb_allowed_rate', 'pitch_days_since_last_start',
        'pitch_fb_allowed_rate', 'pitch_hr_per_9',
        'pitch_avg_velocity',
        'pitch_15pa_hr_rate', 'pitch_30pa_hr_rate',
        'pitch_15pa_barrel_allowed_rate', 'pitch_30pa_barrel_allowed_rate',
        'pitch_15pa_hard_hit_allowed_rate', 'pitch_30pa_hard_hit_allowed_rate',
        'pitch_15pa_fb_allowed_rate', 'pitch_30pa_fb_allowed_rate',
        'pitch_barrel_trend_delta', 'pitch_hard_hit_trend_delta',
        
        # Stadium & Weather Features
        'park_factor', 'temp', 'wind_speed', 'wind_out_component',
        'humidity', 'precipitation', 'pressure',  # NEW: Enhanced weather tracking

        # Matchup & weather edge features
        'ppci_dominance_score', 'dynamic_matchup_grade', 'pitch_arsenal_matchup_score',
        'micro_weather_score', 'lineup_slot_pressure_score', 'lineup_grab_window_score',
        'split_advantage_score', 'flyball_pitcher_target_score', 'hot_streak_contact_score',
        'arsenal_vulnerability_score', 'game_total_context_score',
        'batter_pitch_mix_matchup_score', 'recent_batter_woba_proxy', 'recent_pitcher_damage_proxy',
        'batter_hr_last_7d', 'batter_hr_last_14d', 'batter_hr_rate_7d', 'batter_hr_rate_14d',
        'bat_hr_rate_vs_rhp', 'bat_hr_rate_vs_lhp', 'bat_days_since_last_hr',

        # Professional features used in live scoring and calibration
        'platoon_advantage_multiplier', 'breaking_pitch_vulnerability',
        'left_on_right_fade_score', 'reverse_split_anomaly_score',
        'ballpark_park_factor', 'porch_advantage_bonus', 'death_valley_penalty',
        'would_be_hr_differential', 'bullpen_quality_score_home', 'bullpen_quality_score_away',
        'opp_bullpen_xfip_degradation',
        'umpire_strike_zone_impact', 'density_altitude_factor', 'weather_extremes_multiplier',
        'umpire_strike_to_ball_ratio', 'umpire_runs_created_per_game',
        'sportsbook_value_score', 'pitcher_fear_factor', 'is_elite_power_batter',
        'roof_sealed',
    ]

    simple_baseline_mode = str(os.getenv('SIMPLE_BASELINE_ENABLED', 'false')).strip().lower() in {'1', 'true', 'yes'}
    simple_baseline_model = str(os.getenv('SIMPLE_BASELINE_MODEL', 'lightgbm')).strip().lower()
    if simple_baseline_model not in {'lightgbm', 'xgboost'}:
        simple_baseline_model = 'lightgbm'

    if simple_baseline_mode:
        # Keep only high-signal baseline features; fall back to nearest available proxy when needed.
        core_feature_aliases = [
            ['bat_xslg', 'bat_iso_proxy', 'recent_batter_woba_proxy', 'bat_hr_rate'],
            ['pitch_barrel_allowed_rate', 'pitch_30pa_barrel_allowed_rate', 'pitch_15pa_barrel_allowed_rate'],
            ['park_factor', 'ballpark_park_factor'],
            ['temp'],
            ['has_platoon_advantage', 'platoon_advantage_multiplier', 'split_advantage_score'],
        ]

        resolved_features = []
        for alias_group in core_feature_aliases:
            chosen = next((c for c in alias_group if c in train_df.columns), None)
            if chosen is None:
                chosen = alias_group[0]
                # Create neutral fallback column if no alias exists.
                if chosen not in train_df.columns:
                    if chosen in {'temp'}:
                        train_df[chosen] = 72.0
                    elif chosen in {'park_factor', 'ballpark_park_factor'}:
                        train_df[chosen] = 100.0
                    else:
                        train_df[chosen] = 0.0
            resolved_features.append(chosen)

        features_train = list(dict.fromkeys(resolved_features))
        print(
            "SIMPLE_BASELINE_ENABLED=true -> using 5 core features: "
            + ", ".join(features_train)
        )
    
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
    if simple_baseline_mode:
        features_live = list(features_train)
    
    X_train = train_df[features_train]
    y_train = train_df['is_hr']

    online_sidecar_state = None
    online_sidecar_diag = {'enabled': False, 'applied': False, 'reason': 'not_attempted'}
    if not simple_baseline_mode:
        online_sidecar_state, online_sidecar_diag = update_online_sidecar_model(
            train_df,
            features_train,
            sample_weights,
        )
    else:
        online_sidecar_diag = {'enabled': False, 'applied': False, 'reason': 'simple_baseline_mode'}

    use_model_level_calibration = str(os.getenv('USE_MODEL_LEVEL_CALIBRATION', 'true')).strip().lower() in {'1', 'true', 'yes'}
    model_level_calibration_method = str(os.getenv('MODEL_LEVEL_CALIBRATION_METHOD', 'sigmoid')).strip().lower()
    if model_level_calibration_method not in {'sigmoid', 'isotonic'}:
        model_level_calibration_method = 'sigmoid'
    use_ensemble_platt = str(os.getenv('USE_ENSEMBLE_PLATT_SCALING', 'false')).strip().lower() not in {'0', 'false', 'no'}
    if simple_baseline_mode:
        use_model_level_calibration = False
        use_ensemble_platt = False
    platt_holdout_fraction = float(np.clip(float(os.getenv('PLATT_HOLDOUT_FRACTION', '0.20')), 0.10, 0.40))
    platt_min_rows = max(200, int(float(os.getenv('PLATT_MIN_ROWS', '600'))))

    X_model_fit = X_train
    y_model_fit = y_train
    sample_weights_model_fit = sample_weights
    X_platt_holdout = None
    y_platt_holdout = None
    prior_correction_real_pos_rate = float(pd.to_numeric(y_train, errors='coerce').fillna(0).mean()) if len(y_train) else np.nan
    prior_correction_sampled_pos_rate = prior_correction_real_pos_rate

    use_holdout_split = (use_ensemble_platt or use_model_level_calibration)
    if use_holdout_split and len(X_train) >= platt_min_rows:
        split_idx = int(len(X_train) * (1.0 - platt_holdout_fraction))
        split_idx = max(100, min(split_idx, len(X_train) - 100))
        if split_idx > 0 and split_idx < len(X_train):
            X_model_fit = X_train.iloc[:split_idx].copy()
            y_model_fit = y_train.iloc[:split_idx].copy()
            sample_weights_model_fit = np.asarray(sample_weights)[:split_idx]
            X_platt_holdout = X_train.iloc[split_idx:].copy()
            y_platt_holdout = y_train.iloc[split_idx:].copy()
            print(
                "Calibration holdout split: "
                f"fit_rows={len(X_model_fit)}, holdout_rows={len(X_platt_holdout)}, "
                f"holdout_fraction={platt_holdout_fraction:.2f}"
            )

    downsample_enabled = str(os.getenv('TRAINING_DOWNSAMPLE_ENABLED', 'false')).strip().lower() in {'1', 'true', 'yes'}
    if simple_baseline_mode:
        downsample_enabled = False
    downsample_ratio = float(np.clip(_env_float('TRAINING_NEG_TO_POS_RATIO', 3.0), 1.0, 10.0))
    downsample_strategy = str(os.getenv('TRAINING_DOWNSAMPLE_STRATEGY', 'recent')).strip().lower()
    downsample_random_state = _env_int('TRAINING_DOWNSAMPLE_RANDOM_STATE', 42)
    if downsample_enabled:
        X_model_fit, y_model_fit, sample_weights_model_fit, downsample_diag = _downsample_majority_class(
            X_model_fit,
            y_model_fit,
            sample_weights_model_fit,
            negative_to_positive_ratio=downsample_ratio,
            strategy=downsample_strategy,
            random_state=downsample_random_state,
        )
        if downsample_diag.get('applied', False):
            print(
                "Downsampling applied: "
                f"strategy={downsample_diag.get('strategy')}, "
                f"pos={downsample_diag.get('pos_count')}, "
                f"neg_before={downsample_diag.get('neg_count')}, "
                f"neg_after={downsample_diag.get('target_neg')}, "
                f"neg:pos={downsample_diag.get('neg_to_pos_after'):.2f}"
            )
        else:
            print(
                "Downsampling skipped: "
                f"reason={downsample_diag.get('reason', 'unknown')}, "
                f"pos={downsample_diag.get('pos_count', 'n/a')}, "
                f"neg={downsample_diag.get('neg_count', 'n/a')}"
            )

    prior_correction_sampled_pos_rate = float(pd.to_numeric(y_model_fit, errors='coerce').fillna(0).mean()) if len(y_model_fit) else np.nan
    print(
        "Prior correction rates: "
        f"real_pos_rate={prior_correction_real_pos_rate:.5f}, "
        f"sampled_pos_rate={prior_correction_sampled_pos_rate:.5f}"
    )

    positive_count = int(pd.to_numeric(y_model_fit, errors='coerce').fillna(0).sum())
    negative_count = int(len(y_model_fit) - positive_count)
    raw_scale_pos_weight = max(1.0, min(50.0, negative_count / max(positive_count, 1)))
    scale_pos_weight_override = os.getenv('TRAINING_SCALE_POS_WEIGHT_OVERRIDE')
    if scale_pos_weight_override is not None and str(scale_pos_weight_override).strip() != '':
        try:
            scale_pos_weight = float(scale_pos_weight_override)
            if not np.isfinite(scale_pos_weight):
                raise ValueError("non-finite override")
            scale_pos_weight = round(max(1.0, min(50.0, scale_pos_weight)), 2)
            print(f"[ABLATION] Using hard scale_pos_weight override: {scale_pos_weight:.2f}")
            print(
                "Class imbalance control (fit set): "
                f"positive={positive_count}, negative={negative_count}, "
                f"raw_scale_pos_weight={raw_scale_pos_weight:.2f}, "
                f"override={scale_pos_weight:.2f}"
            )
        except Exception:
            scale_pos_weight_multiplier = float(np.clip(_env_float('TRAINING_SCALE_POS_WEIGHT_MULTIPLIER', 1.0), 0.1, 1.0))
            scale_pos_weight = round(max(1.0, min(50.0, raw_scale_pos_weight * scale_pos_weight_multiplier)), 2)
            print(
                "Class imbalance control (fit set): "
                f"positive={positive_count}, negative={negative_count}, "
                f"raw_scale_pos_weight={raw_scale_pos_weight:.2f}, "
                f"multiplier={scale_pos_weight_multiplier:.2f}, scale_pos_weight={scale_pos_weight:.2f}"
            )
    else:
        scale_pos_weight_multiplier = float(np.clip(_env_float('TRAINING_SCALE_POS_WEIGHT_MULTIPLIER', 1.0), 0.1, 1.0))
        scale_pos_weight = round(max(1.0, min(50.0, raw_scale_pos_weight * scale_pos_weight_multiplier)), 2)
        print(
            "Class imbalance control (fit set): "
            f"positive={positive_count}, negative={negative_count}, "
            f"raw_scale_pos_weight={raw_scale_pos_weight:.2f}, "
            f"multiplier={scale_pos_weight_multiplier:.2f}, scale_pos_weight={scale_pos_weight:.2f}"
        )

    cv_splitter = TimeSeriesSplit(n_splits=3) if TimeSeriesSplit is not None else 3
    base_models = []
    model_names = []

    if simple_baseline_mode:
        if simple_baseline_model == 'lightgbm' and lgb is not None:
            base_models.append(lgb.LGBMClassifier(
                n_estimators=180, max_depth=5, learning_rate=0.04,
                objective='binary', metric='binary_logloss',
                scale_pos_weight=scale_pos_weight, subsample=0.9, colsample_bytree=0.9,
                verbose=-1
            ))
            model_names.append('LightGBM')
        elif simple_baseline_model == 'xgboost' and xgb is not None:
            base_models.append(xgb.XGBClassifier(
                n_estimators=180, max_depth=5, learning_rate=0.04,
                objective='binary:logistic', eval_metric='logloss',
                scale_pos_weight=scale_pos_weight, subsample=0.9, colsample_bytree=0.9
            ))
            model_names.append('XGBoost')
        else:
            # Fallback to whichever tree model is available.
            if lgb is not None:
                base_models.append(lgb.LGBMClassifier(
                    n_estimators=180, max_depth=5, learning_rate=0.04,
                    objective='binary', metric='binary_logloss',
                    scale_pos_weight=scale_pos_weight, subsample=0.9, colsample_bytree=0.9,
                    verbose=-1
                ))
                model_names.append('LightGBM')
            elif xgb is not None:
                base_models.append(xgb.XGBClassifier(
                    n_estimators=180, max_depth=5, learning_rate=0.04,
                    objective='binary:logistic', eval_metric='logloss',
                    scale_pos_weight=scale_pos_weight, subsample=0.9, colsample_bytree=0.9
                ))
                model_names.append('XGBoost')
    elif xgb is not None:
        base_models.append(xgb.XGBClassifier(
            n_estimators=150, max_depth=5, learning_rate=0.04,
            objective='binary:logistic', eval_metric='logloss',
            scale_pos_weight=scale_pos_weight, subsample=0.9, colsample_bytree=0.9
        ))
        model_names.append('XGBoost')

    if (not simple_baseline_mode) and lgb is not None:
        base_models.append(lgb.LGBMClassifier(
            n_estimators=150, max_depth=5, learning_rate=0.04,
            objective='binary', metric='binary_logloss',
            scale_pos_weight=scale_pos_weight, subsample=0.9, colsample_bytree=0.9,
            verbose=-1
        ))
        model_names.append('LightGBM')

    # IMPROVEMENT #7: Ensemble Diversity - Add Random Forest (different approach: bagging vs boosting)
    if (not simple_baseline_mode) and RandomForestClassifier is not None:
        base_models.append(RandomForestClassifier(
            n_estimators=200, max_depth=8, min_samples_split=10,
            class_weight='balanced', random_state=42
        ))
        model_names.append('RandomForest')

    # IMPROVEMENT #7: Add Logistic Regression (captures linear relationships)
    # Wrapped in pipeline with IterativeImputer to preserve feature interactions.
    include_linear_models = str(os.getenv('ENSEMBLE_INCLUDE_LINEAR_MODELS', 'false')).strip().lower() in {'1', 'true', 'yes'}
    if simple_baseline_mode:
        include_linear_models = False
    if include_linear_models:
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import Pipeline
            from sklearn.experimental import enable_iterative_imputer  # noqa: F401
            from sklearn.impute import IterativeImputer

            lr_pipeline = Pipeline([
                ('imputer', IterativeImputer(
                    max_iter=12,
                    random_state=42,
                    initial_strategy='median',
                    skip_complete=True,
                )),
                ('lr', LogisticRegression(max_iter=1000, class_weight='balanced', random_state=42))
            ])
            base_models.append(lr_pipeline)
            model_names.append('LogisticRegression+IterativeImputer')
        except ImportError:
            pass
    else:
        print("Linear models disabled: ENSEMBLE_INCLUDE_LINEAR_MODELS=false")

    # IMPROVEMENT #7: Add Neural Network (captures non-linear interactions)
    # Also wrapped with IterativeImputer for NaN handling.
    include_mlp_model = str(os.getenv('ENSEMBLE_INCLUDE_MLP_MODEL', 'false')).strip().lower() in {'1', 'true', 'yes'}
    if simple_baseline_mode:
        include_mlp_model = False
    if include_mlp_model:
        try:
            from sklearn.neural_network import MLPClassifier
            from sklearn.pipeline import Pipeline
            from sklearn.experimental import enable_iterative_imputer  # noqa: F401
            from sklearn.impute import IterativeImputer

            nn_pipeline = Pipeline([
                ('imputer', IterativeImputer(
                    max_iter=12,
                    random_state=42,
                    initial_strategy='median',
                    skip_complete=True,
                )),
                ('nn', MLPClassifier(hidden_layer_sizes=(128, 64, 32), max_iter=500, random_state=42, early_stopping=True))
            ])
            base_models.append(nn_pipeline)
            model_names.append('NeuralNetwork+IterativeImputer')
        except ImportError:
            pass
    else:
        print("MLP model disabled: ENSEMBLE_INCLUDE_MLP_MODEL=false")

    if not base_models:
        raise ImportError("Missing required ML package: install xgboost, lightgbm, or scikit-learn.")

    trained_models = []
    for m in base_models:
        try:
            m.fit(X_model_fit, y_model_fit, sample_weight=sample_weights_model_fit)
        except Exception as exc:
            msg = str(exc).lower()
            if ('sample_weight' in msg) or ('pipeline.fit does not accept' in msg):
                m.fit(X_model_fit, y_model_fit)
            else:
                raise
        trained_models.append(m)

    print(f"Ensemble base models trained: {', '.join(model_names)}")

    inference_models = trained_models
    member_level_calibrated_count = 0
    if use_model_level_calibration and CalibratedClassifierCV is not None and X_platt_holdout is not None and len(X_platt_holdout) >= 100:
        holdout_labels_for_member_cal = pd.to_numeric(y_platt_holdout, errors='coerce').fillna(0).astype(int)
        if holdout_labels_for_member_cal.nunique() >= 2:
            calibrated_models = []
            for model_name, fitted_model in zip(model_names, trained_models):
                try:
                    # Newer sklearn uses FrozenEstimator for prefit calibration; older releases use cv='prefit'.
                    if FrozenEstimator is not None:
                        cal_model = CalibratedClassifierCV(FrozenEstimator(fitted_model), method=model_level_calibration_method)
                    else:
                        cal_model = CalibratedClassifierCV(fitted_model, method=model_level_calibration_method, cv='prefit')
                    cal_model.fit(X_platt_holdout, holdout_labels_for_member_cal)
                    calibrated_models.append(cal_model)
                    member_level_calibrated_count += 1
                except Exception as exc:
                    calibrated_models.append(fitted_model)
                    print(f"Member calibration fallback ({model_name}): {exc}")
            inference_models = calibrated_models
            print(
                "Per-model holdout calibration complete: "
                f"method={model_level_calibration_method}, calibrated={member_level_calibrated_count}/{len(model_names)}"
            )
        else:
            print("Per-model holdout calibration skipped: holdout has single-class labels.")
    elif use_model_level_calibration:
        print("Per-model holdout calibration skipped: no eligible holdout split.")

    ensemble_weights = np.array([1.0 / max(1, len(trained_models))] * len(trained_models), dtype=float)
    holdout_labels = None
    holdout_per_model = None
    weight_diag = []

    calibration_model = None
    calibration_method = str(os.getenv('ENSEMBLE_CALIBRATION_METHOD', 'platt')).strip().lower()
    if calibration_method not in {'none', 'platt', 'isotonic'}:
        calibration_method = 'platt'
    calibration_blend = float(np.clip(float(os.getenv('ENSEMBLE_CALIBRATION_BLEND', os.getenv('PLATT_BLEND_WEIGHT', '1.00'))), 0.0, 1.0))
    if X_platt_holdout is not None and len(X_platt_holdout) >= 100:
        holdout_labels = pd.to_numeric(y_platt_holdout, errors='coerce').fillna(0).astype(int)
        if holdout_labels.nunique() >= 2:
            try:
                holdout_per_model = [
                    _extract_positive_class_probabilities(m, X_platt_holdout) for m in inference_models
                ]
                ensemble_weights, weight_diag = calculate_ensemble_weights(
                    holdout_labels,
                    holdout_per_model,
                    model_names=model_names,
                )
                if weight_diag:
                    print("Ensemble weights (log-loss aware):")
                    for row in sorted(weight_diag, key=lambda x: x['weight'], reverse=True):
                        print(
                            f"  {row['model']:<28} "
                            f"logloss={row['log_loss']:.5f} weight={row['weight']:.3f}"
                        )
            except Exception as exc:
                print(f"Ensemble holdout weighting unavailable: {exc}")
        else:
            print("Ensemble holdout weighting skipped: holdout has single-class labels.")

    if use_ensemble_platt and member_level_calibrated_count > 0:
        print("Ensemble-level calibration skipped because per-model calibration is active.")
    elif use_ensemble_platt and LogisticRegression is not None and holdout_labels is not None and holdout_labels.nunique() >= 2 and holdout_per_model is not None:
        if holdout_labels.nunique() >= 2:
            try:
                holdout_raw = weighted_ensemble_probabilities(holdout_per_model, ensemble_weights)
                raw_metrics = calculate_probability_metrics(holdout_labels, holdout_raw)

                holdout_calibrated = None
                if calibration_method == 'isotonic' and IsotonicRegression is not None:
                    calibration_model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds='clip')
                    calibration_model.fit(np.asarray(holdout_raw).reshape(-1), holdout_labels)
                    holdout_calibrated = np.asarray(calibration_model.predict(np.asarray(holdout_raw).reshape(-1)))
                elif calibration_method == 'none':
                    calibration_model = None
                else:
                    calibration_method = 'platt'
                    calibration_model = LogisticRegression(max_iter=1000, solver='lbfgs')
                    calibration_model.fit(np.asarray(holdout_raw).reshape(-1, 1), holdout_labels)
                    holdout_calibrated = calibration_model.predict_proba(np.asarray(holdout_raw).reshape(-1, 1))[:, 1]

                if holdout_calibrated is not None:
                    cal_metrics = calculate_probability_metrics(holdout_labels, holdout_calibrated)
                    print(
                        f"Ensemble {calibration_method} calibration (holdout-only): "
                        f"raw_brier={raw_metrics['brier_score']:.5f} -> cal_brier={cal_metrics['brier_score']:.5f}, "
                        f"raw_logloss={raw_metrics['log_loss']:.5f} -> cal_logloss={cal_metrics['log_loss']:.5f}, "
                        f"blend={calibration_blend:.2f}"
                    )
                else:
                    print("Ensemble calibration: method=none (raw probabilities retained)")
            except Exception as exc:
                calibration_model = None
                print(f"Ensemble holdout calibration unavailable: {exc}")
        else:
            print("Ensemble holdout calibration skipped: holdout has single-class labels.")

    if X_platt_holdout is None and len(X_model_fit) >= 100:
        try:
            fit_per_model = [
                _extract_positive_class_probabilities(m, X_model_fit) for m in inference_models
            ]
            ensemble_weights, weight_diag = calculate_ensemble_weights(
                y_model_fit,
                fit_per_model,
                model_names=model_names,
            )
            if weight_diag:
                print("Ensemble weights (fit-set fallback):")
                for row in sorted(weight_diag, key=lambda x: x['weight'], reverse=True):
                    print(
                        f"  {row['model']:<28} "
                        f"logloss={row['log_loss']:.5f} weight={row['weight']:.3f}"
                    )
        except Exception as exc:
            print(f"Ensemble weight fallback unavailable: {exc}")

    try:
        train_per_model = [
            _extract_positive_class_probabilities(m, X_model_fit) for m in inference_models
        ]
        train_probabilities = weighted_ensemble_probabilities(train_per_model, ensemble_weights)
        train_metrics = calculate_probability_metrics(y_model_fit, train_probabilities)
        print(
            "Training probability metrics: "
            f"log_loss={train_metrics['log_loss']:.4f}, brier={train_metrics['brier_score']:.4f}"
        )
    except Exception as exc:
        print(f"Training probability metrics unavailable: {exc}")

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

    # Pregame proxy for live count-based pitch predictability feature.
    # Uses recent pitch-mix vulnerability instead of in-game 2-0 / 3-1 state assumptions.
    pitch_mix_vuln_lookup, pitch_mix_vuln_debug = build_pregame_primary_weapon_vulnerability_lookup(
        statcast_df,
        lookback_days=max(21, _env_int('PREGAME_PITCH_MIX_LOOKBACK_DAYS', 45)),
        min_sample=max(10, _env_int('PREGAME_PITCH_MIX_MIN_SAMPLE', 40)),
        usage_floor=float(np.clip(_env_float('PREGAME_PITCH_MIX_USAGE_FLOOR', 0.12), 0.05, 0.4)),
    )
    live['primary_weapon_vulnerable_pitch_count'] = (
        pd.to_numeric(live.get('pitcher'), errors='coerce')
        .map(lambda x: pitch_mix_vuln_lookup.get(int(x), 0) if pd.notna(x) else 0)
        .fillna(0)
        .astype(float)
    )
    live['batter_pitch_mix_matchup_score'] = build_batter_pitch_mix_matchup_feature(
        statcast_df,
        live[['batter', 'pitcher']],
        lookback_days=max(21, _env_int('PREGAME_PITCH_MIX_LOOKBACK_DAYS', 45)),
        min_sample=max(3, _env_int('PREGAME_PITCH_MIX_MIN_SAMPLE', 10)),
        usage_floor=float(np.clip(_env_float('PREGAME_PITCH_MIX_USAGE_FLOOR', 0.12), 0.05, 0.4)),
        damage_threshold=float(np.clip(_env_float('PREGAME_PITCH_MIX_DAMAGE_THRESHOLD', 0.38), 0.15, 0.70)),
    )
    live['recent_batter_woba_proxy'] = build_recent_batter_woba_proxy(
        statcast_df,
        live[['batter', 'pitcher']],
        lookback_days=max(21, _env_int('RECENT_BATTER_WOBA_LOOKBACK_DAYS', 45)),
        min_sample=max(2, _env_int('RECENT_BATTER_WOBA_MIN_SAMPLE', 4)),
    )
    live['recent_pitcher_damage_proxy'] = build_recent_pitcher_damage_proxy(
        statcast_df,
        live[['batter', 'pitcher']],
        lookback_days=max(21, _env_int('RECENT_PITCHER_DAMAGE_LOOKBACK_DAYS', 45)),
        min_sample=max(2, _env_int('RECENT_PITCHER_DAMAGE_MIN_SAMPLE', 4)),
    )
    _recent_hr_cols = build_batter_recent_hr_features(statcast_df, live[['batter', 'pitcher']])
    for _rh_col in _recent_hr_cols.columns:
        live[_rh_col] = _recent_hr_cols[_rh_col].values
    if pitch_mix_vuln_debug.get('pitchers_seen', 0) > 0:
        print(
            "Pregame pitch-mix vulnerability proxy: "
            f"pitchers_flagged={pitch_mix_vuln_debug.get('pitchers_flagged', 0)}/"
            f"{pitch_mix_vuln_debug.get('pitchers_seen', 0)}"
        )

    live_dataflow_issues = validate_model_dataflow(
        train_df,
        live,
        required_features=features_live,
    )
    if live_dataflow_issues:
        print("ℹ️ Live dataflow validation:")
        for issue in live_dataflow_issues:
            print(f"  - {issue}")

    # Fix: unknown batters get MLB league-average features instead of zeros so they aren't predicted at 0%.
    _batter_league_avg = {
        'bat_pa_count': 0,
        'bat_hr_rate': 0.033,
        'bat_barrel_rate': 0.080,
        'bat_hard_hit_rate': 0.380,
        'bat_sweet_spot_rate': 0.360,
    }
    for col, default in _batter_league_avg.items():
        if col in live.columns:
            live[col] = live[col].fillna(default)
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
    live = build_matchup_weather_features(live)
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
    for _rh_col in ['batter_hr_last_7d', 'batter_hr_last_14d']:
        if _rh_col in live.columns:
            live[_rh_col] = live[_rh_col].fillna(0.0)
    for _rh_col in ['batter_hr_rate_7d', 'batter_hr_rate_14d']:
        if _rh_col in live.columns:
            live[_rh_col] = live[_rh_col].fillna(0.033)
    for _col, _default in [('bat_hr_rate_vs_rhp', 0.033), ('bat_hr_rate_vs_lhp', 0.033),
                            ('bat_days_since_last_hr', 30.0)]:
        if _col in live.columns:
            live[_col] = live[_col].fillna(_default)
        else:
            live[_col] = _default
    # Use handedness-split HR rate when pitcher hand is known.
    if 'pitcher_hand' in live.columns and 'bat_hr_rate_vs_rhp' in live.columns:
        p_throws = live['pitcher_hand'].astype(str).str.upper().str.strip()
        live['bat_hr_rate_vs_rhp'] = live['bat_hr_rate_vs_rhp'].fillna(0.033)
        live['bat_hr_rate_vs_lhp'] = live['bat_hr_rate_vs_lhp'].fillna(0.033)
    if 'lineup_pa_expectation' in live.columns:
        live['lineup_pa_expectation'] = pd.to_numeric(live['lineup_pa_expectation'], errors='coerce').fillna(4.22)
    else:
        live['lineup_pa_expectation'] = live.get('batting_order_slot', pd.Series([5] * len(live))).apply(get_lineup_slot_pa_expectation)
    live['game_time'] = live['game_time'].fillna('') if 'game_time' in live.columns else ''
    live['home_team'] = live['home_team'].fillna('') if 'home_team' in live.columns else ''
    live['away_team'] = live['away_team'].fillna('') if 'away_team' in live.columns else ''
    live['batter_hand'] = live['batter_hand'].fillna('R') if 'batter_hand' in live.columns else 'R'
    live['pitcher_hand'] = live['pitcher_hand'].fillna('R') if 'pitcher_hand' in live.columns else 'R'
    if 'roof_sealed' in live.columns:
        live['roof_sealed'] = pd.to_numeric(live['roof_sealed'], errors='coerce').fillna(0).astype(int)
    else:
        live['roof_sealed'] = 0
    
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

    live['opp_bullpen_xfip_degradation'] = live.apply(
        lambda r: bullpen_xfip_degradation_index(
            r.get('bullpen_quality_score_home', 50.0),
            r.get('bullpen_quality_score_away', 50.0),
            bool(r.get('is_home_game', False)),
        ),
        axis=1,
    )
    
    # 3. Umpire Strike Zone Impact
    live['umpire_strike_zone_impact'] = 1.0
    live['umpire_strike_to_ball_ratio'] = 1.0
    live['umpire_runs_created_per_game'] = 4.40
    if get_todays_umpires is not None:
        try:
            umpires = get_todays_umpires()
            for idx, row in live.iterrows():
                game_id = row.get('game_id')
                if game_id and game_id in umpires:
                    profile = umpires[game_id].get('profile', {})
                    impact = float(profile.get('impact', 1.0) or 1.0)
                    zone_size = float(profile.get('zone_size', 1.0) or 1.0)
                    strike_ball_ratio = profile.get('strike_to_ball_ratio')
                    if strike_ball_ratio is None:
                        strike_ball_ratio = float(np.clip(1.0 + ((zone_size - 1.0) * 1.2), 0.75, 1.30))
                    runs_created_pg = profile.get('runs_created_per_game')
                    if runs_created_pg is None:
                        runs_created_pg = float(np.clip(4.40 * impact, 3.2, 6.8))

                    live.at[idx, 'umpire_strike_zone_impact'] = impact
                    live.at[idx, 'umpire_strike_to_ball_ratio'] = float(strike_ball_ratio)
                    live.at[idx, 'umpire_runs_created_per_game'] = float(runs_created_pg)
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
        'opp_bullpen_xfip_degradation',
        'umpire_strike_zone_impact', 'density_altitude_factor',
        'weather_extremes_multiplier', 'sportsbook_value_score',
        'umpire_strike_to_ball_ratio', 'umpire_runs_created_per_game',
        'lineup_pa_expectation', 'roof_sealed'
    ]
    
    for col in professional_features:
        if col not in live.columns:
            if col in {'roof_sealed'}:
                live[col] = 0
            elif col in {'umpire_runs_created_per_game'}:
                live[col] = 4.40
            elif col in {'lineup_pa_expectation'}:
                live[col] = 4.22
            elif col in {'opp_bullpen_xfip_degradation'}:
                live[col] = 0.0
            elif 'score' in col or 'multiplier' in col or 'impact' in col or 'factor' in col or 'value' in col or 'ratio' in col:
                live[col] = 1.0
            else:
                live[col] = 50.0
        else:
            if col in {'roof_sealed'}:
                live[col] = pd.to_numeric(live[col], errors='coerce').fillna(0).astype(int)
            elif col in {'umpire_runs_created_per_game'}:
                live[col] = pd.to_numeric(live[col], errors='coerce').fillna(4.40)
            elif col in {'lineup_pa_expectation'}:
                live[col] = pd.to_numeric(live[col], errors='coerce').fillna(4.22)
            elif col in {'opp_bullpen_xfip_degradation'}:
                live[col] = pd.to_numeric(live[col], errors='coerce').fillna(0.0)
            elif 'score' in col or 'multiplier' in col or 'impact' in col or 'factor' in col or 'value' in col or 'ratio' in col:
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

    print_x_today_diagnostics(live, features_train, tag='X_today', sample_rows=5)

    X_live = live[features_train]

    # Train-serving skew diagnostics: compare numeric feature distributions at fit vs live inference.
    try:
        train_num = X_model_fit.apply(pd.to_numeric, errors='coerce')
        live_num = X_live.apply(pd.to_numeric, errors='coerce')
        skew_rows = []
        for col in features_train:
            t = pd.to_numeric(train_num[col], errors='coerce').replace([np.inf, -np.inf], np.nan).dropna()
            l = pd.to_numeric(live_num[col], errors='coerce').replace([np.inf, -np.inf], np.nan).dropna()
            if t.empty or l.empty:
                continue
            t_q01 = float(t.quantile(0.01))
            t_q99 = float(t.quantile(0.99))
            l_med = float(l.median())
            l_q90 = float(l.quantile(0.90))
            out_of_band_rate = float(((l < t_q01) | (l > t_q99)).mean())
            skew_rows.append({
                'feature': col,
                'train_q01': t_q01,
                'train_q99': t_q99,
                'live_median': l_med,
                'live_q90': l_q90,
                'live_out_of_band_rate': out_of_band_rate,
            })
        if skew_rows:
            skew_df = pd.DataFrame(skew_rows).sort_values('live_out_of_band_rate', ascending=False)
            top_skew = skew_df.head(10)
            print("Feature alignment check (train vs live): top out-of-band features")
            print(top_skew.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    except Exception as _skew_err:
        print(f"Feature alignment check skipped: {_skew_err}")

    all_probs = [_extract_positive_class_probabilities(m, X_live) for m in inference_models]
    probs = weighted_ensemble_probabilities(all_probs, ensemble_weights)
    live['ensemble_raw_prob'] = np.asarray(probs).reshape(-1)

    use_prior_correction = str(os.getenv('PRIOR_CORRECTION_ENABLED', 'true')).strip().lower() in {'1', 'true', 'yes'}
    if use_prior_correction:
        probs_corrected, prior_diag = _apply_prior_probability_correction(
            probs,
            prior_correction_real_pos_rate,
            prior_correction_sampled_pos_rate,
        )
        if prior_diag.get('applied'):
            probs = probs_corrected
            live['ensemble_prior_corrected_prob'] = np.asarray(probs_corrected).reshape(-1)
            live['prior_correction_weight_factor'] = float(prior_diag.get('weight_factor', 1.0))
            live['prior_real_pos_rate'] = float(prior_diag.get('real_pos_rate', np.nan))
            live['prior_sampled_pos_rate'] = float(prior_diag.get('sampled_pos_rate', np.nan))
            print(
                "Prior correction applied: "
                f"w={prior_diag.get('weight_factor', 1.0):.5f}, "
                f"real={prior_diag.get('real_pos_rate', np.nan):.5f}, "
                f"sampled={prior_diag.get('sampled_pos_rate', np.nan):.5f}"
            )
        else:
            print(f"Prior correction skipped: {prior_diag.get('reason', 'unknown')}")
    if 'ensemble_prior_corrected_prob' not in live.columns:
        live['ensemble_prior_corrected_prob'] = np.nan

    # =====================================================================
    # PROFESSIONAL UPGRADE 1: PA Projection + Poisson-Weighted Conversion
    # =====================================================================
    # Project batting order-based PA count for each batter.
    order_slots = live.get('batting_order_slot', pd.Series([5] * len(live))).fillna(5).astype(int).clip(1, 9)
    slot_projected_pas = order_slots.apply(project_batting_order_pa)
    lineup_pa_expectation = pd.to_numeric(
        live.get('lineup_pa_expectation', slot_projected_pas),
        errors='coerce'
    ).fillna(slot_projected_pas)

    implied_team_total_col = None
    for candidate in ['team_implied_total', 'implied_team_total', 'vegas_team_total']:
        if candidate in live.columns:
            implied_team_total_col = candidate
            break

    if implied_team_total_col is not None:
        implied_team_totals = pd.to_numeric(live.get(implied_team_total_col), errors='coerce')
    else:
        implied_team_totals = pd.Series([np.nan] * len(live), index=live.index)

    game_totals = pd.to_numeric(live.get('game_total', pd.Series([np.nan] * len(live), index=live.index)), errors='coerce')
    pa_dist_spread = float(np.clip(_env_float('PA_DISTRIBUTION_STD', 0.70), 0.30, 1.20))

    projected_pas = []
    game_level_probs = []
    for p_pa, base_pa, team_total, game_total in zip(probs, lineup_pa_expectation, implied_team_totals, game_totals):
        expected_pa = adjust_expected_pa_for_context(
            base_pa,
            team_implied_total=team_total,
            game_total=game_total,
        )
        pa_dist = build_expected_pa_distribution(expected_pa, spread=pa_dist_spread)
        game_prob = calculate_game_hr_probability(p_pa, pa_dist)
        projected_pas.append(expected_pa)
        game_level_probs.append(game_prob)

    live['projected_pas'] = pd.Series(projected_pas, index=live.index).round(3)
    live['pa_distribution_std'] = pa_dist_spread
    live['pa_conversion_method'] = 'poisson_weighted_pa_dist'

    # Convert HR/PA into single-game HR probability analytically.
    probs = np.asarray(game_level_probs)
    base_model_probs = probs.copy()
    
    # =====================================================================
    # IMPROVEMENTS #1-3: PITCHER FORM, BATTER STREAKS, PARK ADJUSTMENT
    # =====================================================================
    print("Applying elite enhancements: pitcher form, park factors, bullpen fatigue...")
    
    # IMPROVEMENT #1: Apply pitcher recent form tracking
    pitcher_form_boosts = np.ones(len(live))
    for idx, pitcher_id in enumerate(live.get('pitcher', pd.Series()).values):
        if pd.notna(pitcher_id) and pitcher_id in statcast_df['pitcher'].values:
            form_mult = get_pitcher_recent_form(statcast_df, pitcher_id, lookback_games=5)
            pitcher_form_boosts[idx] = form_mult
    
    # IMPROVEMENT #3: Apply park-adjusted metrics
    park_adjustments = np.ones(len(live))
    for idx, row in live.iterrows():
        home_team = row.get('home_team', '')
        batter_hand = row.get('stand', 'R')
        park_adj = apply_park_adjustment(1.0, batter_hand, home_team, '')
        park_adjustments[idx] = park_adj

    # NEW: Bullpen fatigue multiplier based on top reliever workload in trailing 48h.
    bullpen_fatigue_map, bullpen_fatigue_debug = build_bullpen_fatigue_multiplier_map(
        statcast_df,
        lookback_days=max(7, _env_int('BULLPEN_FATIGUE_LOOKBACK_DAYS', 14)),
        top_n_arms=max(1, _env_int('BULLPEN_FATIGUE_TOP_ARMS', 3)),
        fatigue_pitch_count=max(10, _env_int('BULLPEN_FATIGUE_PITCH_THRESHOLD', 20)),
        boost_multiplier=float(np.clip(_env_float('BULLPEN_FATIGUE_BOOST_MULTIPLIER', 1.10), 1.0, 1.25)),
    )
    bullpen_fatigue_boosts = np.ones(len(live))
    for idx, row in live.iterrows():
        try:
            opp_team = row.get('away_team') if bool(row.get('is_home_game', False)) else row.get('home_team')
            opp_key = str(opp_team or '').strip().upper()
            bullpen_fatigue_boosts[idx] = float(bullpen_fatigue_map.get(opp_key, 1.0))
        except Exception:
            bullpen_fatigue_boosts[idx] = 1.0
    live['opp_bullpen_fatigue_multiplier'] = bullpen_fatigue_boosts
    
    # Apply combined pre-calibration boosts.
    # NOTE: hot-streak manual multiplier removed to avoid double-counting with time-decay learning.
    combined_boosts = pitcher_form_boosts * park_adjustments * bullpen_fatigue_boosts
    base_model_probs = np.clip(base_model_probs * combined_boosts, 0.0, 1.0)

    live = build_cluster_matchup_probabilities(live)
    cluster_probs = pd.to_numeric(live.get('cluster_platoon_prob', 0.0), errors='coerce').fillna(0.0).values
    cluster_blend = np.clip((base_model_probs * 0.88) + (cluster_probs * 0.12), 0.0, 1.0)
    base_model_probs = np.clip(cluster_blend, 0.0, 1.0)

    # Expert-style matchup and environment boosts for stronger signal separation.
    expert_boosted_probs = apply_expert_signal_boosts(live, base_model_probs)
    base_model_probs = np.clip(expert_boosted_probs, 0.0, 1.0)
    if 'specific_day_upside_score' in live.columns:
        live['specific_day_upside_score'] = pd.to_numeric(live['specific_day_upside_score'], errors='coerce').fillna(0.0)
        live['upside_boost_multiplier'] = pd.to_numeric(live['upside_boost_multiplier'], errors='coerce').fillna(1.0)
    
    elite_enhancements_applied = (
        (pitcher_form_boosts != 1.0).sum()
        + (park_adjustments != 1.0).sum()
        + (bullpen_fatigue_boosts > 1.0).sum()
    )
    if bullpen_fatigue_debug.get('teams_seen', 0) > 0:
        print(
            "Bullpen fatigue multiplier: "
            f"teams_flagged={bullpen_fatigue_debug.get('teams_flagged', 0)}/"
            f"{bullpen_fatigue_debug.get('teams_seen', 0)}"
        )
    print(f"✅ Elite enhancements applied: {elite_enhancements_applied} matchups boosted/adjusted")
    
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
    if simple_baseline_mode:
        prob_mode, physics_weight = 'base', 0.0
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
    # The earlier cap was too conservative for rare-event bets, so we now allow a stronger
    # top-end lift for the most favorable matchups while keeping the rest of the slate sane.
    # This is intentionally a bit looser so that plausible HR candidates are not suppressed.
    stacked_multiplier_cap = np.where(
        base_model_probs >= 0.35,
        1.32,
        np.where(base_model_probs >= 0.20, 1.48, 1.70)
    )
    safe_base = np.clip(base_model_probs, 1e-6, 1.0)
    observed_multiplier = probs / safe_base
    capped_multiplier = np.minimum(observed_multiplier, stacked_multiplier_cap)
    probs = np.clip(safe_base * capped_multiplier, 0.0, 1.0)
    live['stacked_boost_multiplier_raw'] = observed_multiplier
    live['stacked_boost_multiplier_cap'] = stacked_multiplier_cap
    live['stacked_boost_multiplier_final'] = capped_multiplier

    # Final calibration wrapper must run after all pre-model and post-model multipliers.
    if calibration_model is not None:
        try:
            probs_raw = np.asarray(probs).reshape(-1)
            if calibration_method == 'isotonic':
                probs_cal = np.asarray(calibration_model.predict(probs_raw.reshape(-1)))
            else:
                probs_cal = calibration_model.predict_proba(probs_raw.reshape(-1, 1))[:, 1]
            probs = np.clip(((1.0 - calibration_blend) * probs_raw) + (calibration_blend * probs_cal), 0.0, 1.0)
            live['ensemble_calibrated_prob'] = probs_cal
            live['ensemble_calibration_method'] = calibration_method
            live['ensemble_calibration_blend_weight'] = calibration_blend
        except Exception as exc:
            print(f"Final holdout calibration skipped: {exc}")

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

    # Direct pitcher-damage signal boost: recent_pitcher_damage_proxy has ~0.33 correlation
    # with actual HRs but the ML model under-weights it. Apply as a final multiplier.
    use_damage_boost = str(os.getenv('PITCHER_DAMAGE_BOOST_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
    if use_damage_boost and 'recent_pitcher_damage_proxy' in live.columns:
        damage_proxy = pd.to_numeric(live['recent_pitcher_damage_proxy'], errors='coerce').fillna(0.0)
        damage_center = float(np.clip(_env_float('PITCHER_DAMAGE_CENTER', 0.40), 0.10, 0.60))
        damage_strength = float(np.clip(_env_float('PITCHER_DAMAGE_BOOST_STRENGTH', 1.25), 0.0, 3.0))
        # 0.0 means no statcast match (pitcher ID missing) — treat as neutral, not penalized
        damage_proxy_adj = damage_proxy.where(damage_proxy > 0.0, damage_center)
        damage_boost = np.clip(1.0 + (damage_proxy_adj.values - damage_center) * damage_strength, 0.60, 1.45)
        probs = np.clip(probs * damage_boost, 0.0, 0.99)
        live['pitcher_damage_boost'] = damage_boost

    # Optional conservative online sidecar blend (bounded adjustment around base ensemble).
    live['pre_sidecar_prob'] = np.asarray(probs).reshape(-1)
    if simple_baseline_mode:
        sidecar_probs = None
        sidecar_blend_diag = {
            'enabled': False,
            'applied': False,
            'reason': 'simple_baseline_mode',
            'blend_weight': 0.0,
            'max_delta': 0.0,
            'update_count': int(online_sidecar_diag.get('update_count', 0) or 0),
        }
    else:
        probs, sidecar_probs, sidecar_blend_diag = apply_online_sidecar_blend(
            probs,
            live,
            features_train,
            sidecar_state=online_sidecar_state,
        )
    if sidecar_probs is None:
        live['sidecar_online_prob'] = np.nan
    else:
        live['sidecar_online_prob'] = sidecar_probs
    live['sidecar_blend_applied'] = int(bool(sidecar_blend_diag.get('applied', False)))
    live['sidecar_blend_weight'] = float(sidecar_blend_diag.get('blend_weight', 0.0) or 0.0)
    live['sidecar_max_delta'] = float(sidecar_blend_diag.get('max_delta', 0.0) or 0.0)
    live['sidecar_update_count'] = int(sidecar_blend_diag.get('update_count', 0) or 0)
    live['sidecar_training_applied'] = int(bool(online_sidecar_diag.get('applied', False)))
    live['sidecar_training_update_count'] = int(online_sidecar_diag.get('update_count', 0) or 0)

    live['pred_hr_prob'] = probs
    if not simple_baseline_mode:
        if 'game_pk' in live.columns:
            game_count_value = int(pd.to_numeric(live['game_pk'], errors='coerce').dropna().nunique())
        else:
            game_count_value = max(1, int(np.ceil(len(live) / 18.0)))
        live = apply_daily_hr_volume_constraints(
            live,
            game_count=max(1, game_count_value),
            avg_hr_per_game=2.5,
        )
        live = apply_poisson_hr_filter(live, k=5, p_threshold=0.04, min_game_prob=0.18)
        monotonic_gamma = _env_float('MONOTONIC_CALIBRATION_GAMMA', 1.20)
        monotonic_cap = _env_float('MONOTONIC_CALIBRATION_CAP', 0.30)
        monotonic_boost = _env_float('MONOTONIC_CALIBRATION_TOP_SIGNAL_BOOST', 0.010)
        live = apply_monotonic_prob_calibration(
            live,
            gamma=monotonic_gamma,
            cap=monotonic_cap,
            top_signal_boost=monotonic_boost,
        )
    else:
        print("Simple baseline mode: skipped sidecar and post-model probability transforms.")

    use_recent_calibration = str(os.getenv('RECENT_CALIBRATION_CORRECTION_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
    if simple_baseline_mode:
        use_recent_calibration = False
    if use_recent_calibration:
        recent_cal_diag = {'applied': False, 'reason': 'not_run'}
        recent_days = max(3, _env_int('RECENT_CALIBRATION_DAYS', 7))
        recent_alpha = float(np.clip(_env_float('RECENT_CALIBRATION_ALPHA', 0.95), 0.0, 1.0))
        recent_min_rows = max(200, _env_int('RECENT_CALIBRATION_MIN_ROWS', 600))
        recent_min_files = max(2, _env_int('RECENT_CALIBRATION_MIN_FILES', 3))
        recent_min_mult = float(np.clip(_env_float('RECENT_CALIBRATION_MIN_MULTIPLIER', 0.25), 0.2, 1.2))
        recent_max_mult = float(np.clip(_env_float('RECENT_CALIBRATION_MAX_MULTIPLIER', 1.40), 1.0, 2.5))

        live, recent_cal_diag = apply_recent_calibration_correction(
            live,
            days_lookback=recent_days,
            alpha=recent_alpha,
            min_rows=recent_min_rows,
            min_files=recent_min_files,
            min_multiplier=recent_min_mult,
            max_multiplier=recent_max_mult,
        )
        if recent_cal_diag.get('applied'):
            print(
                "Recent calibration correction applied: "
                f"mult={recent_cal_diag.get('correction', 1.0):.3f}, "
                f"pred_mean={recent_cal_diag.get('pred_mean', 0.0):.4f}, "
                f"actual_mean={recent_cal_diag.get('actual_mean', 0.0):.4f}, "
                f"rows={int(recent_cal_diag.get('rows', 0))}"
            )
        else:
            print(
                "Recent calibration correction skipped: "
                f"{recent_cal_diag.get('reason', 'unknown')}"
            )
        live['recent_calibration_multiplier'] = float(recent_cal_diag.get('correction', 1.0))
        live['recent_calibration_ratio_raw'] = float(recent_cal_diag.get('raw_ratio', 1.0))
        live['recent_calibration_pred_mean'] = float(recent_cal_diag.get('pred_mean', 0.0))
        live['recent_calibration_actual_mean'] = float(recent_cal_diag.get('actual_mean', 0.0))
    else:
        live['recent_calibration_multiplier'] = 1.0
        live['recent_calibration_ratio_raw'] = 1.0
        live['recent_calibration_pred_mean'] = 0.0
        live['recent_calibration_actual_mean'] = 0.0

    use_empirical_calibration = str(os.getenv('EMPIRICAL_CALIBRATION_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
    if simple_baseline_mode:
        use_empirical_calibration = False
    if use_empirical_calibration:
        empirical_days = max(5, _env_int('EMPIRICAL_CALIBRATION_DAYS', 14))
        empirical_blend = float(np.clip(_env_float('EMPIRICAL_CALIBRATION_BLEND', 0.90), 0.0, 1.0))
        empirical_min_rows = max(250, _env_int('EMPIRICAL_CALIBRATION_MIN_ROWS', 500))
        empirical_min_files = max(2, _env_int('EMPIRICAL_CALIBRATION_MIN_FILES', 3))
        empirical_unique_probs = max(10, _env_int('EMPIRICAL_CALIBRATION_MIN_UNIQUE_PROBS', 30))
        empirical_max_delta = float(np.clip(_env_float('EMPIRICAL_CALIBRATION_MAX_DELTA', 0.50), 0.05, 0.60))

        live, empirical_diag = apply_recent_empirical_calibration(
            live,
            days_lookback=empirical_days,
            blend=empirical_blend,
            min_rows=empirical_min_rows,
            min_files=empirical_min_files,
            min_unique_probs=empirical_unique_probs,
            max_delta=empirical_max_delta,
        )
        if empirical_diag.get('applied'):
            print(
                "Empirical calibration applied: "
                f"blend={empirical_diag.get('blend', 0.0):.2f}, "
                f"mean_abs_delta={empirical_diag.get('mean_abs_delta', 0.0):.4f}, "
                f"pred_mean={empirical_diag.get('pred_mean_before', 0.0):.4f}->"
                f"{empirical_diag.get('pred_mean_after', 0.0):.4f}, "
                f"eval_actual_mean={empirical_diag.get('actual_mean_eval', 0.0):.4f}"
            )
        else:
            print(
                "Empirical calibration skipped: "
                f"{empirical_diag.get('reason', 'unknown')}"
            )
        live['empirical_calibration_applied'] = int(bool(empirical_diag.get('applied', False)))
        live['empirical_calibration_blend'] = float(empirical_diag.get('blend', empirical_blend))
        live['empirical_calibration_mean_abs_delta'] = float(empirical_diag.get('mean_abs_delta', 0.0))
    else:
        live['empirical_calibration_applied'] = 0
        live['empirical_calibration_blend'] = 0.0
        live['empirical_calibration_mean_abs_delta'] = 0.0

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
    batter_sample_sizes = []
    pred_prob_series = []
    
    for idx, row in live.iterrows():
        batter_id = row.get('batter', None)
        pred_prob = row['pred_hr_prob']
        
        # Get batter consistency (0-1 scale, higher = more consistent)
        batter_sample_size = 30
        if pd.notna(batter_id) and ('batter' in statcast_df.columns):
            batter_rows = statcast_df[statcast_df['batter'] == batter_id]
            batter_sample_size = int(len(batter_rows)) if len(batter_rows) > 0 else 30
            consistency = get_batter_consistency(statcast_df, batter_id)
        else:
            consistency = 0.5
        batter_sample_size = int(np.clip(batter_sample_size, 12, 300))
        consistency_scores.append(consistency)
        batter_sample_sizes.append(batter_sample_size)
        pred_prob_series.append(float(pred_prob))
        
        # Calculate confidence interval
        lower, upper = calculate_confidence_interval(pred_prob, sample_size=batter_sample_size)
        confidence_lower.append(lower)
        confidence_upper.append(upper)
        
        # Estimate reliability level
        reliability = estimate_model_reliability(pred_prob, consistency, sample_size=batter_sample_size)
        reliability_levels.append(reliability)

    # If labels collapse to one tier, use slate-relative fallback ranks to preserve
    # meaningful color separation while staying tied to model probability + consistency.
    unique_levels = set(str(x).upper() for x in reliability_levels)
    n_high_raw = sum(1 for r in reliability_levels if str(r).upper() == 'HIGH')
    # Fire fallback when fewer than 5% are HIGH, regardless of whether MEDIUM exists.
    if n_high_raw < max(1, int(len(reliability_levels) * 0.05)) and len(reliability_levels) >= 20:
        probs_np = pd.to_numeric(pd.Series(pred_prob_series), errors='coerce').fillna(0.0)
        cons_np = pd.to_numeric(pd.Series(consistency_scores), errors='coerce').fillna(0.5)
        prob_rank = probs_np.rank(pct=True)
        sample_rank = pd.Series(batter_sample_sizes).rank(pct=True)
        composite = (prob_rank * 0.60) + (cons_np * 0.30) + (sample_rank * 0.10)

        high_cut = float(composite.quantile(0.85))
        med_cut = float(composite.quantile(0.50))
        fallback_levels = []
        for i in range(len(composite)):
            if float(composite.iloc[i]) >= high_cut:
                fallback_levels.append('HIGH')
            elif float(composite.iloc[i]) >= med_cut:
                fallback_levels.append('MEDIUM')
            else:
                fallback_levels.append('LOW')
        reliability_levels = fallback_levels
    
    live['confidence_lower_95pct'] = confidence_lower
    live['confidence_upper_95pct'] = confidence_upper
    live['model_reliability'] = reliability_levels
    live['batter_consistency_score'] = consistency_scores
    live['batter_sample_size_60d'] = batter_sample_sizes

    # Reliability-aware hard ceilings to prevent unsupported top-end inflation.
    rel_upper = live['model_reliability'].astype(str).str.upper()
    rel_cap_high = float(np.clip(_env_float('RELIABILITY_CAP_HIGH', 0.32), 0.15, 0.80))
    rel_cap_medium = float(np.clip(_env_float('RELIABILITY_CAP_MEDIUM', 0.26), 0.12, rel_cap_high))
    rel_cap_low = float(np.clip(_env_float('RELIABILITY_CAP_LOW', 0.20), 0.08, rel_cap_medium))
    rel_cap = np.where(rel_upper == 'HIGH', rel_cap_high, np.where(rel_upper == 'MEDIUM', rel_cap_medium, rel_cap_low))
    live['reliability_prob_cap'] = rel_cap
    live['pred_hr_prob'] = np.minimum(pd.to_numeric(live['pred_hr_prob'], errors='coerce').fillna(0.0), rel_cap)

    hard_conf_cap_enabled = str(os.getenv('HARD_CONFIDENCE_CAP_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
    hard_conf_cap = float(np.clip(_env_float('HARD_CONFIDENCE_CAP', 0.30), 0.12, 0.60))
    if hard_conf_cap_enabled:
        live['pred_hr_prob'] = np.minimum(pd.to_numeric(live['pred_hr_prob'], errors='coerce').fillna(0.0), hard_conf_cap)
    live['hard_confidence_cap'] = hard_conf_cap if hard_conf_cap_enabled else np.nan

    anchor_enabled = str(os.getenv('DAILY_PROBABILITY_ANCHOR_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
    if anchor_enabled:
        anchor_target_mean = float(np.clip(_env_float('DAILY_PROBABILITY_TARGET_MEAN', 0.095), 0.03, 0.20))
        anchor_strength = float(np.clip(_env_float('DAILY_PROBABILITY_ANCHOR_STRENGTH', 0.90), 0.0, 1.0))
        anchor_min_scale = float(np.clip(_env_float('DAILY_PROBABILITY_MIN_SCALE', 0.20), 0.05, 2.0))
        anchor_max_scale = float(np.clip(_env_float('DAILY_PROBABILITY_MAX_SCALE', 1.10), anchor_min_scale, 3.0))
        anchor_final_cap = float(np.clip(_env_float('DAILY_PROBABILITY_FINAL_CAP', hard_conf_cap), 0.12, 0.60))
        live, anchor_diag = apply_daily_probability_anchor(
            live,
            target_mean=anchor_target_mean,
            strength=anchor_strength,
            min_scale=anchor_min_scale,
            max_scale=anchor_max_scale,
            final_cap=anchor_final_cap,
        )
        if anchor_diag.get('applied'):
            print(
                "Daily probability anchor applied: "
                f"mean={anchor_diag.get('pre_mean', 0.0):.4f}->"
                f"{anchor_diag.get('post_mean', 0.0):.4f}, "
                f"scale={anchor_diag.get('applied_scale', 1.0):.3f}, "
                f"target={anchor_diag.get('target_mean', 0.0):.4f}"
            )
        else:
            print(f"Daily probability anchor skipped: {anchor_diag.get('reason', 'unknown')}")
    else:
        live['daily_anchor_scale'] = np.nan
        live['daily_anchor_target_mean'] = np.nan
        live['daily_anchor_pre_mean'] = np.nan
        live['daily_anchor_post_mean'] = np.nan
        live['daily_anchor_final_cap'] = np.nan
    
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
    snapshot_odds_raw = _load_latest_odds_snapshot_payload()
    if not market_odds_raw and snapshot_odds_raw:
        market_odds_raw = snapshot_odds_raw
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

        raw_book_lines = live['batter_name'].apply(_match_raw_book_lines)
        if raw_book_lines.map(lambda d: len(d) if isinstance(d, dict) else 0).sum() == 0 and snapshot_odds_raw:
            market_odds_raw = snapshot_odds_raw
            market_odds = _build_devigged_probs_from_raw_books(market_odds_raw)
            raw_book_lines = live['batter_name'].apply(_match_raw_book_lines)

        tiered_lines = raw_book_lines.apply(_split_best_lines_by_tier)

        live['sharp_book'] = tiered_lines.apply(lambda x: x.get('sharp_book'))
        live['sharp_market_odds_american'] = tiered_lines.apply(lambda x: x.get('sharp_market_odds_american'))
        live['sharp_market_implied_prob'] = live['sharp_market_odds_american'].apply(
            lambda x: american_to_implied_prob(x) if pd.notna(x) else np.nan
        )

        live['retail_book'] = tiered_lines.apply(lambda x: x.get('retail_book'))
        live['retail_market_odds_american'] = tiered_lines.apply(lambda x: x.get('retail_market_odds_american'))
        live['retail_market_implied_prob'] = live['retail_market_odds_american'].apply(
            lambda x: american_to_implied_prob(x) if pd.notna(x) else np.nan
        )

        live['best_book'] = live['sharp_book'].combine_first(live['retail_book'])
        live['best_market_odds_american'] = live['sharp_market_odds_american'].combine_first(live['retail_market_odds_american'])
        live['best_market_implied_prob'] = live['best_market_odds_american'].apply(
            lambda x: american_to_implied_prob(x) if pd.notna(x) else np.nan
        )
        live['matched_book_count'] = raw_book_lines.apply(lambda d: int(len(d)) if isinstance(d, dict) else 0)
        live['raw_book_lines_json'] = raw_book_lines.apply(
            lambda d: _json.dumps(d) if isinstance(d, dict) and d else '{}'
        )

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

        consensus_min_snapshots = max(1, _env_int('MARKET_CONSENSUS_MIN_SNAPSHOTS', 2))
        consensus_min_books = max(1, _env_int('MARKET_CONSENSUS_MIN_BOOKS', 2))
        consensus_max_spread = max(0.0, _env_float('MARKET_CONSENSUS_MAX_IMPLIED_SPREAD', 0.08))
        snapshots_today = _load_odds_snapshots(datetime.today().strftime('%Y-%m-%d'))

        def _consensus_for_row(row):
            return _snapshot_market_consensus(
                snapshots_today,
                row.get('batter_name', ''),
                min_snapshots=consensus_min_snapshots,
                min_books=consensus_min_books,
                max_implied_spread=consensus_max_spread,
            )

        consensus_rows = live.apply(_consensus_for_row, axis=1)
        live['snapshot_consensus_points'] = consensus_rows.apply(lambda x: int(x.get('snapshot_points', 0)))
        live['snapshot_consensus_books'] = consensus_rows.apply(lambda x: int(x.get('snapshot_books', 0)))
        live['snapshot_consensus_implied_spread'] = consensus_rows.apply(
            lambda x: _safe_float(x.get('snapshot_implied_spread'), np.nan)
        )
        live['market_consensus_ok'] = consensus_rows.apply(lambda x: bool(x.get('snapshot_consensus_ok', False)))

        live['sharp_retail_odds_gap'] = (
            pd.to_numeric(live['retail_market_odds_american'], errors='coerce')
            - pd.to_numeric(live['sharp_market_odds_american'], errors='coerce')
        )
        live['sharp_retail_implied_gap'] = (
            pd.to_numeric(live['retail_market_implied_prob'], errors='coerce')
            - pd.to_numeric(live['sharp_market_implied_prob'], errors='coerce')
        )

        # Use only core sportsbook tiers (sharp/retail) as probability baseline.
        # Do not fall back to best payout line for baseline math because outlier
        # books/promos can produce misleading edge percentages.
        live['market_prob'] = pd.to_numeric(live['market_prob'], errors='coerce')
        live['market_prob'] = pd.to_numeric(live['sharp_market_implied_prob'], errors='coerce').combine_first(
            pd.to_numeric(live['retail_market_implied_prob'], errors='coerce')
        ).combine_first(live['market_prob'])

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
        ev_max_model_prob = float(np.clip(_env_float('EV_MAX_MODEL_PROB', 0.20), 0.01, 0.95))

        def calculate_row_ev(row):
            """Calculate a conservative EV estimate using the market's implied probability."""
            raw_model_p = float(row['pred_hr_prob'])
            model_p_capped = float(min(raw_model_p, ev_max_model_prob))

            market_p = _safe_float(row.get('market_prob'), np.nan)
            if not np.isfinite(market_p) or market_p <= 0 or market_p >= 1:
                market_p = _safe_float(row.get('sharp_market_implied_prob'), np.nan)
            if not np.isfinite(market_p) or market_p <= 0 or market_p >= 1:
                market_p = _safe_float(row.get('retail_market_implied_prob'), np.nan)

            if not np.isfinite(market_p) or market_p <= 0 or market_p >= 1:
                return 0.0, 0.0, model_p_capped, np.nan

            # EV per $1 stake using capped model probability and the best-line market baseline.
            decimal_odds = 1.0 / market_p
            ev_value = model_p_capped * decimal_odds - 1.0
            ev_percent = ev_value * 100.0

            return ev_value, ev_percent, model_p_capped, float(market_p)
        
        live[['ev_value', 'ev_percent', 'model_p_capped', 'ev_market_prob']] = live.apply(
            lambda r: pd.Series(calculate_row_ev(r)), axis=1
        )

        live['fair_odds_american'] = live['pred_hr_prob'].apply(prob_to_fair_american)
        live['prob_edge_abs'] = (live['pred_hr_prob'] - live['market_prob']).fillna(0.0)
        safe_market_den = pd.to_numeric(live['market_prob'], errors='coerce').replace(0, np.nan).fillna(np.nan)
        safe_market_den = safe_market_den.clip(lower=0.05)
        live['market_edge_pct'] = ((live['pred_hr_prob'] - live['market_prob']) / safe_market_den * 100.0).fillna(0.0)

        elite_edge_abs = float(os.getenv('EV_EDGE_TRIGGER_ABS', '0.03'))
        elite_ev_pct = float(os.getenv('EV_TRIGGER_PCT', '10.0'))
        elite_min_books = max(1, _env_int('EV_MIN_MATCHED_BOOKS', 2))
        elite_max_dispersion = max(0.0, _env_float('EV_MAX_BOOK_PROB_DISPERSION', 0.10))
        arb_min_books = max(2, int(float(os.getenv('ARB_MIN_BOOKS', '3'))))
        arb_min_value_pct = float(os.getenv('ARB_MIN_VALUE_PCT', '3.0'))
        elite_market_consensus_ok = (
            live['market_consensus_ok'].fillna(False)
            & (pd.to_numeric(live['matched_book_count'], errors='coerce').fillna(0) >= elite_min_books)
            & (pd.to_numeric(live['book_prob_dispersion'], errors='coerce').fillna(1.0) <= elite_max_dispersion)
        )
        live['elite_market_consensus_ok'] = elite_market_consensus_ok
        live['elite_ev_signal'] = (
            (live['prob_edge_abs'] >= elite_edge_abs) &
            (live['ev_percent'] >= elite_ev_pct) &
            (live['market_edge_pct'] >= 0.0) &
            elite_market_consensus_ok
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
        live['is_positive_ev'] = (live['ev_percent'] > 0) & (live['market_prob'].notna())
        
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
        live['raw_book_lines_json'] = '{}'
        live['snapshot_consensus_points'] = 0
        live['snapshot_consensus_books'] = 0
        live['snapshot_consensus_implied_spread'] = np.nan
        live['market_consensus_ok'] = False
        live['elite_market_consensus_ok'] = False
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
        'batter_pitch_mix_matchup_score': 0.0,
        'recent_batter_woba_proxy': 0.0,
        'recent_pitcher_damage_proxy': 0.0,
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
    def _coerce_numeric_column(value, column_or_default, index=None, default=None):
        try:
            if index is None:
                if isinstance(value, pd.DataFrame):
                    index = value.index
                elif isinstance(value, pd.Series):
                    index = value.index
                elif hasattr(value, '__len__') and not np.isscalar(value):
                    index = pd.RangeIndex(0, len(value))
                else:
                    index = pd.RangeIndex(0, 1)
            if default is None and isinstance(column_or_default, (int, float, np.number)):
                default = float(column_or_default)
                column_or_default = None
            elif default is None:
                default = 0.0

            if isinstance(value, pd.DataFrame):
                if isinstance(column_or_default, str) and column_or_default in value.columns:
                    value = value[column_or_default]
                elif value.shape[1] == 1:
                    value = value.iloc[:, 0]
                else:
                    value = value.iloc[:, 0]
            elif isinstance(value, pd.Series):
                pass
            elif np.isscalar(value):
                return pd.Series([default] * len(index), index=index, dtype=float)
            else:
                value = pd.Series(value, index=index, dtype=object)

            coerced = pd.to_numeric(value, errors='coerce').fillna(default)
            if hasattr(coerced, 'index') and len(coerced.index) != len(index):
                coerced = coerced.reset_index(drop=True)
            if isinstance(coerced, pd.Series):
                if index is not None and len(coerced) != len(index):
                    coerced = pd.Series(coerced.tolist()[:len(index)], index=index, dtype=float)
                return coerced.astype(float)
            return pd.Series([default] * len(index), index=index, dtype=float)
        except Exception:
            return pd.Series([default] * len(index), index=index, dtype=float)

    for _col, _default in physics_output_defaults.items():
        if _col not in live.columns:
            live[_col] = _default
        else:
            live[_col] = _coerce_numeric_column(live.get(_col), _default, live.index)

    market_defaults = {
        'sharp_book': np.nan,
        'sharp_market_odds_american': np.nan,
        'sharp_market_implied_prob': np.nan,
        'retail_book': np.nan,
        'retail_market_odds_american': np.nan,
        'retail_market_implied_prob': np.nan,
        'sharp_retail_odds_gap': np.nan,
        'sharp_retail_implied_gap': np.nan,
        'best_book': np.nan,
        'best_market_odds_american': np.nan,
        'best_market_implied_prob': np.nan,
        'market_prob': np.nan,
        'market_edge_pct': np.nan,
    }
    for _col, _default in market_defaults.items():
        if _col not in live.columns:
            live[_col] = pd.Series([_default] * len(live), index=live.index)

    if 'pitcher_damage_boost' not in live.columns:
        live['pitcher_damage_boost'] = np.nan

    persist_daily_predictions(live[['game_pk', 'game_time', 'batter', 'batter_name', 'pitcher', 'pitcher_name',
                                    'has_platoon_advantage', 'park_factor', 'temp', 'wind_speed',
                                    'pred_hr_prob', 'model_p_capped', 'edge_pct', 'kelly_fraction', 'ev_value', 'ev_percent',
                                    'projected_pas', 'pa_distribution_std', 'pa_conversion_method',
                                    'kelly_multiplier',
                                    'ensemble_raw_prob', 'ensemble_prior_corrected_prob', 'pre_sidecar_prob',
                                    'base_model_prob', 'physics_delta', 'blend_weight_physics', 'probability_mode',
                                    'physics_uplift_cap',
                                    'reliability_prob_cap',
                                    'sharp_book', 'sharp_market_odds_american', 'sharp_market_implied_prob',
                                    'retail_book', 'retail_market_odds_american', 'retail_market_implied_prob',
                                    'sharp_retail_odds_gap', 'sharp_retail_implied_gap',
                                    'adaptive_feedback_multiplier', 'pitcher_fear_factor', 'pitcher_intent_suppression',
                                    'is_elite_power_batter', 'optimist_score', 'pessimist_score', 'debate_rounds',
                                    'optimist_wins', 'pessimist_wins', 'adversarial_margin', 'adversarial_multiplier',
                                    'stacked_boost_multiplier_raw', 'stacked_boost_multiplier_cap',
                                    'stacked_boost_multiplier_final',
                                    'best_book', 'best_market_odds_american', 'best_market_implied_prob',
                                    'market_prob', 'ev_market_prob', 'market_edge_pct',
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
                                    'recent_batter_woba_proxy', 'recent_pitcher_damage_proxy',
                                    'bullpen_exposure_multiplier',
                                    'lineup_protection_woba_proxy', 'context_multiplier',
                                    'recent_calibration_multiplier', 'recent_calibration_ratio_raw',
                                    'recent_calibration_pred_mean', 'recent_calibration_actual_mean',
                                    'pitcher_damage_boost',
                                    'daily_anchor_scale', 'daily_anchor_target_mean',
                                    'daily_anchor_pre_mean', 'daily_anchor_post_mean', 'daily_anchor_final_cap',
                                    'sidecar_online_prob', 'sidecar_blend_applied', 'sidecar_blend_weight',
                                    'sidecar_max_delta', 'sidecar_update_count',
                                    'sidecar_training_applied', 'sidecar_training_update_count',
                                    'confidence_lower_95pct', 'confidence_upper_95pct', 'model_reliability', 'batter_consistency_score',
                                    'model_name', 'prediction_timestamp']])

    # Sort and present elite values
    rankings = _prepare_discord_rankings(
        live[['batter_name', 'pitcher_name', 'pred_hr_prob', 'edge_pct', 'kelly_fraction', 'ev_percent', 'game_time', 'model_reliability']]
    )

    discord_top_prob_n = max(10, _env_int('DISCORD_TOP_PROB_COUNT', 30))
    discord_top_ev_n = max(3, _env_int('DISCORD_TOP_EV_COUNT', 12))
    discord_rows_per_message = max(5, _env_int('DISCORD_ROWS_PER_MESSAGE', 10))
    discord_min_prob = _env_float('DISCORD_MIN_PROB', 0.015)
    discord_min_edge_pct = _env_float('DISCORD_MIN_EDGE_PCT', 4.0)
    discord_min_ev_pct = _env_float('DISCORD_MIN_EV_PCT', 8.0)
    discord_min_kelly = _env_float('DISCORD_MIN_KELLY', 0.02)
    discord_radar_n = max(8, _env_int('DISCORD_RADAR_COUNT', 20))
    discord_window_1_hours = max(1, _env_int('DISCORD_WINDOW_1_HOURS', 2))
    discord_window_2_hours = max(discord_window_1_hours + 1, _env_int('DISCORD_WINDOW_2_HOURS', 6))
    print(f"Discord threshold resolved: {discord_min_prob * 100:.1f}%")

    # Top probabilities for reporting/Discord delivery.
    has_market_data = (
        'best_market_odds_american' in live.columns
        and pd.to_numeric(live['best_market_odds_american'], errors='coerce').notna().any()
    )
    # Most Likely Homers is a pure probability ranking — market-edge gates belong in +EV section only.
    prob_pool = rankings.sort_values(
        by=['hr_probability', 'portfolio_action_score', 'ev_pct', 'kelly_fraction'],
        ascending=[False, False, False, False]
    ).reset_index(drop=True)
    prob_pool = prob_pool[
        pd.to_numeric(prob_pool.get('hr_probability', 0.0), errors='coerce').fillna(0.0) >= discord_min_prob
    ].copy()
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
    radar = radar[
        (radar['hr_probability'] >= max(0.010, discord_min_prob * 0.7))
        & (pd.to_numeric(radar.get('edge_pct', 0.0), errors='coerce').fillna(0.0) >= max(2.0, discord_min_edge_pct * 0.5))
        & (pd.to_numeric(radar.get('ev_pct', 0.0), errors='coerce').fillna(0.0) >= max(3.0, discord_min_ev_pct * 0.5))
    ]

    top_keys = _build_discord_top_keys(top_prob)
    radar = radar[
        ~radar.apply(lambda r: (str(r.get('batter_name', '')), str(r.get('pitcher_name', ''))) in top_keys, axis=1)
    ]
    radar = pd.DataFrame(radar).copy()
    radar = _finalize_discord_radar_frame(radar)
    radar['portfolio_action_score'] = _build_portfolio_action_score(radar)
    radar['hr_probability'] = _coerce_numeric_column(radar, 'hr_probability', default=0.0)
    radar['physics_delta'] = _coerce_numeric_column(radar, 'physics_delta', default=0.0)
    radar['ev_pct'] = _coerce_numeric_column(radar, 'ev_pct', default=0.0)
    radar['physics_delta_abs'] = radar['physics_delta'].abs()
    radar = radar.sort_values(
        by=['portfolio_action_score', 'physics_delta_abs', 'hr_probability', 'ev_pct'],
        ascending=[False, False, False, False]
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
            'true_combo_prob', 'parlay_edge_pct', 'sportsbook_book', 'parlay_edge_signal',
            'learned_multiplier', 'training_days_used'
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
            top_ev = top_ev.sort_values(
                by=['portfolio_action_score', 'ev_pct', 'kelly_fraction', 'hr_probability'],
                ascending=[False, False, False, False]
            ).head(discord_top_ev_n).reset_index(drop=True)
            print(f"\n✅ +EV PREMIUM PICKS (Expected Value > 0%):")
            print(top_ev.to_string(index=False))
    top_ev = _annotate_time_windows(top_ev)

    print(f"\nMost Likely Homers (≥{discord_min_prob*100:.0f}% confidence) - {len(top_prob)} candidates:")
    print(top_prob.to_string(index=False))
    print(f"\nRadar Coverage: {len(radar)} additional candidates")
    print_discord_payload_diagnostics(top_prob, radar, top_ev)

    discrepancy_top_n = max(3, _env_int('STRAIGHT_DISCREPANCY_TOP_N', 12))
    straight_discrepancies = build_single_straight_market_discrepancies(live, top_n=discrepancy_top_n)
    target_date = datetime.today().strftime('%Y-%m-%d')
    if straight_discrepancies.empty:
        print("Single-straight discrepancy scan: no rows passed discrepancy filters.")
    else:
        discrepancy_path = Path('data') / f"straight_market_discrepancies_{target_date}.csv"
        straight_discrepancies.to_csv(discrepancy_path, index=False)
        print("\nSingle Straight Market Discrepancies (Model vs Market):")
        print(straight_discrepancies.to_string(index=False))
        print(f"Saved discrepancy shortlist: {discrepancy_path}")

    # =====================================================================
    # DISCORD WEBHOOK INTEGRATION
    # =====================================================================
    if not _candidate_discord_webhooks():
        print("Discord webhook not configured — skipping notification. Set DISCORD_MLB_WEBHOOK to enable.")
        return live

    # One-time morning signal: "go bet now" before market follower books converge.
    try:
        _morning_sent = send_morning_bet_now_alert(live, date_str=target_date)
        if _morning_sent:
            print("Morning market-release bet alert sent.")
    except Exception as _morning_exc:
        print(f"Morning market-release alert skipped: {_morning_exc}")

    def _american_to_decimal(odds):
        try:
            o = float(odds)
            return 1 + o / 100.0 if o > 0 else 1 + 100.0 / abs(o)
        except Exception:
            return None

    def _build_betting_card(ev_picks_df, date_str):
        """Return an actionable Discord betting card with book, stake, and parlay legs."""
        if ev_picks_df is None or ev_picks_df.empty:
            return None

        card_lines = [
            f"🎯 **MLB HR BETTING CARD — {date_str}**",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "📋 **TODAY'S SINGLES** (place at the book listed)",
        ]

        singles = []
        for i, (_, row) in enumerate(ev_picks_df.head(8).iterrows(), 1):
            name = str(row.get('batter_name', '') or row.get('hr_probability', ''))
            pitcher = str(row.get('pitcher_name', ''))
            prob = float(row.get('hr_probability', row.get('pred_hr_prob', 0)) or 0) * 100
            book = str(row.get('best_book') or 'N/A').title()
            odds_raw = row.get('best_market_odds_american', None)
            ev_pct = float(row.get('ev_pct', row.get('ev_percent', 0)) or 0)
            stake = float(row.get('stake_usd', 0) or 0)
            mkt_pct = float(row.get('market_prob', 0) or 0) * 100
            gtime = str(row.get('game_time', ''))

            if pd.isna(odds_raw):
                continue
            odds_i = int(float(odds_raw))
            emoji = "1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ 6️⃣ 7️⃣ 8️⃣".split()[i - 1]
            card_lines.append(
                f"\n{emoji} **{name}** to HR vs {pitcher}\n"
                f"   🏦 **{book}** | **{odds_i:+d}** | 💵 Stake: **${stake:.0f}**\n"
                f"   📊 Model {prob:.1f}% vs Market {mkt_pct:.1f}% | EV: **{ev_pct:+.0f}%**\n"
                f"   🕐 {gtime}"
            )
            singles.append({'name': name, 'odds': odds_i, 'book': book, 'prob': prob / 100, 'stake': stake})

        if not singles:
            return None

        # Build parlay suggestions
        card_lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        card_lines.append("🎰 **LOTTO PARLAYS** (small stakes, big upside)")

        top3 = singles[:3]
        if len(top3) >= 2:
            # 2-leg parlays
            card_lines.append("\n**2-Leg Bombs (bet $5-15 each):**")
            for a, b in [(top3[0], top3[1]), (top3[0], top3[2]) if len(top3) > 2 else (top3[0], top3[1])]:
                dec_a = _american_to_decimal(a['odds'])
                dec_b = _american_to_decimal(b['odds'])
                if dec_a and dec_b:
                    parlay_dec = dec_a * dec_b
                    parlay_am = int((parlay_dec - 1) * 100) if parlay_dec >= 2 else int(-100 / (parlay_dec - 1))
                    card_lines.append(
                        f"• **{a['name']}** + **{b['name']}** "
                        f"≈ {parlay_am:+,} | Book: {a['book']}"
                    )

        if len(top3) >= 3:
            # 3-leg lotto
            card_lines.append("\n**3-Leg Lotto (bet $1-5):**")
            dec_all = [_american_to_decimal(s['odds']) for s in top3]
            if all(d for d in dec_all):
                p3_dec = dec_all[0] * dec_all[1] * dec_all[2]
                p3_am = int((p3_dec - 1) * 100)
                legs = " + ".join(s['name'].split()[0] for s in top3)
                card_lines.append(f"• **{legs}** ≈ **{p3_am:+,}** | 🎯 True edge via correlation")

        total_singles = sum(s['stake'] for s in singles)
        card_lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        card_lines.append(f"💰 **Suggested singles exposure: ${total_singles:.0f}** | Parlays: $10-30")
        card_lines.append("⚠️ *Line shop — check all books before placing*")
        return "\n".join(card_lines)

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
            conf_emoji = _confidence_grade_emoji(confidence)
            
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
                "Legend: 🔵=HIGH confidence  🟠=MEDIUM confidence  🟣=LOW confidence"
            )
            if not send_discord_webhook(content=message_content):
                return False

        return True

    summary_lines = _build_discord_snapshot_summary(
        target_date,
        rankings,
        top_prob,
        radar,
        top_ev,
        discord_min_prob,
        discord_window_1_hours,
        discord_window_2_hours,
    )
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
        # Send the actionable betting card right after the EV table.
        card_msg = _build_betting_card(top_ev, target_date)
        if card_msg:
            send_discord_webhook(content=card_msg)

    # --- Promo/Boost Detection: flag when any book is 40+ American odds better than Pinnacle ---
    promo_lines = [f"🎁 **PROMO/BOOST ALERTS ({target_date})**"]
    promo_count = 0
    if 'sharp_market_odds_american' in live.columns and 'best_market_odds_american' in live.columns:
        sharp = pd.to_numeric(live['sharp_market_odds_american'], errors='coerce')
        best = pd.to_numeric(live['best_market_odds_american'], errors='coerce')
        boost_mask = (best.notna() & sharp.notna() & (best > 0) & ((best - sharp) >= 40))
        if boost_mask.any():
            for _, row in live[boost_mask].sort_values('best_market_odds_american', ascending=False).head(6).iterrows():
                gap = float(row['best_market_odds_american']) - float(row['sharp_market_odds_american'])
                promo_lines.append(
                    f"🔥 {row.get('batter_name','')} vs {row.get('pitcher_name','')}: "
                    f"Pinnacle {int(row['sharp_market_odds_american']):+d} → {row.get('best_book','?').title()} "
                    f"{int(row['best_market_odds_american']):+d} (gap: +{gap:.0f}) — likely promo/boost"
                )
                promo_count += 1
    if promo_count:
        send_discord_webhook(content="\n".join(promo_lines))
        print(f"Promo/boost alerts sent: {promo_count} opportunities")

    # --- Same-Game Correlated Parlay analysis (consensus-gated) ---
    sgp_lines = [f"🔗 **SAME-GAME PARLAY EDGES ({target_date})**"]
    sgp_count = 0
    if not pair_df.empty:
        sgp_df = pair_df[
            pair_df['pair_type'].astype(str).eq('same_game')
            & pair_df['parlay_edge_signal'].astype(bool)
        ].copy()
        for _, row in sgp_df.head(20).iterrows():
            sgp_lines.append(
                f"🔗 {row.get('pair_leg_1','')} + {row.get('pair_leg_2','')} | "
                f"book={str(row.get('sportsbook_book','n/a')).title()} | "
                f"true={float(row.get('true_combo_prob', 0.0))*100:.1f}%, "
                f"book_imp={float(row.get('sportsbook_parlay_implied_prob', np.nan))*100:.1f}%, "
                f"edge={float(row.get('parlay_edge_pct', 0.0)):+.1f}%"
            )
            sgp_count += 1
    if sgp_count:
        send_discord_webhook(content="\n".join(sgp_lines[:20]))
        print(f"SGP correlation edges sent: {sgp_count}")

    sent_straight = True
    if not straight_discrepancies.empty:
        straight_lines = [
            f"🎯 Single Straight Discrepancies ({target_date})",
            f"Rows: {len(straight_discrepancies)}",
        ]
        for _, row in straight_discrepancies.head(8).iterrows():
            straight_lines.append(
                f"- {row.get('batter_name','')} vs {row.get('pitcher_name','')}: "
                f"model={float(row.get('pred_hr_prob', 0.0))*100:.1f}%, "
                f"market={float(row.get('market_prob', 0.0))*100:.1f}%, "
                f"edge_pts={float(row.get('prob_edge_abs', 0.0))*100:+.2f}, "
                f"edge_rel={float(row.get('market_edge_pct', 0.0)):+.1f}%, "
                f"EV={float(row.get('ev_percent', 0.0)):+.1f}%, "
                f"odds={int(float(row.get('best_market_odds_american', 0))):+d}"
            )
        sent_straight = send_discord_webhook(content="\n".join(straight_lines))

    # --- Conservative shortlist to Discord ---
    try:
        conservative_path = Path('data') / f'conservative_bet_ready_{target_date}.csv'
        if conservative_path.exists():
            cons = pd.read_csv(conservative_path)
            if not cons.empty:
                cons_lines = [f"🛡️ **CONSERVATIVE BET-READY ({target_date}) — {len(cons)} picks**"]
                for _, row in cons.head(8).iterrows():
                    cons_lines.append(
                        f"- {row.get('batter_name','')} vs {row.get('pitcher_name','')}: "
                        f"prob={float(row.get('pred_hr_prob',0))*100:.1f}%, "
                        f"odds={int(float(row.get('best_market_odds_american',0))) if pd.notna(row.get('best_market_odds_american')) else 'N/A'}, "
                        f"EV={float(row.get('ev_percent',0)):+.1f}%, "
                        f"Kelly={float(row.get('kelly_fraction',0)):.3f}, "
                        f"stake=${float(row.get('stake_usd',0)):.0f} [{row.get('game_time','')}]"
                    )
                send_discord_webhook(content="\n".join(cons_lines))
    except Exception:
        pass

    if not sent_prob or not sent_ev or not sent_radar or not sent_straight:
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
    if not _claim_rlm_monitor_pidfile(today_str):
        return
    current_pid = os.getpid()
    atexit.register(_release_rlm_monitor_pidfile, today_str, current_pid)

    pred_file = Path('data') / f'predictions_{today_str}.csv'
    if not pred_file.exists():
        auto_build_preds = str(os.getenv('RLM_AUTO_BUILD_PREDICTIONS', 'true')).strip().lower() not in {'0', 'false', 'no'}
        if auto_build_preds:
            print("No predictions file found for today. Auto-generating predictions for RLM watcher...")
            try:
                generate_daily_predictions()
            except Exception as e:
                print(f"Could not auto-generate predictions for RLM watcher: {e}")
        if not pred_file.exists():
            print("No predictions file found for today. RLM monitor cannot start.")
            return

    preds = pd.read_csv(pred_file)
    for col in ['pred_hr_prob', 'best_market_odds_american', 'ev_percent', 'kelly_fraction']:
        if col in preds.columns:
            preds[col] = pd.to_numeric(preds[col], errors='coerce')

    watch_batters = preds.nlargest(15, 'pred_hr_prob')['batter_name'].dropna().astype(str).tolist()
    model_prob_by_name = {
        _normalize_player_name(r.get('batter_name')): float(pd.to_numeric(r.get('pred_hr_prob', 0.0), errors='coerce') or 0.0)
        for _, r in preds.iterrows()
        if str(r.get('batter_name', '')).strip()
    }
    kelly_by_name = {
        _normalize_player_name(r.get('batter_name')): float(pd.to_numeric(r.get('kelly_fraction', 0.0), errors='coerce') or 0.0)
        for _, r in preds.iterrows()
        if str(r.get('batter_name', '')).strip()
    }
    game_time_by_name = {
        _normalize_player_name(r.get('batter_name')): r.get('game_time', '')
        for _, r in preds.iterrows()
        if str(r.get('batter_name', '')).strip()
    }

    min_alert_odds = _env_int('LIVE_EV_MIN_AMERICAN_ODDS', 300)
    max_alert_odds = max(min_alert_odds, _env_int('LIVE_EV_MAX_AMERICAN_ODDS', 2500))
    min_ev_edge_abs = _env_float('LIVE_EV_MIN_EDGE_ABS', 0.02)
    require_sharp_market = str(os.getenv('LIVE_EV_REQUIRE_SHARP_MARKET', 'true')).strip().lower() not in {'0', 'false', 'no'}
    min_sharp_books = max(1, _env_int('LIVE_EV_MIN_SHARP_BOOKS', 2))
    only_positive_ev = str(os.getenv('LIVE_EV_REQUIRE_POSITIVE_EDGE', 'true')).strip().lower() not in {'0', 'false', 'no'}
    shadow_enable = str(os.getenv('SHADOW_MODEL_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
    shadow_prob_by_name = _load_shadow_model_probability_map(today_str) if shadow_enable else {}
    shadow_alerted = set()
    stale_follower_alerted = set()
    stale_implied_gap = _env_float('FOLLOWER_STALE_MIN_IMPLIED_GAP', 0.015)
    sharp_move_trigger = _env_float('SHARP_MOVE_TRIGGER_IMPLIED_DELTA', 0.020)
    rlm_poll_seconds = max(60, _env_int('RLM_POLL_SECONDS', 900))
    auto_wager_enabled = str(os.getenv('AUTO_WAGER_ENABLED', 'false')).strip().lower() in {'1', 'true', 'yes'}
    auto_wager_min_kelly = max(0.0, _env_float('AUTO_WAGER_MIN_KELLY', 0.01))
    auto_wager_min_edge = max(0.0, _env_float('AUTO_WAGER_MIN_EDGE_ABS', min_ev_edge_abs))
    auto_wager_allow_books = {
        b.strip().lower() for b in str(os.getenv('AUTO_WAGER_ALLOWED_BOOKS', 'pinnacle,circasports')).split(',') if b.strip()
    }
    executed_wager_keys = _load_executed_wager_keys(today_str)

    print(f"RLM watcher started — {len(watch_batters)} batters tracked")
    print(f"Watching: {', '.join(watch_batters[:5])}...")
    print(
        "Live +EV filters: "
        f"min_odds={min_alert_odds:+d}, min_edge_abs={min_ev_edge_abs:.3f}, "
        f"require_model_gt_market={only_positive_ev}"
    )
    print(
        "Monitoring mode: "
        f"poll={rlm_poll_seconds}s, shadow_model={'on' if shadow_enable else 'off'}, "
        f"stale_gap={stale_implied_gap:.3f}, sharp_trigger={sharp_move_trigger:.3f}"
    )
    print(
        "Direct execution: "
        f"enabled={auto_wager_enabled}, min_kelly={auto_wager_min_kelly:.4f}, "
        f"min_edge_abs={auto_wager_min_edge:.4f}, allow_books={sorted(auto_wager_allow_books)}"
    )

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

    alerted_ev = set()

    while True:
        try:
            current_odds = fetch_hr_prop_odds_raw()
            if not current_odds:
                print(f"[{datetime.now().strftime('%H:%M')}] No odds available yet, retrying in 15 min...")
                time.sleep(rlm_poll_seconds)
                continue

            save_odds_snapshot(current_odds, today_str)

            normalized_current = {
                _normalize_player_name(name): (name, book_map)
                for name, book_map in current_odds.items()
                if isinstance(book_map, dict)
            }

            def _resolve_books_for_batter(batter_name):
                norm = _normalize_player_name(batter_name)
                hit = normalized_current.get(norm)
                if hit:
                    return hit[0], hit[1]
                for n, (orig_name, books) in normalized_current.items():
                    if not n:
                        continue
                    if norm in n or n in norm:
                        return orig_name, books
                return None, {}

            ev_candidates = []
            ev_candidate_names = set()
            latest_best_odds_by_batter = {}
            for batter_name in watch_batters:
                odds_key, books = _resolve_books_for_batter(batter_name)
                if not books:
                    continue

                sharp_book, sharp_american = _best_line_from_book_map_with_preference(books, SHARP_BOOKS)
                best_book, best_american = _best_line_from_book_map(books)
                if best_american is None:
                    continue

                norm = _normalize_player_name(batter_name)
                latest_best_odds_by_batter[norm] = int(best_american)
                if int(best_american) < int(min_alert_odds):
                    continue
                if int(best_american) > int(max_alert_odds):
                    continue

                model_prob = float(model_prob_by_name.get(norm, 0.0) or 0.0)

                sharp_market_prob, sharp_count = _book_map_consensus_implied_prob(books, preferred_books=SHARP_BOOKS)
                all_market_prob, all_count = _book_map_consensus_implied_prob(books, preferred_books=None)

                if require_sharp_market and sharp_count < min_sharp_books:
                    continue

                market_prob = sharp_market_prob if sharp_count >= min_sharp_books else all_market_prob
                if market_prob is None or not np.isfinite(market_prob):
                    continue

                edge_abs = model_prob - market_prob
                if only_positive_ev and edge_abs <= 0.0:
                    continue
                if edge_abs < min_ev_edge_abs:
                    continue

                ev_candidates.append((
                    batter_name,
                    int(best_american),
                    model_prob,
                    market_prob,
                    edge_abs,
                    odds_key,
                    best_book,
                    sharp_book,
                    sharp_american,
                    sharp_count,
                    all_count,
                ))
                ev_candidate_names.add(_normalize_player_name(odds_key or batter_name))

            _update_clv_tracker_snapshot(today_str, latest_best_odds_by_batter, game_time_by_name)

            if shadow_enable and shadow_prob_by_name:
                for batter_name, best_american, _, market_prob, _, _, _, _, _, _, _ in ev_candidates:
                    norm = _normalize_player_name(batter_name)
                    shadow_prob = _safe_float(shadow_prob_by_name.get(norm))
                    if shadow_prob is None:
                        continue
                    shadow_edge = shadow_prob - market_prob
                    if shadow_edge >= min_ev_edge_abs and norm not in shadow_alerted:
                        _record_shadow_virtual_bet(today_str, batter_name, best_american, shadow_prob, market_prob)
                        shadow_alerted.add(norm)

            # Market-maker vs follower lag scan: detect stale follower books after sharp repricing.
            for batter_name in watch_batters:
                odds_key, books = _resolve_books_for_batter(batter_name)
                if not books:
                    continue
                prev_books = prev_odds.get(odds_key, {}) if isinstance(prev_odds, dict) else {}
                if not prev_books:
                    continue

                sharp_curr = {k: v for k, v in books.items() if k in SHARP_BOOKS}
                sharp_prev = {k: v for k, v in prev_books.items() if k in SHARP_BOOKS}
                if not sharp_curr or not sharp_prev:
                    continue

                sharp_delta, sharp_delta_n, _ = _book_map_mean_implied_delta(
                    sharp_curr,
                    sharp_prev,
                    preferred_books=SHARP_BOOKS,
                )
                if sharp_delta is None or sharp_delta_n < max(1, min_sharp_books):
                    continue

                sharp_imp_delta = float(sharp_delta)
                if sharp_imp_delta < sharp_move_trigger:
                    continue

                sharp_curr_imp, _ = _book_map_consensus_implied_prob(sharp_curr, preferred_books=SHARP_BOOKS)
                if sharp_curr_imp is None:
                    continue

                followers = {k: v for k, v in books.items() if k in SQUARE_BOOKS}
                if not followers:
                    continue
                stale_hits = []
                for fbk, fline in followers.items():
                    try:
                        f_imp = float(american_to_implied_prob(fline))
                    except Exception:
                        continue
                    # Follower stale if still materially longer than sharp market.
                    if (sharp_curr_imp - f_imp) >= stale_implied_gap:
                        stale_hits.append((fbk, int(round(float(fline))), sharp_curr_imp - f_imp))

                if not stale_hits:
                    continue

                top_stale = sorted(stale_hits, key=lambda x: x[2], reverse=True)[0]
                stale_key = f"{_normalize_player_name(batter_name)}|{top_stale[0]}|{top_stale[1]}"
                if stale_key in stale_follower_alerted:
                    continue

                stale_msg = (
                    f"🕒 **FOLLOWER LAG — {batter_name}**\n"
                    f"Sharp consensus implied move: +{sharp_imp_delta*100:.2f} pts ({sharp_delta_n} books)\n"
                    f"Stale follower: {top_stale[0]} still {top_stale[1]:+d}\n"
                    f"Implied gap: +{top_stale[2]*100:.2f} pts"
                )
                send_discord_webhook(content=stale_msg)
                print(stale_msg)
                stale_follower_alerted.add(stale_key)

            watch_batters_for_rlm = []
            for batter_name in watch_batters:
                odds_key, books = _resolve_books_for_batter(batter_name)
                if books and odds_key:
                    watch_batters_for_rlm.append(odds_key)
            watch_batters_for_rlm = list(dict.fromkeys(watch_batters_for_rlm))

            for batter_name, best_american, model_prob, market_prob, edge_abs, odds_key, best_book, sharp_book, sharp_american, sharp_count, _all_count in ev_candidates:
                norm = _normalize_player_name(batter_name)
                if norm in alerted_ev:
                    continue

                _record_clv_entry(today_str, batter_name, best_american, model_prob, source='live_ev_alert')
                implied_pct = market_prob * 100.0
                model_pct = model_prob * 100.0
                edge_pct = edge_abs * 100.0
                sharp_line_text = f"{int(sharp_american):+d}" if sharp_american is not None else "N/A"
                ev_msg = (
                    f"💰 **+EV WINDOW — {batter_name}**\n"
                    f"Best line: {best_american:+d} ({best_book or odds_key or 'market'})\n"
                    f"Vegas sharp ref: {sharp_line_text} from {int(sharp_count)} sharp books\n"
                    f"Model HR prob: {model_pct:.1f}%\n"
                    f"Market implied: {implied_pct:.1f}%\n"
                    f"Edge: +{edge_pct:.2f} pts"
                )
                send_discord_webhook(content=ev_msg)
                print(ev_msg)

                if auto_wager_enabled:
                    book_key = str(best_book or '').strip().lower()
                    kelly_fraction = float(kelly_by_name.get(norm, 0.0) or 0.0)
                    can_execute = True
                    skip_reason = None
                    if not book_key:
                        can_execute = False
                        skip_reason = 'missing_book'
                    elif auto_wager_allow_books and book_key not in auto_wager_allow_books:
                        can_execute = False
                        skip_reason = f'book_not_allowed:{book_key}'
                    elif kelly_fraction < auto_wager_min_kelly:
                        can_execute = False
                        skip_reason = f'kelly_below_min:{kelly_fraction:.4f}'
                    elif edge_abs < auto_wager_min_edge:
                        can_execute = False
                        skip_reason = f'edge_below_min:{edge_abs:.4f}'

                    execution_seed = f"{today_str}|{norm}|{book_key}|{int(best_american)}|{kelly_fraction:.6f}"
                    execution_key = hashlib.sha256(execution_seed.encode('utf-8')).hexdigest()[:32]

                    if execution_key in executed_wager_keys:
                        can_execute = False
                        skip_reason = 'already_executed'

                    result = {
                        'status': 'skipped',
                        'reason': skip_reason or 'gated',
                        'stake_usd': 0.0,
                    }
                    if can_execute:
                        result = _execute_auto_wager_direct_api(
                            date_str=today_str,
                            batter_name=batter_name,
                            sportsbook=book_key,
                            odds_american=best_american,
                            model_prob=model_prob,
                            market_prob=market_prob,
                            edge_abs=edge_abs,
                            kelly_fraction=kelly_fraction,
                            game_time=game_time_by_name.get(norm),
                            execution_key=execution_key,
                        )

                    log_row = {
                        'timestamp': datetime.now().isoformat(),
                        'execution_key': execution_key,
                        'batter_name': batter_name,
                        'sportsbook': book_key,
                        'odds_american': int(best_american),
                        'model_prob': float(model_prob),
                        'market_prob': float(market_prob),
                        'edge_abs': float(edge_abs),
                        'kelly_fraction': float(kelly_fraction),
                        'status': str(result.get('status', 'unknown')),
                        'reason': str(result.get('reason', '')),
                        'stake_usd': float(_safe_float(result.get('stake_usd'), 0.0) or 0.0),
                        'endpoint': str(result.get('endpoint', '')),
                        'http_status': _safe_int(result.get('http_status')),
                    }
                    _append_auto_wager_log(today_str, log_row)
                    if str(result.get('status')) in {'placed', 'dry_run'}:
                        executed_wager_keys.add(execution_key)

                    exec_msg = (
                        f"🤖 **AUTO EXECUTION — {batter_name}**\n"
                        f"Book: {book_key or 'n/a'} | Line: {int(best_american):+d}\n"
                        f"Status: {result.get('status')} ({result.get('reason')})\n"
                        f"Stake: ${float(_safe_float(result.get('stake_usd'), 0.0) or 0.0):.2f}"
                    )
                    send_discord_webhook(content=exec_msg)
                    print(exec_msg)

                alerted_ev.add(norm)

            if prev_odds:
                alerts = detect_rlm(current_odds, prev_odds, watch_batters_for_rlm)
                for batter, sharp_move, square_move, signal in alerts:
                    batter_norm = _normalize_player_name(batter)
                    if batter_norm not in ev_candidate_names:
                        continue

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
            print(
                f"\n[{datetime.now().strftime('%H:%M')}] {len(current_odds)} players tracked across sportsbooks "
                f"| ev_candidates={len(ev_candidates)}"
            )
            for batter in watch_batters[:5]:
                _, books = _resolve_books_for_batter(batter)
                if books:
                    sharp = {k: v for k, v in books.items() if k in SHARP_BOOKS}
                    square = {k: v for k, v in books.items() if k in SQUARE_BOOKS}
                    sharp_str = f"sharp: {list(sharp.values())[0]:+d}" if sharp else ''
                    square_str = f"public: {min(square.values(), key=abs):+d}" if square else ''
                    print(f"  {batter[:25]:<25} {sharp_str:<15} {square_str}  ({len(books)} books)")

            prev_odds = current_odds
            audit_clv_performance(today_str, lookback_days=max(7, _env_int('CLV_AUDIT_LOOKBACK_DAYS', 30)))
            time.sleep(rlm_poll_seconds)
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
                high_conf_threshold = _env_float('LIVE_HR_HIGH_CONF_THRESHOLD', 0.20)
                most_likely_top_n = max(1, _env_int('LIVE_HR_MOST_LIKELY_TOP_N', 5))
                was_predicted = model_prob >= high_conf_threshold

                preds_ranked = preds.sort_values('pred_hr_prob', ascending=False).reset_index(drop=True)
                ranked_match = preds_ranked[
                    preds_ranked['batter_name'].str.lower().str.strip() == batter_name.lower().strip()
                ]
                if not ranked_match.empty:
                    model_rank = int(ranked_match.index[0] + 1)

                was_most_likely_homer = (
                    model_rank is not None and
                    model_rank <= most_likely_top_n and
                    model_prob >= high_conf_threshold
                )
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
            message_lines.append(f"📊 Pregame model score: {prob_str}")
            if model_rank is not None:
                message_lines.append(f"📈 Pregame rank: #{model_rank}")
            if was_most_likely_homer:
                message_lines.append("🏷️ Tag: High-confidence Most Likely pick")
            elif was_predicted:
                message_lines.append("🏷️ Tag: Above high-confidence threshold")
            else:
                message_lines.append("🏷️ Tag: Below high-confidence threshold (logged for retraining)")

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


def _extract_date_from_prefixed_stem(stem, prefix):
    """Extract YYYY-MM-DD from stems like prefixYYYY-MM-DD[_suffix]."""
    try:
        raw = str(stem or '')
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
        token = raw.split('_', 1)[0].strip()
        if len(token) != 10:
            return None
        return datetime.strptime(token, '%Y-%m-%d')
    except Exception:
        return None


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


def _best_line_from_book_map_with_preference(book_map, preferred_books=None, allow_fallback_to_any=True):
    """Return the best line, preferring a specific sportsbook tier when available."""
    if not isinstance(book_map, dict) or not book_map:
        return None, None

    preferred = {str(b).strip().lower() for b in (preferred_books or set()) if str(b).strip()}
    preferred_map = {
        bk: odds for bk, odds in book_map.items()
        if str(bk).strip().lower() in preferred
    }
    if preferred_map:
        best_book, best_american = _best_line_from_book_map(preferred_map)
        if best_book is not None and best_american is not None:
            return best_book, best_american
    if allow_fallback_to_any:
        return _best_line_from_book_map(book_map)
    return None, None


def _split_best_lines_by_tier(book_map):
    """Return sharp, retail, and all-book best lines for a matchup."""
    sharp_book, sharp_odds = _best_line_from_book_map_with_preference(book_map, SHARP_BOOKS, allow_fallback_to_any=False)
    retail_book, retail_odds = _best_line_from_book_map_with_preference(book_map, SQUARE_BOOKS, allow_fallback_to_any=False)
    best_book, best_odds = _best_line_from_book_map(book_map)
    return {
        'sharp_book': sharp_book,
        'sharp_market_odds_american': sharp_odds,
        'retail_book': retail_book,
        'retail_market_odds_american': retail_odds,
        'best_book': best_book,
        'best_market_odds_american': best_odds,
    }


def _coerce_book_map(value):
    """Normalize a raw/json book map into {book_key: american_odds_float}."""
    data = value
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return {}
        try:
            data = _json.loads(txt)
        except Exception:
            return {}

    if not isinstance(data, dict):
        return {}

    out = {}
    for bk, odds in data.items():
        o = _safe_float(odds)
        if o is None or not np.isfinite(o):
            continue
        out[str(bk).strip().lower()] = float(o)
    return out


def _best_common_book_parlay_offer(book_map_a, book_map_b):
    """Return best same-book 2-leg parlay offer from two leg book maps."""
    a = _coerce_book_map(book_map_a)
    b = _coerce_book_map(book_map_b)
    common = sorted(set(a.keys()) & set(b.keys()))
    if not common:
        return {
            'sportsbook_book': None,
            'sportsbook_leg1_odds_american': np.nan,
            'sportsbook_leg2_odds_american': np.nan,
            'sportsbook_parlay_decimal': np.nan,
            'sportsbook_parlay_implied_prob': np.nan,
            'sportsbook_common_books': 0,
        }

    best_book = None
    best_leg1 = np.nan
    best_leg2 = np.nan
    best_parlay_dec = np.nan
    for bk in common:
        o1 = _safe_float(a.get(bk))
        o2 = _safe_float(b.get(bk))
        d1 = _american_to_decimal_safe(o1)
        d2 = _american_to_decimal_safe(o2)
        if pd.isna(d1) or pd.isna(d2):
            continue
        dec = float(d1 * d2)
        if pd.isna(best_parlay_dec) or dec > best_parlay_dec:
            best_parlay_dec = dec
            best_book = bk
            best_leg1 = float(o1)
            best_leg2 = float(o2)

    implied = np.nan
    if pd.notna(best_parlay_dec) and best_parlay_dec > 0:
        implied = float(1.0 / best_parlay_dec)

    return {
        'sportsbook_book': best_book,
        'sportsbook_leg1_odds_american': best_leg1,
        'sportsbook_leg2_odds_american': best_leg2,
        'sportsbook_parlay_decimal': best_parlay_dec,
        'sportsbook_parlay_implied_prob': implied,
        'sportsbook_common_books': int(len(common)),
    }


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


def _load_shadow_model_probability_map(date_str=None):
    """Load optional shadow model probabilities keyed by normalized batter name."""
    explicit_path = str(os.getenv('SHADOW_MODEL_PROBS_PATH', '') or '').strip()
    if explicit_path:
        candidate = Path(explicit_path)
    else:
        date_str = date_str or datetime.today().strftime('%Y-%m-%d')
        candidate = Path('data') / f'shadow_predictions_{date_str}.csv'

    if not candidate.exists():
        return {}

    try:
        shadow_df = pd.read_csv(candidate)
    except Exception:
        return {}

    if shadow_df.empty:
        return {}

    if 'batter_name' not in shadow_df.columns:
        return {}

    prob_col = None
    for c in ['shadow_pred_hr_prob', 'pred_hr_prob', 'probability']:
        if c in shadow_df.columns:
            prob_col = c
            break
    if prob_col is None:
        return {}

    out = {}
    for _, row in shadow_df.iterrows():
        nm = _normalize_player_name(row.get('batter_name'))
        if not nm:
            continue
        p = _safe_float(row.get(prob_col))
        if p is None or not np.isfinite(p):
            continue
        out[nm] = float(np.clip(p, 0.0, 1.0))
    return out


def _record_shadow_virtual_bet(date_str, batter_name, best_odds_american, shadow_prob, market_prob):
    """Append a paper-trade row for shadow model evaluation without risking capital."""
    out_path = Path('data') / f'shadow_virtual_bets_{date_str}.csv'
    Path('data').mkdir(parents=True, exist_ok=True)
    row = {
        'timestamp': datetime.now().isoformat(),
        'batter_name': str(batter_name or ''),
        'best_odds_american': _safe_float(best_odds_american, np.nan),
        'shadow_pred_hr_prob': _safe_float(shadow_prob, np.nan),
        'market_implied_prob': _safe_float(market_prob, np.nan),
        'shadow_edge_abs': (_safe_float(shadow_prob, 0.0) or 0.0) - (_safe_float(market_prob, 0.0) or 0.0),
    }
    df = pd.DataFrame([row])
    if out_path.exists():
        df.to_csv(out_path, mode='a', header=False, index=False)
    else:
        df.to_csv(out_path, index=False)


def _auto_wager_log_path(date_str=None):
    date_str = date_str or datetime.today().strftime('%Y-%m-%d')
    return Path('data') / f'auto_wager_executions_{date_str}.csv'


def _load_executed_wager_keys(date_str=None):
    path = _auto_wager_log_path(date_str)
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path)
        if 'execution_key' not in df.columns:
            return set()
        return set(df['execution_key'].dropna().astype(str).tolist())
    except Exception:
        return set()


def _append_auto_wager_log(date_str, row):
    path = _auto_wager_log_path(date_str)
    Path('data').mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row])
    if path.exists():
        df.to_csv(path, mode='a', header=False, index=False)
    else:
        df.to_csv(path, index=False)


def _load_auto_wager_book_config():
    """Return optional per-book API execution config from env JSON."""
    raw = str(os.getenv('AUTO_WAGER_BOOK_CONFIG_JSON', '') or '').strip()
    if not raw:
        return {}
    try:
        payload = _json.loads(raw)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}

    out = {}
    for k, v in payload.items():
        if not isinstance(v, dict):
            continue
        out[str(k).strip().lower()] = v
    return out


def _resolve_auto_wager_endpoint(book_key, book_config):
    book_cfg = (book_config or {}).get(str(book_key or '').strip().lower(), {})
    if isinstance(book_cfg, dict) and str(book_cfg.get('url', '')).strip():
        return str(book_cfg.get('url')).strip(), book_cfg

    default_url = str(os.getenv('AUTO_WAGER_DEFAULT_URL', '') or '').strip()
    if default_url:
        return default_url, book_cfg if isinstance(book_cfg, dict) else {}
    return None, {}


def _estimate_actionability_score(row):
    """Create a single quality score for ranking actionable picks."""
    try:
        pred_prob = max(0.0, _safe_float(row.get('pred_hr_prob', 0.0), 0.0) or 0.0)
        ev_pct = max(0.0, _safe_float(row.get('ev_percent', 0.0), 0.0) or 0.0)
        edge_pct = max(0.0, _safe_float(row.get('edge_pct', 0.0), 0.0) or 0.0)
        kelly = max(0.0, _safe_float(row.get('kelly_fraction', 0.0), 0.0) or 0.0)
        reliability = str(row.get('model_reliability', 'MEDIUM') or 'MEDIUM').upper()
        matchup_signal = max(0.0, _safe_float(row.get('batter_pitch_mix_matchup_score', 0.0), 0.0) or 0.0)
        recent_batter = max(0.0, _safe_float(row.get('recent_batter_woba_proxy', 0.0), 0.0) or 0.0)
        recent_pitcher = max(0.0, _safe_float(row.get('recent_pitcher_damage_proxy', 0.0), 0.0) or 0.0)

        reliability_bonus = {'HIGH': 1.0, 'MEDIUM': 0.6, 'LOW': 0.0}.get(reliability, 0.6)
        matchup_bonus = 0.1 * min(1.0, (matchup_signal + recent_batter + recent_pitcher) / 3.0)
        return float(
            (pred_prob * 100.0) * 0.20
            + ev_pct * 0.35
            + edge_pct * 0.15
            + (kelly * 100.0) * 0.20
            + reliability_bonus * 10.0
            + matchup_bonus * 10.0
        )
    except Exception:
        return 0.0


def _estimate_bet_stake_usd(kelly_fraction, bankroll_usd=None, min_stake_usd=None, max_stake_usd=None, kelly_multiplier=None):
    """Estimate a practical stake from a bankroll and Kelly fraction.

    This keeps the staking logic reusable for both auto-wager execution and
    human-readable shortlist reporting. The default env names are split so the
    same helper can be used without coupling to the auto-wager subsystem.
    """
    bankroll = max(0.0, _env_float('BET_STAKE_BANKROLL_USD', bankroll_usd if bankroll_usd is not None else 100.0))
    stake_floor = max(0.0, _env_float('BET_STAKE_MIN_USD', min_stake_usd if min_stake_usd is not None else 5.0))
    stake_cap = max(stake_floor, _env_float('BET_STAKE_MAX_USD', max_stake_usd if max_stake_usd is not None else 50.0))
    kelly_mult = max(0.0, _env_float('BET_STAKE_KELLY_MULTIPLIER', kelly_multiplier if kelly_multiplier is not None else 0.25))
    kf = max(0.0, _safe_float(kelly_fraction, 0.0) or 0.0)
    stake = bankroll * kf * kelly_mult
    if stake <= 0:
        return 0.0
    return float(np.clip(stake, stake_floor, stake_cap))


def _estimate_auto_wager_stake(kelly_fraction):
    return _estimate_bet_stake_usd(
        kelly_fraction,
        bankroll_usd=_env_float('AUTO_WAGER_BANKROLL_USD', 100.0),
        min_stake_usd=_env_float('AUTO_WAGER_MIN_STAKE_USD', 5.0),
        max_stake_usd=_env_float('AUTO_WAGER_MAX_STAKE_USD', 50.0),
        kelly_multiplier=_env_float('AUTO_WAGER_KELLY_MULTIPLIER', 0.25),
    )


def _execute_auto_wager_direct_api(
    date_str,
    batter_name,
    sportsbook,
    odds_american,
    model_prob,
    market_prob,
    edge_abs,
    kelly_fraction,
    game_time,
    execution_key,
):
    """Execute a wager through direct sportsbook/exchange API infrastructure."""
    dry_run = str(os.getenv('AUTO_WAGER_DRY_RUN', 'true')).strip().lower() not in {'0', 'false', 'no'}
    stake_usd = _estimate_auto_wager_stake(kelly_fraction)
    if stake_usd <= 0:
        return {
            'status': 'skipped',
            'reason': 'stake_zero',
            'stake_usd': 0.0,
        }

    if requests is None:
        return {
            'status': 'failed',
            'reason': 'requests_unavailable',
            'stake_usd': stake_usd,
        }

    book_config = _load_auto_wager_book_config()
    endpoint, cfg = _resolve_auto_wager_endpoint(sportsbook, book_config)
    if not endpoint:
        return {
            'status': 'failed',
            'reason': 'missing_execution_endpoint',
            'stake_usd': stake_usd,
        }

    payload = {
        'event_date': date_str,
        'market_type': 'player_home_run',
        'selection_name': str(batter_name),
        'sportsbook': str(sportsbook or ''),
        'odds_american': int(round(float(odds_american))),
        'stake_usd': round(float(stake_usd), 2),
        'model_probability': float(model_prob),
        'market_probability': float(market_prob),
        'edge_abs': float(edge_abs),
        'kelly_fraction': float(_safe_float(kelly_fraction, 0.0) or 0.0),
        'game_time': str(game_time or ''),
        'idempotency_key': str(execution_key),
    }

    headers = {'Content-Type': 'application/json'}
    auth_mode = str((cfg or {}).get('auth', os.getenv('AUTO_WAGER_AUTH_MODE', 'bearer'))).strip().lower()
    token_env = str((cfg or {}).get('token_env', os.getenv('AUTO_WAGER_TOKEN_ENV', 'AUTO_WAGER_API_TOKEN'))).strip()
    token = str(os.getenv(token_env, '') or '').strip() if token_env else ''
    if auth_mode == 'x-api-key' and token:
        headers['x-api-key'] = token
    elif token:
        headers['Authorization'] = f"Bearer {token}"
    headers['Idempotency-Key'] = str(execution_key)

    if dry_run:
        return {
            'status': 'dry_run',
            'reason': 'dry_run_enabled',
            'stake_usd': stake_usd,
            'endpoint': endpoint,
            'request_payload': payload,
        }

    timeout_sec = max(3, _env_int('AUTO_WAGER_HTTP_TIMEOUT_SECONDS', 10))
    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout_sec)
        resp_text = str(getattr(resp, 'text', '') or '')
        body = None
        try:
            body = resp.json()
        except Exception:
            body = {'raw_text': resp_text[:500]}
        if int(resp.status_code) >= 200 and int(resp.status_code) < 300:
            return {
                'status': 'placed',
                'reason': 'ok',
                'stake_usd': stake_usd,
                'endpoint': endpoint,
                'http_status': int(resp.status_code),
                'response': body,
            }
        return {
            'status': 'failed',
            'reason': 'http_error',
            'stake_usd': stake_usd,
            'endpoint': endpoint,
            'http_status': int(resp.status_code),
            'response': body,
        }
    except Exception as exc:
        return {
            'status': 'failed',
            'reason': f'exception: {exc}',
            'stake_usd': stake_usd,
            'endpoint': endpoint,
        }


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

        odds_poll_seconds = max(2, _safe_int(os.getenv('LIVE_ODDS_POLL_SECONDS', '2'), 2) or 2)
        monitor_sleep_seconds = max(1, _safe_int(os.getenv('LIVE_MONITOR_POLL_SECONDS', '1'), 1) or 1)
        heartbeat_enabled = str(os.getenv('LIVE_HEARTBEAT_ENABLED', 'false')).strip().lower() not in {'0', 'false', 'no'}
        heartbeat_minutes = max(5, _safe_int(os.getenv('LIVE_HEARTBEAT_MINUTES', '60'), 60) or 60)
        heartbeat_every_seconds = heartbeat_minutes * 60
        heartbeat_force_on_no_games = str(os.getenv('LIVE_HEARTBEAT_FORCE_ON_NO_GAMES', 'false')).strip().lower() not in {'0', 'false', 'no'}
        backfill_every_seconds = max(10, _safe_int(os.getenv('LIVE_HR_BACKFILL_SECONDS', '15'), 15) or 15)
        nightly_summary_enabled = str(os.getenv('NIGHTLY_ACCURACY_SUMMARY_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
        nightly_summary_hour = int(np.clip(_safe_int(os.getenv('NIGHTLY_ACCURACY_SUMMARY_HOUR', '1'), 1) or 1, 0, 23))
        first_game_start_local = _get_first_scheduled_game_time_local(datetime.today().strftime('%m/%d/%Y'))
        if first_game_start_local is not None:
            print(f"First scheduled game today (local): {first_game_start_local.strftime('%Y-%m-%d %I:%M %p')}")
            if datetime.now() < first_game_start_local:
                print("Pregame tracking active: live monitor is running before first pitch.")
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
            first_game_start_local,
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
                            message_lines.append(f"📊 Pregame model score: {prob_str}")
                            if _model_rank is not None:
                                message_lines.append(f"📈 Pregame rank: #{_model_rank}")
                            if _was_most_likely_homer:
                                message_lines.append("🏷️ Tag: High-confidence Most Likely pick")
                            elif _was_predicted:
                                message_lines.append("🏷️ Tag: Above high-confidence threshold")
                            else:
                                message_lines.append("🏷️ Tag: Below high-confidence threshold (logged for retraining)")
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

                # Once per day after the configured hour, send yesterday's full HR accuracy summary.
                if nightly_summary_enabled:
                    now_local = datetime.now()
                    if now_local.hour >= nightly_summary_hour:
                        summary_date = (now_local - timedelta(days=1)).strftime('%Y-%m-%d')
                        marker = _nightly_accuracy_marker_path(summary_date)
                        if not marker.exists() or os.getenv('FORCE_NIGHTLY_ACCURACY_SUMMARY', 'false').lower() == 'true':
                            try:
                                sent_summary = send_nightly_accuracy_summary(summary_date, webhook_url=WEBHOOK_URL)
                                if sent_summary:
                                    print(f"Nightly accuracy summary sent for {summary_date}")
                            except Exception as summary_err:
                                print(f"Nightly accuracy summary failed: {summary_err}")

                should_emit_heartbeat = (
                    heartbeat_enabled
                    and (now_ts - last_heartbeat_ts) >= heartbeat_every_seconds
                    and (heartbeat_force_on_no_games or in_progress_games > 0)
                )
                if should_emit_heartbeat:
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


def _live_monitor_launcher_pid_file():
    return Path('data') / 'live_monitor_launcher.pid'


def _rlm_monitor_pid_file(day_str=None):
    day_str = day_str or datetime.today().strftime('%Y-%m-%d')
    return Path('data') / f'rlm_monitor_{day_str}.pid'


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


def _release_live_monitor_launcher_pidfile(expected_pid=None):
    pid_file = _live_monitor_launcher_pid_file()
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


def _release_rlm_monitor_pidfile(day_str=None, expected_pid=None):
    pid_file = _rlm_monitor_pid_file(day_str)
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


def _claim_live_monitor_launcher_pidfile():
    """Return True when this process becomes the active live monitor launch watcher."""
    pid_file = _live_monitor_launcher_pid_file()
    Path('data').mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text(encoding='utf-8').strip())
            if existing_pid != current_pid and _is_pid_running(existing_pid):
                print(f"Live monitor launch watcher already active under PID {existing_pid}; exiting duplicate watcher.")
                return False
        except Exception:
            pass

    try:
        pid_file.write_text(str(current_pid), encoding='utf-8')
        return True
    except Exception as e:
        print(f"Could not write live monitor launcher PID file: {e}")
        return False


def _claim_rlm_monitor_pidfile(day_str=None):
    """Return True when this process successfully becomes the active RLM watcher."""
    pid_file = _rlm_monitor_pid_file(day_str)
    Path('data').mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text(encoding='utf-8').strip())
            if existing_pid != current_pid and _is_pid_running(existing_pid):
                print(f"RLM monitor already active under PID {existing_pid}; exiting duplicate watcher.")
                return False
        except Exception:
            pass

    try:
        pid_file.write_text(str(current_pid), encoding='utf-8')
        return True
    except Exception as e:
        print(f"Could not write RLM monitor PID file: {e}")
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


def _get_first_scheduled_game_time_local(today_str=None):
    """Return earliest scheduled game datetime (local time) for today's slate."""
    try:
        today_str = today_str or datetime.today().strftime('%m/%d/%Y')
        games = statsapi.schedule(date=today_str) or []
        starts = []
        for game in games:
            raw_dt = game.get('game_datetime') or game.get('gameDate') or game.get('game_date')
            if not raw_dt:
                continue
            try:
                dt_text = str(raw_dt).replace('Z', '+00:00')
                dt_val = datetime.fromisoformat(dt_text)
                if dt_val.tzinfo is not None:
                    dt_val = dt_val.astimezone().replace(tzinfo=None)
                starts.append(dt_val)
            except Exception:
                continue
        if starts:
            return min(starts)
    except Exception:
        pass
    return None


def _resolve_live_monitor_launch_window(first_game_start_local):
    """Return (window_start, window_end) for live monitor launch around first pitch."""
    if first_game_start_local is None:
        return None, None
    prestart_minutes = max(0, _safe_int(os.getenv('LIVE_MONITOR_PRESTART_MINUTES', '90'), 90) or 90)
    poststart_grace_minutes = max(0, _safe_int(os.getenv('LIVE_MONITOR_POSTSTART_GRACE_MINUTES', '30'), 30) or 30)
    window_start = first_game_start_local - timedelta(minutes=prestart_minutes)
    window_end = first_game_start_local + timedelta(minutes=poststart_grace_minutes)
    return window_start, window_end


def _should_launch_live_monitor_now(now_local, first_game_start_local):
    """Decide whether background live monitor should autolaunch now."""
    strict_window = str(os.getenv('LIVE_MONITOR_STRICT_WINDOW', 'true')).strip().lower() not in {'0', 'false', 'no'}
    if not strict_window:
        return True, None

    if first_game_start_local is None:
        return False, "No scheduled games found today; strict pregame launch skipped."

    window_start, window_end = _resolve_live_monitor_launch_window(first_game_start_local)
    if window_start <= now_local <= window_end:
        return True, None

    msg = (
        f"Strict launch window not active. Allowed window: "
        f"{window_start.strftime('%Y-%m-%d %I:%M %p')} to {window_end.strftime('%Y-%m-%d %I:%M %p')} "
        f"(first game {first_game_start_local.strftime('%I:%M %p')})."
    )
    return False, msg


def run_live_monitor_launch_watcher():
    """Wait until the first-game launch window opens, then start the live monitor."""
    if not _claim_live_monitor_launcher_pidfile():
        return

    current_pid = os.getpid()
    atexit.register(_release_live_monitor_launcher_pidfile, current_pid)

    check_seconds = max(15, _safe_int(os.getenv('LIVE_MONITOR_WINDOW_CHECK_SECONDS', '60'), 60) or 60)
    max_hours = max(1, _safe_int(os.getenv('LIVE_MONITOR_WINDOW_WATCH_MAX_HOURS', '18'), 18) or 18)
    deadline_ts = time.time() + (max_hours * 3600)

    print(
        f"Live monitor launch watcher active (PID {current_pid}) - "
        f"checking every {check_seconds}s for up to {max_hours}h"
    )

    while time.time() < deadline_ts:
        try:
            live_pid_file = _live_monitor_pid_file()
            if live_pid_file.exists():
                try:
                    existing_pid = int(live_pid_file.read_text(encoding='utf-8').strip())
                    if _is_pid_running(existing_pid):
                        print(f"Live monitor already running (PID {existing_pid}); launch watcher exiting.")
                        return
                except Exception:
                    pass

            first_game_start_local = _get_first_scheduled_game_time_local(datetime.today().strftime('%m/%d/%Y'))
            now_local = datetime.now()
            should_launch, reason = _should_launch_live_monitor_now(now_local, first_game_start_local)
            if should_launch:
                print("Launch window reached; starting live monitor now.")
                launch_live_monitor_background(enable_window_watcher=False)
                return

            if first_game_start_local is not None:
                window_start, _window_end = _resolve_live_monitor_launch_window(first_game_start_local)
                if now_local > first_game_start_local:
                    print("First game already started and launch window is closed; watcher exiting.")
                    return
                wait_min = max(0, int((window_start - now_local).total_seconds() // 60))
                print(
                    f"Waiting for launch window. First game: {first_game_start_local.strftime('%I:%M %p')} "
                    f"| about {wait_min}m until prestart window."
                )
            elif reason:
                print(reason)
        except Exception as watcher_err:
            print(f"Launch watcher error: {watcher_err}")

        time.sleep(check_seconds)

    print("Live monitor launch watcher timed out without entering launch window.")


def _send_live_monitor_startup_report(webhook_url, current_pid, processed_home_runs, backfill_sent, monitor_sleep_seconds, odds_poll_seconds, first_game_start_local=None):
    """Send a one-time startup sanity report so ops can verify the watcher is alive."""
    lines = [
        "🟢 **LIVE MONITOR STARTED**",
        f"⏱ Time: {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p ET')}",
        f"🆔 PID: {current_pid}",
        f"⚾ Processed HR events loaded: {len(processed_home_runs)}",
        f"🔁 Backfill alerts sent at startup: {int(backfill_sent or 0)}",
        f"📡 Poll rates: monitor={monitor_sleep_seconds}s, odds={odds_poll_seconds}s",
    ]
    if first_game_start_local is not None:
        lines.append(f"🗓️ First scheduled game (local): {first_game_start_local.strftime('%Y-%m-%d %I:%M %p')}")
        if datetime.now() < first_game_start_local:
            lines.append("✅ Pregame tracking is active before first pitch.")
    ok = send_discord_webhook(content="\n".join(lines), webhook_url=webhook_url, async_send=False)
    if ok:
        print("Startup sanity report sent")
    else:
        print("⚠️ Startup sanity report failed")
    return ok


def launch_live_monitor_background(enable_window_watcher=True):
    """Launch one background live monitor process unless already running."""
    pid_file = _live_monitor_pid_file()
    log_file = _live_monitor_log_file()
    Path('data').mkdir(parents=True, exist_ok=True)

    today_str = datetime.today().strftime('%m/%d/%Y')
    first_game_start_local = _get_first_scheduled_game_time_local(today_str)
    should_launch, skip_reason = _should_launch_live_monitor_now(datetime.now(), first_game_start_local)
    if not should_launch:
        if enable_window_watcher:
            strict_window = str(os.getenv('LIVE_MONITOR_STRICT_WINDOW', 'true')).strip().lower() not in {'0', 'false', 'no'}
            watcher_enabled = str(os.getenv('LIVE_MONITOR_WINDOW_WATCHER_ENABLED', 'true')).strip().lower() not in {'0', 'false', 'no'}
            if strict_window and watcher_enabled and first_game_start_local is not None:
                window_start, _window_end = _resolve_live_monitor_launch_window(first_game_start_local)
                if datetime.now() < window_start:
                    launcher_pid_file = _live_monitor_launcher_pid_file()
                    if launcher_pid_file.exists():
                        try:
                            launcher_pid = int(launcher_pid_file.read_text(encoding='utf-8').strip())
                            if _is_pid_running(launcher_pid):
                                print(f"🕒 Live monitor launch watcher already running (PID {launcher_pid}).")
                                print(f"📡 Live monitor autolaunch skipped for now: {skip_reason}")
                                return
                        except Exception:
                            pass
                    try:
                        child = subprocess.Popen(
                            [sys.executable, __file__, "--live-launcher-watch"],
                            stdout=log_file.open('a', encoding='utf-8'),
                            stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
                        )
                        time.sleep(1)
                        if child.poll() is None:
                            print(f"🕒 Live monitor launch watcher started (PID {child.pid}); it will trigger at game-time window.")
                            print(f"📡 Live monitor autolaunch skipped for now: {skip_reason}")
                            return
                    except Exception as watcher_spawn_err:
                        print(f"⚠️ Could not start live monitor launch watcher: {watcher_spawn_err}")
        print(f"📡 Live monitor autolaunch skipped: {skip_reason}")
        return

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
            if first_game_start_local is not None:
                window_start, window_end = _resolve_live_monitor_launch_window(first_game_start_local)
                log_handle.write(
                    f"[{datetime.now().isoformat()}] First game {first_game_start_local.isoformat()} | "
                    f"window {window_start.isoformat()} -> {window_end.isoformat()}\n"
                )
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
    parser.add_argument("--live-launcher-watch", action="store_true", help="Internal: watch schedule and auto-start live monitor inside launch window")
    parser.add_argument("--rlm", action="store_true", help="Monitor all sportsbooks for reverse line movement on today's picks")
    parser.add_argument("--auto-exec", action="store_true", help="Enable direct API wager execution during live RLM/+EV monitoring")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate saved predictions against actual results")
    parser.add_argument("--eval-date", type=str, help="Date to evaluate predictions for, format YYYY-MM-DD")
    parser.add_argument("--notify-eval", action="store_true", help="Send evaluation summary to Discord if webhook is configured")
    parser.add_argument("--self-check", action="store_true", help="Print active mode/weight and physics calibration readiness")
    parser.add_argument("--backfill-physics", action="store_true", help="Backfill physics columns in recent predictions files")
    parser.add_argument("--weekly-todo", action="store_true", help="Print prioritized next-week action list from current system status")
    parser.add_argument("--systematic-ev", action="store_true", help="Run full +EV operation (backfill, self-check, predict, weekly todo)")
    parser.add_argument("--bet-ready", action="store_true", help="Print only actionable wagers (+EV with odds and Kelly > 0)")
    parser.add_argument("--straight-discrepancies", action="store_true", help="Print single straight wagers where model math disagrees with market")
    parser.add_argument("--audit-clv", action="store_true", help="Audit entry vs closing prices and emit CLV performance reports")
    parser.add_argument("--audit-clv-date", type=str, help="Date for CLV audit, format YYYY-MM-DD")
    parser.add_argument("--audit-clv-days", type=int, default=30, help="Rolling lookback window in days for CLV audit trend stats")
    parser.add_argument("--backfill-days", type=int, default=30, help="Lookback window for self-check/backfill commands")

    args = parser.parse_args()

    if args.live:
        monitor_live_home_runs()
        return

    if args.live_launcher_watch:
        run_live_monitor_launch_watcher()
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

    if args.straight_discrepancies:
        print_single_straight_market_discrepancies()
        return

    if args.audit_clv:
        audit_clv_performance(args.audit_clv_date or args.date, lookback_days=max(1, int(args.audit_clv_days)))
        return

    if args.rlm:
        if args.auto_exec:
            os.environ['AUTO_WAGER_ENABLED'] = 'true'
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
        print_single_straight_market_discrepancies()
        launch_live_monitor_background()
        return

    generate_daily_predictions()
    print_conservative_bet_ready_wagers()
    print_single_straight_market_discrepancies()
    
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

    # Auto-launch RLM line-movement monitor if ODDS_API_KEY is configured.
    if os.getenv('ODDS_API_KEY') and str(os.getenv('RLM_AUTOLAUNCH', 'true')).strip().lower() not in {'0', 'false', 'no'}:
        _rlm_marker = Path('data') / f'rlm_monitor_{datetime.today().strftime("%Y-%m-%d")}.pid'
        if not _rlm_marker.exists():
            try:
                _rlm_log = Path('data') / 'rlm_monitor.log'
                _rlm_proc = subprocess.Popen(
                    [sys.executable, __file__, '--rlm'],
                    stdout=_rlm_log.open('a', encoding='utf-8'),
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0,
                )
                import time as _t; _t.sleep(2)
                if _rlm_proc.poll() is None:
                    _rlm_marker.write_text(str(_rlm_proc.pid), encoding='utf-8')
                    print(f"📈 RLM line-movement monitor launched (PID {_rlm_proc.pid}). Log: {_rlm_log}")
            except Exception as _rlm_exc:
                print(f"⚠️ Could not start RLM monitor: {_rlm_exc}")
        else:
            print("📈 RLM line-movement monitor already running today.")


if __name__ == "__main__":
    main()
