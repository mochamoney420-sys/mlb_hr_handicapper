# =====================================================================
# SECTION 1: IMPORTS
# =====================================================================
import os
import time
import pandas as pd
import numpy as np
import xgboost as xgb
import requests
from datetime import datetime, timedelta
from pybaseball import statcast

# Fix potential Pybaseball/Savant blocking by forcing a browser user-agent header globally
import urllib.request
opener = urllib.request.build_opener()
opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')]
urllib.request.install_opener(opener)

# =====================================================================
# SECTION 2: ROBUST DATA SOURCING WITH DAILY CACHING
# =====================================================================
def get_advanced_hr_metrics(days_back=60):
    """Pulls Statcast data day-by-day, caches locally, and handles server errors safely."""
    cache_dir = "cache"
    os.makedirs(cache_dir, exist_ok=True)
    
    all_days_data = []
    today = datetime.today()
    
    print(f"Gathering data for the last {days_back} days...")
    
    for i in range(1, days_back + 1):
        target_date = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        cache_file = os.path.join(cache_dir, f"statcast_{target_date}.csv")
        
        # Check if we already downloaded this day's data locally
        if os.path.exists(cache_file):
            day_df = pd.read_csv(cache_file)
            all_days_data.append(day_df)
            continue
            
        # If not cached, fetch from server with retries and a cooldown delay
        print(f"Downloading fresh data from server for: {target_date}")
        retries = 3
        success = False
        
        while retries > 0 and not success:
            try:
                # Query just 1 single day to prevent giant server payloads
                day_df = statcast(start_dt=target_date, end_dt=target_date)
                
                if day_df is not None and not day_df.empty:
                    # Save to local cache folder
                    day_df.to_csv(cache_file, index=False)
                    all_days_data.append(day_df)
                
                success = True
                # Cooldown period to avoid rate-limiting/bot-blocking
                time.sleep(2) 
                
            except Exception as e:
                retries -= 1
                print(f"Server connection failed for {target_date}. Retries left: {retries}. Error: {e}")
                time.sleep(5) # Longer wait on failure
                
    if not all_days_data:
        raise ValueError("Critical Error: No data could be retrieved from cache or server.")
        
    # Combine all days together safely
    df = pd.concat(all_days_data, ignore_index=True)
    
    # Process isolated final plate appearance metrics
    pa_df = df.dropna(subset=['events']).drop_duplicates(subset=['game_pk', 'batter', 'at_bat_number']).copy()
    pa_df['has_platoon_advantage'] = (pa_df['stand'] != pa_df['p_throws']).astype(int)
    
    pa_df['launch_speed'] = pd.to_numeric(pa_df['launch_speed'], errors='coerce').fillna(0)
    pa_df['launch_angle'] = pd.to_numeric(pa_df['launch_angle'], errors='coerce').fillna(0)
    
    pa_df['is_hr'] = (pa_df['events'] == 'home_run').astype(int)
    pa_df['is_barrel'] = ((pa_df['launch_speed'] >= 98) & 
                          (pa_df['launch_angle'] >= 4) & 
                          (pa_df['launch_angle'] <= 50) & 
                          ((pa_df['launch_speed'] * 1.5 - pa_df['launch_angle']) >= 117)).astype(int)
    pa_df['is_hard_hit'] = (pa_df['launch_speed'] >= 95).astype(int)
    
    ab_events = ['single', 'double', 'triple', 'home_run', 'field_out', 'strikeout', 'grounded_into_double_play', 'fielders_choice']
    pa_df['is_pa'] = 1  
    pa_df['is_ab'] = pa_df['events'].isin(ab_events).astype(int)
    
    # Structural math: Batter Profiles
    batter_summary = pa_df.groupby(['batter', 'has_platoon_advantage']).agg(
        bat_pa_count=('is_pa', 'sum'), bat_hr_count=('is_hr', 'sum'),
        bat_barrel_count=('is_barrel', 'sum'), bat_hard_hit_count=('is_hard_hit', 'sum'),
        bat_avg_launch_angle=('launch_angle', 'mean')
    ).reset_index()
    
    batter_summary['bat_hr_per_pa'] = batter_summary['bat_hr_count'] / batter_summary['bat_pa_count']
    batter_summary['bat_barrel_per_pa'] = batter_summary['bat_barrel_count'] / batter_summary['bat_pa_count']
    batter_summary['bat_hard_hit_per_pa'] = batter_summary['bat_hard_hit_count'] / batter_summary['bat_pa_count']
    
    # Structural math: Pitcher Profiles
    pitcher_summary = pa_df.groupby(['pitcher', 'stand']).agg(
        pit_pa_count=('is_pa', 'sum'), pit_hr_allowed=('is_hr', 'sum'), pit_barrel_allowed=('is_barrel', 'sum')
    ).reset_index()
    
    pitcher_summary['pit_hr_per_pa'] = pitcher_summary['pit_hr_allowed'] / pitcher_summary['pit_pa_count']
    pitcher_summary['pit_barrel_per_pa'] = pitcher_summary['pit_barrel_allowed'] / pitcher_summary['pit_pa_count']
    
    return batter_summary, pitcher_summary, pa_df

