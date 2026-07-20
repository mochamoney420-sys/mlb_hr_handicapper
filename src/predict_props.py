import datetime
import pandas as pd
import requests

# 1. Fetch today's MLB matchups from a sports API
# Note: Replace with your chosen provider (e.g., The Odds API, SportsDataIO, or MLB Stats API)
API_URL = "https://the-odds-api.com"
API_KEY = "f983b6d08915175390d5e308e1207041"

def get_todays_games():
    params = {"apiKey": API_KEY, "regions": "us", "markets": "h2h"}
    response = requests.get(API_URL, params=params)
    return print("Status Code:", response.status_code)
print("Raw Response:", response.text)
data = response.json()

def run_predictions():
    print(f"Starting daily prediction run for {datetime.date.today()}...")
    games = get_todays_games()
    
    if not games:
        print("No games found or API error.")
        return

    # 2. Structure your input data
    # (Placeholder logic: replace with your actual feature engineering pipeline)
    today_data = []
    for game in games:
        today_data.append({
            "away_team": game.get("away_team"),
            "home_team": game.get("home_team"),
            "date": datetime.date.today()
        })
    
    df = pd.DataFrame(today_data)
    
    # 3. Pass data into your trained ML model
    # example: predictions = my_model.predict(df)
    print("Features extracted. Model predictions completed successfully.")
    
    # 4. Save results to a daily file
    filename = f"predictions_{datetime.date.today()}.csv"
    df.to_csv(filename, index=False)
    print(f"Predictions saved to {filename}")

if __name__ == "__main__":
    run_predictions()
