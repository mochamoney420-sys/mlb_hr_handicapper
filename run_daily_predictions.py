# =====================================================================
# SECTION 1: IMPORTS
# =====================================================================
import os
import time
import requests
import pandas as pd
import numpy as np
import xgboost as xgb
import mlbgame  # Optional backup
try:
    import statsapi
except ImportError:
    statsapi = None
from datetime import datetime, timedelta

if statsapi is None:
    raise ImportError("'statsapi' package not found. Please install it with: pip install statsapi")
from pybaseball import statcast

# Fix Pybaseball/Savant blocking by forcing a global browser user-agent header
import urllib.request
opener = urllib.request.build_opener()
opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')]
urllib.request.install_opener(opener)

# =====================================================================
# SECTION 2: FETCH LIVE ROBUST HISTORICAL HISTORIES
# =====================================================================
def get_advanced_hr_metrics(days_back=60):
    """Pulls Statcast data, applies Empirical Bayes shrinkage math to protect small samples."""
    cache_dir = "cache"
    os.makedirs(cache_dir, exist_ok=True)
    
    all_days_data = []
    today = datetime.today()
    
    print(f"Gathering historical metrics for the last {days_back} days...")
    for i in range(1, days_back + 1):
        target_date = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        cache_file = os.path.join(cache_dir, f"statcast_{target_date}.csv")
        
        if os.path.exists(cache_file):
            all_days_data.append(pd.read_csv(cache_file))
            continue
            
        print(f"Downloading fresh data from server for: {target_date}")
        retries = 2
        while retries > 0:
            try:
                day_df = statcast(start_dt=target_date, end_dt=target_date)
                if day_df is not None and not day_df.empty:
                    day_df.to_csv(cache_file, index=False)
                    all_days_data.append(day_df)
                retries = 0
                time.sleep(1.5)
            except Exception:
                retries -= 1
                time.sleep(3)
                
    if not all_days_data:
        raise ValueError("Critical Error: No historical model training data retrieved.")
        
    df = pd.concat(all_days_data, ignore_index=True)
    pa_df = df.dropna(subset=['events']).drop_duplicates(subset=['game_pk', 'batter', 'at_bat_number']).copy()
    pa_df['has_platoon_advantage'] = (pa_df['stand'] != pa_df['p_throws']).astype(int)
    
    pa_df['launch_speed'] = pd.to_numeric(pa_df['launch_speed'], errors='coerce').fillna(0)
    pa_df['launch_angle'] = pd.to_numeric(pa_df['launch_angle'], errors='coerce').fillna(0)
    pa_df['is_hr'] = (pa_df['events'] == 'home_run').astype(int)
    pa_df['is_barrel'] = ((pa_df['launch_speed'] >= 98) & (pa_df['launch_angle'] >= 4) & 
                          (pa_df['launch_angle'] <= 50) & ((pa_df['launch_speed'] * 1.5 - pa_df['launch_angle']) >= 117)).astype(int)
    pa_df['is_hard_hit'] = (pa_df['launch_speed'] >= 95).astype(int)
    pa_df['is_pa'] = 1
    
    # -----------------------------------------------------------------
    # CALCULATE LEAGUE WIDE BASELINE CONSTANTS (μ)
    # -----------------------------------------------------------------
    league_hr_mu = pa_df['is_hr'].mean()          # Around 0.032
    league_barrel_mu = pa_df['is_barrel'].mean()  # Around 0.075
    league_hard_hit_mu = pa_df['is_hard_hit'].mean() # Around 0.380
    alpha = 75.0                                  # 75 PA Stabilization Floor
    
    # Aggregate raw player profiles
    batter_summary = pa_df.groupby(['batter', 'has_platoon_advantage']).agg(
        bat_pa_count=('is_pa', 'sum'), bat_hr_count=('is_hr', 'sum'),
        bat_barrel_count=('is_barrel', 'sum'), bat_hard_hit_count=('is_hard_hit', 'sum'),
        bat_avg_launch_angle=('launch_angle', 'mean')
    ).reset_index()
    
    # -----------------------------------------------------------------
    # SOLVE STRUCTURAL MATH FOR BATTER SHRINKAGE REGULARIZATION
    # -----------------------------------------------------------------
    batter_summary['bat_hr_per_pa'] = (
        (batter_summary['bat_hr_count'] + (alpha * league_hr_mu)) / 
        (batter_summary['bat_pa_count'] + alpha)
    )
    batter_summary['bat_barrel_per_pa'] = (
        (batter_summary['bat_barrel_count'] + (alpha * league_barrel_mu)) / 
        (batter_summary['bat_pa_count'] + alpha)
    )
    batter_summary['bat_hard_hit_per_pa'] = (
        (batter_summary['bat_hard_hit_count'] + (alpha * league_hard_hit_mu)) / 
        (batter_summary['bat_pa_count'] + alpha)
    )
    
    # Aggregate raw pitcher profiles
    pitcher_summary = pa_df.groupby(['pitcher', 'stand']).agg(
        pit_pa_count=('is_pa', 'sum'), pit_hr_allowed=('is_hr', 'sum'), pit_barrel_allowed=('is_barrel', 'sum')
    ).reset_index()
    
    # -----------------------------------------------------------------
    # SOLVE STRUCTURAL MATH FOR PITCHER SHRINKAGE REGULARIZATION
    # -----------------------------------------------------------------
    pitcher_summary['pit_hr_per_pa'] = (
        (pitcher_summary['pit_hr_allowed'] + (alpha * league_hr_mu)) / 
        (pitcher_summary['pit_pa_count'] + alpha)
    )
    pitcher_summary['pit_barrel_per_pa'] = (
        (pitcher_summary['pit_barrel_allowed'] + (alpha * league_barrel_mu)) / 
        (pitcher_summary['pit_pa_count'] + alpha)
    )
    
    return batter_summary, pitcher_summary, pa_df


