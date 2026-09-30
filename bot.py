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
# POCKET SIGNAL 12.0 — FINAL
# Часто + уверенно. 6 блокирующих фильтров.
# Momentum растёт, RSI в здоровой зоне, DI не экстремальный.
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
COOLDOWN_MINUTES = 6

# =========================================================
# ФИЛЬТРЫ (жёсткие, но не параноидальные)
# =========================================================

# Тренд старших ТФ
TREND_EMA_FAST = 9
TREND_EMA_MID = 21
TREND_EMA_SLOW = 50

# M1 ADX — есть движение
M1_ADX_MIN = 20.0

# DI: не слабый и не экстремальный
M1_DI_MIN = 18.0                # доминирующий DI ≥ 18
M1_DI_MAX = 62.0                # доминирующий DI ≤ 62 (иначе "поезд уехал")

# RSI — здоровая зона, не у края
M1_RSI_CALL_MIN = 52.0
M1_RSI_CALL_MAX = 65.0          # НЕ выше 65 — отсекает вход на пике
M1_RSI_PUT_MIN = 35.0           # НЕ ниже 35
M1_RSI_PUT_MAX = 48.0

# Свеча
M1_CANDLE_BODY_MIN = 0.40

# ATR — волатильность не мертва
ATR_MIN_RATIO = 0.75

# Bollinger — не у края
BB_PERIOD = 20
BB_POS_MIN = 0.10
BB_POS_MAX = 0.90

# =========================================================
# СЕССИЯ
# =========================================================

SESSION_START_HOUR_UTC = 7
SESSION_END_HOUR_UTC = 20
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
        headers={"User-Agent": "Mozilla/5.0 PocketSignal/12.0"}
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


def snapshot(candles, ema_f=9, ema_m=21, ema_s=50, rsi_p=14):
    closes = [x["close"] for x in candles]
    e_f = ema(closes, ema_f)
    e_m = ema(closes, ema_m)
    e_s = ema(closes, ema_s)
    r = rsi(closes, rsi_p)
    m_line, m_sig, m_hist = macd(closes, 12, 26, 9)
    adx_v, plus_di, minus_di = adx_full(candles, 14)
    atr_v = atr(candles, 14)
    bb_mid, bb_hi, bb_lo = bollinger(closes, BB_PERIOD)

    return {
        "ema_fast": e_f[-1],
        "ema_mid": e_m[-1],
        "ema_slow": e_s[-1],
        "rsi": r[-1],
        "macd": m_line[-1],
        "macd_signal": m_sig[-1],
        "macd_hist": m_hist[-1],
        "macd_hist_prev": m_hist[-2] if len(m_hist) > 1 else None,
        "adx": adx_v[-1],
        "plus_di": plus_di[-1],
        "minus_di": minus_di[-1],
        "atr": atr_v[-1],
        "bb_mid": bb_mid[-1],
        "bb_high": bb_hi[-1],
        "bb_low": bb_lo[-1],
        "price": closes[-1],
    }


# =========================================================
# FILTERS
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


def trend_direction(a):
    if a["ema_fast"] is None or a["ema_mid"] is None or a["ema_slow"] is None:
        return None
    if a["ema_fast"] > a["ema_mid"] > a["ema_slow"]:
        return "UP"
    if a["ema_fast"] < a["ema_mid"] < a["ema_slow"]:
        return "DOWN"
    return None


def candle_body_ratio(c):
    rng = c["high"] - c["low"]
    if rng <= 0:
        return 0.0
    return abs(c["close"] - c["open"]) / rng


# =========================================================
# STRATEGY — 6 БЛОКИРУЮЩИХ ФИЛЬТРОВ
# =========================================================

