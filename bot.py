"""
╔══════════════════════════════════════════════════════════════╗
║   SMC PRO SIGNAL BOT — SELF-LEARNING EDITION               ║
║   - Learns from your win/loss history                       ║
║   - Auto-adjusts pair weights & confidence thresholds       ║
║   - Suppresses losing pairs/sessions automatically          ║
║   - Re-evaluates every 10 trades                            ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio, json, os, logging, requests, ta, numpy as np, pandas as pd, signal
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ── CONFIG ────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
CHAT_ID     = os.getenv("CHAT_ID",   "")
TD_API_KEY  = os.getenv("TD_API_KEY","")
WEBHOOK_URL = os.getenv("WEBHOOK_URL","")
WEBHOOK_PORT= int(os.getenv("PORT","8080"))

SCAN_EVERY      = 5       # minutes between scans
MIN_SCORE       = 2       # minimum SMC confluence factors
BASE_CONFIDENCE = 55      # base confidence threshold (%)
PRE_SIGNAL_MIN  = 40      # pre-signal alert threshold (%)
MAX_SCORE       = 7
STATS_FILE      = "stats.json"
OUTCOME_FILE    = "outcomes.json"   # pending outcome checks
OUTCOME_CHECK_MINS = 15             # check outcome after this many minutes
LEARN_FILE      = "learning.json"

PAIRS = {
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "BTCUSD": "BTC/USD",
}

SESSIONS = [(8, 12), (13, 17)]

# ── LOGGING ───────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── GLOBAL STATE ──────────────────────────────────────────────
telegram_bot   = None
bot_paused     = False
presignal_sent = {}

# ══════════════════════════════════════════════════════════════
# SELF-LEARNING ENGINE
# ══════════════════════════════════════════════════════════════
class LearningEngine:
    """
    Tracks performance per pair and per session.
    Auto-adjusts confidence thresholds and pair weights.
    Re-evaluates every 10 trades.
    """
    def __init__(self):
        self.data = self._load()

    def _default(self):
        return {
            "pair_stats": {p: {"wins":0,"losses":0} for p in PAIRS},
            "session_stats": {"london":{"wins":0,"losses":0},"newyork":{"wins":0,"losses":0}},
            "pair_weights": {p: 1.0 for p in PAIRS},
            "pair_threshold": {p: BASE_CONFIDENCE for p in PAIRS},
            "suppressed_pairs": [],
            "total_evaluated": 0,
            "last_evaluation": None,
        }

    def _load(self):
        try:
            if Path(LEARN_FILE).exists():
                with open(LEARN_FILE) as f:
                    data = json.load(f)
                # ensure all pairs exist
                for p in PAIRS:
                    if p not in data["pair_stats"]:
                        data["pair_stats"][p] = {"wins":0,"losses":0}
                    if p not in data["pair_weights"]:
                        data["pair_weights"][p] = 1.0
                    if p not in data["pair_threshold"]:
                        data["pair_threshold"][p] = BASE_CONFIDENCE
                return data
        except Exception as e:
            log.warning(f"Learning load error: {e}")
        return self._default()

    def _save(self):
        with open(LEARN_FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    def record(self, symbol: str, session: str, win: bool):
        """Record a trade result and trigger re-evaluation every 10 trades."""
        ps = self.data["pair_stats"].get(symbol, {"wins":0,"losses":0})
        if win: ps["wins"] += 1
        else:   ps["losses"] += 1
        self.data["pair_stats"][symbol] = ps

        sess_key = "london" if "London" in session else "newyork"
        ss = self.data["session_stats"].get(sess_key, {"wins":0,"losses":0})
        if win: ss["wins"] += 1
        else:   ss["losses"] += 1
        self.data["session_stats"][sess_key] = ss

        self.data["total_evaluated"] = self.data.get("total_evaluated", 0) + 1

        # Re-evaluate every 10 trades
        if self.data["total_evaluated"] % 10 == 0:
            self._evaluate()

        self._save()

    def _win_rate(self, stats: dict) -> float:
        total = stats["wins"] + stats["losses"]
        return stats["wins"] / total if total > 0 else 0.5

    def _evaluate(self):
        """
        Auto-adjust pair weights and thresholds based on performance.
        Rules:
        - Win rate > 70% → lower threshold by 5% (easier to signal = more trades)
        - Win rate 55-70% → keep threshold same
        - Win rate 40-55% → raise threshold by 5% (harder to signal = fewer but better)
        - Win rate < 40% → suppress pair (no signals until performance improves)
        - Win rate < 30% with 10+ trades → block pair completely
        """
        log.info("🧠 Self-learning: re-evaluating performance...")
        suppressed = []

        for pair, stats in self.data["pair_stats"].items():
            total = stats["wins"] + stats["losses"]
            if total < 5:
                log.info(f"  {pair}: insufficient data ({total} trades) — skipping")
                continue

            wr = self._win_rate(stats)
            current_threshold = self.data["pair_threshold"].get(pair, BASE_CONFIDENCE)

            if wr >= 0.70:
                # Great performer — lower threshold to get more signals
                new_threshold = max(45, current_threshold - 5)
                self.data["pair_weights"][pair] = 1.3
                log.info(f"  {pair}: ✅ {wr:.0%} WR → threshold {current_threshold}→{new_threshold}% (boosted)")
            elif wr >= 0.55:
                # Good performer — keep as is
                new_threshold = current_threshold
                self.data["pair_weights"][pair] = 1.0
                log.info(f"  {pair}: 🟡 {wr:.0%} WR → threshold unchanged ({current_threshold}%)")
            elif wr >= 0.40:
                # Poor performer — raise threshold
                new_threshold = min(75, current_threshold + 5)
                self.data["pair_weights"][pair] = 0.8
                log.info(f"  {pair}: ⚠️ {wr:.0%} WR → threshold {current_threshold}→{new_threshold}% (tightened)")
            else:
                # Bad performer — suppress
                new_threshold = min(80, current_threshold + 10)
                self.data["pair_weights"][pair] = 0.5
                suppressed.append(pair)
                log.info(f"  {pair}: 🔴 {wr:.0%} WR → SUPPRESSED (threshold raised to {new_threshold}%)")

            self.data["pair_threshold"][pair] = new_threshold

        self.data["suppressed_pairs"] = suppressed
        self.data["last_evaluation"] = datetime.utcnow().isoformat()
        log.info(f"🧠 Evaluation complete. Suppressed pairs: {suppressed}")

    def get_threshold(self, symbol: str) -> int:
        return self.data["pair_threshold"].get(symbol, BASE_CONFIDENCE)

    def is_suppressed(self, symbol: str) -> bool:
        return symbol in self.data.get("suppressed_pairs", [])

    def get_weight(self, symbol: str) -> float:
        return self.data["pair_weights"].get(symbol, 1.0)

    def summary(self) -> str:
        lines = ["🧠 *Self-Learning Status*\n━━━━━━━━━━━━━━━━━━━━━━"]
        for pair, stats in self.data["pair_stats"].items():
            total = stats["wins"] + stats["losses"]
            wr = self._win_rate(stats) * 100 if total > 0 else 0
            threshold = self.get_threshold(pair)
            weight = self.get_weight(pair)
            suppressed = "🔴 SUPPRESSED" if self.is_suppressed(pair) else ""
            lines.append(
                f"`{pair}`: {stats['wins']}W/{stats['losses']}L "
                f"({wr:.0f}%) | threshold: {threshold}% | weight: {weight:.1f}x {suppressed}"
            )
        lines.append(f"\n📊 Total evaluated: {self.data.get('total_evaluated',0)}")
        last = self.data.get('last_evaluation','Never')
        if last and last != 'Never':
            last = last[:16].replace('T',' ')
        lines.append(f"🕐 Last re-evaluation: {last}")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# STATS TRACKER
# ══════════════════════════════════════════════════════════════
class StatsTracker:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        try:
            if Path(STATS_FILE).exists():
                with open(STATS_FILE) as f:
                    return json.load(f)
        except: pass
        return {"trades":[], "pending":[], "streak":0, "best_streak":0, "trade_counter":0}

    def next_trade_id(self) -> str:
        self.data["trade_counter"] = self.data.get("trade_counter", 0) + 1
        self._save()
        return f"#TRD-{self.data['trade_counter']:03d}"

    def _save(self):
        with open(STATS_FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    def add_pending(self, sig: dict):
        trade_id = self.next_trade_id()
        entry = sig["price"]
        sl, tp1, tp2, tp3 = calculate_sl_tp(entry, sig["raw_dir"], sig["symbol"])
        self.data.setdefault("pending",[]).append({
            "trade_id":  trade_id,
            "symbol":    sig["symbol"],
            "direction": sig["direction"],
            "raw_dir":   sig["raw_dir"],
            "session":   sig["session"],
            "entry":     entry,
            "sl":        sl,
            "tp1":       tp1,
            "tp2":       tp2,
            "tp3":       tp3,
            "time":      datetime.utcnow().isoformat(),
        })
        self._save()
        return trade_id

    def record_result(self, symbol: str, win: bool, session: str = ""):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        self.data.setdefault("trades",[]).append({
            "symbol": symbol, "win": win,
            "date": today, "session": session,
        })
        if win:
            self.data["streak"] = self.data.get("streak",0) + 1
            self.data["best_streak"] = max(
                self.data.get("best_streak",0), self.data["streak"]
            )
        else:
            self.data["streak"] = 0
        self._save()

    def get_today_stats(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        today_trades = [t for t in self.data.get("trades",[]) if t.get("date")==today]
        return {
            "wins":   sum(1 for t in today_trades if t["win"]),
            "losses": sum(1 for t in today_trades if not t["win"]),
        }

    def get_win_rate(self):
        trades = self.data.get("trades",[])
        if not trades: return 0.0
        return round(sum(1 for t in trades if t["win"]) / len(trades) * 100, 1)

    def get_pair_stats(self):
        result = {}
        for t in self.data.get("trades",[]):
            s = t["symbol"]
            result.setdefault(s, {"wins":0,"losses":0})
            if t["win"]: result[s]["wins"] += 1
            else:        result[s]["losses"] += 1
        return result

    def full_report(self) -> str:
        trades  = self.data.get("trades",[])
        total   = len(trades)
        wins    = sum(1 for t in trades if t["win"])
        losses  = total - wins
        wr      = self.get_win_rate()
        today   = self.get_today_stats()
        streak  = self.data.get("streak",0)
        best    = self.data.get("best_streak",0)

        # Win rate bar
        filled  = round(wr / 10)
        wr_bar  = "🟢" * filled + "⬜" * (10 - filled)

        # Pair breakdown
        pair_stats = self.get_pair_stats()
        pair_lines = []
        best_pair  = None
        best_wr    = 0
        for pair, ps in pair_stats.items():
            t = ps["wins"] + ps["losses"]
            pwr = round(ps["wins"]/t*100,1) if t>0 else 0
            icon = "🟢" if pwr >= 55 else "🔴"
            pair_lines.append(f"  {icon} {pair}: {ps['wins']}W/{ps['losses']}L ({pwr}%)")
            if pwr > best_wr:
                best_wr = pwr
                best_pair = pair

        pair_section = "\n".join(pair_lines) if pair_lines else "  No data yet"

        return (
            f"📊 *BOT PERFORMANCE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *Win Rate*\n"
            f"{wr_bar} `{wr}%`\n\n"
            f"📈 Total: `{total}` | ✅ Wins: `{wins}` | ❌ Losses: `{losses}`\n"
            f"📅 Today: ✅ `{today['wins']}W` ❌ `{today['losses']}L`\n"
            f"🔥 Streak: `{streak}` 🏅 Best: `{best}`\n"
            f"💎 Best Pair: `{best_pair or 'N/A'}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Pair Breakdown*\n{pair_section}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 _Bot is self-learning from your results_"
        )


# ══════════════════════════════════════════════════════════════
# SESSION HELPERS
# ══════════════════════════════════════════════════════════════
def is_weekend():
    return datetime.utcnow().weekday() >= 5

def is_active_session(symbol: str = "") -> bool:
    if symbol == "BTCUSD":
        return True
    if is_weekend():
        return False
    hour = datetime.utcnow().hour
    return any(s <= hour < e for s, e in SESSIONS)

def session_name(symbol: str = "") -> str:
    if symbol == "BTCUSD":
        return "₿ Crypto 24/7"
    if is_weekend():
        return "📴 Weekend — Forex Closed"
    hour = datetime.utcnow().hour
    if 8 <= hour < 12:  return "🇬🇧 London Session"
    if 13 <= hour < 17: return "🇺🇸 New York Session"
    return "😴 Off-Session"


# ══════════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════════
def fetch_candles(symbol: str, interval="15min", outputsize=100) -> pd.DataFrame | None:
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
        df = df.rename(columns={"open":"Open","high":"High","low":"Low","close":"Close"})
        for col in ["Open","High","Low","Close"]:
            df[col] = pd.to_numeric(df[col])
        return df.iloc[::-1].reset_index(drop=True)
    except Exception as e:
        log.warning(f"Fetch error {symbol}: {e}")
        return None

def fetch_htf_candles(symbol: str) -> pd.DataFrame | None:
    return fetch_candles(symbol, interval="1h", outputsize=200)

def fetch_binance_candles(symbol: str = "BTCUSDT", interval: str = "15m", limit: int = 100):
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol":symbol,"interval":interval,"limit":limit}, timeout=10)
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        df = pd.DataFrame(data, columns=[
            "OpenTime","Open","High","Low","Close","Volume",
            "CloseTime","QAV","NT","TBBAV","TBQAV","Ignore"
        ])
        for col in ["Open","High","Low","Close"]:
            df[col] = pd.to_numeric(df[col])
        return df.reset_index(drop=True)
    except Exception as e:
        log.warning(f"Binance error {symbol}: {e}")
        return None

def fetch_binance_htf(symbol: str = "BTCUSDT"):
    return fetch_binance_candles(symbol, interval="1h", limit=200)


# ══════════════════════════════════════════════════════════════
# SL / TP CALCULATOR (MT5-optimized)
# ══════════════════════════════════════════════════════════════
def calculate_sl_tp(entry: float, direction: str, symbol: str):
    if symbol == "XAUUSD":
        sl_pct  = 0.0012
        tp_pcts = [0.0018, 0.0024, 0.0036]
    elif symbol == "BTCUSD":
        sl_pct  = 0.0040
        tp_pcts = [0.0060, 0.0080, 0.0120]
    elif symbol == "USDJPY":
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


# ══════════════════════════════════════════════════════════════
# SMC ANALYZER
# ══════════════════════════════════════════════════════════════
class SMCAnalyzer:

    def get_indicators(self, df):
        if len(df) < 30:
            return None, {}
        close = df["Close"]
        high  = df["High"]
        low   = df["Low"]

        rsi   = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        bb    = ta.volatility.BollingerBands(close, window=20)
        bb_up = bb.bollinger_hband().iloc[-1]
        bb_lo = bb.bollinger_lband().iloc[-1]
        price = close.iloc[-1]

        bull_rsi = rsi < 45
        bear_rsi = rsi > 55
        bull_bb  = price <= bb_lo * 1.001
        bear_bb  = price >= bb_up * 0.999

        if bull_rsi and not bear_rsi:   bias = "CALL"
        elif bear_rsi and not bull_rsi: bias = "PUT"
        else:                           bias = None

        meta = {
            "rsi":rsi,"price":price,
            "bull_rsi":bull_rsi,"bear_rsi":bear_rsi,
            "bull_bb":bull_bb,"bear_bb":bear_bb,
        }
        return bias, meta

    def detect_fvg(self, df):
        bull = bear = False
        for i in range(2, min(len(df), 15)):
            if df["Low"].iloc[i] > df["High"].iloc[i-2]:   bull = True
            if df["High"].iloc[i] < df["Low"].iloc[i-2]:   bear = True
        return bull, bear

    def detect_liquidity_sweep(self, df):
        if len(df) < 20: return False, False
        recent_high = df["High"].iloc[-20:-1].max()
        recent_low  = df["Low"].iloc[-20:-1].min()
        last_high   = df["High"].iloc[-1]
        last_low    = df["Low"].iloc[-1]
        last_close  = df["Close"].iloc[-1]
        bull = last_low < recent_low and last_close > recent_low
        bear = last_high > recent_high and last_close < recent_high
        return bull, bear

    def detect_mss(self, df):
        if len(df) < 10: return False, False
        highs = df["High"].iloc[-10:]
        lows  = df["Low"].iloc[-10:]
        bull  = lows.iloc[-1] > lows.iloc[:-1].mean()
        bear  = highs.iloc[-1] < highs.iloc[:-1].mean()
        return bull, bear

    def detect_order_block(self, df):
        if len(df) < 5: return False, False
        body   = abs(df["Close"].iloc[-3] - df["Open"].iloc[-3])
        range_ = df["High"].iloc[-3] - df["Low"].iloc[-3]
        strong = body > range_ * 0.6
        bull   = strong and df["Close"].iloc[-3] > df["Open"].iloc[-3]
        bear   = strong and df["Close"].iloc[-3] < df["Open"].iloc[-3]
        return bull, bear

    def detect_engulfing(self, df):
        if len(df) < 2: return False, False
        prev_body = df["Close"].iloc[-2] - df["Open"].iloc[-2]
        curr_body = df["Close"].iloc[-1] - df["Open"].iloc[-1]
        bull = curr_body > 0 and prev_body < 0 and abs(curr_body) > abs(prev_body)
        bear = curr_body < 0 and prev_body > 0 and abs(curr_body) > abs(prev_body)
        return bull, bear

    def get_htf_bias(self, td_symbol: str, orig_symbol: str = "") -> str | None:
        df = fetch_binance_htf("BTCUSDT") if orig_symbol == "BTCUSD" else fetch_htf_candles(td_symbol)
        if df is None or len(df) < 20: return None
        close = df["Close"]
        ema20 = close.ewm(span=20).mean().iloc[-1]
        ema50 = close.ewm(span=50).mean().iloc[-1]
        if close.iloc[-1] > ema20 > ema50: return "CALL"
        if close.iloc[-1] < ema20 < ema50: return "PUT"
        return None

    def analyze(self, symbol: str, td_symbol: str, learning: "LearningEngine"):
        if not is_active_session(symbol):
            return None, None

        # Check suppression
        if learning.is_suppressed(symbol):
            log.info(f"🔴 {symbol} suppressed by learning engine — skip")
            return None, None

        # Fetch data
        if symbol == "BTCUSD":
            df = fetch_binance_candles("BTCUSDT", interval="15m", limit=100)
        else:
            df = fetch_candles(td_symbol, interval="15min", outputsize=100)

        if df is None or len(df) < 30:
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

        is_bull = (bias == "CALL")
        factors = [
            (fvg_b  if is_bull else fvg_r,  "📦 Fair Value Gap (Bull)" if is_bull else "📦 Fair Value Gap (Bear)"),
            (liq_b  if is_bull else liq_r,  "💧 Liquidity Sweep (Low)" if is_bull else "💧 Liquidity Sweep (High)"),
            (mss_b  if is_bull else mss_r,  "📐 MSS Bullish" if is_bull else "📐 MSS Bearish"),
            (ob_b   if is_bull else ob_r,   "🟫 Order Block (Bull)" if is_bull else "🟫 Order Block (Bear)"),
            (eng_b  if is_bull else eng_r,  "🕯 Bullish Engulfing" if is_bull else "🕯 Bearish Engulfing"),
            (meta["bull_rsi"] if is_bull else meta["bear_rsi"],
             f"📊 RSI {'Bullish' if is_bull else 'Bearish'} ({meta['rsi']:.1f})"),
            (meta["bull_bb"]  if is_bull else meta["bear_bb"],
             f"📉 Price at BB {'Low' if is_bull else 'High'}"),
        ]

        active  = [name for hit, name in factors if hit]
        pending = [name for hit, name in factors if not hit]
        score   = len(active)
        emoji   = "✅ CALL" if is_bull else "🔴 PUT"

        if score < MIN_SCORE:
            return None, None

        # ── CONFIDENCE CALCULATION ──────────────────────
        confidence = round((score / MAX_SCORE) * 100)

        # Bonus: HTF alignment
        htf = self.get_htf_bias(td_symbol, symbol)
        if htf == bias:
            confidence = min(confidence + 8, 100)

        # Bonus: RSI + BB both confirm
        if (meta["bull_rsi"] and meta["bull_bb"] and is_bull) or \
           (meta["bear_rsi"] and meta["bear_bb"] and not is_bull):
            confidence = min(confidence + 5, 100)

        # Apply learning weight (boosts/reduces confidence based on pair history)
        weight = learning.get_weight(symbol)
        confidence = min(int(confidence * weight), 100)

        # Get pair-specific threshold from learning engine
        threshold = learning.get_threshold(symbol)

        sig = {
            "symbol":       symbol,
            "direction":    emoji,
            "raw_dir":      bias,
            "price":        round(meta["price"], 5),
            "rsi":          meta["rsi"],
            "score":        score,
            "confidence":   confidence,
            "threshold":    threshold,
            "smc_tags":     active,
            "pending_tags": pending,
            "session":      session_name(symbol),
            "htf_bias":     htf or "Neutral",
            "weight":       weight,
        }

        if confidence >= threshold:
            return sig, "signal"
        elif confidence >= PRE_SIGNAL_MIN:
            return sig, "presignal"
        else:
            log.info(f"{symbol} confidence {confidence}% < {PRE_SIGNAL_MIN}% (threshold {threshold}%) — skip")
            return None, None



# ══════════════════════════════════════════════════════════════
# STRATEGY 2: TREND PULLBACK
# ══════════════════════════════════════════════════════════════
class TrendPullbackAnalyzer:
    def analyze(self, symbol, td_symbol, df, htf_bias, learning):
        if len(df) < 55: return None, None
        close = df["Close"]; high = df["High"]; low = df["Low"]
        ema20 = close.ewm(span=20).mean()
        ema50 = close.ewm(span=50).mean()
        rsi   = ta.momentum.RSIIndicator(close, window=14).rsi()
        price = close.iloc[-1]; e20 = ema20.iloc[-1]; e50 = ema50.iloc[-1]; rsi_val = rsi.iloc[-1]
        uptrend   = e20 > e50 and close.iloc[-3] > e20
        downtrend = e20 < e50 and close.iloc[-3] < e20
        bull_pb = uptrend   and low.iloc[-1]  <= e20 * 1.001 and price > e20 and rsi_val < 55
        bear_pb = downtrend and high.iloc[-1] >= e20 * 0.999 and price < e20 and rsi_val > 45
        if not bull_pb and not bear_pb: return None, None
        bias = "CALL" if bull_pb else "PUT"
        emoji = "✅ CALL" if bull_pb else "🔴 PUT"
        tags = ([f"📈 Uptrend EMA20>{round(e20,5)}", "🔄 Pullback to EMA20", f"📊 RSI {rsi_val:.1f}"]
                if bull_pb else
                [f"📉 Downtrend EMA20<{round(e20,5)}", "🔄 Pullback to EMA20", f"📊 RSI {rsi_val:.1f}"])
        confidence = min(62 + (10 if htf_bias == bias else 0), 100)
        weight = learning.get_weight(symbol)
        confidence = min(int(confidence * weight), 100)
        threshold = learning.get_threshold(symbol)
        sig = {"symbol":symbol,"direction":emoji,"raw_dir":bias,"price":round(price,5),"rsi":rsi_val,
               "score":len(tags),"confidence":confidence,"threshold":threshold,"smc_tags":tags,
               "pending_tags":[],"session":session_name(symbol),"htf_bias":htf_bias or "Neutral",
               "weight":weight,"strategy":"Trend Pullback"}
        if confidence >= threshold: return sig, "signal"
        if confidence >= PRE_SIGNAL_MIN: return sig, "presignal"
        return None, None


# ══════════════════════════════════════════════════════════════
# STRATEGY 3: LONDON BREAKOUT
# ══════════════════════════════════════════════════════════════
class LondonBreakoutAnalyzer:
    def analyze(self, symbol, td_symbol, df, htf_bias, learning):
        now = datetime.utcnow(); hour = now.hour
        if not (8 <= hour < 9) or symbol == "BTCUSD" or len(df) < 20: return None, None
        close = df["Close"]; high = df["High"]; low = df["Low"]
        pre_high = high.iloc[-10:-2].max(); pre_low = low.iloc[-10:-2].min()
        price = close.iloc[-1]; rng = pre_high - pre_low
        if rng < price * 0.0005: return None, None
        bull_break = price > pre_high * 1.0002
        bear_break = price < pre_low  * 0.9998
        if not bull_break and not bear_break: return None, None
        bias = "CALL" if bull_break else "PUT"
        emoji = "✅ CALL" if bull_break else "🔴 PUT"
        tags = [f"🇬🇧 London Breakout {'Above' if bull_break else 'Below'} Range",
                f"📏 Range: {round(pre_low,5)}—{round(pre_high,5)}",
                f"💥 Break Price: {round(price,5)}"]
        confidence = min(68 + (10 if htf_bias == bias else 0), 100)
        weight = learning.get_weight(symbol)
        confidence = min(int(confidence * weight), 100)
        threshold = learning.get_threshold(symbol)
        sig = {"symbol":symbol,"direction":emoji,"raw_dir":bias,"price":round(price,5),"rsi":50,
               "score":len(tags),"confidence":confidence,"threshold":threshold,"smc_tags":tags,
               "pending_tags":[],"session":session_name(symbol),"htf_bias":htf_bias or "Neutral",
               "weight":weight,"strategy":"London Breakout"}
        if confidence >= threshold: return sig, "signal"
        if confidence >= PRE_SIGNAL_MIN: return sig, "presignal"
        return None, None


# ══════════════════════════════════════════════════════════════
# STRATEGY 4: PRICE ACTION + MARKET STRUCTURE
# ══════════════════════════════════════════════════════════════
class PriceActionAnalyzer:
    def analyze(self, symbol, td_symbol, df, htf_bias, learning):
        if len(df) < 20: return None, None
        close = df["Close"]; high = df["High"]; low = df["Low"]
        price = close.iloc[-1]
        hh = high.iloc[-1] > high.iloc[-3] > high.iloc[-5]
        hl = low.iloc[-1]  > low.iloc[-3]  > low.iloc[-5]
        lh = high.iloc[-1] < high.iloc[-3] < high.iloc[-5]
        ll = low.iloc[-1]  < low.iloc[-3]  < low.iloc[-5]
        bull_struct = hh and hl; bear_struct = lh and ll
        if not bull_struct and not bear_struct: return None, None
        body = abs(close.iloc[-1] - df["Open"].iloc[-1])
        candle = high.iloc[-1] - low.iloc[-1]
        pin_bar = candle > 0 and body < candle * 0.35
        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        bull_ok = bull_struct and rsi_val < 60
        bear_ok = bear_struct and rsi_val > 40
        if not bull_ok and not bear_ok: return None, None
        bias = "CALL" if bull_ok else "PUT"
        emoji = "✅ CALL" if bull_ok else "🔴 PUT"
        tags = (["📈 HH+HL Bullish Structure", f"📊 RSI {rsi_val:.1f}"] +
                (["📌 Bullish Pin Bar"] if pin_bar else [])
                if bull_ok else
                ["📉 LH+LL Bearish Structure", f"📊 RSI {rsi_val:.1f}"] +
                (["📌 Bearish Pin Bar"] if pin_bar else []))
        confidence = min(60 + (10 if htf_bias == bias else 0) + (8 if pin_bar else 0), 100)
        weight = learning.get_weight(symbol)
        confidence = min(int(confidence * weight), 100)
        threshold = learning.get_threshold(symbol)
        sig = {"symbol":symbol,"direction":emoji,"raw_dir":bias,"price":round(price,5),"rsi":rsi_val,
               "score":len(tags),"confidence":confidence,"threshold":threshold,"smc_tags":tags,
               "pending_tags":[],"session":session_name(symbol),"htf_bias":htf_bias or "Neutral",
               "weight":weight,"strategy":"Price Action + Structure"}
        if confidence >= threshold: return sig, "signal"
        if confidence >= PRE_SIGNAL_MIN: return sig, "presignal"
        return None, None


# ══════════════════════════════════════════════════════════════
# STRATEGY 5: TOP DOWN ANALYSIS
# ══════════════════════════════════════════════════════════════
class TopDownAnalyzer:
    def analyze(self, symbol, td_symbol, df, htf_bias, learning):
        if len(df) < 30 or htf_bias is None: return None, None
        close = df["Close"]; high = df["High"]; low = df["Low"]
        price = close.iloc[-1]
        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        ema20 = close.ewm(span=20).mean().iloc[-1]
        if htf_bias == "CALL":
            m15_ok = price > ema20 and rsi_val < 65
            touched = low.iloc[-3:].min() <= ema20 * 1.002
        else:
            m15_ok = price < ema20 and rsi_val > 35
            touched = high.iloc[-3:].max() >= ema20 * 0.998
        if not m15_ok or not touched: return None, None
        bias = htf_bias
        emoji = "✅ CALL" if bias == "CALL" else "🔴 PUT"
        tags = [f"🔭 1H Bias: {htf_bias} (Top Down Confirmed)",
                "📐 15M Aligned with HTF",
                f"🎯 EMA20 Entry Zone ({round(ema20,5)})",
                f"📊 RSI: {rsi_val:.1f}"]
        confidence = 72
        weight = learning.get_weight(symbol)
        confidence = min(int(confidence * weight), 100)
        threshold = learning.get_threshold(symbol)
        sig = {"symbol":symbol,"direction":emoji,"raw_dir":bias,"price":round(price,5),"rsi":rsi_val,
               "score":len(tags),"confidence":confidence,"threshold":threshold,"smc_tags":tags,
               "pending_tags":[],"session":session_name(symbol),"htf_bias":htf_bias or "Neutral",
               "weight":weight,"strategy":"Top Down Analysis"}
        if confidence >= threshold: return sig, "signal"
        if confidence >= PRE_SIGNAL_MIN: return sig, "presignal"
        return None, None


# ══════════════════════════════════════════════════════════════
# MARKET CONDITION DETECTOR
# ══════════════════════════════════════════════════════════════
def detect_market_condition(df):
    if len(df) < 30: return "ranging"
    try:
        close = df["Close"]; high = df["High"]; low = df["Low"]
        adx_ind = ta.trend.ADXIndicator(high, low, close, window=14)
        adx_val = adx_ind.adx().iloc[-1]
        dip     = adx_ind.adx_pos().iloc[-1]
        din     = adx_ind.adx_neg().iloc[-1]
        ema20   = close.ewm(span=20).mean().iloc[-1]
        ema50   = close.ewm(span=50).mean().iloc[-1]
        if adx_val > 25:
            if dip > din and ema20 > ema50: return "trending_up"
            if din > dip and ema20 < ema50: return "trending_down"
        return "ranging"
    except:
        return "ranging"


# ══════════════════════════════════════════════════════════════
# ADAPTIVE STRATEGY SELECTOR
# ══════════════════════════════════════════════════════════════
class AdaptiveStrategySelector:
    def __init__(self):
        self.smc      = SMCAnalyzer()
        self.pullback = TrendPullbackAnalyzer()
        self.breakout = LondonBreakoutAnalyzer()
        self.pa       = PriceActionAnalyzer()
        self.topdown  = TopDownAnalyzer()

    def select(self, symbol, td_symbol, learning):
        if not is_active_session(symbol): return None, None
        if learning.is_suppressed(symbol):
            log.info(f"🔴 {symbol} suppressed — skip")
            return None, None
        if symbol == "BTCUSD":
            df = fetch_binance_candles("BTCUSDT", interval="15m", limit=100)
        else:
            df = fetch_candles(td_symbol, interval="15min", outputsize=100)
        if df is None or len(df) < 30: return None, None
        htf = self.smc.get_htf_bias(td_symbol, symbol)
        condition = detect_market_condition(df)
        hour = datetime.utcnow().hour
        log.info(f"  {symbol}: cond={condition}, htf={htf}, hour={hour}UTC")
        candidates = []
        if 8 <= hour < 9:
            r = self.breakout.analyze(symbol, td_symbol, df, htf, learning)
            if r[0]: candidates.append(r)
        if htf:
            r = self.topdown.analyze(symbol, td_symbol, df, htf, learning)
            if r[0]: candidates.append(r)
        if condition in ("trending_up","trending_down"):
            r = self.pullback.analyze(symbol, td_symbol, df, htf, learning)
            if r[0]: candidates.append(r)
        r = self.pa.analyze(symbol, td_symbol, df, htf, learning)
        if r[0]: candidates.append(r)
        r = self.smc.analyze(symbol, td_symbol, learning)
        if r[0]: candidates.append(r)
        if not candidates: return None, None
        best_sig, best_type = max(candidates, key=lambda x: x[0]["confidence"])
        log.info(f"  ✅ Best: {best_sig.get('strategy','SMC')} ({best_sig['confidence']}%)")
        return best_sig, best_type


# ══════════════════════════════════════════════════════════════
# SIGNAL FORMATTERS
# ══════════════════════════════════════════════════════════════
def confidence_bar(pct: int, color: str = "🟢") -> str:
    filled = round(pct / 10)
    return color * filled + "⬜" * (10 - filled) + f" `{pct}%`"

def format_signal(sig: dict, trade_id: str = "") -> str:
    now   = datetime.utcnow()
    lines = "\n".join(f"    {t}" for t in sig["smc_tags"])
    stars = "⭐" * sig["score"]
    entry = sig["price"]
    conf  = sig["confidence"]
    sl, tp1, tp2, tp3 = calculate_sl_tp(entry, sig["raw_dir"], sig["symbol"])

    if conf >= 85:   conf_label = "🔥 ELITE"
    elif conf >= 70: conf_label = "💎 HIGH"
    else:            conf_label = "✅ GOOD"

    weight = sig.get("weight", 1.0)
    learn_note = ""
    if weight > 1.0:   learn_note = "🧠 _Boosted by learning engine_"
    elif weight < 1.0: learn_note = "🧠 _Filtered by learning engine_"

    tid_line = f"🔖 Trade ID: `{trade_id}`\n" if trade_id else ""

    return (
        f"🚨 *{sig.get('strategy', 'SMC PRO')} SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{tid_line}"
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
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 *SMC Confluence* {stars}\n"
        f"{lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Confidence*: {conf_label}\n"
        f"{confidence_bar(conf)}\n"
        f"{learn_note}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💥 Strength: *{'🔥 VERY STRONG' if sig['score']>=6 else '💪 STRONG' if sig['score']>=4 else '✅ GOOD'}*\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_\n"
        f"📝 _Reply /win {trade_id} or /loss {trade_id} to record_"
    )

def format_presignal(sig: dict) -> str:
    now    = datetime.utcnow()
    active = "\n".join(f"    ✅ {t}" for t in sig["smc_tags"])
    pend   = "\n".join(f"    ⏳ {t}" for t in sig["pending_tags"])
    conf   = sig["confidence"]
    needed = sig["threshold"] - conf
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
        f"⏳ *Still needs:*\n{pend}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Setup Confidence*: `{conf}%` _(+{needed}% for entry signal)_\n"
        f"{confidence_bar(conf, '🟡')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👁 *Watch `{sig['symbol']}` — signal may fire soon!*\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`"
    )


# ══════════════════════════════════════════════════════════════
# AUTO-OUTCOME TRACKER
# ══════════════════════════════════════════════════════════════
class OutcomeTracker:
    """
    Tracks pending trades and auto-detects win/loss by checking
    if price hit TP1 or SL using candle High/Low (catches wicks).
    After auto-detection, sends confirmation buttons to user.
    """
    def __init__(self):
        self.data = self._load()

    def _load(self):
        try:
            if Path(OUTCOME_FILE).exists():
                with open(OUTCOME_FILE) as f:
                    return json.load(f)
        except: pass
        return {"pending": []}

    def _save(self):
        with open(OUTCOME_FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    def add(self, trade_id, symbol, raw_dir, entry, sl, tp1, tp2, tp3, session):
        self.data["pending"].append({
            "trade_id":   trade_id,
            "symbol":     symbol,
            "raw_dir":    raw_dir,
            "entry":      entry,
            "sl":         sl,
            "tp1":        tp1,
            "tp2":        tp2,
            "tp3":        tp3,
            "session":    session,
            "time":       datetime.utcnow().isoformat(),
            "check_after": (datetime.utcnow() + timedelta(minutes=OUTCOME_CHECK_MINS)).isoformat(),
            "confirmed":  False,
        })
        self._save()

    def get_due(self):
        """Return trades whose check time has passed and are not yet confirmed."""
        now = datetime.utcnow()
        due = []
        for t in self.data["pending"]:
            if t.get("confirmed"): continue
            check_time = datetime.fromisoformat(t["check_after"])
            if now >= check_time:
                due.append(t)
        return due

    def mark_confirmed(self, trade_id):
        for t in self.data["pending"]:
            if t["trade_id"] == trade_id:
                t["confirmed"] = True
        self._save()

    def check_outcome(self, trade: dict) -> str | None:
        """
        Fetch latest candles and check if SL or TP1 was hit.
        Uses High/Low of candles (catches wicks, not just close).
        Returns: 'win', 'loss', or None (still open)
        """
        symbol  = trade["symbol"]
        raw_dir = trade["raw_dir"]
        sl      = trade["sl"]
        tp1     = trade["tp1"]

        try:
            if symbol == "BTCUSD":
                df = fetch_binance_candles("BTCUSDT", interval="15m", limit=10)
            else:
                td_symbol = PAIRS.get(symbol, symbol)
                df = fetch_candles(td_symbol, interval="15min", outputsize=10)

            if df is None or len(df) < 2:
                return None

            # Check last 3 candles High/Low for TP/SL hits
            recent = df.tail(3)
            highs  = recent["High"].values
            lows   = recent["Low"].values

            if raw_dir == "CALL":
                # Win: any candle high touched TP1
                if any(h >= tp1 for h in highs): return "win"
                # Loss: any candle low touched SL
                if any(l <= sl  for l in lows):  return "loss"
            else:  # PUT
                # Win: any candle low touched TP1
                if any(l <= tp1 for l in lows):  return "win"
                # Loss: any candle high touched SL
                if any(h >= sl  for h in highs): return "loss"

            return None  # Still open

        except Exception as e:
            log.warning(f"Outcome check error {symbol}: {e}")
            return None


# ══════════════════════════════════════════════════════════════
# SEED HISTORICAL DATA (runs once if no learning data exists)
# ══════════════════════════════════════════════════════════════
def seed_historical_data():
    """
    Pre-loads Emmanuel's 42 real trades into the learning engine.
    Only runs if learning.json doesn't exist yet.
    """
    if Path(LEARN_FILE).exists():
        return  # Already seeded

    log.info("🧠 Seeding learning engine with historical trade data...")
    data = {
        "pair_stats": {
            "XAUUSD": {"wins": 1,  "losses": 8},
            "EURUSD": {"wins": 7,  "losses": 1},
            "GBPUSD": {"wins": 6,  "losses": 1},
            "USDJPY": {"wins": 5,  "losses": 3},
            "BTCUSD": {"wins": 4,  "losses": 6},
        },
        "session_stats": {
            "london":  {"wins": 12, "losses": 10},
            "newyork": {"wins": 11, "losses": 9},
        },
        "pair_weights": {
            "XAUUSD": 0.5,
            "EURUSD": 1.3,
            "GBPUSD": 1.3,
            "USDJPY": 1.0,
            "BTCUSD": 1.0,
        },
        "pair_threshold": {
            "XAUUSD": 80,
            "EURUSD": 45,
            "GBPUSD": 45,
            "USDJPY": 55,
            "BTCUSD": 55,
        },
        "suppressed_pairs": ["XAUUSD"],
        "total_evaluated": 42,
        "last_evaluation": "2026-07-18T14:00:00",
    }
    with open(LEARN_FILE, "w") as f:
        json.dump(data, f, indent=2)
    log.info("✅ Historical data seeded: EURUSD/GBPUSD boosted, XAUUSD suppressed")


# ══════════════════════════════════════════════════════════════
# INIT GLOBAL OBJECTS
# ══════════════════════════════════════════════════════════════
seed_historical_data()
stats          = StatsTracker()
learning       = LearningEngine()
analyzer       = AdaptiveStrategySelector()
outcome_tracker = OutcomeTracker()


# ══════════════════════════════════════════════════════════════
# SCANNER
# ══════════════════════════════════════════════════════════════
async def scan_and_send(context=None):
    global presignal_sent
    if bot_paused or telegram_bot is None:
        return

    log.info(f"🔍 Scanning | {session_name()}")

    for symbol, td_symbol in PAIRS.items():
        await asyncio.sleep(15)
        sig, sig_type = analyzer.select(symbol, td_symbol, learning)

        if sig and sig_type == "signal":
            try:
                trade_id = stats.add_pending(sig)
                entry = sig["price"]
                sl, tp1, tp2, tp3 = calculate_sl_tp(entry, sig["raw_dir"], symbol)
                # Register with auto-outcome tracker
                outcome_tracker.add(
                    trade_id=trade_id, symbol=symbol,
                    raw_dir=sig["raw_dir"], entry=entry,
                    sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                    session=sig["session"]
                )
                await telegram_bot.send_message(
                    chat_id=CHAT_ID,
                    text=format_signal(sig, trade_id),
                    parse_mode="Markdown"
                )
                presignal_sent.pop(symbol, None)
                log.info(f"✅ Signal sent: {symbol} {sig['direction']} ({sig['confidence']}%) {trade_id}")
            except Exception as e:
                log.error(f"Send error: {e}")

        elif sig and sig_type == "presignal":
            last = presignal_sent.get(symbol)
            now  = datetime.utcnow()
            # Only send pre-signal once per 30 minutes per pair
            if not last or (now - last).seconds > 1800:
                try:
                    await telegram_bot.send_message(
                        chat_id=CHAT_ID,
                        text=format_presignal(sig),
                        parse_mode="Markdown"
                    )
                    presignal_sent[symbol] = now
                    log.info(f"👀 Pre-signal sent: {symbol} ({sig['confidence']}%)")
                except Exception as e:
                    log.error(f"Pre-signal send error: {e}")

    log.info("✅ Scan complete.")


# ══════════════════════════════════════════════════════════════
# AUTO OUTCOME CHECKER (runs every 5 mins via job queue)
# ══════════════════════════════════════════════════════════════
async def auto_check_outcomes(context=None):
    """
    Runs every 5 minutes. Checks pending trades for TP/SL hits.
    - If outcome detected: auto-records immediately + notifies user
    - User has 5 minutes to override by tapping WIN/LOSS buttons
    - If trade open 2+ hours with no detection: sends manual buttons
    """
    if telegram_bot is None: return
    due = outcome_tracker.get_due()
    if not due: return

    for trade in due:
        trade_id   = trade["trade_id"]
        symbol     = trade["symbol"]
        outcome    = outcome_tracker.check_outcome(trade)
        trade_time = datetime.fromisoformat(trade["time"])
        age_mins   = (datetime.utcnow() - trade_time).seconds // 60

        if outcome:
            # ── AUTO-RECORD immediately ──────────────────────────
            win = (outcome == "win")

            # Find and pop from stats pending
            pending = stats.data.get("pending", [])
            entry   = None
            for i, p in enumerate(pending):
                if p.get("trade_id") == trade_id:
                    entry = pending.pop(i)
                    break

            sess = entry.get("session","") if entry else ""
            stats.record_result(symbol, win=win, session=sess)
            learning.record(symbol, sess, win=win)
            stats._save()
            outcome_tracker.mark_confirmed(trade_id)

            today  = stats.get_today_stats()
            wr     = stats.get_win_rate()
            streak = stats.data.get("streak", 0)
            icon   = "🏆" if win else "💔"
            label  = "WIN" if win else "LOSS"

            # Send notification with override buttons (5 min window)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Override WIN",  callback_data=f"confirm_win_{trade_id}"),
                InlineKeyboardButton("❌ Override LOSS", callback_data=f"confirm_loss_{trade_id}"),
            ]])
            try:
                await telegram_bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        f"{icon} *AUTO-RECORDED: {label}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔖 Trade ID: `{trade_id}`\n"
                        f"💱 Pair: `{symbol}`\n"
                        f"🤖 Bot auto-recorded: *{'✅ WIN' if win else '❌ LOSS'}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 Win Rate: `{wr}%`\n"
                        f"📅 Today: ✅ {today['wins']}W ❌ {today['losses']}L\n"
                        f"🔥 Streak: `{streak}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"_Tap below within 5 mins to override if wrong_"
                    ),
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
                log.info(f"🤖 Auto-recorded {outcome} for {trade_id}")
            except Exception as e:
                log.error(f"Auto-outcome send error: {e}")

        elif age_mins >= 120:
            # Trade open 2+ hours — no detection, ask manually
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ WIN",  callback_data=f"confirm_win_{trade_id}"),
                InlineKeyboardButton("❌ LOSS", callback_data=f"confirm_loss_{trade_id}"),
                InlineKeyboardButton("⏳ Still Open", callback_data=f"still_open_{trade_id}"),
            ]])
            try:
                await telegram_bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        f"⏰ *TRADE UPDATE NEEDED*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔖 Trade ID: `{trade_id}`\n"
                        f"💱 Pair: `{symbol}`\n"
                        f"⏱ Open for `{age_mins} minutes`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"_What was the result?_"
                    ),
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
                outcome_tracker.mark_confirmed(trade_id)
                log.info(f"⏰ Manual outcome request sent for {trade_id}")
            except Exception as e:
                log.error(f"Manual outcome send error: {e}")


async def callback_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button taps from outcome confirmation messages."""
    query    = update.callback_query
    await query.answer()
    data     = query.data
    parts    = data.split("_")
    action   = parts[1]   # win / loss / open
    trade_id = "_".join(parts[2:])  # e.g. #TRD-043

    if action == "open":
        await query.edit_message_text(
            f"⏳ Got it — `{trade_id}` marked as still open.\n"
            f"I'll check again in 5 minutes.",
            parse_mode="Markdown"
        )
        # Re-schedule check in 30 mins
        for t in outcome_tracker.data["pending"]:
            if t["trade_id"] == trade_id:
                t["confirmed"] = False
                t["check_after"] = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
        outcome_tracker._save()
        return

    win = (action == "win")

    # Find matching pending signal in stats
    pending = stats.data.get("pending", [])
    entry   = None
    for i, p in enumerate(pending):
        if p.get("trade_id") == trade_id:
            entry = pending.pop(i)
            break

    symbol  = entry["symbol"]  if entry else trade_id
    session = entry.get("session","") if entry else ""

    stats.record_result(symbol, win=win, session=session)
    learning.record(symbol, session, win=win)
    stats._save()

    today  = stats.get_today_stats()
    wr     = stats.get_win_rate()
    streak = stats.data.get("streak", 0)

    if win:
        result_text = (
            f"✅ *WIN CONFIRMED!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔖 Trade ID: `{trade_id}`\n"
            f"💱 Pair: `{symbol}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Win Rate: `{wr}%`\n"
            f"📅 Today: ✅ {today['wins']}W ❌ {today['losses']}L\n"
            f"🔥 Streak: `{streak}`\n"
            f"🧠 _Learning engine updated_"
        )
    else:
        result_text = (
            f"❌ *LOSS CONFIRMED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔖 Trade ID: `{trade_id}`\n"
            f"💱 Pair: `{symbol}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Win Rate: `{wr}%`\n"
            f"📅 Today: ✅ {today['wins']}W ❌ {today['losses']}L\n"
            f"🧠 _Learning engine updated_"
        )

    await query.edit_message_text(result_text, parse_mode="Markdown")
    log.info(f"✅ Outcome confirmed: {trade_id} → {'WIN' if win else 'LOSS'}")


