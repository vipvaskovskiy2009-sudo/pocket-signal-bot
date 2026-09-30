import os
import time
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ================= НАСТРОЙКИ =================
TELEGRAM_BOT_TOKEN = "ВАШ_ТОКЕН_БОТА"  # Укажите токен
TELEGRAM_CHAT_ID = "ВАШ_CHAT_ID"       # Укажите Ваш ID чата
SYMBOL = "EURUSD=X"                    # Валютная пара
INTERVAL = "5m"                        # Таймфрейм
CHECK_INTERVAL = 300                   # Проверка раз в 5 минут
# =============================================


def send_telegram_message(message: str):
    """Отправка сообщения в Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")


def calculate_rsi(series, period=14):
    """Расчет RSI."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_stochastic(df, k_period=14, d_period=3):
    """Расчет Stochastic."""
    low_min = df['Low'].rolling(window=k_period).min()
    high_max = df['High'].rolling(window=k_period).max()
    stoch_k = 100 * ((df['Close'] - low_min) / (high_max - low_min))
    stoch_d = stoch_k.rolling(window=d_period).mean()
    return stoch_k, stoch_d


def calculate_supertrend(df, period=10, multiplier=3):
    """Расчет SuperTrend."""
    high = df['High']
    low = df['Low']
    close = df['Close']

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()

    hl2 = (high + low) / 2
    basic_upperband = hl2 + (multiplier * atr)
    basic_lowerband = hl2 - (multiplier * atr)

    upperband = basic_upperband.copy()
    lowerband = basic_lowerband.copy()

    for i in range(1, len(df)):
        if basic_upperband.iloc[i] < upperband.iloc[i-1] or close.iloc[i-1] > upperband.iloc[i-1]:
            upperband.iloc[i] = basic_upperband.iloc[i]
        else:
            upperband.iloc[i] = upperband.iloc[i-1]

        if basic_lowerband.iloc[i] > lowerband.iloc[i-1] or close.iloc[i-1] < lowerband.iloc[i-1]:
            lowerband.iloc[i] = basic_lowerband.iloc[i]
        else:
            lowerband.iloc[i] = lowerband.iloc[i-1]

    direction = np.ones(len(df))
    for i in range(1, len(df)):
        if direction[i-1] == 1:
            direction[i] = -1 if close.iloc[i] < lowerband.iloc[i] else 1
        else:
            direction[i] = 1 if close.iloc[i] > upperband.iloc[i] else -1

    return direction


def analyze_market():
    """Анализ торговой стратегии."""
    try:
        df = yf.download(tickers=SYMBOL, period="2d", interval=INTERVAL, progress=False)

        if df.empty or len(df) < 200:
            print("Недостаточно свечей для анализа.")
            return

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Индикаторы
        df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()
        df['RSI'] = calculate_rsi(df['Close'], 14)
        df['STOCHk'], df['STOCHd'] = calculate_stochastic(df, 14, 3)
        df['ST_DIR'] = calculate_supertrend(df, 10, 3)

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        # Сигнал ВВЕРХ
        call_signal = (
            curr['Close'] > curr['EMA200'] and
            curr['ST_DIR'] == 1 and
            40 < curr['RSI'] < 70 and
            prev['STOCHk'] < prev['STOCHd'] and curr['STOCHk'] > curr['STOCHd'] and
            curr['STOCHk'] < 50
        )

        # Сигнал ВНИЗ
        put_signal = (
            curr['Close'] < curr['EMA200'] and
            curr['ST_DIR'] == -1 and
            30 < curr['RSI'] < 60 and
            prev['STOCHk'] > prev['STOCHd'] and curr['STOCHk'] < curr['STOCHd'] and
            curr['STOCHk'] > 50
        )

        if call_signal:
            msg = f"🚀 *СИГНАЛ: ВВЕРХ (CALL)*\n\n📊 Пара: {SYMBOL}\n⏱ Таймфрейм: {INTERVAL}\n💡 Экспирация: 10-15 мин\n📈 Цена: {curr['Close']:.5f}"
            send_telegram_message(msg)
            print(f"[{pd.Timestamp.now()}] Отправлен сигнал CALL")
        elif put_signal:
            msg = f"🔻 *СИГНАЛ: ВНИЗ (PUT)*\n\n📊 Пара: {SYMBOL}\n⏱ Таймфрейм: {INTERVAL}\n💡 Экспирация: 10-15 мин\n📉 Цена: {curr['Close']:.5f}"
            send_telegram_message(msg)
            print(f"[{pd.Timestamp.now()}] Отправлен сигнал PUT")
        else:
            print(f"[{pd.Timestamp.now()}] Проверка успешна. Сигналов нет.")

    except Exception as e:
        print(f"Ошибка во время анализа: {e}")


if __name__ == "__main__":
    send_telegram_message("🤖 *Бот успешно запущен и анализирует рынок!*")
    while True:
        analyze_market()
        time.sleep(CHECK_INTERVAL)



