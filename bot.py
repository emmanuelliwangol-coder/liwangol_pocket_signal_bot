"""
╔══════════════════════════════════════════════════╗
║   POCKET OPTION SIGNAL BOT — SMC EDITION        ║
║   Signals: FVG + Liquidity Sweep + MSS + EMA    ║
║   Commands: /start /stats /pairs /help /pause   ║
╚══════════════════════════════════════════════════╝
"""

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
import pandas_ta as ta
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ──────────────────────────────────────────────────
# CONFIG  — edit these before running
# ──────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "8628123105:AAGRCl-k3O-0xXfI7fHgWoonvaN1Q8F_pRU")
CHAT_ID     = os.getenv("CHAT_ID",   "8494805451")   # Emmanuel personal Telegram ID
EXPIRY_MIN  = 5          # minutes per trade
SCAN_EVERY  = 5          # scan interval in minutes
MIN_SCORE   = 3          # minimum SMC confluence score to send signal (max=5)
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
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# STATS MANAGER
# ──────────────────────────────────────────────────
class StatsManager:
    def __init__(self, filepath: str = STATS_FILE):
        self.filepath = filepath
        self.data = self._load()

    def _load(self) -> dict:
        if Path(self.filepath).exists():
            with open(self.filepath) as f:
                return json.load(f)
        return {
            "total": 0, "wins": 0, "losses": 0, "pending": [],
            "pairs": {}, "daily": {}, "streak": 0, "best_streak": 0,
        }

    def _save(self):
        with open(self.filepath, "w") as f:
            json.dump(self.data, f, indent=2, default=str)

    def add_signal(self, symbol: str, direction: str, price: float, score: int):
        entry_time = datetime.utcnow()
        expiry_time = entry_time + timedelta(minutes=EXPIRY_MIN)
        self.data["pending"].append({
            "symbol": symbol,
            "direction": direction,
            "entry_price": price,
            "score": score,
            "entry_time": entry_time.isoformat(),
            "expiry_time": expiry_time.isoformat(),
        })
        self._save()
        log.info(f"Signal logged: {symbol} {direction} @ {price}")

    def record_result(self, symbol: str, win: bool):
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
        if win:
            self.data["pairs"][symbol]["wins"] += 1
        else:
            self.data["pairs"][symbol]["losses"] += 1

        self.data["daily"].setdefault(today, {"wins": 0, "losses": 0})
        if win:
            self.data["daily"][today]["wins"] += 1
        else:
            self.data["daily"][today]["losses"] += 1

        self._save()

    def get_win_rate(self) -> float:
        if self.data["total"] == 0:
            return 0.0
        return round((self.data["wins"] / self.data["total"]) * 100, 1)

    def get_today_stats(self) -> dict:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return self.data["daily"].get(today, {"wins": 0, "losses": 0})

    def get_best_pair(self) -> str:
        pairs = self.data.get("pairs", {})
        if not pairs:
            return "N/A"
        best = max(pairs, key=lambda p: pairs[p].get("wins", 0) /
                   max(pairs[p].get("wins", 0) + pairs[p].get("losses", 0), 1))
        return best

    def format_stats(self) -> str:
        d = self.data
        wr = self.get_win_rate()
        today = self.get_today_stats()
        best_pair = self.get_best_pair()
        pairs_block = ""
        for sym, rec in d.get("pairs", {}).items():
            total = rec["wins"] + rec["losses"]
            rate = round(rec["wins"] / total * 100, 1) if total else 0
            pairs_block += f"  • {sym}: {rec['wins']}W/{rec['losses']}L ({rate}%)\n"

        bar = self._winrate_bar(wr)
        return (
            f"📊 *BOT PERFORMANCE STATS*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *Overall Win Rate*\n"
            f"{bar} `{wr}%`\n\n"
            f"📈 Total Signals: `{d['total']}`\n"
            f"✅ Wins: `{d['wins']}`\n"
            f"❌ Losses: `{d['losses']}`\n\n"
            f"📅 *Today*\n"
            f"  ✅ {today['wins']}W  ❌ {today['losses']}L\n\n"
            f"🔥 Current Streak: `{d.get('streak', 0)}`\n"
            f"🥇 Best Streak: `{d.get('best_streak', 0)}`\n"
            f"💎 Best Pair: `{best_pair}`\n\n"
            f"📊 *Per Pair Breakdown*\n"
            f"{pairs_block}"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Last updated: {datetime.utcnow().strftime('%H:%M UTC')}_"
        )

    def _winrate_bar(self, wr: float) -> str:
        filled = int(wr / 10)
        return "🟩" * filled + "⬜" * (10 - filled)


