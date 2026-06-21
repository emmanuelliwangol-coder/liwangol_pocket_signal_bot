"""
╔══════════════════════════════════════════════════╗
║   POCKET OPTION SIGNAL BOT — SMC PRO EDITION   ║
║   Data: Twelve Data API                         ║
║   Expiry: 3 min | Sessions: London + NY         ║
║   MODE: Single-Pair Focus (User-Controlled)     ║
╚══════════════════════════════════════════════════╝
"""

import asyncio
import json
import os
import logging
import requests
import ta
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ──────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN", "8628123105:AAGRCl-k3O-0xXfI7fHgWoonvaN1Q8F_pRU")
CHAT_ID    = os.getenv("CHAT_ID",   "8494805451")
TD_API_KEY = os.getenv("TD_API_KEY","310a0ed4468144a09c38b2687369f314")
EXPIRY_MIN = 3
SCAN_EVERY = 3
MIN_SCORE  = 2
STATS_FILE = "stats.json"

PAIRS = {
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "BTCUSD": "BTC/USD",
}

SESSIONS = [
    (8, 12),   # London
    (13, 17),  # New York
]

# ──────────────────────────────────────────────────
# STATE — tracks selected pair per user
# ──────────────────────────────────────────────────
# Key: chat_id (str), Value: pair key e.g. "EURUSD"
user_selected_pair: dict[str, str] = {}

# ──────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# SESSION HELPERS
# ──────────────────────────────────────────────────
def is_active_session():
    hour = datetime.utcnow().hour
    return any(s <= hour < e for s, e in SESSIONS)

def session_name():
    hour = datetime.utcnow().hour
    if 8  <= hour < 12: return "🇬🇧 London Session"
    if 13 <= hour < 17: return "🇺🇸 New York Session"
    return "😴 Off-Session"

# ──────────────────────────────────────────────────
# TWELVE DATA FETCHER
# ──────────────────────────────────────────────────
def fetch_candles(symbol: str, interval="1min", outputsize=100) -> pd.DataFrame | None:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     TD_API_KEY,
        "format":     "JSON",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if "values" not in data:
            log.warning(f"No values for {symbol}: {data.get('message','')}")
            return None
        df = pd.DataFrame(data["values"])
        df = df.rename(columns={
            "open": "Open", "high": "High",
            "low":  "Low",  "close": "Close",
            "volume": "Volume"
        })
        for col in ["Open","High","Low","Close"]:
            df[col] = pd.to_numeric(df[col])
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        log.warning(f"Fetch error {symbol}: {e}")
        return None

# ──────────────────────────────────────────────────
# SL / TP CALCULATOR
# ──────────────────────────────────────────────────
def calculate_sl_tp(entry: float, direction: str, symbol: str):
    if symbol == "XAUUSD":
        sl_pct = 0.0015; tp_pcts = [0.0008, 0.0015, 0.0025]
    elif symbol == "BTCUSD":
        sl_pct = 0.0030; tp_pcts = [0.0015, 0.0030, 0.0050]
    else:
        sl_pct = 0.0010; tp_pcts = [0.0005, 0.0010, 0.0015]

    if direction == "CALL":
        sl  = round(entry * (1 - sl_pct), 5)
        tp1 = round(entry * (1 + tp_pcts[0]), 5)
        tp2 = round(entry * (1 + tp_pcts[1]), 5)
        tp3 = round(entry * (1 + tp_pcts[2]), 5)
    else:
        sl  = round(entry * (1 + sl_pct), 5)
        tp1 = round(entry * (1 - tp_pcts[0]), 5)
        tp2 = round(entry * (1 - tp_pcts[1]), 5)
        tp3 = round(entry * (1 - tp_pcts[2]), 5)
    return sl, tp1, tp2, tp3

# ──────────────────────────────────────────────────
# STATS MANAGER
# ──────────────────────────────────────────────────
class StatsManager:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        if Path(STATS_FILE).exists():
            with open(STATS_FILE) as f:
                return json.load(f)
        return {"wins": 0, "losses": 0, "total": 0}

    def save(self):
        with open(STATS_FILE, "w") as f:
            json.dump(self.data, f)

    def summary(self):
        t = self.data["total"]
        w = self.data["wins"]
        wr = (w / t * 100) if t else 0
        return f"📊 Stats: {w}W / {self.data['losses']}L | {t} trades | WR: {wr:.1f}%"

stats = StatsManager()

