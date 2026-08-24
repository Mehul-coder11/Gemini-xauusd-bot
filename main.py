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

# Refined prompt with updated first line and dual-direction rules
PROMPT_BASE = (
    "Assume you are world's best intraday trader in XAU/USD (Gold). "
    "Analyze the provided live price data, key technical indicators, and attached 1-minute and 5-minute chart images with maximum precision.\n\n"
    "TRADE EVALUATION INSTRUCTIONS:\n"
    "1. You are fully authorized and expected to take BOTH BUY AND SELL TRADES depending on real-time market structure.\n"
    "2. Evaluate market trend, momentum, support/resistance, RSI, and candle patterns objectively across both timeframes.\n"
    "3. If the market is in a downtrend or rejecting resistance, generate a SELL trade setup.\n"
    "4. If the market is in an uptrend or bouncing off support, generate a BUY trade setup.\n\n"
    "STRICT OUTPUT RULES:\n"
    "1. Do NOT write explanations, reasoning, commentary, or market condition descriptions under any circumstances.\n"
    "2. If there is NO viable trade setup, reply ONLY with: NO TRADE.\n"
    "3. If there IS a viable trade setup (aiming for 1:1.5 or 1:2 R:R), reply ONLY using this exact format:\n"
    "Direction: [BUY/SELL]\n"
    "Entry: [Price]\n"
    "SL: [Price]\n"
    "TP: [Price]"
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
    url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={interval}&outputsize=100&apikey={TWELVE_DATA_API_KEY}"
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

def compute_indicators(df):
    """Calculates key technical metrics to feed Gemini alongside charts."""
    if len(df) < 20:
        return {}
    
    close = df['close']
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    
    # RSI calculation
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs.iloc[-1])) if loss.iloc[-1] != 0 else 50
    
    return {
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "rsi": round(rsi, 2),
        "recent_high": round(df['high'].max(), 2),
        "recent_low": round(df['low'].min(), 2)
    }

def generate_chart_bytes(df, title):
    buf = io.BytesIO()
    plot_df = df.tail(50)
    mpf.plot(plot_df, type='candle', style='charles', volume=True, title=title, savefig=dict(fname=buf, format='png', dpi=100))
    buf.seek(0)
    return buf.read()

def process_and_analyze():
    df_1m = fetch_chart_data("1min")
    df_5m = fetch_chart_data("5min")

    if df_1m.empty or df_5m.empty:
        send_telegram_message("Error: Unable to fetch candle data from Twelve Data API.")
        return

    current_price = df_1m.iloc[-1]['close']
    ind_5m = compute_indicators(df_5m)

    # Generate visual PNG bytes for 1-minute and 5-minute charts
    chart_1m_bytes = generate_chart_bytes(df_1m, "XAUUSD - 1 Min Frame")
    chart_5m_bytes = generate_chart_bytes(df_5m, "XAUUSD - 5 Min Frame")

    metrics_text = (
        f"Live Price: {current_price}\n"
        f"5m EMA20: {ind_5m.get('ema20', 'N/A')}\n"
        f"5m EMA50: {ind_5m.get('ema50', 'N/A')}\n"
        f"5m RSI(14): {ind_5m.get('rsi', 'N/A')}\n"
        f"Recent 5m High: {ind_5m.get('recent_high', 'N/A')}\n"
        f"Recent 5m Low: {ind_5m.get('recent_low', 'N/A')}\n\n"
    )

    full_prompt = metrics_text + PROMPT_BASE

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=[
                full_prompt,
                types.Part.from_bytes(data=chart_5m_bytes, mime_type="image/png"),
                types.Part.from_bytes(data=chart_1m_bytes, mime_type="image/png")
            ]
        )
        gemini_reply = response.text.strip()
        
        final_message = f"📊 XAU/USD Current Price: ${current_price:.2f}\n\n{gemini_reply}"
        send_telegram_message(final_message)
    except Exception as e:
        print("Gemini API Error:", e)
        send_telegram_message(f"Error generating analysis: {str(e)}")

def notify_startup():
    send_telegram_message("🚀 XAUUSD Analysis Bot has been started and is ready for triggers!")

notify_startup()

@app.route("/")
def home():
    return "XAUUSD Bot is Running", 200

@app.route("/run")
def trigger_run():
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
