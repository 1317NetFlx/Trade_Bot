import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from data_sources import get_klines
from strategies import check_signals

logging.basicConfig(level=logging.INFO)

# Загружаем токен и настройки из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
INTERVAL = os.getenv("INTERVAL", "1h")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", 300))
  # опционально, чтобы бот слал только тебе

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

