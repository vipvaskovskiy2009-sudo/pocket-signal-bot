import os
import json
import time
import math
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


# =========================================================
# POCKET SIGNAL
# Strategy 3.0
# M1 + M5
# EMA 9/21/50
# RSI
# MACD
# ADX / DI
# ATR
# Bollinger Bands
# Closed candles only
# Strict scoring
# =========================================================


BOT_TOKEN = os.environ["BOT_TOKEN"]

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
}

CHECK_EVERY_SECONDS = 60

COOLDOWN_MINUTES = 3

# =========================================================
# STRATEGY SETTINGS
# =========================================================

ADX_MIN = 20.0

RSI_CALL_MIN = 55.0
RSI_CALL_MAX = 75.0

RSI_PUT_MIN = 25.0
RSI_PUT_MAX = 45.0

MIN_SCORE = 7
MIN_DIRECTION_ADVANTAGE = 3

# Минимальная ATR для определения, что рынок не "мертвый".
# Значение адаптивное: сравниваем ATR с собственной историей.
ATR_HISTORY = 30

# Bollinger proximity
BB_EDGE_ZONE = 0.15

# =========================================================
# MARTINGALE / PAPER STATE
# =========================================================

BASE_AMOUNT = 1.0
MARTINGALE_MULT = 2.2
MAX_STEPS = 3

state = {
    "martingale_step": 0,
    "current_amount": BASE_AMOUNT,
    "wins": 0,
    "losses": 0,
    "last_signal_time": None,
    "last_signal_direction": None,
    "last_signal_symbol": None,
    "series_results": [],
}


# =========================================================
# LOGGING
# =========================================================

def log(message):
    print(
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "|",
        message,
        flush=True
    )


# =========================================================
# HTTP
# =========================================================

def http_get(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 PocketSignal/3.0"
        }
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


# =========================================================
# TELEGRAM
# =========================================================

def telegram(method, payload=None):
    url = f"{TELEGRAM_API}/{method}"

    data = urllib.parse.urlencode(
        payload or {}
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded"
        },
        method="POST"
    )

    with urllib.request.urlopen(
        req,
        timeout=30
    ) as response:

        return json.loads(
            response.read().decode("utf-8")
        )


def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if keyboard:
        payload["reply_markup"] = json.dumps(
            keyboard,
            ensure_ascii=False
        )

    return telegram(
        "sendMessage",
        payload
    )


MAIN_KEYBOARD = {
    "keyboard": [
        [
            {"text": "📊 СИГНАЛЫ"},
            {"text": "📈 СТАТУС"}
        ],
        [
            {"text": "🔄 СКАНИРОВАТЬ"}
        ],
        [
            {"text": "🔴 CALL"},
            {"text": "🔵 PUT"}
        ]
    ],
    "resize_keyboard": True
}


# =========================================================
# YAHOO FINANCE
# =========================================================

def get_market_data(symbol):
    """
    Получаем M1 за последний день.
    Текущую формирующуюся свечу исключаем.
    """

    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + "?interval=1m"
        + "&range=1d"
        + "&includePrePost=false"
        + "&events=div%2Csplits"
    )

    try:
        data = http_get(url)
    except Exception as e:
        log(f"{symbol}: DATA ERROR {e}")
        return []

    result = data.get("chart", {}).get("result")

    if not result:
        return []

    result = result[0]

    timestamps = result.get("timestamp", [])
    quote = result.get("indicators", {}).get(
        "quote",
        []
    )

    if not quote:
        return []

    quote = quote[0]

    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    candles = []

    now = int(time.time())

    for i, ts in enumerate(timestamps):

        if i >= len(opens):
            continue

        if (
            opens[i] is None
            or highs[i] is None
            or lows[i] is None
            or closes[i] is None
        ):
            continue

        # Исключаем текущую формирующуюся M1 свечу.
        if int(ts) + 60 > now:
            continue

        candles.append({
            "time": int(ts),
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
            "volume": (
                float(volumes[i])
                if i < len(volumes)
                and volumes[i] is not None
                else 0.0
            )
        })

    return candles


# =========================================================
# M5 AGGREGATION
# =========================================================

