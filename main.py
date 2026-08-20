import os
import time
import traceback
from threading import Thread
import matplotlib
matplotlib.use('Agg')
import mplfinance as mpf
import pandas as pd
from flask import Flask, request
from google import genai
import requests
import websocket
import json

# Load Environment Variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- Virtual Tracker Settings & State ---
starting_balance = 20.0
virtual_balance = 20.0
leverage = 1000
lot_size = 0.01
contract_size = 100
active_virtual_trades = []
closed_trades = []
peak_equity = 20.0
max_drawdown = 0.0
low_balance_alert_sent = False
latest_live_price = 0.0

# --- Direct Binance Price Fetcher (100% Accurate) ---
def get_live_binance_price():
  try:
    url = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
    res = requests.get(url, timeout=5).json()
    if 'price' in res:
      return float(res['price'])
  except Exception:
    pass
  return 0.0

# --- Live Price Web Socket Background Thread ---
def on_message(ws, message):
  global latest_live_price
  try:
    data = json.loads(message)
    if 'p' in data:
      latest_live_price = float(data['p'])
  except Exception:
    pass

def start_websocket():
  while True:
    try:
      ws = websocket.WebSocketApp(
          "wss://stream.binance.com:9443/ws/paxgusdt@trade",
          on_message=on_message
      )
      ws.run_forever(ping_interval=30)
    except Exception:
      time.sleep(5)

Thread(target=start_websocket, daemon=True).start()

# --- Real-Time 1-Second Trade Monitor ---
def live_trade_monitor():
  global virtual_balance, active_virtual_trades, closed_trades
  while True:
    time.sleep(1)
    current_price = get_live_binance_price()
    if current_price == 0.0:
      current_price = latest_live_price
      
    if not active_virtual_trades or current_price == 0.0:
      continue
    
    remaining_trades = []
    for trade in active_virtual_trades:
      hit = False
      if trade['type'] == 'BUY':
        if current_price >= trade['tp']:
          profit = 8.0 * lot_size * contract_size
          virtual_balance += profit
          closed_trades.append({'type': 'BUY', 'result': 'WIN', 'pnl': profit})
          send_telegram_message(f"✅ *Virtual BUY Target Hit*\nTP: {trade['tp']} | Price: {current_price}\nProfit: +${profit:.2f}\nNew Balance: ${virtual_balance:.2f}")
          hit = True
        elif current_price <= trade['sl']:
          loss = 4.0 * lot_size * contract_size
          virtual_balance -= loss
          closed_trades.append({'type': 'BUY', 'result': 'LOSS', 'pnl': -loss})
          send_telegram_message(f"❌ *Virtual BUY Stop Loss Hit*\nSL: {trade['sl']} | Price: {current_price}\nLoss: -${loss:.2f}\nNew Balance: ${virtual_balance:.2f}")
          hit = True
      elif trade['type'] == 'SELL':
        if current_price <= trade['tp']:
          profit = 8.0 * lot_size * contract_size
          virtual_balance += profit
          closed_trades.append({'type': 'SELL', 'result': 'WIN', 'pnl': profit})
          send_telegram_message(f"✅ *Virtual SELL Target Hit*\nTP: {trade['tp']} | Price: {current_price}\nProfit: +${profit:.2f}\nNew Balance: ${virtual_balance:.2f}")
          hit = True
        elif current_price >= trade['sl']:
          loss = 4.0 * lot_size * contract_size
          virtual_balance -= loss
          closed_trades.append({'type': 'SELL', 'result': 'LOSS', 'pnl': -loss})
          send_telegram_message(f"❌ *Virtual SELL Stop Loss Hit*\nSL: {trade['sl']} | Price: {current_price}\nLoss: -${loss:.2f}\nNew Balance: ${virtual_balance:.2f}")
          hit = True
      
      if not hit:
        remaining_trades.append(trade)
    
    active_virtual_trades = remaining_trades
    check_balance_threshold()

