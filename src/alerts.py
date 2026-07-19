import requests
import os

def alert_discord(bet_message):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    payload = {"content": f"🚨 **Value HR Prop Found!** 🚨\n{bet_message}"}
    requests.post(webhook_url, json=payload)

# Calculation checking for a market betting edge
model_prob = 0.28   # 28% calculated chance to hit HR
market_prob = 0.20  # Bookmaker implies 20% (+400 odds)

if model_prob > market_prob:
    message = f"Player: Shohei Ohtani\nModel Prob: {model_prob:.2%}\nMarket Implied: {market_prob:.2%}\nEdge: +8.0%"
    alert_discord(message)
