
import requests
import os

def alert_discord(bet_message):
    webhook_url = os.environ.get("https://discord.com/api/webhooks/1525525618861543654/jBzZ7vTarJs-j2apC7Ws2M29cF5aaJ9-0JkvdyyK9aJUJRziU9MXqfHyzx0roW4HVHIZ")
    if not webhook_url:
        print("Error: DISCORD_WEBHOOK_URL environment variable is missing!")
        return

    payload = {"content": f"🚨 **Value HR Prop Found!** 🚨\n{bet_message}"}
    response = requests.post(webhook_url, json=payload)
    if response.status_code == 204:
        print("Alert successfully sent to Discord!")
    else:
        print(f"Failed to send alert. Status code: {response.status_code}")