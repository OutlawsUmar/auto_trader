import ccxt
import pandas as pd
from telegram import Bot
import time
import datetime
import math

import hmac
import hashlib
import requests
from urllib.parse import urlencode

from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# ================= CONFIG =================
API_TOKEN = "8764116821:AAEPAwJq5hy3bAUD7VSdgz7juwfz2i2_kD4"
CHAT_ID = "-1003978043796"


BINANCE_API_KEY = "u7dAiy2khBOgBiz5T2bSA742Et6AlX9KBxo2cTkvijEfgS9AeTDkJREI9xH7jGk9"
BINANCE_API_SECRET = "cxaVPBF1CI4cS4BezfrGuJiqp8FP5wLCWjucYLqSXlovbszGrhAKN7sC1Tp4YWmq"

BASE_FUTURES_URL = "https://fapi.binance.com"

LEVERAGE = 33
BALANCE_USAGE = 0.99          # 99,5% от доступного USDT
ENTRY_TIMEOUT_SEC = 60 * 60   # 1 час
RISK_MIN = 0.005
RISK_MAX = 0.04
TIME_OFFSET_MS = 0

symbols = [
    "AVAX/USDT", "LINK/USDT", "INJ/USDT", "XRP/USDT",  
    "NEAR/USDT", "SUI/USDT", "SOL/USDT", "BNB/USDT" 
] 

timeframe = "15m"
limit = 400

bot = Bot(token=API_TOKEN)
exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_API_SECRET,
    "enableRateLimit": True,
    "timeout": 20000,
    "options": {
        "adjustForTimeDifference": True,
        "defaultType": "future",
    },
})
exchange.load_markets()

last_signal = {}  # чтобы не слать один и тот же сигнал по символу бесконечно


# ================= DATA =================
def get_data(symbol):
    for i in range(3):  # retry 3 раза
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
            return df
        except Exception as e:
            print(f"[{symbol}] fetch error retry {i+1}: {e}")
            time.sleep(2)

    return pd.DataFrame()  # если всё умерло


# ================= INDICATORS =================
def add_indicators(df):
    df["ema50"] = EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema200"] = EMAIndicator(df["close"], window=200).ema_indicator()

    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()

    macd = MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()

    atr = AverageTrueRange(df["high"], df["low"], df["close"], window=14)
    df["atr"] = atr.average_true_range()
    

    df["swing_low"] = (
        (df["low"] < df["low"].shift(1)) &
        (df["low"] < df["low"].shift(2)) &
        (df["low"] < df["low"].shift(-1)) &
        (df["low"] < df["low"].shift(-2))
    )

    df["swing_high"] = (
        (df["high"] > df["high"].shift(1)) &
        (df["high"] > df["high"].shift(2)) &
        (df["high"] > df["high"].shift(-1)) &
        (df["high"] > df["high"].shift(-2))
    )

    df = df.dropna().reset_index(drop=True)
    return df


# ================= HELPERS =================
def get_trend(last):
    if last["ema50"] > last["ema200"]:
        return "UP"
    elif last["ema50"] < last["ema200"]:
        return "DOWN"
    return "SIDEWAYS"


def make_candidate(strategy, signal, score, reasons):
    return {
        "strategy": strategy,
        "signal": signal,
        "score": float(score),
        "reasons": reasons,
    }