# =====================================================================
# SECTION 3: DAILY STARTING LINEUPS INJECTION
# =====================================================================
def get_today_live_lineups():
    """Queries official MLB endpoints for confirmed active lineups & matchup profiles."""
    today_str = datetime.today().strftime('%Y-%m-%d')
    matchup_rows = []
    
    try:
        # Pull all scheduled games for today
        sched = statsapi.schedule(date=today_str)
        for game in sched:
            game_id = game.get('game_pk')
            # Extract line scores/box scores containing real-time rosters
            try:
                box = statsapi.boxscore_data(game_id)
                # Map home starters
                for b_id in box['home']['battingOrder']:
                    p_info = box['home']['teamPlayers'].get(f"ID{b_id}")
                    if p_info:
                        matchup_rows.append({
                            'player_name': p_info['person']['fullName'], 'batter': b_id, 'stand': p_info['boxscoreName'][0],
                            'pitcher': box['away']['pitchers'][0], 'p_throws': 'R' # Fallback default orientation
                        })
                # Map away starters
                for b_id in box['away']['battingOrder']:
                    p_info = box['away']['teamPlayers'].get(f"ID{b_id}")
                    if p_info:
                        matchup_rows.append({
                            'player_name': p_info['person']['fullName'], 'batter': b_id, 'stand': p_info['boxscoreName'][0],
                            'pitcher': box['home']['pitchers'][0], 'p_throws': 'R'
                        })
            except Exception:
                continue # Skip games not initialized yet
    except Exception as e:
        print(f"Lineup pull failed: {e}. Generating clean mock set for test verification.")
        # Fallback tracking validation block if no games active at script time
        return pd.DataFrame([{
            'player_name': 'Aaron Judge', 'batter': 592450, 'stand': 'R', 'pitcher': 669203, 'p_throws': 'L'
        }])
        
    return pd.DataFrame(matchup_rows) if matchup_rows else get_today_live_lineups()