# =====================================================================
# SECTION 3: FEATURE MATRIX SELECTION
# =====================================================================
def build_training_dataset(pa_df, batter_df, pitcher_df):
    """Combines features safely into an XGBoost matrix."""
    base = pa_df[['batter', 'pitcher', 'stand', 'p_throws', 'has_platoon_advantage', 'is_hr']].copy()
    base = base.merge(batter_df, on=['batter', 'has_platoon_advantage'], how='left')
    base = base.merge(pitcher_df, on=['pitcher', 'stand'], how='left')
    base = base.fillna(0)
    
    features = [
        'has_platoon_advantage', 'bat_hr_per_pa', 'bat_barrel_per_pa', 
        'bat_hard_hit_per_pa', 'bat_avg_launch_angle', 'pit_hr_per_pa', 'pit_barrel_per_pa'
    ]
    return base[features], base['is_hr']

# =====================================================================
# SECTION 4: MACHINE LEARNING MODEL
# =====================================================================
def train_hr_model(X, y):
    scale_weight = (len(y) - sum(y)) / sum(y) if sum(y) > 0 else 1
    model = xgb.XGBClassifier(
        n_estimators=150, max_depth=4, learning_rate=0.05,
        scale_pos_weight=scale_weight, objective='binary:logistic', eval_metric='logloss'
    )
    model.fit(X, y)
    return model

# =====================================================================
# SECTION 5: DISCORD BOT ALERTS
# =====================================================================
def alert_to_discord(webhook_url, message_content):
    payload = {"content": str(message_content)}
    try:
        requests.post(webhook_url, json=payload)
    except Exception as e:
        print(f"Discord broadcast alert failed: {e}")

# =====================================================================
# SECTION 6: AUTOMATED EXECUTION CORE
# =====================================================================
if __name__ == "__main__":
    # Change to your actual Discord webhook string URL:
    DISCORD_WEBHOOK = "https://discord.com"
    
    try:
        batters, pitchers, raw_pas = get_advanced_hr_metrics(days_back=60)
        X_train, y_train = build_training_dataset(raw_pas, batters, pitchers)
        
        print(f"Training XGBoost classifier model on {len(X_train)} dataset rows...")
        hr_model = train_hr_model(X_train, y_train)
        
        sample_alert = "✅ **MLB PIPELINE SUCCESS** 🚨\nSavant data successfully bypassed, compiled, and cached. XGBoost training matrix active."
        alert_to_discord(DISCORD_WEBHOOK, sample_alert)
        print("Handicapping run successfully finished!")
        
    except Exception as main_error:
        error_alert = f"❌ **PIPELINE FAILURE NOTIFICATION**\nError details encountered: {main_error}"
        alert_to_discord(DISCORD_WEBHOOK, error_alert)
        print(f"Process terminated by script: {main_error}")