# ================= STRATEGY 1: TREND PULLBACK =================
def strategy_trend_pullback(df, last, trend):

    if len(df) < 20:
        return None

    # последние свечи перед сигнальной
    recent = df.iloc[-6:-1]

    recent_atr = recent["atr"].mean()

    body = abs(last["close"] - last["open"])
    candle_range = last["high"] - last["low"]

    # защита от division by zero
    if candle_range == 0:
        return None

    # ================= MARKET HEALTH =================
    # фильтр dead market

    avg_range_pct = (
        (recent["high"] - recent["low"]) / recent["close"]
    ).mean()

    market_active = avg_range_pct > 0.003

    # ================= CLOSE STRENGTH =================

    close_strength_long = (
        (last["close"] - last["low"]) /
        candle_range
    )

    close_strength_short = (
        (last["high"] - last["close"]) /
        candle_range
    )

    # ================= STRONG BODY =================

    strong_body = body >= last["atr"] * 0.45

    # ================= PULLBACK STRUCTURE =================
    # хотим увидеть нормальный откат перед continuation

    prev1 = df.iloc[-3]
    prev2 = df.iloc[-4]

    prev1_body = abs(prev1["close"] - prev1["open"])
    prev2_body = abs(prev2["close"] - prev2["open"])

    min_pullback_body = recent_atr * 0.30

    # нормальные bearish свечи перед BUY
    bearish_pullback = (
        prev1["close"] < prev1["open"]
        and prev2["close"] < prev2["open"]
        and prev1_body > min_pullback_body
        and prev2_body > min_pullback_body
    )

    # нормальные bullish свечи перед SELL
    bullish_pullback = (
        prev1["close"] > prev1["open"]
        and prev2["close"] > prev2["open"]
        and prev1_body > min_pullback_body
        and prev2_body > min_pullback_body
    )

    distance_from_ema = (
    abs(last["close"] - last["ema50"])
    / last["close"]
    )

    near_ema = (
        distance_from_ema < 0.02
    )

    # ================= BUY =================

    if (
        trend == "UP"
        and last["macd"] > last["macd_signal"]
        and last["close"] > last["ema50"]

        # market not dead
        and market_active
        and near_ema

        # pullback structure
        and bearish_pullback

        # continuation confirmation
        and strong_body
        and close_strength_long > 0.65
    ):

        reasons = [
            "Trend UP",
            "Bearish pullback detected",
            "MACD bullish",
            "Close near/above EMA50",
            "Strong continuation candle",
            "Market active",
        ]

        return make_candidate("TREND", "BUY", 1.5, reasons)

    # ================= SELL =================

    if (
        trend == "DOWN"
        and last["macd"] < last["macd_signal"]
        and last["close"] < last["ema50"]

        # market not dead
        and market_active
        and near_ema

        # pullback structure
        and bullish_pullback

        # continuation confirmation
        and strong_body
        and close_strength_short > 0.65
    ):

        reasons = [
            "Trend DOWN",
            "Bullish pullback detected",
            "MACD bearish",
            "Close near/below EMA50",
            "Strong continuation candle",
            "Market active",
        ]

        return make_candidate("TREND", "SELL", 1.5, reasons)

    return None


# ================= STRATEGY 2: BREAKOUT / COMPRESSION BREAKOUT =================
def strategy_breakout_compression(df, last):

    if len(df) < 30:
        return None

    recent = df.iloc[-10:-2]

    if len(recent) < 6:
        return None

    recent_high = recent["high"].max()
    recent_low = recent["low"].min()

    recent_range = recent_high - recent_low
    recent_range_pct = recent_range / last["close"]

    recent_atr = recent["atr"].mean()
    recent_volume = recent["volume"].mean()

    body = abs(last["close"] - last["open"])

    # ================= REAL COMPRESSION =================

    candle_ranges_pct = (
    (recent["high"] - recent["low"]) / recent["close"]
    )


    tight_ranges = (
    (candle_ranges_pct < 0.003).sum() >= 7
    )

    breakout_strength_buy = (
    (last["close"] - recent_high) / last["close"]
    )

    breakout_strength_sell = (
        (recent_low - last["close"]) / last["close"]
    )

    compression = (
        recent_range_pct <= 0.012
        and last["atr"] <= recent_atr * 1.05
        and tight_ranges
    )

    # ================= DEAD MARKET FILTER =================
    # не убиваем compression,
    # просто фильтруем totally dead volatility

    atr_pct = recent_atr / last["close"]

    market_alive = atr_pct > 0.0025

    # ================= DIRECTIONAL PRESSURE =================

    bullish_candles = (
        recent["close"] > recent["open"]
    ).sum()

    bearish_candles = (
        recent["close"] < recent["open"]
    ).sum()

    bullish_pressure = bullish_candles >= 6
    bearish_pressure = bearish_candles >= 6

    # ================= CONFIRMATIONS =================

    volume_ok = last["volume"] >= recent_volume * 1.05


    # ================= BUY =================

    if (
        compression
        and market_alive
        and bullish_pressure
        and last["close"] > recent_high
        and last["close"] > last["ema50"]
        and last["macd"] > last["macd_signal"]
        and last["close"] > last["open"]
        and volume_ok
        and breakout_strength_buy > 0.0015
    ):

        reasons = [
            "Compression detected",
            "Bullish pressure",
            "Break above recent high",
            "Above EMA50",
            "MACD bullish",
            "High volume",
            "Market active",
        ]

        return make_candidate("BREAKOUT", "BUY", 1.4, reasons)

    # ================= SELL =================

    if (
        compression
        and market_alive
        and bearish_pressure
        and last["close"] < recent_low
        and last["close"] < last["ema50"]
        and last["macd"] < last["macd_signal"]
        and last["close"] < last["open"]
        and volume_ok
        and breakout_strength_sell > 0.0015
    ):

        reasons = [
            "Compression detected",
            "Bearish pressure",
            "Break below recent low",
            "Below EMA50",
            "MACD bearish",
            "High volume",
            "Market active",
        ]

        return make_candidate("BREAKOUT", "SELL", 1.4, reasons)

    return None