# =====================================================================
# SECTION 4: DATA COMPILATION & MACHINE LEARNING MODEL
# =====================================================================
def build_training_dataset(pa_df, batter_df, pitcher_df):
    base = pa_df[['batter', 'pitcher', 'stand', 'p_throws', 'has_platoon_advantage', 'is_hr']].copy()
    base = base.merge(batter_df, on=['batter', 'has_platoon_advantage'], how='left')
    base = base.merge(pitcher_df, on=['pitcher', 'stand'], how='left').fillna(0)
    
    features = ['has_platoon_advantage', 'bat_hr_per_pa', 'bat_barrel_per_pa', 'bat_hard_hit_per_pa', 'bat_avg_launch_angle', 'pit_hr_per_pa', 'pit_barrel_per_pa']
    return base[features], base['is_hr']

def train_hr_model(X, y):
    scale_weight = (len(y) - sum(y)) / sum(y) if sum(y) > 0 else 1
    model = xgb.XGBClassifier(n_estimators=100, max_depth=4, learning_rate=0.06, scale_pos_weight=scale_weight, objective='binary:logistic', eval_metric='logloss')
    model.fit(X, y)
    return model

# =====================================================================
# SECTION 5: DISCORD BOT ALERTS
# =====================================================================
def alert_to_discord(webhook_url, message_content):
    if "YOUR_ID_HERE" in webhook_url or not webhook_url.startswith("http"):
        print(f"[Local Console Display Only]:\n{message_content}")
        return
    try:
        requests.post(webhook_url, json={"content": str(message_content)})
    except Exception as e:
        print(f"Discord broadcast alert failed: {e}")

# =====================================================================
# SECTION 6: AUTOMATED MODEL EXECUTION CORE
# =====================================================================
if __name__ == "__main__":
    DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1525525618861543654/jBzZ7vTarJs-j2apC7Ws2M29cF5aaJ9-0JkvdyyK9aJUJRziU9MXqfHyzx0roW4HVHIZ")
    print("🚀 Initializing Live MLB HR Prediction System Workflow...")
    
    try:
        # 1. Fetch metrics & lineups
        batters, pitchers, raw_pas = get_advanced_hr_metrics(days_back=10) # 10 days for fast test execution pass
        today_matchups = get_today_live_lineups()
        
        # 2. Fit machine engine
        X_train, y_train = build_training_dataset(raw_pas, batters, pitchers)
        hr_model = train_hr_model(X_train, y_train)
        
        # 3. Model Predictions on Today's Live Active Slates
        today_matchups['has_platoon_advantage'] = (today_matchups['stand'] != today_matchups['p_throws']).astype(int)
        today_features = today_matchups.merge(batters, on=['batter', 'has_platoon_advantage'], how='left')
        today_features = today_features.merge(pitchers, on=['pitcher', 'stand'], how='left').fillna(0)
        
        features_list = ['has_platoon_advantage', 'bat_hr_per_pa', 'bat_barrel_per_pa', 'bat_hard_hit_per_pa', 'bat_avg_launch_angle', 'pit_hr_per_pa', 'pit_barrel_per_pa']
        
        # Generate real home run prop probabilities
        today_matchups['hr_prob'] = hr_model.predict_proba(today_features[features_list])[:, 1]
        top_picks = today_matchups.sort_values(by='hr_prob', ascending=False).head(3)
        
        # 4. Construct automated alert strings
        alert_msg = "🚨 **MLB MODEL DAILY PROP EDGE HIGHLIGHTS** 🚨\n"
        for _, row in top_picks.iterrows():
            alert_msg += f"🔹 **{row['player_name']}**: Predicted HR Prob: *{row['hr_prob']:.1%}* (Platoon Advantage: {row['has_platoon_advantage']})\n"
            
        alert_to_discord(DISCORD_WEBHOOK, alert_msg)
    except Exception as e:
        print(f"Error running MLB HR prediction workflow: {e}")