Thread(target=live_trade_monitor, daemon=True).start()

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
          current_price = latest_live_price if latest_live_price > 0 else 4500.0

        if msg_text == "/balance":
          send_telegram_message(f"💰 *Current Virtual Balance:* ${virtual_balance:.2f}")
        elif msg_text == "/price":
          send_telegram_message(f"⚡ *Current Live Price:* ${current_price:.2f}")
        elif msg_text == "/open":
          send_telegram_message(get_open_trades_details(current_price))
        elif msg_text == "/history":
          send_telegram_message(get_trade_history())
        elif msg_text == "/report":
          send_telegram_message(get_bot_report(current_price))
  except Exception as e:
    print("Webhook Error:", e)
  return "OK", 200

def run_flask():
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)

# --- 2. Market & Bot Logic ---
def fetch_binance_klines(interval):
  interval_map = {"1min": "1m", "15min": "15m"}
  binance_interval = interval_map.get(interval, "1m")
  url = f"https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={binance_interval}&limit=30"
  res = requests.get(url).json()
  
  data = []
  for candle in res:
    data.append({
        "datetime": pd.to_datetime(candle[0], unit='ms'),
        "open": float(candle[1]),
        "high": float(candle[2]),
        "low": float(candle[3]),
        "close": float(candle[4])
    })
  df = pd.DataFrame(data)
  df.set_index("datetime", inplace=True)
  return df

def send_telegram_message(text):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
  requests.post(url, json=payload)

def check_balance_threshold():
  global virtual_balance, low_balance_alert_sent
  if virtual_balance < 2.50 and not low_balance_alert_sent:
    send_telegram_message(
        f"🚨 *CRITICAL WARNING: Low Balance Alert!*\n"
        f"Your virtual balance has dropped to ${virtual_balance:.2f}."
    )
    low_balance_alert_sent = True
  elif virtual_balance >= 2.50:
    low_balance_alert_sent = False

def update_drawdown_metrics(current_price):
  global peak_equity, max_drawdown
  floating_pnl = 0.0
  for trade in active_virtual_trades:
    if trade['status'] == 'ACTIVE':
      if trade['type'] == 'BUY':
        floating_pnl += (current_price - trade['entry']) * lot_size * contract_size
      elif trade['type'] == 'SELL':
        floating_pnl += (trade['entry'] - current_price) * lot_size * contract_size
  equity = virtual_balance + floating_pnl
  if equity > peak_equity:
    peak_equity = equity
  drawdown = peak_equity - equity
  if drawdown > max_drawdown:
    max_drawdown = drawdown

def get_account_status(current_price):
  update_drawdown_metrics(current_price)
  check_balance_threshold()
  floating_pnl = 0.0
  used_margin = 0.0
  active_count = 0
  for trade in active_virtual_trades:
    if trade['status'] == 'ACTIVE':
      active_count += 1
      if trade['type'] == 'BUY':
        floating_pnl += (current_price - trade['entry']) * lot_size * contract_size
      elif trade['type'] == 'SELL':
        floating_pnl += (trade['entry'] - current_price) * lot_size * contract_size
      used_margin += (trade['entry'] * lot_size * contract_size) / leverage
  equity = virtual_balance + floating_pnl
  return (
      f"\n\n📊 *Virtual Account Status*\n"
      f"• Balance: ${virtual_balance:.2f}\n"
      f"• Floating PnL: ${floating_pnl:+.2f}\n"
      f"• Equity: ${equity:.2f}\n"
      f"• Active Trades: {active_count}"
  )

def get_open_trades_details(current_price):
  if not active_virtual_trades:
    return "📭 You currently have no open virtual trades."
  msg = "📈 *Current Open Trades:*\n"
  for idx, trade in enumerate(active_virtual_trades, 1):
    pnl = (current_price - trade['entry']) * lot_size * contract_size if trade['type'] == 'BUY' else (trade['entry'] - current_price) * lot_size * contract_size
    msg += f"\n{idx}. *{trade['type']}* (ACTIVE)\n   • Entry: {trade['entry']} | TP: {trade['tp']} | SL: {trade['sl']}\n   • Open PnL: ${pnl:+.2f}"
  return msg

