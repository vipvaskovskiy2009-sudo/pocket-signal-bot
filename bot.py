import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import yfinance as yf
import pandas as pd
import ta
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- НАСТРОЙКИ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

SYMBOLS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X"]  # Сканируем несколько пар
CHECK_EVERY_SECONDS = 60
COOLDOWN_MINUTES = 3           # Минимум между сигналами
EXPIRY_MINUTES = 1             # Экспирация опциона

# Фильтры
ADX_MIN = 20                   # Тренд должен быть сильным
ATR_MIN_PIPS = 0.0003          # Мин. волатильность (для EURUSD ~3 пипса)
RSI_LONG = 55                  # CALL только если RSI выше
RSI_SHORT = 45                 # PUT только если RSI ниже
BB_PROXIMITY = 0.15            # Не входить в 15% от границы Боллинджера

# Мартингейл
BASE_AMOUNT = 1.0
MARTINGALE_MULT = 2.2
MAX_STEPS = 3

# --- СОСТОЯНИЕ ---
state = {
    "martingale_step": 0,
    "current_amount": BASE_AMOUNT,
    "last_signal_time": None,
    "last_signal_direction": None,
    "wins": 0,
    "losses": 0,
    "series_results": [],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
bot = Bot(token=TELEGRAM_TOKEN)


# ---------- ДАННЫЕ И ИНДИКАТОРЫ ----------

def fetch_data(symbol: str, interval: str = "1m", hours: int = 6):
    """Загружает свечи с Yahoo Finance."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        df = yf.download(
            symbol, start=start, end=end,
            interval=interval, progress=False, auto_adjust=True
        )
    except Exception as e:
        logging.warning(f"{symbol}: ошибка загрузки — {e}")
        return None
    if df is None or df.empty or len(df) < 30:
        return None
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema9"] = ta.trend.EMAIndicator(df["Close"], window=9).ema_indicator()
    df["ema21"] = ta.trend.EMAIndicator(df["Close"], window=21).ema_indicator()
    df["ema50"] = ta.trend.EMAIndicator(df["Close"], window=50).ema_indicator()
    df["rsi"] = ta.momentum.RSIIndicator(df["Close"], window=14).rsi()
    df["adx"] = ta.trend.ADXIndicator(
        df["High"], df["Low"], df["Close"], window=14
    ).adx()
    df["atr"] = ta.volatility.AverageTrueRange(
        df["High"], df["Low"], df["Close"], window=14
    ).average_true_range()
    bb = ta.volatility.BollingerBands(df["Close"], window=20, window_dev=2)
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()
    return df.dropna()


def check_signal(df: pd.DataFrame):
    """Возвращает 'CALL', 'PUT' или None с пояснением."""
    last = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(last["Close"])
    candle_body = abs(float(last["Close"]) - float(last["Open"]))
    candle_range = float(last["High"]) - float(last["Low"])
    if candle_range == 0:
        return None, "нулевой диапазон свечи"

    # --- ФИЛЬТР 1: ADX (сила тренда) ---
    if float(last["adx"]) < ADX_MIN:
        return None, f"ADX {last['adx']:.1f} < {ADX_MIN} (боковик)"

    # --- ФИЛЬТР 2: ATR (волатильность) ---
    if float(last["atr"]) < ATR_MIN_PIPS:
        return None, "низкая волатильность"

    # --- ФИЛЬТР 3: подтверждающая свеча (не дожи) ---
    if candle_body / candle_range < 0.5:
        return None, "слабая свеча (дожи)"

    # --- ФИЛЬТР 4: Bollinger Bands ---
    bb_range = float(last["bb_high"]) - float(last["bb_low"])
    if bb_range == 0:
        return None, "BB схлопнуты"
    bb_pos = (price - float(last["bb_low"])) / bb_range
    if bb_pos > (1 - BB_PROXIMITY) or bb_pos < BB_PROXIMITY:
        return None, f"цена у границы BB ({bb_pos:.2f})"

    # --- ЛОГИКА CALL ---
    cross_up = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
    trend_up_5m = last["Close"] > last["ema50"]  # грубый фильтр тренда
    if (cross_up
            and float(last["rsi"]) > RSI_LONG
            and trend_up_5m
            and float(last["rsi"]) < 75):  # не покупаем перекупленность
        return "CALL", f"EMA cross + RSI {last['rsi']:.1f} + ADX {last['adx']:.1f}"

    # --- ЛОГИКА PUT ---
    cross_down = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]
    trend_down_5m = last["Close"] < last["ema50"]
    if (cross_down
            and float(last["rsi"]) < RSI_SHORT
            and trend_down_5m
            and float(last["rsi"]) > 25):
        return "PUT", f"EMA cross + RSI {last['rsi']:.1f} + ADX {last['adx']:.1f}"

    return None, None


# ---------- ОТПРАВКА ----------

async def send_telegram(text: str):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Telegram error: {e}")


async def send_signal(symbol: str, direction: str, price: float, reason: str):
    emoji = "🟢" if direction == "CALL" else "🔴"
    step = state["martingale_step"]
    amount = state["current_amount"]
    msg = (
        f"{emoji} <b>{direction}</b> | {symbol}\n"
        f"Цена: <code>{price:.5f}</code>\n"
        f"Ставка: <b>${amount:.2f}</b> (шаг {step}/{MAX_STEPS})\n"
        f"Экспирация: {EXPIRY_MINUTES} мин\n"
        f"Причина: {reason}\n"
        f"Время: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC\n\n"
        f"После сделки: /win или /loss"
    )
    await send_telegram(msg)
    logging.info(f"Сигнал: {direction} {symbol} @ {price:.5f}")


# ---------- ЛОГИКА ЦИКЛА ----------

def can_send_signal() -> bool:
    last = state["last_signal_time"]
    if last is None:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= COOLDOWN_MINUTES * 60


async def scan_market():
    if not can_send_signal():
        return

    for symbol in SYMBOLS:
        df = fetch_data(symbol)
        if df is None:
            continue
        try:
            df = add_indicators(df)
        except Exception as e:
            logging.warning(f"{symbol}: индикаторы — {e}")
            continue

        direction, reason = check_signal(df)
        if direction:
            price = float(df["Close"].iloc[-1])
            state["last_signal_time"] = datetime.now(timezone.utc)
            state["last_signal_direction"] = direction
            await send_signal(symbol, direction, price, reason)
            return  # один сигнал за цикл — не спамим


async def main_loop():
    logging.info("Бот запущен. Сканирую рынок...")
    while True:
        try:
            await scan_market()
        except Exception as e:
            logging.error(f"Ошибка в цикле: {e}")
        await asyncio.sleep(CHECK_EVERY_SECONDS)


# ---------- КОМАНДЫ TELEGRAM ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот сигналов запущен.\n"
        "/status — текущее состояние\n"
        "/win — предыдущая сделка в плюс\n"
        "/loss — предыдущая сделка в минус\n"
        "/reset — сбросить серию"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = state
    total = s["wins"] + s["losses"]
    wr = (s["wins"] / total * 100) if total else 0
    await update.message.reply_text(
        f"Шаг мартингейла: {s['martingale_step']}/{MAX_STEPS}\n"
        f"Текущая ставка: ${s['current_amount']:.2f}\n"
        f"Wins: {s['wins']} | Losses: {s['losses']} | Winrate: {wr:.1f}%"
    )


async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["wins"] += 1
    state["series_results"].append("W")
    state["martingale_step"] = 0
    state["current_amount"] = BASE_AMOUNT
    await update.message.reply_text(
        f"✅ Плюс. Серия сброшена. Ставка: ${BASE_AMOUNT:.2f}"
    )


async def cmd_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["losses"] += 1
    state["series_results"].append("L")
    if state["martingale_step"] >= MAX_STEPS:
        state["martingale_step"] = 0
        state["current_amount"] = BASE_AMOUNT
        await update.message.reply_text(
            f"⛔ Лимит {MAX_STEPS} минусов. Серия сброшена. Ставка: ${BASE_AMOUNT:.2f}"
        )
    else:
        state["martingale_step"] += 1
        state["current_amount"] = BASE_AMOUNT * (MARTINGALE_MULT ** state["martingale_step"])
        await update.message.reply_text(
            f"❌ Минус. Шаг {state['martingale_step']}/{MAX_STEPS}. "
            f"Ставка: ${state['current_amount']:.2f}"
        )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["martingale_step"] = 0
    state["current_amount"] = BASE_AMOUNT
    state["wins"] = 0
    state["losses"] = 0
    state["series_results"] = []
    await update.message.reply_text("🔄 Всё сброшено.")


# ---------- ЗАПУСК ----------

async def run():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("win", cmd_win))
    app.add_handler(CommandHandler("loss", cmd_loss))
    app.add_handler(CommandHandler("reset", cmd_reset))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        await main_loop()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
