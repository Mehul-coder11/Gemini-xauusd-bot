import os
import io
import time
import requests
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import mplfinance as mpf
import ta
from flask import Flask, request
from google import genai

app = Flask(__name__)

# Credentials & Configurations
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "7963353406:AAF-6oU40pXzZ3D6w7c1E450Q-U50607")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-100234567890")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

client = genai.Client(api_key=GEMINI_API_KEY)

# Global State Variables
virtual_balance = 10000.0
open_trades = []
trade_history = []
last_run_timestamp = 0

def send_telegram_message(text, photo_bytes=None):
    try:
        if photo_bytes:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {'photo': ('chart.png', photo_bytes, 'image/png')}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": text, "parse_mode": "Markdown"}
            requests.post(url, data=data, files=files, timeout=15)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
            requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Telegram Send Exception:", e)

def get_live_binance_price():
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
        response = requests.get(url, timeout=5)
        data = response.json()
        if "price" in data:
            return float(data["price"])
    except Exception as e:
        print("Binance Live Price Error:", e)
    return 0.0

def fetch_and_prepare_data(interval):
    binance_interval = "1m" if interval == "1min" else "15m"
    url = f"https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={binance_interval}&limit=50"
    res = requests.get(url).json()
    
    data = []
    for candle in res:
        data.append({
            "datetime": pd.to_datetime(int(candle[0]), unit='ms', origin='unix'),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5])
        })
    df = pd.DataFrame(data)
    df.set_index("datetime", inplace=True)
    
    # Calculate indicators for liquidity & supply/demand clues
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd()
    df['sma_fast'] = ta.trend.SMAIndicator(df['close'], window=9).sma_indicator()
    df['sma_slow'] = ta.trend.SMAIndicator(df['close'], window=21).sma_indicator()
    return df

def generate_chart_image(df, title):
    buf = io.BytesIO()
    mpf.plot(df, type='candle', style='charles', volume=True, title=title, savefig=dict(fname=buf, format='png', dpi=100))
    buf.seek(0)
    return buf.read()

