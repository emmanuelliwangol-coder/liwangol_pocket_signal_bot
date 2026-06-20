"""
╔══════════════════════════════════════════════════╗
║   POCKET OPTION SIGNAL BOT — SMC PRO EDITION   ║
║   Optimized: XAUUSD/EURUSD/GBPUSD/BTC/USDJPY  ║
║   Expiry: 3 min | Sessions: London + NY         ║
║   Features: SL/TP + SMC + HTF Bias + JobQueue  ║
╚══════════════════════════════════════════════════╝
"""

import asyncio
import json
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf
import pandas as pd
import numpy as np
import ta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ──────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN", "8628123105:AAGRCl-k3O-0xXfI7fHgWoonvaN1Q8F_pRU")
CHAT_ID    = os.getenv("CHAT_ID",   "8494805451")
EXPIRY_MIN = 3        # 3 minute expiry (your preference)
SCAN_EVERY = 3        # scan every 3 minutes
MIN_SCORE  = 4        # minimum confluence score (max 7)
STATS_FILE = "stats.json"

PAIRS = {
    "XAUUSD": "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "BTCUSD": "BTC-USD",
    "USDJPY": "USDJPY=X",
}

# Session filters — London + NY only (UTC)
SESSIONS = [
    (8, 12),    # London: 08:00–12:00 UTC
    (13, 17),   # New York: 13:00–17:00 UTC
]

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
    if 8 <= hour < 12:
        return "🇬🇧 London Session"
    if 13 <= hour < 17:
        return "🇺🇸 New York Session"
    return "😴 Off-Session"

