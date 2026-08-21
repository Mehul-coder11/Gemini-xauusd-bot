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

# Credentials & Configurations (Pulled securely from Render Environment Variables)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "demo")

client = genai.Client(api_key=GEMINI_API_KEY)

# Global State Variables
virtual_balance = 10000.0
open_trades = []
trade_history = []
last_run_timestamp = 0
latest_live_price = 2500.0

def send_telegram_message(text, photo_bytes=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram Error: Token or Chat ID is missing from environment variables.")
        return
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

def get_live_twelvedata_price():
    global latest_live_price
    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={TWELVE_DATA_API_KEY}"
        response = requests.get(url, timeout=5)
        data = response.json()
        print("Twelve Data Price Response:", data)
        if "price" in data:
            latest_live_price = float(data["price"])
            return latest_live_price
    except Exception as e:
        print("Twelve Data Live Price Error:", e)
    return latest_live_price

def fetch_and_prepare_data(interval):
    twelve_interval = "1min" if interval == "1min" else "15min"
    url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={twelve_interval}&outputsize=50&apikey={TWELVE_DATA_API_KEY}"
    res = requests.get(url).json()
    print(f"Twelve Data Time Series Response ({interval}):", res)
    
    if "values" not in res:
        print("Twelve Data Time Series Error or Rate Limit:", res)
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
        if current_time - last_run_timestamp < 550 and last_run_timestamp != 0:
            print("Run requested too soon, skipping...")
            return
        last_run_timestamp = current_time

        print("Executing scheduled 10-minute bot analysis...")
        current_price = get_live_twelvedata_price()

        df_1m = fetch_and_prepare_data("1min")
        df_15m = fetch_and_prepare_data("15min")

        if df_1m.empty:
            print("Skipping execution due to empty dataset.")
            send_telegram_message("⚠️ *Bot Error:* Twelve Data returned empty data or hit rate limit.")
            return

        chart_1m_bytes = generate_chart_image(df_1m, "XAUUSD - 1 Min")
        
        # Check active trades against SL / TP rules
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
    return "Gemini XAUUSD Trading Bot with Twelve Data is Active!", 200

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
                live_p = get_live_twelvedata_price()
                if msg_text == "/price":
                    send_telegram_message(f"⚡ *Live XAU/USD Price:* ${live_p:.2f}")
                elif msg_text in ["/open", "/active", "/trade"]:
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
                elif msg_text == "/history":
                    if not trade_history:
                        send_telegram_message("📜 *Trade History:* No closed trades yet.")
                    else:
                        history = "📜 *Last Trades:*\n"
                        for t in trade_history[-5:]:
                            history += f"• {t['type']} | Exit: ${t['exit']:.2f} | PnL: ${t['pnl']:.2f}\n"
                        send_telegram_message(history)
                elif msg_text == "/run":
                    execute_bot_logic()
                    send_telegram_message("🔄 Manual run executed via command.")
    except Exception as e:
        print("Webhook Error:", e)
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
