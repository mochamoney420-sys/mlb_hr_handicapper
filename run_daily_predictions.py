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
import subprocess
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


def send_discord_webhook(content=None, embeds=None, webhook_url=None):
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

    for idx, candidate in enumerate(webhook_candidates):
        try:
            response = requests.post(candidate, json=payload, timeout=8)
            if response.status_code == 204:
                if idx > 0:
                    print("Discord notification sent successfully using backup webhook.")
                else:
                    print("Discord notification sent successfully.")
                return True
            if response.status_code == 404:
                print(
                    f"Discord webhook returned 404 for candidate #{idx + 1}. "
                    "This webhook was likely deleted/rotated; trying next candidate if available."
                )
                continue
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                print(
                    "Discord rate-limited webhook call (429). "
                    f"Retry-After={retry_after}; trying next candidate if available."
                )
                continue

            print(
                f"Discord webhook returned status {response.status_code}: "
                f"{getattr(response, 'text', '')}"
            )
            return False
        except Exception as e:
            print(f"Failed to send Discord notification via candidate #{idx + 1}: {e}")

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


def calibrate_physics_blend_weight(days_lookback=30, default_weight=0.45):
    """Grid-search blend weight over recent predictions with physics diagnostics.

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


def resolve_kelly_multiplier(days_lookback=14, default_multiplier=0.50):
    """Resolve Kelly multiplier from env override or recent evaluation drift.

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
    if avg_brier <= 0.08:
        mult = 0.60
    elif avg_brier <= 0.12:
        mult = 0.50
    elif avg_brier <= 0.16:
        mult = 0.40
    else:
        mult = 0.30

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
    """Print current runtime mode/weight and physics calibration readiness."""
    print("\n" + "=" * 70)
    print("MODEL SELF-CHECK")
    print("=" * 70)

    mode_env = str(os.getenv('HR_PROB_MODE', 'blended')).strip().lower()
    weight_override = os.getenv('HR_PHYSICS_BLEND_WEIGHT')
    print(f"Physics module loaded: {apply_physics_pipeline_to_live is not None}")
    print(f"HR_PROB_MODE env: {mode_env}")
    print(f"HR_PHYSICS_BLEND_WEIGHT env: {weight_override if weight_override else 'not set'}")
    print(f"FREE_ODDS_STRICT_SCHEMA: {os.getenv('FREE_ODDS_STRICT_SCHEMA', 'true')}")

    reject_log = os.getenv('FREE_ODDS_REJECT_LOG', '').strip()
    if reject_log:
        reject_path = Path(reject_log)
    else:
        reject_path = Path('data') / f"free_odds_rejects_{datetime.today().strftime('%Y-%m-%d')}.csv"
    if reject_path.exists():
        try:
            _rej = pd.read_csv(reject_path)
            print(f"Free-odds reject log: {reject_path} ({len(_rej)} rows)")
        except Exception:
            print(f"Free-odds reject log: {reject_path} (unreadable)")
    else:
        print(f"Free-odds reject log: {reject_path} (not created yet)")

    cutoff = datetime.today() - timedelta(days=days_lookback)
    pred_files = []
    files_with_physics = 0
    for f in sorted(Path('data').glob('predictions_*.csv')):
        try:
            file_date = datetime.strptime(f.stem.replace('predictions_', ''), '%Y-%m-%d')
            if file_date < cutoff:
                continue
            pred_files.append(f)
            df = pd.read_csv(f, nrows=5)
            if 'physics_hr_prob' in df.columns:
                files_with_physics += 1
        except Exception:
            continue

    print(f"Prediction files in lookback window: {len(pred_files)}")
    print(f"Files with physics columns: {files_with_physics}")

    effective_mode, effective_weight = resolve_probability_mode_and_weight()
    print(f"Effective mode: {effective_mode}")
    print(f"Effective physics blend weight: {effective_weight:.2f}")


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
        todos.append(("HIGH", "Set ODDS_API_KEY in .vscode/.env to unlock live market EV and RLM."))
        todos.append(("HIGH", "Or configure compliant free-source ingest: FREE_ODDS_JSON_PATH / FREE_ODDS_CSV_PATH / FREE_ODDS_PUBLIC_URLS."))
    else:
        try:
            probe = fetch_hr_prop_odds()
            if not probe:
                todos.append(("HIGH", "Odds API responding without usable HR prop data. Verify plan/market access and request parameters."))
        except Exception:
            todos.append(("HIGH", "Odds API probe failed. Verify key, plan tier, and API availability."))

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
    'player_home_runs',
]


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


