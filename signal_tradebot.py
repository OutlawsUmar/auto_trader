import ccxt
import pandas as pd
from telegram import Bot
import time
import datetime

from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# ================= CONFIG =================
API_TOKEN = "8764116821:AAEPAwJq5hy3bAUD7VSdgz7juwfz2i2_kD4"
CHAT_ID = "-1003978043796"

symbols = [
    "SOL/USDT", "BNB/USDT", "XRP/USDT", "AVAX/USDT", "LINK/USDT",
    "SUI/USDT", "NEAR/USDT", "INJ/USDT", "BTC/USDT"
]   

timeframe = "15m"
limit = 400

bot = Bot(token=API_TOKEN)
exchange = ccxt.binance({
    "enableRateLimit": True,
    "timeout": 20000,
    "options": {
        "adjustForTimeDifference": True,
    },
})

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
    c2 = strategy_breakout_compression(df, last)
    c3 = strategy_expansion_volatility(df, last, trend)
    c4 = strategy_liquidity_sweep(df, last, trend)

    for c in [c1, c2, c3, c4]:
        if c is not None:
            candidates.append(c)

    return candidates, last


def get_entry_price(price, atr, signal, strategy_name):

    if "TREND" in strategy_name:

        if signal == "BUY":
            return price - atr * 0.2
        else:
            return price + atr * 0.2

    elif "LIQUIDITY" in strategy_name:

        if signal == "BUY":
            return price - atr * 0.3
        else:
            return price + atr * 0.3

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
        stop_mult = 1.0
        tp_mult = 3.0

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


# ================= MAIN =================
def run():
    for symbol in symbols:
        time.sleep(2)

        df = get_data(symbol)

        if df is None or len(df) < 50:
            print(f"{symbol}: skipped (no data)")
            continue

        df = add_indicators(df)

        candidates, last = analyze(df, symbol)

        if candidates:
            for c in candidates:

                signal = c["signal"]
                strategy_name = c["strategy"]
                total_score = c["score"]
                reasons = c["reasons"]

                signal_key = f"{symbol}_{strategy_name}_{signal}"

                current_candle_time = last["time"]

                if last_signal.get(signal_key) == current_candle_time:
                    continue

                last_signal[signal_key] = current_candle_time

                price = last["close"]

                entry_price = get_entry_price(
                    price,
                    last["atr"],
                    signal,
                    strategy_name
                )

                stop, tp = calculate_levels(
                    entry_price,
                    last["atr"],
                    signal,
                    strategy_name
                )

                risk_pct = abs(entry_price - stop) / entry_price

                if risk_pct < 0.0075 or risk_pct > 0.015:
                    continue

                send_signal(
                    symbol,
                    signal,
                    entry_price,
                    stop,
                    tp,
                    reasons,
                    strategy_name,
                    total_score
                )

                print(
                    f"Signal sent: {symbol} "
                    f"{signal} | {strategy_name} "
                    f"| Score: {total_score:.1f}"
                )

        else:
            print(f"No signal: {symbol}")


# ================= LOOP =================
while True:
    try:
        now = datetime.datetime.now(datetime.timezone.utc)

        # Анализируем сразу после закрытия 15m-свечи
        # UTC минуты: 01, 16, 31, 46
        if now.minute in [1, 16, 31, 46]:
            print(f"\n=== RUNNING ANALYSIS {now} ===")
            run()

            # защита от повторного запуска в ту же минуту
            time.sleep(60)
        else:
            # лёгкая проверка времени, нагрузки почти нет
            time.sleep(5)

    except Exception as e:
        print("Error:", e)
        time.sleep(60)
