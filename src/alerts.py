
import requests
import os

def alert_discord(bet_message):
    webhook_url = os.getenv("DISCORD_MLB_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("Error: DISCORD_MLB_WEBHOOK or DISCORD_WEBHOOK_URL environment variable is missing!")
        return

    payload = {"content": f"🚨 **Value HR Prop Found!** 🚨\n{bet_message}"}
    response = requests.post(webhook_url, json=payload)
    if response.status_code == 204:
        print("Alert successfully sent to Discord!")
    else:
        print(f"Failed to send alert. Status code: {response.status_code}")