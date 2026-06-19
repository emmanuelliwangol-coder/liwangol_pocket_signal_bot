\"\"\"
╔══════════════════════════════════════════════════╗
║   POCKET OPTION SIGNAL BOT — SMC EDITION        ║
║   Signals: FVG + Liquidity Sweep + MSS + EMA    ║
║   Commands: /start /stats /pairs /help /pause   ║
╚══════════════════════════════════════════════════╝
\"\"\"

import asyncio
import json
import os
import logging
import schedule
import time
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf
import pandas as pd
import numpy as np
import ta
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ──────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "8628123105:AAGRCl-k3O-0xXfI7fHgWoonvaN1Q8F_pRU")
CHAT_ID     = os.getenv("CHAT_ID",   "8494805451")
EXPIRY_MIN  = 5
SCAN_EVERY  = 5
MIN_SCORE   = 3
STATS_FILE  = "stats.json"

PAIRS = {
    "XAUUSD": "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "BTCUSD": "BTC-USD",
}

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
# STATS MANAGER
# ──────────────────────────────────────────────────
class StatsManager:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        if Path(STATS_FILE).exists():
            with open(STATS_FILE) as f:
                return json.load(f)
        return {"total": 0, "wins": 0, "losses": 0, "pending": [],
                "pairs": {}, "daily": {}, "streak": 0, "best_streak": 0}

    def _save(self):
        with open(STATS_FILE, "w") as f:
            json.dump(self.data, f, indent=2, default=str)

    def add_signal(self, symbol, direction, price, score):
        entry_time = datetime.utcnow()
        self.data["pending"].append({
            "symbol": symbol, "direction": direction,
            "entry_price": price, "score": score,
            "entry_time": entry_time.isoformat(),
            "expiry_time": (entry_time + timedelta(minutes=EXPIRY_MIN)).isoformat(),
        })
        self._save()

    def record_result(self, symbol, win):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        self.data["total"] += 1
        if win:
            self.data["wins"] += 1
            self.data["streak"] = self.data.get("streak", 0) + 1
        else:
            self.data["losses"] += 1
            self.data["streak"] = 0
        if self.data["streak"] > self.data.get("best_streak", 0):
            self.data["best_streak"] = self.data["streak"]
        self.data["pairs"].setdefault(symbol, {"wins": 0, "losses": 0})
        self.data["pairs"][symbol]["wins" if win else "losses"] += 1
        self.data["daily"].setdefault(today, {"wins": 0, "losses": 0})
        self.data["daily"][today]["wins" if win else "losses"] += 1
        self._save()

    def get_win_rate(self):
        if self.data["total"] == 0:
            return 0.0
        return round((self.data["wins"] / self.data["total"]) * 100, 1)

    def get_today_stats(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return self.data["daily"].get(today, {"wins": 0, "losses": 0})

    def get_best_pair(self):
        pairs = self.data.get("pairs", {})
        if not pairs:
            return "N/A"
        return max(pairs, key=lambda p: pairs[p].get("wins", 0) /
                   max(pairs[p].get("wins", 0) + pairs[p].get("losses", 0), 1))

    def _winrate_bar(self, wr):
        filled = int(wr / 10)
        return "🟩" * filled + "⬜" * (10 - filled)

    def format_stats(self):
        d = self.data
        wr = self.get_win_rate()
        today = self.get_today_stats()
        pairs_block = ""
        for sym, rec in d.get("pairs", {}).items():
            total = rec["wins"] + rec["losses"]
            rate = round(rec["wins"] / total * 100, 1) if total else 0
            pairs_block += f"  • {sym}: {rec['wins']}W/{rec['losses']}L ({rate}%)\n"
        return (
            f"📊 *BOT PERFORMANCE STATS*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *Overall Win Rate*\n"
            f"{self._winrate_bar(wr)} `{wr}%`\n\n"
            f"📈 Total: `{d['total']}`  ✅ Wins: `{d['wins']}`  ❌ Losses: `{d['losses']}`\n\n"
            f"📅 *Today*: ✅ {today['wins']}W  ❌ {today['losses']}L\n\n"
            f"🔥 Streak: `{d.get('streak', 0)}`  🥇 Best: `{d.get('best_streak', 0)}`\n"
            f"💎 Best Pair: `{self.get_best_pair()}`\n\n"
            f"📊 *Per Pair*\n{pairs_block}"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Updated: {datetime.utcnow().strftime('%H:%M UTC')}_"
        )

# ──────────────────────────────────────────────────
# SMC ANALYSIS ENGINE
# ──────────────────────────────────────────────────
class SMCAnalyzer:

    def fetch_data(self, ticker):
        try:
            df = yf.download(ticker, period="3d", interval="5m",
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 60:
                return None
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            return df
        except Exception as e:
            log.warning(f"Fetch error {ticker}: {e}")
            return None

    def detect_fvg(self, df):
        if len(df) < 4:
            return False, False
        bull = bool(df["Low"].iloc[-2] > df["High"].iloc[-4])
        bear = bool(df["High"].iloc[-2] < df["Low"].iloc[-4])
        return bull, bear

    def detect_liquidity_sweep(self, df):
        if len(df) < 25:
            return False, False
        lookback = df.iloc[-23:-2]
        recent_high = lookback["High"].max()
        recent_low  = lookback["Low"].min()
        prev, last = df.iloc[-2], df.iloc[-1]
        swept_low  = bool((prev["Low"]  <= recent_low  * 1.0002) and (last["Close"] > recent_low))
        swept_high = bool((prev["High"] >= recent_high * 0.9998) and (last["Close"] < recent_high))
        return swept_low, swept_high

    def detect_mss(self, df):
        if len(df) < 12:
            return False, False
        swing = df.iloc[-12:-2]
        last_close = float(df["Close"].iloc[-1])
        return bool(last_close > swing["High"].max()), bool(last_close < swing["Low"].min())

    def detect_order_block(self, df):
        if len(df) < 6:
            return False, False
        r = df.iloc[-6:-1]
        bull = bool(r["Close"].iloc[0] < r["Open"].iloc[0] and
                    r["Close"].iloc[-1] > r["Open"].iloc[-1] and
                    (r["Close"].iloc[-1] - r["Open"].iloc[-1]) >
                    (r["High"].iloc[0] - r["Low"].iloc[0]) * 0.5)
        bear = bool(r["Close"].iloc[0] > r["Open"].iloc[0] and
                    r["Close"].iloc[-1] < r["Open"].iloc[-1] and
                    (r["Open"].iloc[-1] - r["Close"].iloc[-1]) >
                    (r["High"].iloc[0] - r["Low"].iloc[0]) * 0.5)
        return bull, bear

    def get_indicator_bias(self, df):
        close = df["Close"].squeeze()

        ema8  = ta.trend.EMAIndicator(close, window=8).ema_indicator()
        ema21 = ta.trend.EMAIndicator(close, window=21).ema_indicator()
        rsi   = ta.momentum.RSIIndicator(close, window=14).rsi()
        macd_obj = ta.trend.MACD(close)
        macd_line   = macd_obj.macd()
        signal_line = macd_obj.macd_signal()

        last_ema8  = float(ema8.iloc[-1])
        last_ema21 = float(ema21.iloc[-1])
        prev_ema8  = float(ema8.iloc[-2])
        prev_ema21 = float(ema21.iloc[-2])
        last_rsi   = float(rsi.iloc[-1])
        last_macd  = float(macd_line.iloc[-1])
        last_sig   = float(signal_line.iloc[-1])
        last_close = float(close.iloc[-1])

        bull_ema  = (prev_ema8 < prev_ema21) and (last_ema8 > last_ema21)
        bear_ema  = (prev_ema8 > prev_ema21) and (last_ema8 < last_ema21)
        bull_rsi  = last_rsi > 50
        bear_rsi  = last_rsi < 50
        bull_macd = last_macd > last_sig
        bear_macd = last_macd < last_sig

        if bull_ema and bull_rsi and bull_macd:
            bias = "CALL"
        elif bear_ema and bear_rsi and bear_macd:
            bias = "PUT"
        else:
            bias = None

        return bias, {"rsi": round(last_rsi, 1), "price": last_close,
                      "ema8": round(last_ema8, 5), "ema21": round(last_ema21, 5)}

    def analyze(self, symbol, ticker):
        df = self.fetch_data(ticker)
        if df is None:
            return None
        try:
            bias, meta = self.get_indicator_bias(df)
        except Exception as e:
            log.warning(f"Indicator error {symbol}: {e}")
            return None
        if bias is None:
            return None

        fvg_bull,  fvg_bear  = self.detect_fvg(df)
        liq_bull,  liq_bear  = self.detect_liquidity_sweep(df)
        mss_bull,  mss_bear  = self.detect_mss(df)
        ob_bull,   ob_bear   = self.detect_order_block(df)

        if bias == "CALL":
            hits = [fvg_bull, liq_bull, mss_bull, ob_bull]
            tags = ["📦 FVG Bullish", "💧 Liquidity Sweep Low", "📐 MSS Bullish", "🧱 Order Block Bull"]
            emoji = "✅ CALL"
        else:
            hits = [fvg_bear, liq_bear, mss_bear, ob_bear]
            tags = ["📦 FVG Bearish", "💧 Liquidity Sweep High", "📐 MSS Bearish", "🧱 Order Block Bear"]
            emoji = "❌ PUT"

        active = [tags[i] for i, h in enumerate(hits) if h]
        score  = len(active) + 1

        if score < MIN_SCORE:
            log.info(f"{symbol} score {score} < {MIN_SCORE} — skip")
            return None

        return {"symbol": symbol, "direction": emoji, "raw_dir": bias,
                "price": round(meta["price"], 5), "rsi": meta["rsi"],
                "score": score, "smc_tags": active}

# ──────────────────────────────────────────────────
# SIGNAL FORMATTER
# ──────────────────────────────────────────────────
def format_signal(sig):
    now    = datetime.utcnow()
    expiry = (now + timedelta(minutes=EXPIRY_MIN)).strftime("%H:%M UTC")
    lines  = "\n".join(f"    {t}" for t in sig["smc_tags"]) or "    (Base indicators)"
    stars  = "⭐" * sig["score"]
    
    # Calculate SL and 3 TP levels
    entry = sig['price']
    if sig['direction'] == "✅ CALL":
        sl = round(entry * 0.9990, 5)
        tp1 = round(entry * 1.0005, 5)
        tp2 = round(entry * 1.0010, 5)
        tp3 = round(entry * 1.0015, 5)
    else:
        sl = round(entry * 1.0010, 5)
        tp1 = round(entry * 0.9995, 5)
        tp2 = round(entry * 0.9990, 5)
        tp3 = round(entry * 0.9985, 5)
    
    return (
        f"🚨 *POCKET OPTION SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{sig['symbol']}`\n"
        f"🎯 Direction: *{sig['direction']}*\n"
        f"🎯 *Sniper Entry*: `{entry}`\n"
        f"⛔ *Stop Loss (SL)*: `{sl}`\n"
        f"✅ *TP1*: `{tp1}` | *TP2*: `{tp2}` | *TP3*: `{tp3}`\n"
        f"⏱ Expiry: *{EXPIRY_MIN} min* → `{expiry}`\n"
        f"📊 RSI: `{sig['rsi']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 *SMC Confluence* {stars}\n"
        f"{lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_"
    )

# ──────────────────────────────────────────────────
# BOT STATE
# ──────────────────────────────────────────────────
bot_paused   = False
stats        = StatsManager()
analyzer     = SMCAnalyzer()
telegram_bot = None

# ──────────────────────────────────────────────────
# SCANNER
# ──────────────────────────────────────────────────
async def scan_and_send():
    if bot_paused or telegram_bot is None:
        return
    log.info("🔍 Scanning markets...")
    sent = 0
    for symbol, ticker in PAIRS.items():
        sig = analyzer.analyze(symbol, ticker)
        if sig:
            try:
                await telegram_bot.send_message(
                    chat_id=CHAT_ID, text=format_signal(sig), parse_mode="Markdown"
                )
                stats.add_signal(sig["symbol"], sig["raw_dir"], sig["price"], sig["score"])
                sent += 1
                log.info(f"✅ Signal: {symbol} {sig['direction']} score={sig['score']}")
                await asyncio.sleep(1.5)
            except Exception as e:
                log.error(f"Send error {symbol}: {e}")
    if sent == 0:
        log.info("No qualifying signals this scan.")

# ──────────────────────────────────────────────────
# COMMAND HANDLERS
# ──────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Pocket Option Signal Bot — SMC Edition*\n\n"
        "Scanning markets every 5 min using:\n"
        "• Fair Value Gap (FVG)\n"
        "• Liquidity Sweep\n"
        "• Market Structure Shift (MSS)\n"
        "• Order Block\n"
        "• EMA + RSI + MACD\n\n"
        "Use /help to see all commands.", parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Commands*\n\n"
        "/start — Welcome\n/stats — Performance report\n"
        "/pairs — Active pairs\n/scan — Force scan now\n"
        "/win — Record WIN\n/loss — Record LOSS\n"
        "/pause — Pause signals\n/resume — Resume signals\n"
        "/help — This message", parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats.format_stats(), parse_mode="Markdown")

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = "\n".join(f"  • `{s}`" for s in PAIRS)
    await update.message.reply_text(f"📈 *Active Pairs*\n{lines}", parse_mode="Markdown")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning now...", parse_mode="Markdown")
    await scan_and_send()
    await update.message.reply_text("✅ Scan complete.", parse_mode="Markdown")

async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text("No pending signals.")
        return
    last = pending.pop()
    stats.record_result(last["symbol"], win=True)
    stats._save()
    await update.message.reply_text(
        f"✅ WIN recorded for `{last['symbol']}`\nWin rate: `{stats.get_win_rate()}%`",
        parse_mode="Markdown")

async def cmd_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text("No pending signals.")
        return
    last = pending.pop()
    stats.record_result(last["symbol"], win=False)
    stats._save()
    await update.message.reply_text(
        f"❌ LOSS recorded for `{last['symbol']}`\nWin rate: `{stats.get_win_rate()}%`",
        parse_mode="Markdown")

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = True
    await update.message.reply_text("⏸ Scanning paused. Use /resume to restart.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = False
    await update.message.reply_text("▶️ Scanning resumed.")

# ──────────────────────────────────────────────────
# SCHEDULER
# ──────────────────────────────────────────────────
def run_scheduler(loop):
    def job():
        asyncio.run_coroutine_threadsafe(scan_and_send(), loop)
    schedule.every(SCAN_EVERY).minutes.do(job)
    log.info(f"Scheduler started — every {SCAN_EVERY} min")
    while True:
        schedule.run_pending()
        time.sleep(10)

# ──────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────
def main():
    global telegram_bot
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_bot = app.bot

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("pairs",  cmd_pairs))
    app.add_handler(CommandHandler("scan",   cmd_scan))
    app.add_handler(CommandHandler("win",    cmd_win))
    app.add_handler(CommandHandler("loss",   cmd_loss))
    app.add_handler(CommandHandler("pause",  cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))

    import threading
    loop = asyncio.get_event_loop()
    t = threading.Thread(target=run_scheduler, args=(loop,), daemon=True)
    t.start()

    log.info("🤖 Bot is online!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
