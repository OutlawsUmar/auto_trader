import ccxt
import pandas as pd
from telegram import Bot
import time

from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# ================= CONFIG =================
API_TOKEN = "8764116821:AAEPAwJq5hy3bAUD7VSdgz7juwfz2i2_kD4"
CHAT_ID = "-1003978043796"

symbols = ["SOL/USDT", "BTC/USDT", "ETH/USDT", "BNB/USDT", "LTC/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT"]
timeframe = "15m"
limit = 250

bot = Bot(token=API_TOKEN)
exchange = ccxt.binance()

last_signal = {}  # 👈 для убирания дублей 

# ================= DATA =================
def get_data(symbol):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","volume"])
    return df

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

# ================= LOGIC =================
def analyze(df):
    last = df.iloc[-1]

    signal = None
    reason = []

    # 1. Trend (как ты сказал)
    if last["ema50"] > last["ema200"]:
        trend = "UP"
    elif last["ema50"] < last["ema200"]:
        trend = "DOWN"
    else:
        trend = "SIDEWAYS"

    # 2. RSI
    if last["rsi"] < 40:
        reason.append("RSI oversold")
    if last["rsi"] > 60:
        reason.append("RSI overbought")

    # 3. MACD
    if last["macd"] > last["macd_signal"]:
        reason.append("MACD bullish")
    elif last["macd"] < last["macd_signal"]:
        reason.append("MACD bearish")
    else:
        reason.append("MACD neutral")

    # 4. Volume
    if last["volume"] > df["volume"].mean():
        reason.append("High volume")

    # 5. ATR filter
    if last["atr"] < df["atr"].mean():
        reason.append("Low volatility")

    # ===== ENTRY LOGIC =====
    if trend == "UP" and last["rsi"] < 40 and last["macd"] > last["macd_signal"]:
        signal = "BUY"

    elif trend == "DOWN" and last["rsi"] > 60 and last["macd"] < last["macd_signal"]:
        signal = "SELL"

    return signal, reason, last

# ================= RISK =================
def calculate_levels(price, atr, signal):
    if signal == "BUY":
        stop = price - atr * 1.5
        tp = price + atr * 3
    elif signal == "SELL":
        stop = price + atr * 1.5
        tp = price - atr * 3
    else:
        return None, None

    return stop, tp

# ================= SEND =================
def send_signal(symbol, signal, price, stop, tp, reasons):
    message = f"""
📊 {symbol} SIGNAL: {signal}
Price: {price:.2f}

🛑 Stop: {stop:.2f}
🎯 TP: {tp:.2f}

📌 Reasons:
{", ".join(reasons)}
"""
    bot.send_message(chat_id=CHAT_ID, text=message)

# ================= MAIN =================
def run():
    global last_signal

    for symbol in symbols:
        df = get_data(symbol)
        df = add_indicators(df)

        signal, reasons, last = analyze(df)

        if signal:
            if last_signal.get(symbol) != signal:
                price = last["close"]
                stop, tp = calculate_levels(price, last["atr"], signal)

                send_signal(symbol, signal, price, stop, tp, reasons)
                print(f"Signal sent: {symbol} {signal}")

                last_signal[symbol] = signal
        else:
            print(f"No signal: {symbol}")

# ================= LOOP =================
while True:
    try:
        run()
        time.sleep(900)  # 15 минут
    except Exception as e:
        print("Error:", e)
        time.sleep(60)
