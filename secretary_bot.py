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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
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
dp = Dispatcher(storage=MemoryStorage())


class KeywordSetup(StatesGroup):
    waiting_keyword = State()
    confirm_keyword = State()
    waiting_reply = State()
    confirm_reply = State()


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
            media_type TEXT,
            file_id TEXT,
            ts TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat ON messages(chat_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            reply_text TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            chat_id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL,
            chat_name TEXT
        )
    """)
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


async def get_or_create_topic(chat_id: int, chat_name: str) -> int:
    conn = db()
    row = conn.execute("SELECT topic_id FROM topics WHERE chat_id = ?", (chat_id,)).fetchone()
    if row:
        conn.close()
        return row[0]

    topic = await bot.create_forum_topic(chat_id=BACKUP_GROUP_ID, name=chat_name[:128])
    conn.execute(
        "INSERT INTO topics (chat_id, topic_id, chat_name) VALUES (?, ?, ?)",
        (chat_id, topic.message_thread_id, chat_name),
    )
    conn.commit()
    conn.close()
    return topic.message_thread_id


@dp.business_connection()
async def on_business_connection(conn: BusinessConnection):
    logging.info(f"Business connection: {conn.id} enabled={conn.is_enabled}")


@dp.business_message()
async def on_business_message(message: Message):
    text = message.text or message.caption or ""

    media_type = None
    file_id = None
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.voice:
        media_type = "voice"
        file_id = message.voice.file_id
    elif message.audio:
        media_type = "audio"
        file_id = message.audio.file_id
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
    elif message.video_note:
        media_type = "video_note"
        file_id = message.video_note.file_id
    elif message.sticker:
        media_type = "sticker"
        file_id = message.sticker.file_id
    elif message.animation:
        media_type = "animation"
        file_id = message.animation.file_id

    conn = db()
    conn.execute(
        "INSERT INTO messages (chat_id, chat_name, sender_id, sender_name, sender_username, text, media_type, file_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message.chat.id,
            chat_display_name(message),
            message.from_user.id if message.from_user else None,
            sender_display_name(message),
            message.from_user.username if message.from_user else None,
            text,
            media_type,
            file_id,
            message.date.isoformat() if message.date else datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    try:
        thread_id = await get_or_create_topic(message.chat.id, chat_display_name(message))
    except Exception as e:
        logging.error(f"Failed to get/create topic: {e}")
        thread_id = None

    sender_label = sender_display_name(message) + (
        f" (@{message.from_user.username})" if message.from_user and message.from_user.username else ""
    )

    if media_type and file_id:
        caption = f"از: {sender_label}" + (f"\n{text}" if text else "")
        send_map = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "voice": bot.send_voice,
            "audio": bot.send_audio,
            "document": bot.send_document,
            "animation": bot.send_animation,
        }
        try:
            if thread_id is None:
                pass
            elif media_type == "video_note":
                await bot.send_video_note(chat_id=BACKUP_GROUP_ID, video_note=file_id, message_thread_id=thread_id)
                await bot.send_message(chat_id=BACKUP_GROUP_ID, text=caption, message_thread_id=thread_id)
            elif media_type == "sticker":
                await bot.send_sticker(chat_id=BACKUP_GROUP_ID, sticker=file_id, message_thread_id=thread_id)
                await bot.send_message(chat_id=BACKUP_GROUP_ID, text=caption, message_thread_id=thread_id)
            else:
                await send_map[media_type](
                    chat_id=BACKUP_GROUP_ID, **{media_type: file_id}, caption=caption, message_thread_id=thread_id
                )
        except Exception as e:
            logging.error(f"Failed to relay media: {e}")
    elif text and thread_id is not None:
        try:
            await bot.send_message(
                chat_id=BACKUP_GROUP_ID,
                text=f"از: {sender_label}\n{text}",
                message_thread_id=thread_id,
            )
        except Exception as e:
            logging.error(f"Failed to relay text: {e}")

    if text and (not message.from_user or message.from_user.id != OWNER_ID):
        kconn = db()
        krows = kconn.execute("SELECT keyword, reply_text FROM keywords").fetchall()
        kconn.close()
        for keyword, reply_text in krows:
            if keyword.lower() in text.lower():
                try:
                    await bot.send_message(
                        chat_id=message.chat.id,
                        text=reply_text,
                        business_connection_id=message.business_connection_id,
                    )
                except Exception as e:
                    logging.error(f"Failed to send keyword auto-reply: {e}")
                break




def is_owner_in_group(message: Message) -> bool:
    return message.chat.id == BACKUP_GROUP_ID and message.from_user.id == OWNER_ID


@dp.message(F.text == "تنظیم کیورد")
async def start_keyword_setup(message: Message, state: FSMContext):
    if not is_owner_in_group(message):
        return
    prompt = await message.answer("پیامی که می‌خوای کیورد باشه رو روی همین پیام ریپلای کن.")
    await state.set_state(KeywordSetup.waiting_keyword)
    await state.update_data(prompt_id=prompt.message_id)


@dp.message(KeywordSetup.waiting_keyword)
async def receive_keyword(message: Message, state: FSMContext):
    if not is_owner_in_group(message):
        return
    data = await state.get_data()
    if not message.reply_to_message or message.reply_to_message.message_id != data.get("prompt_id"):
        return
    if not message.text:
        await message.answer("فقط متن قابل قبوله.")
        return

    await state.update_data(keyword=message.text)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ تایید", callback_data="kw_confirm_keyword"),
        InlineKeyboardButton(text="❌ رد", callback_data="kw_cancel"),
    )
    await message.answer(
        f"این کیورد تنظیم بشه؟\n«{message.text}»",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(KeywordSetup.confirm_keyword)


@dp.callback_query(F.data == "kw_confirm_keyword", KeywordSetup.confirm_keyword)
async def confirm_keyword(callback: CallbackQuery, state: FSMContext):
    prompt = await callback.message.answer("حالا پیامی که می‌خوای در جواب این کیورد ارسال بشه رو روی همین پیام ریپلای کن.")
    await state.update_data(prompt_id=prompt.message_id)
    await state.set_state(KeywordSetup.waiting_reply)
    await callback.answer()


@dp.message(KeywordSetup.waiting_reply)
async def receive_reply_text(message: Message, state: FSMContext):
    if not is_owner_in_group(message):
        return
    data = await state.get_data()
    if not message.reply_to_message or message.reply_to_message.message_id != data.get("prompt_id"):
        return
    if not message.text:
        await message.answer("فقط متن قابل قبوله.")
        return

    await state.update_data(reply_text=message.text)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ تایید", callback_data="kw_confirm_reply"),
        InlineKeyboardButton(text="❌ رد", callback_data="kw_cancel"),
    )
    await message.answer(
        f"کیورد: «{data.get('keyword')}»\nپاسخ: «{message.text}»\nتایید می‌کنی؟",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(KeywordSetup.confirm_reply)


@dp.callback_query(F.data == "kw_confirm_reply", KeywordSetup.confirm_reply)
async def confirm_reply(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    conn = db()
    conn.execute(
        "INSERT INTO keywords (keyword, reply_text) VALUES (?, ?)",
        (data.get("keyword"), data.get("reply_text")),
    )
    conn.commit()
    conn.close()
    await state.clear()
    await callback.message.answer("اتومیشن تنظیم شد ✅")
    await callback.answer()


@dp.callback_query(F.data == "kw_cancel")
async def cancel_keyword_setup(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("لغو شد ❌")
    await callback.answer()


def build_keyword_list_markup(rows):
    builder = InlineKeyboardBuilder()
    for kw_id, keyword, _ in rows:
        builder.row(InlineKeyboardButton(text=keyword, callback_data=f"kwv:{kw_id}"))
    return builder.as_markup()


def build_keyword_item_markup(kw_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑 حذف کیورد", callback_data=f"kwdel:{kw_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ برگشت", callback_data="kwback"))
    return builder.as_markup()


@dp.message(F.text == "لیست کیورد")
async def list_keywords(message: Message):
    if not is_owner_in_group(message):
        return

    conn = db()
    rows = conn.execute("SELECT id, keyword, reply_text FROM keywords ORDER BY id").fetchall()
    conn.close()

    if not rows:
        await message.answer("هنوز کیوردی ثبت نشده.")
        return

    await message.answer("کیورد مورد نظر رو انتخاب کن:", reply_markup=build_keyword_list_markup(rows))


@dp.callback_query(F.data.startswith("kwv:"))
async def on_keyword_view(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return

    kw_id = int(callback.data.split(":", 1)[1])

    conn = db()
    row = conn.execute("SELECT id, keyword, reply_text FROM keywords WHERE id = ?", (kw_id,)).fetchone()
    conn.close()

    if not row:
        await callback.answer("این کیورد دیگه وجود نداره.", show_alert=True)
        return

    _, keyword, reply_text = row
    await callback.message.edit_text(
        f"کیورد: «{keyword}»\nپاسخ: «{reply_text}»",
        reply_markup=build_keyword_item_markup(kw_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("kwdel:"))
async def on_keyword_delete(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return

    kw_id = int(callback.data.split(":", 1)[1])

    conn = db()
    conn.execute("DELETE FROM keywords WHERE id = ?", (kw_id,))
    conn.commit()
    rows = conn.execute("SELECT id, keyword, reply_text FROM keywords ORDER BY id").fetchall()
    conn.close()

    if not rows:
        await callback.message.edit_text("هنوز کیوردی ثبت نشده.")
    else:
        await callback.message.edit_text("کیورد مورد نظر رو انتخاب کن:", reply_markup=build_keyword_list_markup(rows))
    await callback.answer("حذف شد ✅")


@dp.callback_query(F.data == "kwback")
async def on_keyword_back(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return

    conn = db()
    rows = conn.execute("SELECT id, keyword, reply_text FROM keywords ORDER BY id").fetchall()
    conn.close()

    if not rows:
        await callback.message.edit_text("هنوز کیوردی ثبت نشده.")
    else:
        await callback.message.edit_text("کیورد مورد نظر رو انتخاب کن:", reply_markup=build_keyword_list_markup(rows))
    await callback.answer()


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
        "SELECT chat_name, sender_name, sender_username, text, media_type, ts FROM messages WHERE chat_id = ? ORDER BY ts",
        (chat_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await callback.answer("پیامی یافت نشد.", show_alert=True)
        return

    chat_name = rows[0][0]
    lines = [f"===== {chat_name} (ID: {chat_id}) ====="]
    for _, sender_name, sender_username, text, media_type, ts in rows:
        try:
            ts_fmt = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            ts_fmt = ts
        uname = f" (@{sender_username})" if sender_username else ""
        content = f"[{media_type}] {text}".strip() if media_type else text
        lines.append(f"[{ts_fmt}] {sender_name}{uname}: {content}")

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
