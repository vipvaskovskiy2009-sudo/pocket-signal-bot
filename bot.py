import os
import time
import requests
import pandas as pd
import pandas_ta as ta
import yfinance as yf

# ================= НАСТРОЙКИ =================
TELEGRAM_BOT_TOKEN = "ВАШ_ТОКЕН_БОТА"  # Вставьте токен от @BotFather
TELEGRAM_CHAT_ID = "ВАШ_CHAT_ID"       # Вставьте ваш ID чата или канала
SYMBOL = "EURUSD=X"                    # Валютная пара (EUR/USD)
INTERVAL = "5m"                        # Таймфрейм свечей
CHECK_INTERVAL = 300                   # Пауза между проверками (в секундах, 300 сек = 5 мин)
# =============================================


def send_telegram_message(message: str):
    """Отправка сообщения в Telegram-чат."""
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


def analyze_market():
    """Анализ рынка по стратегии Trend-Momentum."""
    try:
        # Скачиваем свечи
        df = yf.download(tickers=SYMBOL, period="2d", interval=INTERVAL, progress=False)
        
        if df.empty or len(df) < 200:
            print("Недостаточно данных для анализа.")
            return

        # Обработка структуры MultiIndex, если yfinance возвращает её
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # 1. Moving Average (EMA 200)
        df['EMA200'] = ta.ema(df['Close'], length=200)

        # 2. RSI (14)
        df['RSI'] = ta.rsi(df['Close'], length=14)

        # 3. Stochastic Oscillator (14, 3, 3)
        stoch = ta.stoch(df['High'], df['Low'], df['Close'], k=14, d=3, smooth_k=3)
        df['STOCHk'] = stoch['STOCHk_14_3_3']
        df['STOCHd'] = stoch['STOCHd_14_3_3']

        # 4. SuperTrend (10, 3)
        st = ta.supertrend(df['High'], df['Low'], df['Close'], length=10, multiplier=3)
        df['ST_DIR'] = st['SUPERTd_10_3.0']

        # Берем свежие свечи
        curr = df.iloc[-1]
        prev = df.iloc[-2]

        # Проверка условий CALL (ВВЕРХ)
        call_signal = (
            curr['Close'] > curr['EMA200'] and
            curr['ST_DIR'] == 1 and
            40 < curr['RSI'] < 70 and
            prev['STOCHk'] < prev['STOCHd'] and curr['STOCHk'] > curr['STOCHd'] and
            curr['STOCHk'] < 50
        )

        # Проверка условий PUT (ВНИЗ)
        put_signal = (
            curr['Close'] < curr['EMA200'] and
            curr['ST_DIR'] == -1 and
            30 < curr['RSI'] < 60 and
            prev['STOCHk'] > prev['STOCHd'] and curr['STOCHk'] < curr['STOCHd'] and
            curr['STOCHk'] > 50
        )

        # Отправка уведомления при наличии сигнала
        if call_signal:
            msg = (
                f"🚀 *СИГНАЛ: ВВЕРХ (CALL)*\n\n"
                f"📊 *Инструмент:* {SYMBOL}\n"
                f"⏱ *Таймфрейм:* {INTERVAL}\n"
                f"💡 *Экспирация:* 2-3 свечи (10-15 мин)\n"
                f"📈 *Цена:* {curr['Close']:.5f}"
            )
            print(f"[{pd.Timestamp.now()}] Отправлен сигнал CALL")
            send_telegram_message(msg)

        elif put_signal:
            msg = (
                f"🔻 *СИГНАЛ: ВНИЗ (PUT)*\n\n"
                f"📊 *Инструмент:* {SYMBOL}\n"
                f"⏱ *Таймфрейм:* {INTERVAL}\n"
                f"💡 *Экспирация:* 2-3 свечи (10-15 мин)\n"
                f"📉 *Цена:* {curr['Close']:.5f}"
            )
            print(f"[{pd.Timestamp.now()}] Отправлен сигнал PUT")
            send_telegram_message(msg)
        else:
            print(f"[{pd.Timestamp.now()}] Анализ выполнен. Сигналов нет.")

    except Exception as e:
        print(f"Ошибка во время анализа: {e}")


if __name__ == "__main__":
    send_telegram_message("🤖 *Бот успешно запущен и отслеживает сигналы!*")
    while True:
        analyze_market()
        time.sleep(CHECK_INTERVAL)

