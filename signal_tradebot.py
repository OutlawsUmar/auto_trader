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
    "BTC/USDT", "SOL/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT",
    "AVAX/USDT", "LINK/USDT", "SUI/USDT", "NEAR/USDT"
]

timeframe = "15m"
limit = 220

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

    df = df.dropna()
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
    # Классический pullback continuation:
    # тренд уже есть, цена откатила, но momentum еще не сломан.
    if (
        trend == "UP"
        and last["rsi"] < 45
        and last["macd"] > last["macd_signal"]
        and last["close"] > last["ema50"] * 0.997
    ):
        reasons = [
            "Trend UP",
            "RSI pullback",
            "MACD bullish",
            "Close near/above EMA50",
        ]
        return make_candidate("TREND", "BUY", 1.5, reasons)

    if (
        trend == "DOWN"
        and last["rsi"] > 55
        and last["macd"] < last["macd_signal"]
        and last["close"] < last["ema50"] * 1.003
    ):
        reasons = [
            "Trend DOWN",
            "RSI pullback",
            "MACD bearish",
            "Close near/below EMA50",
        ]
        return make_candidate("TREND", "SELL", 1.5, reasons)

    return None


# ================= STRATEGY 2: BREAKOUT / COMPRESSION BREAKOUT =================
def strategy_breakout_compression(df, last):
    # Идея:
    # рынок сжимается -> пробивает локальный high/low -> начинается импульс.
    if len(df) < 30:
        return None

    recent = df.iloc[-12:-2]  # последние закрытые свечи до текущей закрытой
    if len(recent) < 8:
        return None

    recent_high = recent["high"].max()
    recent_low = recent["low"].min()

    recent_range = recent_high - recent_low
    recent_range_pct = recent_range / last["close"]

    recent_atr = recent["atr"].mean()
    recent_volume = recent["volume"].mean()
    body = abs(last["close"] - last["open"])

    compression = recent_range_pct <= 0.020 and last["atr"] <= recent_atr * 1.05
    volume_ok = last["volume"] >= recent_volume * 1.05
    strong_body = body >= last["atr"] * 0.35

    if (
        compression
        and last["close"] > recent_high
        and last["close"] > last["ema50"]
        and last["macd"] > last["macd_signal"]
        and last["close"] > last["open"]
    ):
        reasons = [
            "Compression detected",
            "Break above recent high",
            "Above EMA50",
            "MACD bullish",
        ]
        if volume_ok:
            reasons.append("High volume")
        if strong_body:
            reasons.append("Strong candle body")
        return make_candidate("BREAKOUT", "BUY", 1.0 + (0.2 if volume_ok else 0) + (0.2 if strong_body else 0), reasons)

    if (
        compression
        and last["close"] < recent_low
        and last["close"] < last["ema50"]
        and last["macd"] < last["macd_signal"]
        and last["close"] < last["open"]
    ):
        reasons = [
            "Compression detected",
            "Break below recent low",
            "Below EMA50",
            "MACD bearish",
        ]
        if volume_ok:
            reasons.append("High volume")
        if strong_body:
            reasons.append("Strong candle body")
        return make_candidate("BREAKOUT", "SELL", 1.0 + (0.2 if volume_ok else 0) + (0.2 if strong_body else 0), reasons)

    return None


# ================= STRATEGY 3: EXPANSION VOLATILITY =================
def strategy_expansion_volatility(df, last, trend):
    # Идея:
    # ATR начинает расширяться, свечи становятся крупнее,
    # появляется импульс и ускорение движения.
    if len(df) < 40:
        return None

    recent = df.iloc[-20:-2]
    if len(recent) < 10:
        return None

    atr_avg = recent["atr"].mean()
    vol_avg = recent["volume"].mean()

    body = abs(last["close"] - last["open"])
    candle_range = last["high"] - last["low"]

    atr_expanding = last["atr"] > atr_avg * 1.10
    body_expanding = body >= last["atr"] * 0.55
    range_expanding = candle_range >= atr_avg * 1.20
    volume_spike = last["volume"] > vol_avg * 1.10

    if (
        trend == "UP"
        and last["close"] > last["ema50"]
        and last["macd"] > last["macd_signal"]
        and last["close"] > last["open"]
        and atr_expanding
        and body_expanding
        and volume_spike
        and range_expanding
    ):
        reasons = [
            "ATR expanding",
            "Bullish displacement",
            "Above EMA50",
            "MACD bullish",
        ]
        if volume_spike:
            reasons.append("Volume spike")
        if range_expanding:
            reasons.append("Range expansion")
        return make_candidate("EXPANSION", "BUY", 1.0 + (0.2 if volume_spike else 0) + (0.2 if range_expanding else 0), reasons)

    if (
        trend == "DOWN"
        and last["close"] < last["ema50"]
        and last["macd"] < last["macd_signal"]
        and last["close"] < last["open"]
        and atr_expanding
        and body_expanding
        and volume_spike
        and range_expanding
    ):
        reasons = [
            "ATR expanding",
            "Bearish displacement",
            "Below EMA50",
            "MACD bearish",
        ]
        if volume_spike:
            reasons.append("Volume spike")
        if range_expanding:
            reasons.append("Range expansion")
        return make_candidate("EXPANSION", "SELL", 1.0 + (0.2 if volume_spike else 0) + (0.2 if range_expanding else 0), reasons)

    return None