# ──────────────────────────────────────────────────
# SIGNAL ANALYSIS
# ──────────────────────────────────────────────────
def analyse_pair(pair_key: str) -> dict | None:
    symbol = PAIRS[pair_key]
    df = fetch_candles(symbol)
    if df is None or len(df) < 50:
        return None

    score = 0
    direction = None

    close = df["Close"]
    ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator()
    rsi   = ta.momentum.RSIIndicator(close, window=14).rsi()

    last_close = close.iloc[-1]
    last_ema   = ema20.iloc[-1]
    last_rsi   = rsi.iloc[-1]

    if last_close > last_ema:
        score += 1; direction = "CALL"
    else:
        score += 1; direction = "PUT"

    if direction == "CALL" and last_rsi < 70:
        score += 1
    elif direction == "PUT" and last_rsi > 30:
        score += 1

    if score < MIN_SCORE:
        return None

    entry = last_close
    sl, tp1, tp2, tp3 = calculate_sl_tp(entry, direction, pair_key)

    return {
        "pair":      pair_key,
        "symbol":    symbol,
        "direction": direction,
        "score":     score,
        "entry":     entry,
        "sl":        sl,
        "tp1":       tp1,
        "tp2":       tp2,
        "tp3":       tp3,
        "rsi":       round(last_rsi, 2),
        "session":   session_name(),
        "time":      datetime.utcnow().strftime("%H:%M UTC"),
    }

def format_signal(s: dict) -> str:
    arrow = "🟢 CALL ▲" if s["direction"] == "CALL" else "🔴 PUT ▼"
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 *SIGNAL ALERT*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair    : `{s['symbol']}`\n"
        f"📌 Signal  : *{arrow}*\n"
        f"⏱ Expiry  : `{EXPIRY_MIN} minutes`\n"
        f"🕐 Time    : `{s['time']}`\n"
        f"📍 Session : {s['session']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Entry   : `{s['entry']}`\n"
        f"🛑 SL      : `{s['sl']}`\n"
        f"🎯 TP1     : `{s['tp1']}`\n"
        f"🎯 TP2     : `{s['tp2']}`\n"
        f"🎯 TP3     : `{s['tp3']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Score   : `{s['score']}/5`\n"
        f"📈 RSI     : `{s['rsi']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

# ──────────────────────────────────────────────────
# COMMANDS
# ──────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    current = user_selected_pair.get(chat_id, "Not set")
    await update.message.reply_text(
        f"👋 *Pocket Option Signal Bot*\n\n"
        f"🔍 Current pair: `{current}`\n\n"
        f"Commands:\n"
        f"`/setpair` — Choose which pair to monitor\n"
        f"`/signal` — Get signal for current pair\n"
        f"`/status` — Show active pair & session\n"
        f"`/stats` — Win/Loss stats\n"
        f"`/stop` — Stop signals",
        parse_mode="Markdown"
    )

async def cmd_setpair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"{v} ({k})", callback_data=f"setpair_{k}")]
        for k, v in PAIRS.items()
    ]
    await update.message.reply_text(
        "📊 *Select the pair you want signals for:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def callback_setpair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    pair_key = query.data.replace("setpair_", "")
    user_selected_pair[chat_id] = pair_key
    await query.edit_message_text(f"✅ *Pair set to: {PAIRS[pair_key]}*", parse_mode="Markdown")

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    pair_key = context.args[0].upper() if context.args else user_selected_pair.get(chat_id)
    
    if not pair_key or pair_key not in PAIRS:
        await update.message.reply_text("⚠️ No valid pair selected. Use `/setpair`.")
        return

    signal = analyse_pair(pair_key)
    if signal:
        await update.message.reply_text(format_signal(signal), parse_mode="Markdown")
    else:
        await update.message.reply_text("⏳ No strong signal right now.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    pair_key = user_selected_pair.get(chat_id)
    pair_display = f"`{PAIRS[pair_key]}`" if pair_key else "❌ Not set"
    await update.message.reply_text(f"📡 *Bot Status*\n\n💱 Active pair : {pair_display}\n🕐 Session : {session_name()}", parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats.summary(), parse_mode="Markdown")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_selected_pair.pop(chat_id, None)
    await update.message.reply_text("🛑 Signals stopped.")

# ──────────────────────────────────────────────────
# AUTO SCANNER
# ──────────────────────────────────────────────────
async def auto_scan(app: Application):
    while True:
        await asyncio.sleep(SCAN_EVERY * 60)
        if not is_active_session() or not user_selected_pair:
            continue
        
        pair_to_users = {}
        for chat_id, pair_key in user_selected_pair.items():
            pair_to_users.setdefault(pair_key, []).append(chat_id)

        for pair_key, chat_ids in pair_to_users.items():
            signal = analyse_pair(pair_key)
            if signal:
                msg = format_signal(signal)
                for chat_id in chat_ids:
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    except Exception as e:
                        log.error(f"Failed to send to {chat_id}: {e}")

# ──────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("setpair", cmd_setpair))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CallbackQueryHandler(callback_setpair, pattern="^setpair_"))

    async def post_init(app: Application):
        asyncio.create_task(auto_scan(app))

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
