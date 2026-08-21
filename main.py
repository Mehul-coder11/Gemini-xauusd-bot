import os
import requests
import pandas as pd
from flask import Flask, request
from google import genai

app = Flask(__name__)

# Credentials & Configurations
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "7963353406:AAF-6oU40pXzZ3D6w7c1E450Q-U50607")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-100234567890")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Initialize Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY)

# Global State Variables
virtual_balance = 10000.0
open_trades = []
trade_history = []
latest_live_price = 0.0

def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }
        res = requests.post(url, json=payload, timeout=10)
        print(f"Telegram API Response -> Status: {res.status_code}, Body: {res.text}")
    except Exception as e:
        print("Telegram Send Exception:", e)

def get_live_binance_price():
    global latest_live_price
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
        response = requests.get(url, timeout=5)
        data = response.json()
        if "price" in data:
            latest_live_price = float(data["price"])
            return latest_live_price
    except Exception as e:
        print("Binance Live Price Error:", e)
    return latest_live_price

def fetch_binance_klines(interval):
    interval_map = {"1min": "1m", "15min": "15m"}
    binance_interval = interval_map.get(interval, "1m")
    url = f"https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={binance_interval}&limit=30"
    res = requests.get(url).json()
    
    data = []
    for candle in res:
        data.append({
            "datetime": pd.to_datetime(int(candle[0]), unit='ms', origin='unix'),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4])
        })
    df = pd.DataFrame(data)
    df.set_index("datetime", inplace=True)
    return df

def get_open_trades_details(current_price):
    if not open_trades:
        return "📂 *Open Trades:* None"
    
    details = "📂 *Active Trades:*\n"
    for t in open_trades:
        pnl = (current_price - t['entry']) if t['type'] == 'BUY' else (t['entry'] - current_price)
        details += f"• {t['type']} @ ${t['entry']:.2f} | PnL: ${pnl:.2f}\n"
    return details

def get_trade_history():
    if not trade_history:
        return "📜 *Trade History:* No closed trades yet."
    
    history = "📜 *Recent Closed Trades:*\n"
    for t in trade_history[-5:]:
        history += f"• {t['type']} Entry: ${t['entry']:.2f} | Exit: ${t['exit']:.2f} | PnL: ${t['pnl']:.2f}\n"
    return history

def get_bot_report(current_price):
    return (
        f"📊 *Bot Status Report*\n"
        f"• Live Price: ${current_price:.2f}\n"
        f"• Virtual Balance: ${virtual_balance:.2f}\n"
        f"• Open Positions: {len(open_trades)}\n"
        f"• Total Closed Trades: {len(trade_history)}"
    )

def execute_bot_logic():
    global virtual_balance
    try:
        print("Cron triggered /run: Fetching market data & evaluating Gemini AI signals...")
        current_price = get_live_binance_price()
        if current_price == 0.0:
            current_price = latest_live_price
            
        df_1m = fetch_binance_klines("1min")
        
        # Check active trades against take-profit / stop-loss thresholds
        for t in list(open_trades):
            pnl_points = (current_price - t['entry']) if t['type'] == 'BUY' else (t['entry'] - current_price)
            if pnl_points >= 15.0 or pnl_points <= -10.0:
                virtual_balance += pnl_points
                trade_history.append({
                    "type": t['type'],
                    "entry": t['entry'],
                    "exit": current_price,
                    "pnl": pnl_points
                })
                open_trades.remove(t)
                send_telegram_message(f"🚨 *Trade Closed* ({t['type']})\nExit Price: ${current_price:.2f}\nRealized PnL: ${pnl_points:.2f}\nNew Balance: ${virtual_balance:.2f}")

        # Ask Gemini to evaluate market conditions and give a structured trading decision
        prompt = f"Analyze XAUUSD (PAXGUSDT) current price {current_price} and recent candles. Should we open a BUY, SELL, or HOLD position? Give a concise reasoning."
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        decision = response.text.strip()
        decision_upper = decision.upper()

        if "BUY" in decision_upper and len(open_trades) < 2 and current_price > 0:
            open_trades.append({"type": "BUY", "entry": current_price})
            send_telegram_message(f"🟢 *New BUY Trade Opened*\nPrice: ${current_price:.2f}\nAnalysis: {decision}")
        elif "SELL" in decision_upper and len(open_trades) < 2 and current_price > 0:
            open_trades.append({"type": "SELL", "entry": current_price})
            send_telegram_message(f"🔴 *New SELL Trade Opened*\nPrice: ${current_price:.2f}\nAnalysis: {decision}")
        else:
            send_telegram_message(f"🤖 *Gemini Market Update (HOLD)*\nPrice: ${current_price:.2f}\nAnalysis: {decision}")

    except Exception as e:
        print("Bot Logic Error:", e)

@app.route("/")
def home():
    return "Gemini XAUUSD Trading Bot is Live and Running!", 200

@app.route("/run")
def trigger_run():
    execute_bot_logic()
    return "Cron job executed successfully.", 200

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        update = request.json
        if "message" in update and "text" in update["message"]:
            msg_text = update["message"]["text"].strip().lower()
            chat_id = str(update["message"]["chat"]["id"])
            
            if chat_id == str(TELEGRAM_CHAT_ID):
                current_price = get_live_binance_price()
                if current_price == 0.0:
                    current_price = latest_live_price

                if msg_text == "/balance":
                    send_telegram_message(f"💰 *Current Virtual Balance:* ${virtual_balance:.2f}")
                elif msg_text == "/price":
                    if current_price > 0:
                        send_telegram_message(f"⚡ *Current Live Binance Price:* ${current_price:.2f}")
                    else:
                        send_telegram_message("⚠️ Could not fetch live price right now. Please try again.")
                elif msg_text == "/open":
                    send_telegram_message(get_open_trades_details(current_price))
                elif msg_text == "/history":
                    send_telegram_message(get_trade_history())
                elif msg_text == "/report":
                    send_telegram_message(get_bot_report(current_price))
    except Exception as e:
        print("Webhook Error:", e)
    return "OK", 200

if __name__ == "__main__":
    print("Sending startup message to Telegram...")
    send_telegram_message("🚀 *Gemini XAUUSD Trading Bot has successfully started and is online!*")
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
