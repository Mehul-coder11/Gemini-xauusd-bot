import os
import io
import threading
import requests
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import mplfinance as mpf
from flask import Flask, request
from google import genai
from google.genai import types
 
app = Flask(__name__)
 
# Fetch configuration strictly from Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

PROMPT_TEXT = (
    "Assume you are a professional intraday trader and this is the data of live xauusd, with 1 minute chart "
    "and 15 minute chart please analyse it carefully ,and guve me a trade in xauusd but guve trade only when "
    "you have confidence of 60 to 70 percent or more otherwise say no trade, and when there is a trade then "
    "also tell what to do like buy or sell and what is the current price and at what price to enter the trade "
    "and what should be the take profit and sl, always keep risk reward ratio to 1 ratio 2 which means to "
    "should be double of sl and teh and only guve trades in which sl should not be more than 4 dollars and "
    "the trades in which has a minimum tp of 4 dollars or more, only guve intraday trades and only guve tp "
    "which will be achieved surely before today market close, and your main objective is to give profitable "
    "trades and grow the urers capital and the give him net gain. "
    "Don't guve me reasons of trade or your analysis , just give me what is the current price and if there is a "
    "trade then give me only trade decision like entry price and direction of trade and take profit and stop loss, "
    "make sure that it should reach its tp before today market close"
)

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Error: Missing Telegram configuration.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        res = requests.post(url, json=payload, timeout=10)
        print("Telegram Response:", res.json())
    except Exception as e:
        print("Telegram Exception:", e)

def fetch_chart_data(interval):
    url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={interval}&outputsize=50&apikey={TWELVE_DATA_API_KEY}"
    res = requests.get(url, timeout=10).json()
    
    if "values" not in res:
        print(f"Error fetching {interval} data:", res)
        return pd.DataFrame()

    data = []
    for candle in reversed(res["values"]):
        data.append({
            "datetime": pd.to_datetime(candle["datetime"]),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume", 100))
        })
    df = pd.DataFrame(data)
    df.set_index("datetime", inplace=True)
    return df

def generate_chart_bytes(df, title):
    buf = io.BytesIO()
    mpf.plot(df, type='candle', style='charles', volume=True, title=title, savefig=dict(fname=buf, format='png', dpi=100))
    buf.seek(0)
    return buf.read()

def process_and_analyze():
    df_1m = fetch_chart_data("1min")
    df_15m = fetch_chart_data("15min")

    if df_1m.empty or df_15m.empty:
        send_telegram_message("Error: Unable to fetch candle data from Twelve Data API.")
        return

    current_price = df_1m.iloc[-1]['close']
    chart_1m_bytes = generate_chart_bytes(df_1m, "XAUUSD - 1 Min")
    chart_15m_bytes = generate_chart_bytes(df_15m, "XAUUSD - 15 Min")

    full_prompt = f"Live Price: {current_price}\n\n" + PROMPT_TEXT

    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[
                full_prompt,
                types.Part.from_bytes(data=chart_1m_bytes, mime_type="image/png"),
                types.Part.from_bytes(data=chart_15m_bytes, mime_type="image/png")
            ]
        )
        gemini_reply = response.text.strip()
        send_telegram_message(gemini_reply)
    except Exception as e:
        print("Gemini API Error:", e)
        send_telegram_message(f"Error generating analysis: {str(e)}")

def notify_startup():
    send_telegram_message("🚀 XAUUSD Analysis Bot has been started and is ready for triggers!")

# Send start message as soon as the script loads
notify_startup()

@app.route("/")
def home():
    return "XAUUSD Bot is Running", 200

@app.route("/run")
def trigger_run():
    # Run in background thread to return 200 OK instantly
    thread = threading.Thread(target=process_and_analyze)
    thread.start()
    return "Job started", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    thread = threading.Thread(target=process_and_analyze)
    thread.start()
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
