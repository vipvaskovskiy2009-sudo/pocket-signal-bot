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
# POCKET SIGNAL 16.0 — SuperTrend + Stochastic + BBW + AO
# Совершенно другой набор индикаторов.
# =========================================================


BOT_TOKEN = os.environ["BOT_TOKEN"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CAD": "USDCAD=X",
    "NZD/USD": "NZDUSD=X",
}

CHECK_EVERY_SECONDS = 45
COOLDOWN_MINUTES = 5

# =========================================================
# НАСТРОЙКИ
# =========================================================

# SuperTrend
ST_PERIOD = 10
ST_MULT = 3.0

# Stochastic
STOCH_K = 5
STOCH_D = 3
STOCH_SMOOTH = 3
STOCH_CALL_MIN = 20     # CALL когда выходит из перепроданности
STOCH_CALL_MAX = 55
STOCH_PUT_MIN = 45
STOCH_PUT_MAX = 80

# Bollinger Bands Width
BBW_PERIOD = 20
BBW_MIN_RATIO = 1.05    # ширина BB должна быть ≥ 105% от средней

# Awesome Oscillator
AO_FAST = 5
AO_SLOW = 34

# ADX
ADX_MIN = 15.0

# Сессия
SESSION_START_HOUR_UTC = 7
SESSION_END_HOUR_UTC = 21
TRADE_WEEKDAYS = {0, 1, 2, 3, 4}
NEWS_BLACKOUT_MINUTES = {28, 29, 30, 31, 32, 58, 59, 0, 1, 2}

# =========================================================
# STATE
# =========================================================

FIXED_AMOUNT = 5.0

state = {
    "wins": 0,
    "losses": 0,
    "signals_sent": 0,
    "last_signal_time": None,
    "last_signal_direction": None,
    "last_signal_symbol": None,
    "last_signal_score": None,
    "series_results": [],
    "block_stats": {},
}


def log(message):
    print(
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "|", message,
        flush=True
    )


# =========================================================
# HTTP / TELEGRAM
# =========================================================

def http_get(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 PocketSignal/16.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def telegram(method, payload=None):
    url = f"{TELEGRAM_API}/{method}"
    data = urllib.parse.urlencode(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    return telegram("sendMessage", payload)


MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 СИГНАЛЫ"}, {"text": "📈 СТАТУС"}],
        [{"text": "🔄 СКАНИРОВАТЬ"}],
        [{"text": "✅ WIN"}, {"text": "❌ LOSS"}],
    ],
    "resize_keyboard": True
}


# =========================================================
# MARKET DATA
# =========================================================

def get_market_data(symbol, interval="1m", rng="1d"):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + f"?interval={interval}&range={rng}"
        + "&includePrePost=false&events=div%2Csplits"
    )
    try:
        data = http_get(url)
    except Exception as e:
        log(f"{symbol} {interval}: ERROR {e}")
        return []

    result = data.get("chart", {}).get("result")
    if not result:
        return []
    result = result[0]

    timestamps = result.get("timestamp", [])
    quote = result.get("indicators", {}).get("quote", [])
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
    interval_seconds = {
        "1m": 60, "5m": 300, "15m": 900, "1h": 3600
    }.get(interval, 60)

    for i, ts in enumerate(timestamps):
        if i >= len(opens):
            continue
        if (opens[i] is None or highs[i] is None
                or lows[i] is None or closes[i] is None):
            continue
        if int(ts) + interval_seconds > now:
            continue
        candles.append({
            "time": int(ts),
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
            "volume": float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0
        })
    return candles


# =========================================================
# INDICATORS
# =========================================================

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    result = [None] * len(values)
    mult = 2.0 / (period + 1)
    first = sum(values[:period]) / period
    result[period - 1] = first
    prev = first
    for i in range(period, len(values)):
        cur = (values[i] - prev) * mult + prev
        result[i] = cur
        prev = cur
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