# ================= STRATEGY 3: EXPANSION VOLATILITY =================
def strategy_expansion_volatility(df, last, trend):

    # Идея:
    # ATR начинает расширяться, свечи становятся крупнее,
    # появляется импульс и ускорение движения.

    if len(df) < 40:
        return None

    recent = df.iloc[-10:-2]

    if len(recent) < 6:
        return None

    atr_avg = recent["atr"].mean()
    vol_avg = recent["volume"].mean()

    body = abs(last["close"] - last["open"])
    candle_range = last["high"] - last["low"]

    if candle_range <= 0:
        return None

    # ================= CORE EXPANSION =================

    atr_expanding = (
        last["atr"] > atr_avg * 1.10
    )

    body_expanding = (
        body >= last["atr"] * 0.70
    )

    range_expanding = (
        candle_range >= atr_avg * 1.20
    )

    volume_spike = (
        last["volume"] > vol_avg * 1.10
    )
    
    atr_pct = atr_avg / last["close"]

    market_alive = atr_pct > 0.0025

    # ================= CLOSE STRENGTH =================

    close_strength_long = (
        (last["close"] - last["low"]) /
        candle_range
    )

    close_strength_short = (
        (last["high"] - last["close"]) /
        candle_range
    )

    # ================= OVEREXTENSION FILTER =================
    # не брать если цена уже слишком улетела

    distance_from_ema = (
        abs(last["close"] - last["ema50"]) /
        last["close"]
    )

    not_overextended = (
        distance_from_ema < 0.018
    )

    # ================= RECENT MOVE FILTER =================
    # не брать после huge move

    recent_move_up = (
        (last["close"] - recent["low"].min()) /
        last["close"]
    )

    recent_move_down = (
        (recent["high"].max() - last["close"]) /
        last["close"]
    )

    fresh_long_move = (
        recent_move_up < 0.028
    )

    fresh_short_move = (
        recent_move_down < 0.028
    )

    # ================= EXPANSION FRESHNESS =================
    # не брать 5 expansion подряд

    recent_expansion_candles = (
        (
            abs(recent["close"] - recent["open"])
            >= recent["atr"] * 0.70
        )
    ).sum()

    fresh_expansion = (
        recent_expansion_candles <= 2
    )

    prev1 = df.iloc[-3]

    prev1_expansion = (
        abs(prev1["close"] - prev1["open"])
        >= prev1["atr"] * 0.70
    )


    # ================= BUY =================

    if (
        atr_expanding
        and trend == "UP"
        and last["close"] > last["ema50"]
        and last["macd"] > last["macd_signal"]
        and last["close"] > last["open"]

        and body_expanding
        and close_strength_long > 0.7
        and volume_spike
        and range_expanding

        # NEW FILTERS
        and not_overextended
        and fresh_long_move
        and fresh_expansion
        and market_alive
        and not prev1_expansion
    ):

        reasons = [
            "ATR expanding",
            "Bullish displacement",
            "Fresh expansion move",
            "Above EMA50",
            "MACD bullish",
        ]

        if volume_spike:
            reasons.append("Volume spike")

        if range_expanding:
            reasons.append("Range expansion")

        return make_candidate(
            "EXPANSION",
            "BUY",
            1.4,
            reasons
        )

    # ================= SELL =================

    if (
        atr_expanding
        and trend == "DOWN"
        and last["close"] < last["ema50"]
        and last["macd"] < last["macd_signal"]
        and last["close"] < last["open"]

        and body_expanding
        and close_strength_short > 0.7
        and range_expanding
        and volume_spike

        # NEW FILTERS
        and not_overextended
        and fresh_short_move
        and fresh_expansion
        and market_alive
        and not prev1_expansion
    ):

        reasons = [
            "ATR expanding",
            "Bearish displacement",
            "Fresh expansion move",
            "Below EMA50",
            "MACD bearish",
        ]

        if volume_spike:
            reasons.append("Volume spike")

        if range_expanding:
            reasons.append("Range expansion")

        return make_candidate(
            "EXPANSION",
            "SELL",
            1.4,
            reasons
        )

    return None