def aggregate_m5(m1):
    groups = {}

    for candle in m1:
        bucket = (
            candle["time"] // 300
        ) * 300

        groups.setdefault(
            bucket,
            []
        ).append(candle)

    result = []

    for ts in sorted(groups):

        rows = groups[ts]

        if not rows:
            continue

        result.append({
            "time": ts,
            "open": rows[0]["open"],
            "high": max(
                x["high"] for x in rows
            ),
            "low": min(
                x["low"] for x in rows
            ),
            "close": rows[-1]["close"],
            "volume": sum(
                x["volume"] for x in rows
            )
        })

    return result


# =========================================================
# BASIC INDICATORS
# =========================================================

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    multiplier = 2.0 / (period + 1)

    first = sum(
        values[:period]
    ) / period

    result[period - 1] = first

    previous = first

    for i in range(period, len(values)):

        current = (
            (values[i] - previous)
            * multiplier
            + previous
        )

        result[i] = current
        previous = current

    return result


def sma(values, period):
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    total = sum(values[:period])

    result[period - 1] = total / period

    for i in range(period, len(values)):

        total += values[i]
        total -= values[i - period]

        result[i] = total / period

    return result


def rsi(values, period=14):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = values[i] - values[i - 1]

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = (
            100 - 100 / (1 + rs)
        )

    for i in range(period + 1, len(values)):

        gain = gains[i - 1]
        loss = losses[i - 1]

        avg_gain = (
            (avg_gain * (period - 1))
            + gain
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + loss
        ) / period

        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss

            result[i] = (
                100 - 100 / (1 + rs)
            )

    return result


def true_ranges(candles):

    result = []

    for i, candle in enumerate(candles):

        if i == 0:
            tr = (
                candle["high"]
                - candle["low"]
            )

        else:

            previous = candles[i - 1]["close"]

            tr = max(
                candle["high"]
                - candle["low"],

                abs(
                    candle["high"]
                    - previous
                ),

                abs(
                    candle["low"]
                    - previous
                )
            )

        result.append(tr)

    return result


def atr(candles, period=14):

    tr = true_ranges(candles)

    result = [None] * len(candles)

    if len(tr) < period:
        return result

    current = (
        sum(tr[:period])
        / period
    )

    result[period - 1] = current

    for i in range(period, len(tr)):

        current = (
            (current * (period - 1))
            + tr[i]
        ) / period

        result[i] = current

    return result


# =========================================================
# MACD
# =========================================================

def macd(values):

    ema12 = ema(values, 12)
    ema26 = ema(values, 26)

    line = [None] * len(values)

    for i in range(len(values)):

        if (
            ema12[i] is not None
            and ema26[i] is not None
        ):
            line[i] = (
                ema12[i]
                - ema26[i]
            )

    valid = [
        x for x in line
        if x is not None
    ]

    signal_valid = ema(
        valid,
        9
    )

    signal = [None] * len(values)

    pos = 0

    for i in range(len(values)):

        if line[i] is not None:

            signal[i] = signal_valid[pos]

            pos += 1

    histogram = [None] * len(values)

    for i in range(len(values)):

        if (
            line[i] is not None
            and signal[i] is not None
        ):

            histogram[i] = (
                line[i]
                - signal[i]
            )

    return line, signal, histogram


# =========================================================
# BOLLINGER
# =========================================================

def bollinger(values, period=20):

    middle = sma(
        values,
        period
    )

    upper = [None] * len(values)
    lower = [None] * len(values)

    for i in range(period - 1, len(values)):

        window = values[
            i - period + 1:
            i + 1
        ]

        mean = middle[i]

        variance = sum(
            (x - mean) ** 2
            for x in window
        ) / period

        deviation = math.sqrt(
            variance
        )

        upper[i] = (
            mean + 2 * deviation
        )

        lower[i] = (
            mean - 2 * deviation
        )

    return middle, upper, lower


# =========================================================
# ADX
# =========================================================