def atr(candles, period=14):
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["high"] - c["low"])
        else:
            prev = candles[i - 1]["close"]
            trs.append(max(
                c["high"] - c["low"],
                abs(c["high"] - prev),
                abs(c["low"] - prev)
            ))
    result = [None] * len(candles)
    if len(trs) < period:
        return result
    curr = sum(trs[:period]) / period
    result[period - 1] = curr
    for i in range(period, len(trs)):
        curr = (curr * (period - 1) + trs[i]) / period
        result[i] = curr
    return result


def adx_full(candles, period=14):
    if len(candles) < period * 2:
        return [None] * len(candles), [None] * len(candles), [None] * len(candles)

    trs, plus_dm, minus_dm = [], [], []
    for i in range(len(candles)):
        if i == 0:
            trs.append(candles[i]["high"] - candles[i]["low"])
            plus_dm.append(0.0)
            minus_dm.append(0.0)
            continue
        curr, prev = candles[i], candles[i - 1]
        up = curr["high"] - prev["high"]
        down = prev["low"] - curr["low"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr = max(
            curr["high"] - curr["low"],
            abs(curr["high"] - prev["close"]),
            abs(curr["low"] - prev["close"])
        )
        trs.append(tr)

    if len(candles) < period + 1:
        return [None] * len(candles), [None] * len(candles), [None] * len(candles)

    atr_c = sum(trs[1:period + 1]) / period
    plus_c = sum(plus_dm[1:period + 1]) / period
    minus_c = sum(minus_dm[1:period + 1]) / period

    adx_v = [None] * len(candles)
    plus_di = [None] * len(candles)
    minus_di = [None] * len(candles)
    dxs = []

    for i in range(period, len(candles)):
        if i > period:
            atr_c = (atr_c * (period - 1) + trs[i]) / period
            plus_c = (plus_c * (period - 1) + plus_dm[i]) / period
            minus_c = (minus_c * (period - 1) + minus_dm[i]) / period
        if atr_c == 0:
            continue
        p = 100 * plus_c / atr_c
        m = 100 * minus_c / atr_c
        plus_di[i] = p
        minus_di[i] = m
        denom = p + m
        dx = 0 if denom == 0 else 100 * abs(p - m) / denom
        dxs.append((i, dx))

    if len(dxs) >= period:
        first = sum(x[1] for x in dxs[:period]) / period
        adx_v[dxs[period - 1][0]] = first
        curr = first
        for j in range(period, len(dxs)):
            idx, dx = dxs[j]
            curr = (curr * (period - 1) + dx) / period
            adx_v[idx] = curr

    return adx_v, plus_di, minus_di


def supertrend(candles, period=10, mult=3.0):
    """Возвращает (trend, line). trend = 1 (UP) или -1 (DOWN)."""
    n = len(candles)
    if n < period + 1:
        return [None] * n, [None] * n

    atr_v = atr(candles, period)
    trend = [None] * n
    line = [None] * n

    for i in range(period, n):
        if atr_v[i] is None:
            continue
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        upper = hl2 + mult * atr_v[i]
        lower = hl2 - mult * atr_v[i]

        if i == period:
            trend[i] = 1
            line[i] = lower
            continue

        prev_trend = trend[i - 1]
        prev_line = line[i - 1]

        # если предыдущий тренд UP — фиксируем только повышение нижней
        if prev_trend == 1:
            if candles[i]["close"] > prev_line:
                trend[i] = 1
                line[i] = max(lower, prev_line)
            else:
                trend[i] = -1
                line[i] = upper
        else:
            if candles[i]["close"] < prev_line:
                trend[i] = -1
                line[i] = min(upper, prev_line)
            else:
                trend[i] = 1
                line[i] = lower

    return trend, line


def stochastic(candles, k_period=5, d_period=3, smooth=3):
    """Возвращает (%K, %D) с сглаживанием."""
    n = len(candles)
    raw_k = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1:i + 1]
        hh = max(x["high"] for x in window)
        ll = min(x["low"] for x in window)
        if hh - ll == 0:
            raw_k[i] = 50.0
        else:
            raw_k[i] = 100 * (candles[i]["close"] - ll) / (hh - ll)

    # сглаживаем K
    valid_k = [x for x in raw_k if x is not None]
    if len(valid_k) < smooth:
        return [None] * n, [None] * n

    smoothed_k_valid = sma(valid_k, smooth)
    k_line = [None] * n
    pos = 0
    for i in range(n):
        if raw_k[i] is not None:
            k_line[i] = smoothed_k_valid[pos]
            pos += 1

    # D = SMA от K
    valid_k2 = [x for x in k_line if x is not None]
    d_valid = sma(valid_k2, d_period)
    d_line = [None] * n
    pos = 0
    for i in range(n):
        if k_line[i] is not None:
            d_line[i] = d_valid[pos]
            pos += 1

    return k_line, d_line