# ──────────────────────────────────────────────────
# SMC ANALYSIS ENGINE
# ──────────────────────────────────────────────────
class SMCAnalyzer:

    def fetch_data(self, ticker: str, interval="5m", period="3d") -> pd.DataFrame | None:
        try:
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 60:
                return None
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            return df
        except Exception as e:
            log.warning(f"Data fetch error for {ticker}: {e}")
            return None

    # ── 1. Fair Value Gap ──────────────────────────
    def detect_fvg(self, df: pd.DataFrame) -> tuple[bool, bool]:
        """Returns (bullish_fvg, bearish_fvg)"""
        if len(df) < 4:
            return False, False
        c1_high = df["High"].iloc[-4]
        c1_low  = df["Low"].iloc[-4]
        c3_high = df["High"].iloc[-2]
        c3_low  = df["Low"].iloc[-2]
        bull_fvg = c3_low > c1_high   # gap above candle 1
        bear_fvg = c3_high < c1_low   # gap below candle 1
        return bool(bull_fvg), bool(bear_fvg)

    # ── 2. Liquidity Sweep ─────────────────────────
    def detect_liquidity_sweep(self, df: pd.DataFrame) -> tuple[bool, bool]:
        """Detects equal highs/lows sweep (stop hunt)"""
        window = 20
        if len(df) < window + 3:
            return False, False
        lookback = df.iloc[-(window + 2):-2]
        recent_high = lookback["High"].max()
        recent_low  = lookback["Low"].min()
        last  = df.iloc[-1]
        prev  = df.iloc[-2]
        # Swept high then closed below = bearish sweep
        swept_high = (prev["High"] >= recent_high * 0.9998) and (last["Close"] < recent_high)
        # Swept low then closed above = bullish sweep
        swept_low  = (prev["Low"]  <= recent_low  * 1.0002) and (last["Close"] > recent_low)
        return bool(swept_low), bool(swept_high)

    # ── 3. Market Structure Shift ──────────────────
    def detect_mss(self, df: pd.DataFrame) -> tuple[bool, bool]:
        """Bullish MSS: breaks above last swing high. Bearish: breaks below last swing low."""
        if len(df) < 10:
            return False, False
        swing_window = df.iloc[-10:-2]
        swing_high = swing_window["High"].max()
        swing_low  = swing_window["Low"].min()
        last_close = df["Close"].iloc[-1]
        bull_mss = last_close > swing_high
        bear_mss = last_close < swing_low
        return bool(bull_mss), bool(bear_mss)

    # ── 4. Order Block ─────────────────────────────
    def detect_order_block(self, df: pd.DataFrame) -> tuple[bool, bool]:
        """Last down candle before a strong up move = bull OB (and vice versa)"""
        if len(df) < 6:
            return False, False
        recent = df.iloc[-6:-1]
        # Bullish OB: last bearish candle followed by strong bullish candles
        bull_ob = (recent["Close"].iloc[-4] < recent["Open"].iloc[-4] and
                   recent["Close"].iloc[-1] > recent["Open"].iloc[-1] and
                   (recent["Close"].iloc[-1] - recent["Open"].iloc[-1]) >
                   (recent["High"].iloc[-4] - recent["Low"].iloc[-4]) * 0.5)
        # Bearish OB: last bullish candle followed by strong bearish candles
        bear_ob = (recent["Close"].iloc[-4] > recent["Open"].iloc[-4] and
                   recent["Close"].iloc[-1] < recent["Open"].iloc[-1] and
                   (recent["Open"].iloc[-1] - recent["Close"].iloc[-1]) >
                   (recent["High"].iloc[-4] - recent["Low"].iloc[-4]) * 0.5)
        return bool(bull_ob), bool(bear_ob)

    # ── 5. Classic Indicators ─────────────────────
    def get_indicator_bias(self, df: pd.DataFrame) -> tuple[str | None, dict]:
        close = df["Close"].squeeze()
        ema8  = ta.ema(close, length=8)
        ema21 = ta.ema(close, length=21)
        rsi   = ta.rsi(close, length=14)
        macd_df = ta.macd(close)

        if ema8 is None or ema21 is None or rsi is None or macd_df is None:
            return None, {}

        last_ema8  = float(ema8.iloc[-1])
        last_ema21 = float(ema21.iloc[-1])
        prev_ema8  = float(ema8.iloc[-2])
        prev_ema21 = float(ema21.iloc[-2])
        last_rsi   = float(rsi.iloc[-1])
        last_macd  = float(macd_df["MACD_12_26_9"].iloc[-1])
        last_sig   = float(macd_df["MACDs_12_26_9"].iloc[-1])
        last_close = float(close.iloc[-1])

        bull_ema = (prev_ema8 < prev_ema21) and (last_ema8 > last_ema21)
        bear_ema = (prev_ema8 > prev_ema21) and (last_ema8 < last_ema21)
        bull_rsi = last_rsi > 50
        bear_rsi = last_rsi < 50
        bull_macd = last_macd > last_sig
        bear_macd = last_macd < last_sig

        if bull_ema and bull_rsi and bull_macd:
            bias = "CALL"
        elif bear_ema and bear_rsi and bear_macd:
            bias = "PUT"
        else:
            bias = None

        meta = {
            "ema8": round(last_ema8, 5),
            "ema21": round(last_ema21, 5),
            "rsi": round(last_rsi, 1),
            "macd": round(last_macd, 5),
            "price": last_close,
        }
        return bias, meta

    # ── MASTER SIGNAL BUILDER ─────────────────────
    def analyze(self, symbol: str, ticker: str) -> dict | None:
        df = self.fetch_data(ticker)
        if df is None:
            return None

        bias, meta = self.get_indicator_bias(df)
        if bias is None:
            return None

        fvg_bull,   fvg_bear   = self.detect_fvg(df)
        liq_bull,   liq_bear   = self.detect_liquidity_sweep(df)
        mss_bull,   mss_bear   = self.detect_mss(df)
        ob_bull,    ob_bear    = self.detect_order_block(df)

        if bias == "CALL":
            smc_hits = [fvg_bull, liq_bull, mss_bull, ob_bull]
            tags = ["📦 FVG (Bullish)", "💧 Liquidity Sweep (Low)", "📐 MSS Bullish", "🧱 Order Block (Bull)"]
            emoji = "✅ CALL"
        else:
            smc_hits = [fvg_bear, liq_bear, mss_bear, ob_bear]
            tags = ["📦 FVG (Bearish)", "💧 Liquidity Sweep (High)", "📐 MSS Bearish", "🧱 Order Block (Bear)"]
            emoji = "❌ PUT"

        active_tags = [tags[i] for i, hit in enumerate(smc_hits) if hit]
        score = len(active_tags) + 1  # +1 for base EMA/RSI/MACD alignment

        if score < MIN_SCORE:
            log.info(f"{symbol} score {score} < {MIN_SCORE} — skipping")
            return None

        return {
            "symbol":    symbol,
            "direction": emoji,
            "raw_dir":   bias,
            "price":     round(meta["price"], 5),
            "rsi":       meta["rsi"],
            "ema8":      meta["ema8"],
            "ema21":     meta["ema21"],
            "score":     score,
            "smc_tags":  active_tags,
        }


