"""
╔══════════════════════════════════════════════════╗
║   POCKET OPTION SIGNAL BOT — SMC PRO EDITION   ║
║   Data: Twelve Data API (replaces yfinance)     ║
║   Strategies: SMC + London Breakout             ║
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

from london_breakout import (
    LondonBreakoutAnalyzer,
    format_breakout_signal,
)
from trend_pullback import (
    TrendPullbackAnalyzer,
    format_pullback_signal,
)

# ──────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
CHAT_ID      = os.getenv("CHAT_ID",   "")
TD_API_KEY   = os.getenv("TD_API_KEY", "")
EXPIRY_MIN      = 3
SCAN_EVERY      = 5
MIN_SCORE       = 2
MIN_CONFIDENCE  = 55
PRE_SIGNAL_MIN  = 45
MAX_SCORE       = 7
STATS_FILE      = "stats.json"

WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PORT = int(os.getenv("PORT", "8080"))

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
def is_weekend():
    return datetime.utcnow().weekday() >= 5

def is_active_session(symbol: str = ""):
    if symbol == "BTCUSD":
        return True
    if is_weekend():
        return False
    hour = datetime.utcnow().hour
    return any(s <= hour < e for s, e in SESSIONS)

def session_name(symbol: str = ""):
    if symbol == "BTCUSD":
        if is_weekend():
            return "₿ Crypto Weekend Session"
        hour = datetime.utcnow().hour
        if 8 <= hour < 12:  return "₿ Crypto | 🇬🇧 London Hours"
        if 13 <= hour < 17: return "₿ Crypto | 🇺🇸 NY Hours"
        return "₿ Crypto 24/7"
    if is_weekend():
        return "📴 Weekend — Forex Closed"
    hour = datetime.utcnow().hour
    if 8 <= hour < 12:  return "🇬🇧 London Session"
    if 13 <= hour < 17: return "🇺🇸 New York Session"
    return "😴 Off-Session"

# ──────────────────────────────────────────────────
# TWELVE DATA FETCHER  (now preserves timestamps — required by London Breakout)
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
            "datetime": "Datetime",
            "open": "Open", "high": "High",
            "low": "Low",  "close": "Close",
            "volume": "Volume"
        })
        for col in ["Open","High","Low","Close"]:
            df[col] = pd.to_numeric(df[col])
        if "Datetime" in df.columns:
            df["Datetime"] = pd.to_datetime(df["Datetime"])
        df = df.iloc[::-1].reset_index(drop=True)  # oldest first
        return df
    except Exception as e:
        log.warning(f"Fetch error {symbol}: {e}")
        return None

def fetch_htf_candles(symbol: str) -> pd.DataFrame | None:
    return fetch_candles(symbol, interval="1h", outputsize=200)

# ──────────────────────────────────────────────────
# BINANCE FETCHER (for BTCUSD — free, no API key)
# Patched: better error logging + fallback to public data mirror
# ──────────────────────────────────────────────────
BINANCE_URLS = [
    "https://data-api.binance.vision/api/v3/klines",  # public market-data mirror (tried first)
    "https://api.binance.com/api/v3/klines",           # main API (fallback)
]

def fetch_binance_candles(symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 100):
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for url in BINANCE_URLS:
        try:
            r = requests.get(url, params=params, timeout=10)

            if r.status_code != 200:
                log.warning(f"Binance [{url.split('/')[2]}] HTTP {r.status_code} for {symbol}: {r.text[:200]}")
                continue

            data = r.json()

            if not isinstance(data, list) or len(data) == 0:
                log.warning(f"Binance [{url.split('/')[2]}]: empty/invalid data for {symbol}: {str(data)[:200]}")
                continue

            df = pd.DataFrame(data, columns=[
                "OpenTime","Open","High","Low","Close","Volume",
                "CloseTime","QAV","NT","TBBAV","TBQAV","Ignore"
            ])
            for col in ["Open","High","Low","Close"]:
                df[col] = pd.to_numeric(df[col])
            df["Datetime"] = pd.to_datetime(df["OpenTime"], unit="ms")
            log.info(f"Binance [{url.split('/')[2]}] OK — {len(df)} candles for {symbol}")
            return df.reset_index(drop=True)

        except Exception as e:
            log.warning(f"Binance [{url.split('/')[2]}] fetch error {symbol}: {e}")
            continue

    log.error(f"Binance: ALL sources failed for {symbol} — no candle data available this cycle")
    return None

def fetch_binance_htf(symbol: str = "BTCUSDT"):
    return fetch_binance_candles(symbol, interval="1h", limit=200)

# ──────────────────────────────────────────────────
# SL / TP CALCULATOR (SMC strategy)
# ──────────────────────────────────────────────────
def calculate_sl_tp(entry: float, direction: str, symbol: str):
    if symbol == "XAUUSD":
        sl_pct  = 0.0012
        tp_pcts = [0.0018, 0.0024, 0.0036]
    elif symbol == "BTCUSD":
        sl_pct  = 0.0040
        tp_pcts = [0.0060, 0.0080, 0.0120]
    elif symbol in ("USDJPY",):
        sl_pct  = 0.0020
        tp_pcts = [0.0030, 0.0040, 0.0060]
    else:
        sl_pct  = 0.0020
        tp_pcts = [0.0030, 0.0040, 0.0060]

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
# STATS MANAGER  (now tracks per-strategy results too)
# ──────────────────────────────────────────────────
class StatsManager:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        if Path(STATS_FILE).exists():
            with open(STATS_FILE) as f:
                return json.load(f)
        return {"total":0,"wins":0,"losses":0,"pending":[],
                "pairs":{},"daily":{},"streak":0,"best_streak":0,
                "strategies":{}}

    def _save(self):
        with open(STATS_FILE,"w") as f:
            json.dump(self.data, f, indent=2, default=str)

    def add_signal(self, symbol, direction, price, score, strategy="SMC"):
        self.data["pending"].append({
            "symbol": symbol, "direction": direction,
            "entry_price": price, "score": score,
            "strategy": strategy,
            "session": session_name(symbol),
            "entry_time": datetime.utcnow().isoformat(),
            "expiry_time": (datetime.utcnow()+timedelta(minutes=EXPIRY_MIN)).isoformat(),
        })
        self._save()

    def record_result(self, symbol, win, strategy="SMC"):
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
        self.data.setdefault("strategies", {})
        self.data["strategies"].setdefault(strategy, {"wins":0,"losses":0})
        self.data["strategies"][strategy]["wins" if win else "losses"] += 1
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

        sb = ""
        for strat, rec in d.get("strategies", {}).items():
            tot = rec["wins"]+rec["losses"]
            rate = round(rec["wins"]/tot*100,1) if tot else 0
            bar = "🟩" if rate>=60 else "🟨" if rate>=50 else "🟥"
            sb += f"  {bar} `{strat}`: {rec['wins']}W/{rec['losses']}L ({rate}%)\n"

        return (
            f"📊 *BOT PERFORMANCE — MULTI-STRATEGY*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *Win Rate*\n{self._bar(wr)} `{wr}%`\n\n"
            f"📈 Total: `{d['total']}`\n"
            f"✅ Wins: `{d['wins']}`  ❌ Losses: `{d['losses']}`\n\n"
            f"📅 *Today*: ✅ {today['wins']}W  ❌ {today['losses']}L\n\n"
            f"🔥 Streak: `{d.get('streak',0)}`  🥇 Best: `{d.get('best_streak',0)}`\n"
            f"💎 Best Pair: `{self.get_best_pair()}`\n\n"
            f"📊 *Pair Breakdown*\n{pb}\n"
            f"🧩 *Strategy Breakdown*\n{sb}"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 `{datetime.utcnow().strftime('%H:%M UTC')}`"
        )

# ──────────────────────────────────────────────────
# SMC PRO ANALYSIS ENGINE  (unchanged from your validated 15M/1H version)
# ──────────────────────────────────────────────────
class SMCProAnalyzer:

    def get_htf_bias(self, symbol, orig_symbol=""):
        if orig_symbol == "BTCUSD":
            df = fetch_binance_htf("BTCUSDT")
        else:
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

    def analyze(self, symbol, td_symbol, df=None):
        if not is_active_session(symbol):
            return None, None

        if df is None:
            df = fetch_candles(td_symbol, interval="15min", outputsize=100)
        if df is None or len(df) < 60:
            return None, None

        try:
            bias, meta = self.get_indicators(df)
        except Exception as e:
            log.warning(f"Indicator error {symbol}: {e}")
            return None, None

        if bias is None:
            return None, None

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
            return None, None

        confidence = round((score / MAX_SCORE) * 100)

        htf = self.get_htf_bias(td_symbol, symbol)
        if htf and htf == bias:
            confidence = min(confidence + 5, 100)

        rsi_hit = meta["bull_rsi"] if bias == "CALL" else meta["bear_rsi"]
        bb_hit  = meta["bull_bb"]  if bias == "CALL" else meta["bear_bb"]
        if rsi_hit and bb_hit:
            confidence = min(confidence + 5, 100)

        all_tags = tags
        pending  = [tags[i] for i,h in enumerate(hits) if not h]

        strength = ("🔥 VERY STRONG" if score>=6 else
                    "💪 STRONG"      if score>=4 else "✅ GOOD")

        result = {"symbol":symbol,"direction":emoji,"raw_dir":bias,
                  "price":round(meta["price"],5),"rsi":meta["rsi"],
                  "score":score,"strength":strength,"smc_tags":active,
                  "pending_tags":pending,
                  "confidence":confidence,
                  "session":session_name(symbol),"htf_bias":htf or "Neutral"}

        if confidence >= MIN_CONFIDENCE:
            log.info(f"{symbol} confidence {confidence}% — SIGNAL")
            return result, "signal"
        elif confidence >= PRE_SIGNAL_MIN:
            log.info(f"{symbol} confidence {confidence}% — PRE-SIGNAL")
            return result, "presignal"
        else:
            log.info(f"{symbol} confidence {confidence}% < {PRE_SIGNAL_MIN}% — skip")
            return None, None

# ──────────────────────────────────────────────────
# SIGNAL FORMATTER (SMC)
# ──────────────────────────────────────────────────
def confidence_bar(pct: int) -> str:
    filled = round(pct / 10)
    bar = "🟢" * filled + "⬜" * (10 - filled)
    return f"{bar} `{pct}%`"

def format_signal(sig):
    now    = datetime.utcnow()
    expiry = (now+timedelta(minutes=EXPIRY_MIN)).strftime("%H:%M UTC")
    lines  = "\n".join(f"    {t}" for t in sig["smc_tags"])
    stars  = "⭐"*sig["score"]
    entry  = sig["price"]
    conf   = sig["confidence"]
    sl,tp1,tp2,tp3 = calculate_sl_tp(entry, sig["raw_dir"], sig["symbol"])

    if conf >= 90:
        conf_label = "🔥 ELITE"
    elif conf >= 80:
        conf_label = "💎 HIGH"
    else:
        conf_label = "✅ GOOD"

    return (
        f"🚨 *SMC SIGNAL*\n"
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
        f"📊 *Confidence*: {conf_label}\n"
        f"{confidence_bar(conf)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💥 Strength: *{sig['strength']}*\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_\n"
        f"📝 _Reply /win SMC or /loss SMC after trade_"
    )

def format_presignal(sig):
    now     = datetime.utcnow()
    active  = "\n".join(f"    ✅ {t}" for t in sig["smc_tags"])
    pending = "\n".join(f"    ⏳ {t}" for t in sig["pending_tags"])
    conf    = sig["confidence"]
    needed  = MIN_CONFIDENCE - conf
    filled  = round(conf / 10)
    bar     = "🟡" * filled + "⬜" * (10 - filled)
    return (
        f"👀 *SETUP FORMING — {sig['symbol']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Market is building confluence..._\n"
        f"🎯 Potential: *{sig['direction']}*\n"
        f"📍 Session: {sig['session']}\n"
        f"📈 HTF Bias: `{sig['htf_bias']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Confirmed so far:*\n{active}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ *Still needs:*\n{pending}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Setup Confidence*: `{conf}%` _(+{needed}% more needed for entry signal)_\n"
        f"{bar} `{conf}%`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👁 *Watch `{sig['symbol']}` closely — signal may fire soon!*\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`"
    )


# ──────────────────────────────────────────────────
# BOT STATE
# ──────────────────────────────────────────────────
bot_paused        = False
stats             = StatsManager()
analyzer          = SMCProAnalyzer()
breakout_analyzer = LondonBreakoutAnalyzer()
pullback_analyzer = TrendPullbackAnalyzer()
telegram_bot      = None
presignal_sent    = {}

# ──────────────────────────────────────────────────
# SCANNER  (now runs SMC + Breakout off the same fetched candles)
# ──────────────────────────────────────────────────
async def scan_and_send(context=None):
    global presignal_sent
    if bot_paused or telegram_bot is None:
        return
    log.info(f"🔍 Scanning | {session_name()}")
    sent = 0
    for symbol, td_symbol in PAIRS.items():
        await asyncio.sleep(15)

        # Fetch once, reuse for both strategies
        if symbol == "BTCUSD":
            df = fetch_binance_candles("BTCUSDT", interval="15m", limit=100)
        else:
            df = fetch_candles(td_symbol, interval="15min", outputsize=100)

        # ── SMC strategy ──
        sig, sig_type = analyzer.analyze(symbol, td_symbol, df=df)
        if sig and sig_type == "signal":
            try:
                await telegram_bot.send_message(
                    chat_id=CHAT_ID, text=format_signal(sig), parse_mode="Markdown"
                )
                stats.add_signal(sig["symbol"], sig["raw_dir"], sig["price"], sig["score"], strategy="SMC")
                presignal_sent.pop(symbol, None)
                sent += 1
                log.info(f"[SMC] Signal sent: {symbol} {sig['direction']} score={sig['score']}")
                await asyncio.sleep(2)
            except Exception as e:
                log.error(f"[SMC] Send error {symbol}: {e}")
        elif sig and sig_type == "presignal":
            last_conf = presignal_sent.get(symbol, 0)
            if sig["confidence"] >= last_conf + 5:
                try:
                    await telegram_bot.send_message(
                        chat_id=CHAT_ID, text=format_presignal(sig), parse_mode="Markdown"
                    )
                    presignal_sent[symbol] = sig["confidence"]
                    sent += 1
                    log.info(f"[SMC] Pre-signal sent: {symbol} conf={sig['confidence']}%")
                    await asyncio.sleep(2)
                except Exception as e:
                    log.error(f"[SMC] Pre-signal send error {symbol}: {e}")
            else:
                log.info(f"[SMC] Pre-signal suppressed: {symbol} conf={sig['confidence']}%")
        else:
            presignal_sent.pop(symbol, None)

        # ── London Breakout strategy ──
        try:
            bsig = breakout_analyzer.analyze(symbol, df)
            if bsig:
                await telegram_bot.send_message(
                    chat_id=CHAT_ID, text=format_breakout_signal(bsig), parse_mode="Markdown"
                )
                stats.add_signal(bsig["symbol"], bsig["raw_dir"], bsig["price"], 0, strategy="BREAKOUT")
                sent += 1
                log.info(f"[BREAKOUT] Signal sent: {symbol} {bsig['direction']}")
                await asyncio.sleep(2)
        except Exception as e:
            log.error(f"[BREAKOUT] Error {symbol}: {e}")

        # ── Trend Pullback strategy ──
        try:
            htf_bias = analyzer.get_htf_bias(td_symbol, symbol)
            psig = pullback_analyzer.analyze(symbol, df, htf_bias)
            if psig:
                await telegram_bot.send_message(
                    chat_id=CHAT_ID, text=format_pullback_signal(psig), parse_mode="Markdown"
                )
                stats.add_signal(psig["symbol"], psig["raw_dir"], psig["price"], 0, strategy="PULLBACK")
                sent += 1
                log.info(f"[PULLBACK] Signal sent: {symbol} {psig['direction']}")
                await asyncio.sleep(2)
        except Exception as e:
            log.error(f"[PULLBACK] Error {symbol}: {e}")

    if sent == 0:
        log.info("No qualifying signals.")

# ──────────────────────────────────────────────────
# COMMAND HANDLERS
# ──────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Multi-Strategy Signal Bot*\n\n"
        f"⚙️ *Active Strategies*\n"
        f"• 🧠 SMC PRO (15M entries, 1H HTF bias)\n"
        f"• 🇬🇧 London Breakout (session range breakout)\n"
        f"• 📈 Trend Pullback (EMA21 pullback + rejection)\n\n"
        f"⚙️ *Settings*\n"
        f"• Min SMC Score: `{MIN_SCORE}/7`\n"
        f"• Min Confidence: `{MIN_CONFIDENCE}%`\n"
        f"• Pairs: XAUUSD, EURUSD, GBPUSD, USDJPY, BTCUSD\n\n"
        f"Use /help for all commands.",
        parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Commands*\n\n"
        "/start — Bot info\n"
        "/stats — Performance report (per strategy + per pair)\n"
        "/pairs — Active pairs\n"
        "/session — Session status\n"
        "/scan — Force immediate scan\n\n"
        "📝 *After each trade:*\n"
        "/win — Record last signal as WIN\n"
        "/win XAUUSD — Record WIN for specific pair\n"
        "/loss — Record last signal as LOSS\n"
        "/loss EURUSD — Record LOSS for specific pair\n\n"
        "/pause — Pause signals\n"
        "/resume — Resume signals\n"
        "/help — This message",
        parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats.format_stats(), parse_mode="Markdown")

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = "\n".join(f"  • `{s}`" for s in PAIRS)
    await update.message.reply_text(
        f"📈 *Active Pairs*\n{lines}",
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

def _pop_pending(context_args: list) -> dict | None:
    """
    Pop the most recent pending signal.
    Supports: /win, /win XAUUSD, /win SMC, /win BREAKOUT,
    or /win XAUUSD SMC to match both symbol and strategy.
    """
    pending = stats.data.get("pending", [])
    if not pending:
        return None
    if not context_args:
        return pending.pop()

    args = [a.upper() for a in context_args]
    strategies = {"SMC", "BREAKOUT", "PULLBACK"}
    symbol_arg   = next((a for a in args if a not in strategies), None)
    strategy_arg = next((a for a in args if a in strategies), None)

    for i in range(len(pending) - 1, -1, -1):
        entry = pending[i]
        if symbol_arg and entry["symbol"] != symbol_arg:
            continue
        if strategy_arg and entry.get("strategy") != strategy_arg:
            continue
        return pending.pop(i)
    return None

async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text(
            "⚠️ No pending signals to record.", parse_mode="Markdown")
        return

    entry = _pop_pending(context.args)
    if entry is None:
        await update.message.reply_text(
            "⚠️ No matching pending signal found.", parse_mode="Markdown")
        return

    strategy = entry.get("strategy", "SMC")
    stats.record_result(entry["symbol"], win=True, strategy=strategy)

    today   = stats.get_today_stats()
    wr      = stats.get_win_rate()
    streak  = stats.data.get("streak", 0)
    streak_txt = f"🔥 Streak: `{streak}`" if streak > 1 else ""

    await update.message.reply_text(
        f"✅ *WIN RECORDED!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{entry['symbol']}`  |  🧩 `{strategy}`\n"
        f"📍 Session: {entry.get('session','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Overall Win Rate*: `{wr}%`\n"
        f"📅 *Today*: ✅ {today['wins']}W  ❌ {today['losses']}L\n"
        f"{streak_txt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💪 Use /stats for the full breakdown.",
        parse_mode="Markdown")

async def cmd_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text(
            "⚠️ No pending signals to record.", parse_mode="Markdown")
        return

    entry = _pop_pending(context.args)
    if entry is None:
        await update.message.reply_text(
            "⚠️ No matching pending signal found.", parse_mode="Markdown")
        return

    strategy = entry.get("strategy", "SMC")
    stats.record_result(entry["symbol"], win=False, strategy=strategy)

    today  = stats.get_today_stats()
    wr     = stats.get_win_rate()

    await update.message.reply_text(
        f"❌ *LOSS RECORDED*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{entry['symbol']}`  |  🧩 `{strategy}`\n"
        f"📍 Session: {entry.get('session','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Overall Win Rate*: `{wr}%`\n"
        f"📅 *Today*: ✅ {today['wins']}W  ❌ {today['losses']}L\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧘 Use /stats for the full breakdown.",
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
# ERROR HANDLER
# ──────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import NetworkError, TimedOut
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning(f"Network error (will retry): {err}")
    else:
        log.error(f"Unhandled error: {err}", exc_info=context.error)


# ──────────────────────────────────────────────────
# MAIN — webhook mode
# ──────────────────────────────────────────────────
def main():
    global telegram_bot

    if not WEBHOOK_URL:
        log.critical(
            "WEBHOOK_URL env var is not set!\n"
            "Go to Railway → your service → Variables and add:\n"
            "  WEBHOOK_URL = https://<your-service>.up.railway.app"
        )
        raise SystemExit(1)

    async def post_init(application):
        global telegram_bot
        telegram_bot = application.bot
        if application.job_queue is not None:
            application.job_queue.run_repeating(
                scan_and_send, interval=SCAN_EVERY * 60, first=30
            )
            log.info("Job queue scanner started.")
        else:
            log.warning("JobQueue not available — install python-telegram-bot[job-queue]")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .post_init(post_init)
        .build()
    )

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
    app.add_error_handler(error_handler)

    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"

    log.info(f"Starting webhook on port {WEBHOOK_PORT}")
    log.info(f"Webhook URL: {full_webhook_url}")

    app.run_webhook(
        listen="0.0.0.0",
        port=WEBHOOK_PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