# ================= STRATEGY 4: LIQUIDITY SWEEP / REVERSAL =================
def strategy_liquidity_sweep(df, last, trend):

    if len(df) < 30:
        return None

    recent = df.iloc[-30:-2]

    if len(recent) < 24:
        return None

    atr_avg = recent["atr"].mean()
    vol_avg = recent["volume"].mean()

    swing_lows = recent[recent["swing_low"]]
    swing_highs = recent[recent["swing_high"]]

    if len(swing_lows) == 0 or len(swing_highs) == 0:
        return None

    prev_low = swing_lows["low"].iloc[-1]
    prev_high = swing_highs["high"].iloc[-1]

    tolerance = atr_avg * 0.2

    # ================= STRONG LEVELS =================

    low_touches = (
        abs(swing_lows["low"] - prev_low) <= tolerance
    ).sum()

    high_touches = (
        abs(swing_highs["high"] - prev_high) <= tolerance
    ).sum()

    strong_low_level = low_touches >= 2
    strong_high_level = high_touches >= 2

    # ================= RANGE STRUCTURE =================

    range_size_pct = (
        (prev_high - prev_low) / last["close"]
    )

    valid_range = (
        range_size_pct > 0.01
        and range_size_pct < 0.04
    )

    # ================= MARKET ALIVE =================

    atr_pct = atr_avg / last["close"]

    market_alive = atr_pct > 0.003

    # ================= CANDLE STRUCTURE =================

    body = abs(last["close"] - last["open"])

    upper_wick = (
        last["high"] - max(last["close"], last["open"])
    )

    lower_wick = (
        min(last["close"], last["open"]) - last["low"]
    )

    candle_range = (
        last["high"] - last["low"]
    )

    if candle_range == 0:
        return None

    # ================= PRIMARY FILTERS =================
    # самые важные

    strong_candle = (
        candle_range > atr_avg * 1.3
    )

    lower_wick_pct = (
        lower_wick / candle_range
    )

    upper_wick_pct = (
        upper_wick / candle_range
    )

    # ================= SECONDARY FILTERS =================
    # подтверждения

    strong_body = (
        body >= atr_avg * 0.4
    )

    close_strength_long = (
        (last["close"] - last["low"])
        / candle_range
    )

    close_strength_short = (
        (last["high"] - last["close"])
        / candle_range
    )

    range_low = recent["low"].min()
    range_high = recent["high"].max()
    
    extreme_low = (
    abs(prev_low - range_low)
    <= atr_avg * 0.3
    )

    extreme_high = (
        abs(prev_high - range_high)
        <= atr_avg * 0.3
    )
    
    # ================= BUY SWEEP =================

    sell_side_sweep = (

        # ===== ОБЯЗАТЕЛЬНЫЕ =====

        last["low"] < (prev_low - tolerance)
        and last["close"] > prev_low

        and strong_low_level
        and valid_range

        and strong_candle

        # нижняя тень должна занимать
        # минимум 45% всей свечи

        and lower_wick_pct > 0.45

        # ===== ПОДТВЕРЖДЕНИЯ =====

        and close_strength_long > 0.65
        and strong_body
        and market_alive
        and extreme_low
    )

    # ================= SELL SWEEP =================

    buy_side_sweep = (

        # ===== ОБЯЗАТЕЛЬНЫЕ =====

        last["high"] > (prev_high + tolerance)
        and last["close"] < prev_high

        and strong_high_level
        and valid_range

        and strong_candle

        # верхняя тень должна занимать
        # минимум 45% всей свечи

        and upper_wick_pct > 0.45

        # ===== ПОДТВЕРЖДЕНИЯ =====

        and close_strength_short > 0.65
        and strong_body
        and market_alive
        and extreme_high
    )

    # ================= BUY =================

    if sell_side_sweep:

        reasons = [
            "Sell-side liquidity sweep",
            "Strong support level",
            "Range structure confirmed",
            "Bullish reclaim",
            "Strong rejection candle",
            "Market active",
        ]

        if last["volume"] > vol_avg * 1.05:
            reasons.append("Volume confirmation")

        return make_candidate(
            "LIQUIDITY",
            "BUY",
            1.4 + (
                0.2 if last["volume"] > vol_avg * 1.05
                else 0
            ),
            reasons
        )

    # ================= SELL =================

    if buy_side_sweep:

        reasons = [
            "Buy-side liquidity sweep",
            "Strong resistance level",
            "Range structure confirmed",
            "Bearish rejection",
            "Strong rejection candle",
            "Market active",
        ]

        if last["volume"] > vol_avg * 1.05:
            reasons.append("Volume confirmation")

        return make_candidate(
            "LIQUIDITY",
            "SELL",
            1.4 + (
                0.2 if last["volume"] > vol_avg * 1.05
                else 0
            ),
            reasons
        )

    return None


