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
import requests

# Load Environment Variables
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- 1. Flask Web Server ---
app = Flask(__name__)


@app.route("/")
def home():
  return "OK"


@app.route("/run")
def trigger_run():
  try:
    run_bot_task()
    return "Bot task executed successfully!", 200
  except Exception as e:
    return f"Error executing task: {str(e)}", 500


def run_flask():
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)


# --- 2. Market & Bot Logic ---
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

  cols_to_float = ["open", "high", "low", "close"]
  if "volume" in df.columns:
    cols_to_float.append("volume")
  for col in cols_to_float:
    df[col] = df[col].astype(float)

  return df.iloc[::-1]


def send_telegram_message(text):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
  requests.post(url, json=payload)


def send_telegram_photos(caption1, caption2):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
  files = {
      "photo1": open("chart_1m.png", "rb"),
      "photo2": open("chart_15m.png", "rb"),
  }
  media = [
      {"type": "photo", "media": "attach://photo1", "caption": caption1},
      {"type": "photo", "media": "attach://photo2", "caption": caption2},
  ]
  data = {"chat_id": TELEGRAM_CHAT_ID, "media": str(media).replace("'", '"')}
  requests.post(url, data=data, files=files)


def run_bot_task():
  print("Generating charts...")
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

  send_telegram_message(response.text)
  send_telegram_photos("1-Minute Chart", "15-Minute Chart")
  print("Task executed and sent successfully!")


def background_scheduler():
  while True:
    try:
      if is_market_open():
        run_bot_task()
      else:
        print("Market is closed. Skipping execution.")
    except Exception as e:
      print("Error in background task:", e)
    time.sleep(900)


if __name__ == "__main__":
  send_telegram_message(
      "🚀 XAU/USD Bot Web Service has successfully started and is running!"
  )

  flask_thread = Thread(target=run_flask)
  flask_thread.start()

  bot_thread = Thread(target=background_scheduler)
  bot_thread.start()
