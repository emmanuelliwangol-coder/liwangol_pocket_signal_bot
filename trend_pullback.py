"""
╔══════════════════════════════════════════════════╗
║   TREND PULLBACK STRATEGY MODULE                  ║
║   Strategy #3 of the Multi-Strategy Adaptive Bot   ║
║   Tag: [PULLBACK]                                  ║
╚══════════════════════════════════════════════════╝

HOW IT WORKS
------------
1. Confirms an established trend using 1H EMA50 vs EMA200 (same HTF
   bias logic SMC already uses — bullish if EMA50>EMA200, bearish if
   EMA50<EMA200).
2. On the 15-min chart, waits for price to pull back and touch/cross
   the EMA21 (a dynamic support/resistance level inside the trend).
3. Only fires on a REJECTION candle at that level — a pin bar or
   engulfing pattern in the direction of the trend. A pullback alone
   isn't enough; price has to show it's actually turning back in the
   trend direction.
4. SL goes just beyond the pullback's swing low/high. TP is based on
   the measured distance of the prior trend leg (not a fixed % like
   SMC/Breakout), so targets scale with how far price has already run.
5. Cooldown per pair — won't re-fire on the same trend leg repeatedly.

Independent from SMCProAnalyzer and LondonBreakoutAnalyzer — no shared
scoring. Tracked separately in /stats via the [PULLBACK] tag.
"""

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────
EMA_FAST   = 21     # pullback level on the 15M chart
EMA_TREND  = 50     # 15M trend filter
COOLDOWN_MIN = 90   # minimum minutes between signals on the same pair

TOUCH_TOLERANCE_PCT = 0.0015   # how close price must get to EMA21 to count as a "touch"

# SL buffer beyond the pullback swing
SL_BUFFER_PCT = {
    "XAUUSD": 0.0006,
    "BTCUSD": 0.0020,
    "USDJPY": 0.0008,
    "DEFAULT": 0.0008,
}

# TP measured-move multiples of the prior trend leg's size
TP_LEG_MULTIPLES = [0.5, 1.0, 1.618]   # TP1/TP2/TP3 — 1.618 = fib extension


# ──────────────────────────────────────────────────
# STATE (per-pair cooldown tracking)
# ──────────────────────────────────────────────────
class PullbackState:
    def __init__(self):
        self.last_signal_time = {}   # symbol -> datetime

    def can_fire(self, symbol):
        last = self.last_signal_time.get(symbol)
        if last is None:
            return True
        return (datetime.utcnow() - last) >= timedelta(minutes=COOLDOWN_MIN)

    def mark_fired(self, symbol):
        self.last_signal_time[symbol] = datetime.utcnow()


pullback_state = PullbackState()


# ──────────────────────────────────────────────────
# SL / TP CALCULATOR
# ──────────────────────────────────────────────────
def calculate_pullback_sl_tp(entry: float, direction: str, symbol: str,
                              swing_low: float, swing_high: float,
                              leg_size: float):
    buf = SL_BUFFER_PCT.get(symbol, SL_BUFFER_PCT["DEFAULT"])

    if direction == "CALL":
        sl = round(swing_low * (1 - buf), 5)
        tps = [round(entry + leg_size * m, 5) for m in TP_LEG_MULTIPLES]
    else:
        sl = round(swing_high * (1 + buf), 5)
        tps = [round(entry - leg_size * m, 5) for m in TP_LEG_MULTIPLES]

    return sl, tps[0], tps[1], tps[2]


# ──────────────────────────────────────────────────
# CANDLE PATTERN HELPERS
# ──────────────────────────────────────────────────
def _is_bullish_pin_bar(o, h, l, c):
    body = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    total = h - l
    if total == 0:
        return False
    return lower_wick > body * 1.5 and lower_wick > upper_wick * 2

def _is_bearish_pin_bar(o, h, l, c):
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    total = h - l
    if total == 0:
        return False
    return upper_wick > body * 1.5 and upper_wick > lower_wick * 2

def _is_bullish_engulfing(prev, last):
    return (prev["Close"] < prev["Open"] and last["Close"] > last["Open"] and
            last["Close"] > prev["Open"] and last["Open"] < prev["Close"])

def _is_bearish_engulfing(prev, last):
    return (prev["Close"] > prev["Open"] and last["Close"] < last["Open"] and
            last["Close"] < prev["Open"] and last["Open"] > prev["Close"])


