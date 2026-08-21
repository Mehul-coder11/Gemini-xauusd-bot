import matplotlib.pyplot as plt
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
matplotlib.use('Agg')
import mplfinance as mpf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from flask import Flask, request, jsonify
from google import genai
from google.genai import types

# ---------------------------------------------------------
# 1. CONFIGURATION
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PORT = int(os.getenv("PORT", 10000))

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
app = Flask(__name__)
STATE_FILE = "trading_state.json"

# Full browser spoofing headers to avoid Cloudflare/Binance hosting block
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9"
}

# ---------------------------------------------------------
# 2. VIRTUAL ACCOUNT & PERSISTENCE
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

app_state = load_state()

# ---------------------------------------------------------
# 3. RELIABLE MULTI-ENDPOINT LIVE PRICE FETCHING
# ---------------------------------------------------------
def get_binance_live_price():
    """Fetches live PAXG (Gold) spot price with reliable fallbacks if primary Binance endpoints block cloud hosting IPs."""
    endpoints = [
        "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT",
        "https://api1.binance.com/api/v3/ticker/price?symbol=PAXGUSDT",
        "https://api2.binance.com/api/v3/ticker/price?symbol=PAXGUSDT",
        "https://api3.binance.com/api/v3/ticker/price?symbol=PAXGUSDT",
        "https://api.coingecko.com/api/v3/simple/price?ids=pax-gold&vs_currencies=usd"
    ]
    
    for url in endpoints:
        try:
            res = requests.get(url, headers=HTTP_HEADERS, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if "price" in data:
                    return float(data["price"])
                elif "pax-gold" in data and "usd" in data["pax-gold"]:
                    return float(data["pax-gold"]["usd"])
        except Exception as e:
            logging.error(f"Price fetch error for {url}: {e}")
            
    return None

# ---------------------------------------------------------
# 4. TELEGRAM INTEGRATION
# ---------------------------------------------------------
def send_telegram_message(text, photo_bytes=None, target_chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    chat = target_chat_id or TELEGRAM_CHAT_ID
    if not chat:
        return
    try:
        if photo_bytes:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {'photo': ('chart.png', photo_bytes, 'image/png')}
            data = {'chat_id': chat, 'caption': text, 'parse_mode': 'Markdown'}
            requests.post(url, data=data, files=files, timeout=15)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {'chat_id': chat, 'text': text, 'parse_mode': 'Markdown'}
            requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Telegram API Error: {e}")

# ---------------------------------------------------------
# 5. BINANCE KLINE DATA & CHART GENERATION
# ---------------------------------------------------------
def fetch_and_process_data(interval):
    """Fetches OHLCV candlestick data directly with fallback mirrors."""
    binance_interval = interval.replace('min', 'm')
    
    urls = [
        f"https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={binance_interval}&limit=60",
        f"https://api1.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={binance_interval}&limit=60",
        f"https://api2.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={binance_interval}&limit=60"
    ]
    
    for url in urls:
        try:
            res = requests.get(url, headers=HTTP_HEADERS, timeout=5)
            if res.status_code != 200:
                continue
                
            data = res.json()
            if not isinstance(data, list) or len(data) == 0:
                continue
                
            df = pd.DataFrame(data, columns=[
                'datetime', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'number_of_trades',
                'taker_buy_base', 'taker_buy_quote', 'ignore'
            ])
            
            df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
            df.set_index('datetime', inplace=True)
            df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
            
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
            logging.error(f"Kline fetch error ({url}): {e}")
            
    return None, None

def generate_candlestick_chart(df, title):
    plot_df = df.tail(40)
    mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
    s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)
    
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
# 6. TICK MONITOR (BACKGROUND LOOP)
# ---------------------------------------------------------
def background_trade_monitor():
    while True:
        try:
            trade = app_state.get("active_trade")
            if trade:
                current_price = get_binance_live_price()
                if not current_price:
                    time.sleep(2)
                    continue

                entry = trade["entry"]
                t_type = trade["type"]
                tp = trade["tp"]
                sl = trade["sl"]
                size = trade["size"]
                
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
                save_state(app_state)
                
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
                           f"Exit Price: `${current_price}`\n"
                           f"Net PnL: `${pnl:.2f}`\n"
                           f"New Balance: `${app_state['balance']:.2f}`")
                    send_telegram_message(msg)
                    
        except Exception as e:
            logging.error(f"Monitor Thread Error: {e}")
        
        time.sleep(2)

