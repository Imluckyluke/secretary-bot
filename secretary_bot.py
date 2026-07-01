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
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BusinessConnection,
    FSInputFile,
    CallbackQuery,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
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
            sender_username TEXT,
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
        "INSERT INTO messages (chat_id, chat_name, sender_id, sender_name, sender_username, text, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            message.chat.id,
            chat_display_name(message),
            message.from_user.id if message.from_user else None,
            sender_display_name(message),
            message.from_user.username if message.from_user else None,
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
    if message.chat.id != BACKUP_GROUP_ID:
        return

    conn = db()
    chats = conn.execute(
        "SELECT DISTINCT chat_id, chat_name FROM messages ORDER BY chat_name COLLATE NOCASE"
    ).fetchall()
    conn.close()

    if not chats:
        await message.answer("هنوز پیامی ذخیره نشده.")
        return

    builder = InlineKeyboardBuilder()
    for chat_id, chat_name in chats:
        builder.row(InlineKeyboardButton(text=chat_name, callback_data=f"backup:{chat_id}"))

    await message.answer("کاربر مورد نظر رو انتخاب کن:", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("backup:"))
async def on_backup_click(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return

    chat_id = int(callback.data.split(":", 1)[1])

    conn = db()
    rows = conn.execute(
        "SELECT chat_name, sender_name, sender_username, text, ts FROM messages WHERE chat_id = ? ORDER BY ts",
        (chat_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await callback.answer("پیامی یافت نشد.", show_alert=True)
        return

    chat_name = rows[0][0]
    lines = [f"===== {chat_name} (ID: {chat_id}) ====="]
    for _, sender_name, sender_username, text, ts in rows:
        try:
            ts_fmt = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            ts_fmt = ts
        uname = f" (@{sender_username})" if sender_username else ""
        lines.append(f"[{ts_fmt}] {sender_name}{uname}: {text}")

    safe_name = "".join(c for c in chat_name if c.isalnum() or c in " _-").strip() or str(chat_id)
    filename = f"backup_{safe_name}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    filepath = f"/tmp/{filename}"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    await bot.send_document(
        chat_id=BACKUP_GROUP_ID,
        document=FSInputFile(filepath, filename=filename),
        caption=f"بکاپ {chat_name} — {len(rows)} پیام",
    )
    await callback.answer("ارسال شد ✅")


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