# ──────────────────────────────────────────────────
# TREND PULLBACK ANALYZER
# ──────────────────────────────────────────────────
class TrendPullbackAnalyzer:
    """
    Call analyze(symbol, df, htf_bias) once per scan cycle.
    df: 15-min candle DataFrame (Open/High/Low/Close, oldest-first)
    htf_bias: "CALL", "PUT", or None — pass in the same HTF bias value
              SMC's get_htf_bias() already computes, so we don't
              duplicate that API call.
    """

    def _ema(self, series, window):
        return series.ewm(span=window, adjust=False).mean()

    def analyze(self, symbol: str, df, htf_bias):
        if df is None or len(df) < max(EMA_TREND, 30) + 5:
            return None

        if htf_bias not in ("CALL", "PUT"):
            return None   # no clear HTF trend — pullback strategy needs one

        if not pullback_state.can_fire(symbol):
            return None

        close = df["Close"]
        ema_fast  = self._ema(close, EMA_FAST)
        ema_trend = self._ema(close, EMA_TREND)

        lc  = float(close.iloc[-1])
        ef  = float(ema_fast.iloc[-1])
        et  = float(ema_trend.iloc[-1])

        # 15M trend must agree with HTF bias
        if htf_bias == "CALL" and not (lc > et):
            return None
        if htf_bias == "PUT" and not (lc < et):
            return None

        # ── Check for a pullback touch of EMA21 within the last few candles ──
        recent = df.tail(4)
        touched = False
        for i in range(len(recent)):
            row = recent.iloc[i]
            ema_val = float(ema_fast.iloc[-(len(recent) - i)])
            dist_pct = abs(row["Low"] - ema_val) / ema_val if htf_bias == "CALL" \
                       else abs(row["High"] - ema_val) / ema_val
            if htf_bias == "CALL" and row["Low"] <= ema_val * (1 + TOUCH_TOLERANCE_PCT):
                touched = True
            if htf_bias == "PUT" and row["High"] >= ema_val * (1 - TOUCH_TOLERANCE_PCT):
                touched = True

        if not touched:
            return None

        # ── Confirm rejection candle on the latest closed candle ──
        last = df.iloc[-1]
        prev = df.iloc[-2]
        o, h, l, c = float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"])

        if htf_bias == "CALL":
            rejection = _is_bullish_pin_bar(o, h, l, c) or _is_bullish_engulfing(prev, last)
            direction = "CALL"
        else:
            rejection = _is_bearish_pin_bar(o, h, l, c) or _is_bearish_engulfing(prev, last)
            direction = "PUT"

        if not rejection:
            return None

        # ── Measure the prior trend leg for TP sizing ──
        lookback = df.tail(20)
        if direction == "CALL":
            leg_start = float(lookback["Low"].min())
            leg_end   = float(lookback["High"].max())
        else:
            leg_start = float(lookback["High"].max())
            leg_end   = float(lookback["Low"].min())
        leg_size = abs(leg_end - leg_start)
        if leg_size == 0:
            return None

        swing_low  = float(df.tail(6)["Low"].min())
        swing_high = float(df.tail(6)["High"].max())

        sl, tp1, tp2, tp3 = calculate_pullback_sl_tp(
            c, direction, symbol, swing_low, swing_high, leg_size
        )

        pullback_state.mark_fired(symbol)

        result = {
            "symbol": symbol,
            "direction": "✅ CALL" if direction == "CALL" else "❌ PUT",
            "raw_dir": direction,
            "price": round(c, 5),
            "ema_fast": round(ef, 5),
            "ema_trend": round(et, 5),
            "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "pattern": "Pin Bar" if (_is_bullish_pin_bar(o,h,l,c) or _is_bearish_pin_bar(o,h,l,c)) else "Engulfing",
            "strategy": "PULLBACK",
            "session": "📈 Trend Pullback",
        }
        log.info(f"[PULLBACK] {symbol} {direction} fired — EMA21 pullback, {result['pattern']}")
        return result


# ──────────────────────────────────────────────────
# FORMATTER
# ──────────────────────────────────────────────────
def format_pullback_signal(sig):
    now = datetime.utcnow()
    return (
        f"🚨 *TREND PULLBACK SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{sig['symbol']}`\n"
        f"🎯 Direction: *{sig['direction']}*\n"
        f"📍 Strategy: `[PULLBACK]`\n"
        f"🕯 Pattern: `{sig['pattern']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 EMA21: `{sig['ema_fast']}`  |  EMA50: `{sig['ema_trend']}`\n"
        f"🎯 *Entry*: `{sig['price']}`\n"
        f"⛔ *Stop Loss*: `{sig['sl']}`\n"
        f"✅ *TP1*: `{sig['tp1']}`\n"
        f"✅ *TP2*: `{sig['tp2']}`\n"
        f"✅ *TP3*: `{sig['tp3']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_\n"
        f"📝 _Reply /win PULLBACK or /loss PULLBACK after trade_"
    )