# ══════════════════════════════════════════════════════════════
# TELEGRAM COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    suppressed = learning.data.get("suppressed_pairs", [])
    supp_str   = f"🔴 Suppressed: {', '.join(suppressed)}" if suppressed else "✅ All pairs active"
    await update.message.reply_text(
        f"👋 *Pocket Option Signal Bot*\n"
        f"🤖 *Multi-Strategy | Self-Learning Edition*\n\n"
        f"⚙️ *Settings*\n"
        f"• Scan Every: `{SCAN_EVERY} minutes`\n"
        f"• Base Confidence: `{BASE_CONFIDENCE}%`\n"
        f"• Chart: `15-minute` | HTF: `1-hour`\n"
        f"• Sessions: 🇬🇧 London + 🇺🇸 New York\n"
        f"• Pairs: {', '.join(PAIRS.keys())}\n\n"
        f"🎯 *Active Strategies*\n"
        f"• 🧠 SMC PRO (FVG, OB, Liquidity, MSS)\n"
        f"• 📈 Trend Pullback (EMA20/50 zones)\n"
        f"• 🇬🇧 London Breakout (08:00-09:00 UTC)\n"
        f"• 📐 Price Action + Market Structure\n"
        f"• 🔭 Top Down Analysis (1H → 15M)\n\n"
        f"🧠 *Self-Learning Engine*\n"
        f"• Re-evaluates every 10 trades\n"
        f"• Auto-adjusts thresholds per pair\n"
        f"• Boosts winning pairs, suppresses losers\n"
        f"• {supp_str}\n\n"
        f"Use /help for all commands.",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Commands*\n\n"
        "/start — Bot info\n"
        "/stats — Performance report\n"
        "/learning — Self-learning status\n"
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
        parse_mode="Markdown"
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats.full_report(), parse_mode="Markdown")

