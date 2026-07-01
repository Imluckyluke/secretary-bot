"""
Telegram Secretary Bot (Business Mode) - Railway webhook deployment
- Connect via: Settings > Telegram Business > Chatbots > @your_bot_username
- Logs all business chat messages to SQLite, live.
- /backup sends a readable text backup (sorted by chat name/id) to BACKUP_GROUP_ID.
"""

import logging
import os
import sqlite3
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, BusinessConnection, FSInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
BACKUP_GROUP_ID = int(os.environ["BACKUP_GROUP_ID"])
WEBHOOK_PATH = "/webhook"
PORT = int(os.environ.get("PORT", 8080))

DB_PATH = "/data/backup.db" if os.path.isdir("/data") else "backup.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            chat_name TEXT,
            sender_id INTEGER,
            sender_name TEXT,
            text TEXT,
            ts TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat ON messages(chat_id)")
    return conn


def chat_display_name(message: Message) -> str:
    chat = message.chat
    if chat.title:
        return chat.title
    parts = [p for p in [chat.first_name, chat.last_name] if p]
    name = " ".join(parts) if parts else (chat.username or str(chat.id))
    return name


def sender_display_name(message: Message) -> str:
    u = message.from_user
    if not u:
        return "?"
    parts = [p for p in [u.first_name, u.last_name] if p]
    return " ".join(parts) if parts else (u.username or str(u.id))


@dp.business_connection()
async def on_business_connection(conn: BusinessConnection):
    logging.info(f"Business connection: {conn.id} enabled={conn.is_enabled}")


@dp.business_message()
async def on_business_message(message: Message):
    text = message.text or message.caption or ""
    conn = db()
    conn.execute(
        "INSERT INTO messages (chat_id, chat_name, sender_id, sender_name, text, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (
            message.chat.id,
            chat_display_name(message),
            message.from_user.id if message.from_user else None,
            sender_display_name(message),
            text,
            message.date.isoformat() if message.date else datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()




@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    conn = db()
    rows = conn.execute(
        "SELECT chat_id, chat_name, sender_name, text, ts FROM messages ORDER BY chat_name COLLATE NOCASE, chat_id, ts"
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer("هنوز پیامی ذخیره نشده.")
        return

    lines = []
    current_chat = None
    for chat_id, chat_name, sender_name, text, ts in rows:
        key = (chat_name, chat_id)
        if key != current_chat:
            if current_chat is not None:
                lines.append("")
            lines.append(f"===== {chat_name} (ID: {chat_id}) =====")
            current_chat = key
        try:
            ts_fmt = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            ts_fmt = ts
        lines.append(f"[{ts_fmt}] {sender_name}: {text}")

    filename = f"backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    filepath = f"/tmp/{filename}"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    await bot.send_document(
        chat_id=BACKUP_GROUP_ID,
        document=FSInputFile(filepath, filename=filename),
        caption=f"بکاپ چت‌ها — {len(rows)} پیام",
    )
    await message.answer("بکاپ ارسال شد ✅")


async def on_startup(app: web.Application):
    db().close()
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
