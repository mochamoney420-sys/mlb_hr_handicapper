# src/backtester.py
import pandas as pd
from sklearn.metrics import log_loss, brier_score_loss

def audit_daily_predictions(predictions_df, real_results_df):
    """
    Compares model probabilities against actual outcomes to track accuracy.
    """
    # Calculate Brier Score (closer to 0.0 means perfect probability calibration)
    brier_score = brier_score_loss(real_results_df['hit_home_run'], predictions_df['model_prob'])
    print(f"Daily Model Brier Score: {brier_score:.4f}")
    
    # Alert your Discord if the model's accuracy drops (indicating data drift)
    if brier_score > 0.15:
         print("Warning: Model accuracy is drifting. Retraining recommended.")
         
    return brier_score