# ──────────────────────────────────────────────────
# SIGNAL FORMATTER
# ──────────────────────────────────────────────────
def format_signal(sig: dict) -> str:
    now = datetime.utcnow()
    expiry = (now + timedelta(minutes=EXPIRY_MIN)).strftime("%H:%M UTC")
    smc_lines = "\n".join(f"    {t}" for t in sig["smc_tags"]) or "    (Base indicators only)"
    stars = "⭐" * sig["score"]
    return (
        f"🚨 *POCKET OPTION SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{sig['symbol']}`\n"
        f"🎯 Direction: *{sig['direction']}*\n"
        f"💰 Entry: `{sig['price']}`\n"
        f"⏱ Expiry: *{EXPIRY_MIN} min* → `{expiry}`\n"
        f"📊 RSI: `{sig['rsi']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 *SMC Confluence* {stars}\n"
        f"{smc_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_"
    )


# ──────────────────────────────────────────────────
# BOT STATE
# ──────────────────────────────────────────────────
bot_paused  = False
stats       = StatsManager()
analyzer    = SMCAnalyzer()
telegram_bot: Bot | None = None


# ──────────────────────────────────────────────────
# SCANNER — runs on schedule
# ──────────────────────────────────────────────────
async def scan_and_send():
    global telegram_bot
    if bot_paused or telegram_bot is None:
        return

    log.info("🔍 Scanning markets...")
    sent = 0
    for symbol, ticker in PAIRS.items():
        sig = analyzer.analyze(symbol, ticker)
        if sig:
            msg = format_signal(sig)
            try:
                await telegram_bot.send_message(
                    chat_id=CHAT_ID, text=msg, parse_mode="Markdown"
                )
                stats.add_signal(sig["symbol"], sig["raw_dir"], sig["price"], sig["score"])
                sent += 1
                log.info(f"✅ Signal sent: {symbol} {sig['direction']} score={sig['score']}")
                await asyncio.sleep(1.5)
            except Exception as e:
                log.error(f"Send error for {symbol}: {e}")

    if sent == 0:
        log.info("No qualifying signals this scan.")


