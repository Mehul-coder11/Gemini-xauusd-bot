import os
import io
import json
import time
import logging
import threading
import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg') # Required for headless server rendering
import mplfinance as mpf
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from flask import Flask, request, jsonify
from google import genai
from google.genai import types

# ---------------------------------------------------------
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
 
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PORT = int(os.getenv("PORT", 10000))

# Initialize Gemini Client
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

app = Flask(__name__)
STATE_FILE = "trading_state.json"

# ---------------------------------------------------------
# 2. VIRTUAL ACCOUNT MANAGEMENT (Persistent)
# ---------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading state: {e}")
    return {
        "balance": 20.0,
        "leverage": 1000,
        "active_trade": None, 
        "history": []
    }

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving state: {e}")

# Load initial state into memory
app_state = load_state()

# ---------------------------------------------------------
# 3. TELEGRAM INTEGRATION
# ---------------------------------------------------------
def send_telegram_message(text, photo_bytes=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram credentials missing. Skipping message.")
        return

    try:
        if photo_bytes:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {'photo': ('chart.png', photo_bytes, 'image/png')}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': text, 'parse_mode': 'Markdown'}
            requests.post(url, data=data, files=files, timeout=15)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}
            requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Telegram API Error: {e}")

# ---------------------------------------------------------
# 4. DATA FETCHING & INDICATOR PROCESSING
# ---------------------------------------------------------
def get_live_price():
    """Fetches the immediate live spot price of XAUUSD without hitting rate limits excessively."""
    try:
        url = f"https://api.twelvedata.com/price?symbol=XAU/USD&apikey={TWELVE_DATA_API_KEY}"
        res = requests.get(url, timeout=5).json()
        if "price" in res:
            return float(res["price"])
    except Exception as e:
        logging.error(f"Live price fetch error: {e}")
    return None

def fetch_and_process_data(interval):
    """Fetches OHLC data and calculates technical indicators."""
    try:
        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={interval}&outputsize=60&apikey={TWELVE_DATA_API_KEY}"
        res = requests.get(url, timeout=10).json()
        
        if 'values' not in res:
            logging.error(f"API Error fetching {interval}: {res}")
            return None, None
            
        df = pd.DataFrame(res['values'])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        df = df.astype(float)
        df.sort_index(inplace=True) # Sort oldest to newest
        
        # Technical Indicators for Gemini Prompt Context
        df['RSI'] = RSIIndicator(df['close'], window=14).rsi()
        df['EMA_20'] = EMAIndicator(df['close'], window=20).ema_indicator()
        df['EMA_50'] = EMAIndicator(df['close'], window=50).ema_indicator()
        
        latest_data = df.iloc[-1]
        context = {
            "close": latest_data['close'],
            "rsi": round(latest_data['RSI'], 2),
            "ema_20": round(latest_data['EMA_20'], 2),
            "ema_50": round(latest_data['EMA_50'], 2)
        }
        
        return df, context
    except Exception as e:
        logging.error(f"Data processing error ({interval}): {e}")
        return None, None

def generate_candlestick_chart(df, title):
    """Generates a professional PNG candlestick chart in memory."""
    # We plot the last 40 candles for clean visibility
    plot_df = df.tail(40)
    
    # Custom professional styling
    mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
    s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)
    
    # Add EMA lines to the chart visually
    ap = [
        mpf.make_addplot(plot_df['EMA_20'], color='blue', width=1.5),
        mpf.make_addplot(plot_df['EMA_50'], color='orange', width=1.5)
    ]
    
    fig, ax = mpf.plot(plot_df, type='candle', style=s, addplot=ap, returnfig=True, figsize=(8, 5))
    ax[0].set_title(title, fontsize=12, weight='bold')
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=100)
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()