def get_trade_history():
  if not closed_trades:
    return "📜 No closed trade history available yet."
  msg = "📜 *Closed Trades History:*\n"
  for idx, t in enumerate(closed_trades[-10:], 1):
    msg += f"\n{idx}. {t['type']} | Result: {t['result']} | PnL: ${t['pnl']:+.2f}"
  return msg

def get_bot_report(current_price):
  total_trades = len(closed_trades)
  wins = sum(1 for t in closed_trades if t['result'] == 'WIN')
  win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
  return (
      f"📊 *Bot Performance Report*\n"
      f"• Starting Balance: ${starting_balance:.2f}\n"
      f"• Current Balance: ${virtual_balance:.2f}\n"
      f"• Total Closed Trades: {total_trades}\n"
      f"• Win Rate: {win_rate:.1f}%\n"
      f"• Max Drawdown: ${max_drawdown:.2f}"
  )

def parse_trade_from_text(text, current_price):
  text_lower = text.lower()
  if "no trade" in text_lower:
    return None

  t_type = "BUY" if "buy" in text_lower and "sell" not in text_lower else "SELL" if "sell" in text_lower else None
  if not t_type:
    return None

  entry = current_price
  for line in text.split('\n'):
    if 'entry' in line.lower():
      nums = re.findall(r'[\d.]+', line)
      if nums and float(nums[-1]) > 1000:
        entry = float(nums[-1])

  if t_type == 'BUY':
    sl = entry - 4.0
    tp = entry + 8.0
  else:
    sl = entry + 4.0
    tp = entry - 8.0

  return {'type': t_type, 'entry': entry, 'tp': tp, 'sl': sl, 'status': 'ACTIVE'}

def run_bot_task():
  df_1m = fetch_binance_klines("1min")
  df_15m = fetch_binance_klines("15min")
  
  # Force fresh REST API price check so it never uses old cached/default values
  current_price = get_live_binance_price()
  if current_price == 0.0:
    current_price = df_1m.iloc[-1]['close']

  mpf.plot(df_1m, type="candle", style="charles", savefig=dict(fname="chart_1m.png", dpi=100))
  mpf.plot(df_15m, type="candle", style="charles", savefig=dict(fname="chart_15m.png", dpi=100))

  client = genai.Client(api_key=GEMINI_API_KEY)
  image_1m = client.files.upload(file="chart_1m.png")
  image_15m = client.files.upload(file="chart_15m.png")

  prompt = (
      f"Current Live Price of PAXG/USDT is {current_price}. "
      "Analyze the attached 1m and 15m charts using demand/supply zones, "
      "liquidity reversals, and high-probability momentum. "
      "Provide ONLY the trade signal layout without any analysis or extra words. "
      "Format strictly like this:\n"
      "ACTION: BUY or SELL\n"
      "ENTRY: [exact price]\n"
      "If no high-probability setup meets criteria before market close, output: NO TRADE"
  )

  response = client.models.generate_content(
      model="gemini-3.5-flash-lite", contents=[image_1m, image_15m, prompt]
  )

  new_trade = parse_trade_from_text(response.text, current_price)
  if new_trade:
    active_virtual_trades.append(new_trade)
    send_telegram_message(
        f"📝 *Trade Signal Executed*\n"
        f"Live Price at Execution: ${current_price:.2f}\n"
        f"Type: {new_trade['type']} | Entry: {new_trade['entry']}\n"
        f"TP: {new_trade['tp']} | SL: {new_trade['sl']}"
    )

  open_trades_summary = get_open_trades_details(current_price)
  send_telegram_message(f"⚡ *Live Price:* ${current_price:.2f}\n\n" + response.text + "\n\n" + open_trades_summary + get_account_status(current_price))

def background_scheduler():
  while True:
    try:
      run_bot_task()
    except Exception as e:
      print("Error:", e)
    time.sleep(900)

if __name__ == "__main__":
  flask_thread = Thread(target=run_flask)
  flask_thread.start()
  background_scheduler()