# ================= STRATEGY 4: LIQUIDITY SWEEP / REVERSAL =================
def strategy_liquidity_sweep(df, last, trend):
    # Идея:
    # рынок снимает очевидные стопы за локальным high/low,
    # потом возвращается обратно — это часто reversal / trap move.
    if len(df) < 30:
        return None

    recent = df.iloc[-12:-2]
    if len(recent) < 8:
        return None

    prev_high = recent["high"].max()
    prev_low = recent["low"].min()
    vol_avg = recent["volume"].mean()

    body = abs(last["close"] - last["open"])
    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]

    sell_side_sweep = (
        
        last["low"] < prev_low
        and last["close"] > prev_low
        and last["close"] > last["open"]
        and last["rsi"] < 45
        and lower_wick > body * 0.8
        and trend != "DOWN"
    )

    buy_side_sweep = (
        last["high"] > prev_high
        and last["close"] < prev_high
        and last["close"] < last["open"]
        and last["rsi"] > 55
        and upper_wick > body * 0.8
        and trend != "UP"
    )

    if sell_side_sweep:
        reasons = [
            "Sell-side liquidity sweep",
            "Reclaim above prior low",
            "Bullish rejection",
            "RSI exhausted",
        ]
        if last["volume"] > vol_avg * 1.05:
            reasons.append("Volume confirmation")
        return make_candidate("LIQUIDITY", "BUY", 1.0 + (0.2 if last["volume"] > vol_avg * 1.05 else 0), reasons)

    if buy_side_sweep:
        reasons = [
            "Buy-side liquidity sweep",
            "Reject below prior high",
            "Bearish rejection",
            "RSI exhausted",
        ]
        if last["volume"] > vol_avg * 1.05:
            reasons.append("Volume confirmation")
        return make_candidate("LIQUIDITY", "SELL", 1.0 + (0.2 if last["volume"] > vol_avg * 1.05 else 0), reasons)

    return None


# ================= LOGIC =================
def analyze(df, symbol):
    last = df.iloc[-2]  # только закрытая свеча
    trend = get_trend(last)

    candidates = []

    # Собираем сигналы со всех стилей
    c1 = strategy_trend_pullback(df, last, trend)
    c2 = strategy_breakout_compression(df, last)
    c3 = strategy_expansion_volatility(df, last, trend)
    c4 = strategy_liquidity_sweep(df, last, trend)

    for c in [c1, c2, c3, c4]:
        if c is not None:
            candidates.append(c)

    if not candidates:
        return None, [], last, None, 0.0

    buys = [c for c in candidates if c["signal"] == "BUY"]
    sells = [c for c in candidates if c["signal"] == "SELL"]

    buy_score = sum(c["score"] for c in buys)
    sell_score = sum(c["score"] for c in sells)

    # Если BUY и SELL почти равны — не лезем в кашу
    if buys and sells and abs(buy_score - sell_score) < 0.25:
        return None, [f"Conflict filtered: BUY={buy_score:.1f} vs SELL={sell_score:.1f}"], last, None, 0.0

    # Выбираем сторону с большим суммарным весом
    if buy_score > sell_score:
        selected = buys
        signal = "BUY"
        total_score = buy_score
    else:
        selected = sells
        signal = "SELL"
        total_score = sell_score

    # Названия стратегий, которые подтвердили сигнал
    strategy_name = "+".join(sorted(set(c["strategy"] for c in selected)))

    # Склеиваем причины выбранной стороны
    reasons = []
    for c in selected:
        for r in c["reasons"]:
            reasons.append(f'[{c["strategy"]}] {r}')

    # Убираем повторы, сохраняя порядок
    reasons = list(dict.fromkeys(reasons))

    return signal, reasons, last, strategy_name, total_score


# ================= RISK =================
def calculate_levels(price, atr, signal, strategy_name):
    # Базовые ATR-множители можно потом подстроить под каждую стратегию отдельно.
    name = (strategy_name or "").upper()

    if "LIQUIDITY" in name:
        stop_mult = 1.8
        tp_mult = 3.6
    elif "BREAKOUT" in name and "EXPANSION" in name:
        stop_mult = 2.0
        tp_mult = 5.0
    elif "BREAKOUT" in name:
        stop_mult = 1.8
        tp_mult = 4.5
    elif "EXPANSION" in name:
        stop_mult = 2.0
        tp_mult = 5.0
    else:
        stop_mult = 2.0
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


# ================= MAIN =================
def run():
    global last_signal

    for symbol in symbols:
        time.sleep(2)  # 👈 пауза между запросами к Binance

        df = get_data(symbol)

        if df is None or len(df) < 50:
            print(f"{symbol}: skipped (no data)")
            continue

        df = add_indicators(df)

        signal, reasons, last, strategy_name, total_score = analyze(df, symbol)

        if signal:
            # Оставил старую логику анти-дубля по символу и направлению
            if last_signal.get(symbol) != signal:
                price = last["close"]
                stop, tp = calculate_levels(price, last["atr"], signal, strategy_name)

                send_signal(symbol, signal, price, stop, tp, reasons, strategy_name, total_score)
                print(f"Signal sent: {symbol} {signal} | Strategy: {strategy_name} | Score: {total_score:.1f}")

                last_signal[symbol] = signal
        else:
            if reasons and reasons[0].startswith("Conflict filtered"):
                print(f"{symbol}: {reasons[0]}")
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
