# TOP OF THE FILE
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent))

# NOW your local imports will never break!
root_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(root_dir / "src"))

from scraper import get_daily_statcast_baselines
from model import train_hr_model

# run_daily_predictions.py
import datetime
import xgboost as xgb

if __name__ == "__main__":
    # Check if today is Monday (0 = Monday, 1 = Tuesday... 6 = Sunday)
    today = datetime.datetime.today().weekday()
    
    # Define file path using pathlib
    model_path = Path("data/latest_hr_model.json")
    
    # Corrected if statement using .exists()
    if today == 0 or not model_path.exists():
        print("📅 Scheduled Retrain: Scraping fresh data and training model...")
        raw_data = get_daily_statcast_baselines(start_date="2025-04-01", end_date="2026-10-01")
        hr_model = train_hr_model(raw_data)
    else:
        print("🤖 Loading existing model from local file...")
        hr_model = xgb.XGBClassifier()
        # Convert Path object back to string format for XGBoost compatibility
        hr_model.load_model(str(model_path))
        
    print("🚀 Model is ready for today's predictions!")

{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python: Current File",
      "type": "python",
      "request": "launch",
      "program": "${file}",
      "env": {
        "PYTHONPATH": "${workspaceFolder}"
      }
    }
  ]
}
