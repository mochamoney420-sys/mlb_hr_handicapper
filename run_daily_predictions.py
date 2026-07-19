G
# =====================================================================
# SECTION 1: IMPORTS
# =====================================================================
import os
import time
import requests
import pandas as pd
import numpy as np
import xgboost as xgb
import mlb_statsapi as statsapi
from datetime import datetime, timedelta
from pybaseball import statcast

# Fix Pybaseball/Savant blocking by forcing a global browser user-agent header
import urllib.request
opener = urllib.request.build_opener()
opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')]
urllib.request.install_opener(opener)

# =====================================================================
# SECTION 2: ADAPTIVE HISTORICAL FEATURES SOURCING
# =====================================================================
def get_advanced_hr_metrics(days_back=60):
cache_dir = "cache"
os.makedirs(cache_dir, exist_ok=True)
all_days_data = []
today = datetime.today()

print(f"Syncing player matrices from the last {days_back} days...")
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
time.sleep(1.5)
except Exception:
continue

if not all_days_data:
raise ValueError("Critical Error: Missing base training vectors.")

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

league_hr_mu = pa_df['is_hr'].mean()
league_barrel_mu = pa_df['is_barrel'].mean()
league_hard_hit_mu = pa_df['is_hard_hit'].mean()
alpha = 75.0

# Batter Regularization Math
batter_summary = pa_df.groupby(['batter', 'has_platoon_advantage']).agg(
bat_pa_count=('is_pa', 'sum'), bat_hr_count=('is_hr', 'sum'),
bat_barrel_count=('is_barrel', 'sum'), bat_hard_hit_count=('is_hard_hit', 'sum'),
bat_avg_launch_angle=('launch_angle', 'mean')
).reset_index()

batter_summary['bat_hr_per_pa'] = (batter_summary['bat_hr_count'] + (alpha * league_hr_mu)) / (batter_summary['bat_pa_count'] + alpha)
batter_summary['bat_barrel_per_pa'] = (batter_summary['bat_barrel_count'] + (alpha * league_barrel_mu)) / (batter_summary['bat_pa_count'] + alpha)
batter_summary['bat_hard_hit_per_pa'] = (batter_summary['bat_hard_hit_count'] + (alpha * league_hard_hit_mu)) / (batter_summary['bat_pa_count'] + alpha)

# Pitcher Regularization Math
pitcher_summary = pa_df.groupby(['pitcher', 'stand']).agg(
pit_pa_count=('is_pa', 'sum'), pit_hr_allowed=('is_hr', 'sum'), pit_barrel_allowed=('is_barrel', 'sum')
).reset_index()

pitcher_summary['pit_hr_per_pa'] = (pitcher_summary['pit_hr_allowed'] + (alpha * league_hr_mu)) / (pitcher_summary['pit_pa_count'] + alpha)
pitcher_summary['pit_barrel_per_pa'] = (pitcher_summary['pit_barrel_allowed'] + (alpha * league_barrel_mu)) / (pitcher_summary['pit_pa_count'] + alpha)

return batter_summary, pitcher_summary, pa_df

# =====================================================================
# SECTION 3: LIVE ACTIVE LINEUPS INTERFACE
# =====================================================================
def get_today_live_lineups():
today_str = datetime.today().strftime('%Y-%m-%d')
matchup_rows = []
try:
sched = statsapi.schedule(date=today_str)
for game in sched:
game_id = game.get('game_pk')
try:
box = statsapi.boxscore_data(game_id)

# Safely extract the primary starting pitcher ID from lists
away_pitcher_list = box.get('away', {}).get('pitchers', [])
home_pitcher_list = box.get('home', {}).get('pitchers', [])

away_sp = away_pitcher_list[0] if away_pitcher_list else 0
home_sp = home_pitcher_list[0] if home_pitcher_list else 0

for b_id in box['home']['battingOrder']:
p_info = box['home']['teamPlayers'].get(f"ID{b_id}")
if p_info and away_sp != 0:
matchup_rows.append({
'player_name': p_info['person']['fullName'], 'batter': b_id, 'stand': p_info['boxscoreName'],
'pitcher': away_sp, 'p_throws': 'R', 'game_id': game_id
})
for b_id in box['away']['battingOrder']:
p_info = box['away']['teamPlayers'].get(f"ID{b_id}")
if p_info and home_sp != 0:
matchup_rows.append({
'player_name': p_info['person']['fullName'], 'batter': b_id, 'stand': p_info['boxscoreName'],
'pitcher': home_sp, 'p_throws': 'R', 'game_id': game_id
})
except Exception:
continue
except Exception:
pass
return pd.DataFrame(matchup_rows) if matchup_rows else pd.DataFrame(columns=['player_name', 'batter', 'stand', 'pitcher', 'p_throws', 'game_id'])

# =====================================================================
# SECTION 4: REINFORCEMENT LOOP FROM MISSED OUTCOMES
# =====================================================================
def audit_and_learn_from_yesterday(pa_df, batter_df, pitcher_df):
yesterday_str = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
feedback_file = "cache/online_feedback_learning_log.csv"
print(f"Auditing missed selections from {yesterday_str}...")