# ---------------------------------------------------------
# 5. LIVE TRADE MONITORING THREAD
# ---------------------------------------------------------
def background_trade_monitor():
    """Runs continuously to check open PNL and TP/SL execution using live data ONLY."""
    while True:
        try:
            trade = app_state.get("active_trade")
            if trade:
                current_price = get_live_price()
                if not current_price:
                    time.sleep(5)
                    continue

                entry = trade["entry"]
                t_type = trade["type"]
                tp = trade["tp"]
                sl = trade["sl"]
                size = trade["size"]
                
                # Calculate live unclosed PnL
                if t_type == "BUY":
                    pnl = (current_price - entry) * size
                    hit_tp = current_price >= tp
                    hit_sl = current_price <= sl
                else:
                    pnl = (entry - current_price) * size
                    hit_tp = current_price <= tp
                    hit_sl = current_price >= sl
                
                trade["open_pnl"] = round(pnl, 2)
                trade["current_price"] = current_price
                save_state(app_state) # Save PnL updates locally
                
                # Execute Closure logic
                if hit_tp or hit_sl:
                    result = "PROFIT (TP) 🎯" if hit_tp else "LOSS (SL) 🛑"
                    app_state["balance"] += pnl
                    
                    closed_trade = trade.copy()
                    closed_trade["result"] = result
                    closed_trade["exit_price"] = current_price
                    closed_trade["final_pnl"] = round(pnl, 2)
                    closed_trade.pop("open_pnl", None)
                    closed_trade.pop("current_price", None)
                    
                    app_state["history"].append(closed_trade)
                    app_state["active_trade"] = None
                    save_state(app_state)
                    
                    msg = (f"🔔 *TRADE CLOSED: {result}*\n\n"
                           f"Type: {t_type}\n"
                           f"Entry: `${entry}`\n"
                           f"Exit: `${current_price}`\n"
                           f"Net PnL: `${pnl:.2f}`\n"
                           f"New Balance: `${app_state['balance']:.2f}`")
                    send_telegram_message(msg)
                    
        except Exception as e:
            logging.error(f"Monitor Thread Error: {e}")
        
        # Poll Twelve Data every 15 seconds to respect free-tier limits, 
        # while keeping live PnL actively updated.
        time.sleep(15)

