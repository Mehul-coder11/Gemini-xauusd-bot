import os
import time
import traceback
from datetime import datetime, timezone
from threading import Thread
import matplotlib

matplotlib.use('Agg')
import mplfinance as mpf
import pandas as pd
from flask import Flask
from google import genai
import requests
import re

# Load Environment Variables
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Virtual Tracker Storage
active_virtual_trades = []

# --- 1. Flask Web Server ---
app = Flask(__name__)


@app.route("/")
def home():
  return "OK"


@app.route("/run")
def trigger_run():
  try:
    Thread(target=run_bot_task).start()
    return "Bot task triggered successfully!", 200
  except Exception as e:
    print("Error triggering task:")
    traceback.print_exc()
    return f"Error triggering task: {str(e)}", 500


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


def track_virtual_trades(df):
  global active_virtual_trades
  if not active_virtual_trades:
    return

  remaining_trades = []
  for trade in active_virtual_trades:
    hit = False
    for index, candle in df.iterrows():
      high = candle['high']
      low = candle['low']

      if trade['type'] == 'BUY':
        if high >= trade['tp']:
          send_telegram_message(f"✅ Virtual BUY Target Hit! TP: {trade['tp']}")
          hit = True
          break
        elif low <= trade['sl']:
          send_telegram_message(f"❌ Virtual BUY Stop Loss Hit! SL: {trade['sl']}")
          hit = True
          break
      elif trade['type'] == 'SELL':
        if low <= trade['tp']:
          send_telegram_message(f"✅ Virtual SELL Target Hit! TP: {trade['tp']}")
          hit = True
          break
        elif high >= trade['sl']:
          send_telegram_message(f"❌ Virtual SELL Stop Loss Hit! SL: {trade['sl']}")
          hit = True
          break

    if not hit:
      remaining_trades.append(trade)

  active_virtual_trades = remaining_trades


def parse_trade_from_text(text):
  if "no trade" in text.lower():
    return None
  t_type = "BUY" if "buy" in text.lower() else "SELL" if "sell" in text.lower() else None
  if not t_type:
    return None

  tp_match = re.search(r'(?:tp|take profit)[:\s]*([\d.]+)', text, re.IGNORECASE)
  sl_match = re.search(r'(?:sl|stop loss)[:\s]*([\d.]+)', text, re.IGNORECASE)

  if tp_match and sl_match:
    try:
      return {
          'type': t_type,
          'tp': float(tp_match.group(1)),
          'sl': float(sl_match.group(1))
      }
    except ValueError:
      pass
  return None


def run_bot_task():
  print("Generating charts...")
  df_1m = fetch_chart_data("1min")
  df_15m = fetch_chart_data("15min")

  # Track existing virtual trades against latest 1m candles
  track_virtual_trades(df_1m)

  mpf.plot(
      df_1m,
      type="candle",
      style="charles",
      savefig="chart_1m.png",
      title="XAU/USD 1m",
  )
  mpf.plot(
      df_15m,
      type="candle",
      style="charles",
      savefig="chart_15m.png",
      title="XAU/USD 15m",
  )

  client = genai.Client(api_key=GEMINI_API_KEY)
  image_1m = client.files.upload(file="chart_1m.png")
  image_15m = client.files.upload(file="chart_15m.png")

  prompt = (
      "Assume you are a professional and this is the data of live XAU/USD, "
      "with 1 minute chart and 15 minute chart. Read the current price directly "
      "from the price axis and latest candles on the chart (do not use generic "
      "estimates or old data). Analyze it carefully and give me a trade in XAU/USD "
      "only when you are completely sure; otherwise say "
      "no trade. When there is a trade, tell what to do like buy or sell, state "
      "the exact current price shown on the chart, at what price to enter the trade, "
      "and what should be the take profit and SL. Make sure that you only give "
      "trades in which the TP is double than the SL, and for this also make sure "
      "that only give TP which will be achieved before today market close and that "
      "the given trade should not hit the SL, also keep in mind that I don't want "
      "logic only give me either the trade or no trade"
  )

  response = client.models.generate_content(
      model="gemini-2.5-flash", contents=[image_1m, image_15m, prompt]
  )

  print("AI Response:", response.text[:200])

  # Check if AI gave a new trade to track
  new_trade = parse_trade_from_text(response.text)
  if new_trade:
    active_virtual_trades.append(new_trade)
    send_telegram_message(f"📝 Virtual Trade Logged: {new_trade['type']} | TP: {new_trade['tp']} | SL: {new_trade['sl']}")

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
      traceback.print_exc()
    time.sleep(900)


if __name__ == "__main__":
  send_telegram_message(
      "🚀 XAU/USD Bot Web Service has successfully started and is running!"
  )

  flask_thread = Thread(target=run_flask)
  flask_thread.start()

  bot_thread = Thread(target=background_scheduler)
  bot_thread.start()
