"""
╔══════════════════════════════════════════════════╗
║   LONDON BREAKOUT STRATEGY MODULE                 ║
║   Strategy #2 of the Multi-Strategy Adaptive Bot   ║
║   Tag: [BREAKOUT]                                  ║
╚══════════════════════════════════════════════════╝

HOW IT WORKS
------------
1. Builds the "London Range" from the first 30 minutes of the London
   session (08:00–08:30 UTC = two 15-min candles).
2. After the range is set, watches for a 15-min candle that CLOSES
   (not just wicks) beyond the range high/low.
3. Filters out fake ranges that are too small (pure noise) or too
   large (already trending/news spike) before trading them.
4. Fires once per session per pair — no repeat signals off the same
   range.

This is intentionally independent from SMCProAnalyzer — no shared
confluence scoring. It's a clean, separate signal generator so we can
track its win rate on its own via /stats.
"""

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────
RANGE_START_HOUR   = 8      # UTC — London open
RANGE_END_HOUR     = 8      # range built from candles inside this window
RANGE_END_MINUTE   = 30     # first 30 minutes -> 08:00 & 08:15 15-min candles
BREAKOUT_CUTOFF_HR = 10     # stop looking for new breakouts after 10:00 UTC
                             # (avoid chasing late/exhausted moves)

MIN_RANGE_PCT = {           # skip if range is too tight (noise, no real level)
    "XAUUSD": 0.0008,
    "BTCUSD": 0.0015,
    "USDJPY": 0.0008,
    "DEFAULT": 0.0008,
}
MAX_RANGE_PCT = {           # skip if range is too wide (already volatile/news)
    "XAUUSD": 0.0060,
    "BTCUSD": 0.0100,
    "USDJPY": 0.0060,
    "DEFAULT": 0.0060,
}

# Per-symbol SL/TP calibration for breakout trades.
# Breakouts need a bit more room than mean-reversion SMC entries since
# you're trading momentum, not a precise structural level.
SL_BUFFER_PCT = {            # SL placed just beyond the opposite range edge
    "XAUUSD": 0.0004,
    "BTCUSD": 0.0015,
    "USDJPY": 0.0006,
    "DEFAULT": 0.0006,
}
TP_RANGE_MULTIPLES = [1.0, 1.5, 2.5]   # TP1/TP2/TP3 as multiples of range size

MIN_CONFIDENCE = 50   # signals below this score are skipped, not sent