def adx(candles, period=14):

    if len(candles) < period * 2:
        return (
            [None] * len(candles),
            [None] * len(candles),
            [None] * len(candles)
        )

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(len(candles)):

        if i == 0:

            trs.append(
                candles[i]["high"]
                - candles[i]["low"]
            )

            plus_dm.append(0.0)
            minus_dm.append(0.0)

            continue

        current = candles[i]
        previous = candles[i - 1]

        up_move = (
            current["high"]
            - previous["high"]
        )

        down_move = (
            previous["low"]
            - current["low"]
        )

        if (
            up_move > down_move
            and up_move > 0
        ):
            plus = up_move
        else:
            plus = 0.0

        if (
            down_move > up_move
            and down_move > 0
        ):
            minus = down_move
        else:
            minus = 0.0

        tr = max(
            current["high"]
            - current["low"],

            abs(
                current["high"]
                - previous["close"]
            ),

            abs(
                current["low"]
                - previous["close"]
            )
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    atr_values = [None] * len(candles)
    plus_di = [None] * len(candles)
    minus_di = [None] * len(candles)
    adx_values = [None] * len(candles)

    if len(candles) < period + 1:
        return (
            adx_values,
            plus_di,
            minus_di
        )

    atr_current = sum(
        trs[1:period + 1]
    ) / period

    plus_current = sum(
        plus_dm[1:period + 1]
    ) / period

    minus_current = sum(
        minus_dm[1:period + 1]
    ) / period

    dx_values = []

    for i in range(
        period,
        len(candles)
    ):

        if i > period:

            atr_current = (
                (
                    atr_current
                    * (period - 1)
                )
                + trs[i]
            ) / period

            plus_current = (
                (
                    plus_current
                    * (period - 1)
                )
                + plus_dm[i]
            ) / period

            minus_current = (
                (
                    minus_current
                    * (period - 1)
                )
                + minus_dm[i]
            ) / period

        atr_values[i] = atr_current

        if atr_current == 0:
            continue

        plus = (
            100
            * plus_current
            / atr_current
        )

        minus = (
            100
            * minus_current
            / atr_current
        )

        plus_di[i] = plus
        minus_di[i] = minus

        denominator = plus + minus

        if denominator == 0:
            dx = 0
        else:
            dx = (
                100
                * abs(plus - minus)
                / denominator
            )

        dx_values.append(
            (i, dx)
        )

    if len(dx_values) >= period:

        first_adx = sum(
            x[1]
            for x in dx_values[:period]
        ) / period

        index = dx_values[
            period - 1
        ][0]

        adx_values[index] = first_adx

        current = first_adx

        for j in range(
            period,
            len(dx_values)
        ):

            idx, dx = dx_values[j]

            current = (
                (
                    current
                    * (period - 1)
                )
                + dx
            ) / period

            adx_values[idx] = current

    return (
        adx_values,
        plus_di,
        minus_di
    )


# =========================================================
# ANALYZE TIMEFRAME
# =========================================================

def indicators(candles):

    closes = [
        x["close"]
        for x in candles
    ]

    ema9 = ema(
        closes,
        9
    )

    ema21 = ema(
        closes,
        21
    )

    ema50 = ema(
        closes,
        50
    )

    rsi_values = rsi(
        closes,
        14
    )

    macd_line, macd_signal, macd_hist = macd(
        closes
    )

    atr_values = atr(
        candles,
        14
    )

    bb_mid, bb_high, bb_low = bollinger(
        closes,
        20
    )

    adx_values, plus_di, minus_di = adx(
        candles,
        14
    )

    return {
        "ema9": ema9[-1],
        "ema21": ema21[-1],
        "ema50": ema50[-1],

        "rsi": rsi_values[-1],

        "macd": macd_line[-1],
        "macd_signal": macd_signal[-1],
        "macd_hist": macd_hist[-1],

        "atr": atr_values[-1],

        "bb_mid": bb_mid[-1],
        "bb_high": bb_high[-1],
        "bb_low": bb_low[-1],

        "adx": adx_values[-1],
        "plus_di": plus_di[-1],
        "minus_di": minus_di[-1],

        "price": closes[-1]
    }


# =========================================================
# SCORE
# =========================================================

def check_strategy(m1, m5):

    if len(m1) < 80:
        return None

    if len(m5) < 80:
        return None

    a1 = indicators(m1)
    a5 = indicators(m5)

    values = list(
        a1.values()
    ) + list(
        a5.values()
    )

    if any(
        x is None
        for x in values
        if not isinstance(x, str)
    ):
        return None

    call = 0
    put = 0

    call_reasons = []
    put_reasons = []

    # =====================================================
    # M1 EMA STRUCTURE
    # =====================================================

    if (
        a1["ema9"]
        > a1["ema21"]
        > a1["ema50"]
    ):
        call += 1
        call_reasons.append(
            "M1 EMA 9>21>50"
        )

    if (
        a1["ema9"]
        < a1["ema21"]
        < a1["ema50"]
    ):
        put += 1
        put_reasons.append(
            "M1 EMA 9<21<50"
        )

    # =====================================================
    # M5 TREND
    # =====================================================

    if (
        a5["ema9"]
        > a5["ema21"]
        > a5["ema50"]
    ):
        call += 2
        call_reasons.append(
            "M5 trend UP"
        )

    if (
        a5["ema9"]
        < a5["ema21"]
        < a5["ema50"]
    ):
        put += 2
        put_reasons.append(
            "M5 trend DOWN"
        )

    # =====================================================
    # RSI
    # =====================================================

    if (
        RSI_CALL_MIN
        <= a1["rsi"]
        < RSI_CALL_MAX
    ):
        call += 1
        call_reasons.append(
            f"RSI {a1['rsi']:.1f}"
        )

    if (
        RSI_PUT_MIN
        < a1["rsi"]
        <= RSI_PUT_MAX
    ):
        put += 1
        put_reasons.append(
            f"RSI {a1['rsi']:.1f}"
        )

    # =====================================================
    # MACD
    # =====================================================

    if (
        a1["macd"] > a1["macd_signal"]
        and a1["macd_hist"] > 0
    ):
        call += 1
        call_reasons.append(
            "MACD bullish"
        )

    if (
        a1["macd"] < a1["macd_signal"]
        and a1["macd_hist"] < 0
    ):
        put += 1
        put_reasons.append(
            "MACD bearish"
        )

    # =====================================================
    # ADX + DI
    # =====================================================

    if (
        a1["adx"] >= ADX_MIN
        and a1["plus_di"]
        > a1["minus_di"]
    ):
        call += 1
        call_reasons.append(
            f"ADX {a1['adx']:.1f} +DI"
        )

    if (
        a1["adx"] >= ADX_MIN
        and a1["minus_di"]
        > a1["plus_di"]
    ):
        put += 1
        put_reasons.append(
            f"ADX {a1['adx']:.1f} -DI"
        )

    # =====================================================
    # M5 RSI CONFIRMATION
    # =====================================================

    if (
        a5["rsi"] > 50
        and a5["rsi"] < 70
    ):
        call += 1
        call_reasons.append(
            f"M5 RSI {a5['rsi']:.1f}"
        )

    if (
        a5["rsi"] < 50
        and a5["rsi"] > 30
    ):
        put += 1
        put_reasons.append(
            f"M5 RSI {a5['rsi']:.1f}"
        )

    # =====================================================
    # BOLLINGER
    # =====================================================

    bb_range = (
        a1["bb_high"]
        - a1["bb_low"]
    )

    if bb_range > 0:

        position = (
            a1["price"]
            - a1["bb_low"]
        ) / bb_range

        if (
            position > BB_EDGE_ZONE
            and position < 0.85
        ):
            if a1["price"] > a1["bb_mid"]:
                call += 1
                call_reasons.append(
                    "BB bullish zone"
                )

            if a1["price"] < a1["bb_mid"]:
                put += 1
                put_reasons.append(
                    "BB bearish zone"
                )

    # =====================================================
    # ATR VOLATILITY
    # =====================================================

    recent_atr = []

    for i in range(
        max(
            14,
            len(m1) - ATR_HISTORY
        ),
        len(m1)
    ):

        partial = m1[:i + 1]

        value = indicators(
            partial
        )["atr"]

        if value is not None:
            recent_atr.append(value)

    if recent_atr:

        atr_average = (
            sum(recent_atr)
            / len(recent_atr)
        )

        if (
            a1["atr"]
            >= atr_average * 0.80
        ):
            call += 1
            put += 1

    # =====================================================
    # FINAL DECISION
    # =====================================================

    if (
        call >= MIN_SCORE
        and call - put
        >= MIN_DIRECTION_ADVANTAGE
    ):

        return {
            "direction": "CALL",
            "call": call,
            "put": put,
            "reasons": call_reasons,
            "m1": a1,
            "m5": a5
        }

    if (
        put >= MIN_SCORE
        and put - call
        >= MIN_DIRECTION_ADVANTAGE
    ):

        return {
            "direction": "PUT",
            "call": call,
            "put": put,
            "reasons": put_reasons,
            "m1": a1,
            "m5": a5
        }

    return {
        "direction": None,
        "call": call,
        "put": put,
        "reasons": [],
        "m1": a1,
        "m5": a5
    }


# =========================================================
# SIGNAL
# =========================================================

def can_signal():

    last = state["last_signal_time"]

    if last is None:
        return True

    elapsed = (
        datetime.now(timezone.utc)
        - last
    ).total_seconds()

    return elapsed >= (
        COOLDOWN_MINUTES * 60
    )


def signal_message(
    name,
    result
):

    direction = result["direction"]

    emoji = (
        "🟢"
        if direction == "CALL"
        else "🔴"
    )

    a1 = result["m1"]
    a5 = result["m5"]

    reasons = "\n".join(
        "• " + x
        for x in result["reasons"][:8]
    )

    return (
        f"{emoji} <b>{direction}</b>\n\n"

        f"💱 <b>{name}</b>\n"
        f"Цена: <code>{a1['price']:.5f}</code>\n\n"

        f"📊 Score:\n"
        f"CALL: {result['call']}\n"
        f"PUT: {result['put']}\n\n"

        f"📈 M1:\n"
        f"RSI: {a1['rsi']:.1f}\n"
        f"ADX: {a1['adx']:.1f}\n"
        f"MACD: {a1['macd_hist']:.6f}\n\n"

        f"📈 M5:\n"
        f"RSI: {a5['rsi']:.1f}\n"
        f"ADX: {a5['adx']:.1f}\n\n"

        f"🔎 <b>Подтверждения:</b>\n"
        f"{reasons}\n\n"

        f"⏱ Экспирация модели: "
        f"1 минута\n"

        f"💵 Paper amount: "
        f"${state['current_amount']:.2f}\n"

        f"⚠️ Модельный сигнал. "
        f"Не гарантия результата."
    )


# =========================================================
# MARKET SCAN
# =========================================================

def scan_market():

    if not can_signal():
        return

    for name, symbol in SYMBOLS.items():

        try:

            m1 = get_market_data(
                symbol
            )

            if len(m1) < 100:
                log(
                    f"{name}: недостаточно M1"
                )
                continue

            m5 = aggregate_m5(
                m1
            )

            if len(m5) < 80:
                log(
                    f"{name}: недостаточно M5"
                )
                continue

            result = check_strategy(
                m1,
                m5
            )

            if result is None:
                continue

            log(
                f"{name}: "
                f"CALL={result['call']} "
                f"PUT={result['put']}"
            )

            if result["direction"]:

                state[
                    "last_signal_time"
                ] = datetime.now(
                    timezone.utc
                )

                state[
                    "last_signal_direction"
                ] = result[
                    "direction"
                ]

                state[
                    "last_signal_symbol"
                ] = name

                send_message_to_last_chat(
                    signal_message(
                        name,
                        result
                    )
                )

                return

        except Exception as e:

            log(
                f"{name}: ERROR {e}"
            )


# =========================================================
# TELEGRAM TARGET
# =========================================================

LAST_CHAT_ID = None


def send_message_to_last_chat(text):

    global LAST_CHAT_ID

    if LAST_CHAT_ID is None:
        log(
            "Нет Telegram chat_id"
        )
        return

    try:

        send_message(
            LAST_CHAT_ID,
            text,
            MAIN_KEYBOARD
        )

    except Exception as e:

        log(
            f"Telegram send error: {e}"
        )


# =========================================================
# COMMANDS
# =========================================================

def handle_command(
    chat_id,
    text
):

    global LAST_CHAT_ID

    LAST_CHAT_ID = chat_id

    if text == "/start":

        send_message(
            chat_id,

            "⚡ <b>POCKET SIGNAL 3.0</b>\n\n"
            "M1 + M5 анализ.\n"
            "EMA 9/21/50\n"
            "RSI\n"
            "MACD\n"
            "ADX + DI\n"
            "ATR\n"
            "Bollinger Bands\n\n"
            "Строгий фильтр сигналов.",
            
            MAIN_KEYBOARD
        )

        return

    if text == "📊 СИГНАЛЫ":

        send_message(
            chat_id,
            "📊 Автоматический сканер активен.\n\n"
            "Проверяются:\n"
            "EUR/USD\n"
            "GBP/USD\n"
            "USD/JPY\n\n"
            "При отсутствии сильного сигнала — SKIP.",
            MAIN_KEYBOARD
        )

        return

    if text == "🔄 СКАНИРОВАТЬ":

        send_message(
            chat_id,
            "🔎 Запускаю сканирование...",
            MAIN_KEYBOARD
        )

        scan_market()

        return

    if text == "📈 СТАТУС":

        total = (
            state["wins"]
            + state["losses"]
        )

        if total:
            winrate = (
                state["wins"]
                / total
                * 100
            )
        else:
            winrate = 0

        send_message(
            chat_id,

            "📈 <b>СТАТУС</b>\n\n"
            f"Шаг: "
            f"{state['martingale_step']}/"
            f"{MAX_STEPS}\n"
            f"Paper amount: "
            f"${state['current_amount']:.2f}\n\n"
            f"Wins: {state['wins']}\n"
            f"Losses: {state['losses']}\n"
            f"Winrate: {winrate:.1f}%",

            MAIN_KEYBOARD
        )

        return

    if text == "🔴 CALL":

        send_message(
            chat_id,
            "🔴 Фильтр CALL включён.\n"
            "Автоматический вход не выполняется.",
            MAIN_KEYBOARD
        )

        return

    if text == "🔵 PUT":

        send_message(
            chat_id,
            "🔵 Фильтр PUT включён.\n"
            "Автоматический вход не выполняется.",
            MAIN_KEYBOARD
        )

        return

    if text == "/win":

        state["wins"] += 1

        state[
            "series_results"
        ].append("W")

        state[
            "martingale_step"
        ] = 0

        state[
            "current_amount"
        ] = BASE_AMOUNT

        send_message(
            chat_id,

            "✅ <b>WIN</b>\n\n"
            "Серия сброшена.\n"
            f"Paper amount: "
            f"${BASE_AMOUNT:.2f}",

            MAIN_KEYBOARD
        )

        return

    if text == "/loss":

        state["losses"] += 1

        state[
            "series_results"
        ].append("L")

        if (
            state["martingale_step"]
            >= MAX_STEPS
        ):

            state[
                "martingale_step"
            ] = 0

            state[
                "current_amount"
            ] = BASE_AMOUNT

            send_message(
                chat_id,

                "⛔ <b>MAX STEPS</b>\n\n"
                "Серия сброшена.\n"
                f"Paper amount: "
                f"${BASE_AMOUNT:.2f}",

                MAIN_KEYBOARD
            )

        else:

            state[
                "martingale_step"
            ] += 1

            state[
                "current_amount"
            ] = (
                BASE_AMOUNT
                * (
                    MARTINGALE_MULT
                    ** state[
                        "martingale_step"
                    ]
                )
            )

            send_message(
                chat_id,

                "❌ <b>LOSS</b>\n\n"
                f"Шаг: "
                f"{state['martingale_step']}/"
                f"{MAX_STEPS}\n"
                f"Следующий paper amount: "
                f"${state['current_amount']:.2f}",

                MAIN_KEYBOARD
            )

        return

    if text == "/reset":

        state[
            "martingale_step"
        ] = 0

        state[
            "current_amount"
        ] = BASE_AMOUNT

        state["wins"] = 0
        state["losses"] = 0
        state[
            "series_results"
        ] = []

        send_message(
            chat_id,
            "🔄 Статистика и серия сброшены.",
            MAIN_KEYBOARD
        )


# =========================================================
# TELEGRAM POLLING
# =========================================================

def telegram_loop():

    offset = 0

    log(
        "Pocket Signal 3.0 started"
    )

    while True:

        try:

            result = telegram(
                "getUpdates",
                {
                    "timeout": 30,
                    "offset": offset
                }
            )

            for update in result.get(
                "result",
                []
            ):

                offset = (
                    update["update_id"]
                    + 1
                )

                message = update.get(
                    "message"
                )

                if not message:
                    continue

                chat_id = (
                    message[
                        "chat"
                    ]["id"]
                )

                text = (
                    message.get(
                        "text",
                        ""
                    )
                )

                handle_command(
                    chat_id,
                    text
                )

        except Exception as e:

            log(
                f"Telegram ERROR: {e}"
            )

            time.sleep(5)


# =========================================================
# AUTO SCANNER
# =========================================================

def scanner_loop():

    while True:

        try:

            scan_market()

        except Exception as e:

            log(
                f"SCANNER ERROR: {e}"
            )

        time.sleep(
            CHECK_EVERY_SECONDS
        )


# =========================================================
# RENDER HEALTH SERVER
# =========================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"Pocket Signal 3.0 is running."
        )

    def log_message(
        self,
        format,
        *args
    ):
        return


def health_server():

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

    log(
        f"Health server on {port}"
    )

    server.serve_forever()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    threading.Thread(
        target=health_server,
        daemon=True
    ).start()

    threading.Thread(
        target=telegram_loop,
        daemon=True
    ).start()

    scanner_loop()
