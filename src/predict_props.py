import datetime
import json
import os
from pathlib import Path

import pandas as pd
import requests

# This script is intentionally resilient: the app should never crash just because
# the live betting API is down, invalid, or returning the wrong market contract.
API_PROVIDER = (os.getenv("ODDS_API_PROVIDER") or os.getenv("SHARP_API_PROVIDER") or "sharpapi").strip().lower()
API_URL = os.getenv("ODDS_API_PROVIDER_URL")
if API_PROVIDER in {"sharpapi", "sharp_api", "sharp-api"}:
    API_URL = API_URL or "https://api.sharpapi.io/api/v1/odds"
else:
    API_URL = API_URL or "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
API_KEY = (
    os.getenv("SHARP_API_KEY")
    or os.getenv("SHARPAPI_API_KEY")
    or os.getenv("ODDS_API_KEY")
)


def _load_local_fallback_rows():
    """Return rows from the project snapshot/cache files when live odds are unavailable."""
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    candidates = [
        data_dir / "latest_hr_prop_odds_cache.json",
        data_dir / "hr_prop_odds.json",
        data_dir / "free_odds.json",
        data_dir / "odds.json",
        data_dir / "market_odds.json",
    ]

    snapshot_files = sorted(data_dir.glob("odds_snapshots_*.jsonl"), reverse=True)
    candidates.extend(snapshot_files)

    for candidate in candidates:
        if not candidate.exists():
            continue

        try:
            if candidate.suffix == ".jsonl":
                lines = candidate.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    payload = record.get("odds") if isinstance(record, dict) else None
                    if isinstance(payload, dict) and payload:
                        return payload
            else:
                payload = json.loads(candidate.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(payload, dict):
                    if payload.get("raw"):
                        return payload["raw"]
                    if payload.get("odds"):
                        return payload["odds"]
                    if payload:
                        return payload
        except Exception:
            continue

    return []


def _normalize_payload(payload):
    """Normalize supported odds payload shapes into a list of rows."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    for key in ("data", "games", "results", "events"):
        value = payload.get(key)
        if isinstance(value, list):
            return value

    if "rows" in payload and isinstance(payload["rows"], list):
        return payload["rows"]

    if all(isinstance(v, dict) for v in payload.values()):
        rows = []
        for player, book_map in payload.items():
            if isinstance(book_map, dict):
                for book, odds in book_map.items():
                    rows.append({
                        "player": player,
                        "book": book,
                        "odds": odds,
                    })
        return rows

    return [payload]


def get_todays_games():
    if API_KEY:
        try:
            if API_PROVIDER in {"sharpapi", "sharp_api", "sharp-api"}:
                headers = {"X-API-Key": str(API_KEY).strip()}
                params = {
                    "sport": "baseball",
                    "league": "MLB",
                    "market": "player_home_runs",
                    "is_main_line": "true",
                    "is_active": "true",
                    "limit": "200",
                }
                response = requests.get(API_URL, headers=headers, params=params, timeout=20)
                print("Status Code:", response.status_code)
                if response.ok:
                    raw_text = response.text.strip()
                    if raw_text:
                        payload = response.json()
                        normalized = _normalize_payload(payload.get("data") if isinstance(payload, dict) else payload)
                        if normalized:
                            print("Using live SharpAPI key for MLB HR props.")
                            return normalized
                        print("SharpAPI returned no usable row data; falling back to local snapshot.")
                    else:
                        print("Empty SharpAPI response body; falling back to local snapshot.")
                else:
                    body = (response.text or "")[:300]
                    print(f"Failed to fetch SharpAPI games: HTTP {response.status_code}")
                    print(body)
                    print("Falling back to local snapshot.")
            else:
                params = {
                    "apiKey": API_KEY,
                    "regions": "us",
                    "markets": "h2h",
                    "oddsFormat": "american",
                    "dateFormat": "iso",
                }
                response = requests.get(API_URL, params=params, timeout=20)
                print("Status Code:", response.status_code)
                if response.ok:
                    raw_text = response.text.strip()
                    if raw_text:
                        payload = response.json()
                        normalized = _normalize_payload(payload)
                        if normalized:
                            print("Using live odds API with configured key.")
                            return normalized
                        print("Live API returned no usable game rows; falling back to local snapshot.")
                    else:
                        print("Empty API response body; falling back to local snapshot.")
                else:
                    body = (response.text or "")[:300]
                    print(f"Failed to fetch games: HTTP {response.status_code}")
                    print(body)
                    print("Falling back to local snapshot.")
        except ValueError as exc:
            print(f"Malformed or non-JSON API response: {exc}")
            print("Falling back to local snapshot.")
        except requests.RequestException as exc:
            print(f"Network error while fetching MLB props: {exc}")
            print("Falling back to local snapshot.")
        except Exception as exc:
            print(f"Unexpected error while fetching MLB props: {exc}")
            print("Falling back to local snapshot.")

    fallback_rows = _load_local_fallback_rows()
    fallback_payload = _normalize_payload(fallback_rows)
    if fallback_payload:
        print("Using local fallback odds snapshot instead of live API.")
        return fallback_payload

    if not API_KEY:
        print("Missing SharpAPI / ODDS_API_KEY in environment and no local odds snapshot available; refusing to call the live provider.")
    return []


def run_predictions():
    print(f"Starting daily prediction run for {datetime.date.today()}...")
    games = get_todays_games()

    if not games:
        print("No usable odds data found; exiting cleanly.")
        return

    today_data = []
    for game in games:
        if not isinstance(game, dict):
            continue

        if "away_team" in game or "home_team" in game:
            today_data.append({
                "away_team": game.get("away_team"),
                "home_team": game.get("home_team"),
                "date": datetime.date.today(),
                "source": "live_odds",
            })
        else:
            today_data.append({
                "player": game.get("player") or game.get("name") or game.get("batter"),
                "book": game.get("book") or game.get("sportsbook") or "unknown",
                "odds": game.get("odds") or game.get("price") or game.get("american"),
                "date": datetime.date.today(),
                "source": "player_odds",
            })

    if not today_data:
        print("No rows were usable after normalizing the odds payload.")
        return

    df = pd.DataFrame(today_data)
    print(f"Features extracted: {len(df)} rows")

    filename = f"predictions_{datetime.date.today()}.csv"
    df.to_csv(filename, index=False)
    print(f"Predictions saved to {filename}")


if __name__ == "__main__":
    run_predictions()
