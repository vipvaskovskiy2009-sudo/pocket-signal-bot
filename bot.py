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
# POCKET SIGNAL 5.0 — 1-минутная экспирация
# Быстрые индикаторы, фильтр волатильности, блок ADX
# =========================================================


BOT_TOKEN = os.environ["BOT_TOKEN"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "AUD/USD": "AUDUSD=X",
}

CHECK_EVERY_SECONDS = 30          # сканируем каждые 30 сек
COOLDOWN_MINUTES = 3              # сигнал не чаще 1 раза в 3 мин

EXPIRY_SECONDS = 60               # экспирация 1 минута

# =========================================================
# STRATEGY SETTINGS — заточены под 1 минуту
# =========================================================

ADX_MIN = 20.0                    # блокирующий фильтр (боковик)
ATR_MIN_RATIO = 0.75              # ATR должен быть >= 75% от среднего

# Быстрые EMA для M1
EMA_FAST = 5
EMA_MID = 13
EMA_SLOW = 21

# Быстрый RSI
RSI_PERIOD = 7
RSI_CALL_MIN = 52.0
RSI_CALL_MAX = 72.0
RSI_PUT_MIN = 28.0
RSI_PUT_MAX = 48.0

# MACD (быстрый)
MACD_FAST = 5
MACD_SLOW = 13
MACD_SIGNAL = 4

# Bollinger
BB_PERIOD = 15

# Очки
MIN_SCORE = 4
MIN_DIRECTION_ADVANTAGE = 2

# Сессия (UTC)
SESSION_START_HOUR_UTC = 7
SESSION_END_HOUR_UTC = 20
TRADE_WEEKDAYS = {0, 1, 2, 3, 4}

# Исключаем минуты вокруг релизов (минуты 28–32 и 58–02 каждого часа)
# — там новостной шум, M1 сигналы особенно опасны
NEWS_BLACKOUT_MINUTES = {28, 29, 30, 31, 32, 58, 59, 0, 1, 2}

# =========================================================
# STATE
# =========================================================

FIXED_AMOUNT = 5.0

state = {
    "wins": 0,
    "losses": 0,
    "signals_today": 0,
    "last_signal_time": None,
    "last_signal_direction": None,
    "last_signal_symbol": None,
    "last_day_reset": None,
    "series_results": [],
}


# =========================================================
# LOGGING
# =========================================================

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
        headers={"User-Agent": "Mozilla/5.0 PocketSignal/5.0"}
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
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    return telegram("sendMessage", payload)


MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 СИГНАЛЫ"}, {"text": "📈 СТАТУС"}],
        [{"text": "🔄 СКАНИРОВАТЬ"}],
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
        log(f"{symbol}: DATA ERROR {e}")
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
    interval_seconds = 60 if interval == "1m" else 300 if interval == "5m" else 3600

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


