# 📊 Pocket Option Signal Bot — SMC Edition

A Telegram signal bot for Pocket Option trading, built with **Smart Money Concepts** (SMC) analysis:
- Fair Value Gap (FVG) detection
- Liquidity Sweep identification
- Market Structure Shift (MSS)
- Order Block confirmation
- EMA + RSI + MACD confluence filter

---

## ⚡ Quick Setup (5 minutes)

### Step 1 — Create Your Telegram Bot
1. Open Telegram → search **@BotFather**
2. Send `/newbot` → follow prompts
3. Copy your **BOT_TOKEN**

### Step 2 — Get Your Chat ID
1. Create a Telegram channel or group
2. Add your bot as **admin**
3. Send any message to the channel
4. Visit: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
5. Find `"chat":{"id": -XXXXXXXXX}` → that's your **CHAT_ID**

### Step 3 — Configure the Bot
```bash
cp .env.example .env
# Edit .env and fill in BOT_TOKEN and CHAT_ID
```

Or edit `bot.py` directly:
```python
BOT_TOKEN = "7xxxxxxxxxx:AAF..."
CHAT_ID   = "-100xxxxxxxxxx"
```

### Step 4 — Install & Run
```bash
pip install -r requirements.txt
python bot.py
```

---

## 🤖 Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/stats` | Full performance report with win rate |
| `/pairs` | List all active trading pairs |
| `/scan` | Force an immediate market scan |
| `/win` | Record last signal as a WIN |
| `/loss` | Record last signal as a LOSS |
| `/pause` | Stop sending signals |
| `/resume` | Resume sending signals |
| `/help` | Show all commands |

---

## 📈 Signal Logic

A signal is only sent when **3 or more** of these conditions align:

| Condition | Bullish (CALL) | Bearish (PUT) |
|-----------|---------------|---------------|
| EMA Cross | EMA8 > EMA21 | EMA8 < EMA21 |
| RSI | > 50 | < 50 |
| MACD | Above signal | Below signal |
| FVG | Bullish gap | Bearish gap |
| Liquidity Sweep | Low swept | High swept |
| MSS | Break above swing high | Break below swing low |
| Order Block | Last bear candle before rally | Last bull candle before drop |

**Minimum score = 3** (configurable via `MIN_SCORE` in bot.py)

---

## ⚙️ Configuration

Edit these values in `bot.py`:

```python
EXPIRY_MIN  = 5     # Trade expiry in minutes
SCAN_EVERY  = 5     # How often to scan (minutes)
MIN_SCORE   = 3     # Minimum SMC confluence (1–5)

PAIRS = {
    "XAUUSD": "GC=F",       # Gold
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "BTCUSD": "BTC-USD",
}
```

---

## 🚀 Deploy 24/7 (Free)

### Option A — Railway.app (Recommended)
1. Push this folder to GitHub
2. Go to railway.app → New Project → Deploy from GitHub
3. Add environment variables: `BOT_TOKEN`, `CHAT_ID`
4. Deploy as background worker

### Option B — VPS (Most Reliable)
```bash
# Upload files to your VPS, then:
screen -S signalbot
python bot.py
# Press Ctrl+A then D to detach
```

### Option C — Docker
```bash
docker build -t signal-bot .
docker run -d \
  -e BOT_TOKEN="your_token" \
  -e CHAT_ID="your_chat_id" \
  --name signal-bot \
  signal-bot
```

---

## 📊 Win/Loss Tracking

The bot saves all signal results to `stats.json` automatically.

After each trade closes on Pocket Option:
- Send `/win` → records a WIN for the last signal
- Send `/loss` → records a LOSS for the last signal
- Send `/stats` → view full breakdown

Stats include:
- Overall win rate with visual bar
- Per-pair breakdown
- Today's session results
- Current & best win streak
- Best performing pair

---

## ⚠️ Disclaimer

- This bot does **not** connect directly to Pocket Option
- Signals are based on technical analysis — **not guaranteed**
- Always use proper risk management (1–2% per trade max)
- Binary options carry high risk. Trade responsibly.