def bollinger_width(candles, period=20):
    """Возвращает список ширин BB (upper - lower)."""
    closes = [x["close"] for x in candles]
    n = len(closes)
    width = [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        dev = math.sqrt(var)
        width[i] = 4 * dev  # upper - lower = 2*dev * 2
    return width


def awesome_oscillator(candles, fast=5, slow=34):
    """AO = SMA(median, 5) - SMA(median, 34)."""
    median = [(c["high"] + c["low"]) / 2 for c in candles]
    fast_sma = sma(median, fast)
    slow_sma = sma(median, slow)
    n = len(candles)
    ao = [None] * n
    for i in range(n):
        if fast_sma[i] is not None and slow_sma[i] is not None:
            ao[i] = fast_sma[i] - slow_sma[i]
    return ao


# =========================================================
# SNAPSHOT
# =========================================================

def snapshot(candles):
    closes = [x["close"] for x in candles]
    st_trend, st_line = supertrend(candles, ST_PERIOD, ST_MULT)
    k, d = stochastic(candles, STOCH_K, STOCH_D, STOCH_SMOOTH)
    bbw = bollinger_width(candles, BBW_PERIOD)
    ao = awesome_oscillator(candles, AO_FAST, AO_SLOW)
    adx_v, plus_di, minus_di = adx_full(candles, 14)

    return {
        "st_trend": st_trend[-1],
        "st_trend_prev": st_trend[-2] if len(st_trend) > 1 else None,
        "st_line": st_line[-1],
        "k": k[-1],
        "k_prev": k[-2] if len(k) > 1 else None,
        "d": d[-1],
        "d_prev": d[-2] if len(d) > 1 else None,
        "bbw": bbw[-1],
        "bbw_list": bbw,
        "ao": ao[-1],
        "ao_prev": ao[-2] if len(ao) > 1 else None,
        "adx": adx_v[-1],
        "plus_di": plus_di[-1],
        "minus_di": minus_di[-1],
        "price": closes[-1],
    }


# =========================================================
# HELPERS
# =========================================================

def is_market_open():
    now = datetime.now(timezone.utc)
    if now.weekday() not in TRADE_WEEKDAYS:
        return False, "выходной"
    if not (SESSION_START_HOUR_UTC <= now.hour < SESSION_END_HOUR_UTC):
        return False, f"вне сессии ({now.hour:02d} UTC)"
    if now.minute in NEWS_BLACKOUT_MINUTES:
        return False, "новостное окно"
    return True, "ok"


def bbw_above_average(bbw_list, ratio):
    """Проверяет, что текущая ширина BB ≥ ratio * средняя за 30 свечей."""
    if bbw_list[-1] is None:
        return False
    recent = [x for x in bbw_list[-31:-1] if x is not None]
    if len(recent) < 15:
        return False
    avg = sum(recent) / len(recent)
    if avg <= 0:
        return False
    return bbw_list[-1] >= avg * ratio


# =========================================================
# STRATEGY
# =========================================================

def check_strategy(m1, m5):
    """
    Основная логика:
    1) SuperTrend M5 — определяет направление
    2) SuperTrend M1 — подтверждает
    3) Stochastic — момент входа
    4) BBW — волатильность растёт
    5) AO — momentum подтверждает
    6) ADX — тренд есть
    """
    if len(m1) < 60 or len(m5) < 60:
        return {"direction": None, "block_reason": "мало данных"}

    a1 = snapshot(m1)
    a5 = snapshot(m5)

    for a in (a1, a5):
        for v in a.values():
            if isinstance(v, list):
                continue
            if v is None:
                return {"direction": None, "block_reason": "NaN"}

    # БЛОК 1: SuperTrend M5 — определяет сторону
    if a5["st_trend"] not in (1, -1):
        return {"direction": None, "block_reason": "ST M5 не определён"}

    direction = "CALL" if a5["st_trend"] == 1 else "PUT"

    # БЛОК 2: SuperTrend M1 — согласие с M5
    if a1["st_trend"] != a5["st_trend"]:
        return {"direction": None,
                "block_reason": f"ST M1≠M5"}

    # БЛОК 3: BBW — волатильность растёт
    if not bbw_above_average(a1["bbw_list"], BBW_MIN_RATIO):
        return {"direction": None,
                "block_reason": "BBW сжата"}

    # БЛОК 4: ADX M1 ≥ 15
    if a1["adx"] < ADX_MIN:
        return {"direction": None,
                "block_reason": f"ADX {a1['adx']:.1f}"}

    # ОЧКИ
    score = 0
    reasons = []

    # Stochastic %K в нужной зоне
    if direction == "CALL":
        if STOCH_CALL_MIN <= a1["k"] <= STOCH_CALL_MAX:
            score += 1
            reasons.append(f"Stoch K={a1['k']:.0f}")
    else:
        if STOCH_PUT_MIN <= a1["k"] <= STOCH_PUT_MAX:
            score += 1
            reasons.append(f"Stoch K={a1['k']:.0f}")

    # Stochastic K > D для CALL, K < D для PUT
    if a1["k"] is not None and a1["d"] is not None:
        if direction == "CALL" and a1["k"] > a1["d"]:
            score += 1
            reasons.append("K>D")
        elif direction == "PUT" and a1["k"] < a1["d"]:
            score += 1
            reasons.append("K<D")

    # Stochastic растёт/падает
    if a1["k_prev"] is not None:
        if direction == "CALL" and a1["k"] > a1["k_prev"]:
            score += 1
            reasons.append("K растёт")
        elif direction == "PUT" and a1["k"] < a1["k_prev"]:
            score += 1
            reasons.append("K падает")

    # AO подтверждает направление
    if a1["ao"] is not None and a1["ao_prev"] is not None:
        if direction == "CALL" and a1["ao"] > 0 and a1["ao"] > a1["ao_prev"]:
            score += 1
            reasons.append("AO bullish")
        elif direction == "PUT" and a1["ao"] < 0 and a1["ao"] < a1["ao_prev"]:
            score += 1
            reasons.append("AO bearish")

    # DI согласие
    if direction == "CALL" and a1["plus_di"] > a1["minus_di"]:
        score += 1
        reasons.append(f"+DI {a1['plus_di']:.0f}")
    elif direction == "PUT" and a1["minus_di"] > a1["plus_di"]:
        score += 1
        reasons.append(f"-DI {a1['minus_di']:.0f}")

    # Свеча в направлении
    last = m1[-1]
    if direction == "CALL" and last["close"] > last["open"]:
        score += 1
        reasons.append("Свеча UP")
    elif direction == "PUT" and last["close"] < last["open"]:
        score += 1
        reasons.append("Свеча DOWN")

    # Порог
    MIN_SCORE = 4
    if score < MIN_SCORE:
        return {"direction": None,
                "block_reason": f"score {score}/6"}

    label = "СИЛЬНЫЙ" if score >= 5 else "УМЕРЕННЫЙ"

    return {
        "direction": direction,
        "block_reason": None,
        "score": score,
        "label": label,
        "reasons": reasons,
        "m1": a1, "m5": a5,
    }


# =========================================================
# SIGNAL MESSAGE
# =========================================================

def can_signal():
    last = state["last_signal_time"]
    if last is None:
        return True
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return elapsed >= COOLDOWN_MINUTES * 60


def signal_message(name, r):
    direction = r["direction"]
    a1 = r["m1"]
    a5 = r["m5"]
    score = r["score"]
    label = r["label"]

    if direction == "CALL":
        header = "🐂 БЫК → ВВЕРХ"
    else:
        header = "🐻 МЕДВЕДЬ → ВНИЗ"

    stars = "🟢🟢🟢" if score >= 5 else "🟢🟢"
    reasons_txt = "\n".join("• " + x for x in r["reasons"])

    return (
        f"{stars}\n"
        f"<b>{direction}</b>\n"
        f"{header}\n"
        f"Качество: <b>{label}</b> ({score}/6)\n\n"
        f"💱 <b>{name}</b>\n"
        f"Цена: <code>{a1['price']:.5f}</code>\n\n"
        f"<b>✅ Блоки пройдены:</b>\n"
        f"• SuperTrend M5 = {'UP' if a5['st_trend'] == 1 else 'DOWN'}\n"
        f"• SuperTrend M1 совпадает\n"
        f"• BBW растёт\n"
        f"• ADX {a1['adx']:.1f} ≥ {ADX_MIN}\n\n"
        f"<b>📋 Очки ({score}/6):</b>\n{reasons_txt}\n\n"
        f"<b>📊 Снимок:</b>\n"
        f"M5 ST: {'UP' if a5['st_trend'] == 1 else 'DOWN'} | ADX {a5['adx']:.1f}\n"
        f"M1 ST: {'UP' if a1['st_trend'] == 1 else 'DOWN'} | ADX {a1['adx']:.1f}\n"
        f"M1 Stoch: K={a1['k']:.0f} D={a1['d']:.0f}\n"
        f"M1 AO: {a1['ao']:.5f}\n"
        f"M1 +DI/-DI: {a1['plus_di']:.0f}/{a1['minus_di']:.0f}\n\n"
        f"⏱ <b>Экспирация: 60 сек</b>\n"
        f"💵 Ставка: <b>${FIXED_AMOUNT:.2f}</b>\n\n"
        f"⚠️ Не гарантия. Статистический сетап."
    )


# =========================================================
# SCAN
# =========================================================

LAST_CHAT_ID = None


def send_message_to_last_chat(text):
    global LAST_CHAT_ID
    if LAST_CHAT_ID is None:
        return
    try:
        send_message(LAST_CHAT_ID, text, MAIN_KEYBOARD)
    except Exception as e:
        log(f"Telegram error: {e}")


def scan_market(force=False):
    if not force:
        ok, reason = is_market_open()
        if not ok:
            return
        if not can_signal():
            return

    for name, symbol in SYMBOLS.items():
        try:
            m1 = get_market_data(symbol, "1m", "1d")
            time.sleep(0.2)
            m5 = get_market_data(symbol, "5m", "5d")
            time.sleep(0.2)

            if len(m1) < 60 or len(m5) < 60:
                continue

            result = check_strategy(m1, m5)

            if result["direction"]:
                log(f"✅ {name}: {result['direction']} {result['label']} ({result['score']}/6)")
                state["last_signal_time"] = datetime.now(timezone.utc)
                state["last_signal_direction"] = result["direction"]
                state["last_signal_symbol"] = name
                state["last_signal_score"] = result["score"]
                state["signals_sent"] += 1
                send_message_to_last_chat(signal_message(name, result))
                return
            else:
                br = result.get("block_reason", "?")
                state["block_stats"][br] = state["block_stats"].get(br, 0) + 1

        except Exception as e:
            log(f"{name}: ERROR {e}")


# =========================================================
# COMMANDS
# =========================================================

def handle_command(chat_id, text):
    global LAST_CHAT_ID
    LAST_CHAT_ID = chat_id

    if text == "/start":
        send_message(
            chat_id,
            "⚡ <b>POCKET SIGNAL 16.0</b>\n\n"
            "<b>Новые индикаторы:</b>\n"
            "• SuperTrend (M1 + M5)\n"
            "• Stochastic Oscillator\n"
            "• Bollinger Bands Width\n"
            "• Awesome Oscillator\n"
            "• ADX + DI\n\n"
            "<b>Блокирующие:</b>\n"
            "1. ST M5 определяет направление\n"
            "2. ST M1 согласен с M5\n"
            "3. BBW растёт (не мёртвый рынок)\n"
            "4. ADX ≥ 15\n\n"
            "<b>Очки (нужно ≥ 4):</b>\n"
            "• Stoch K в зоне\n"
            "• Stoch K>D или K<D\n"
            "• Stoch растёт/падает\n"
            "• AO подтверждает\n"
            "• DI согласен\n"
            "• Свеча в направлении\n\n"
            "📊 5–10 сигналов в день.",
            MAIN_KEYBOARD
        )
        return

    if text == "📊 СИГНАЛЫ":
        send_message(
            chat_id,
            "📊 Signal 16.0 активен.\n\n"
            "SuperTrend + Stochastic + BBW + AO.\n"
            "Порог: 4/6 очков.",
            MAIN_KEYBOARD
        )
        return

    if text == "🔄 СКАНИРОВАТЬ":
        send_message(chat_id, "🔎 Скан...", MAIN_KEYBOARD)
        scan_market(force=True)
        return

    if text == "📈 СТАТУС":
        total = state["wins"] + state["losses"]
        wr = (state["wins"] / total * 100) if total else 0
        top_blocks = sorted(
            state["block_stats"].items(),
            key=lambda x: -x[1]
        )[:5]
        blocks_txt = "\n".join(f"• {k}: {v}" for k, v in top_blocks) or "—"
        send_message(
            chat_id,
            "📈 <b>СТАТУС</b>\n\n"
            f"Сигналов: {state['signals_sent']}\n"
            f"Wins: {state['wins']}\n"
            f"Losses: {state['losses']}\n"
            f"Winrate: {wr:.1f}%\n"
            f"Breakeven: 55.5%\n\n"
            f"Последний: {state['last_signal_symbol'] or '—'} "
            f"{state['last_signal_direction'] or ''} "
            f"({state['last_signal_score'] or '—'}/6)\n\n"
            f"<b>Топ блокировок:</b>\n{blocks_txt}",
            MAIN_KEYBOARD
        )
        return

    if text == "/win" or text == "✅ WIN":
        state["wins"] += 1
        state["series_results"].append("W")
        send_message(chat_id, "✅ WIN записан.", MAIN_KEYBOARD)
        return

    if text == "/loss" or text == "❌ LOSS":
        state["losses"] += 1
        state["series_results"].append("L")
        send_message(chat_id, "❌ LOSS записан.", MAIN_KEYBOARD)
        return

    if text == "/reset":
        state["wins"] = 0
        state["losses"] = 0
        state["signals_sent"] = 0
        state["series_results"] = []
        state["block_stats"] = {}
        send_message(chat_id, "🔄 Сброшено.", MAIN_KEYBOARD)
        return


# =========================================================
# LOOPS
# =========================================================

def telegram_loop():
    offset = 0
    log("Pocket Signal 16.0 started")
    while True:
        try:
            result = telegram("getUpdates", {"timeout": 30, "offset": offset})
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue
                handle_command(message["chat"]["id"], message.get("text", ""))
        except Exception as e:
            log(f"Telegram ERROR: {e}")
            time.sleep(5)


def scanner_loop():
    while True:
        try:
            scan_market()
        except Exception as e:
            log(f"SCANNER ERROR: {e}")
        time.sleep(CHECK_EVERY_SECONDS)


# =========================================================
# HEALTH
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Pocket Signal 16.0 is running.")

    def log_message(self, format, *args):
        return


def health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log(f"Health server on {port}")
    server.serve_forever()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    threading.Thread(target=health_server, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    scanner_loop()