def rsi(values, period=14):
    result = [None] * len(values)
    if len(values) <= period:
        return result
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        result[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return result


def macd(values, fast=12, slow=26, sig=9):
    ema_f = ema(values, fast)
    ema_s = ema(values, slow)
    line = [None] * len(values)
    for i in range(len(values)):
        if ema_f[i] is not None and ema_s[i] is not None:
            line[i] = ema_f[i] - ema_s[i]
    valid = [x for x in line if x is not None]
    sig_valid = ema(valid, sig)
    signal = [None] * len(values)
    pos = 0
    for i in range(len(values)):
        if line[i] is not None:
            signal[i] = sig_valid[pos]
            pos += 1
    hist = [None] * len(values)
    for i in range(len(values)):
        if line[i] is not None and signal[i] is not None:
            hist[i] = line[i] - signal[i]
    return line, signal, hist


def bollinger(values, period=20):
    middle = sma(values, period)
    upper = [None] * len(values)
    lower = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        mean = middle[i]
        var = sum((x - mean) ** 2 for x in window) / period
        dev = math.sqrt(var)
        upper[i] = mean + 2 * dev
        lower[i] = mean - 2 * dev
    return middle, upper, lower


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


def adx(candles, period=14):
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


# =========================================================
# SNAPSHOT
# =========================================================

def indicators(candles, fast_ema=True):
    closes = [x["close"] for x in candles]

    if fast_ema:
        e_fast = ema(closes, EMA_FAST)
        e_mid = ema(closes, EMA_MID)
        e_slow = ema(closes, EMA_SLOW)
        r = rsi(closes, RSI_PERIOD)
        m_line, m_sig, m_hist = macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        bb_mid, bb_hi, bb_lo = bollinger(closes, BB_PERIOD)
    else:
        e_fast = ema(closes, 9)
        e_mid = ema(closes, 21)
        e_slow = ema(closes, 50)
        r = rsi(closes, 14)
        m_line, m_sig, m_hist = macd(closes, 12, 26, 9)
        bb_mid, bb_hi, bb_lo = bollinger(closes, 20)

    adx_v, plus_di, minus_di = adx(candles, 14)
    atr_v = atr(candles, 14)

    return {
        "ema_fast": e_fast[-1],
        "ema_mid": e_mid[-1],
        "ema_slow": e_slow[-1],
        "rsi": r[-1],
        "macd": m_line[-1],
        "macd_signal": m_sig[-1],
        "macd_hist": m_hist[-1],
        "bb_mid": bb_mid[-1],
        "bb_high": bb_hi[-1],
        "bb_low": bb_lo[-1],
        "adx": adx_v[-1],
        "plus_di": plus_di[-1],
        "minus_di": minus_di[-1],
        "atr": atr_v[-1],
        "price": closes[-1]
    }


# =========================================================
# FILTERS
# =========================================================

def is_market_open():
    now = datetime.now(timezone.utc)
    if now.weekday() not in TRADE_WEEKDAYS:
        return False, f"выходной ({now.strftime('%A')})"
    if not (SESSION_START_HOUR_UTC <= now.hour < SESSION_END_HOUR_UTC):
        return False, f"вне сессии ({now.hour:02d} UTC)"
    if now.minute in NEWS_BLACKOUT_MINUTES:
        return False, f"новостное окно (: {now.minute:02d})"
    return True, "ok"


# =========================================================
# STRATEGY
# =========================================================

def check_strategy(m1, m5):
    if len(m1) < 60:
        return None
    if len(m5) < 30:
        return None

    a1 = indicators(m1, fast_ema=True)
    a5 = indicators(m5, fast_ema=False)

    values = list(a1.values()) + list(a5.values())
    if any(x is None for x in values):
        return None

    # ---------- БЛОК 1: ADX ----------
    if a1["adx"] < ADX_MIN:
        return {
            "direction": None,
            "block_reason": f"ADX {a1['adx']:.1f} < {ADX_MIN} (боковик)",
            "m1": a1, "m5": a5
        }

    # ---------- БЛОК 2: Волатильность (ATR) ----------
    # Сравниваем текущий ATR со средним за последние 20 M1 свечей
    recent_atr = []
    for i in range(max(15, len(m1) - 20), len(m1)):
        val = atr(m1[:i + 1], 14)[-1]
        if val is not None:
            recent_atr.append(val)
    if recent_atr:
        avg_atr = sum(recent_atr) / len(recent_atr)
        if avg_atr > 0 and a1["atr"] < avg_atr * ATR_MIN_RATIO:
            return {
                "direction": None,
                "block_reason": f"низкая волатильность (ATR {a1['atr']:.5f})",
                "m1": a1, "m5": a5
            }

    # ---------- БЛОК 3: M5 контекстный тренд ----------
    m5_up = a5["ema_fast"] > a5["ema_mid"] > a5["ema_slow"]
    m5_down = a5["ema_fast"] < a5["ema_mid"] < a5["ema_slow"]
    if not (m5_up or m5_down):
        return {
            "direction": None,
            "block_reason": "M5 без чёткого направления",
            "m1": a1, "m5": a5
        }

    # ---------- ОЧКИ ----------
    call_reasons, put_reasons = [], []
    call, put = 0, 0

    # EMA порядок M1
    if a1["ema_fast"] > a1["ema_mid"] > a1["ema_slow"]:
        call += 1
        call_reasons.append(f"M1 EMA {EMA_FAST}>{EMA_MID}>{EMA_SLOW}")
    if a1["ema_fast"] < a1["ema_mid"] < a1["ema_slow"]:
        put += 1
        put_reasons.append(f"M1 EMA {EMA_FAST}<{EMA_MID}<{EMA_SLOW}")

    # RSI M1
    if RSI_CALL_MIN <= a1["rsi"] < RSI_CALL_MAX:
        call += 1
        call_reasons.append(f"M1 RSI {a1['rsi']:.1f}")
    if RSI_PUT_MIN < a1["rsi"] <= RSI_PUT_MAX:
        put += 1
        put_reasons.append(f"M1 RSI {a1['rsi']:.1f}")

    # MACD hist M1
    if a1["macd_hist"] > 0 and a1["macd"] > a1["macd_signal"]:
        call += 1
        call_reasons.append("MACD bullish")
    if a1["macd_hist"] < 0 and a1["macd"] < a1["macd_signal"]:
        put += 1
        put_reasons.append("MACD bearish")

    # DI M1
    if a1["plus_di"] > a1["minus_di"]:
        call += 1
        call_reasons.append(f"+DI {a1['plus_di']:.1f}")
    if a1["minus_di"] > a1["plus_di"]:
        put += 1
        put_reasons.append(f"-DI {a1['minus_di']:.1f}")

    # Bollinger M1
    bb_rng = a1["bb_high"] - a1["bb_low"]
    if bb_rng > 0:
        pos = (a1["price"] - a1["bb_low"]) / bb_rng
        if 0.5 < pos < 0.88:
            call += 1
            call_reasons.append(f"BB {pos:.2f}")
        if 0.12 < pos < 0.5:
            put += 1
            put_reasons.append(f"BB {pos:.2f}")

    # M5 подтверждение направления
    if m5_up:
        call += 1
        call_reasons.append("M5 UP")
    if m5_down:
        put += 1
        put_reasons.append("M5 DOWN")

    # ---------- РЕШЕНИЕ ----------
    if call >= MIN_SCORE and call - put >= MIN_DIRECTION_ADVANTAGE:
        return {
            "direction": "CALL",
            "call": call, "put": put,
            "reasons": call_reasons,
            "m1": a1, "m5": a5, "block_reason": None
        }
    if put >= MIN_SCORE and put - call >= MIN_DIRECTION_ADVANTAGE:
        return {
            "direction": "PUT",
            "call": call, "put": put,
            "reasons": put_reasons,
            "m1": a1, "m5": a5, "block_reason": None
        }

    return {
        "direction": None,
        "call": call, "put": put,
        "reasons": [],
        "m1": a1, "m5": a5,
        "block_reason": f"C{call}/P{put} — нет перевеса"
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


def signal_message(name, result):
    direction = result["direction"]
    emoji = "🟢" if direction == "CALL" else "🔴"
    a1 = result["m1"]
    a5 = result["m5"]
    reasons = "\n".join("• " + x for x in result["reasons"][:8])

    return (
        f"{emoji} <b>{direction}</b>\n\n"
        f"💱 <b>{name}</b>\n"
        f"Цена: <code>{a1['price']:.5f}</code>\n\n"
        f"📊 Score: CALL {result['call']} / PUT {result['put']}\n\n"
        f"📈 M1:\n"
        f"RSI: {a1['rsi']:.1f} | ADX: {a1['adx']:.1f}\n"
        f"MACD hist: {a1['macd_hist']:.6f}\n"
        f"+DI/-DI: {a1['plus_di']:.1f}/{a1['minus_di']:.1f}\n\n"
        f"📊 M5: RSI {a5['rsi']:.1f} | ADX {a5['adx']:.1f}\n\n"
        f"🔎 <b>Подтверждения:</b>\n{reasons}\n\n"
        f"⏱ <b>Экспирация: 60 секунд</b>\n"
        f"💵 Paper: ${FIXED_AMOUNT:.2f}\n\n"
        f"⚠️ 1-минутка = шум. Винрейт ожидается 52–56%. "
        f"Breakeven при 80% выплате = 55.5%."
    )


# =========================================================
# SCAN
# =========================================================

LAST_CHAT_ID = None


def send_message_to_last_chat(text):
    global LAST_CHAT_ID
    if LAST_CHAT_ID is None:
        log("Нет Telegram chat_id")
        return
    try:
        send_message(LAST_CHAT_ID, text, MAIN_KEYBOARD)
    except Exception as e:
        log(f"Telegram send error: {e}")


def scan_market(force=False):
    if not force:
        ok, reason = is_market_open()
        if not ok:
            log(f"Пропуск: {reason}")
            return
        if not can_signal():
            return

    for name, symbol in SYMBOLS.items():
        try:
            m1 = get_market_data(symbol, "1m", "1d")
            if len(m1) < 60:
                log(f"{name}: мало M1 ({len(m1)})")
                continue

            m5 = get_market_data(symbol, "5m", "5d")
            if len(m5) < 30:
                log(f"{name}: мало M5 ({len(m5)})")
                continue

            result = check_strategy(m1, m5)
            if result is None:
                continue

            log(f"{name}: CALL={result['call']} PUT={result['put']} "
                f"block={result.get('block_reason')}")

            if result["direction"]:
                state["last_signal_time"] = datetime.now(timezone.utc)
                state["last_signal_direction"] = result["direction"]
                state["last_signal_symbol"] = name
                state["signals_today"] += 1
                send_message_to_last_chat(signal_message(name, result))
                return

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
            "⚡ <b>POCKET SIGNAL 5.0</b>\n\n"
            "<b>Режим:</b> 1-минутная экспирация\n"
            "<b>Пары:</b> " + ", ".join(SYMBOLS.keys()) + "\n"
            "<b>Сессия:</b> 07–20 UTC, Пн–Пт\n\n"
            "<b>Фильтры:</b>\n"
            "• ADX ≥ 20 (блок)\n"
            "• ATR ≥ 75% среднего (блок)\n"
            "• M5 тренд (блок)\n"
            "• Новостные окна исключены\n\n"
            "⚠️ 1-минутка = шум. Винрейт обычно 52–56%. "
            "Breakeven = 55.5%.",
            MAIN_KEYBOARD
        )
        return

    if text == "📊 СИГНАЛЫ":
        send_message(
            chat_id,
            "📊 Сканер активен.\n\n"
            "Проверка каждые 30 сек.\n"
            "Cooldown 3 мин между сигналами.\n"
            "Экспирация 60 секунд.",
            MAIN_KEYBOARD
        )
        return

    if text == "🔄 СКАНИРОВАТЬ":
        send_message(chat_id, "🔎 Принудительный скан...", MAIN_KEYBOARD)
        scan_market(force=True)
        return

    if text == "📈 СТАТУС":
        total = state["wins"] + state["losses"]
        wr = (state["wins"] / total * 100) if total else 0
        send_message(
            chat_id,
            "📈 <b>СТАТУС</b>\n\n"
            f"Paper: ${FIXED_AMOUNT:.2f}\n"
            f"Wins: {state['wins']}\n"
            f"Losses: {state['losses']}\n"
            f"Winrate: {wr:.1f}%\n"
            f"Breakeven: 55.5%\n\n"
            f"Последний: {state['last_signal_symbol'] or '—'} "
            f"{state['last_signal_direction'] or ''}",
            MAIN_KEYBOARD
        )
        return

    if text == "/win":
        state["wins"] += 1
        state["series_results"].append("W")
        send_message(chat_id, "✅ WIN записан.", MAIN_KEYBOARD)
        return

    if text == "/loss":
        state["losses"] += 1
        state["series_results"].append("L")
        send_message(chat_id, "❌ LOSS записан.", MAIN_KEYBOARD)
        return

    if text == "/reset":
        state["wins"] = 0
        state["losses"] = 0
        state["signals_today"] = 0
        state["series_results"] = []
        send_message(chat_id, "🔄 Сброшено.", MAIN_KEYBOARD)
        return


# =========================================================
# LOOPS
# =========================================================

def telegram_loop():
    offset = 0
    log("Pocket Signal 5.0 started (1-min mode)")
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
        self.wfile.write(b"Pocket Signal 5.0 (1-min) is running.")

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
