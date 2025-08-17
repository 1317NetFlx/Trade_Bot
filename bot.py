import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from data_sources import get_klines
from strategies import check_signals

logging.basicConfig(level=logging.INFO)

# Загружаем токен и настройки из переменных окружения
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
INTERVAL = os.getenv("INTERVAL", "1h")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", 300))
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # опционально, чтобы бот слал только тебе

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def start_command(message: types.Message):
    await message.answer("Привет! Я сигнальный бот 📊 Буду следить за рынком и кидать сигналы.")


async def signal_worker():
    while True:
        for symbol in SYMBOLS:
            try:
                data = get_klines(symbol, INTERVAL, limit=100)
                signals = check_signals(data, symbol)
                for sig in signals:
                    text = f"⚡ {symbol}: {sig}"
                    if CHAT_ID:
                        await bot.send_message(CHAT_ID, text)
                    else:
                        # fallback — если чат не указан, бот пишет сам себе
                        await bot.send_message((await bot.get_me()).id, text)
            except Exception as e:
                logging.error(f"Ошибка при обработке {symbol}: {e}")
        await asyncio.sleep(POLL_SECONDS)


async def main():
    asyncio.create_task(signal_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
