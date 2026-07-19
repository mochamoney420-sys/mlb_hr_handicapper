# src/model.py
import xgboost as xgb
from sklearn.model_selection import train_test_split

def train_hr_model(model_data):
    """
    Trains an XGBoost model using historical data to predict 
    the probability of a player hitting a home run.
    """
    print("Training XGBoost Home Run prediction model...")
    
    # 1. Select the specific high-density features for prediction
    features = [
        'batter_barrel_rate', 'batter_hard_hit_rate', 'batter_fb_rate', 'batter_pull_rate',
        'pitcher_barrel_rate_allowed', 'pitcher_fb_rate_allowed', 
        'park_factor_hr', 'temperature', 'wind_speed_outward'
    ]

    X = model_data[features]
    y = model_data['hit_home_run'] # Binary target: 1 = Home Run, 0 = No Home Run

    # 2. Split your data into a training set and a testing set (80/20 split)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    # 3. Initialize and configure the XGBoost Classifier
    hr_model = xgb.XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.05,
        scale_pos_weight=25, # Counteracts the rarity of HR events (1 out of ~25 plate appearances)
        objective='binary:logistic'
    )
    
    # 4. Train the model on your dataset
    hr_model.fit(X_train, y_train)
    
    print("Model training complete!")
    return hr_model
# ... (rest of your code above stays the same)

    # 4. Train the model on your dataset
    hr_model.fit(X_train, y_train)
    
    # ADD THESE LINES HERE TO SAVE IT:
    import os
    os.makedirs('data', exist_ok=True)
    hr_model.save_model("data/latest_hr_model.json")
    
    print("Model training complete and saved to data/latest_hr_model.json!")
    return hr_model