# ---------------------------------------------------------
# 6. CRON TRIGGER ENDPOINT & GEMINI LOGIC
# ---------------------------------------------------------
@app.route('/run', methods=['GET', 'POST'])
def run_cron_bot():
    if app_state["active_trade"]:
        return jsonify({"status": "ignored", "message": "Active trade already running."}), 200

    logging.info("Cron triggered. Fetching market data for Gemini...")
    df_1m, ctx_1m = fetch_and_process_data('1min')
    df_15m, ctx_15m = fetch_and_process_data('15min')
    
    if df_1m is None or df_15m is None:
        return jsonify({"status": "error", "message": "Failed to fetch market data"}), 500

    current_price = get_live_price() or ctx_1m['close']
    
    # Notify user that Gemini request is firing
    send_telegram_message(f"📡 *Fetching Gemini Analysis...*\nCurrent XAUUSD Price: `${current_price}`")
    
    img_1m = generate_candlestick_chart(df_1m, f"XAU/USD 1 Min Chart - Live: ${current_price}")
    img_15m = generate_candlestick_chart(df_15m, f"XAU/USD 15 Min Chart - Live: ${current_price}")

    # Build History Context
    history_str = "No previous trades yet."
    if app_state["history"]:
        recent_trades = app_state["history"][-5:]
        history_str = "\n".join([
            f"- {t['type']} | Entry: {t['entry']} | Result: {t['result']} | PnL: ${t['final_pnl']}" 
            for t in recent_trades
        ])

    prompt = f"""
You are a professional intraday high-frequency trader. Analyze this live XAUUSD (Gold) data and charts.
Current XAUUSD Price: {current_price}

Technical Context (1min): RSI={ctx_1m['rsi']}, EMA20={ctx_1m['ema_20']}, EMA50={ctx_1m['ema_50']}
Technical Context (15min): RSI={ctx_15m['rsi']}, EMA20={ctx_15m['ema_20']}, EMA50={ctx_15m['ema_50']}

Last 5 Trade Results (Review to improve your future performance):
{history_str}

STRICT INSTRUCTIONS:
1. Review the 1-min and 15-min charts (candlesticks + EMA blue/orange lines) and indicators.
2. Only output a trade if you have 60% to 70% confidence or higher. Otherwise, say: NO TRADE.
3. Risk/Reward MUST be exactly 1:2. The TP distance from Entry MUST be double the SL distance.
4. SL distance MUST NOT exceed $4.00.
5. TP distance MUST be minimum $4.00 (which implies SL minimum $2.00).
6. Target achievable intraday moves.

OUTPUT FORMAT (DO NOT output ANY analysis text or reasoning, just the exact block below):
TRADE: BUY (or SELL)
ENTRY: [exact numerical price]
TP: [exact numerical price]
SL: [exact numerical price]

If no high probability setup is found, output ONLY:
NO TRADE
"""

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                prompt,
                types.Part.from_bytes(data=img_1m, mime_type='image/png'),
                types.Part.from_bytes(data=img_15m, mime_type='image/png')
            ]
        )
        
        reply = response.text.strip().upper()
        logging.info(f"Gemini Response:\n{reply}")
        
        if "TRADE: BUY" in reply or "TRADE: SELL" in reply:
            try:
                # Safely parse the response block
                lines = reply.split('\n')
                t_type = "BUY" if "BUY" in reply else "SELL"
                entry_p, tp_p, sl_p = 0.0, 0.0, 0.0
                
                for line in lines:
                    if "ENTRY:" in line: entry_p = float(line.split(':')[1].strip())
                    if "TP:" in line: tp_p = float(line.split(':')[1].strip())
                    if "SL:" in line: sl_p = float(line.split(':')[1].strip())
                
                # Execute Virtual Trade
                account_power = app_state["balance"] * app_state["leverage"]
                position_size = account_power / entry_p
                
                app_state["active_trade"] = {
                    "type": t_type,
                    "entry": entry_p,
                    "tp": tp_p,
                    "sl": sl_p,
                    "size": position_size,
                    "open_pnl": 0.0,
                    "current_price": current_price
                }
                save_state(app_state)
                
                msg = (f"🟢 *NEW TRADE EXECUTED*\n\n"
                       f"Action: *{t_type} XAUUSD*\n"
                       f"Entry Price: `${entry_p}`\n"
                       f"Take Profit: `${tp_p}`\n"
                       f"Stop Loss: `${sl_p}`\n"
                       f"Size (Leveraged): {position_size:.4f} units")
                send_telegram_message(msg, photo_bytes=img_15m)
                
                return jsonify({"status": "Trade Opened", "details": app_state["active_trade"]})
                
            except Exception as parse_err:
                logging.error(f"Error parsing Gemini response: {parse_err}")
                send_telegram_message("⚠️ *Gemini returned an invalid trade format.*")
                return jsonify({"status": "Parse Error"}), 500
        else:
            # NO TRADE executed
            send_telegram_message("💤 *Analysis complete. No high-probability trade found.* (NO TRADE)")
            return jsonify({"status": "No Trade"})

    except Exception as e:
        logging.error(f"Gemini API Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ---------------------------------------------------------
# 7. TELEGRAM WEBHOOK / COMMAND HANDLER
# ---------------------------------------------------------
@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    """Handles incoming commands from Telegram."""
    update = request.get_json()
    if not update or 'message' not in update or 'text' not in update['message']:
        return "OK", 200

    chat_id = str(update['message']['chat']['id'])
    text = update['message']['text'].lower().strip()
    
    # Simple Security: Only respond to your configured chat ID
    if chat_id != TELEGRAM_CHAT_ID:
        return "Unauthorized", 403

    if text == '/balance':
        bal = app_state['balance']
        trade = app_state.get('active_trade')
        eq = bal + (trade['open_pnl'] if trade else 0.0)
        send_telegram_message(f"💰 *Account Status*\nBalance: `${bal:.2f}`\nEquity: `${eq:.2f}`\nLeverage: {app_state['leverage']}x")
        
    elif text == '/price':
        price = get_live_price()
        send_telegram_message(f"📈 *Live XAUUSD Price:* `${price}`")
        
    elif text == '/active':
        trade = app_state.get('active_trade')
        if trade:
            msg = (f"📊 *Active Trade: {trade['type']}*\n"
                   f"Entry: `${trade['entry']}`\n"
                   f"Current Price: `${trade.get('current_price', 0)}`\n"
                   f"Open PnL: `${trade.get('open_pnl', 0):.2f}`\n"
                   f"TP: `${trade['tp']}` | SL: `${trade['sl']}`")
            send_telegram_message(msg)
        else:
            send_telegram_message("No active trades running currently.")
            
    elif text == '/history':
        if not app_state["history"]:
            send_telegram_message("No trade history available yet.")
        else:
            hist = app_state["history"][-5:]
            msg = "📜 *Last 5 Trades:*\n\n"
            for t in hist:
                msg += f"• *{t['type']}* | {t['result']}\n  Entry: `${t['entry']}` → Exit: `${t['exit_price']}`\n  PnL: `${t['final_pnl']:.2f}`\n\n"
            send_telegram_message(msg)

    return "OK", 200

# Endpoint to easily register your Render URL with Telegram Webhooks
@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    host_url = request.url_root.replace('http://', 'https://')
    webhook_url = f"{host_url}telegram_webhook"
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook?url={webhook_url}"
    res = requests.get(api_url).json()
    return jsonify(res)

@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "Active", "active_trade": bool(app_state["active_trade"])}), 200

# ---------------------------------------------------------
# 8. STARTUP SCRIPT
# ---------------------------------------------------------
if __name__ == '__main__':
    logging.info("Starting up Bot & Threads...")
    
    # Send Startup Message and attempt an immediate first run in background
    send_telegram_message("🚀 *XAUUSD AI Trading Bot Started!*\n\nConnected to Binance/TwelveData tick streams.\nMonitoring market for initial entry...")
    
    # Start Live Trade Monitor Thread
    monitor_thread = threading.Thread(target=background_trade_monitor, daemon=True)
    monitor_thread.start()
    
    # Optional: trigger the first run automatically locally (Wait 3 seconds for server to bind)
    def initial_trigger():
        time.sleep(3)
        requests.get(f"http://127.0.0.1:{PORT}/run")
        
    threading.Thread(target=initial_trigger, daemon=True).start()
    
    app.run(host='0.0.0.0', port=PORT, use_reloader=False)
