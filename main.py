import os
import time
from datetime import datetime, timezone
from threading import Thread
import matplotlib

matplotlib.use('Agg')
import mplfinance as mpf
import pandas as pd
from flask import Flask
from google import genai
from telegram import Bot, InputMediaPhoto
import requests
import asyncio

# Load Environment Variables
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 1. Flask Web Server (Satisfies Render's Port Requirement) ---
app = Flask(__name__)


@app.route("/")
def home():
  return (
      "XAU/USD Bot Web Service is running and checking markets every 15"
      " minutes!"
  )


def run_flask():
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)


# --- 2. Bot Logic & Market Verification ---
def is_market_open():
  try:
    url = f"https://api.twelvedata.com/market_state?apikey={TWELVE_DATA_API_KEY}"
    response = requests.get(url).json()
    for item in response.get("forex", []):
      if item.get("symbol") == "XAU/USD":
        return item.get("is_open", False)
    now = datetime.now(timezone.utc)
    if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 22):
      return False
    if now.weekday() == 4 and now.hour >= 21:
      return False
    return True
  except Exception:
    now = datetime.now(timezone.utc)
    if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 22):
      return False
    return True


def fetch_chart_data(interval):
  url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={interval}&outputsize=30&apikey={TWELVE_DATA_API_KEY}"
  data = requests.get(url).json()
  if "values" not in data:
    raise ValueError(f"API Error for {interval}: {data}")
  df = pd.DataFrame(data["values"])
  df["datetime"] = pd.to_datetime(df["datetime"])
  df.set_index("datetime", inplace=True)
  df = df.astype(
      {"open": float, "high": float, "low": float, "close": float, "volume": float}
  )
  return df.iloc[::-1]


async def send_telegram_startup():
  try:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="🚀 XAU/USD Bot Web Service has successfully started and is running!",
    )
  except Exception as e:
    print("Failed to send startup message:", e)


def run_bot_task():
  if not is_market_open():
    print("Market is closed. Skipping execution.")
    return

  print("Market is open. Generating charts...")
  df_1m = fetch_chart_data("1min")
  df_15m = fetch_chart_data("15min")

  mpf.plot(
      df_1m,
      type="candle",
      style="charles",
      savefig="chart_1m.png",
      title="XAU/USD 1m",
      axisoff=True,
  )
  mpf.plot(
      df_15m,
      type="candle",
      style="charles",
      savefig="chart_15m.png",
      title="XAU/USD 15m",
      axisoff=True,
  )

  client = genai.Client(api_key=GEMINI_API_KEY)
  image_1m = client.files.upload(file="chart_1m.png")
  image_15m = client.files.upload(file="chart_15m.png")

  prompt = (
      "Analyze these XAU/USD 1-minute and 15-minute charts. Provide a precise,"
      " actionable 50-word market summary paragraph outlining current trends,"
      " key levels, and outlook."
  )

  response = client.models.generate_content(
      model="gemini-2.5-flash", contents=[image_1m, image_15m, prompt]
  )

  async def send_messages():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    async with bot:
      await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=response.text)
      with open("chart_1m.png", "rb") as f1, open("chart_15m.png", "rb") as f2:
        await bot.send_media_group(
            chat_id=TELEGRAM_CHAT_ID,
            media=[
                InputMediaPhoto(media=f1, caption="1-Minute Chart"),
                InputMediaPhoto(media=f2, caption="15-Minute Chart"),
            ],
        )

  asyncio.run(send_messages())


def background_scheduler():
  while True:
    try:
      run_bot_task()
    except Exception as e:
      print("Error in background task:", e)
    time.sleep(900)  # Sleep for 15 minutes


if __name__ == "__main__":
  # Send startup confirmation message asynchronously
  asyncio.run(send_telegram_startup())

  # Start Flask web server in background thread for Render
  flask_thread = Thread(target=run_flask)
  flask_thread.start()

  # Start bot loop in background thread
  bot_thread = Thread(target=background_scheduler)
  bot_thread.start()
