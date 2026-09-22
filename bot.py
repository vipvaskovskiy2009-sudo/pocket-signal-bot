import os
import json
import time
import threading
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}/"

PAIRS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
}


# =========================
# TELEGRAM
# =========================

def api(method, data=None):
    url = API + method

    if data:
        data = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=data)
    else:
        req = urllib.request.Request(url)

    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def keyboard():
    return json.dumps({
        "inline_keyboard": [
            [
                {"text": "EUR/USD", "callback_data": "EUR/USD"},
                {"text": "GBP/USD", "callback_data": "GBP/USD"}
            ],
            [
                {"text": "USD/JPY", "callback_data": "USD/JPY"}
            ]
        ]
    })


def send(chat_id, text):
    api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": keyboard()
    })


# =========================
# MARKET DATA
# =========================

def get_data(symbol):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + symbol +
        "?interval=1m&range=1d"
    )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())

    result = data["chart"]["result"][0]

    quote = result["indicators"]["quote"][0]

    opens = quote["open"]
    highs = quote["high"]
    lows = quote["low"]
    closes = quote["close"]

    candles = []

    for i in range(len(closes)):
        if (
            opens[i] is not None and
            highs[i] is not None and
            lows[i] is not None and
            closes[i] is not None
        ):
            candles.append({
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i])
            })

    # Не используем последнюю формирующуюся свечу.
    if len(candles) > 1:
        candles = candles[:-1]

    return candles


# =========================
# INDICATORS
# =========================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for price in values[period:]:
        result = (price - result) * multiplier + result

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def true_ranges(candles):
    trs = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        trs.append(tr)

    return trs


def atr(candles, period=14):
    trs = true_ranges(candles)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def bollinger(values, period=20, deviation=2):
    if len(values) < period:
        return None

    recent = values[-period:]

    middle = sum(recent) / period

    variance = sum(
        (x - middle) ** 2 for x in recent
    ) / period

    std = variance ** 0.5

    upper = middle + deviation * std
    lower = middle - deviation * std

    return middle, upper, lower


def macd(values):
    if len(values) < 35:
        return None

    ema12 = ema(values, 12)
    ema26 = ema(values, 26)

    if ema12 is None or ema26 is None:
        return None

    line = ema12 - ema26

    # Для стабильности используем MACD
    # как направление импульса.
    return line


def adx(candles, period=14):
    if len(candles) < period * 2 + 1:
        return None, None, None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up_move = current["high"] - previous["high"]
        down_move = previous["low"] - current["low"]

        plus = up_move if up_move > down_move and up_move > 0 else 0
        minus = down_move if down_move > up_move and down_move > 0 else 0

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(trs) < period:
        return None, None, None

    atr_value = sum(trs[:period]) / period
    plus_value = sum(plus_dm[:period]) / period
    minus_value = sum(minus_dm[:period]) / period

    dx_values = []

    plus_di = 0
    minus_di = 0

    for i in range(period, len(trs)):
        atr_value = (
            (atr_value * (period - 1)) + trs[i]
        ) / period

        plus_value = (
            (plus_value * (period - 1)) + plus_dm[i]
        ) / period

        minus_value = (
            (minus_value * (period - 1)) + minus_dm[i]
        ) / period

        if atr_value == 0:
            continue

        plus_di = 100 * plus_value / atr_value
        minus_di = 100 * minus_value / atr_value

        total = plus_di + minus_di

        if total == 0:
            continue

        dx = 100 * abs(plus_di - minus_di) / total

        dx_values.append(dx)

    if len(dx_values) < period:
        return None, None, None

    adx_value = sum(dx_values[:period]) / period

    for dx in dx_values[period:]:
        adx_value = (
            (adx_value * (period - 1)) + dx
        ) / period

    return adx_value, plus_di, minus_di


# =========================
# 5-MINUTE TREND
# =========================

def aggregate_5m(candles):
    result = []

    for i in range(0, len(candles) - 4, 5):
        block = candles[i:i + 5]

        if len(block) < 5:
            continue

        result.append({
            "open": block[0]["open"],
            "high": max(x["high"] for x in block),
            "low": min(x["low"] for x in block),
            "close": block[-1]["close"]
        })

    return result


# =========================
# SIGNAL ENGINE
# =========================

