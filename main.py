import os
from datetime import datetime
import requests
import matplotlib

matplotlib.use('Agg')  # Non-interactive backend to save PNGs securely
import mplfinance as mpf
import pandas as pd
from google import genai
from telegram import Bot

# Load Environment Variables securely from Render Dashboard
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def is_market_open():
  """Checks Twelve Data market status or relies on UTC Forex Schedule (Sun 22:00 - Fri 21:00 UTC)."""
  try:
    url = f"https://api.twelvedata.com/market_state?apikey={TWELVE_DATA_API_KEY}"
    response = requests.get(url).json()
    # Find XAU/USD or global forex status
    for item in response.get("forex", []):
      if item.get("symbol") == "XAU/USD":
        return item.get("is_open", False)
    # Fallback to general check if specific symbol isn't listed
    now = datetime.utcnow()
    weekday = now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    hour = now.hour
    if weekday == 5:
      return False  # Saturday closed
    if weekday == 6 and hour < 22:
      return False  # Sunday before open
    if weekday == 4 and hour >= 21:
      return False  # Friday after close
    return True
  except Exception as e:
    print(
        "Error checking market status via API, falling back to time check:", e
    )
    # Basic fallback time check
    now = datetime.utcnow()
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
  return df.iloc[::-1]  # Reverse to chronological order


def generate_pngs():
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


def main():
  if not is_market_open():
    print("Market is closed. Skipping execution to save resources/limits.")
    return

  print("Market is open. Generating charts...")
  generate_pngs()

  # Initialize Gemini Client (Google GenAI SDK)
  client = genai.Client(api_key=GEMINI_API_KEY)

  # Upload or open local images for Gemini
  image_1m = client.files.upload(file="chart_1m.png")
  image_15m = client.files.upload(file="chart_15m.png")

  prompt = (
      "Analyze these XAU/USD 1-minute and 15-minute charts. Provide a precise, "
      "actionable 50-word market summary paragraph outlining current trends, key levels, and outlook."
  )

  response = client.models.generate_content(
      model="gemini-2.5-flash", contents=[image_1m, image_15m, prompt]
  )

  analysis_text = response.text
  print("Gemini Response generated successfully.")

  # Send to Telegram Bot
  bot = Bot(token=TELEGRAM_BOT_TOKEN)
  bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=analysis_text)
  with open("chart_1m.png", "rb") as f1, open("chart_15m.png", "rb") as f2:
    bot.send_media_group(
        chat_id=TELEGRAM_CHAT_ID,
        media=[
            InputMediaPhoto(media=f1, caption="1-Minute Chart"),
            InputMediaPhoto(media=f2, caption="15-Minute Chart"),
        ],
    )


if __name__ == "__main__":
  main()
