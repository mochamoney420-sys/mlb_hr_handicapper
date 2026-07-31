import pandas as pd
from src.advanced_math import calculate_platoon_cluster_prob

# Example local data layer: aggregated cluster observations
# Columns expected: batter_id, pitcher_id, batter_hand, pitcher_hand, pa, hr
cluster_df = pd.DataFrame([
    {"batter_id": 1001, "pitcher_id": 2001, "batter_hand": "R", "pitcher_hand": "R", "pa": 600, "hr": 14},
    {"batter_id": 1001, "pitcher_id": 2001, "batter_hand": "R", "pitcher_hand": "L", "pa": 500, "hr": 13},
    {"batter_id": 1001, "pitcher_id": 2001, "batter_hand": "L", "pitcher_hand": "R", "pa": 550, "hr": 11},
    {"batter_id": 1001, "pitcher_id": 2001, "batter_hand": "L", "pitcher_hand": "L", "pa": 520, "hr": 10},
], columns=["batter_id", "pitcher_id", "batter_hand", "pitcher_hand", "pa", "hr"])

prob = calculate_platoon_cluster_prob(
    cluster_df,
    batter_id=1001,
    pitcher_id=2001,
    batter_hand="R",
    pitcher_hand="R",
    league_hr_pa=0.031,
    min_pa=500,
    projected_pa=4.1,
    park_factor_hr=1.20,
    temperature=82.0,
    wind_speed=12.0,
    wind_direction="outward",
)

print(f"Cluster-based HR prop probability: {prob:.4f}")
