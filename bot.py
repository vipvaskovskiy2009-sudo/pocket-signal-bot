import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
SSID = os.getenv("PO_SSID", "ВАШ_SSID_ЗДЕСЬ")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Ваш Telegram ID

# Актив для торговли. ВАЖНО: правильный формат для API - EURUSD_otc (без слэшей и пробелов!)
ASSET = "EURUSD_otc" 
TIMEFRAME = 5  # 5-секундные свечи

# ================= ИНДИКАТОРЫ =================
def calc_ema(prices, period):
    if len(prices) < period: return None
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_bollinger(prices, period=20, std_dev=2):
    if len(prices) < period: return None, None, None
    sma = sum(prices[-period:]) / period
    variance = sum((x - sma) ** 2 for x in prices[-period:]) / period
    std = variance ** 0.5
    return sma - std_dev * std, sma, sma + std_dev * std

# ================= ЛОГИКА БОТА =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("PocketSignalBot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

prices = []
is_running = False
current_task = None

async def send_signal(signal_type, price):
    """Отправляет сигнал в Telegram"""
    emoji = "🟢 CALL (Вверх)" if signal_type == "CALL" else "🔴 PUT (Вниз)"
    text = (
        f"🚀 **СИГНАЛ!**\n\n"
        f"Актив: `{ASSET}`\n"
        f"Направление: {emoji}\n"
        f"Цена входа: `{price:.5f}`\n"
        f"Экспирация: 1 минута\n"
        f"Время: {datetime.now().strftime('%H:%M:%S')}"
    )
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
        logger.info(f"Сигнал отправлен: {signal_type} по цене {price}")
    except Exception as e:
        logger.error(f"Ошибка отправки в ТГ: {e}")

async def trading_loop():
    """Основной цикл подключения и анализа"""
    global prices, is_running
    
    logger.info(f"Подключение к Pocket Option для {ASSET}...")
    api = PocketOptionAsync(SSID)
    await asyncio.sleep(5) # Ждем соединения
    
    try:
        balance = await api.balance()
        await bot.send_message(ADMIN_ID, f"✅ Бот запущен!\nБаланс: ${balance:.2f}\nАктив: {ASSET}")
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"❌ Ошибка подключения: {e}")
        is_running = False
        return

    async for candle in api.subscribe_symbol_chunked(ASSET, TIMEFRAME):
        if not is_running:
            break
            
        price = float(candle.close)
        prices.append(price)
        if len(prices) > 100:
            prices.pop(0)

        # Ждем накопления истории для индикаторов
        if len(prices) < 30:
            continue

        # Считаем индикаторы
        ema_fast = calc_ema(prices, 9)
        ema_slow = calc_ema(prices, 21)
        rsi = calc_rsi(prices, 14)
        bb_lower, bb_mid, bb_upper = calc_bollinger(prices, 20, 2)

        if not all([ema_fast, ema_slow, rsi, bb_lower, bb_upper]):
            continue

        # ================= СТРАТЕГИЯ "КОНФЛЮЭНС" =================
        # Логика: Тренд (EMA) + Импульс (RSI) + Волатильность (BB)
        
        # Сигнал CALL: Тренд вверх, RSI не перекуплен, цена у нижней границы BB
        if ema_fast > ema_slow and rsi < 45 and price <= bb_lower:
            await send_signal("CALL", price)
            prices.clear() # Очищаем историю, чтобы не спамить сигналами
            await asyncio.sleep(60) # Пауза 1 минута после сигнала

        # Сигнал PUT: Тренд вниз, RSI не перепродан, цена у верхней границы BB
        elif ema_fast < ema_slow and rsi > 55 and price >= bb_upper:
            await send_signal("PUT", price)
            prices.clear()
            await asyncio.sleep(60)

# ================= КОМАНДЫ TELEGRAM =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="▶️ Запустить"), types.KeyboardButton(text="⏹ Остановить")],
            [types.KeyboardButton(text="📊 Статус")]
        ], resize_keyboard=True
    )
    await message.answer("🤖 Бот для сигналов Pocket Option (OTC) готов.", reply_markup=kb)

@dp.message(lambda msg: msg.text == "▶️ Запустить")
async def cmd_run(message: types.Message):
    global is_running, current_task
    if message.from_user.id != ADMIN_ID: return
    if is_running:
        return await message.answer("Бот уже работает.")
    
    is_running = True
    current_task = asyncio.create_task(trading_loop())
    await message.answer("🚀 Поиск сигналов запущен...")

@dp.message(lambda msg: msg.text == "⏹ Остановить")
async def cmd_stop(message: types.Message):
    global is_running, current_task
    if message.from_user.id != ADMIN_ID: return
    is_running = False
    if current_task:
        current_task.cancel()
    await message.answer("⏹ Бот остановлен.")

@dp.message(lambda msg: msg.text == "📊 Статус")
async def cmd_status(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    status = "🟢 Работает" if is_running else "🔴 Остановлен"
    await message.answer(f"Статус: {status}\nСобрано свечей: {len(prices)}")

async def main():
    logger.info("Запуск Telegram бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