# ──────────────────────────────────────────────────
# TELEGRAM COMMAND HANDLERS
# ──────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Pocket Option Signal Bot — SMC Edition*\n\n"
        "I scan markets every 5 minutes using:\n"
        "• Fair Value Gap (FVG)\n"
        "• Liquidity Sweep detection\n"
        "• Market Structure Shift (MSS)\n"
        "• Order Block confirmation\n"
        "• EMA + RSI + MACD confluence\n\n"
        "Use /help to see all commands.",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Available Commands*\n\n"
        "/start — Welcome message\n"
        "/stats — Full performance report\n"
        "/pairs — List active pairs\n"
        "/scan — Force an immediate scan\n"
        "/win — Record last signal as WIN\n"
        "/loss — Record last signal as LOSS\n"
        "/pause — Pause signal sending\n"
        "/resume — Resume signal sending\n"
        "/help — This message",
        parse_mode="Markdown",
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats.format_stats(), parse_mode="Markdown")

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = "\n".join(f"  • `{s}`" for s in PAIRS)
    await update.message.reply_text(
        f"📈 *Active Pairs ({len(PAIRS)})*\n{lines}", parse_mode="Markdown"
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Running manual scan...", parse_mode="Markdown")
    await scan_and_send()
    await update.message.reply_text("✅ Scan complete.", parse_mode="Markdown")

async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text("No pending signals to record.")
        return
    last = pending[-1]
    stats.record_result(last["symbol"], win=True)
    stats.data["pending"].pop()
    stats.data_save = stats._save()
    await update.message.reply_text(
        f"✅ WIN recorded for `{last['symbol']}`\n"
        f"Win rate: `{stats.get_win_rate()}%`",
        parse_mode="Markdown",
    )

async def cmd_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text("No pending signals to record.")
        return
    last = pending[-1]
    stats.record_result(last["symbol"], win=False)
    stats.data["pending"].pop()
    stats._save()
    await update.message.reply_text(
        f"❌ LOSS recorded for `{last['symbol']}`\n"
        f"Win rate: `{stats.get_win_rate()}%`",
        parse_mode="Markdown",
    )

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = True
    await update.message.reply_text("⏸ Signal scanning paused. Use /resume to restart.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = False
    await update.message.reply_text("▶️ Signal scanning resumed.")


# ──────────────────────────────────────────────────
# SCHEDULER THREAD
# ──────────────────────────────────────────────────
def run_scheduler(loop: asyncio.AbstractEventLoop):
    def job():
        asyncio.run_coroutine_threadsafe(scan_and_send(), loop)

    schedule.every(SCAN_EVERY).minutes.do(job)
    log.info(f"Scheduler started — scanning every {SCAN_EVERY} minutes")
    while True:
        schedule.run_pending()
        time.sleep(10)


# ──────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────
def main():
    global telegram_bot

    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌  Set BOT_TOKEN and CHAT_ID before running!")
        return

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

    log.info("🤖 Bot is online. Listening for commands...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
