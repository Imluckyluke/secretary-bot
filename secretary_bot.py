"""
Telegram Secretary Bot (Business Mode) - Railway webhook deployment
- Connect via: Settings > Telegram Business > Chatbots > @your_bot_username
- Each connected business account gets its own group: add the bot to a new
  group from that account and it's auto-registered as that account's home group.
- Logs all business chat messages to SQLite, live, forwarded into per-chat topics.
- OWNER_ID is the supervisor account: in its own home group, /backup lets it
  pick any connected account, then any of its chats, and get a text backup.
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
    ChatMemberUpdated,
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
    msg_cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "business_connection_id" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN business_connection_id TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat ON messages(chat_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conn ON messages(business_connection_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            reply_text TEXT NOT NULL
        )
    """)
    kw_cols = [r[1] for r in conn.execute("PRAGMA table_info(keywords)").fetchall()]
    if "owner_user_id" not in kw_cols:
        conn.execute("ALTER TABLE keywords ADD COLUMN owner_user_id INTEGER")
        conn.execute("UPDATE keywords SET owner_user_id = ? WHERE owner_user_id IS NULL", (OWNER_ID,))

    conn.execute("""
        CREATE TABLE IF NOT EXISTS connections (
            business_connection_id TEXT PRIMARY KEY,
            user_id INTEGER,
            display_name TEXT,
            username TEXT,
            is_enabled INTEGER,
            connected_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS home_groups (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            chat_title TEXT,
            set_at TEXT
        )
    """)

    topic_cols = [r[1] for r in conn.execute("PRAGMA table_info(topics)").fetchall()]
    if topic_cols and "business_connection_id" not in topic_cols:
        conn.execute("ALTER TABLE topics RENAME TO topics_legacy")
        topic_cols = []
    if not topic_cols:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                home_chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                chat_name TEXT,
                UNIQUE(business_connection_id, chat_id)
            )
        """)
    elif "home_chat_id" not in topic_cols:
        conn.execute("ALTER TABLE topics ADD COLUMN home_chat_id INTEGER")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backup_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_connection_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            home_chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            chat_name TEXT,
            UNIQUE(business_connection_id, chat_id)
        )
    """)

    return conn


