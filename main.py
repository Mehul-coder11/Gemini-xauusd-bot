import os
import time
import traceback
from datetime import datetime, timezone
from threading import Thread
import matplotlib

matplotlib.use('Agg')
import mplfinance as mpf
import pandas as pd
from flask import Flask, request
from google import genai
import requests
import re

# Load Environment Variables
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- Virtual Tracker Settings & State ---
starting_balance = 20.0
virtual_balance = 20.0
leverage = 1000
lot_size = 0.01
contract_size = 100  # 1 standard lot of XAU/USD = 100 ounces
active_virtual_trades = []
closed_trades = []
peak_equity = 20.0
max_drawdown = 0.0
low_balance_alert_sent = False

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
      
      # Only respond to authorized chat
      if chat_id == str(TELEGRAM_CHAT_ID):
        df_temp = fetch_chart_data("1min")
        current_price = df_temp.iloc[0]['close']
        
        if msg_text == "/balance":
          send_telegram_message(f"💰 *Current Virtual Balance:* ${virtual_balance:.2f}")
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
def is_market_open():
  try:
    url = f"https://api.twelvedata.com/market_state?apikey={TWELVE_DATA_API_KEY}"
    response = requests.get(url).json()
    for item in response.get("forex", []):
      if item.get("symbol") == "XAU/USD":
        return item.get("is_open", False)
    now = datetime.now(timezone.utc)
    if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 22):
      return False
    if now.weekday() == 4 and now.hour >= 21:
      return False
    return True
  except Exception:
    now = datetime.now(timezone.utc)
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

  cols_to_float = ["open", "high", "low", "close"]
  if "volume" in df.columns:
    cols_to_float.append("volume")
  for col in cols_to_float:
    df[col] = df[col].astype(float)

  return df.iloc[::-1]


def send_telegram_message(text):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
  requests.post(url, json=payload)


def send_telegram_photos(caption1, caption2):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
  files = {
      "photo1": open("chart_1m.png", "rb"),
      "photo2": open("chart_15m.png", "rb"),
  }
  media = [
      {"type": "photo", "media": "attach://photo1", "caption": caption1},
      {"type": "photo", "media": "attach://photo2", "caption": caption2},
  ]
  data = {"chat_id": TELEGRAM_CHAT_ID, "media": str(media).replace("'", '"')}
  requests.post(url, data=data, files=files)