# ──────────────────────────────────────────────────
# SL / TP CALCULATOR (from Gemini addition)
# ──────────────────────────────────────────────────
def calculate_sl_tp(entry: float, direction: str, symbol: str):
    """Dynamic SL/TP based on pair volatility"""
    # Tighter for forex, wider for Gold and BTC
    if symbol == "XAUUSD":
        sl_pct = 0.0015;  tp_pcts = [0.0008, 0.0015, 0.0025]
    elif symbol == "BTCUSD":
        sl_pct = 0.0030;  tp_pcts = [0.0015, 0.0030, 0.0050]
    else:  # Forex pairs
        sl_pct = 0.0010;  tp_pcts = [0.0005, 0.0010, 0.0015]

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
        return {"total": 0, "wins": 0, "losses": 0, "pending": [],
                "pairs": {}, "daily": {}, "streak": 0, "best_streak": 0}

    def _save(self):
        with open(STATS_FILE, "w") as f:
            json.dump(self.data, f, indent=2, default=str)

    def add_signal(self, symbol, direction, price, score):
        self.data["pending"].append({
            "symbol": symbol, "direction": direction,
            "entry_price": price, "score": score,
            "session": session_name(),
            "entry_time": datetime.utcnow().isoformat(),
            "expiry_time": (datetime.utcnow() + timedelta(minutes=EXPIRY_MIN)).isoformat(),
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
            bar = "🟩" if rate >= 60 else "🟨" if rate >= 50 else "🟥"
            pairs_block += f"  {bar} `{sym}`: {rec['wins']}W/{rec['losses']}L ({rate}%)\n"
        return (
            f"📊 *BOT PERFORMANCE — SMC PRO*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *Win Rate*\n"
            f"{self._winrate_bar(wr)} `{wr}%`\n\n"
            f"📈 Total: `{d['total']}`\n"
            f"✅ Wins: `{d['wins']}`  ❌ Losses: `{d['losses']}`\n\n"
            f"📅 *Today*: ✅ {today['wins']}W  ❌ {today['losses']}L\n\n"
            f"🔥 Streak: `{d.get('streak',0)}`  🥇 Best: `{d.get('best_streak',0)}`\n"
            f"💎 Best Pair: `{self.get_best_pair()}`\n\n"
            f"📊 *Pair Breakdown*\n{pairs_block}"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Expiry: `{EXPIRY_MIN} minutes`\n"
            f"🕐 `{datetime.utcnow().strftime('%H:%M UTC')}`"
        )

# ──────────────────────────────────────────────────
# SMC PRO ANALYSIS ENGINE
# ──────────────────────────────────────────────────
class SMCProAnalyzer:

    def fetch_data(self, ticker, interval="1m", period="1d"):
        try:
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 60:
                return None
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            return df
        except Exception as e:
            log.warning(f"Fetch error {ticker}: {e}")
            return None

    def get_htf_bias(self, ticker):
        """5m chart EMA50 vs EMA200 trend direction"""
        try:
            df = yf.download(ticker, period="5d", interval="5m",
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 200:
                return None
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            close = df["Close"].squeeze()
            ema50  = ta.trend.EMAIndicator(close, window=50).ema_indicator()
            ema200 = ta.trend.EMAIndicator(close, window=200).ema_indicator()
            if float(ema50.iloc[-1]) > float(ema200.iloc[-1]):
                return "CALL"
            elif float(ema50.iloc[-1]) < float(ema200.iloc[-1]):
                return "PUT"
            return None
        except:
            return None

    def detect_fvg(self, df):
        if len(df) < 4:
            return False, False
        return (bool(df["Low"].iloc[-2]  > df["High"].iloc[-4]),
                bool(df["High"].iloc[-2] < df["Low"].iloc[-4]))

    def detect_liquidity_sweep(self, df):
        if len(df) < 25:
            return False, False
        lookback   = df.iloc[-23:-2]
        high, low  = lookback["High"].max(), lookback["Low"].min()
        prev, last = df.iloc[-2], df.iloc[-1]
        return (bool(prev["Low"]  <= low  * 1.0002 and last["Close"] > low),
                bool(prev["High"] >= high * 0.9998 and last["Close"] < high))

    def detect_mss(self, df):
        if len(df) < 12:
            return False, False
        swing = df.iloc[-12:-2]
        lc = float(df["Close"].iloc[-1])
        return bool(lc > swing["High"].max()), bool(lc < swing["Low"].min())

    def detect_order_block(self, df):
        if len(df) < 6:
            return False, False
        r = df.iloc[-6:-1]
        bull = bool(r["Close"].iloc[0] < r["Open"].iloc[0] and
                    r["Close"].iloc[-1] > r["Open"].iloc[-1] and
                    (r["Close"].iloc[-1] - r["Open"].iloc[-1]) >
                    (r["High"].iloc[0]  - r["Low"].iloc[0]) * 0.5)
        bear = bool(r["Close"].iloc[0] > r["Open"].iloc[0] and
                    r["Close"].iloc[-1] < r["Open"].iloc[-1] and
                    (r["Open"].iloc[-1] - r["Close"].iloc[-1]) >
                    (r["High"].iloc[0]  - r["Low"].iloc[0]) * 0.5)
        return bull, bear

    def detect_engulfing(self, df):
        if len(df) < 3:
            return False, False
        p, l = df.iloc[-2], df.iloc[-1]
        bull = bool(p["Close"] < p["Open"] and l["Close"] > l["Open"] and
                    l["Close"] > p["Open"] and l["Open"] < p["Close"])
        bear = bool(p["Close"] > p["Open"] and l["Close"] < l["Open"] and
                    l["Close"] < p["Open"] and l["Open"] > p["Close"])
        return bull, bear

    def get_indicators(self, df):
        close = df["Close"].squeeze()
        ema8   = ta.trend.EMAIndicator(close, window=8).ema_indicator()
        ema21  = ta.trend.EMAIndicator(close, window=21).ema_indicator()
        ema50  = ta.trend.EMAIndicator(close, window=50).ema_indicator()
        rsi    = ta.momentum.RSIIndicator(close, window=14).rsi()
        macd_o = ta.trend.MACD(close)
        bb     = ta.volatility.BollingerBands(close, window=20, window_dev=2)

        e8, e21, e50 = float(ema8.iloc[-1]), float(ema21.iloc[-1]), float(ema50.iloc[-1])
        pe8, pe21    = float(ema8.iloc[-2]), float(ema21.iloc[-2])
        rsi_val      = float(rsi.iloc[-1])
        macd_val     = float(macd_o.macd().iloc[-1])
        sig_val      = float(macd_o.macd_signal().iloc[-1])
        lc           = float(close.iloc[-1])
        bbh          = float(bb.bollinger_hband().iloc[-1])
        bbl          = float(bb.bollinger_lband().iloc[-1])

        bull_cross = (pe8 < pe21) and (e8 > e21)
        bear_cross = (pe8 > pe21) and (e8 < e21)

        if bull_cross and lc > e50 and macd_val > sig_val:
            bias = "CALL"
        elif bear_cross and lc < e50 and macd_val < sig_val:
            bias = "PUT"
        else:
            bias = None

        return bias, {
            "rsi": round(rsi_val, 1), "price": lc,
            "bull_rsi": rsi_val > 50,  "bear_rsi": rsi_val < 50,
            "bull_bb":  lc <= bbl,     "bear_bb":  lc >= bbh,
        }

    def analyze(self, symbol, ticker):
        if not is_active_session():
            return None

        df = self.fetch_data(ticker, interval="1m", period="1d")
        if df is None:
            return None

        try:
            bias, meta = self.get_indicators(df)
        except Exception as e:
            log.warning(f"Indicator error {symbol}: {e}")
            return None

        if bias is None:
            return None

        htf = self.get_htf_bias(ticker)
        if htf is not None and htf != bias:
            log.info(f"{symbol} HTF conflict ({htf} vs {bias}) — skip")
            return None

        fvg_b,  fvg_r  = self.detect_fvg(df)
        liq_b,  liq_r  = self.detect_liquidity_sweep(df)
        mss_b,  mss_r  = self.detect_mss(df)
        ob_b,   ob_r   = self.detect_order_block(df)
        eng_b,  eng_r  = self.detect_engulfing(df)

        if bias == "CALL":
            hits = [fvg_b, liq_b, mss_b, ob_b, eng_b, meta["bull_rsi"], meta["bull_bb"]]
            tags = ["📦 Fair Value Gap (Bull)", "💧 Liquidity Sweep (Low)",
                    "📐 Market Structure Shift (Bull)", "🧱 Order Block (Bull)",
                    "🕯 Bullish Engulfing", f"📊 RSI Bullish ({meta['rsi']})",
                    "📉 Price at Bollinger Low"]
            emoji = "✅ CALL"
        else:
            hits = [fvg_r, liq_r, mss_r, ob_r, eng_r, meta["bear_rsi"], meta["bear_bb"]]
            tags = ["📦 Fair Value Gap (Bear)", "💧 Liquidity Sweep (High)",
                    "📐 Market Structure Shift (Bear)", "🧱 Order Block (Bear)",
                    "🕯 Bearish Engulfing", f"📊 RSI Bearish ({meta['rsi']})",
                    "📈 Price at Bollinger High"]
            emoji = "❌ PUT"

        active = [tags[i] for i, h in enumerate(hits) if h]
        score  = len(active)

        if score < MIN_SCORE:
            log.info(f"{symbol} score {score} < {MIN_SCORE} — skip")
            return None

        strength = ("🔥 VERY STRONG" if score >= 6 else
                    "💪 STRONG"      if score >= 5 else "✅ GOOD")

        return {"symbol": symbol, "direction": emoji, "raw_dir": bias,
                "price": round(meta["price"], 5), "rsi": meta["rsi"],
                "score": score, "strength": strength,
                "smc_tags": active, "session": session_name(),
                "htf_bias": htf or "Neutral"}

# ──────────────────────────────────────────────────
# SIGNAL FORMATTER — merged with Gemini's SL/TP
# ──────────────────────────────────────────────────
def format_signal(sig):
    now    = datetime.utcnow()
    expiry = (now + timedelta(minutes=EXPIRY_MIN)).strftime("%H:%M UTC")
    lines  = "\n".join(f"    {t}" for t in sig["smc_tags"])
    stars  = "⭐" * sig["score"]
    entry  = sig["price"]

    sl, tp1, tp2, tp3 = calculate_sl_tp(entry, sig["raw_dir"], sig["symbol"])

    return (
        f"🚨 *POCKET OPTION SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{sig['symbol']}`\n"
        f"🎯 Direction: *{sig['direction']}*\n"
        f"📍 Session: {sig['session']}\n"
        f"📈 HTF Bias: `{sig['htf_bias']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Sniper Entry*: `{entry}`\n"
        f"⛔ *Stop Loss*:    `{sl}`\n"
        f"✅ *TP1*: `{tp1}`\n"
        f"✅ *TP2*: `{tp2}`\n"
        f"✅ *TP3*: `{tp3}`\n"
        f"⏱ Expiry: *{EXPIRY_MIN} min* → `{expiry}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 *SMC Confluence* {stars}\n"
        f"{lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💥 Strength: *{sig['strength']}*\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_"
    )

# ──────────────────────────────────────────────────
# BOT STATE
# ──────────────────────────────────────────────────
bot_paused   = False
stats        = StatsManager()
analyzer     = SMCProAnalyzer()
telegram_bot = None

# ──────────────────────────────────────────────────
# SCANNER
# ──────────────────────────────────────────────────
async def scan_and_send(context=None):
    if bot_paused or telegram_bot is None:
        return
    if not is_active_session():
        log.info(f"Outside session — skip scan")
        return

    log.info(f"🔍 Scanning | {session_name()}")
    sent = 0
    for symbol, ticker in PAIRS.items():
        sig = analyzer.analyze(symbol, ticker)
        if sig:
            try:
                await telegram_bot.send_message(
                    chat_id=CHAT_ID,
                    text=format_signal(sig),
                    parse_mode="Markdown"
                )
                stats.add_signal(sig["symbol"], sig["raw_dir"],
                                 sig["price"], sig["score"])
                sent += 1
                log.info(f"✅ {symbol} {sig['direction']} score={sig['score']}")
                await asyncio.sleep(2)
            except Exception as e:
                log.error(f"Send error {symbol}: {e}")
    if sent == 0:
        log.info("No qualifying signals.")

# ──────────────────────────────────────────────────
# COMMAND HANDLERS
# ──────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Pocket Option Signal Bot — SMC PRO*\n\n"
        f"⚙️ *Settings*\n"
        f"• Expiry: `{EXPIRY_MIN} minutes`\n"
        f"• Min Score: `{MIN_SCORE}/7`\n"
        f"• Sessions: London + New York only\n"
        f"• Pairs: XAUUSD, EURUSD, GBPUSD, BTCUSD, USDJPY\n\n"
        f"🧠 *Active Filters*\n"
        f"• HTF Trend Bias (5m)\n"
        f"• Fair Value Gap\n"
        f"• Liquidity Sweep\n"
        f"• Market Structure Shift\n"
        f"• Order Block\n"
        f"• Engulfing Candle\n"
        f"• RSI + Bollinger Bands\n"
        f"• Dynamic SL + TP1/TP2/TP3\n\n"
        f"Use /help for all commands.",
        parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Commands*\n\n"
        "/start — Bot info\n"
        "/stats — Performance report\n"
        "/pairs — Active pairs\n"
        "/session — Current session status\n"
        "/scan — Force immediate scan\n"
        "/win — Record last signal as WIN\n"
        "/loss — Record last signal as LOSS\n"
        "/pause — Pause signals\n"
        "/resume — Resume signals\n"
        "/help — This message",
        parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats.format_stats(), parse_mode="Markdown")

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = "\n".join(f"  • `{s}`" for s in PAIRS)
    await update.message.reply_text(
        f"📈 *Active Pairs*\n{lines}\n\n⏱ Expiry: `{EXPIRY_MIN} min`",
        parse_mode="Markdown")

async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = is_active_session()
    status = "✅ ACTIVE — Signals ON" if active else "😴 INACTIVE — No signals"
    await update.message.reply_text(
        f"🕐 *Session Status*\n\n"
        f"Current: {session_name()}\n"
        f"Status: {status}\n"
        f"Time: `{datetime.utcnow().strftime('%H:%M UTC')}`\n\n"
        f"📅 *Active Windows (UTC)*\n"
        f"  🇬🇧 London: `08:00 – 12:00`\n"
        f"  🇺🇸 New York: `13:00 – 17:00`",
        parse_mode="Markdown")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning now...", parse_mode="Markdown")
    await scan_and_send()
    await update.message.reply_text("✅ Scan complete.", parse_mode="Markdown")

async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text("No pending signals to record.")
        return
    last = pending.pop()
    stats.record_result(last["symbol"], win=True)
    stats._save()
    await update.message.reply_text(
        f"✅ *WIN* recorded for `{last['symbol']}`\n"
        f"Win Rate: `{stats.get_win_rate()}%`",
        parse_mode="Markdown")

async def cmd_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text("No pending signals to record.")
        return
    last = pending.pop()
    stats.record_result(last["symbol"], win=False)
    stats._save()
    await update.message.reply_text(
        f"❌ *LOSS* recorded for `{last['symbol']}`\n"
        f"Win Rate: `{stats.get_win_rate()}%`",
        parse_mode="Markdown")

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = True
    await update.message.reply_text("⏸ Signals paused. Use /resume to restart.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = False
    await update.message.reply_text("▶️ Signals resumed.")

# ──────────────────────────────────────────────────
# MAIN — uses JobQueue (Gemini's improvement)
# ──────────────────────────────────────────────────
def main():
    global telegram_bot
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_bot = app.bot

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("pairs",   cmd_pairs))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("scan",    cmd_scan))
    app.add_handler(CommandHandler("win",     cmd_win))
    app.add_handler(CommandHandler("loss",    cmd_loss))
    app.add_handler(CommandHandler("pause",   cmd_pause))
    app.add_handler(CommandHandler("resume",  cmd_resume))

    # JobQueue scheduler (cleaner than threading)
    app.job_queue.run_repeating(
        callback=scan_and_send,
        interval=SCAN_EVERY * 60,
        first=10
    )

    log.info(f"🤖 SMC PRO Bot online! Scanning every {SCAN_EVERY} min.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
