# ==============================================================================
# HIGH-PROBABILITY ZONE SIGNAL BOT – RENDER READY (NO MT5)
# Uses public market data APIs | Telegram signals only | Wide SL/TP logic
# ==============================================================================
"""
Deploy this on Render as a Web Service.
It does NOT need MetaTrader 5.
It fetches live prices via public APIs, runs the same zone + probability logic,
and sends only high-probability signals to your Telegram.
"""

import os
import time
import math
import json
import logging
import threading
import collections
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone
import urllib.request
import urllib.parse
import urllib.error

# Optional: FastAPI keeps the Render service alive with a health endpoint
try:
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

# ==============================================================================
# LOGGING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("RenderHighProb")

# ==============================================================================
# CONFIG – set these as Environment Variables on Render
# ==============================================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Symbols we support (mapped to free data sources)
# You can add more later
SYMBOLS = ["XAUUSD", "BTCUSD"]

# High-probability settings (same philosophy as before)
REV_PROB_THRESHOLD = 0.78
MIN_TOUCHES = 4
MIN_ZONE_CONFIDENCE = 0.62
SL_ATR_MULT = 3.2          # wide stop
TP_ATR_MULT = 9.5          # high R:R ≈ 1:3
COOLDOWN_SEC = 900         # 15 min between signals per symbol
TICK_BUFFER = 600
ATR_PERIOD = 40
ZONE_EPS_ATR = 0.38
ZONE_DECAY = 0.00025
ZONE_DEAD_TIME = 1800
SWING_K = 4
RSI_PERIOD = 14
POLL_INTERVAL = 8          # seconds between price fetches (Render friendly)

# ==============================================================================
# DATA STRUCTURES
# ==============================================================================
@dataclass
class Zone:
    center: float
    width: float
    touches: int = 0
    confidence: float = 1.0
    polarity: str = "NEUTRAL"
    last_touch_time: float = field(default_factory=time.time)
    anchors: List[float] = field(default_factory=list)
    strength: float = 0.5


# ==============================================================================
# GLOBAL STATE
# ==============================================================================
price_buffer: Dict[str, collections.deque] = {
    s: collections.deque(maxlen=TICK_BUFFER) for s in SYMBOLS
}
zones: Dict[str, List[Zone]] = {s: [] for s in SYMBOLS}
atr_cache: Dict[str, float] = {s: 0.0 for s in SYMBOLS}
rsi_cache: Dict[str, float] = {s: 50.0 for s in SYMBOLS}
last_signal_time: Dict[str, float] = {s: 0.0 for s in SYMBOLS}
last_signal_hash: Dict[str, str] = {s: "" for s in SYMBOLS}
htf_trend: Dict[str, str] = {s: "NEUTRAL" for s in SYMBOLS}

# ==============================================================================
# TELEGRAM
# ==============================================================================
def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing – set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=12) as resp:
            ok = resp.status == 200
            if ok:
                logger.info("Telegram signal delivered")
            return ok
    except Exception as e:
        logger.error(f"Telegram failed: {e}")
        return False


