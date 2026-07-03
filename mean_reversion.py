"""
╔══════════════════════════════════════════════════╗
║   MEAN REVERSION STRATEGY MODULE                   ║
║   Strategy #5 of the Multi-Strategy Adaptive Bot   ║
║   Tag: [MEANREV]                                   ║
╚══════════════════════════════════════════════════╝

HOW IT WORKS
------------
This strategy is deliberately the OPPOSITE condition of Trend Pullback:
it only activates when the 1H HTF bias is NEUTRAL (EMA50 ≈ EMA200 —
no clear trend). SMC and Pullback both require a directional bias to
even evaluate, so on genuinely ranging days they go quiet. Mean
Reversion fills that gap instead of leaving it untraded.

1. Confirms the market is ranging (HTF bias == None from the same
   get_htf_bias() logic SMC/Pullback already use).
2. On the 15-min chart, watches for price to tag or exceed a
   Bollinger Band extreme (20, 2) while RSI(14) confirms exhaustion
   (RSI < 30 near the lower band, RSI > 70 near the upper band).
3. Only fires on a rejection candle at the extreme (pin bar or
   engulfing back toward the mean) — a touch alone isn't enough.
4. TP1 targets the middle band (the mean) — the core thesis of mean
   reversion. TP2/TP3 extend slightly beyond for trades that run
   further. SL sits just beyond the band extreme.
5. Cooldown per pair so it doesn't re-fire on the same band touch
   repeatedly.

Independent from the other four strategies. Tracked separately in
/stats via the [MEANREV] tag.
"""

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────
BB_WINDOW    = 20
BB_STD       = 2
RSI_WINDOW   = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70
COOLDOWN_MIN = 60   # per-pair cooldown between MEANREV signals

SL_BUFFER_PCT = {
    "XAUUSD": 0.0006,
    "BTCUSD": 0.0020,
    "USDJPY": 0.0008,
    "DEFAULT": 0.0008,
}
# TP as fraction of the distance from entry back to the mean (mid band)
TP_MEAN_FRACTIONS = [0.6, 1.0, 1.4]   # TP1 = 60% of the way to the mean, TP3 overshoots slightly

MIN_CONFIDENCE = 50   # signals below this score are skipped, not sent


# ──────────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────────
class MeanRevState:
    def __init__(self):
        self.last_signal_time = {}

    def can_fire(self, symbol):
        last = self.last_signal_time.get(symbol)
        if last is None:
            return True
        return (datetime.utcnow() - last) >= timedelta(minutes=COOLDOWN_MIN)

    def mark_fired(self, symbol):
        self.last_signal_time[symbol] = datetime.utcnow()


meanrev_state = MeanRevState()


# ──────────────────────────────────────────────────
# CANDLE PATTERN HELPERS (same logic used across the other modules)
# ──────────────────────────────────────────────────
def _is_bullish_pin_bar(o, h, l, c):
    body = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    if h - l == 0:
        return False
    return lower_wick > body * 1.5 and lower_wick > upper_wick * 2

def _is_bearish_pin_bar(o, h, l, c):
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if h - l == 0:
        return False
    return upper_wick > body * 1.5 and upper_wick > lower_wick * 2

def _is_bullish_engulfing(prev, last):
    return (prev["Close"] < prev["Open"] and last["Close"] > last["Open"] and
            last["Close"] > prev["Open"] and last["Open"] < prev["Close"])

def _is_bearish_engulfing(prev, last):
    return (prev["Close"] > prev["Open"] and last["Close"] < last["Open"] and
            last["Close"] < prev["Open"] and last["Open"] > prev["Close"])


# ──────────────────────────────────────────────────
# INDICATOR HELPERS (kept local so this module has zero dependency
# on bot.py internals beyond the raw candle DataFrame)
# ──────────────────────────────────────────────────
def _bollinger_bands(close, window=BB_WINDOW, num_std=BB_STD):
    sma = close.rolling(window=window).mean()
    std = close.rolling(window=window).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    return upper, sma, lower

def _rsi(close, window=RSI_WINDOW):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=window).mean()
    avg_loss = loss.rolling(window=window).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


# ──────────────────────────────────────────────────
# CONFIDENCE SCORER
# ──────────────────────────────────────────────────
def _score_meanrev_confidence(direction, r, o, h, l, c, band_extreme, band_penetration_ref):
    # 1. RSI extremity — how far past the 30/70 threshold (more extreme = higher confidence)
    if direction == "CALL":
        rsi_extremity = max(0, min(100, ((RSI_OVERSOLD - r) / RSI_OVERSOLD) * 200))
    else:
        rsi_extremity = max(0, min(100, ((r - RSI_OVERBOUGHT) / (100 - RSI_OVERBOUGHT)) * 200))

    # 2. Band penetration depth — how far price pushed beyond the band,
    #    relative to the band's own reference distance (deeper = higher)
    penetration = abs((l if direction == "CALL" else h) - band_extreme)
    band_depth = min(100, (penetration / band_penetration_ref) * 300) if band_penetration_ref else 0

    # 3. Rejection candle quality — wick-to-range ratio (same style as other modules)
    total = h - l
    if direction == "CALL":
        wick = min(o, c) - l
    else:
        wick = h - max(o, c)
    candle_quality = min(100, (wick / total) * 150) if total > 0 else 0

    confidence = round(0.35 * rsi_extremity + 0.30 * band_depth + 0.35 * candle_quality)
    return max(0, min(100, confidence))