def execute_bot_logic():
    global virtual_balance, last_run_timestamp
    try:
        current_time = time.time()
        # Enforce 10 minute check interval restriction on run
        if current_time - last_run_timestamp < 550 and last_run_timestamp != 0:
            print("Run requested too soon, skipping...")
            return
        last_run_timestamp = current_time

        print("Executing scheduled 10-minute bot analysis...")
        current_price = get_live_binance_price()
        if current_price == 0:
            return

        df_1m = fetch_and_prepare_data("1min")
        df_15m = fetch_and_prepare_data("15m")

        chart_1m_bytes = generate_chart_image(df_1m, "XAUUSD (PAXGUSDT) - 1 Min")
        
        # Check active trades against SL / TP rules with accurate PnL math
        for t in list(open_trades):
            hit_exit = False
            exit_price = current_price
            pnl_dollars = 0.0

            if t['type'] == 'BUY':
                if current_price <= t['sl']:
                    exit_price = t['sl']
                    pnl_dollars = (exit_price - t['entry']) * 10
                    hit_exit = True
                elif current_price >= t['tp']:
                    exit_price = t['tp']
                    pnl_dollars = (exit_price - t['entry']) * 10
                    hit_exit = True
            elif t['type'] == 'SELL':
                if current_price >= t['sl']:
                    exit_price = t['sl']
                    pnl_dollars = (t['entry'] - exit_price) * 10
                    hit_exit = True
                elif current_price <= t['tp']:
                    exit_price = t['tp']
                    pnl_dollars = (t['entry'] - exit_price) * 10
                    hit_exit = True

            if hit_exit:
                virtual_balance += pnl_dollars
                trade_history.append({"type": t['type'], "entry": t['entry'], "exit": exit_price, "pnl": pnl_dollars})
                open_trades.remove(t)
                send_telegram_message(f"🚨 *Trade Closed by SL/TP ({t['type']})*\nExit Price: ${exit_price:.2f}\nRealized PnL: ${pnl_dollars:.2f}\nNew Balance: ${virtual_balance:.2f}")

        # Gather Indicator Summary for Gemini Context
        latest_1m = df_1m.iloc[-1]
        latest_15m = df_15m.iloc[-1]
        indicators_summary = (
            f"1M RSI: {latest_1m['rsi']:.2f}, SMA9: {latest_1m['sma_fast']:.2f}, SMA21: {latest_1m['sma_slow']:.2f}\n"
            f"15M RSI: {latest_15m['rsi']:.2f}, SMA9: {latest_15m['sma_fast']:.2f}, SMA21: {latest_15m['sma_slow']:.2f}"
        )

        prompt = (
            f"Analyze XAUUSD current price {current_price}. Indicators:\n{indicators_summary}\n"
            "Evaluate liquidity, supply/demand, order blocks, market sweeps, and volume. "
            "Return a decision starting with BUY, SELL, or HOLD, followed by your reasoning, "
            "and suggest explicit SL (Stop Loss) and TP (Take Profit) levels."
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt, client.types.Part.from_bytes(data=chart_1m_bytes, mime_type="image/png")]
        )
        decision = response.text.strip()
        decision_upper = decision.upper()

        if "BUY" in decision_upper and len(open_trades) < 2:
            sl = current_price - 3.0
            tp = current_price + 5.0
            open_trades.append({"type": "BUY", "entry": current_price, "sl": sl, "tp": tp})
            send_telegram_message(f"🟢 *New BUY Position Opened*\nPrice: ${current_price:.2f}\nSL: ${sl:.2f} | TP: ${tp:.2f}\n\n{decision}", chart_1m_bytes)
        elif "SELL" in decision_upper and len(open_trades) < 2:
            sl = current_price + 3.0
            tp = current_price - 5.0
            open_trades.append({"type": "SELL", "entry": current_price, "sl": sl, "tp": tp})
            send_telegram_message(f"🔴 *New SELL Position Opened*\nPrice: ${current_price:.2f}\nSL: ${sl:.2f} | TP: ${tp:.2f}\n\n{decision}", chart_1m_bytes)
        else:
            send_telegram_message(f"🤖 *Gemini Market Update (HOLD)*\nPrice: ${current_price:.2f}\n{decision}", chart_1m_bytes)

    except Exception as e:
        print("Bot Logic Error:", e)

@app.route("/")
def home():
    return "Gemini XAUUSD Trading Bot is Active!", 200

@app.route("/run")
def trigger_run():
    execute_bot_logic()
    return "Cron job processed successfully.", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        update = request.json
        if "message" in update and "text" in update["message"]:
            msg_text = update["message"]["text"].strip().lower()
            chat_id = str(update["message"]["chat"]["id"])
            
            if chat_id == str(TELEGRAM_CHAT_ID):
                if msg_text == "/price":
                    live_p = get_live_binance_price()
                    send_telegram_message(f"⚡ *Live Binance Price:* ${live_p:.2f}")
                elif msg_text in ["/open", "/active"]:
                    live_p = get_live_binance_price()
                    if not open_trades:
                        send_telegram_message("📂 *Active Trades:* None")
                    else:
                        details = "📂 *Active Trades:*\n"
                        for t in open_trades:
                            pnl = (live_p - t['entry']) * 10 if t['type'] == 'BUY' else (t['entry'] - live_p) * 10
                            details += f"• {t['type']} @ ${t['entry']:.2f} | SL: ${t['sl']} | TP: ${t['tp']} | PnL: ${pnl:.2f}\n"
                        send_telegram_message(details)
                elif msg_text == "/balance":
                    send_telegram_message(f"💰 *Virtual Balance:* ${virtual_balance:.2f}")
    except Exception as e:
        print("Webhook Error:", e)
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
