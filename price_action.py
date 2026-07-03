"""
╔══════════════════════════════════════════════════╗
║   PRICE ACTION + STRUCTURE STRATEGY MODULE         ║
║   Strategy #4 of the Multi-Strategy Adaptive Bot   ║
║   Tag: [STRUCTURE]                                 ║
╚══════════════════════════════════════════════════╝

HOW IT WORKS
------------
1. Detects swing highs/lows on the 15-min chart using a simple
   fractal method (a candle is a confirmed swing point once 2 candles
   have closed on each side of it).
2. Tracks these as "structure levels" — old support/resistance.
3. When price CLOSES beyond one of these levels, that's a structure
   break — old resistance becomes potential new support (or vice
   versa) and gets flagged as "awaiting retest".
4. Only fires a signal when price RETESTS that broken level and shows
   a rejection candle (pin bar or engulfing) confirming the level is
   holding in the new direction. A break alone is not a signal — the
   retest + rejection is the actual entry trigger.
5. Levels expire after a set number of candles if never retested, so
   stale levels don't linger and re-trigger old news.

Independent from the other three strategies — no shared scoring.
Tracked separately in /stats via the [STRUCTURE] tag.
"""

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────
FRACTAL_WING       = 2      # candles on each side to confirm a swing point
MAX_TRACKED_LEVELS = 6       # keep only the most recent N levels per pair
LEVEL_EXPIRY_BARS  = 40      # ~10 hours on 15M — drop levels older than this
RETEST_TOLERANCE_PCT = 0.0012
COOLDOWN_MIN       = 60      # per-pair cooldown between STRUCTURE signals

SL_BUFFER_PCT = {
    "XAUUSD": 0.0006,
    "BTCUSD": 0.0020,
    "USDJPY": 0.0008,
    "DEFAULT": 0.0008,
}
TP_MOVE_MULTIPLES = [1.0, 1.5, 2.5]   # multiples of the original break-leg size


# ──────────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────────
class StructureState:
    """
    Per-symbol tracking of:
      - swing_levels: recent confirmed swing highs/lows [(price, bar_index, type)]
      - broken_levels: levels that have been broken and are awaiting retest
      - last_signal_time: for cooldown
      - bar_count: running candle counter (for level expiry)
    """
    def __init__(self):
        self.swing_levels  = {}   # symbol -> list of dicts
        self.broken_levels = {}   # symbol -> list of dicts
        self.last_signal_time = {}
        self.bar_count = {}       # symbol -> int

    def can_fire(self, symbol):
        last = self.last_signal_time.get(symbol)
        if last is None:
            return True
        return (datetime.utcnow() - last) >= timedelta(minutes=COOLDOWN_MIN)

    def mark_fired(self, symbol):
        self.last_signal_time[symbol] = datetime.utcnow()

    def get_bar_count(self, symbol):
        return self.bar_count.get(symbol, 0)

    def incr_bar_count(self, symbol):
        self.bar_count[symbol] = self.get_bar_count(symbol) + 1


structure_state = StructureState()


# ──────────────────────────────────────────────────
# CANDLE PATTERN HELPERS (same logic as trend_pullback.py)
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
# SWING / FRACTAL DETECTION
# ──────────────────────────────────────────────────
def _find_new_swing_points(df, wing=FRACTAL_WING):
    """
    Returns any newly-confirmed swing high/low from the candle that is
    `wing` positions back from the latest close (so it has `wing`
    candles confirmed on both sides).
    """
    idx = len(df) - 1 - wing
    if idx - wing < 0:
        return []

    window = df.iloc[idx - wing: idx + wing + 1]
    center = df.iloc[idx]

    points = []
    if center["High"] == window["High"].max():
        points.append((float(center["High"]), "high"))
    if center["Low"] == window["Low"].min():
        points.append((float(center["Low"]), "low"))
    return points


