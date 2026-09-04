
# ==============================================================================
# HIGH-PROBABILITY ZONE SIGNAL BOT – RENDER + TWELVE DATA
# Human-style Telegram signals | Wide SL/TP | Real-time | Startup test
# ==============================================================================

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
logger = logging.getLogger("HighProb")

# ==============================================================================
# CONFIG
# ==============================================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Twelve Data API Key
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "abb27fe4fa8749d8a20a042ef4d100ee")

# Symbols (Twelve Data format -> internal name)
SYMBOLS = {
    "XAU/USD": "XAUUSD",
    "BTC/USD": "BTCUSD",
}

# Signal quality
REV_PROB_THRESHOLD = 0.72
MIN_TOUCHES = 3
MIN_ZONE_CONFIDENCE = 0.55
SL_ATR_MULT = 3.0
TP_ATR_MULT = 9.0
COOLDOWN_SEC = 720
TICK_BUFFER = 500
ATR_PERIOD = 30
ZONE_EPS_ATR = 0.40
ZONE_DECAY = 0.0003
ZONE_DEAD_TIME = 1500
SWING_K = 3
RSI_PERIOD = 14
POLL_INTERVAL = 12

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
    s: collections.deque(maxlen=TICK_BUFFER) for s in SYMBOLS.values()
}
zones: Dict[str, List[Zone]] = {s: [] for s in SYMBOLS.values()}
atr_cache: Dict[str, float] = {s: 0.0 for s in SYMBOLS.values()}
rsi_cache: Dict[str, float] = {s: 50.0 for s in SYMBOLS.values()}
last_signal_time: Dict[str, float] = {s: 0.0 for s in SYMBOLS.values()}
last_signal_hash: Dict[str, str] = {s: "" for s in SYMBOLS.values()}
htf_trend: Dict[str, str] = {s: "NEUTRAL" for s in SYMBOLS.values()}
last_price: Dict[str, float] = {s: 0.0 for s in SYMBOLS.values()}
signal_count = 0

# ==============================================================================
# TELEGRAM – HUMAN STYLE
# ==============================================================================
def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured")
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
                logger.info("Telegram delivered")
            return ok
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False


def human_signal(symbol: str, bias: str, price: float, sl: float, tp: float,
                 prob: float, atr: float, zone_center: float, touches: int,
                 rsi: float, htf: str) -> str:
    rr = abs(tp - price) / max(abs(price - sl), 1e-9)
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if bias == "BUY":
        action = "Looking to buy"
        emoji = "🟢"
        reason = "price reacting from support with good confluence"
    else:
        action = "Looking to sell"
        emoji = "🔴"
        reason = "price rejecting resistance with decent momentum shift"

    conf_text = "high conviction" if prob >= 0.80 else "solid setup"

    msg = (
        f"{emoji} <b>{symbol}</b> — {action}\n\n"
        f"Entry zone: <b>{price:.2f}</b>\n"
        f"Stop loss: <b>{sl:.2f}</b>\n"
        f"Take profit: <b>{tp:.2f}</b>\n\n"
        f"Risk : Reward ≈ <b>1 : {rr:.1f}</b>\n"
        f"Probability: {prob*100:.0f}% ({conf_text})\n\n"
        f"Why: {reason}\n"
        f"Zone strength: {touches} touches | RSI {rsi:.0f} | HTF {htf}\n"
        f"ATR: {atr:.2f}\n\n"
        f"<i>Hold for the move — ideally 30 min+</i>\n"
        f"{now}"
    )
    return msg

# ==============================================================================
# TWELVE DATA
# ==============================================================================
def td_get(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HighProbBot/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"TwelveData error: {e}")
        return None


def fetch_price_twelvedata(td_symbol: str) -> Optional[float]:
    url = (
        f"https://api.twelvedata.com/price"
        f"?symbol={urllib.parse.quote(td_symbol)}"
        f"&apikey={TWELVEDATA_API_KEY}"
    )
    data = td_get(url)
    if data and "price" in data:
        try:
            return float(data["price"])
        except (TypeError, ValueError):
            return None
    if data and "code" in data:
        logger.warning(f"TwelveData {td_symbol}: {data.get('message', data)}")
    return None


def fetch_time_series(td_symbol: str, interval: str = "1min", outputsize: int = 60) -> List[float]:
    url = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol={urllib.parse.quote(td_symbol)}"
        f"&interval={interval}"
        f"&outputsize={outputsize}"
        f"&apikey={TWELVEDATA_API_KEY}"
    )
    data = td_get(url)
    if not data or "values" not in data:
        return []
    try:
        closes = [float(v["close"]) for v in reversed(data["values"])]
        return closes
    except Exception:
        return []

# ==============================================================================
# INDICATORS + ZONE ENGINE
# ==============================================================================
def compute_atr(prices: List[float], period: int = ATR_PERIOD) -> float:
    if len(prices) < period + 1:
        return 0.0
    diffs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    if len(diffs) < period:
        return sum(diffs) / len(diffs) if diffs else 0.0
    return sum(diffs[-period:]) / period