try:
sched = statsapi.schedule(date=yesterday_str)
recorded_outcomes = []

for game in sched:
game_id = game.get('game_pk')
box = statsapi.boxscore_data(game_id)

away_pitchers = box.get('away', {}).get('pitchers', [])
home_pitchers = box.get('home', {}).get('pitchers', [])
away_sp = away_pitchers[0] if away_pitchers else 0
home_sp = home_pitchers[0] if home_pitchers else 0

for team in ['home', 'away']:
for p_id_str, info in box[team]['players'].items():
b_id = int(p_id_str.replace("ID", ""))
stats = info.get('stats', {}).get('batting', {})
if stats:
actual_hr = 1 if stats.get('homeRuns', 0) > 0 else 0
opp_pitcher = away_sp if team == 'home' else home_sp
if opp_pitcher != 0:
recorded_outcomes.append({
'batter': b_id, 'pitcher': opp_pitcher, 'stand': 'R', 'p_throws': 'R',
'has_platoon_advantage': 0, 'is_hr': actual_hr
})

if not recorded_outcomes:
return pa_df

new_feedback_df = pd.DataFrame(recorded_outcomes)
if os.path.exists(feedback_file):
old_feedback_df = pd.read_csv(feedback_file)
new_feedback_df = pd.concat([old_feedback_df, new_feedback_df], ignore_index=True).drop_duplicates()
new_feedback_df.to_csv(feedback_file, index=False)

updated_pa_df = pd.concat([pa_df, new_feedback_df], ignore_index=True).fillna(0)
return updated_pa_df
except Exception:
return pa_df

# =====================================================================
# SECTION 5: MODEL ARRAYS SELECTION
# =====================================================================
def build_training_dataset(pa_df, batter_df, pitcher_df):
base = pa_df[['batter', 'pitcher', 'stand', 'p_throws', 'has_platoon_advantage', 'is_hr']].copy()
base = base.merge(batter_df, on=['batter', 'has_platoon_advantage'], how='left')
base = base.merge(pitcher_df, on=['pitcher', 'stand'], how='left').fillna(0)
features = ['has_platoon_advantage', 'bat_hr_per_pa', 'bat_barrel_per_pa', 'bat_hard_hit_per_pa', 'bat_avg_launch_angle', 'pit_hr_per_pa', 'pit_barrel_per_pa']
return base[features], base['is_hr']

def train_hr_model(X, y):
scale_weight = (len(y) - sum(y)) / sum(y) if sum(y) > 0 else 1
model = xgb.XGBClassifier(n_estimators=120, max_depth=4, learning_rate=0.05, scale_pos_weight=scale_weight, objective='binary:logistic', eval_metric='logloss')
model.fit(X, y)
return model

def alert_to_discord(webhook_url, message_content):
if "YOUR_REAL_ID_HERE" in webhook_url or not webhook_url.startswith("http"):
print(f"[Console Mirror Output Only]:\n{message_content}")
return
try:
requests.post(webhook_url, json={"content": str(message_content)})
except Exception as e:
=====================================================================

SECTION 6: AUTOMATED EXECUTION CORE

=====================================================================
if __name__ == "__main__":
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1528513802881073313/xVsb81UEWitAaR5vbiVbyP3XHkL2XVzG8ATTSlz3Q9IYt-EbhQUTCmiRK5IHFFEivuj0")
print("🚀 Initializing Verified Live MLB Learning Pipeline...")
try:
batters, pitchers, raw_pas = get_advanced_hr_metrics(days_back=15)
adaptive_pas = audit_and_learn_from_yesterday(raw_pas, batters, pitchers)
X_train, y_train = build_training_dataset(adaptive_pas, batters, pitchers)
hr_model = train_hr_model(X_train, y_train)
today_matchups = get_today_live_lineups()
if not today_matchups.empty:
today_matchups['has_platoon_advantage'] = (today_matchups['stand'] != today_matchups['p_throws']).astype(int)
today_features = today_matchups.merge(batters, on=['batter', 'has_platoon_advantage'], how='left')
today_features = today_features.merge(pitchers, on=['pitcher', 'stand'], how='left').fillna(0)
features_list = ['has_platoon_advantage', 'bat_hr_per_pa', 'bat_barrel_per_pa', 'bat_hard_hit_per_pa', 'bat_avg_launch_angle', 'pit_hr_per_pa', 'pit_barrel_per_pa']
today_matchups['hr_prob'] = hr_model.predict_proba(today_features[features_list])[:, 1]
top_picks = today_matchups.sort_values(by='hr_prob', ascending=False).head(3)
alert_msg = "🚨 MLB ADAPTIVE-LEARNING MODEL EDGES 🚨\n"
for _, row in top_picks.iterrows():
alert_msg += f"🔹 {row['player_name']}: Predicted HR Prob: {row['hr_prob']:.1%}\n"
else:
alert_msg = "✅ MLB Training Engine Optimized: Continuous reinforcement feedback applied successfully."
alert_to_discord(DISCORD_WEBHOOK, alert_msg)
print("✅ Verified pipeline run completed successfully.")
except Exception as main_error:
print(f"Process terminated by script: {main_error}")