async def cmd_learning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(learning.summary(), parse_mode="Markdown")

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    suppressed = learning.data.get("suppressed_pairs", [])
    lines = []
    for p in PAIRS:
        t = learning.get_threshold(p)
        w = learning.get_weight(p)
        ps = learning.data["pair_stats"].get(p, {"wins":0,"losses":0})
        total = ps["wins"] + ps["losses"]
        wr = round(ps["wins"]/total*100) if total > 0 else 0
        status = "🔴 SUPPRESSED" if p in suppressed else "🟢 ACTIVE"
        lines.append(f"{status} `{p}` | threshold: {t}% | weight: {w:.1f}x | WR: {wr}%")
    await update.message.reply_text(
        "💱 *Pair Status*\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now  = datetime.utcnow()
    hour = now.hour
    active = is_active_session()
    await update.message.reply_text(
        f"🕐 *Session Status*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Current: {session_name()}\n"
        f"Status: {'✅ ACTIVE — Signals ON' if active else '😴 INACTIVE — No signals'}\n"
        f"Time: `{now.strftime('%H:%M UTC')}`\n\n"
        f"📅 *Active Windows (UTC)*\n"
        f"🇬🇧 London: 08:00 – 12:00\n"
        f"🇺🇸 New York: 13:00 – 17:00",
        parse_mode="Markdown"
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning now...")
    await scan_and_send()
    await update.message.reply_text("✅ Scan complete.")

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = True
    await update.message.reply_text("⏸ Bot paused. Use /resume to restart.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = False
    await update.message.reply_text("▶️ Bot resumed!")

def _pop_pending(args: list) -> dict | None:
    pending = stats.data.get("pending", [])
    if not pending: return None
    if args:
        symbol = args[0].upper()
        for i in range(len(pending)-1, -1, -1):
            if pending[i]["symbol"] == symbol:
                return pending.pop(i)
        return None
    return pending.pop()

async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text("⚠️ No pending signals to record.")
        return
    entry = _pop_pending(context.args)
    if not entry:
        await update.message.reply_text(f"⚠️ No pending signal found for `{context.args[0].upper() if context.args else ''}`.", parse_mode="Markdown")
        return
    stats.record_result(entry["symbol"], win=True, session=entry.get("session",""))
    learning.record(entry["symbol"], entry.get("session",""), win=True)
    today  = stats.get_today_stats()
    wr     = stats.get_win_rate()
    streak = stats.data.get("streak", 0)
    tid = entry.get("trade_id", "")
    await update.message.reply_text(
        f"✅ *WIN RECORDED!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 Trade ID: `{tid}`\n"
        f"💱 Pair: `{entry['symbol']}` | {entry.get('direction','')}\n"
        f"📍 Session: {entry.get('session','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Win Rate: `{wr}%`\n"
        f"📅 Today: ✅ {today['wins']}W ❌ {today['losses']}L\n"
        f"🔥 Streak: `{streak}`\n"
        f"🧠 _Learning engine updated_",
        parse_mode="Markdown"
    )

async def cmd_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = stats.data.get("pending", [])
    if not pending:
        await update.message.reply_text("⚠️ No pending signals to record.")
        return
    entry = _pop_pending(context.args)
    if not entry:
        await update.message.reply_text(f"⚠️ No pending signal found for `{context.args[0].upper() if context.args else ''}`.", parse_mode="Markdown")
        return
    stats.record_result(entry["symbol"], win=False, session=entry.get("session",""))
    learning.record(entry["symbol"], entry.get("session",""), win=False)
    today = stats.get_today_stats()
    wr    = stats.get_win_rate()
    tid = entry.get("trade_id", "")
    await update.message.reply_text(
        f"❌ *LOSS RECORDED*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 Trade ID: `{tid}`\n"
        f"💱 Pair: `{entry['symbol']}` | {entry.get('direction','')}\n"
        f"📍 Session: {entry.get('session','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Win Rate: `{wr}%`\n"
        f"📅 Today: ✅ {today['wins']}W ❌ {today['losses']}L\n"
        f"🧠 _Learning engine updated_",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════
# ERROR HANDLER
# ══════════════════════════════════════════════════════════════
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import NetworkError, TimedOut
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning(f"Network error: {err}")
    else:
        log.error(f"Unhandled error: {err}", exc_info=context.error)


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
def main():
    global telegram_bot

    if not WEBHOOK_URL:
        log.critical(
            "WEBHOOK_URL not set!\n"
            "Go to Railway → Variables → add:\n"
            "WEBHOOK_URL = https://worker-production-a81a.up.railway.app"
        )
        raise SystemExit(1)

    async def post_init(application):
        global telegram_bot
        telegram_bot = application.bot
        if application.job_queue:
            application.job_queue.run_repeating(
                scan_and_send, interval=SCAN_EVERY * 60, first=30
            )
            application.job_queue.run_repeating(
                auto_check_outcomes, interval=5 * 60, first=60
            )
            log.info("Job queue scanner + auto-outcome checker started.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CallbackQueryHandler(callback_outcome))
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("learning", cmd_learning))
    app.add_handler(CommandHandler("pairs",    cmd_pairs))
    app.add_handler(CommandHandler("session",  cmd_session))
    app.add_handler(CommandHandler("scan",     cmd_scan))
    app.add_handler(CommandHandler("win",      cmd_win))
    app.add_handler(CommandHandler("loss",     cmd_loss))
    app.add_handler(CommandHandler("pause",    cmd_pause))
    app.add_handler(CommandHandler("resume",   cmd_resume))
    app.add_error_handler(error_handler)

    webhook_path    = f"/webhook/{BOT_TOKEN}"
    full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"

    log.info(f"🤖 SMC PRO Self-Learning Bot starting on port {WEBHOOK_PORT}")
    app.run_webhook(
        listen="0.0.0.0",
        port=WEBHOOK_PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
