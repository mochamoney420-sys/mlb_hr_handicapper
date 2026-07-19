# test_pipeline.py
import pandas as pd
from src.model import train_hr_model
from src.alerts import alert_discord

if __name__ == "__main__":
    print("🤖 Simulating 2025-2026 Statcast Data for Quick Test...")
    
    # Generate 50 rows of dummy data matching the feature list format
    mock_data = pd.DataFrame({
        'batter_barrel_rate': [0.15] * 50,
        'batter_hard_hit_rate': [0.45] * 50,
        'batter_fb_rate': [0.38] * 50,
        'batter_pull_rate': [0.41] * 50,
        'pitcher_barrel_rate_allowed': [0.12] * 50,
        'pitcher_fb_rate_allowed': [0.44] * 50,
        'park_factor_hr': [105] * 50,
        'temperature': [82.0] * 50,
        'wind_speed_outward': [12.5] * 50,
        'hit_home_run': [0, 1] * 25  # Alternate 0 and 1 targets to satisfy XGBoost criteria
    })
    
    # Train model using mock dataset
    trained_model = train_hr_model(mock_data)
    
    # Simulate a value bet trigger condition
    test_message = (
        "Player: Shohei Ohtani\n"
        "Model Prob: 28.50%\n"
        "Market Implied: 20.00% (+400)\n"
        "Edge: +8.50% (SYSTEM TEST SUCCESSFUL)"
    )
    
    # Direct notification fire test
    print("📤 Dispatching notification request to Discord...")
    alert_discord(test_message)