# ================= LOGIC =================
def analyze(df, symbol):
    last = df.iloc[-2]
    trend = get_trend(last)

    candidates = []

    c1 = strategy_trend_pullback(df, last, trend)
    # c2 = strategy_breakout_compression(df, last)
    c3 = strategy_expansion_volatility(df, last, trend)
    # c4 = strategy_liquidity_sweep(df, last, trend)

    for c in (c1, c3):
        if c is not None:
            candidates.append(c)

    return candidates, last


def get_entry_price(price, atr, signal, strategy_name, last=None):
    name = (strategy_name or "").upper()

    # TREND
    if "TREND" in name:
        return price - atr * 0.6 if signal == "BUY" else price + atr * 0.6

    # EXPANSION
    elif "EXPANSION" in name:

        if last is not None:
            candle_range = last["high"] - last["low"]

            if signal == "BUY":
                return last["high"] - candle_range * 0.6
            else:
                return last["low"] + candle_range * 0.6

        return price

    return price

# ================= RISK =================
def calculate_levels(price, atr, signal, strategy_name):
    # Базовые ATR-множители можно потом подстроить под каждую стратегию отдельно.
    name = (strategy_name or "").upper()

    if "LIQUIDITY" in name:
        stop_mult = 1.0
        tp_mult = 4.0

    elif "BREAKOUT" in name:
        stop_mult = 1.5
        tp_mult = 4.5

    elif "EXPANSION" in name:
        stop_mult = 1.5
        tp_mult = 6.0
    else:
        stop_mult = 1.0 #1 
        tp_mult = 4.0

    if signal == "BUY":
        stop = price - atr * stop_mult
        tp = price + atr * tp_mult
    elif signal == "SELL":
        stop = price + atr * stop_mult
        tp = price - atr * tp_mult
    else:
        return None, None

    return stop, tp


# ================= SEND =================
def send_signal(symbol, signal, price, stop, tp, reasons, strategy_name, total_score):
    message = f"""
📊 {symbol} SIGNAL: {signal}
Strategy: {strategy_name}
Confluence: {total_score:.1f}

Price: {price:.4f}

🛑 Stop: {stop:.4f}
🎯 TP: {tp:.4f}

📌 Reasons:
{chr(10).join(reasons)}
"""
    bot.send_message(chat_id=CHAT_ID, text=message)





# ================= EXECUTION CONFIG =================


TRADE_STATE = {
    "locked": False,
    "symbol": None,
    "signal": None,
    "strategy": None,
    "score": None,
    "reasons": None,
    "entry_order_id": None,
    "entry_order_time": None,
    "entry_price": None,
    "stop_price": None,
    "tp_price": None,
    "amount": None,
    "position_open": False,
    "sl_algo_id": None,
    "tp_algo_id": None,
    "sl_tp_attempted": False,
    "sl_tp_requested": False,
}



def clear_trade_state():
    TRADE_STATE.update({
        "locked": False,
        "symbol": None,
        "signal": None,
        "strategy": None,
        "score": None,
        "reasons": None,
        "entry_order_id": None,
        "entry_order_time": None,
        "entry_price": None,
        "stop_price": None,
        "tp_price": None,
        "amount": None,
        "position_open": False,
        "sl_algo_id": None,
        "tp_algo_id": None,
        "sl_tp_attempted": False,
        "sl_tp_requested": False,
    })


def futures_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace(":USDT", "")


def send_telegram_safe(text: str, chat_id=None):
    chat_id = CHAT_ID if chat_id is None else chat_id

    for attempt in range(3):
        try:
            bot.send_message(chat_id=chat_id, text=text)
            return True

        except Exception as e:
            print(f"[TELEGRAM ERROR attempt {attempt+1}/3] {e}")
            time.sleep(0.8)

    print(f"[TELEGRAM FAIL] message not sent: {text[:50]}")
    return False


def sync_binance_time():
    global TIME_OFFSET_MS
    r = requests.get(f"{BASE_FUTURES_URL}/fapi/v1/time", timeout=10)
    r.raise_for_status()
    server_time = int(r.json()["serverTime"])
    TIME_OFFSET_MS = server_time - int(time.time() * 1000)

def signed_futures_request(method: str, path: str, params: dict | None = None):
    method = method.upper()
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000) + TIME_OFFSET_MS
    params["recvWindow"] = 20000

    query = urlencode(params, doseq=True)
    signature = hmac.new(
        BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    url = f"{BASE_FUTURES_URL}{path}?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

    attempts = 2 if method in ("GET", "DELETE") else 1

    for attempt in range(attempts):
        try:
            resp = requests.request(method, url, headers=headers, timeout=20)
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}

            if resp.status_code >= 400:
                if isinstance(data, dict) and data.get("code") == -1021 and attempt == 0:
                    sync_binance_time()
                    continue
                raise RuntimeError(f"{method} {path} -> {resp.status_code}: {data}")

            return data

        except requests.exceptions.RequestException as e:
            if attempt < attempts - 1:
                time.sleep(0.5)
                continue
            raise e


