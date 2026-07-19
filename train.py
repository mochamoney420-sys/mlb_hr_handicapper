## CORRECT
import datetime
from pathlib import Path
import importlib.util

import xgboost as xgb

def import_module_from_file(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

project_root = Path(__file__).resolve().parent
src_dir = project_root / "src"

scraper = import_module_from_file("src.scraper", src_dir / "scraper.py")
model = import_module_from_file("src.model", src_dir / "model.py")

get_daily_statcast_baselines = scraper.get_daily_statcast_baselines
train_hr_model = model.train_hr_model

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
