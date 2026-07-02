"""
Telegram Secretary Bot (Business Mode) - Railway webhook deployment
- Connect via: Settings > Telegram Business > Chatbots > @your_bot_username
- Each connected business account gets its own group: add the bot to a new
  group from that account and it's auto-registered as that account's home group.
- Logs all business chat messages to SQLite, live, forwarded into per-chat topics.
- OWNER_ID is the supervisor account: in its own home group, /backup lets it
  pick any connected account, then any of its chats, and get a text backup.
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
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

# All blocking sqlite access is serialized through this lock and pushed to a
# worker thread via asyncio.to_thread so a slow/contended DB call can never
# block the aiohttp event loop (which was the root cause of requests piling
# up and turning into "database is locked" errors under concurrent webhooks).
_DB_LOCK = asyncio.Lock()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class KeywordSetup(StatesGroup):
    waiting_keyword = State()
    confirm_keyword = State()
    waiting_reply = State()
    confirm_reply = State()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    # busy_timeout makes sqlite retry internally (up to 30s) instead of
    # raising "database is locked" the instant it hits contention.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _init_db_sync():
    """Runs once at startup. Creates tables / runs migrations. This used to
    run on *every* db() call (i.e. multiple times per incoming message),
    which under concurrent webhook traffic was the main source of DB lock
    contention. Now it only runs once."""
    conn = _connect()
    conn.execute("PRAGMA journal_mode = WAL")  # allows concurrent readers/writers

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
    if "owner_user_id" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN owner_user_id INTEGER")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat ON messages(chat_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conn ON messages(business_connection_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_owner ON messages(owner_user_id)")

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

    # backfill owner_user_id on messages for rows written before this column existed
    conn.execute("""
        UPDATE messages SET owner_user_id = (
            SELECT user_id FROM connections WHERE connections.business_connection_id = messages.business_connection_id
        )
        WHERE owner_user_id IS NULL AND business_connection_id IS NOT NULL
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS home_groups (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            chat_title TEXT,
            set_at TEXT
        )
    """)

    # topics / backup_topics are keyed by owner_user_id (stable per real account),
    # not business_connection_id (which changes if the account reconnects the bot).
    topic_cols = [r[1] for r in conn.execute("PRAGMA table_info(topics)").fetchall()]
    if topic_cols and "owner_user_id" not in topic_cols:
        conn.execute("ALTER TABLE topics RENAME TO topics_legacy")
        topic_cols = []
    if not topic_cols:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                home_chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                chat_name TEXT,
                UNIQUE(owner_user_id, chat_id)
            )
        """)

    backup_topic_cols = [r[1] for r in conn.execute("PRAGMA table_info(backup_topics)").fetchall()]
    if backup_topic_cols and "owner_user_id" not in backup_topic_cols:
        conn.execute("ALTER TABLE backup_topics RENAME TO backup_topics_legacy")
        backup_topic_cols = []
    if not backup_topic_cols:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backup_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                home_chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                chat_name TEXT,
                UNIQUE(owner_user_id, chat_id)
            )
        """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS unified_media_topic (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            home_chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL
        )
    """)

    conn.commit()
    conn.close()


async def init_db():
    async with _DB_LOCK:
        await asyncio.to_thread(_init_db_sync)


async def run_db(fn: Callable[[sqlite3.Connection], object]):
    """Run `fn(conn)` against a fresh connection in a worker thread, holding
    the shared lock so writes never race each other, then commit + close.
    Returns whatever `fn` returns."""

    def _work():
        conn = _connect()
        try:
            result = fn(conn)
            conn.commit()
            return result
        finally:
            conn.close()

    async with _DB_LOCK:
        return await asyncio.to_thread(_work)