def get_free_usdt():
    bal = exchange.fetch_balance()

    if isinstance(bal.get("USDT"), dict):
        free = bal["USDT"].get("free")
        if free is not None:
            return float(free)

    return float(bal.get("free", {}).get("USDT", 0) or 0)


def get_min_notional(symbol: str):
    market = exchange.market(symbol)
    cost_min = market.get("limits", {}).get("cost", {}).get("min")
    if cost_min is None:
        return None
    try:
        return float(cost_min)
    except Exception:
        return None


def get_step_size(symbol: str):
    market = exchange.market(symbol)
    for f in market.get("info", {}).get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            return float(f.get("stepSize", 1))
    return 1.0


def get_min_amount(symbol: str):
    market = exchange.market(symbol)
    min_amt = market.get("limits", {}).get("amount", {}).get("min")
    if min_amt is not None:
        try:
            return float(min_amt)
        except Exception:
            return 0.0
    return get_step_size(symbol)


def floor_to_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step) * step


def calc_full_size(symbol: str, entry_price: float):
    free_usdt = get_free_usdt()
    usable_usdt = free_usdt * BALANCE_USAGE
    notional = usable_usdt * LEVERAGE

    min_notional = get_min_notional(symbol)
    min_amount = get_min_amount(symbol)
    step = get_step_size(symbol)

    if min_notional and notional < min_notional:
        return None, "notional too small"

    raw_qty = notional / entry_price

    # 🔥 правильное floor к stepSize (через деление)
    qty = math.floor(raw_qty / step) * step

    # 🔥 защита от float мусора
    qty = float(f"{qty:.8f}")

    # ❌ защита от 0
    if qty <= 0:
        return None, "qty <= 0"

    # ❌ min amount check
    if min_amount and qty < min_amount:
        return None, f"qty {qty} < min_amount {min_amount}"

    return qty, None


def set_leverage(symbol: str, leverage: int = 1):
    return signed_futures_request("POST", "/fapi/v1/leverage", {
        "symbol": futures_symbol(symbol),
        "leverage": leverage,
    })


def place_limit_entry(symbol: str, signal: str, qty: float, entry_price: float):
    side = "BUY" if signal == "BUY" else "SELL"
    client_id = f"entry_{futures_symbol(symbol)}_{int(time.time() * 1000)}"

    return signed_futures_request("POST", "/fapi/v1/order", {
        "symbol": futures_symbol(symbol),
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": entry_price,
        "newClientOrderId": client_id,
    })


def place_sl_tp(symbol: str, signal: str, qty: float, stop_price: float, tp_price: float):
    exit_side = "SELL" if signal == "BUY" else "BUY"

    stop_order = signed_futures_request("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL",
        "symbol": futures_symbol(symbol),
        "side": exit_side,
        "type": "STOP_MARKET",
        "quantity": qty,
        "triggerPrice": stop_price,
        "workingType": "CONTRACT_PRICE",
    })

    tp_order = signed_futures_request("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL",
        "symbol": futures_symbol(symbol),
        "side": exit_side,
        "type": "TAKE_PROFIT_MARKET",
        "quantity": qty,
        "triggerPrice": tp_price,
        "workingType": "CONTRACT_PRICE",
    })

    return stop_order, tp_order


def cancel_entry_order(symbol: str, order_id: int | str):
    return signed_futures_request("DELETE", "/fapi/v1/order", {
        "symbol": futures_symbol(symbol),
        "orderId": order_id,
    })


def cancel_all_symbol_orders(symbol: str):
    try:
        signed_futures_request("DELETE", "/fapi/v1/allOpenOrders", {
            "symbol": futures_symbol(symbol),
        })
    except Exception as e:
        print(f"[{symbol}] cancel all open orders warn: {e}")

    try:
        signed_futures_request("DELETE", "/fapi/v1/algoOpenOrders", {
            "symbol": futures_symbol(symbol),
        })
    except Exception as e:
        print(f"[{symbol}] cancel all algo orders warn: {e}")


def get_position_amt(symbol: str) -> float:
    data = signed_futures_request("GET", "/fapi/v2/positionRisk", {})
    fsym = futures_symbol(symbol)

    for row in data:
        if row.get("symbol") == fsym:
            try:
                return float(row.get("positionAmt", 0) or 0)
            except Exception:
                return 0.0

    return 0.0


