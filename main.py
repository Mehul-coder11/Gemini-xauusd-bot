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

PROMPT_BASE = (
    "Assume you are a professional intraday trader analyzing live XAU/USD (Gold) data with attached 1-minute and 15-minute chart images. "
    "Evaluate the market structure, momentum, and key indicator values carefully.\n\n"
    "Rules for trade generation:\n"
    "- Give a trade decision ONLY if you have high confidence based on market structure and trend alignment. Otherwise, respond strictly with 'NO TRADE'.\n"
    "- If there is a high probability setup, provide: Direction (BUY/SELL), Entry Price, Stop Loss (SL), Take Profit (TP).\n"
    "- Base Stop Loss and Take Profit on recent support/resistance levels or swing highs/lows (Aim for a minimum Risk-to-Reward ratio of 1:1.5 or 1:2).\n"
    "- Focus exclusively on intraday momentum.\n"
    "- Output ONLY the final trade decision without explanations, reasons, or analysis commentary."
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
    
    # Simple RSI calculation
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs.iloc[-1])) if loss.iloc[-1] != 0 else 50
    
    high_recent = df['high'].max()
    low_recent = df['low'].min()
    
    return {
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "rsi": round(rsi, 2),
        "recent_high": round(high_recent, 2),
        "recent_low": round(low_recent, 2)
    }

def generate_chart_bytes(df, title):
    buf = io.BytesIO()
    # Using last 50 candles for chart plotting
    plot_df = df.tail(50)
    mpf.plot(plot_df, type='candle', style='charles', volume=True, title=title, savefig=dict(fname=buf, format='png', dpi=100))
    buf.seek(0)
    return buf.read()

def process_and_analyze():
    df_1m = fetch_chart_data("1min")
    df_15m = fetch_chart_data("15min")

    if df_1m.empty or df_15m.empty:
        send_telegram_message("Error: Unable to fetch candle data from Twelve Data API.")
        return

    current_price = df_1m.iloc[-1]['close']
    ind_15m = compute_indicators(df_15m)

    chart_1m_bytes = generate_chart_bytes(df_1m, "XAUUSD - 1 Min")
    chart_15m_bytes = generate_chart_bytes(df_15m, "XAUUSD - 15 Min")

    # Construct explicit context text
    metrics_text = (
        f"Live Price: {current_price}\n"
        f"15m EMA20: {ind_15m.get('ema20', 'N/A')}\n"
        f"15m EMA50: {ind_15m.get('ema50', 'N/A')}\n"
        f"15m RSI(14): {ind_15m.get('rsi', 'N/A')}\n"
        f"Recent High: {ind_15m.get('recent_high', 'N/A')}\n"
        f"Recent Low: {ind_15m.get('recent_low', 'N/A')}\n\n"
    )

    full_prompt = metrics_text + PROMPT_BASE

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