# ---------------------------------------------------------
# 7. CRON TRIGGER ENDPOINT & GEMINI
# ---------------------------------------------------------
@app.route('/run', methods=['GET', 'POST'])
def run_cron_bot():
    if app_state["active_trade"]:
        return jsonify({"status": "ignored", "message": "Active trade running."}), 200

    current_binance_price = get_binance_live_price()
    
    price_display = f"`${current_binance_price}`" if current_binance_price else "Unavailable"
    send_telegram_message(
        f"📡 *Sending Data to Gemini API...*\n"
        f"💰 *Current Gold Price:* {price_display}"
    )

    df_1m, ctx_1m = fetch_and_process_data('1min')
    df_15m, ctx_15m = fetch_and_process_data('15min')
    
    contents = []
    
    if ctx_1m and ctx_15m:
        prompt = f"""
Assume you are a professional intraday trader and this is the data of live xauusd, with 1 minute chart and 15 minute chart please analyse it carefully ,and guve me a trade in xauusd but guve trade only when you have confidence of 60 to 70 percent or more otherwise say no trade, and when there is a trade then also tell what to do like buy or sell and what is the current price and at what price to enter the trade and what should be the take profit and sl, always keep risk reward ratio to 1 ratio 2 which means to should be double of sl and teh and only guve trades in which sl should not be more than 4 dollars and the trades in which has a minimum tp of 4 dollars or more, only guve intraday trades and only guve tp which will be achieved surely before today market close, and your main objective is to give profitable trades and grow the urers capital and the give him net gain.

Current Price: {current_binance_price}
Indicators (1m): RSI={ctx_1m['rsi']}, EMA20={ctx_1m['ema_20']}, EMA50={ctx_1m['ema_50']}
Indicators (15m): RSI={ctx_15m['rsi']}, EMA20={ctx_15m['ema_20']}, EMA50={ctx_15m['ema_50']}

OUTPUT STRICT FORMAT (No analysis text, no reasoning):
TRADE: BUY (or SELL)
ENTRY: [price]
TP: [price]
SL: [price]

If no confidence, output strictly:
NO TRADE
"""
        contents.append(prompt)
    else:
        prompt = f"Current Price: {current_binance_price}\nOUTPUT STRICT FORMAT:\nNO TRADE"
        contents.append(prompt)

    if df_1m is not None and df_15m is not None:
        try:
            img_1m = generate_candlestick_chart(df_1m, f"1m Chart - Price: ${current_binance_price}")
            img_15m = generate_candlestick_chart(df_15m, f"15m Chart - Price: ${current_binance_price}")
            contents.append(types.Part.from_bytes(data=img_1m, mime_type='image/png'))
            contents.append(types.Part.from_bytes(data=img_15m, mime_type='image/png'))
        except Exception as e:
            logging.error(f"Chart Attachment Error: {e}")

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents
        )
        
        reply = response.text.strip().upper()
        
        if "TRADE: BUY" in reply or "TRADE: SELL" in reply:
            lines = reply.split('\n')
            t_type = "BUY" if "BUY" in reply else "SELL"
            entry_p = current_binance_price if current_binance_price else 0.0
            tp_p, sl_p = 0.0, 0.0
            
            for line in lines:
                if "ENTRY:" in line: 
                    try: entry_p = float(line.split(':')[1].strip())
                    except: pass
                if "TP:" in line: 
                    try: tp_p = float(line.split(':')[1].strip())
                    except: pass
                if "SL:" in line: 
                    try: sl_p = float(line.split(':')[1].strip())
                    except: pass
            
            position_size = (app_state["balance"] * app_state["leverage"]) / (entry_p if entry_p > 0 else 1)
            
            app_state["active_trade"] = {
                "type": t_type, "entry": entry_p, "tp": tp_p, "sl": sl_p, 
                "size": position_size, "open_pnl": 0.0, "current_price": current_binance_price
            }
            save_state(app_state)
            
            msg = (f"🟢 *NEW TRADE EXECUTED*\n\n"
                   f"Action: *{t_type} XAUUSD*\n"
                   f"Current Price: `${current_binance_price}`\n"
                   f"Entry Price: `${entry_p}`\n"
                   f"Take Profit: `${tp_p}`\n"
                   f"Stop Loss: `${sl_p}`")
            send_telegram_message(msg)
            return jsonify({"status": "Trade Executed", "reply": reply})
        else:
            send_telegram_message(f"🚫 *NO TRADE RECOMMENDED*\nCurrent Price: `${current_binance_price}`")
            return jsonify({"status": "No Trade"})

    except Exception as e:
        logging.error(f"Gemini API Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ---------------------------------------------------------
# 8. TELEGRAM COMMANDS & WEBHOOKS
# ---------------------------------------------------------
@app.route('/webhook', methods=['POST'])
@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if not update or 'message' not in update:
        return jsonify({"status": "ok"}), 200

    msg_obj = update['message']
    chat_id = msg_obj['chat']['id']
    text = msg_obj.get('text', '').lower().strip()

    if text.startswith('/balance'):
        bal = app_state['balance']
        trade = app_state.get('active_trade')
        eq = bal + (trade['open_pnl'] if trade else 0.0)
        send_telegram_message(f"💰 *Balance:* `${bal:.2f}`\n*Equity:* `${eq:.2f}`", target_chat_id=chat_id)
        
    elif text.startswith('/price'):
        price = get_binance_live_price()
        if price:
            send_telegram_message(f"📈 *Live Gold (PAXG/USDT):* `${price}`", target_chat_id=chat_id)
        else:
            send_telegram_message("❌ *Error fetching live price.* Check host IP restrictions.", target_chat_id=chat_id)
        
    elif text.startswith('/active'):
        trade = app_state.get('active_trade')
        if trade:
            send_telegram_message(f"📊 *Trade:* {trade['type']}\nEntry: `${trade['entry']}`\nCurrent: `${trade.get('current_price', 0)}`\nPnL: `${trade.get('open_pnl', 0):.2f}`", target_chat_id=chat_id)
        else:
            send_telegram_message("No active trades.", target_chat_id=chat_id)
            
    elif text.startswith('/history'):
        hist = app_state["history"][-5:]
        if hist:
            msg = "📜 *Last Trades:*\n" + "\n".join([f"• {t['type']} | {t['result']} | PnL: `${t['final_pnl']:.2f}`" for t in hist])
        else:
            msg = "📜 No trade history available."
        send_telegram_message(msg, target_chat_id=chat_id)

    elif text.startswith('/start'):
        send_telegram_message("👋 *Welcome to XAU/USD Bot!*\nAvailable commands: /price, /balance, /active, /history", target_chat_id=chat_id)

    return jsonify({"status": "ok"}), 200

@app.route('/', methods=['GET', 'HEAD'])
def health():
    return "XAUUSD Bot Active", 200

if __name__ == '__main__':
    send_telegram_message("🚀 *xauusd bot has started*")
    threading.Thread(target=background_trade_monitor, daemon=True).start()
    
    app.run(host='0.0.0.0', port=PORT, use_reloader=False)
