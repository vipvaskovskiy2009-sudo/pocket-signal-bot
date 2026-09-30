import yfinance as yf
import pandas as pd
import pandas_ta as ta

def get_trading_signal(symbol="EURUSD=X", interval="5m"):
    # Загрузка свечных данных
    data = yf.download(tickers=symbol, period="1d", interval=interval)
    if data.empty:
        return "Ошибка получения данных"

    # Расчет индикаторов из списка Pocket Option
    # 1. Moving Average (EMA 200)
    data['EMA200'] = ta.ema(data['Close'], length=200)
    
    # 2. RSI (14)
    data['RSI'] = ta.rsi(data['Close'], length=14)
    
    # 3. Stochastic Oscillator (14, 3, 3)
    stoch = ta.stoch(data['High'], data['Low'], data['Close'], k=14, d=3, smooth_k=3)
    data['STOCHk'] = stoch['STOCHk_14_3_3']
    data['STOCHd'] = stoch['STOCHd_14_3_3']
    
    # 4. SuperTrend
    st = ta.supertrend(data['High'], data['Low'], data['Close'], length=10, multiplier=3)
    data['ST_DIR'] = st['SUPERTd_10_3.0'] # 1 для бычьего, -1 для медвежьего

    # Данные последней и предыдущей закрытой свечи
    curr = data.iloc[-1]
    prev = data.iloc[-2]

    # Проверка условий CALL (Вверх)
    call_cond = (
        curr['Close'] > curr['EMA200'] and
        curr['ST_DIR'] == 1 and
        40 < curr['RSI'] < 70 and
        prev['STOCHk'] < prev['STOCHd'] and curr['STOCHk'] > curr['STOCHd'] and
        curr['STOCHk'] < 50
    )

    # Проверка условий PUT (Вниз)
    put_cond = (
        curr['Close'] < curr['EMA200'] and
        curr['ST_DIR'] == -1 and
        30 < curr['RSI'] < 60 and
        prev['STOCHk'] > prev['STOCHd'] and curr['STOCHk'] < curr['STOCHd'] and
        curr['STOCHk'] > 50
    )

    if call_cond:
        return f"СИГНАЛ: CALL (ВВЕРХ) | Инструмент: {symbol} | Время: {curr.name}"
    elif put_cond:
        return f"СИГНАЛ: PUT (ВНИЗ) | Инструмент: {symbol} | Время: {curr.name}"
    else:
        return f"НЕТ СИГНАЛА | Инструмент: {symbol}"

# Пример запуска анализа для пары EUR/USD
print(get_trading_signal("EURUSD=X", interval="5m"))



# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    threading.Thread(target=health_server, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    scanner_loop()