def _parse_odds_event_commence_time(event_obj):
    """Parse Odds API commence_time into a datetime, or None if unavailable."""
    try:
        ts = str((event_obj or {}).get('commence_time', '')).strip()
        if not ts:
            return None
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


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
    market_keys = _get_hr_prop_market_key_candidates()
    unsupported_markets = []
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
                    return parsed
                print(f"Odds API top-level endpoint returned no {market_key} outcomes; trying event-level endpoint.")
            else:
                body = (getattr(resp, 'text', '') or '')[:220]
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
                return merged

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

    return {}


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
    """Fetch live HR prop lines from The Odds API. Returns {player_name: devigged_prob}.
    Set ODDS_API_KEY in .env to enable real market edge calculation."""
    api_key = os.getenv('ODDS_API_KEY')
    if not api_key:
        if load_free_odds_sources is not None and build_devigged_probs_from_books is not None:
            raw_free = load_free_odds_sources()
            if raw_free:
                probs = build_devigged_probs_from_books(raw_free)
                print(f"Free source odds: {sum(len(v) for v in raw_free.values())} book-player pairs across {len(probs)} players")
                return probs
        return {}
    try:
        player_all_odds = _fetch_hr_props_raw_from_odds_api(api_key)
        if not player_all_odds:
            if load_free_odds_sources is not None and build_devigged_probs_from_books is not None:
                raw_free = load_free_odds_sources()
                if raw_free:
                    probs = build_devigged_probs_from_books(raw_free)
                    print(f"Free source odds fallback: {sum(len(v) for v in raw_free.values())} book-player pairs across {len(probs)} players")
                    return probs
            return {}

        return _build_devigged_probs_from_raw_books(player_all_odds)
    except Exception as e:
        print(f"Odds API fetch failed: {e}")
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
    if not api_key:
        if load_free_odds_sources is not None:
            return load_free_odds_sources()
        return {}
    try:
        raw = _fetch_hr_props_raw_from_odds_api(api_key)
        if not raw:
            if load_free_odds_sources is not None:
                return load_free_odds_sources()
            return {}
        return raw
    except Exception as e:
        print(f"Odds raw fetch failed: {e}")
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
    except Exception as e:
        print(f"⚠️  HR pattern learning failed: {e}")
    
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
    
    # =====================================================================
    # PHASE 1: LOAD TRAINING DATA
    # =====================================================================
    print("\n" + "="*70)
    print("PHASE 1: LOADING TRAINING DATA")
    print("="*70)
    b_stats, p_stats, raw_pa, pitch_statcast_df = get_advanced_hr_metrics(days_back=60)
    
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

    sample_weights = load_feedback_weights(train_df)
    missed_count = int((sample_weights > 1.2).sum())
    print(f"Feedback weights loaded — {missed_count} training rows upweighted from past misses.")
    
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
    ]
    
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

    # Fill missing numeric features with reasonable baselines
    for col in ['bat_pa_count', 'bat_hr_rate', 'bat_barrel_rate', 'bat_hard_hit_rate', 'bat_sweet_spot_rate']:
        if col in live.columns:
            live[col] = live[col].fillna(0)
    for col in ['pitch_pa_count', 'pitch_hr_allowed_rate', 'pitch_barrel_allowed_rate', 'pitch_hard_hit_allowed_rate', 'pitch_sweet_spot_allowed_rate']:
        if col in live.columns:
            live[col] = live[col].fillna(live[col].mean() if not live[col].isna().all() else 0)

    live['park_factor'] = live['park_factor'].fillna(100)
    live['temp'] = live['temp'].fillna(71.0)
    live['wind_speed'] = live['wind_speed'].fillna(5.0)
    live['bat_hr_fb_rate'] = live['bat_hr_fb_rate'].fillna(0.12)
    live['pitch_hr_fb_allowed_rate'] = live['pitch_hr_fb_allowed_rate'].fillna(0.12)
    live['bat_ev90'] = live['bat_ev90'].fillna(88.0)
    live['bat_avg_exit_velocity'] = live['bat_avg_exit_velocity'].fillna(88.0)
    live['bat_max_exit_velocity'] = live['bat_max_exit_velocity'].fillna(102.0)
    live['bat_avg_launch_angle'] = live['bat_avg_launch_angle'].fillna(12.0)
    live['bat_iso_proxy'] = live['bat_iso_proxy'].fillna(0.08)
    live['bat_days_since_last_game'] = live['bat_days_since_last_game'].fillna(1)
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
        except Exception as _physics_err:
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

    live['physics_hr_prob'] = physics_probs
    live['blend_weight_physics'] = physics_weight if prob_mode == 'blended' else 0.0
    live['probability_mode'] = prob_mode
    live['physics_delta'] = probs - base_model_probs

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
    live['edge_pct'] = ((live['pred_hr_prob'] - _market_prob) / _market_prob * 100).round(1)
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
        live['best_book'] = raw_matches.apply(lambda x: x[0])
        live['best_market_odds_american'] = raw_matches.apply(lambda x: x[1])
        live['best_market_implied_prob'] = live['best_market_odds_american'].apply(
            lambda x: american_to_implied_prob(x) if pd.notna(x) else np.nan
        )

        # Prefer devigged market prob where available, else implied from best line.
        live['market_prob'] = pd.to_numeric(live['market_prob'], errors='coerce')
        live['market_prob'] = live['market_prob'].fillna(live['best_market_implied_prob'])

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
        live['elite_ev_signal'] = (
            (live['prob_edge_abs'] >= elite_edge_abs) &
            (live['ev_percent'] >= elite_ev_pct)
        )
        live['release_window_sniper_signal'] = (
            (live.get('line_release_window_flag', 0) == 1) & live['elite_ev_signal']
        )

        # Blend sportsbook value score with observed edge and under-drag regime.
        _sv = 1.0 + live['prob_edge_abs'].clip(lower=0.0, upper=0.12)
        _nrfi = 1.0 + pd.to_numeric(live.get('nrfi_under_drag_score', 0.0), errors='coerce').fillna(0.0) * 0.08
        live['sportsbook_value_score'] = (_sv * _nrfi).clip(1.0, 1.25)
        
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
        live['ev_value'] = 0.0
        live['ev_percent'] = 0.0
        live['is_positive_ev'] = False
        live['best_book'] = None
        live['best_market_odds_american'] = np.nan
        live['best_market_implied_prob'] = np.nan
        live['fair_odds_american'] = live['pred_hr_prob'].apply(prob_to_fair_american)
        live['prob_edge_abs'] = 0.0
        live['elite_ev_signal'] = False
        live['release_window_sniper_signal'] = False

    physics_output_defaults = {
        'physics_hr_prob': 0.0,
        'physics_per_pa_hr_prob': 0.0,
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
        'context_multiplier': 1.0
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
                                    'best_book', 'best_market_odds_american', 'best_market_implied_prob',
                                    'fair_odds_american', 'prob_edge_abs', 'elite_ev_signal',
                                    'release_window_sniper_signal', 'line_release_window_flag', 'nrfi_under_drag_score',
                                    'physics_hr_prob', 'physics_per_pa_hr_prob', 'density_altitude_ft',
                                    'air_density_kg_m3', 'drag_multiplier', 'pitch_micro_matchup_score',
                                    'pitch_arsenal_matchup_score', 'vaa_attack_angle_score', 'umpire_catcher_cascade', 'umpire_zone_drift_score',
                                    'umpire_hotzone_overlap', 'fatigue_index', 'circadian_disruption_index', 'visual_fatigue_modifier',
                                    'travel_distance_miles', 'rest_day_count',
                                    'spin_decay_rpm', 'spin_decay_flag', 'release_pos_x_std_15', 'release_pos_z_std_15',
                                    'release_extension_decay_ft', 'spin_velocity_ratio_decay', 'primary_weapon_vulnerable_pitch_count',
                                    'bullpen_exposure_multiplier',
                                    'lineup_protection_woba_proxy', 'context_multiplier',
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
    rankings = live[['batter_name', 'pitcher_name', 'pred_hr_prob', 'edge_pct', 'kelly_fraction', 'ev_percent', 'game_time']].rename(
        columns={'pred_hr_prob': 'hr_probability', 'ev_percent': 'ev_pct'})

    discord_top_prob_n = max(10, _env_int('DISCORD_TOP_PROB_COUNT', 30))
    discord_top_ev_n = max(3, _env_int('DISCORD_TOP_EV_COUNT', 12))
    discord_rows_per_message = max(5, _env_int('DISCORD_ROWS_PER_MESSAGE', 10))
    discord_min_prob = _env_float('DISCORD_MIN_PROB', 0.06)
    discord_radar_n = max(8, _env_int('DISCORD_RADAR_COUNT', 20))
    discord_window_1_hours = max(1, _env_int('DISCORD_WINDOW_1_HOURS', 2))
    discord_window_2_hours = max(discord_window_1_hours + 1, _env_int('DISCORD_WINDOW_2_HOURS', 6))

    # Top probabilities for reporting/Discord delivery.
    prob_pool = rankings.sort_values(by='hr_probability', ascending=False).reset_index(drop=True)
    top_prob = prob_pool[prob_pool['hr_probability'] >= discord_min_prob].head(discord_top_prob_n).copy()
    if top_prob.empty:
        top_prob = prob_pool.head(discord_top_prob_n).copy()

    radar = pd.DataFrame()
    if 'physics_delta' in live.columns:
        radar = live[[
            'batter_name', 'pitcher_name', 'pred_hr_prob', 'edge_pct',
            'kelly_fraction', 'ev_percent', 'game_time', 'physics_delta'
        ]].rename(columns={'pred_hr_prob': 'hr_probability', 'ev_percent': 'ev_pct'}).copy()
        radar['hr_probability'] = pd.to_numeric(radar['hr_probability'], errors='coerce').fillna(0.0)
        radar['physics_delta'] = pd.to_numeric(radar['physics_delta'], errors='coerce').fillna(0.0)
        radar = radar[radar['hr_probability'] >= max(0.03, discord_min_prob * 0.7)]

        top_keys = set(zip(top_prob['batter_name'].astype(str), top_prob['pitcher_name'].astype(str)))
        radar = radar[
            ~radar.apply(lambda r: (str(r['batter_name']), str(r['pitcher_name'])) in top_keys, axis=1)
        ]
        radar = radar.sort_values(
            by=['physics_delta', 'hr_probability'],
            key=lambda s: s.abs() if s.name == 'physics_delta' else s,
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
            top_ev = positive_ev[['batter_name', 'pitcher_name', 'pred_hr_prob', 'edge_pct', 'kelly_fraction', 'ev_percent', 'game_time']].rename(
                columns={'pred_hr_prob': 'hr_probability', 'ev_percent': 'ev_pct'})
            top_ev = top_ev.sort_values(by='ev_pct', ascending=False).head(discord_top_ev_n).reset_index(drop=True)
            print(f"\n✅ +EV PREMIUM PICKS (Expected Value > 0%):")
            print(top_ev.to_string(index=False))
    top_ev = _annotate_time_windows(top_ev)

    print(f"\nTop {min(5, len(top_prob))} Daily Projected HR Probabilities:")
    print(top_prob.head(5).to_string(index=False))
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
            table_rows.append(
                f"| {str(row['batter_name'])[:14]:<14} | {str(row['pitcher_name'])[:14]:<14} | {gtime:<8} | {win:<10} | {pct:<6} | {edge:<7} | {ev_str:<7} | {kelly:<6} |"
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
                f"| {'Batter':<14} | {'Pitcher':<14} | {'Time ET':<8} | {'Window':<10} | {'Prob':<6} | {'Edge':<7} | {'EV%':<7} | {'Kelly':<6} |\n"
                f"|{'-'*16}|{'-'*16}|{'-'*10}|{'-'*12}|{'-'*8}|{'-'*9}|{'-'*9}|{'-'*8}|\n"
                f"{table_str}\n"
                "```"
            )
            if not send_discord_webhook(content=message_content):
                return False

        return True

    summary_lines = [
        f"\u26be MLB HR MODEL SNAPSHOT ({target_date})",
        f"Candidates ranked: {len(rankings)}",
        f"Delivered top probability picks: {len(top_prob)}",
        f"Delivered radar picks: {len(radar)}",
        f"Delivered +EV picks: {len(top_ev)}",
        f"Time windows: <= {discord_window_1_hours}h, <= {discord_window_2_hours}h, later",
    ]
    if not send_discord_webhook(content="\n".join(summary_lines)):
        print("Failed to transmit Discord summary after trying configured candidates.")

    if top_prob.empty:
        print("No predictions available to send to Discord.")
        return live

    sent_prob = _post_pick_table_batches(top_prob, f"\u26be Top Probability HR Picks (Top {len(top_prob)})")
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
    and return (model_prob, was_predicted, was_top5, model_rank) for Discord annotation."""
    today_str = datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{today_str}.csv'

    model_prob = None
    batter_id = None
    pitcher_id = None
    was_predicted = False
    was_top5 = False
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
                top5_names = preds.nlargest(5, 'pred_hr_prob')['batter_name'].str.lower().str.strip().tolist()
                was_top5 = batter_name.lower().strip() in top5_names
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

    return model_prob, was_predicted, was_top5, model_rank


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

    print("🚀 Monitoring started: Waiting for live MLB home run events...")
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

    odds_poll_seconds = max(5, _safe_int(os.getenv('LIVE_ODDS_POLL_SECONDS', '5'), 5) or 5)
    monitor_sleep_seconds = max(5, _safe_int(os.getenv('LIVE_MONITOR_POLL_SECONDS', '5'), 5) or 5)
    last_odds_poll_ts = 0.0
    best_odds_prev = {}
    best_odds_now = {}

    while True:
        try:
            today_str = datetime.today().strftime('%m/%d/%Y')
            games = statsapi.schedule(date=today_str) or []
            in_progress_games = 0
            detected_this_loop = 0
            sent_this_loop = 0
            micro_signals_this_loop = 0

            now_ts = time.time()
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
                if game.get('status') != 'In Progress':
                    continue
                in_progress_games += 1
                game_id = game.get('game_pk') or game.get('game_id')
                if not game_id:
                    continue
                play_by_play = statsapi.get('game', {'gamePk': game_id}) or {}
                all_plays = play_by_play.get('liveData', {}).get('plays', {}).get('allPlays', [])

                # Micro-signal: release-axis tilt
                for alert in _detect_release_axis_tilt_signal(game, play_by_play, power_profile, micro_alert_keys):
                    msg = _format_micro_signal_message(alert)
                    if msg and send_discord_webhook(content=msg, webhook_url=WEBHOOK_URL):
                        micro_signals_this_loop += 1
                        sent_this_loop += 1
                        print(f"Micro signal sent: {alert.get('type')} ({alert.get('game_display','')})")

                # Micro-signal: predictable pitch sequence in hitter's counts
                for alert in _detect_predictable_count_signal(game, play_by_play, power_profile, tendency_lookup, micro_alert_keys):
                    msg = _format_micro_signal_message(alert)
                    if msg and send_discord_webhook(content=msg, webhook_url=WEBHOOK_URL):
                        micro_signals_this_loop += 1
                        sent_this_loop += 1
                        print(f"Micro signal sent: {alert.get('type')} ({alert.get('game_display','')})")

                # Micro-signal: odds inversion after hard-hit lineout clusters
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

                # Live reprice: send refreshed HR predictions when pitcher quality deteriorates.
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

                for play in all_plays:
                    result = play.get('result', {})
                    about = play.get('about', {})
                    matchup = play.get('matchup', {})
                    event_name = str(result.get('event', '')).lower()
                    event_type = str(result.get('eventType', '')).lower()
                    if event_name not in ('home run', 'home_run', 'homerun', 'hr') and event_type not in ('home_run', 'home run', 'homerun', 'hr'):
                        continue

                    event_id = about.get('playId')
                    if not event_id:
                        # Some StatsAPI payloads omit playId for completed events.
                        # Build a deterministic fallback key for dedupe.
                        batter_id = matchup.get('batter', {}).get('id') or ''
                        at_bat_idx = about.get('atBatIndex', '')
                        inning = about.get('inning', '')
                        half = about.get('halfInning', '')
                        event_id = f"{game_id}:{inning}:{half}:{at_bat_idx}:{batter_id}:{event_type or event_name}"

                    if event_id in processed_home_runs:
                        continue
                    detected_this_loop += 1

                    description = result.get('description', 'A home run was hit!')
                    inning_half = about.get('halfInning', '')
                    num_inning = about.get('inning', '')
                    batter_name = matchup.get('batter', {}).get('fullName') or ''
                    pitcher_name = matchup.get('pitcher', {}).get('fullName') or ''
                    game_display = f"{game.get('away_name','Away')} @ {game.get('home_name','Home')}"
                    alert_ts = datetime.now().strftime('%Y-%m-%d %I:%M:%S %p ET')

                    # Log live outcome against today's predictions for learning feedback
                    _model_prob, _was_predicted, _was_top5, _model_rank = None, False, False, None
                    if batter_name:
                        try:
                            _model_prob, _was_predicted, _was_top5, _model_rank = log_live_hr_feedback(
                                batter_name, pitcher_name, game_id, inning_half, num_inning
                            )
                        except Exception:
                            pass

                    message_lines = [
                        "\U0001f6a8 **LIVE HOME RUN ALERT** \U0001f6a8",
                        f"\u23f0 Time: {alert_ts}",
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
                        elif _model_rank is not None:
                            message_lines.append(
                                f"\U0001f4ca Model rank: #{_model_rank} (Prob: {prob_str}) — not a Top 5 list pick"
                            )
                        elif _was_predicted:
                            message_lines.append(f"\u2705 Model signaled HR risk (Prob: {prob_str})")
                        else:
                            message_lines.append(f"\u26a0\ufe0f Model missed (had: {prob_str}) — logged for retraining")
                    else:
                        message_lines.append(f"\u26a0\ufe0f Not in today's predictions — logged for retraining")

                    payload = {"content": "\n".join(message_lines)}

                    sent = send_discord_webhook(content=payload.get("content"), webhook_url=WEBHOOK_URL)
                    if sent:
                        print(f"Live HR alert sent for play {event_id} in {game_display}.")
                        processed_home_runs.add(event_id)
                        save_processed_home_run_events(processed_home_runs)
                        sent_this_loop += 1
                    else:
                        print(f"Live HR webhook post failed for play {event_id}; will retry on next poll.")
            write_live_monitor_status({
                'mode': 'live_hr_monitor',
                'in_progress_games': in_progress_games,
                'detected_events_this_loop': detected_this_loop,
                'sent_events_this_loop': sent_this_loop,
                'micro_signals_this_loop': micro_signals_this_loop,
                'processed_event_count': len(processed_home_runs),
                'odds_players_tracked': len(best_odds_now),
                'live_monitor_poll_seconds': monitor_sleep_seconds,
                'live_odds_poll_seconds': odds_poll_seconds,
            })
            time.sleep(monitor_sleep_seconds)
        except Exception as e:
            print("Error checking live feeds:", e)
            write_live_monitor_status({
                'mode': 'live_hr_monitor',
                'error': str(e),
                'processed_event_count': len(processed_home_runs),
            })
            time.sleep(10)


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


def launch_live_monitor_background():
    """Launch one background live monitor process unless already running."""
    pid_file = Path('data') / 'live_monitor.pid'
    Path('data').mkdir(parents=True, exist_ok=True)

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
            if _is_pid_running(existing_pid):
                print(f"Live monitor already running (PID {existing_pid}).")
                return
        except Exception:
            pass

    try:
        child = subprocess.Popen(
            [sys.executable, __file__, "--live"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        )
        pid_file.write_text(str(child.pid), encoding='utf-8')
        print(f"✅ Live monitor launched in background (PID {child.pid}).")
    except Exception as e:
        print(f"⚠️ Could not start live monitor: {e}")


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
        launch_live_monitor_background()
        return

    generate_daily_predictions()
    
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