def is_self_account_sync(conn: sqlite3.Connection, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    row = conn.execute("SELECT 1 FROM connections WHERE user_id = ?", (user_id,)).fetchone()
    return row is not None


async def is_self_account(user_id: int) -> bool:
    return await run_db(lambda conn: is_self_account_sync(conn, user_id))


async def connection_owner_user_id(business_connection_id: str):
    def _q(conn):
        row = conn.execute(
            "SELECT user_id FROM connections WHERE business_connection_id = ?",
            (business_connection_id,),
        ).fetchone()
        return row[0] if row else None

    return await run_db(_q)


async def get_home_chat_id(user_id: int):
    def _q(conn):
        row = conn.execute("SELECT chat_id FROM home_groups WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    return await run_db(_q)


async def get_group_owner_user_id(chat_id: int):
    def _q(conn):
        row = conn.execute("SELECT user_id FROM home_groups WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row else None

    return await run_db(_q)


async def register_home_group(user_id: int, chat_id: int, chat_title: str):
    def _w(conn):
        conn.execute(
            """
            INSERT INTO home_groups (user_id, chat_id, chat_title, set_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, chat_title=excluded.chat_title, set_at=excluded.set_at
            """,
            (user_id, chat_id, chat_title, now_iso()),
        )

    await run_db(_w)


async def is_authorized_in_group(message: Message) -> bool:
    if not message.from_user:
        return False
    owner_user_id = await get_group_owner_user_id(message.chat.id)
    return owner_user_id is not None and message.from_user.id == owner_user_id


async def connection_display_name(owner_user_id: int) -> str:
    def _q(conn):
        return conn.execute(
            "SELECT display_name, username FROM connections WHERE user_id = ? ORDER BY connected_at DESC LIMIT 1",
            (owner_user_id,),
        ).fetchone()

    row = await run_db(_q)
    if not row:
        return str(owner_user_id)
    display_name, username = row
    return display_name or (f"@{username}" if username else str(owner_user_id))


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


# ---------------------------------------------------------------------------
# Safe sending helpers: Telegram raises TOPIC_CLOSED if a forum topic was
# closed by hand and we try to post into it. Previously this exception was
# never caught, so it crashed the update handler. These helpers catch it,
# try to reopen the topic once, retry, and otherwise fail quietly (logged)
# instead of blowing up the whole update.
# ---------------------------------------------------------------------------

async def safe_send(
    coro_factory: Callable[[], Awaitable],
    chat_id: int,
    message_thread_id: Optional[int] = None,
):
    try:
        return await coro_factory()
    except TelegramBadRequest as e:
        if "TOPIC_CLOSED" not in str(e):
            logging.error(f"Send failed for chat {chat_id}: {e}")
            return None
        try:
            if message_thread_id:
                await bot.reopen_forum_topic(chat_id=chat_id, message_thread_id=message_thread_id)
            else:
                await bot.reopen_general_forum_topic(chat_id=chat_id)
            return await coro_factory()
        except Exception as e2:
            logging.error(f"Topic reopen+retry failed for chat {chat_id}: {e2}")
            return None
    except Exception as e:
        logging.error(f"Unexpected send error for chat {chat_id}: {e}")
        return None


async def safe_send_message(chat_id: int, text: str, message_thread_id: Optional[int] = None, **kwargs):
    return await safe_send(
        lambda: bot.send_message(chat_id=chat_id, text=text, message_thread_id=message_thread_id, **kwargs),
        chat_id=chat_id,
        message_thread_id=message_thread_id,
    )


async def safe_reply(message: Message, text: str, **kwargs):
    return await safe_send_message(
        chat_id=message.chat.id,
        text=text,
        message_thread_id=message.message_thread_id,
        **kwargs,
    )


MEDIA_SEND_METHODS = {
    "photo": "send_photo",
    "video": "send_video",
    "voice": "send_voice",
    "audio": "send_audio",
    "document": "send_document",
    "animation": "send_animation",
}


async def safe_send_media(chat_id: int, media_type: str, file_id: str, caption: str, message_thread_id: int):
    """Relays a single media item into a topic, handling video_note/sticker
    (which have no caption param) and TOPIC_CLOSED the same way as text."""

    async def _do():
        if media_type == "video_note":
            await bot.send_video_note(chat_id=chat_id, video_note=file_id, message_thread_id=message_thread_id)
            await bot.send_message(chat_id=chat_id, text=caption, message_thread_id=message_thread_id)
        elif media_type == "sticker":
            await bot.send_sticker(chat_id=chat_id, sticker=file_id, message_thread_id=message_thread_id)
            await bot.send_message(chat_id=chat_id, text=caption, message_thread_id=message_thread_id)
        else:
            method_name = MEDIA_SEND_METHODS.get(media_type)
            if not method_name:
                return
            method = getattr(bot, method_name)
            await method(
                chat_id=chat_id,
                **{media_type: file_id},
                caption=caption,
                message_thread_id=message_thread_id,
            )

    await safe_send(_do, chat_id=chat_id, message_thread_id=message_thread_id)


async def get_or_create_topic(owner_user_id: int, chat_id: int, chat_name: str):
    home_chat_id = await get_home_chat_id(owner_user_id) if owner_user_id else None
    if home_chat_id is None:
        logging.warning(
            f"get_or_create_topic: no home_chat_id for owner_user_id={owner_user_id} "
            f"(chat_id={chat_id}) — topic/relay skipped"
        )
        return None, None

    def _select(conn):
        return conn.execute(
            "SELECT topic_id, home_chat_id FROM topics WHERE owner_user_id = ? AND chat_id = ?",
            (owner_user_id, chat_id),
        ).fetchone()

    row = await run_db(_select)
    if row and row[1] == home_chat_id:
        return row[0], home_chat_id

    acc_name = await connection_display_name(owner_user_id)
    topic_name = f"{acc_name} — {chat_name}"[:128]
    try:
        topic = await bot.create_forum_topic(chat_id=home_chat_id, name=topic_name)
    except TelegramBadRequest as e:
        logging.error(f"Failed to create topic in {home_chat_id}: {e}")
        return None, None

    def _upsert(conn):
        conn.execute(
            """
            INSERT INTO topics (owner_user_id, chat_id, home_chat_id, topic_id, chat_name) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner_user_id, chat_id) DO UPDATE SET
                home_chat_id=excluded.home_chat_id, topic_id=excluded.topic_id, chat_name=excluded.chat_name
            """,
            (owner_user_id, chat_id, home_chat_id, topic.message_thread_id, chat_name),
        )

    await run_db(_upsert)
    return topic.message_thread_id, home_chat_id


async def get_or_create_unified_media_topic():
    """Single shared topic (in the owner's supervisor group) that receives
    live media from ALL sub-accounts as it arrives. Created once and reused
    forever — never recreated, never touched by /backup."""
    supervisor_chat_id = await get_home_chat_id(OWNER_ID)
    if supervisor_chat_id is None:
        return None, None

    def _select(conn):
        return conn.execute(
            "SELECT topic_id, home_chat_id FROM unified_media_topic WHERE id = 1"
        ).fetchone()

    row = await run_db(_select)
    if row and row[1] == supervisor_chat_id:
        return row[0], supervisor_chat_id

    try:
        topic = await bot.create_forum_topic(chat_id=supervisor_chat_id, name="📎 رسانه همه اکانت‌ها")
    except TelegramBadRequest as e:
        logging.error(f"Failed to create unified media topic in {supervisor_chat_id}: {e}")
        return None, None

    def _upsert(conn):
        conn.execute(
            """
            INSERT INTO unified_media_topic (id, home_chat_id, topic_id) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET home_chat_id=excluded.home_chat_id, topic_id=excluded.topic_id
            """,
            (supervisor_chat_id, topic.message_thread_id),
        )

    await run_db(_upsert)
    return topic.message_thread_id, supervisor_chat_id


@dp.business_connection()
async def on_business_connection(business_conn: BusinessConnection):
    logging.info(f"Business connection: {business_conn.id} enabled={business_conn.is_enabled}")

    user = business_conn.user
    parts = [p for p in [user.first_name, user.last_name] if p]
    display_name = " ".join(parts) if parts else (user.username or str(user.id))

    def _select_existed(conn):
        return conn.execute("SELECT 1 FROM connections WHERE user_id = ?", (user.id,)).fetchone()

    def _upsert(conn):
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
                now_iso(),
            ),
        )

    def _work(conn):
        existed = _select_existed(conn)
        _upsert(conn)
        return existed

    existed = await run_db(_work)

    if existed:
        return

    notify_chat_id = await get_home_chat_id(user.id) or await get_home_chat_id(OWNER_ID)
    if notify_chat_id:
        await safe_send_message(
            chat_id=notify_chat_id,
            text=f"🔗 اکانت جدید وصل شد: {display_name}" + (f" (@{user.username})" if user.username else ""),
        )


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
        await register_home_group(OWNER_ID, event.chat.id, event.chat.title)
        await safe_send_message(
            chat_id=event.chat.id,
            text="✅ این گروه به‌عنوان گروه سوپروایزر (اکانت اصلی) ثبت شد.\nبرای بکاپ گرفتن از کیورد /backup استفاده کن.",
        )
        return

    def _q(conn):
        return conn.execute("SELECT 1 FROM connections WHERE user_id = ?", (adder.id,)).fetchone()

    row = await run_db(_q)

    if row:
        await register_home_group(adder.id, event.chat.id, event.chat.title)
        await safe_send_message(
            chat_id=event.chat.id,
            text="✅ این گروه برای این اکانت ثبت شد. پیام‌ها اینجا به تفکیک تاپیک لاگ میشن و «تنظیم کیورد» / «لیست کیورد» اینجا کار می‌کنن.",
        )
    else:
        await safe_send_message(
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

    owner_user_id = await connection_owner_user_id(message.business_connection_id)
    logging.info(
        f"business_message: business_connection_id={message.business_connection_id} "
        f"resolved owner_user_id={owner_user_id} chat_id={message.chat.id}"
    )

    def _insert(conn):
        conn.execute(
            "INSERT INTO messages (chat_id, chat_name, sender_id, sender_name, sender_username, text, media_type, file_id, ts, business_connection_id, owner_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.chat.id,
                chat_display_name(message),
                message.from_user.id if message.from_user else None,
                sender_display_name(message),
                message.from_user.username if message.from_user else None,
                text,
                media_type,
                file_id,
                message.date.isoformat() if message.date else now_iso(),
                message.business_connection_id,
                owner_user_id,
            ),
        )

    await run_db(_insert)

    try:
        thread_id, home_chat_id = await get_or_create_topic(owner_user_id, message.chat.id, chat_display_name(message))
    except Exception as e:
        logging.error(f"Failed to get/create topic: {e}")
        thread_id, home_chat_id = None, None

    if message.from_user and owner_user_id and message.from_user.id == owner_user_id:
        sender_label = "شما"
    else:
        sender_label = sender_display_name(message) + (
            f" (@{message.from_user.username})" if message.from_user and message.from_user.username else ""
        )

    if media_type and file_id:
        caption = f"از: {sender_label}" + (f"\n{text}" if text else "")
        if thread_id is not None:
            await safe_send_media(home_chat_id, media_type, file_id, caption, thread_id)

        if owner_user_id and owner_user_id != OWNER_ID:
            try:
                umt_id, umt_chat_id = await get_or_create_unified_media_topic()
            except Exception as e:
                logging.error(f"Failed to get/create unified media topic: {e}")
                umt_id, umt_chat_id = None, None
            if umt_id is not None:
                acc_name = await connection_display_name(owner_user_id)
                unified_caption = (
                    f"👤 {acc_name}\n💬 {chat_display_name(message)}\nاز: {sender_label}"
                    + (f"\n{text}" if text else "")
                )
                await safe_send_media(umt_chat_id, media_type, file_id, unified_caption, umt_id)
    elif text and thread_id is not None:
        await safe_send_message(
            chat_id=home_chat_id,
            text=f"از: {sender_label}\n{text}",
            message_thread_id=thread_id,
        )

    if text and (not message.from_user or not await is_self_account(message.from_user.id)):
        def _q(conn):
            return conn.execute(
                "SELECT keyword, reply_text FROM keywords WHERE owner_user_id = ?", (owner_user_id,)
            ).fetchall()

        krows = await run_db(_q)
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
    if not await is_authorized_in_group(message):
        return
    prompt = await safe_reply(message, "پیامی که می‌خوای کیورد باشه رو روی همین پیام ریپلای کن.")
    if not prompt:
        return
    await state.set_state(KeywordSetup.waiting_keyword)
    await state.update_data(prompt_id=prompt.message_id)


@dp.message(KeywordSetup.waiting_keyword)
async def receive_keyword(message: Message, state: FSMContext):
    if not await is_authorized_in_group(message):
        return
    data = await state.get_data()
    if not message.reply_to_message or message.reply_to_message.message_id != data.get("prompt_id"):
        return
    if not message.text:
        await safe_reply(message, "فقط متن قابل قبوله.")
        return

    await state.update_data(keyword=message.text)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ تایید", callback_data="kw_confirm_keyword"),
        InlineKeyboardButton(text="❌ رد", callback_data="kw_cancel"),
    )
    await safe_reply(
        message,
        f"این کیورد تنظیم بشه؟\n«{message.text}»",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(KeywordSetup.confirm_keyword)


@dp.callback_query(F.data == "kw_confirm_keyword", KeywordSetup.confirm_keyword)
async def confirm_keyword(callback: CallbackQuery, state: FSMContext):
    prompt = await safe_reply(callback.message, "حالا پیامی که می‌خوای در جواب این کیورد ارسال بشه رو روی همین پیام ریپلای کن.")
    if prompt:
        await state.update_data(prompt_id=prompt.message_id)
        await state.set_state(KeywordSetup.waiting_reply)
    await callback.answer()


@dp.message(KeywordSetup.waiting_reply)
async def receive_reply_text(message: Message, state: FSMContext):
    if not await is_authorized_in_group(message):
        return
    data = await state.get_data()
    if not message.reply_to_message or message.reply_to_message.message_id != data.get("prompt_id"):
        return
    if not message.text:
        await safe_reply(message, "فقط متن قابل قبوله.")
        return

    await state.update_data(reply_text=message.text)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ تایید", callback_data="kw_confirm_reply"),
        InlineKeyboardButton(text="❌ رد", callback_data="kw_cancel"),
    )
    await safe_reply(
        message,
        f"کیورد: «{data.get('keyword')}»\nپاسخ: «{message.text}»\nتایید می‌کنی؟",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(KeywordSetup.confirm_reply)


@dp.callback_query(F.data == "kw_confirm_reply", KeywordSetup.confirm_reply)
async def confirm_reply(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    def _insert(conn):
        conn.execute(
            "INSERT INTO keywords (keyword, reply_text, owner_user_id) VALUES (?, ?, ?)",
            (data.get("keyword"), data.get("reply_text"), callback.from_user.id),
        )

    await run_db(_insert)
    await state.clear()
    await safe_reply(callback.message, "اتومیشن تنظیم شد ✅")
    await callback.answer()


@dp.callback_query(F.data == "kw_cancel")
async def cancel_keyword_setup(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_reply(callback.message, "لغو شد ❌")
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


async def is_authorized_callback(callback: CallbackQuery) -> bool:
    owner_user_id = await get_group_owner_user_id(callback.message.chat.id)
    return owner_user_id is not None and callback.from_user.id == owner_user_id


@dp.message(F.text == "لیست کیورد")
async def list_keywords(message: Message):
    if not await is_authorized_in_group(message):
        return

    def _q(conn):
        return conn.execute(
            "SELECT id, keyword, reply_text FROM keywords WHERE owner_user_id = ? ORDER BY id",
            (message.from_user.id,),
        ).fetchall()

    rows = await run_db(_q)

    if not rows:
        await safe_reply(message, "هنوز کیوردی ثبت نشده.")
        return

    await safe_reply(message, "کیورد مورد نظر رو انتخاب کن:", reply_markup=build_keyword_list_markup(rows))


@dp.callback_query(F.data.startswith("kwv:"))
async def on_keyword_view(callback: CallbackQuery):
    if not await is_authorized_callback(callback):
        await callback.answer("⛔", show_alert=True)
        return

    kw_id = int(callback.data.split(":", 1)[1])

    def _q(conn):
        return conn.execute(
            "SELECT id, keyword, reply_text FROM keywords WHERE id = ? AND owner_user_id = ?",
            (kw_id, callback.from_user.id),
        ).fetchone()

    row = await run_db(_q)

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
    if not await is_authorized_callback(callback):
        await callback.answer("⛔", show_alert=True)
        return

    kw_id = int(callback.data.split(":", 1)[1])

    def _work(conn):
        conn.execute("DELETE FROM keywords WHERE id = ? AND owner_user_id = ?", (kw_id, callback.from_user.id))
        return conn.execute(
            "SELECT id, keyword, reply_text FROM keywords WHERE owner_user_id = ? ORDER BY id",
            (callback.from_user.id,),
        ).fetchall()

    rows = await run_db(_work)

    if not rows:
        await callback.message.edit_text("هنوز کیوردی ثبت نشده.")
    else:
        await callback.message.edit_text("کیورد مورد نظر رو انتخاب کن:", reply_markup=build_keyword_list_markup(rows))
    await callback.answer("حذف شد ✅")


@dp.callback_query(F.data == "kwback")
async def on_keyword_back(callback: CallbackQuery):
    if not await is_authorized_callback(callback):
        await callback.answer("⛔", show_alert=True)
        return

    def _q(conn):
        return conn.execute(
            "SELECT id, keyword, reply_text FROM keywords WHERE owner_user_id = ? ORDER BY id",
            (callback.from_user.id,),
        ).fetchall()

    rows = await run_db(_q)

    if not rows:
        await callback.message.edit_text("هنوز کیوردی ثبت نشده.")
    else:
        await callback.message.edit_text("کیورد مورد نظر رو انتخاب کن:", reply_markup=build_keyword_list_markup(rows))
    await callback.answer()


def build_accounts_markup(rows):
    builder = InlineKeyboardBuilder()
    for owner_user_id, display_name in rows:
        builder.row(InlineKeyboardButton(text=display_name, callback_data=f"bkacc:{owner_user_id}"))
    return builder.as_markup()


def build_backup_chats_markup(owner_user_id: int, chats):
    builder = InlineKeyboardBuilder()
    for chat_id, chat_name in chats:
        builder.row(InlineKeyboardButton(text=chat_name, callback_data=f"backup:{owner_user_id}|{chat_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ لیست اکانت‌ها", callback_data="bkback"))
    return builder.as_markup()


async def get_backup_accounts():
    def _q(conn):
        return conn.execute(
            """
            SELECT DISTINCT m.owner_user_id
            FROM messages m
            WHERE m.owner_user_id IS NOT NULL AND m.owner_user_id != ?
            """,
            (OWNER_ID,),
        ).fetchall()

    accounts = await run_db(_q)
    result = []
    for row in accounts:
        result.append((row[0], await connection_display_name(row[0])))
    result.sort(key=lambda r: r[1].lower())
    return result


@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    if message.chat.id != await get_home_chat_id(OWNER_ID):
        return

    accounts = await get_backup_accounts()

    if not accounts:
        await safe_reply(message, "پیامی از ساب‌اکانتی ذخیره نشده.")
        return

    if len(accounts) == 1:
        await show_backup_chats(message, accounts[0][0])
        return

    await safe_reply(message, "اکانت مورد نظر رو انتخاب کن:", reply_markup=build_accounts_markup(accounts))


async def show_backup_chats(target, owner_user_id: int):
    def _q(conn):
        return conn.execute(
            "SELECT DISTINCT chat_id, chat_name FROM messages WHERE owner_user_id = ? ORDER BY chat_name COLLATE NOCASE",
            (owner_user_id,),
        ).fetchall()

    chats = await run_db(_q)

    if not chats:
        if isinstance(target, Message):
            await safe_reply(target, "پیامی برای این اکانت یافت نشد.")
        else:
            await target.edit_text("پیامی برای این اکانت یافت نشد.")
        return

    markup = build_backup_chats_markup(owner_user_id, chats)
    acc_name = await connection_display_name(owner_user_id)
    text = f"کاربر مورد نظر رو انتخاب کن ({acc_name}):"
    if isinstance(target, Message):
        await safe_reply(target, text, reply_markup=markup)
    else:
        await target.edit_text(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("bkacc:"))
async def on_account_click(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    owner_user_id = int(callback.data.split(":", 1)[1])
    await show_backup_chats(callback.message, owner_user_id)
    await callback.answer()


@dp.callback_query(F.data == "bkback")
async def on_backup_back(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return

    accounts = await get_backup_accounts()

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
    owner_user_id_str, chat_id_str = payload.rsplit("|", 1)
    owner_user_id = int(owner_user_id_str)
    chat_id = int(chat_id_str)

    def _q(conn):
        return conn.execute(
            "SELECT chat_name, sender_name, sender_username, text, media_type, ts, file_id FROM messages WHERE owner_user_id = ? AND chat_id = ? ORDER BY ts",
            (owner_user_id, chat_id),
        ).fetchall()

    rows = await run_db(_q)

    if not rows:
        await callback.answer("پیامی یافت نشد.", show_alert=True)
        return

    acc_name = await connection_display_name(owner_user_id)
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

    safe_acc = "".join(c for c in acc_name if c.isalnum() or c in " _-").strip() or str(owner_user_id)
    safe_name = "".join(c for c in chat_name if c.isalnum() or c in " _-").strip() or str(chat_id)
    filename = f"backup_{safe_acc}_{safe_name}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
    filepath = f"/tmp/{filename}"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    try:
        await bot.send_document(
            chat_id=callback.message.chat.id,
            document=FSInputFile(filepath, filename=filename),
            caption=f"بکاپ {acc_name} | {chat_name} — {len(rows)} پیام",
            message_thread_id=callback.message.message_thread_id,
        )
    except TelegramBadRequest as e:
        if "TOPIC_CLOSED" in str(e):
            try:
                if callback.message.message_thread_id:
                    await bot.reopen_forum_topic(
                        chat_id=callback.message.chat.id,
                        message_thread_id=callback.message.message_thread_id,
                    )
                await bot.send_document(
                    chat_id=callback.message.chat.id,
                    document=FSInputFile(filepath, filename=filename),
                    caption=f"بکاپ {acc_name} | {chat_name} — {len(rows)} پیام",
                    message_thread_id=callback.message.message_thread_id,
                )
            except Exception as e2:
                logging.error(f"Failed to send backup document: {e2}")
        else:
            logging.error(f"Failed to send backup document: {e}")
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass

    # Media is already relayed live into the unified media topic as it
    # arrives (see on_business_message) — /backup only sends the text file
    # and does not resend media or create/touch any topic.

    await callback.answer("ارسال شد ✅")


async def on_startup(app: web.Application):
    await init_db()
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
