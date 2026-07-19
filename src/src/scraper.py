import os
import pandas as pd
from pybaseball import statcast

def get_daily_statcast_baselines(start_date="2025-04-01", end_date="2026-10-01"):
    print(f"Downloading Statcast data from {start_date} to {end_date}...")
    
    # Download the data using pybaseball
    df = statcast(start_dt=start_date, end_dt=end_date)
    
    # Ensure the data directory exists
    os.makedirs('data', exist_ok=True)
    
    # Save it as a CSV file so you don't have to re-download it every time
    df.to_csv('data/raw_statcast_data.csv', index=False)
    print("Data saved successfully to data/raw_statcast_data.csv")
    
    return df
