import asyncio
import random
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, html
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command Start

# ⚠️ ВСТАВЬТЕ СЮДА ВАШ ТОКЕН ОТ BOTFATHER
BOT_TOKEN = "ВАШ_ТОКЕН_БОТА"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Список популярных OTC пар
OTC_PAIRS = [
    "EUR/USD (OTC)", "GBP/USD (OTC)", "USD/JPY (OTC)", 
    "AUD/USD (OTC)", "EUR/GBP (OTC)", "USD/CAD (OTC)"
]

# Создание клавиатуры для удобства
keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🤖 Получить сигнал OTC")],
        [KeyboardButton(text="📊 Доступные пары")]
    ],
    resize_keyboard=True
)

@dp.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer(
        f"Привет, {html.bold(message.from_user.first_name)}! 👋\n"
        f"Я бот, генерирующий торговые сигналы для OTC пар.\n\n"
        f"Нажми кнопку ниже, чтобы получить актуальный аналитический сигнал.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.message(lambda message: message.text == "📊 Доступные пары")
async def show_pairs(message: Message):
    pairs_list = "\n".join([f"• {pair}" for pair in OTC_PAIRS])
    await message.answer(f"📈 {html.bold('Анализируемые OTC пары:')}\n\n{pairs_list}", parse_mode="HTML")

@dp.message(lambda message: message.text == "🤖 Получить сигнал OTC")
async def generate_signal(message: Message):
    # Отправляем имитацию анализа
    status_msg = await message.answer("🔄 <i>Анализирую OTC рынок, сверяю индикаторы RSI и MACD...</i>", parse_mode="HTML")
    await asyncio.sleep(1.5)  # Небольшая пауза для реалистичности
    
    # Случайный выбор пары и параметров
    pair = random.choice(OTC_PAIRS)
    direction = random.choice(["🟢 ВВЕРХ (CALL)", "🔴 ВНИЗ (PUT)"])
    timeframe = random.choice(["1 MIN", "2 MIN", "5 MIN"])
    accuracy = random.randint(78, 94) # Процент уверенности алгоритма
    
    # Время экспирации
    now = datetime.now()
    minutes_to_add = int(timeframe.split()[0])
    expiration_time = (now + timedelta(minutes=minutes_to_add)).strftime("%H:%M:%S")

    # Формируем красивый текст сигнала
    signal_text = (
        f"🎲 {html.bold('НОВЫЙ СИГНАЛ (OTC)')} 🎲\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Валютная пара: {html.code(pair)}\n"
        f"Направление: {html.bold(direction)}\n"
        f"Время действия: {html.bold(timeframe)}\n"
        f"Экспирация до: {html.code(expiration_time)}\n"
        f"Проходимость: {html.bold(f'{accuracy}%')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ {html.italic('Внимание: OTC-рынок имеет повышенные риски. Соблюдайте риск-менеджмент!')}"
    )
    
    # Удаляем сообщение об анализе и присылаем сигнал
    await status_msg.delete()
    await message.answer(signal_text, parse_mode="HTML")

async def main():
    print("Бот успешно запущен и выдает сигналы!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