def safe_cancel_entry(symbol: str):
    order_id = TRADE_STATE.get("entry_order_id")
    if not order_id:
        return

    try:
        order = signed_futures_request("GET", "/fapi/v1/order", {
            "symbol": futures_symbol(symbol),
            "orderId": order_id,
        })

        status = (order.get("status") or "").upper()

        if status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            TRADE_STATE["entry_order_id"] = None
            TRADE_STATE["entry_order_time"] = None
            return

        if status in ("NEW", "PARTIALLY_FILLED"):
            try:
                cancel_entry_order(symbol, order_id)
            except Exception as e:
                if "Unknown order sent" in str(e):
                    TRADE_STATE["entry_order_id"] = None
                    TRADE_STATE["entry_order_time"] = None
                else:
                    raise e

    except Exception as e:
        print(f"[{symbol}] safe cancel error: {e}")


def verify_sl_tp(symbol: str):
    try:
        orders = signed_futures_request("GET", "/fapi/v1/openAlgoOrders", {
            "symbol": futures_symbol(symbol)
        })
    except Exception as e:
        print(f"[{symbol}] verify_sl_tp error: {e}")
        return False, False

    sl = False
    tp = False

    for o in orders or []:
        t = o.get("type")
        if t == "STOP_MARKET":
            sl = True
        elif t == "TAKE_PROFIT_MARKET":
            tp = True

    return sl, tp


def attach_sl_tp(symbol, signal, qty, stop_price, tp_price) -> bool:
    try:
        sl_ok, tp_ok = verify_sl_tp(symbol)
        if sl_ok and tp_ok:
            return True

        place_sl_tp(
            symbol=symbol,
            signal=signal,
            qty=qty,
            stop_price=stop_price,
            tp_price=tp_price
        )

        sl_ok, tp_ok = verify_sl_tp(symbol)
        return sl_ok and tp_ok

    except Exception as e:
        print(f"[{symbol}] SL/TP attach error: {e}")
        return False


def cleanup_sl_tp(symbol):
    try:
        cancel_all_symbol_orders(symbol)
    except Exception as e:
        print(f"[{symbol}] cleanup error: {e}")

    TRADE_STATE["sl_algo_id"] = None
    TRADE_STATE["tp_algo_id"] = None

def manage_active_trade():
    try:
        symbol = TRADE_STATE.get("symbol")
        if not symbol:
            return

        try:
            pos_amt = abs(get_position_amt(symbol))
        except Exception as e:
            print(f"[{symbol}] position check error: {e}")
            return

        # sync with Binance
        if pos_amt > 0:
            TRADE_STATE["position_open"] = True
            TRADE_STATE["locked"] = True

        if not TRADE_STATE["locked"]:
            return

        # 1) position is open
        if pos_amt > 0:
            # send open message only once
            if not TRADE_STATE["sl_tp_requested"]:
                TRADE_STATE["sl_tp_requested"] = True

                safe_cancel_entry(symbol)
                ok = attach_sl_tp(
                    symbol=symbol,
                    signal=TRADE_STATE["signal"],
                    qty=pos_amt,
                    stop_price=float(exchange.price_to_precision(symbol, TRADE_STATE["stop_price"])),
                    tp_price=float(exchange.price_to_precision(symbol, TRADE_STATE["tp_price"]))
                )
                if ok:
                    send_telegram_safe(f"✅ {symbol} SL/TP attached")
                else:
                    print(f"[{symbol}] SL/TP failed once")
                    send_telegram_safe(f"✅ {symbol} SL/TP attached")
            return
 
           

        # 2) entry order still pending
        if TRADE_STATE.get("entry_order_id"):
            elapsed = time.time() - TRADE_STATE.get("entry_order_time", time.time())

            if elapsed >= ENTRY_TIMEOUT_SEC:
                safe_cancel_entry(symbol)
                send_telegram_safe(f"⏱ {symbol} entry timeout cancel")
                clear_trade_state()
                return

            try:
                order = signed_futures_request("GET", "/fapi/v1/order", {
                    "symbol": futures_symbol(symbol),
                    "orderId": TRADE_STATE["entry_order_id"],
                })

                status = (order.get("status") or "").upper()

                if status in ("CANCELED", "REJECTED", "EXPIRED"):
                    safe_cancel_entry(symbol)
                    send_telegram_safe(f"⚠️ {symbol} entry finished: {status}")
                    clear_trade_state()
                    return

            except Exception as e:
                print(f"[{symbol}] entry check error: {e}")

        # 3) position closed
        if pos_amt == 0 and TRADE_STATE["position_open"]:
            TRADE_STATE["position_open"] = False

            cleanup_sl_tp(symbol)
            send_telegram_safe(f"🧹 {symbol} closed → all orders removed")

            clear_trade_state()
            return

    except Exception as e:
        print(f"[MANAGE_ACTIVE_TRADE CRASH] {e}")