def get_signal(symbol):

    candles = get_data(symbol)

    if len(candles) < 120:
        return (
            "⚪ ПРОПУСК\n"
            "Недостаточно рыночных данных."
        )

    closes = [x["close"] for x in candles]

    current = closes[-1]
    previous = closes[-2]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)

    rsi_value = rsi(closes, 14)
    macd_value = macd(closes)

    atr_value = atr(candles, 14)
    bb = bollinger(closes, 20, 2)

    adx_value, plus_di, minus_di = adx(
        candles,
        14
    )

    if any(x is None for x in [
        ema9,
        ema21,
        ema50,
        rsi_value,
        macd_value,
        atr_value,
        bb,
        adx_value,
        plus_di,
        minus_di
    ]):
        return "⚪ ПРОПУСК\nНедостаточно данных для анализа."

    middle, upper, lower = bb

    # =========================
    # M5 TREND
    # =========================

    candles5 = aggregate_5m(candles)

    if len(candles5) < 30:
        return "⚪ ПРОПУСК\nНедостаточно M5 данных."

    closes5 = [x["close"] for x in candles5]

    ema9_5 = ema(closes5, 9)
    ema21_5 = ema(closes5, 21)

    if ema9_5 is None or ema21_5 is None:
        return "⚪ ПРОПУСК"

    score_up = 0
    score_down = 0

    reasons_up = []
    reasons_down = []

    # =========================
    # TREND
    # =========================

    if ema9 > ema21:
        score_up += 1
        reasons_up.append("EMA9>EMA21")

    elif ema9 < ema21:
        score_down += 1
        reasons_down.append("EMA9<EMA21")

    if current > ema50:
        score_up += 1
        reasons_up.append("цена>EMA50")

    elif current < ema50:
        score_down += 1
        reasons_down.append("цена<EMA50")

    # =========================
    # M5
    # =========================

    if ema9_5 > ema21_5:
        score_up += 2
        reasons_up.append("M5↑")

    elif ema9_5 < ema21_5:
        score_down += 2
        reasons_down.append("M5↓")

    # =========================
    # RSI
    # =========================

    if 52 <= rsi_value <= 68:
        score_up += 1
        reasons_up.append("RSI↑")

    elif 32 <= rsi_value <= 48:
        score_down += 1
        reasons_down.append("RSI↓")

    # Не покупаем экстремально перекупленный рынок
    # и не продаём экстремально перепроданный.
    if rsi_value > 72 or rsi_value < 28:
        return (
            "⚪ ПРОПУСК\n"
            "RSI показывает экстремальное состояние."
        )

    # =========================
    # MACD
    # =========================

    if macd_value > 0:
        score_up += 1
        reasons_up.append("MACD↑")

    elif macd_value < 0:
        score_down += 1
        reasons_down.append("MACD↓")

    # =========================
    # ADX / DIRECTION
    # =========================

    if adx_value >= 20:

        if plus_di > minus_di:
            score_up += 1
            reasons_up.append("ADX+DI")

        elif minus_di > plus_di:
            score_down += 1
            reasons_down.append("ADX-DI")

    else:
        return (
            "⚪ ПРОПУСК\n"
            "Рынок недостаточно трендовый."
        )

    # =========================
    # CANDLE CONFIRMATION
    # =========================

    candle_change = current - previous

    if candle_change > 0:
        score_up += 1
        reasons_up.append("последняя свеча↑")

    elif candle_change < 0:
        score_down += 1
        reasons_down.append("последняя свеча↓")

    # =========================
    # VOLATILITY
    # =========================

    recent_ranges = [
        x["high"] - x["low"]
        for x in candles[-10:]
    ]

    average_range = (
        sum(recent_ranges) /
        len(recent_ranges)
    )

    if average_range < atr_value * 0.45:
        return (
            "⚪ ПРОПУСК\n"
            "Слишком низкая волатильность."
        )

    if average_range > atr_value * 2.5:
        return (
            "⚪ ПРОПУСК\n"
            "Слишком резкий импульс."
        )

    # =========================
    # BOLLINGER
    # =========================

    # Не входим после слишком сильного
    # выхода за верхнюю/нижнюю полосу.
    if current > upper:
        score_up -= 1

    if current < lower:
        score_down -= 1

    # =========================
    # FINAL FILTER
    # =========================

    difference = abs(score_up - score_down)

    if score_up >= 7 and difference >= 3:
        return (
            "🟢 ВВЕРХ ↑\n"
            "⏱ 1 минута\n\n"
            f"📊 Сила сигнала: {score_up}/9\n"
            "🔎 Подтверждение: "
            + ", ".join(reasons_up)
        )

    if score_down >= 7 and difference >= 3:
        return (
            "🔴 ВНИЗ ↓\n"
            "⏱ 1 минута\n\n"
            f"📊 Сила сигнала: {score_down}/9\n"
            "🔎 Подтверждение: "
            + ", ".join(reasons_down)
        )

    return (
        "⚪ ПРОПУСК\n\n"
        f"ВВЕРХ: {score_up}\n"
        f"ВНИЗ: {score_down}\n\n"
        "Недостаточно подтверждений."
    )


# =========================
# TELEGRAM LOOP
# =========================

def main():

    offset = 0

    while True:

        try:

            result = api(
                "getUpdates",
                {
                    "timeout": 25,
                    "offset": offset
                }
            )

            for update in result.get("result", []):

                offset = update["update_id"] + 1

                if "message" in update:

                    msg = update["message"]

                    chat_id = msg["chat"]["id"]

                    text = msg.get("text", "")

                    if text == "/start":

                        send(
                            chat_id,
                            "🤖 Pocket Signal 2.0\n\n"
                            "Новая система анализа M1.\n\n"
                            "Я проверяю:\n"
                            "• M1 + M5 тренд\n"
                            "• EMA 9/21/50\n"
                            "• RSI\n"
                            "• MACD\n"
                            "• ADX\n"
                            "• ATR\n"
                            "• Bollinger Bands\n"
                            "• силу последней свечи\n\n"
                            "Если подтверждений недостаточно —\n"
                            "⚪ ПРОПУСК.\n\n"
                            "Экспирация: 1 минута."
                        )

                if "callback_query" in update:

                    q = update["callback_query"]

                    chat_id = q["message"]["chat"]["id"]

                    pair = q["data"]

                    api(
                        "answerCallbackQuery",
                        {
                            "callback_query_id": q["id"]
                        }
                    )

                    try:

                        signal = get_signal(
                            PAIRS[pair]
                        )

                        send(
                            chat_id,
                            f"📊 {pair}\n\n"
                            f"{signal}\n\n"
                            "⚠️ Технический сигнал. "
                            "Не является гарантией результата."
                        )

                    except Exception:

                        send(
                            chat_id,
                            f"📊 {pair}\n\n"
                            "⚪ ПРОПУСК\n\n"
                            "Не удалось получить рыночные данные."
                        )

        except Exception:

            time.sleep(5)


# =========================
# RENDER HEALTH SERVER
# =========================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"Pocket Signal 2.0 is running"
        )

    def log_message(self, format, *args):

        return


def start_web_server():

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    server.serve_forever()


# =========================
# START
# =========================

if __name__ == "__main__":

    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    main()
