# run_daily_predictions.py
import datetime
import os
import xgboost as xgb
from src.scraper import get_daily_statcast_baselines
from src.model import train_hr_model

if __name__ == "__main__":
    # Check if today is Monday (0 = Monday, 1 = Tuesday... 6 = Sunday)
    today = datetime.datetime.today().weekday()
    model_path = "data/latest_hr_model.json"
    
    # If it is Monday OR we don't have a saved model yet, trigger a full retrain
    if today == 0 or not os.path.exists(model_path):
        print("📅 Scheduled Retrain: Scraping fresh data and training model...")
        raw_data = get_daily_statcast_baselines(start_date="2025-04-01", end_date="2026-10-01")
        hr_model = train_hr_model(raw_data)
    else:
        print("🤖 Loading existing model from local file...")
        hr_model = xgb.XGBClassifier()
        hr_model.load_model(model_path)
        
    print("🚀 Model is ready for today's predictions!")