# ──────────────────────────────────────────────────
# MEAN REVERSION ANALYZER
# ──────────────────────────────────────────────────
class MeanReversionAnalyzer:
    """
    Call analyze(symbol, df, htf_bias) once per scan cycle.
    df: 15-min candle DataFrame (Open/High/Low/Close, oldest-first)
    htf_bias: pass in the SAME value SMC's get_htf_bias() already
              computed this cycle — None means ranging, which is the
              only condition this strategy trades.
    """

    def analyze(self, symbol: str, df, htf_bias):
        if df is None or len(df) < BB_WINDOW + 5:
            return None

        if htf_bias is not None:
            return None   # market has a clear trend — leave this to Pullback/SMC

        if not meanrev_state.can_fire(symbol):
            return None

        close = df["Close"]
        upper, mid, lower = _bollinger_bands(close)
        rsi = _rsi(close)

        u  = float(upper.iloc[-1])
        m  = float(mid.iloc[-1])
        lo = float(lower.iloc[-1])
        r  = float(rsi.iloc[-1])

        if any(pd_isnan(x) for x in [u, m, lo, r]):
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        o, h, l, c = float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"])

        direction = None
        if l <= lo and r < RSI_OVERSOLD:
            if _is_bullish_pin_bar(o, h, l, c) or _is_bullish_engulfing(prev, last):
                direction = "CALL"
        elif h >= u and r > RSI_OVERBOUGHT:
            if _is_bearish_pin_bar(o, h, l, c) or _is_bearish_engulfing(prev, last):
                direction = "PUT"

        if direction is None:
            return None

        band_penetration_ref = abs(u - m)   # half the band width, used as a depth reference
        band_extreme_val = lo if direction == "CALL" else u
        confidence = _score_meanrev_confidence(direction, r, o, h, l, c, band_extreme_val, band_penetration_ref)
        if confidence < MIN_CONFIDENCE:
            log.info(f"[MEANREV] {symbol} {direction} confidence {confidence}% < {MIN_CONFIDENCE}% — skip")
            return None

        buf = SL_BUFFER_PCT.get(symbol, SL_BUFFER_PCT["DEFAULT"])
        dist_to_mean = abs(m - c)

        if direction == "CALL":
            sl  = round(lo * (1 - buf), 5)
            tps = [round(c + dist_to_mean * f, 5) for f in TP_MEAN_FRACTIONS]
        else:
            sl  = round(u * (1 + buf), 5)
            tps = [round(c - dist_to_mean * f, 5) for f in TP_MEAN_FRACTIONS]

        meanrev_state.mark_fired(symbol)

        result = {
            "symbol": symbol,
            "direction": "✅ CALL" if direction == "CALL" else "❌ PUT",
            "raw_dir": direction,
            "price": round(c, 5),
            "rsi": round(r, 1),
            "band_mean": round(m, 5),
            "band_extreme": round(band_extreme_val, 5),
            "confidence": confidence,
            "sl": sl, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2],
            "pattern": "Pin Bar" if (_is_bullish_pin_bar(o,h,l,c) or _is_bearish_pin_bar(o,h,l,c)) else "Engulfing",
            "strategy": "MEANREV",
            "session": "🔁 Mean Reversion (Ranging)",
        }
        log.info(f"[MEANREV] {symbol} {direction} fired — confidence {confidence}%, RSI {r:.1f}, band touch at {result['band_extreme']}")
        return result


def pd_isnan(x):
    import math
    try:
        return math.isnan(x)
    except (TypeError, ValueError):
        return False


# ──────────────────────────────────────────────────
# FORMATTER
# ──────────────────────────────────────────────────
def format_meanrev_signal(sig):
    now = datetime.utcnow()
    conf = sig.get("confidence", 0)
    conf_label = "🔥 ELITE" if conf >= 85 else "💎 HIGH" if conf >= 70 else "✅ GOOD"
    filled = round(conf / 10)
    bar = "🟢" * filled + "⬜" * (10 - filled)
    return (
        f"🚨 *MEAN REVERSION SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{sig['symbol']}`\n"
        f"🎯 Direction: *{sig['direction']}*\n"
        f"📍 Strategy: `[MEANREV]`\n"
        f"🕯 Pattern: `{sig['pattern']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 RSI: `{sig['rsi']}`\n"
        f"📦 Band Extreme: `{sig['band_extreme']}`  |  Mean: `{sig['band_mean']}`\n"
        f"🎯 *Entry*: `{sig['price']}`\n"
        f"⛔ *Stop Loss*: `{sig['sl']}`\n"
        f"✅ *TP1 (mean)*: `{sig['tp1']}`\n"
        f"✅ *TP2*: `{sig['tp2']}`\n"
        f"✅ *TP3*: `{sig['tp3']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Confidence*: {conf_label}\n"
        f"{bar} `{conf}%`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_\n"
        f"📝 _Reply /win MEANREV or /loss MEANREV after trade_"
    )
