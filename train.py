# run_daily_predictions.py
import sys
import datetime
from pathlib import Path

# Fix python paths before importing local modules
sys.path.append(str(Path(__file__).resolve().parent))

import xgboost as xgb
from src.scraper import get_daily_statcast_baselines
from src.model import train_hr_model

if __name__ == "__main__":
    today = datetime.datetime.today().weekday()
    model_path = Path("data/latest_hr_model.json")
    
    if today == 0 or not model_path.exists():
        print("📅 Scheduled Retrain: Scraping fresh data and training model...")
        raw_data = get_daily_statcast_baselines(start_date="2025-04-01", end_date="2026-10-01")
        hr_model = train_hr_model(raw_data)
    else:
        print("🤖 Loading existing model from local file...")
        hr_model = xgb.XGBClassifier()
        hr_model.load_model(str(model_path))
        
    print("🚀 Model is ready for today's predictions!")