def compute_rsi(prices: List[float], period: int = RSI_PERIOD) -> float:
    if len(prices) < period + 2:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss <= 0:
        return 70.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def detect_swing(buffer: collections.deque, k: int = SWING_K) -> Optional[Tuple[str, float]]:
    if len(buffer) < 2 * k + 1:
        return None
    prices = list(buffer)
    mid_idx = -k - 1
    mid = prices[mid_idx]
    left = prices[-2*k-1:mid_idx]
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
            z.confidence = min(1.0, z.confidence + 0.12)
            z.strength = z.confidence * min(1.0, z.touches / 6.0)
            if swing_type == "HIGH":
                z.polarity = "RESISTANCE"
            elif swing_type == "LOW":
                z.polarity = "SUPPORT"
            attached = True
            break
    if not attached:
        pol = "RESISTANCE" if swing_type == "HIGH" else "SUPPORT" if swing_type == "LOW" else "NEUTRAL"
        zones[symbol].append(Zone(
            center=anchor, width=0.28 * atr, anchors=[anchor],
            polarity=pol, strength=0.4
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
            strength = conf * min(1.0, touches / 6.0)
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
        z.strength = z.confidence * min(1.0, z.touches / 6.0)
        if abs(price - z.center) < z.width and age > 10:
            z.touches += 1
            z.last_touch_time = now
            z.confidence = min(1.0, z.confidence + 0.14)
            z.strength = z.confidence * min(1.0, z.touches / 6.0)


def prune_zones(symbol: str):
    now = time.time()
    zones[symbol] = [
        z for z in zones[symbol]
        if z.confidence > 0.22 and (now - z.last_touch_time) < ZONE_DEAD_TIME
    ]


def compute_reversal_probability(symbol: str, price: float, atr: float) -> List[Dict]:
    results = []
    prices = list(price_buffer[symbol])
    if len(prices) < 35 or atr <= 0:
        return results

    velocity = [prices[i] - prices[i-1] for i in range(-6, 0)]
    v_mean = sum(velocity) / len(velocity)
    v_norm = abs(v_mean) / atr
    exhaustion = max(0.0, 1.0 - min(v_norm, 1.5) / 1.5)

    short_std = (sum((p - sum(prices[-15:])/15)**2 for p in prices[-15:]) / 15)**0.5
    long_std  = (sum((p - sum(prices[-50:])/50)**2 for p in prices[-50:]) / 50)**0.5 + 1e-9
    compression = max(0.0, 1.0 - short_std / long_std)

    current_rsi = rsi_cache[symbol]
    htf = htf_trend[symbol]

    for z in zones[symbol]:
        if z.touches < MIN_TOUCHES or z.confidence < MIN_ZONE_CONFIDENCE:
            continue
        dist = abs(price - z.center)
        if dist > z.width * 1.1:
            continue

        impulse = math.exp(-dist / (atr * 0.95))
        approach = 0.0
        rsi_bonus = 0.0

        if z.polarity == "RESISTANCE":
            approach = 0.14 * v_norm if v_mean > 0 else -0.05
            rsi_bonus = 0.11 if current_rsi > 65 else (-0.07 if current_rsi < 38 else 0)
        elif z.polarity == "SUPPORT":
            approach = 0.14 * v_norm if v_mean < 0 else -0.05
            rsi_bonus = 0.11 if current_rsi < 35 else (-0.07 if current_rsi > 62 else 0)
        else:
            continue

        htf_score = 0.0
        if z.polarity == "SUPPORT" and htf == "BULL":
            htf_score = 0.11
        elif z.polarity == "RESISTANCE" and htf == "BEAR":
            htf_score = 0.11
        elif z.polarity == "SUPPORT" and htf == "BEAR":
            htf_score = -0.15
        elif z.polarity == "RESISTANCE" and htf == "BULL":
            htf_score = -0.15

        strength_score = 0.28 * z.strength
        raw = (strength_score + 0.15 * exhaustion + 0.11 * compression +
               0.14 * impulse + approach + rsi_bonus + htf_score)
        prob = max(0.0, min(1.0, raw))
        results.append({"zone": z, "probability": prob})
    return results


def update_htf(symbol: str):
    prices = list(price_buffer[symbol])
    if len(prices) < 60:
        return
    ema_fast = sum(prices[-20:]) / 20
    ema_slow = sum(prices[-50:]) / 50
    if ema_fast > ema_slow * 1.0006:
        htf_trend[symbol] = "BULL"
    elif ema_fast < ema_slow * 0.9994:
        htf_trend[symbol] = "BEAR"
    else:
        htf_trend[symbol] = "NEUTRAL"

# ==============================================================================
# STARTUP + MAIN LOOP
# ==============================================================================
def seed_buffers():
    logger.info("Seeding price buffers from Twelve Data...")
    for td_sym, internal in SYMBOLS.items():
        closes = fetch_time_series(td_sym, interval="1min", outputsize=80)
        if closes:
            for c in closes:
                price_buffer[internal].append(c)
            last_price[internal] = closes[-1]
            logger.info(f"  {internal}: seeded {len(closes)} bars | last={closes[-1]:.2f}")
        else:
            logger.warning(f"  {internal}: could not seed – will wait for live prices")
        time.sleep(1.5)


def startup_test():
    test_msg = (
        "✅ <b>System online</b>\n\n"
        "High-probability signal engine is running.\n"
        "I will only send clean setups with proper Stop Loss and Take Profit.\n\n"
        f"Watching: {', '.join(SYMBOLS.values())}\n"
        f"Min probability: {int(REV_PROB_THRESHOLD*100)}%\n"
        f"Target R:R ≈ 1:{TP_ATR_MULT/SL_ATR_MULT:.1f}\n\n"
        f"<i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>"
    )
    ok = send_telegram(test_msg)
    if ok:
        logger.info("Startup test message sent to Telegram")
    else:
        logger.warning("Startup test failed – check TELEGRAM_BOT_TOKEN and CHAT_ID")


def signal_loop():
    global signal_count
    logger.info("Real-time signal loop started")
    while True:
        try:
            for td_sym, symbol in SYMBOLS.items():
                price = fetch_price_twelvedata(td_sym)
                if price is None or price <= 0:
                    continue

                price_buffer[symbol].append(price)
                last_price[symbol] = price

                prices = list(price_buffer[symbol])
                atr = compute_atr(prices)
                atr_cache[symbol] = atr
                rsi_cache[symbol] = compute_rsi(prices)

                if atr <= 0 or len(prices) < 40:
                    continue

                update_htf(symbol)

                swing = detect_swing(price_buffer[symbol])
                if swing:
                    update_zones(symbol, swing[1], atr, swing[0])
                    merge_zones(symbol, atr)
                process_zone_interactions(symbol, price, atr)
                prune_zones(symbol)

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

                    if now - last_signal_time.get(symbol, 0) < COOLDOWN_SEC:
                        continue
                    sig_hash = f"{symbol}-{bias}-{round(zone.center, 1)}"
                    if last_signal_hash.get(symbol) == sig_hash:
                        continue

                    last_signal_time[symbol] = now
                    last_signal_hash[symbol] = sig_hash
                    signal_count += 1

                    if bias == "BUY":
                        sl = price - SL_ATR_MULT * atr
                        tp = price + TP_ATR_MULT * atr
                    else:
                        sl = price + SL_ATR_MULT * atr
                        tp = price - TP_ATR_MULT * atr

                    msg = human_signal(
                        symbol, bias, price, sl, tp,
                        r["probability"], atr, zone.center,
                        zone.touches, rsi_cache[symbol], htf_trend[symbol]
                    )
                    send_telegram(msg)
                    logger.info(
                        f"SIGNAL #{signal_count} {bias} {symbol} @ {price:.2f} "
                        f"| Prob={r['probability']:.2f} | SL={sl:.2f} TP={tp:.2f}"
                    )

            time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Loop error: {e}", exc_info=True)
            time.sleep(20)


# ==============================================================================
# FASTAPI
# ==============================================================================
if HAS_FASTAPI:
    app = FastAPI(title="HighProb Signals")

    @app.get("/")
    def root():
        return {
            "status": "live",
            "engine": "High-Probability Zone System + Twelve Data",
            "symbols": list(SYMBOLS.values()),
            "signals_sent": signal_count,
            "telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
            "twelvedata": bool(TWELVEDATA_API_KEY),
            "min_probability": REV_PROB_THRESHOLD,
            "last_prices": last_price,
            "time": datetime.now(timezone.utc).isoformat()
        }

    @app.get("/health")
    def health():
        return PlainTextResponse("ok")

# ==============================================================================
# MAIN
# ==============================================================================
if __name__ == "__main__":
    logger.info("=" * 64)
    logger.info("  HIGH-PROBABILITY SIGNAL BOT + TWELVE DATA")
    logger.info(f"  Symbols      : {list(SYMBOLS.values())}")
    logger.info(f"  Min Prob     : {REV_PROB_THRESHOLD}")
    logger.info(f"  SL / TP      : {SL_ATR_MULT}x / {TP_ATR_MULT}x ATR")
    logger.info(f"  TwelveData   : {'SET' if TWELVEDATA_API_KEY else 'MISSING'}")
    logger.info(f"  Telegram     : {'SET' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else 'MISSING'}")
    logger.info("=" * 64)

    seed_buffers()
    startup_test()

    t = threading.Thread(target=signal_loop, daemon=True)
    t.start()

    if HAS_FASTAPI:
        port = int(os.getenv("PORT", 10000))
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    else:
        while True:
            time.sleep(60)
