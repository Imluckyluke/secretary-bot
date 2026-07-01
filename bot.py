"""
Telegram Secretary Bot (Business Mode) - Railway webhook deployment
- Connect via: Settings > Telegram Business > Chatbots > @your_bot_username
- Auto-replies to your private chats when they message you.
"""

import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, BusinessConnection, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # e.g. https://your-app.up.railway.app
WEBHOOK_PATH = "/webhook"
PORT = int(os.environ.get("PORT", 8080))

AUTO_REPLY_TEXT = os.environ.get(
    "AUTO_REPLY_TEXT",
    "سلام 👋 در حال حاضر پاسخگو نیستم، به‌زودی جواب می‌دم.",
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

secretary_enabled = True


@dp.business_connection()
async def on_business_connection(conn: BusinessConnection):
    logging.info(f"Business connection: {conn.id} enabled={conn.is_enabled}")


@dp.business_message()
async def on_business_message(message: Message):
    if message.from_user.id == OWNER_ID:
        return
    if not secretary_enabled:
        return
    await bot.send_message(
        chat_id=message.chat.id,
        text=AUTO_REPLY_TEXT,
        business_connection_id=message.business_connection_id,
    )


@dp.message(Command("on"))
async def cmd_on(message: Message):
    global secretary_enabled
    if message.from_user.id != OWNER_ID:
        return
    secretary_enabled = True
    await message.answer("منشی فعال شد ✅")


@dp.message(Command("off"))
async def cmd_off(message: Message):
    global secretary_enabled
    if message.from_user.id != OWNER_ID:
        return
    secretary_enabled = False
    await message.answer("منشی غیرفعال شد ⛔")


async def on_startup(app: web.Application):
    await bot.set_webhook(f"{WEBHOOK_URL}{WEBHOOK_PATH}")
    logging.info("Webhook set")


async def on_shutdown(app: web.Application):
    await bot.delete_webhook()


def main():
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
