"""
╔══════════════════════════════════════════════════╗
║   POCKET OPTION SIGNAL BOT — SMC PRO EDITION   ║
║   Data: Twelve Data API (replaces yfinance)     ║
║   Expiry: 3 min | Sessions: London + NY         ║
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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ──────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "8628123105:AAGRCl-k3O-0xXfI7fHgWoonvaN1Q8F_pRU")
CHAT_ID      = os.getenv("CHAT_ID",   "8494805451")
TD_API_KEY   = os.getenv("TD_API_KEY","310a0ed4468144a09c38b2687369f314")
EXPIRY_MIN   = 3
SCAN_EVERY   = 3
MIN_SCORE    = 2
STATS_FILE   = "stats.json"

# Twelve Data symbols
PAIRS = {
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "BTCUSD": "BTC/USD",
}

SESSIONS = [
    (8, 12),    # London
    (13, 17),   # New York
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
    if 8 <= hour < 12:  return "🇬🇧 London Session"
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
            "low": "Low",  "close": "Close",
            "volume": "Volume"
        })
        for col in ["Open","High","Low","Close"]:
            df[col] = pd.to_numeric(df[col])
        df = df.iloc[::-1].reset_index(drop=True)  # oldest first
        return df
    except Exception as e:
        log.warning(f"Fetch error {symbol}: {e}")
        return None

def fetch_htf_candles(symbol: str) -> pd.DataFrame | None:
    return fetch_candles(symbol, interval="5min", outputsize=200)

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
        return {"total":0,"wins":0,"losses":0,"pending":[],
                "pairs":{},"daily":{},"streak":0,"best_streak":0}

    def _save(self):
        with open(STATS_FILE,"w") as f:
            json.dump(self.data, f, indent=2, default=str)

    def add_signal(self, symbol, direction, price, score):
        self.data["pending"].append({
            "symbol": symbol, "direction": direction,
            "entry_price": price, "score": score,
            "session": session_name(),
            "entry_time": datetime.utcnow().isoformat(),
            "expiry_time": (datetime.utcnow()+timedelta(minutes=EXPIRY_MIN)).isoformat(),
        })
        self._save()

    def record_result(self, symbol, win):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        self.data["total"] += 1
        if win:
            self.data["wins"] += 1
            self.data["streak"] = self.data.get("streak",0) + 1
        else:
            self.data["losses"] += 1
            self.data["streak"] = 0
        if self.data["streak"] > self.data.get("best_streak",0):
            self.data["best_streak"] = self.data["streak"]
        self.data["pairs"].setdefault(symbol, {"wins":0,"losses":0})
        self.data["pairs"][symbol]["wins" if win else "losses"] += 1
        self.data["daily"].setdefault(today, {"wins":0,"losses":0})
        self.data["daily"][today]["wins" if win else "losses"] += 1
        self._save()

    def get_win_rate(self):
        if self.data["total"] == 0: return 0.0
        return round((self.data["wins"]/self.data["total"])*100, 1)

    def get_today_stats(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return self.data["daily"].get(today, {"wins":0,"losses":0})

    def get_best_pair(self):
        pairs = self.data.get("pairs",{})
        if not pairs: return "N/A"
        return max(pairs, key=lambda p: pairs[p].get("wins",0)/
                   max(pairs[p].get("wins",0)+pairs[p].get("losses",0),1))

    def _bar(self, wr):
        f = int(wr/10)
        return "🟩"*f + "⬜"*(10-f)

    def format_stats(self):
        d = self.data
        wr = self.get_win_rate()
        today = self.get_today_stats()
        pb = ""
        for sym, rec in d.get("pairs",{}).items():
            tot = rec["wins"]+rec["losses"]
            rate = round(rec["wins"]/tot*100,1) if tot else 0
            bar = "🟩" if rate>=60 else "🟨" if rate>=50 else "🟥"
            pb += f"  {bar} `{sym}`: {rec['wins']}W/{rec['losses']}L ({rate}%)\n"
        return (
            f"📊 *BOT PERFORMANCE — SMC PRO*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *Win Rate*\n{self._bar(wr)} `{wr}%`\n\n"
            f"📈 Total: `{d['total']}`\n"
            f"✅ Wins: `{d['wins']}`  ❌ Losses: `{d['losses']}`\n\n"
            f"📅 *Today*: ✅ {today['wins']}W  ❌ {today['losses']}L\n\n"
            f"🔥 Streak: `{d.get('streak',0)}`  🥇 Best: `{d.get('best_streak',0)}`\n"
            f"💎 Best Pair: `{self.get_best_pair()}`\n\n"
            f"📊 *Pair Breakdown*\n{pb}"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Expiry: `{EXPIRY_MIN} minutes`\n"
            f"🕐 `{datetime.utcnow().strftime('%H:%M UTC')}`"
        )

# ──────────────────────────────────────────────────
# SMC PRO ANALYSIS ENGINE
# ──────────────────────────────────────────────────
class SMCProAnalyzer:

    def get_htf_bias(self, symbol):
        df = fetch_htf_candles(symbol)
        if df is None or len(df) < 60:
            return None
        try:
            close = df["Close"]
            ema50  = ta.trend.EMAIndicator(close, window=50).ema_indicator()
            ema200 = ta.trend.EMAIndicator(close, window=min(200,len(df)-1)).ema_indicator()
            if float(ema50.iloc[-1]) > float(ema200.iloc[-1]):
                return "CALL"
            elif float(ema50.iloc[-1]) < float(ema200.iloc[-1]):
                return "PUT"
        except:
            pass
        return None

    def detect_fvg(self, df):
        if len(df) < 4: return False, False
        return (bool(df["Low"].iloc[-2]  > df["High"].iloc[-4]),
                bool(df["High"].iloc[-2] < df["Low"].iloc[-4]))

    def detect_liquidity_sweep(self, df):
        if len(df) < 25: return False, False
        lb = df.iloc[-23:-2]
        hi, lo = lb["High"].max(), lb["Low"].min()
        p, l = df.iloc[-2], df.iloc[-1]
        return (bool(p["Low"]  <= lo*1.0002 and l["Close"] > lo),
                bool(p["High"] >= hi*0.9998 and l["Close"] < hi))

    def detect_mss(self, df):
        if len(df) < 12: return False, False
        swing = df.iloc[-12:-2]
        lc = float(df["Close"].iloc[-1])
        return bool(lc > swing["High"].max()), bool(lc < swing["Low"].min())

    def detect_order_block(self, df):
        if len(df) < 6: return False, False
        r = df.iloc[-6:-1]
        bull = bool(r["Close"].iloc[0] < r["Open"].iloc[0] and
                    r["Close"].iloc[-1] > r["Open"].iloc[-1] and
                    (r["Close"].iloc[-1]-r["Open"].iloc[-1]) >
                    (r["High"].iloc[0]-r["Low"].iloc[0])*0.5)
        bear = bool(r["Close"].iloc[0] > r["Open"].iloc[0] and
                    r["Close"].iloc[-1] < r["Open"].iloc[-1] and
                    (r["Open"].iloc[-1]-r["Close"].iloc[-1]) >
                    (r["High"].iloc[0]-r["Low"].iloc[0])*0.5)
        return bull, bear

    def detect_engulfing(self, df):
        if len(df) < 3: return False, False
        p, l = df.iloc[-2], df.iloc[-1]
        bull = bool(p["Close"]<p["Open"] and l["Close"]>l["Open"] and
                    l["Close"]>p["Open"] and l["Open"]<p["Close"])
        bear = bool(p["Close"]>p["Open"] and l["Close"]<l["Open"] and
                    l["Close"]<p["Open"] and l["Open"]>p["Close"])
        return bull, bear

    def get_indicators(self, df):
        close = df["Close"]
        ema8   = ta.trend.EMAIndicator(close, window=8).ema_indicator()
        ema21  = ta.trend.EMAIndicator(close, window=21).ema_indicator()
        ema50  = ta.trend.EMAIndicator(close, window=50).ema_indicator()
        rsi    = ta.momentum.RSIIndicator(close, window=14).rsi()
        macd_o = ta.trend.MACD(close)
        bb     = ta.volatility.BollingerBands(close, window=20, window_dev=2)

        e8,e21,e50 = float(ema8.iloc[-1]),float(ema21.iloc[-1]),float(ema50.iloc[-1])
        pe8,pe21   = float(ema8.iloc[-2]),float(ema21.iloc[-2])
        rsi_v      = float(rsi.iloc[-1])
        macd_v     = float(macd_o.macd().iloc[-1])
        sig_v      = float(macd_o.macd_signal().iloc[-1])
        lc         = float(close.iloc[-1])
        bbh        = float(bb.bollinger_hband().iloc[-1])
        bbl        = float(bb.bollinger_lband().iloc[-1])

        bull_cross = (pe8 < pe21) and (e8 > e21)
        bear_cross = (pe8 > pe21) and (e8 < e21)

        if bull_cross and lc > e50 and macd_v > sig_v:
            bias = "CALL"
        elif bear_cross and lc < e50 and macd_v < sig_v:
            bias = "PUT"
        else:
            bias = None

        return bias, {
            "rsi": round(rsi_v,1), "price": lc,
            "bull_rsi": rsi_v>50,  "bear_rsi": rsi_v<50,
            "bull_bb":  lc<=bbl,   "bear_bb":  lc>=bbh,
        }

    def analyze(self, symbol, td_symbol):
        if not is_active_session():
            return None

        df = fetch_candles(td_symbol, interval="1min", outputsize=100)
        if df is None or len(df) < 60:
            return None

        try:
            bias, meta = self.get_indicators(df)
        except Exception as e:
            log.warning(f"Indicator error {symbol}: {e}")
            return None

        if bias is None:
            return None

        fvg_b,  fvg_r  = self.detect_fvg(df)
        liq_b,  liq_r  = self.detect_liquidity_sweep(df)
        mss_b,  mss_r  = self.detect_mss(df)
        ob_b,   ob_r   = self.detect_order_block(df)
        eng_b,  eng_r  = self.detect_engulfing(df)

        if bias == "CALL":
            hits = [fvg_b, liq_b, mss_b, ob_b, eng_b, meta["bull_rsi"], meta["bull_bb"]]
            tags = ["📦 Fair Value Gap (Bull)","💧 Liquidity Sweep (Low)",
                    "📐 MSS Bullish","🧱 Order Block (Bull)",
                    "🕯 Bullish Engulfing",f"📊 RSI Bullish ({meta['rsi']})",
                    "📉 Price at Bollinger Low"]
            emoji = "✅ CALL"
        else:
            hits = [fvg_r, liq_r, mss_r, ob_r, eng_r, meta["bear_rsi"], meta["bear_bb"]]
            tags = ["📦 Fair Value Gap (Bear)","💧 Liquidity Sweep (High)",
                    "📐 MSS Bearish","🧱 Order Block (Bear)",
                    "🕯 Bearish Engulfing",f"📊 RSI Bearish ({meta['rsi']})",
                    "📈 Price at Bollinger High"]
            emoji = "❌ PUT"

        active = [tags[i] for i,h in enumerate(hits) if h]
        score  = len(active)

        if score < MIN_SCORE:
            log.info(f"{symbol} score {score} < {MIN_SCORE} — skip")
            return None

        strength = ("🔥 VERY STRONG" if score>=6 else
                    "💪 STRONG"      if score>=4 else "✅ GOOD")

        htf = self.get_htf_bias(td_symbol)

        return {"symbol":symbol,"direction":emoji,"raw_dir":bias,
                "price":round(meta["price"],5),"rsi":meta["rsi"],
                "score":score,"strength":strength,"smc_tags":active,
                "session":session_name(),"htf_bias":htf or "Neutral"}

# ──────────────────────────────────────────────────
# SIGNAL FORMATTER
# ──────────────────────────────────────────────────
def format_signal(sig):
    now    = datetime.utcnow()
    expiry = (now+timedelta(minutes=EXPIRY_MIN)).strftime("%H:%M UTC")
    lines  = "\n".join(f"    {t}" for t in sig["smc_tags"])
    stars  = "⭐"*sig["score"]
    entry  = sig["price"]
    sl,tp1,tp2,tp3 = calculate_sl_tp(entry, sig["raw_dir"], sig["symbol"])
    return (
        f"🚨 *POCKET OPTION SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{sig['symbol']}`\n"
        f"🎯 Direction: *{sig['direction']}*\n"
        f"📍 Session: {sig['session']}\n"
        f"📈 HTF Bias: `{sig['htf_bias']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Entry*: `{entry}`\n"
        f"⛔ *Stop Loss*: `{sl}`\n"
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
        log.info("Outside session — skip")
        return

    log.info(f"🔍 Scanning | {session_name()}")
    sent = 0
    for symbol, td_symbol in PAIRS.items():
        sig = analyzer.analyze(symbol, td_symbol)
        if sig:
            try:
                await telegram_bot.send_message(
                    chat_id=CHAT_ID,
                    text=format_signal(sig),
                    parse_mode="Markdown"
                )
                stats.add_signal(sig["symbol"],sig["raw_dir"],sig["price"],sig["score"])
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
        f"• Sessions: London + New York\n"
        f"• Data: Twelve Data API ✅\n"
        f"• Pairs: XAUUSD, EURUSD, GBPUSD, BTCUSD, USDJPY\n\n"
        f"🧠 *Active Filters*\n"
        f"• HTF Trend Bias\n• Fair Value Gap\n"
        f"• Liquidity Sweep\n• MSS\n"
        f"• Order Block\n• Engulfing Candle\n"
        f"• RSI + Bollinger Bands\n• SL + TP1/TP2/TP3\n\n"
        f"Use /help for all commands.",
        parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Commands*\n\n"
        "/start — Bot info\n"
        "/stats — Performance report\n"
        "/pairs — Active pairs\n"
        "/session — Session status\n"
        "/scan — Force immediate scan\n"
        "/win — Record WIN\n"
        "/loss — Record LOSS\n"
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
    pending = stats.data.get("pending",[])
    if not pending:
        await update.message.reply_text("No pending signals.")
        return
    last = pending.pop()
    stats.record_result(last["symbol"], win=True)
    stats._save()
    await update.message.reply_text(
        f"✅ *WIN* for `{last['symbol']}`\nWin Rate: `{stats.get_win_rate()}%`",
        parse_mode="Markdown")

async def cmd_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending",[])
    if not pending:
        await update.message.reply_text("No pending signals.")
        return
    last = pending.pop()
    stats.record_result(last["symbol"], win=False)
    stats._save()
    await update.message.reply_text(
        f"❌ *LOSS* for `{last['symbol']}`\nWin Rate: `{stats.get_win_rate()}%`",
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
# MAIN
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

    import threading, time as _time
    def _scheduler():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _time.sleep(15)
        while True:
            try:
                loop.run_until_complete(scan_and_send())
            except Exception as e:
                log.error(f"Scheduler error: {e}")
            _time.sleep(SCAN_EVERY * 60)

    t = threading.Thread(target=_scheduler, daemon=True)
    t.start()

    log.info(f"🤖 SMC PRO Bot online! Scanning every {SCAN_EVERY} min.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