def check_balance_threshold():
  global virtual_balance, low_balance_alert_sent
  if virtual_balance < 2.50 and not low_balance_alert_sent:
    send_telegram_message(
        f"🚨 *CRITICAL WARNING: Low Balance Alert!*\n"
        f"Your virtual balance has dropped to ${virtual_balance:.2f} (below $2.50)."
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
  global virtual_balance, active_virtual_trades
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
  margin_level = (equity / used_margin * 100) if used_margin > 0 else 0.0
  
  status_text = (
      f"\n\n📊 *Virtual Account Status*\n"
      f"• Balance: ${virtual_balance:.2f}\n"
      f"• Floating PnL: ${floating_pnl:+.2f}\n"
      f"• Equity: ${equity:.2f}\n"
      f"• Used Margin: ${used_margin:.2f}\n"
      f"• Margin Level: {margin_level:.1f}%\n"
      f"• Active Trades: {active_count}"
  )
  return status_text


def get_open_trades_details(current_price):
  if not active_virtual_trades:
    return "📭 You currently have no open virtual trades."
  
  msg = "📈 *Current Open Trades:*\n"
  for idx, trade in enumerate(active_virtual_trades, 1):
    pnl = 0.0
    if trade['type'] == 'BUY':
      pnl = (current_price - trade['entry']) * lot_size * contract_size
    else:
      pnl = (trade['entry'] - current_price) * lot_size * contract_size
    margin = (trade['entry'] * lot_size * contract_size) / leverage
    msg += f"\n{idx}. *{trade['type']}* (ACTIVE)\n   • Entry: {trade['entry']} | TP: {trade['tp']} | SL: {trade['sl']}\n   • Margin Used: ${margin:.2f} | Open PnL: ${pnl:+.2f}"
  return msg


def get_trade_history():
  if not closed_trades:
    return "📜 No closed trade history available yet."
  
  msg = "📜 *Previous Closed Trades History:*\n"
  for idx, t in enumerate(closed_trades[-10:], 1):
    msg += f"\n{idx}. {t['type']} | Result: {t['result']} | PnL: ${t['pnl']:+.2f}"
  return msg


def get_bot_report(current_price):
  total_trades = len(closed_trades)
  wins = sum(1 for t in closed_trades if t['result'] == 'WIN')
  win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
  net_increase = virtual_balance - starting_balance
  
  return (
      f"📊 *Bot Performance Report*\n"
      f"• Starting Balance: ${starting_balance:.2f}\n"
      f"• Current Balance: ${virtual_balance:.2f}\n"
      f"• Net Increase: ${net_increase:+.2f}\n"
      f"• Total Closed Trades: {total_trades}\n"
      f"• Win Rate: {win_rate:.1f}%\n"
      f"• Max Drawdown: ${max_drawdown:.2f}"
  )


def track_virtual_trades(df_1m):
  global virtual_balance, active_virtual_trades, closed_trades
  if not active_virtual_trades:
    return

  remaining_trades = []
  for trade in active_virtual_trades:
    hit = False
    # Strictly checking 1-minute chart candles high/low for TP/SL hits
    for index, candle in df_1m.iterrows():
      high = candle['high']
      low = candle['low']

      if trade['type'] == 'BUY':
        if high >= trade['tp']:
          profit = (trade['tp'] - trade['entry']) * lot_size * contract_size
          virtual_balance += profit
          closed_trades.append({'type': 'BUY', 'result': 'WIN', 'pnl': profit})
          send_telegram_message(f"✅ *Virtual BUY Target Hit (1m Chart)*\nTP: {trade['tp']} | Profit: +${profit:.2f}\nNew Balance: ${virtual_balance:.2f}")
          hit = True
          break
        elif low <= trade['sl']:
          loss = (trade['entry'] - trade['sl']) * lot_size * contract_size
          virtual_balance -= loss
          closed_trades.append({'type': 'BUY', 'result': 'LOSS', 'pnl': -loss})
          send_telegram_message(f"❌ *Virtual BUY Stop Loss Hit (1m Chart)*\nSL: {trade['sl']} | Loss: -${loss:.2f}\nNew Balance: ${virtual_balance:.2f}")
          hit = True
          break
      elif trade['type'] == 'SELL':
        if low <= trade['tp']:
          profit = (trade['entry'] - trade['tp']) * lot_size * contract_size
          virtual_balance += profit
          closed_trades.append({'type': 'SELL', 'result': 'WIN', 'pnl': profit})
          send_telegram_message(f"✅ *Virtual SELL Target Hit (1m Chart)*\nTP: {trade['tp']} | Profit: +${profit:.2f}\nNew Balance: ${virtual_balance:.2f}")
          hit = True
          break
        elif high >= trade['sl']:
          loss = (trade['sl'] - trade['entry']) * lot_size * contract_size
          virtual_balance -= loss
          closed_trades.append({'type': 'SELL', 'result': 'LOSS', 'pnl': -loss})
          send_telegram_message(f"❌ *Virtual SELL Stop Loss Hit (1m Chart)*\nSL: {trade['sl']} | Loss: -${loss:.2f}\nNew Balance: ${virtual_balance:.2f}")
          hit = True
          break

    if not hit:
      remaining_trades.append(trade)

  active_virtual_trades = remaining_trades
  check_balance_threshold()


def parse_trade_from_text(text, current_price):
  text_lower = text.lower()
  if "no trade" in text_lower or ("trade:" in text_lower and "none" in text_lower):
    return None

  if "buy" in text_lower and "sell" not in text_lower:
    t_type = "BUY"
  elif "sell" in text_lower and "buy" not in text_lower:
    t_type = "SELL"
  elif "buy" in text_lower:
    t_type = "BUY"
  elif "sell" in text_lower:
    t_type = "SELL"
  else:
    return None

  entry = current_price
  tp = 0.0
  sl = 0.0

  for line in text.split('\n'):
    line_lower = line.lower()
    if ('entry' in line_lower or 'at' in line_lower) and ('price' in line_lower or 'entry' in line_lower):
      nums = re.findall(r'[\d.]+', line)
      if nums:
        try:
          val = float(nums[-1])
          if val > 1000:
            entry = val
        except:
          pass
    elif 'tp' in line_lower or 'take profit' in line_lower:
      nums = re.findall(r'[\d.]+', line)
      if nums:
        try:
          tp = float(nums[-1])
        except:
          pass
    elif 'sl' in line_lower or 'stop loss' in line_lower:
      nums = re.findall(r'[\d.]+', line)
      if nums:
        try:
          sl = float(nums[-1])
        except:
          pass

  if tp == 0.0:
    m = re.search(r'(?:tp|take\s*profit)[:\s]*([\d.]+)', text, re.IGNORECASE)
    if m:
      try:
        tp = float(m.group(1))
      except:
        pass
  if sl == 0.0:
    m = re.search(r'(?:sl|stop\s*loss)[:\s]*([\d.]+)', text, re.IGNORECASE)
    if m:
      try:
        sl = float(m.group(1))
      except:
        pass

  if tp == 0.0:
    tp = current_price + 5.0 if t_type == 'BUY' else current_price - 5.0
  if sl == 0.0:
    sl = current_price - 5.0 if t_type == 'BUY' else current_price + 5.0

  return {
      'type': t_type,
      'entry': entry,
      'tp': tp,
      'sl': sl,
      'status': 'ACTIVE'
  }


def run_bot_task():
  print("Generating charts...")
  df_1m = fetch_chart_data("1min")
  df_15m = fetch_chart_data("15min")
  current_price = df_1m.iloc[0]['close']

  # 1. Check existing open trades against 1-minute chart for TP/SL hits first
  track_virtual_trades(df_1m)

  mpf.plot(
      df_1m,
      type="candle",
      style="charles",
      savefig="chart_1m.png",
      title="XAU/USD 1m",
  )
  mpf.plot(
      df_15m,
      type="candle",
      style="charles",
      savefig="chart_15m.png",
      title="XAU/USD 15m",
  )

  client = genai.Client(api_key=GEMINI_API_KEY)
  image_1m = client.files.upload(file="chart_1m.png")
  image_15m = client.files.upload(file="chart_15m.png")

  prompt = (
      "Assume you are a professional and this is the data of live XAU/USD, "
      "with 1 minute chart and 15 minute chart. Read the current price directly "
      "from the price axis and latest candles on the chart (do not use generic "
      "estimates or old data). Analyze it carefully and give me a trade in XAU/USD "
      "only when you have confidence of 60 to 70 percent or more; otherwise say "
      "no trade. When there is a trade, tell what to do like buy or sell, state "
      "the exact current price shown on the chart, at what price to enter the trade, "
      "and what should be the take profit and SL. Make sure that you only give "
      "trades in which the TP is double than the SL, and for this also make sure "
      "that only give TP which will be achieved before today market close, "
      "also keep in mind that I don't want logic only give me either the trade or no trade"
  )

  response = client.models.generate_content(
      model="gemini-3.5-flash-lite", contents=[image_1m, image_15m, prompt]
  )

  print("AI Response:", response.text[:200])

  # 2. Evaluate for a new trade and append to active list if found
  new_trade = parse_trade_from_text(response.text, current_price)
  if new_trade:
    active_virtual_trades.append(new_trade)
    send_telegram_message(
        f"📝 *Active Order Opened* ({lot_size} Lot | 1:{leverage} Lev):\n"
        f"Type: {new_trade['type']} | Entry: {new_trade['entry']} | TP: {new_trade['tp']} | SL: {new_trade['sl']}"
    )

  # 3. Include current open trades summary with open PnL & account status
  open_trades_summary = get_open_trades_details(current_price)
  send_telegram_message(response.text + "\n\n" + open_trades_summary + get_account_status(current_price))
  send_telegram_photos("1-Minute Chart", "15-Minute Chart")
  print("Task executed and sent successfully!")


def background_scheduler():
  while True:
    try:
      if is_market_open():
        run_bot_task()
      else:
        print("Market is closed. Skipping execution.")
    except Exception as e:
      print("Error in background task:", e)
      traceback.print_exc()
    time.sleep(900)


if __name__ == "__main__":
  send_telegram_message(
      "🚀 *XAU/USD Bot Web Service* has successfully started and is running!\n\n"
      "Send commands anytime:\n"
      "/balance - View account balance\n"
      "/open - View open trades & margin\n"
      "/history - View past trade results\n"
      "/report - View win rate, net increase & drawdown"
  )

  flask_thread = Thread(target=run_flask)
  flask_thread.start()

  bot_thread = Thread(target=background_scheduler)
  bot_thread.start()