def format_signal(symbol: str, bias: str, price: float, sl: float, tp: float,
                  prob: float, atr: float, zone_center: float, touches: int,
                  rsi: float, htf: str) -> str:
    rr = abs(tp - price) / max(abs(price - sl), 1e-9)
    emoji = "🟢 BUY" if bias == "BUY" else "🔴 SELL"
    return (
        f"<b>HIGH PROBABILITY SIGNAL</b>\n\n"
        f"{emoji} <b>{symbol}</b>\n"
        f"Entry ≈ <code>{price:.5f}</code>\n"
        f"SL     <code>{sl:.5f}</code>\n"
        f"TP     <code>{tp:.5f}</code>\n\n"
        f"Risk:Reward ≈ <b>1:{rr:.1f}</b>\n"
        f"Probability: <b>{prob*100:.1f}%</b>\n"
        f"Zone touches: {touches}\n"
        f"ATR: {atr:.5f} | RSI: {rsi:.1f}\n"
        f"HTF Bias: {htf}\n"
        f"Zone center: {zone_center:.5f}\n\n"
        f"<i>Intended hold: 30 min – several hours</i>\n"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

# ==============================================================================
# MARKET DATA (NO MT5 – works on Render Linux)
# ==============================================================================
def fetch_binance_price(symbol: str) -> Optional[float]:
    """BTCUSDT from Binance public API"""
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            return float(data["price"])
    except Exception as e:
        logger.debug(f"Binance error: {e}")
        return None


def fetch_gold_price() -> Optional[float]:
    """
    Free gold price sources (fallback chain).
    1. metals-api style free endpoints are limited, so we use a simple public source.
    For production you should replace with a reliable paid API (TwelveData, Polygon, etc.)
    """
    # Simple free source – may have rate limits
    sources = [
        "https://api.metalpriceapi.com/v1/latest?api_key=demo&base=USD&currencies=XAU",  # demo often limited
    ]
    # Fallback: use a public JSON that many free sites expose
    try:
        # Alternative: Yahoo-style via a public proxy or just skip if fails
        # For reliability on Render we use a lightweight approach
        url = "https://data-asg.goldprice.org/dbXRates/USD"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            # structure: {"items":[{"xauPrice": ....}]}
            if "items" in data and len(data["items"]) > 0:
                return float(data["items"][0].get("xauPrice", 0))
    except Exception as e:
        logger.debug(f"Gold source error: {e}")
    return None


def get_live_price(symbol: str) -> Optional[float]:
    if symbol.upper() in ("BTCUSD", "BTCUSDT", "BTC"):
        return fetch_binance_price(symbol)
    if symbol.upper() in ("XAUUSD", "GOLD", "XAU"):
        return fetch_gold_price()
    return None


def get_recent_closes(symbol: str, count: int = 100) -> List[float]:
    """
    Build a simple close series from successive live prices.
    On Render we accumulate prices over time in the buffer.
    For initial seed we just return what we have.
    """
    return list(price_buffer[symbol])[-count:]

# ==============================================================================
# INDICATORS & ZONE LOGIC (same core as your good system)
# ==============================================================================
def compute_atr_from_prices(prices: List[float], period: int = ATR_PERIOD) -> float:
    if len(prices) < period + 2:
        return 0.0
    arr = np_diff = []
    # Approximate true range from close-to-close for simplicity on tick stream
    diffs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    if len(diffs) < period:
        return float(sum(diffs) / len(diffs)) if diffs else 0.0
    return float(sum(diffs[-period:]) / period)


def compute_rsi(prices: List[float], period: int = RSI_PERIOD) -> float:
    if len(prices) < period + 2:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 70.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def detect_swing(buffer: collections.deque, k: int = SWING_K) -> Optional[Tuple[str, float]]:
    if len(buffer) < 2 * k + 1:
        return None
    prices = list(buffer)
    mid_idx = -k - 1
    mid = prices[mid_idx]
    left = prices[-2 * k - 1: mid_idx]
    right = prices[-k:]
    if mid > max(left) and mid > max(right):
        return ("HIGH", mid)
    if mid < min(left) and mid < min(right):
        return ("LOW", mid)
    return None


def update_zones(symbol: str, anchor: float, atr: float, swing_type: Optional[str]):
    attached = False
    for z in zones[symbol]:
        if abs(anchor - z.center) < ZONE_EPS_ATR * atr:
            z.anchors.append(anchor)
            if len(z.anchors) > 12:
                z.anchors = z.anchors[-12:]
            z.center = sum(z.anchors) / len(z.anchors)
            std = (sum((a - z.center)**2 for a in z.anchors) / len(z.anchors))**0.5
            z.width = std + 0.15 * atr
            z.touches += 1
            z.confidence = min(1.0, z.confidence + 0.11)
            z.strength = z.confidence * min(1.0, z.touches / 7.0)
            if swing_type == "HIGH":
                z.polarity = "RESISTANCE"
            elif swing_type == "LOW":
                z.polarity = "SUPPORT"
            attached = True
            break
    if not attached:
        pol = "RESISTANCE" if swing_type == "HIGH" else "SUPPORT" if swing_type == "LOW" else "NEUTRAL"
        zones[symbol].append(Zone(
            center=anchor, width=0.25 * atr, anchors=[anchor],
            polarity=pol, strength=0.35
        ))


def merge_zones(symbol: str, atr: float):
    if len(zones[symbol]) < 2:
        return
    zones[symbol].sort(key=lambda z: z.center)
    i = 0
    while i < len(zones[symbol]) - 1:
        z1, z2 = zones[symbol][i], zones[symbol][i+1]
        if abs(z1.center - z2.center) < (z1.width + z2.width) * 0.55:
            anchors = z1.anchors + z2.anchors
            center = sum(anchors) / len(anchors)
            std = (sum((a - center)**2 for a in anchors) / len(anchors))**0.5
            width = std + 0.15 * atr
            touches = z1.touches + z2.touches
            conf = (z1.confidence + z2.confidence) / 2
            pol = z1.polarity if z1.polarity == z2.polarity else "NEUTRAL"
            strength = conf * min(1.0, touches / 7.0)
            zones[symbol][i] = Zone(center, width, touches, conf, pol,
                                    max(z1.last_touch_time, z2.last_touch_time),
                                    anchors, strength)
            del zones[symbol][i+1]
        else:
            i += 1


def process_zone_interactions(symbol: str, price: float, atr: float):
    now = time.time()
    for z in zones[symbol]:
        age = now - z.last_touch_time
        z.confidence *= math.exp(-ZONE_DECAY * age)
        z.strength = z.confidence * min(1.0, z.touches / 7.0)
        if abs(price - z.center) < z.width and age > 12:
            z.touches += 1
            z.last_touch_time = now
            z.confidence = min(1.0, z.confidence + 0.15)
            z.strength = z.confidence * min(1.0, z.touches / 7.0)


def prune_zones(symbol: str):
    now = time.time()
    zones[symbol] = [
        z for z in zones[symbol]
        if z.confidence > 0.25 and (now - z.last_touch_time) < ZONE_DEAD_TIME
    ]


def compute_reversal_probability(symbol: str, price: float, atr: float) -> List[Dict]:
    results = []
    prices = list(price_buffer[symbol])
    if len(prices) < max(30, RSI_PERIOD + 5) or atr <= 0:
        return results

    velocity = [prices[i] - prices[i-1] for i in range(-7, 0)]
    v_mean = sum(velocity) / len(velocity)
    v_norm = abs(v_mean) / atr
    exhaustion = max(0.0, 1.0 - min(v_norm, 1.6) / 1.6)

    short_std = (sum((p - sum(prices[-18:])/18)**2 for p in prices[-18:]) / 18)**0.5
    long_std  = (sum((p - sum(prices[-70:])/70)**2 for p in prices[-70:]) / 70)**0.5 + 1e-9
    compression = max(0.0, 1.0 - short_std / long_std)

    current_rsi = rsi_cache[symbol]
    htf = htf_trend[symbol]

    for z in zones[symbol]:
        if z.touches < MIN_TOUCHES or z.confidence < MIN_ZONE_CONFIDENCE:
            continue
        dist = abs(price - z.center)
        if dist > z.width * 1.08:
            continue

        impulse = math.exp(-dist / (atr * 0.9))
        approach = 0.0
        rsi_bonus = 0.0

        if z.polarity == "RESISTANCE":
            approach = 0.15 * v_norm if v_mean > 0 else -0.06
            rsi_bonus = 0.12 if current_rsi > 66 else (-0.08 if current_rsi < 36 else 0)
        elif z.polarity == "SUPPORT":
            approach = 0.15 * v_norm if v_mean < 0 else -0.06
            rsi_bonus = 0.12 if current_rsi < 34 else (-0.08 if current_rsi > 64 else 0)
        else:
            continue

        # Simple HTF penalty/bonus
        htf_score = 0.0
        if z.polarity == "SUPPORT" and htf == "BULL":
            htf_score = 0.12
        elif z.polarity == "RESISTANCE" and htf == "BEAR":
            htf_score = 0.12
        elif z.polarity == "SUPPORT" and htf == "BEAR":
            htf_score = -0.16
        elif z.polarity == "RESISTANCE" and htf == "BULL":
            htf_score = -0.16

        strength_score = 0.30 * z.strength
        raw = (strength_score + 0.16 * exhaustion + 0.12 * compression +
               0.13 * impulse + approach + rsi_bonus + htf_score)
        prob = max(0.0, min(1.0, raw))
        results.append({"zone": z, "probability": prob})
    return results


def update_simple_htf(symbol: str):
    """Very light HTF bias from the price buffer itself."""
    prices = list(price_buffer[symbol])
    if len(prices) < 80:
        return
    ema_fast = sum(prices[-21:]) / 21
    ema_slow = sum(prices[-55:]) / 55
    if ema_fast > ema_slow * 1.0005:
        htf_trend[symbol] = "BULL"
    elif ema_fast < ema_slow * 0.9995:
        htf_trend[symbol] = "BEAR"
    else:
        htf_trend[symbol] = "NEUTRAL"

# ==============================================================================
# MAIN SIGNAL LOOP
# ==============================================================================
def signal_loop():
    logger.info("Signal engine started (Render mode – no MT5)")
    while True:
        try:
            for symbol in SYMBOLS:
                price = get_live_price(symbol)
                if price is None or price <= 0:
                    continue

                price_buffer[symbol].append(price)

                # ATR & RSI
                prices = list(price_buffer[symbol])
                atr = compute_atr_from_prices(prices)
                atr_cache[symbol] = atr
                rsi_cache[symbol] = compute_rsi(prices)

                if atr <= 0 or len(prices) < 40:
                    continue

                update_simple_htf(symbol)

                # Zones
                swing = detect_swing(price_buffer[symbol])
                if swing:
                    update_zones(symbol, swing[1], atr, swing[0])
                    merge_zones(symbol, atr)
                process_zone_interactions(symbol, price, atr)
                prune_zones(symbol)

                # High-prob signals only
                reversals = compute_reversal_probability(symbol, price, atr)
                now = time.time()
                for r in reversals:
                    if r["probability"] < REV_PROB_THRESHOLD:
                        continue
                    zone = r["zone"]
                    bias = "SELL" if zone.polarity == "RESISTANCE" else \
                           "BUY" if zone.polarity == "SUPPORT" else None
                    if not bias:
                        continue

                    # Cooldown + de-dupe
                    if now - last_signal_time.get(symbol, 0) < COOLDOWN_SEC:
                        continue
                    sig_hash = f"{symbol}-{bias}-{round(zone.center, 2)}"
                    if last_signal_hash.get(symbol) == sig_hash:
                        continue

                    last_signal_time[symbol] = now
                    last_signal_hash[symbol] = sig_hash

                    # Calculate wide SL / TP
                    if bias == "BUY":
                        sl = price - SL_ATR_MULT * atr
                        tp = price + TP_ATR_MULT * atr
                    else:
                        sl = price + SL_ATR_MULT * atr
                        tp = price - TP_ATR_MULT * atr

                    msg = format_signal(
                        symbol, bias, price, sl, tp,
                        r["probability"], atr, zone.center,
                        zone.touches, rsi_cache[symbol], htf_trend[symbol]
                    )
                    send_telegram(msg)
                    logger.info(f"SIGNAL {bias} {symbol} | Prob={r['probability']:.3f}")

            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Loop error: {e}", exc_info=True)
            time.sleep(15)


# ==============================================================================
# FASTAPI HEALTH (keeps Render web service alive)
# ==============================================================================
if HAS_FASTAPI:
    app = FastAPI(title="HighProb Signal Bot")

    @app.get("/")
    def health():
        return {
            "status": "running",
            "symbols": SYMBOLS,
            "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
            "time": datetime.now(timezone.utc).isoformat()
        }

    @app.get("/health")
    def health_check():
        return PlainTextResponse("ok")


# ==============================================================================
# ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  RENDER HIGH-PROBABILITY SIGNAL BOT")
    logger.info(f"  Symbols     : {SYMBOLS}")
    logger.info(f"  Min Prob    : {REV_PROB_THRESHOLD}")
    logger.info(f"  SL/TP ATR   : {SL_ATR_MULT}x / {TP_ATR_MULT}x  (R:R ≈ 1:{TP_ATR_MULT/SL_ATR_MULT:.1f})")
    logger.info(f"  Telegram    : {'READY' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else 'NOT CONFIGURED'}")
    logger.info("=" * 60)

    # Start signal engine in background
    t = threading.Thread(target=signal_loop, daemon=True)
    t.start()

    if HAS_FASTAPI:
        port = int(os.getenv("PORT", 10000))
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    else:
        # If no FastAPI, just keep the loop alive
        while True:
            time.sleep(60)
