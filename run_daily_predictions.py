# run_daily_predictions.py
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.scraper import get_daily_statcast_baselines

if __name__ == "__main__":
    raw_data = get_daily_statcast_baselines(start_date="2025-04-01", end_date="2026-10-01")