# ──────────────────────────────────────────────────
# PRICE ACTION + STRUCTURE ANALYZER
# ──────────────────────────────────────────────────
class StructureAnalyzer:
    """
    Call analyze(symbol, df) once per scan cycle with a 15-min candle
    DataFrame (Open/High/Low/Close, oldest-first).
    """

    def analyze(self, symbol: str, df):
        if df is None or len(df) < 15:
            return None

        structure_state.incr_bar_count(symbol)
        bar_now = structure_state.get_bar_count(symbol)

        levels  = structure_state.swing_levels.setdefault(symbol, [])
        broken  = structure_state.broken_levels.setdefault(symbol, [])

        # ── Step 1: register any newly confirmed swing points ──
        new_points = _find_new_swing_points(df)
        for price, kind in new_points:
            levels.append({"price": price, "type": kind, "bar": bar_now})
        # keep only the most recent N
        structure_state.swing_levels[symbol] = levels[-MAX_TRACKED_LEVELS:]
        levels = structure_state.swing_levels[symbol]

        last = df.iloc[-1]
        close = float(last["Close"])

        # ── Step 2: check if the latest close breaks any tracked level ──
        for lvl in levels:
            already_broken = any(abs(b["price"] - lvl["price"]) < 1e-9 for b in broken)
            if already_broken:
                continue
            if lvl["type"] == "high" and close > lvl["price"]:
                broken.append({"price": lvl["price"], "direction": "CALL",
                                "bar_broken": bar_now, "origin_bar": lvl["bar"]})
                log.info(f"[STRUCTURE] {symbol} broke resistance at {lvl['price']} — awaiting retest")
            elif lvl["type"] == "low" and close < lvl["price"]:
                broken.append({"price": lvl["price"], "direction": "PUT",
                                "bar_broken": bar_now, "origin_bar": lvl["bar"]})
                log.info(f"[STRUCTURE] {symbol} broke support at {lvl['price']} — awaiting retest")

        # drop expired broken levels
        broken = [b for b in broken if (bar_now - b["bar_broken"]) <= LEVEL_EXPIRY_BARS]
        structure_state.broken_levels[symbol] = broken

        if not structure_state.can_fire(symbol):
            return None

        # ── Step 3: check for a retest + rejection on any broken level ──
        o, h, l, c = float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"])
        prev = df.iloc[-2]

        for b in broken:
            level = b["price"]
            direction = b["direction"]
            tol = level * RETEST_TOLERANCE_PCT

            touched_retest = (l <= level + tol) if direction == "CALL" else (h >= level - tol)
            if not touched_retest:
                continue

            if direction == "CALL":
                rejection = _is_bullish_pin_bar(o, h, l, c) or _is_bullish_engulfing(prev, last)
            else:
                rejection = _is_bearish_pin_bar(o, h, l, c) or _is_bearish_engulfing(prev, last)

            if not rejection:
                continue

            # measured move = distance from the origin swing to the break point
            recent = df.tail(min(bar_now - b["origin_bar"] + 2, 30)) if bar_now > b["origin_bar"] else df.tail(10)
            if direction == "CALL":
                leg_size = abs(float(recent["High"].max()) - level)
            else:
                leg_size = abs(level - float(recent["Low"].min()))
            if leg_size == 0:
                leg_size = abs(level) * 0.005   # fallback so TP isn't zero

            buf = SL_BUFFER_PCT.get(symbol, SL_BUFFER_PCT["DEFAULT"])
            if direction == "CALL":
                sl  = round(level * (1 - buf), 5)
                tps = [round(c + leg_size * m, 5) for m in TP_MOVE_MULTIPLES]
            else:
                sl  = round(level * (1 + buf), 5)
                tps = [round(c - leg_size * m, 5) for m in TP_MOVE_MULTIPLES]

            structure_state.mark_fired(symbol)
            # remove this level so it doesn't refire immediately
            structure_state.broken_levels[symbol] = [
                x for x in structure_state.broken_levels[symbol] if x is not b
            ]

            result = {
                "symbol": symbol,
                "direction": "✅ CALL" if direction == "CALL" else "❌ PUT",
                "raw_dir": direction,
                "price": round(c, 5),
                "level": round(level, 5),
                "sl": sl, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2],
                "pattern": "Retest + Rejection",
                "strategy": "STRUCTURE",
                "session": "🧱 Break & Retest",
            }
            log.info(f"[STRUCTURE] {symbol} {direction} fired — retest of {level}")
            return result

        return None


# ──────────────────────────────────────────────────
# FORMATTER
# ──────────────────────────────────────────────────
def format_structure_signal(sig):
    now = datetime.utcnow()
    return (
        f"🚨 *STRUCTURE SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Pair: `{sig['symbol']}`\n"
        f"🎯 Direction: *{sig['direction']}*\n"
        f"📍 Strategy: `[STRUCTURE]`\n"
        f"🕯 Pattern: `{sig['pattern']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧱 Flipped Level: `{sig['level']}`\n"
        f"🎯 *Entry*: `{sig['price']}`\n"
        f"⛔ *Stop Loss*: `{sig['sl']}`\n"
        f"✅ *TP1*: `{sig['tp1']}`\n"
        f"✅ *TP2*: `{sig['tp2']}`\n"
        f"✅ *TP3*: `{sig['tp3']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now.strftime('%H:%M:%S UTC')}`\n"
        f"⚠️ _Risk management always applies_\n"
        f"📝 _Reply /win STRUCTURE or /loss STRUCTURE after trade_"
    )