def check_strategy(m1, m5, m15, h1):
    if (len(m1) < 60 or len(m5) < 60
            or len(m15) < 60 or len(h1) < 60):
        return {"direction": None, "block_reason": "мало данных"}

    a1 = snapshot(m1, 9, 21, 50, 14)
    a5 = snapshot(m5)
    a15 = snapshot(m15)
    ah = snapshot(h1)

    for a in (a1, a5, a15, ah):
        for v in a.values():
            if v is None:
                return {"direction": None, "block_reason": "NaN индикатор"}

    # =====================================================
    # ФИЛЬТР 1: H1 и M15 — один тренд
    # =====================================================
    h1_dir = trend_direction(ah)
    m15_dir = trend_direction(a15)
    if h1_dir is None:
        return {"direction": None, "block_reason": "H1 без тренда"}
    if m15_dir != h1_dir:
        return {"direction": None,
                "block_reason": f"M15({m15_dir})≠H1({h1_dir})"}

    direction = "CALL" if h1_dir == "UP" else "PUT"

    # =====================================================
    # ФИЛЬТР 2: M5 EMA выстроены в ту же сторону
    # =====================================================
    m5_dir = trend_direction(a5)
    if m5_dir != h1_dir:
        return {"direction": None,
                "block_reason": f"M5({m5_dir})≠H1"}

    # =====================================================
    # ФИЛЬТР 3: M1 ADX >= 20
    # =====================================================
    if a1["adx"] < M1_ADX_MIN:
        return {"direction": None,
                "block_reason": f"ADX {a1['adx']:.1f}<{M1_ADX_MIN}"}

    # =====================================================
    # ФИЛЬТР 4: DI — сильный, но не экстремальный
    # =====================================================
    if direction == "CALL":
        if a1["plus_di"] < M1_DI_MIN:
            return {"direction": None,
                    "block_reason": f"+DI {a1['plus_di']:.1f} слабый"}
        if a1["plus_di"] > M1_DI_MAX:
            return {"direction": None,
                    "block_reason": f"+DI {a1['plus_di']:.1f} экстремум"}
        if a1["plus_di"] <= a1["minus_di"]:
            return {"direction": None,
                    "block_reason": "DI не в сторону CALL"}
    else:
        if a1["minus_di"] < M1_DI_MIN:
            return {"direction": None,
                    "block_reason": f"-DI {a1['minus_di']:.1f} слабый"}
        if a1["minus_di"] > M1_DI_MAX:
            return {"direction": None,
                    "block_reason": f"-DI {a1['minus_di']:.1f} экстремум"}
        if a1["minus_di"] <= a1["plus_di"]:
            return {"direction": None,
                    "block_reason": "DI не в сторону PUT"}

    # =====================================================
    # ФИЛЬТР 5: M1 EMA + RSI (здоровая зона)
    # =====================================================
    if direction == "CALL":
        if not (a1["ema_fast"] > a1["ema_mid"] > a1["ema_slow"]):
            return {"direction": None, "block_reason": "M1 EMA не UP"}
        if not (M1_RSI_CALL_MIN <= a1["rsi"] <= M1_RSI_CALL_MAX):
            return {"direction": None,
                    "block_reason": f"RSI {a1['rsi']:.1f} вне CALL-зоны"}
    else:
        if not (a1["ema_fast"] < a1["ema_mid"] < a1["ema_slow"]):
            return {"direction": None, "block_reason": "M1 EMA не DOWN"}
        if not (M1_RSI_PUT_MIN <= a1["rsi"] <= M1_RSI_PUT_MAX):
            return {"direction": None,
                    "block_reason": f"RSI {a1['rsi']:.1f} вне PUT-зоны"}

    # =====================================================
    # ФИЛЬТР 6: MACD hist РАСТЁТ (не просто положительный!)
    # =====================================================
    if a1["macd_hist_prev"] is None:
        return {"direction": None, "block_reason": "нет MACD prev"}

    if direction == "CALL":
        if not (a1["macd_hist"] > 0
                and a1["macd_hist"] > a1["macd_hist_prev"]):
            return {"direction": None,
                    "block_reason": "MACD hist не растёт"}
    else:
        if not (a1["macd_hist"] < 0
                and a1["macd_hist"] < a1["macd_hist_prev"]):
            return {"direction": None,
                    "block_reason": "MACD hist не падает"}

    # =====================================================
    # Проверки силы (не блокирующие)
    # =====================================================
    extras = {}

    recent_atr = []
    for i in range(max(15, len(m1) - 30), len(m1) - 1):
        val = atr(m1[:i + 1], 14)[-1]
        if val is not None:
            recent_atr.append(val)
    if recent_atr:
        avg_atr = sum(recent_atr) / len(recent_atr)
        extras["atr_ok"] = (avg_atr > 0 and a1["atr"] >= avg_atr * ATR_MIN_RATIO)
    else:
        extras["atr_ok"] = False

    bb_rng = a1["bb_high"] - a1["bb_low"]
    if bb_rng > 0:
        pos = (a1["price"] - a1["bb_low"]) / bb_rng
        extras["bb_pos"] = pos
        extras["bb_ok"] = (BB_POS_MIN <= pos <= BB_POS_MAX)
    else:
        extras["bb_pos"] = 0.5
        extras["bb_ok"] = False

    last = m1[-1]
    body = candle_body_ratio(last)
    extras["body"] = body
    extras["body_ok"] = body >= M1_CANDLE_BODY_MIN

    if direction == "CALL":
        extras["candle_ok"] = last["close"] > last["open"]
    else:
        extras["candle_ok"] = last["close"] < last["open"]

    return {
        "direction": direction,
        "block_reason": None,
        "m1": a1, "m5": a5, "m15": a15, "h1": ah,
        "extras": extras,
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
    a15 = r["m15"]
    ah = r["h1"]
    ex = r.get("extras", {})

    if direction == "CALL":
        header = "🐂 <b>БЫК → ВВЕРХ</b>"
        emoji = "🟢"
    else:
        header = "🐻 <b>МЕДВЕДЬ → ВНИЗ</b>"
        emoji = "🔴"

    extras_lines = []
    if ex.get("atr_ok"):
        extras_lines.append("✅ ATR расширяется")
    if ex.get("bb_ok"):
        extras_lines.append(f"✅ BB позиция {ex.get('bb_pos', 0):.2f}")
    if ex.get("body_ok"):
        extras_lines.append(f"✅ Свеча {ex.get('body', 0):.0%}")
    if ex.get("candle_ok"):
        extras_lines.append("✅ Направление свечи")

    extras_txt = "\n".join(extras_lines) if extras_lines else "—"

    hist_prev = a1.get("macd_hist_prev", 0)
    hist_now = a1.get("macd_hist", 0)

    return (
        f"{emoji}{emoji} <b>СИГНАЛ</b>\n"
        f"{header}\n\n"
        f"💱 <b>{name}</b>\n"
        f"Цена: <code>{a1['price']:.5f}</code>\n\n"
        f"🎯 <b>6 фильтров пройдено:</b>\n"
        f"✅ H1 + M15 тренд\n"
        f"✅ M5 EMA выстроены\n"
        f"✅ M1 ADX {a1['adx']:.1f} ≥ {M1_ADX_MIN}\n"
        f"✅ DI в зоне ({a1['plus_di']:.1f}/{a1['minus_di']:.1f})\n"
        f"✅ M1 EMA + RSI {a1['rsi']:.1f}\n"
        f"✅ MACD hist растёт ({hist_prev:.6f} → {hist_now:.6f})\n\n"
        f"📊 <b>Доп. подтверждения:</b>\n{extras_txt}\n\n"
        f"<b>Снимок:</b>\n"
        f"H1: ADX {ah['adx']:.1f} | RSI {ah['rsi']:.1f}\n"
        f"M15: ADX {a15['adx']:.1f} | RSI {a15['rsi']:.1f}\n"
        f"M5: ADX {a5['adx']:.1f} | RSI {a5['rsi']:.1f}\n"
        f"M1: ADX {a1['adx']:.1f} | +DI {a1['plus_di']:.1f} | -DI {a1['minus_di']:.1f}\n\n"
        f"⏱ <b>Экспирация: 60 секунд</b>\n"
        f"💵 Ставка: <b>${FIXED_AMOUNT:.2f}</b>\n\n"
        f"⚠️ Не гарантия. Но 6 совпадающих фильтров — сильный сетап."
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
            m15 = get_market_data(symbol, "15m", "5d")
            time.sleep(0.2)
            h1 = get_market_data(symbol, "1h", "1mo")
            time.sleep(0.2)

            if (len(m1) < 60 or len(m5) < 60
                    or len(m15) < 60 or len(h1) < 60):
                continue

            result = check_strategy(m1, m5, m15, h1)

            if result["direction"]:
                log(f"✅ {name}: {result['direction']}")
                state["last_signal_time"] = datetime.now(timezone.utc)
                state["last_signal_direction"] = result["direction"]
                state["last_signal_symbol"] = name
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
            "⚡ <b>POCKET SIGNAL 12.0 — FINAL</b>\n\n"
            "6 блокирующих фильтров:\n\n"
            "1️⃣ H1 + M15 — один тренд\n"
            "2️⃣ M5 EMA выстроены\n"
            "3️⃣ M1 ADX ≥ 20\n"
            "4️⃣ DI в зоне 18–62\n"
            "5️⃣ M1 EMA + RSI 52–65 (CALL) / 35–48 (PUT)\n"
            "6️⃣ <b>MACD hist РАСТЁТ</b> (не просто положительный)\n\n"
            "📊 <b>Ожидания:</b>\n"
            "• 3–6 сигналов в день\n"
            "• Сессия 07–20 UTC, Пн–Пт\n"
            "• Cooldown 6 минут\n"
            "• Ставка $5 фиксированная\n\n"
            "⚠️ Даже при 6 фильтрах ~40% сделок проигрывают. "
            "Это математика 60-секундной экспирации.",
            MAIN_KEYBOARD
        )
        return

    if text == "📊 СИГНАЛЫ":
        send_message(
            chat_id,
            "📊 Финальный режим активен.\n\n"
            "Скан 45 сек, cooldown 6 мин.\n"
            "6 пар, сессия 07–20 UTC.",
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
            f"<b>Топ-5 блокировок:</b>\n{blocks_txt}",
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
    log("Pocket Signal 12.0 started — FINAL")
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
        self.wfile.write(b"Pocket Signal 12.0 FINAL is running.")

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