def is_self_account(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    conn = db()
    row = conn.execute("SELECT 1 FROM connections WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def connection_owner_user_id(business_connection_id: str):
    conn = db()
    row = conn.execute(
        "SELECT user_id FROM connections WHERE business_connection_id = ?",
        (business_connection_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_home_chat_id(user_id: int):
    conn = db()
    row = conn.execute("SELECT chat_id FROM home_groups WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def get_group_owner_user_id(chat_id: int):
    conn = db()
    row = conn.execute("SELECT user_id FROM home_groups WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def register_home_group(user_id: int, chat_id: int, chat_title: str):
    conn = db()
    conn.execute(
        """
        INSERT INTO home_groups (user_id, chat_id, chat_title, set_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, chat_title=excluded.chat_title, set_at=excluded.set_at
        """,
        (user_id, chat_id, chat_title, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def is_authorized_in_group(message: Message) -> bool:
    if not message.from_user:
        return False
    owner_user_id = get_group_owner_user_id(message.chat.id)
    return owner_user_id is not None and message.from_user.id == owner_user_id


def connection_display_name(business_connection_id: str) -> str:
    conn = db()
    row = conn.execute(
        "SELECT display_name, username FROM connections WHERE business_connection_id = ?",
        (business_connection_id,),
    ).fetchone()
    conn.close()
    if not row:
        return business_connection_id
    display_name, username = row
    return display_name or (f"@{username}" if username else business_connection_id)


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


async def get_or_create_topic(business_connection_id: str, chat_id: int, chat_name: str):
    owner_user_id = connection_owner_user_id(business_connection_id)
    home_chat_id = get_home_chat_id(owner_user_id) if owner_user_id else None
    if home_chat_id is None:
        return None, None

    conn = db()
    row = conn.execute(
        "SELECT topic_id, home_chat_id FROM topics WHERE business_connection_id = ? AND chat_id = ?",
        (business_connection_id, chat_id),
    ).fetchone()
    if row and row[1] == home_chat_id:
        conn.close()
        return row[0], home_chat_id

    acc_name = connection_display_name(business_connection_id)
    topic_name = f"{acc_name} — {chat_name}"[:128]
    topic = await bot.create_forum_topic(chat_id=home_chat_id, name=topic_name)
    conn.execute(
        """
        INSERT INTO topics (business_connection_id, chat_id, home_chat_id, topic_id, chat_name) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET
            home_chat_id=excluded.home_chat_id, topic_id=excluded.topic_id, chat_name=excluded.chat_name
        """,
        (business_connection_id, chat_id, home_chat_id, topic.message_thread_id, chat_name),
    )
    conn.commit()
    conn.close()
    return topic.message_thread_id, home_chat_id


async def get_or_create_backup_topic(business_connection_id: str, chat_id: int, chat_name: str):
    supervisor_chat_id = get_home_chat_id(OWNER_ID)
    if supervisor_chat_id is None:
        return None, None

    conn = db()
    row = conn.execute(
        "SELECT topic_id FROM backup_topics WHERE business_connection_id = ? AND chat_id = ? AND home_chat_id = ?",
        (business_connection_id, chat_id, supervisor_chat_id),
    ).fetchone()
    if row:
        conn.close()
        return row[0], supervisor_chat_id

    acc_name = connection_display_name(business_connection_id)
    topic_name = f"{acc_name} — {chat_name}"[:128]
    topic = await bot.create_forum_topic(chat_id=supervisor_chat_id, name=topic_name)
    conn.execute(
        """
        INSERT INTO backup_topics (business_connection_id, chat_id, home_chat_id, topic_id, chat_name) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET
            home_chat_id=excluded.home_chat_id, topic_id=excluded.topic_id, chat_name=excluded.chat_name
        """,
        (business_connection_id, chat_id, supervisor_chat_id, topic.message_thread_id, chat_name),
    )
    conn.commit()
    conn.close()
    return topic.message_thread_id, supervisor_chat_id


@dp.business_connection()
async def on_business_connection(business_conn: BusinessConnection):
    logging.info(f"Business connection: {business_conn.id} enabled={business_conn.is_enabled}")

    user = business_conn.user
    parts = [p for p in [user.first_name, user.last_name] if p]
    display_name = " ".join(parts) if parts else (user.username or str(user.id))

    conn = db()
    conn.execute(
        """
        INSERT INTO connections (business_connection_id, user_id, display_name, username, is_enabled, connected_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(business_connection_id) DO UPDATE SET
            user_id=excluded.user_id,
            display_name=excluded.display_name,
            username=excluded.username,
            is_enabled=excluded.is_enabled,
            connected_at=excluded.connected_at
        """,
        (
            business_conn.id,
            user.id,
            display_name,
            user.username,
            1 if business_conn.is_enabled else 0,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    notify_chat_id = get_home_chat_id(user.id) or get_home_chat_id(OWNER_ID)
    if notify_chat_id:
        try:
            await bot.send_message(
                chat_id=notify_chat_id,
                text=f"🔗 اکانت جدید وصل شد: {display_name}" + (f" (@{user.username})" if user.username else ""),
            )
        except Exception as e:
            logging.error(f"Failed to notify new connection: {e}")


@dp.my_chat_member()
async def on_bot_membership_change(event: ChatMemberUpdated):
    if event.chat.type not in ("group", "supergroup"):
        return

    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    just_joined = old_status in ("left", "kicked") and new_status in ("member", "administrator")
    if not just_joined:
        return

    adder = event.from_user
    if not adder:
        return

    if adder.id == OWNER_ID:
        register_home_group(OWNER_ID, event.chat.id, event.chat.title)
        await bot.send_message(
            chat_id=event.chat.id,
            text="✅ این گروه به‌عنوان گروه سوپروایزر (اکانت اصلی) ثبت شد.\nبرای بکاپ گرفتن از کیورد /backup استفاده کن.",
        )
        return

    conn = db()
    row = conn.execute("SELECT 1 FROM connections WHERE user_id = ?", (adder.id,)).fetchone()
    conn.close()

    if row:
        register_home_group(adder.id, event.chat.id, event.chat.title)
        await bot.send_message(
            chat_id=event.chat.id,
            text="✅ این گروه برای این اکانت ثبت شد. پیام‌ها اینجا به تفکیک تاپیک لاگ میشن و «تنظیم کیورد» / «لیست کیورد» اینجا کار می‌کنن.",
        )
    else:
        await bot.send_message(
            chat_id=event.chat.id,
            text="⚠️ این اکانت هنوز از تنظیمات بیزینس تلگرام به بات وصل نشده، برای همین این گروه ثبت نشد.",
        )


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
        "INSERT INTO messages (chat_id, chat_name, sender_id, sender_name, sender_username, text, media_type, file_id, ts, business_connection_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            message.business_connection_id,
        ),
    )
    conn.commit()
    conn.close()

    try:
        thread_id, home_chat_id = await get_or_create_topic(message.business_connection_id, message.chat.id, chat_display_name(message))
    except Exception as e:
        logging.error(f"Failed to get/create topic: {e}")
        thread_id, home_chat_id = None, None

    conn_owner_user_id = connection_owner_user_id(message.business_connection_id)
    if message.from_user and conn_owner_user_id and message.from_user.id == conn_owner_user_id:
        sender_label = "شما"
    else:
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
                await bot.send_video_note(chat_id=home_chat_id, video_note=file_id, message_thread_id=thread_id)
                await bot.send_message(chat_id=home_chat_id, text=caption, message_thread_id=thread_id)
            elif media_type == "sticker":
                await bot.send_sticker(chat_id=home_chat_id, sticker=file_id, message_thread_id=thread_id)
                await bot.send_message(chat_id=home_chat_id, text=caption, message_thread_id=thread_id)
            else:
                await send_map[media_type](
                    chat_id=home_chat_id, **{media_type: file_id}, caption=caption, message_thread_id=thread_id
                )
        except Exception as e:
            logging.error(f"Failed to relay media: {e}")
    elif text and thread_id is not None:
        try:
            await bot.send_message(
                chat_id=home_chat_id,
                text=f"از: {sender_label}\n{text}",
                message_thread_id=thread_id,
            )
        except Exception as e:
            logging.error(f"Failed to relay text: {e}")

    if text and (not message.from_user or not is_self_account(message.from_user.id)):
        owner_user_id = connection_owner_user_id(message.business_connection_id)
        kconn = db()
        krows = kconn.execute(
            "SELECT keyword, reply_text FROM keywords WHERE owner_user_id = ?", (owner_user_id,)
        ).fetchall()
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




@dp.message(F.text == "تنظیم کیورد")
async def start_keyword_setup(message: Message, state: FSMContext):
    if not is_authorized_in_group(message):
        return
    prompt = await message.answer("پیامی که می‌خوای کیورد باشه رو روی همین پیام ریپلای کن.")
    await state.set_state(KeywordSetup.waiting_keyword)
    await state.update_data(prompt_id=prompt.message_id)


@dp.message(KeywordSetup.waiting_keyword)
async def receive_keyword(message: Message, state: FSMContext):
    if not is_authorized_in_group(message):
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
    if not is_authorized_in_group(message):
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
        "INSERT INTO keywords (keyword, reply_text, owner_user_id) VALUES (?, ?, ?)",
        (data.get("keyword"), data.get("reply_text"), callback.from_user.id),
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


def is_authorized_callback(callback: CallbackQuery) -> bool:
    owner_user_id = get_group_owner_user_id(callback.message.chat.id)
    return owner_user_id is not None and callback.from_user.id == owner_user_id


@dp.message(F.text == "لیست کیورد")
async def list_keywords(message: Message):
    if not is_authorized_in_group(message):
        return

    conn = db()
    rows = conn.execute(
        "SELECT id, keyword, reply_text FROM keywords WHERE owner_user_id = ? ORDER BY id",
        (message.from_user.id,),
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer("هنوز کیوردی ثبت نشده.")
        return

    await message.answer("کیورد مورد نظر رو انتخاب کن:", reply_markup=build_keyword_list_markup(rows))


@dp.callback_query(F.data.startswith("kwv:"))
async def on_keyword_view(callback: CallbackQuery):
    if not is_authorized_callback(callback):
        await callback.answer("⛔", show_alert=True)
        return

    kw_id = int(callback.data.split(":", 1)[1])

    conn = db()
    row = conn.execute(
        "SELECT id, keyword, reply_text FROM keywords WHERE id = ? AND owner_user_id = ?",
        (kw_id, callback.from_user.id),
    ).fetchone()
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
    if not is_authorized_callback(callback):
        await callback.answer("⛔", show_alert=True)
        return

    kw_id = int(callback.data.split(":", 1)[1])

    conn = db()
    conn.execute("DELETE FROM keywords WHERE id = ? AND owner_user_id = ?", (kw_id, callback.from_user.id))
    conn.commit()
    rows = conn.execute(
        "SELECT id, keyword, reply_text FROM keywords WHERE owner_user_id = ? ORDER BY id",
        (callback.from_user.id,),
    ).fetchall()
    conn.close()

    if not rows:
        await callback.message.edit_text("هنوز کیوردی ثبت نشده.")
    else:
        await callback.message.edit_text("کیورد مورد نظر رو انتخاب کن:", reply_markup=build_keyword_list_markup(rows))
    await callback.answer("حذف شد ✅")


@dp.callback_query(F.data == "kwback")
async def on_keyword_back(callback: CallbackQuery):
    if not is_authorized_callback(callback):
        await callback.answer("⛔", show_alert=True)
        return

    conn = db()
    rows = conn.execute(
        "SELECT id, keyword, reply_text FROM keywords WHERE owner_user_id = ? ORDER BY id",
        (callback.from_user.id,),
    ).fetchall()
    conn.close()

    if not rows:
        await callback.message.edit_text("هنوز کیوردی ثبت نشده.")
    else:
        await callback.message.edit_text("کیورد مورد نظر رو انتخاب کن:", reply_markup=build_keyword_list_markup(rows))
    await callback.answer()


def build_accounts_markup(rows):
    builder = InlineKeyboardBuilder()
    for business_connection_id, display_name in rows:
        builder.row(InlineKeyboardButton(text=display_name, callback_data=f"bkacc:{business_connection_id}"))
    return builder.as_markup()


def build_backup_chats_markup(business_connection_id: str, chats):
    builder = InlineKeyboardBuilder()
    for chat_id, chat_name in chats:
        builder.row(InlineKeyboardButton(text=chat_name, callback_data=f"backup:{business_connection_id}|{chat_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ لیست اکانت‌ها", callback_data="bkback"))
    return builder.as_markup()


@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    if message.chat.id != get_home_chat_id(OWNER_ID):
        return

    conn = db()
    accounts = conn.execute(
        """
        SELECT DISTINCT m.business_connection_id, COALESCE(c.display_name, m.business_connection_id)
        FROM messages m
        LEFT JOIN connections c ON c.business_connection_id = m.business_connection_id
        WHERE m.business_connection_id IS NOT NULL AND (c.user_id IS NULL OR c.user_id != ?)
        ORDER BY 2 COLLATE NOCASE
        """,
        (OWNER_ID,),
    ).fetchall()
    conn.close()

    if not accounts:
        await message.answer("پیامی از ساب‌اکانتی ذخیره نشده.")
        return

    if len(accounts) == 1:
        business_connection_id = accounts[0][0]
        await show_backup_chats(message, business_connection_id)
        return

    await message.answer("اکانت مورد نظر رو انتخاب کن:", reply_markup=build_accounts_markup(accounts))


async def show_backup_chats(target, business_connection_id: str):
    conn = db()
    chats = conn.execute(
        "SELECT DISTINCT chat_id, chat_name FROM messages WHERE business_connection_id = ? ORDER BY chat_name COLLATE NOCASE",
        (business_connection_id,),
    ).fetchall()
    conn.close()

    if not chats:
        await target.answer("پیامی برای این اکانت یافت نشد.")
        return

    markup = build_backup_chats_markup(business_connection_id, chats)
    acc_name = connection_display_name(business_connection_id)
    text = f"کاربر مورد نظر رو انتخاب کن ({acc_name}):"
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        await target.edit_text(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("bkacc:"))
async def on_account_click(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    business_connection_id = callback.data.split(":", 1)[1]
    await show_backup_chats(callback.message, business_connection_id)
    await callback.answer()


@dp.callback_query(F.data == "bkback")
async def on_backup_back(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return

    conn = db()
    accounts = conn.execute(
        """
        SELECT DISTINCT m.business_connection_id, COALESCE(c.display_name, m.business_connection_id)
        FROM messages m
        LEFT JOIN connections c ON c.business_connection_id = m.business_connection_id
        WHERE m.business_connection_id IS NOT NULL AND (c.user_id IS NULL OR c.user_id != ?)
        ORDER BY 2 COLLATE NOCASE
        """,
        (OWNER_ID,),
    ).fetchall()
    conn.close()

    if not accounts:
        await callback.message.edit_text("پیامی از ساب‌اکانتی ذخیره نشده.")
    else:
        await callback.message.edit_text("اکانت مورد نظر رو انتخاب کن:", reply_markup=build_accounts_markup(accounts))
    await callback.answer()


@dp.callback_query(F.data.startswith("backup:"))
async def on_backup_click(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return

    payload = callback.data.split(":", 1)[1]
    business_connection_id, chat_id_str = payload.rsplit("|", 1)
    chat_id = int(chat_id_str)

    conn = db()
    rows = conn.execute(
        "SELECT chat_name, sender_name, sender_username, text, media_type, ts, file_id FROM messages WHERE business_connection_id = ? AND chat_id = ? ORDER BY ts",
        (business_connection_id, chat_id),
    ).fetchall()
    conn.close()

    if not rows:
        await callback.answer("پیامی یافت نشد.", show_alert=True)
        return

    acc_name = connection_display_name(business_connection_id)
    chat_name = rows[0][0]
    lines = [f"===== {acc_name} | {chat_name} (ID: {chat_id}) ====="]
    for _, sender_name, sender_username, text, media_type, ts, _ in rows:
        try:
            ts_fmt = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            ts_fmt = ts
        uname = f" (@{sender_username})" if sender_username else ""
        content = f"[{media_type}] {text}".strip() if media_type else text
        lines.append(f"[{ts_fmt}] {sender_name}{uname}: {content}")

    safe_acc = "".join(c for c in acc_name if c.isalnum() or c in " _-").strip() or business_connection_id
    safe_name = "".join(c for c in chat_name if c.isalnum() or c in " _-").strip() or str(chat_id)
    filename = f"backup_{safe_acc}_{safe_name}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    filepath = f"/tmp/{filename}"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    await bot.send_document(
        chat_id=callback.message.chat.id,
        document=FSInputFile(filepath, filename=filename),
        caption=f"بکاپ {acc_name} | {chat_name} — {len(rows)} پیام",
    )

    send_map = {
        "photo": bot.send_photo,
        "video": bot.send_video,
        "voice": bot.send_voice,
        "audio": bot.send_audio,
        "document": bot.send_document,
        "animation": bot.send_animation,
    }
    media_rows = [r for r in rows if r[4] and r[6]]
    if media_rows:
        try:
            topic_id, mgmt_chat_id = await get_or_create_backup_topic(business_connection_id, chat_id, chat_name)
        except Exception as e:
            logging.error(f"Failed to get/create backup topic: {e}")
            topic_id, mgmt_chat_id = None, None

        for _, _, _, _, media_type, _, file_id in media_rows:
            caption = f"از: {acc_name}"
            try:
                if topic_id is None:
                    pass
                elif media_type == "video_note":
                    await bot.send_video_note(chat_id=mgmt_chat_id, video_note=file_id, message_thread_id=topic_id)
                    await bot.send_message(chat_id=mgmt_chat_id, text=caption, message_thread_id=topic_id)
                elif media_type == "sticker":
                    await bot.send_sticker(chat_id=mgmt_chat_id, sticker=file_id, message_thread_id=topic_id)
                    await bot.send_message(chat_id=mgmt_chat_id, text=caption, message_thread_id=topic_id)
                else:
                    await send_map[media_type](
                        chat_id=mgmt_chat_id, **{media_type: file_id}, caption=caption, message_thread_id=topic_id
                    )
            except Exception as e:
                logging.error(f"Failed to forward backup media: {e}")

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
