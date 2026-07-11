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
TP_LEG_MULTIPLES = [1.0, 1.5, 2.5]   # TP1/TP2/TP3 — same ratio pattern as Breakout/SMC

# Caps how far SL can sit from ENTRY, in multiples of leg_size. Without
# this, SL is anchored to the raw swing low/high of the last 6 candles —
# a value with no relationship to leg_size (which drives TP). If a wick
# a few candles back sits far from the current entry, SL balloons while
# TP stays fixed, producing the same broken risk profile Breakout had.
#
# NOTE: because the actual SL always picks whichever of (natural swing
# stop, this cap) is TIGHTER, this cap can only ever make SL equal to or
# smaller than before — never wider. An earlier version set this to 0.5,
# matching TP1's old multiplier exactly — which meant SL had almost no
# room to absorb normal pullback noise before either hitting the stop or
# TP1, causing frequent premature stop-outs even on directionally correct
# calls. Widened to 1.0 to give real breathing room, with TP1 raised to
# match — same 1:1 minimum R:R, just sized less like a hair-trigger.
MAX_SL_LEG_MULTIPLE = 1.0

MIN_CONFIDENCE = 50   # signals below this score are skipped, not sent


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
    max_sl_distance = leg_size * MAX_SL_LEG_MULTIPLE

    if direction == "CALL":
        natural_sl = swing_low * (1 - buf)      # raw swing extreme
        capped_sl  = entry - max_sl_distance      # bounded risk from entry
        # Use whichever is CLOSER to entry — the raw swing low if it's
        # already tight, or the cap if the swing low is too far away.
        sl = round(max(natural_sl, capped_sl), 5)
        tps = [round(entry + leg_size * m, 5) for m in TP_LEG_MULTIPLES]
    else:
        natural_sl = swing_high * (1 + buf)
        capped_sl  = entry + max_sl_distance
        sl = round(min(natural_sl, capped_sl), 5)
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
# CONFIDENCE SCORER
# ──────────────────────────────────────────────────
def _score_pullback_confidence(ef, et, direction, o, h, l, c, touch_dist_pct):
    # 1. Trend strength — how far EMA21 has separated from EMA50 (stronger trend = higher)
    trend_sep_pct = abs(ef - et) / et if et else 0
    trend_strength = min(100, (trend_sep_pct / 0.01) * 100)   # 1% separation ≈ full score

    # 2. Rejection candle quality — wick-to-body ratio
    body = abs(c - o)
    total = h - l
    if direction == "CALL":
        wick = min(o, c) - l
    else:
        wick = h - max(o, c)
    candle_quality = min(100, (wick / total) * 150) if total > 0 else 0

    # 3. Touch precision — closer to the EMA (smaller touch_dist_pct) scores higher
    touch_precision = max(0, 100 - (touch_dist_pct / TOUCH_TOLERANCE_PCT) * 100)

    confidence = round(0.35 * trend_strength + 0.40 * candle_quality + 0.25 * touch_precision)
    return max(0, min(100, confidence))


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
        best_touch_dist_pct = None
        for i in range(len(recent)):
            row = recent.iloc[i]
            ema_val = float(ema_fast.iloc[-(len(recent) - i)])
            dist_pct = abs(row["Low"] - ema_val) / ema_val if htf_bias == "CALL" \
                       else abs(row["High"] - ema_val) / ema_val
            if htf_bias == "CALL" and row["Low"] <= ema_val * (1 + TOUCH_TOLERANCE_PCT):
                touched = True
                best_touch_dist_pct = dist_pct if best_touch_dist_pct is None else min(best_touch_dist_pct, dist_pct)
            if htf_bias == "PUT" and row["High"] >= ema_val * (1 - TOUCH_TOLERANCE_PCT):
                touched = True
                best_touch_dist_pct = dist_pct if best_touch_dist_pct is None else min(best_touch_dist_pct, dist_pct)

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

        confidence = _score_pullback_confidence(ef, et, direction, o, h, l, c, best_touch_dist_pct or 0)
        if confidence < MIN_CONFIDENCE:
            log.info(f"[PULLBACK] {symbol} {direction} confidence {confidence}% < {MIN_CONFIDENCE}% — skip")
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
            "confidence": confidence,
            "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "pattern": "Pin Bar" if (_is_bullish_pin_bar(o,h,l,c) or _is_bearish_pin_bar(o,h,l,c)) else "Engulfing",
            "strategy": "PULLBACK",
            "session": "📈 Trend Pullback",
        }
        log.info(f"[PULLBACK] {symbol} {direction} fired — confidence {confidence}%, EMA21 pullback, {result['pattern']}")
        return result


# ──────────────────────────────────────────────────
# FORMATTER
# ──────────────────────────────────────────────────
def format_pullback_signal(sig):
    now = datetime.utcnow()
    conf = sig.get("confidence", 0)
    conf_label = "🔥 ELITE" if conf >= 85 else "💎 HIGH" if conf >= 70 else "✅ GOOD"
    filled = round(conf / 10)
    bar = "🟢" * filled + "⬜" * (10 - filled)
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
        f"📊 *Confidence*: {conf_label}\n"
        f"{bar} `{conf}%`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_\n"
        f"📝 _Reply /win PULLBACK or /loss PULLBACK after trade_"
    )