def try_execute_candidate(symbol, c, last):
    if TRADE_STATE["locked"]:
        return False

    signal = c["signal"]
    strategy_name = c["strategy"]
    total_score = c["score"]
    reasons = c["reasons"]

    price = float(last["close"])

    entry_price = get_entry_price(
        price,
        float(last["atr"]),
        signal,
        strategy_name,
        last=last
    )
    entry_price = float(exchange.price_to_precision(symbol, entry_price))

    stop, tp = calculate_levels(
        entry_price,
        float(last["atr"]),
        signal,
        strategy_name
    )
    stop = float(exchange.price_to_precision(symbol, stop))
    tp = float(exchange.price_to_precision(symbol, tp))

    risk_pct = abs(entry_price - stop) / entry_price
    if risk_pct < RISK_MIN or risk_pct > RISK_MAX:
        print(f"[{symbol}] skip risk_pct={risk_pct:.4f}")
        return False

    qty, reason = calc_full_size(symbol, entry_price)
    if qty is None:
        print(f"[{symbol}] skip sizing: {reason}")
        return False

    try:
        set_leverage(symbol, LEVERAGE)
    except Exception as e:
        print(f"[{symbol}] leverage error: {e}")
        return False

    side = "BUY" if signal == "BUY" else "SELL"

    try:
        entry_order = place_limit_entry(symbol, signal, qty, entry_price)
    except Exception as e:
        print(f"[{symbol}] entry order error: {e}")
        return False

    TRADE_STATE.update({
        "locked": True,
        "symbol": symbol,
        "signal": signal,
        "strategy": strategy_name,
        "score": total_score,
        "reasons": reasons,
        "entry_order_id": entry_order.get("orderId"),
        "entry_order_time": time.time(),
        "entry_price": entry_price,
        "stop_price": stop,
        "tp_price": tp,
        "amount": qty,
        "position_open": False,
        "sl_algo_id": None,
        "tp_algo_id": None,
    })

    send_telegram_safe(
        chat_id=CHAT_ID,
        text=(
            f"📊 {symbol} SIGNAL: {signal}\n"
            f"Strategy: {strategy_name}\n"
            f"Confluence: {total_score:.1f}\n\n"
            f"Mode: LIVE LIMIT ENTRY | 1x\n"
            f"Balance use: {BALANCE_USAGE*100:.0f}%\n\n"
            f"Entry: {entry_price:.4f}\n"
            f"Stop: {stop:.4f}\n"
            f"TP: {tp:.4f}\n"
            f"Qty: {qty}\n\n"
            f"Reasons:\n{chr(10).join(reasons)}\n\n"
            f"Entry order id: {TRADE_STATE['entry_order_id']}"
        )
    )

    print(f"ENTRY placed: {symbol} {signal} | {strategy_name} | qty={qty}")
    return True




# ================= MAIN SCAN =================
def run():
    # если уже есть активный pending order / position, новые сигналы игнорируем
    if TRADE_STATE["locked"]:
        return

    for symbol in symbols:
        time.sleep(2)

        # ещё раз: если уже что-то открылось по более приоритетному символу
        if TRADE_STATE["locked"]:
            return

        df = get_data(symbol)

        if df is None or len(df) < 50:
            print(f"{symbol}: skipped (no data)")
            continue

        df = add_indicators(df)
        candidates, last = analyze(df, symbol)

        if not candidates:
            print(f"No signal: {symbol}")
            continue

        # сначала TREND, потом EXPANSION (потому что analyze() уже возвращает их в этом порядке)
        for c in candidates:
            signal_key = f"{symbol}_{c['strategy']}_{c['signal']}"
            candle_time = last["time"]

            if last_signal.get(signal_key) == candle_time:
                continue

            placed = try_execute_candidate(symbol, c, last)
            if placed:
                last_signal[signal_key] = candle_time
                return  # одна сделка за раз, остальные сигналы игнорируем

# синхронизация времени Binance (ВАЖНО)
sync_binance_time()

# ================= LOOP =================
while True:
    try:
        manage_active_trade()

        now = datetime.datetime.now(datetime.timezone.utc)

        # анализируем сразу после закрытия 15m-свечи
        if now.minute in [1, 16, 31, 46]:
            if not TRADE_STATE["locked"]:
                print(f"\n=== RUNNING ANALYSIS {now} ===")
                run()
                time.sleep(60)
            else:
                time.sleep(5)
        else:
            time.sleep(5)

    except Exception as e:
        print("Error:", e)
        time.sleep(5)