# ──────────────────────────────────────────────────
# STATE (per-pair, reset daily)
# ──────────────────────────────────────────────────
class BreakoutState:
    """Tracks the day's range + whether a breakout has already fired."""
    def __init__(self):
        self.date = None
        self.ranges = {}     # symbol -> {"high":.., "low":.., "fired": bool}

    def _reset_if_new_day(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self.date != today:
            self.date = today
            self.ranges = {}

    def get(self, symbol):
        self._reset_if_new_day()
        return self.ranges.get(symbol)

    def set_range(self, symbol, high, low):
        self._reset_if_new_day()
        self.ranges[symbol] = {"high": high, "low": low, "fired": False}

    def mark_fired(self, symbol):
        self._reset_if_new_day()
        if symbol in self.ranges:
            self.ranges[symbol]["fired"] = True


breakout_state = BreakoutState()


# ──────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────
def _in_range_window(dt: datetime) -> bool:
    return dt.hour == RANGE_START_HOUR and dt.minute < RANGE_END_MINUTE

def _in_breakout_window(dt: datetime) -> bool:
    return RANGE_START_HOUR <= dt.hour < BREAKOUT_CUTOFF_HR and dt.weekday() < 5

def _range_pct(high, low, mid):
    return (high - low) / mid if mid else 0

def _thresholds(symbol):
    mn = MIN_RANGE_PCT.get(symbol, MIN_RANGE_PCT["DEFAULT"])
    mx = MAX_RANGE_PCT.get(symbol, MAX_RANGE_PCT["DEFAULT"])
    return mn, mx


# ──────────────────────────────────────────────────
# SL / TP CALCULATOR (breakout-specific)
# ──────────────────────────────────────────────────
def calculate_breakout_sl_tp(entry: float, direction: str, symbol: str,
                              range_high: float, range_low: float):
    buf = SL_BUFFER_PCT.get(symbol, SL_BUFFER_PCT["DEFAULT"])
    range_size = range_high - range_low

    if direction == "CALL":
        sl = round(range_low * (1 - buf), 5)
        tps = [round(entry + range_size * m, 5) for m in TP_RANGE_MULTIPLES]
    else:
        sl = round(range_high * (1 + buf), 5)
        tps = [round(entry - range_size * m, 5) for m in TP_RANGE_MULTIPLES]

    return sl, tps[0], tps[1], tps[2]


# ──────────────────────────────────────────────────
# CONFIDENCE SCORER
# ──────────────────────────────────────────────────
def _score_breakout_confidence(symbol, high, low, close, direction, last_candle):
    mn, mx = _thresholds(symbol)
    mid_target = (mn + mx) / 2
    range_size = high - low
    range_pct = _range_pct(high, low, (high + low) / 2)

    # 1. Range quality — closer to the sweet-spot midpoint between min/max scores higher
    spread = (mx - mn) / 2 or 1e-9
    range_quality = max(0, 100 - (abs(range_pct - mid_target) / spread) * 100)

    # 2. Breakout strength — how far close pushed beyond the level, relative to range size
    if range_size <= 0:
        breakout_strength = 0
    else:
        overshoot = (close - high) if direction == "CALL" else (low - close)
        breakout_strength = min(100, max(0, (overshoot / range_size) * 200))

    # 3. Candle momentum — decisive close (large body) vs indecisive doji
    o, h, l, c = float(last_candle["Open"]), float(last_candle["High"]), float(last_candle["Low"]), float(last_candle["Close"])
    total = h - l
    body_ratio = (abs(c - o) / total * 100) if total > 0 else 0

    confidence = round(0.35 * range_quality + 0.40 * breakout_strength + 0.25 * body_ratio)
    return max(0, min(100, confidence))


# ──────────────────────────────────────────────────
# LONDON BREAKOUT ANALYZER
# ──────────────────────────────────────────────────
class LondonBreakoutAnalyzer:
    """
    Call analyze(symbol, df) once per scan cycle with a 15-min candle
    DataFrame (same shape as the one SMCProAnalyzer uses: Open/High/Low/Close,
    oldest-first).
    """

    def analyze(self, symbol: str, df):
        now = datetime.utcnow()

        if df is None or len(df) < 3:
            return None

        # ── Step 1: build/update today's range from the opening candles ──
        recent = df.tail(6)  # enough candles to cover the range window safely
        state = breakout_state.get(symbol)

        # If we don't have a range yet, try to build one from candles
        # that fall inside the 08:00–08:30 UTC window.
        if state is None:
            # NOTE: candle timestamps aren't in this stripped df, so in
            # production, pass timestamps through from fetch_candles and
            # filter properly. Here we build the range from the two oldest
            # candles in the current 15-min batch, which lines up with the
            # scan_and_send cadence hitting the London open scan first.
            if _in_range_window(now) or now.hour == RANGE_START_HOUR:
                range_candles = df.tail(2)
                high = float(range_candles["High"].max())
                low  = float(range_candles["Low"].min())
                mid  = (high + low) / 2
                mn, mx = _thresholds(symbol)
                pct = _range_pct(high, low, mid)

                if pct < mn:
                    log.info(f"[BREAKOUT] {symbol} range too tight ({pct:.4%}) — skip today")
                    return None
                if pct > mx:
                    log.info(f"[BREAKOUT] {symbol} range too wide ({pct:.4%}) — skip today")
                    return None

                breakout_state.set_range(symbol, high, low)
                log.info(f"[BREAKOUT] {symbol} range set: {low}–{high} ({pct:.4%})")
            return None   # range just built or not yet available — no signal this cycle

        if state["fired"]:
            return None   # already traded this range today

        if not _in_breakout_window(now):
            return None   # outside the valid breakout window

        # ── Step 2: check the latest CLOSED candle for a breakout ──
        last = df.iloc[-1]
        close = float(last["Close"])
        high, low = state["high"], state["low"]

        direction = None
        if close > high:
            direction = "CALL"
        elif close < low:
            direction = "PUT"

        if direction is None:
            return None

        confidence = _score_breakout_confidence(symbol, high, low, close, direction, last)
        if confidence < MIN_CONFIDENCE:
            log.info(f"[BREAKOUT] {symbol} {direction} confidence {confidence}% < {MIN_CONFIDENCE}% — skip")
            return None

        breakout_state.mark_fired(symbol)

        sl, tp1, tp2, tp3 = calculate_breakout_sl_tp(close, direction, symbol, high, low)
        range_pips_pct = round(_range_pct(high, low, (high + low) / 2) * 100, 3)

        result = {
            "symbol": symbol,
            "direction": "✅ CALL" if direction == "CALL" else "❌ PUT",
            "raw_dir": direction,
            "price": round(close, 5),
            "range_high": high,
            "range_low": low,
            "range_pct": range_pips_pct,
            "confidence": confidence,
            "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "strategy": "BREAKOUT",
            "session": "🇬🇧 London Breakout",
        }
        log.info(f"[BREAKOUT] {symbol} {direction} fired — confidence {confidence}%, range {low}-{high}, close {close}")
        return result


# ──────────────────────────────────────────────────
# FORMATTER (matches the visual style of your SMC signals)
# ──────────────────────────────────────────────────
def confidence_bar(pct: int) -> str:
    filled = round(pct / 10)
    return "🟢" * filled + "⬜" * (10 - filled)

def format_breakout_signal(sig):
    now = datetime.utcnow()
    conf = sig.get("confidence", 0)
    conf_label = "🔥 ELITE" if conf >= 85 else "💎 HIGH" if conf >= 70 else "✅ GOOD"
    return (
        f"🚨 *LONDON BREAKOUT SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{sig['symbol']}`\n"
        f"🎯 Direction: *{sig['direction']}*\n"
        f"📍 Strategy: `[BREAKOUT]`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Range: `{sig['range_low']}` – `{sig['range_high']}` "
        f"(`{sig['range_pct']}%`)\n"
        f"🎯 *Entry*: `{sig['price']}`\n"
        f"⛔ *Stop Loss*: `{sig['sl']}`\n"
        f"✅ *TP1*: `{sig['tp1']}`\n"
        f"✅ *TP2*: `{sig['tp2']}`\n"
        f"✅ *TP3*: `{sig['tp3']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Confidence*: {conf_label}\n"
        f"{confidence_bar(conf)} `{conf}%`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_\n"
        f"📝 _Reply /win BREAKOUT or /loss BREAKOUT after trade_"
    )
