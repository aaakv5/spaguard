import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions
from aiogram.exceptions import TelegramBadRequest

# ─── ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ──────────────────────────────
# Локально читает .env; на хостинге (Railway/Render) просто берёт env vars.
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Переменная окружения BOT_TOKEN не задана."
    )

WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))         # сколько варнов до автобана
AUTO_BAN_DAYS = int(os.getenv("AUTO_BAN_DAYS", "7"))   # на сколько дней банить
DB_PATH = os.getenv("DB_PATH", "spaguard.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("SpaGuard")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ─── БАЗА ДАННЫХ (SQLite) ───────────────────────────────────────

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bans (
            chat_id INTEGER,
            user_id INTEGER,
            until_date TEXT,
            reason TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            chat_id INTEGER,
            user_id INTEGER,
            until_date TEXT,
            reason TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            reason TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ────────────────────────────────────

def parse_duration(duration_str: Optional[str]) -> Optional[datetime]:
    """'30m' / '2h' / '7d' → datetime (UTC). Без суффикса — None (навсегда)."""
    if not duration_str:
        return None
    unit = duration_str[-1].lower()
    try:
        amount = int(duration_str[:-1])
    except ValueError:
        return None
    if unit == "m":
        delta = timedelta(minutes=amount)
    elif unit == "h":
        delta = timedelta(hours=amount)
    elif unit == "d":
        delta = timedelta(days=amount)
    else:
        return None
    return datetime.utcnow() + delta

async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except TelegramBadRequest:
        return False

def add_warn(chat_id: int, user_id: int, reason: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO warns (chat_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, reason, datetime.utcnow().isoformat()),
    )
    conn.commit()
    cur.execute(
        "SELECT COUNT(*) FROM warns WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    count = cur.fetchone()[0]
    conn.close()
    return count

def reset_warns(chat_id: int, user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()

def save_ban(chat_id: int, user_id: int, until_date: Optional[datetime], reason: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO bans (chat_id, user_id, until_date, reason) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, until_date.isoformat() if until_date else None, reason),
    )
    conn.commit()
    conn.close()

def remove_ban(chat_id: int, user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM bans WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()

def save_mute(chat_id: int, user_id: int, until_date: Optional[datetime], reason: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO mutes (chat_id, user_id, until_date, reason) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, until_date.isoformat() if until_date else None, reason),
    )
    conn.commit()
    conn.close()

def remove_mute(chat_id: int, user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM mutes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()

def get_list(table: str, chat_id: int) -> list[tuple]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"SELECT user_id, until_date, reason FROM {table} WHERE chat_id = ?", (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def resolve_target(message: types.Message, target: str) -> Optional[int]:
    """Пытается получить user_id из reply или из @ника/ID."""
    if message.reply_to_message:
        return message.reply_to_message.from_user.id
    try:
        return int(target.strip("@"))
    except (ValueError, AttributeError):
        return None

# ─── ХЭНДЛЕРЫ КОМАНД ────────────────────────────────────────────

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")

    if not command.args:
        return await message.reply("ℹ️ /ban @ник [время] причина\nПример: /ban @user 2d спам")

    parts = command.args.split(maxsplit=2)
    target, duration, reason = parts[0], None, None

    if len(parts) >= 2:
        parsed = parse_duration(parts[1])
        if parsed:
            duration = parsed
            reason = parts[2] if len(parts) >= 3 else "Без причины"
        else:
            reason = " ".join(parts[1:])
    else:
        reason = "Без причины"

    user_id = resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=user_id, until_date=duration)
        save_ban(message.chat.id, user_id, duration, reason)
        until_text = f"до {duration.strftime('%d.%m.%Y %H:%M')} UTC" if duration else "навсегда"
        await message.reply(f"🔨 {target} забанен {until_text}.\nПричина: {reason}")
    except TelegramBadRequest as e:
        await message.reply(f"❌ Не удалось забанить: {e}")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")
    if not command.args:
        return await message.reply("ℹ️ /unban @ник причина")

    parts = command.args.split(maxsplit=1)
    target = parts[0]
    reason = parts[1] if len(parts) > 1 else None
    if not reason:
        return await message.reply("❌ Причина обязательна. /unban @ник причина")

    user_id = resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    try:
        await bot.unban_chat_member(chat_id=message.chat.id, user_id=user_id)
        remove_ban(message.chat.id, user_id)
        await message.reply(f"✅ {target} разбанен.\nПричина: {reason}")
    except TelegramBadRequest as e:
        await message.reply(f"❌ Не удалось разбанить: {e}")

@dp.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")
    if not command.args:
        return await message.reply("ℹ️ /mute @ник [время] причина\nПример: /mute @user 30m флуд")

    parts = command.args.split(maxsplit=2)
    target, duration, reason = parts[0], None, None

    if len(parts) >= 2:
        parsed = parse_duration(parts[1])
        if parsed:
            duration = parsed
            reason = parts[2] if len(parts) >= 3 else "Без причины"
        else:
            reason = " ".join(parts[1:])
    else:
        reason = "Без причины"

    user_id = resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=duration,
        )
        save_mute(message.chat.id, user_id, duration, reason)
        until_text = f"до {duration.strftime('%d.%m.%Y %H:%M')} UTC" if duration else "навсегда"
        await message.reply(f"🔇 {target} замучен {until_text}.\nПричина: {reason}")
    except TelegramBadRequest as e:
        await message.reply(f"❌ Не удалось замутить: {e}")

@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")
    if not command.args:
        return await message.reply("ℹ️ /unmute @ник причина")

    parts = command.args.split(maxsplit=1)
    target = parts[0]
    reason = parts[1] if len(parts) > 1 else None
    if not reason:
        return await message.reply("❌ Причина обязательна. /unmute @ник причина")

    user_id = resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        remove_mute(message.chat.id, user_id)
        await message.reply(f"✅ С {target} снят мут.\nПричина: {reason}")
    except TelegramBadRequest as e:
        await message.reply(f"❌ Не удалось снять мут: {e}")

@dp.message(Command("warn"))
async def cmd_warn(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")
    if not command.args:
        return await message.reply("ℹ️ /warn @ник причина")

    parts = command.args.split(maxsplit=1)
    target = parts[0]
    reason = parts[1] if len(parts) > 1 else "Без причины"

    user_id = resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    count = add_warn(message.chat.id, user_id, reason)
    await message.reply(f"⚠️ {target} получил предупреждение ({count}/{WARN_LIMIT}).\nПричина: {reason}")

    if count >= WARN_LIMIT:
        until = datetime.utcnow() + timedelta(days=AUTO_BAN_DAYS)
        try:
            await bot.ban_chat_member(chat_id=message.chat.id, user_id=user_id, until_date=until)
            save_ban(message.chat.id, user_id, until, f"Автобан: {WARN_LIMIT} варнов")
            reset_warns(message.chat.id, user_id)
            await message.answer(
                f"🚫 {target} автоматически забанен на {AUTO_BAN_DAYS} дней "
                f"за достижение лимита предупреждений ({WARN_LIMIT})."
            )
        except TelegramBadRequest as e:
            await message.answer(f"❌ Не удалось автоматически забанить: {e}")

@dp.message(Command("unwarn"))
async def cmd_unwarn(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")
    if not command.args:
        return await message.reply("ℹ️ /unwarn @ник причина")

    parts = command.args.split(maxsplit=1)
    target = parts[0]
    reason = parts[1] if len(parts) > 1 else None
    if not reason:
        return await message.reply("❌ Причина обязательна. /unwarn @ник причина")

    user_id = resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    reset_warns(message.chat.id, user_id)
    await message.reply(f"✅ С {target} сняты все предупреждения.\nПричина: {reason}")

# ─── КОМАНДЫ СПИСКОВ ────────────────────────────────────────────

@dp.message(Command("banlist"))
async def cmd_banlist(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут просматривать список.")
    rows = get_list("bans", message.chat.id)
    if not rows:
        return await message.reply("📭 Список забаненных пуст.")
    lines = ["🔨 **Забаненные:**"]
    for user_id, until, reason in rows:
        until_text = until.split("T")[0] if until else "навсегда"
        lines.append(f"• `{user_id}` — до {until_text} — {reason}")
    await message.reply("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("mutelist"))
async def cmd_mutelist(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут просматривать список.")
    rows = get_list("mutes", message.chat.id)
    if not rows:
        return await message.reply("📭 Список замученных пуст.")
    lines = ["🔇 **Замученные:**"]
    for user_id, until, reason in rows:
        until_text = until.split("T")[0] if until else "навсегда"
        lines.append(f"• `{user_id}` — до {until_text} — {reason}")
    await message.reply("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("warnlist"))
async def cmd_warnlist(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут просматривать список.")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, COUNT(*) FROM warns WHERE chat_id = ? GROUP BY user_id",
        (message.chat.id,),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return await message.reply("📭 Список предупреждений пуст.")
    lines = ["⚠️ **Предупреждения:**"]
    for user_id, count in rows:
        lines.append(f"• `{user_id}` — {count} шт.")
    await message.reply("\n".join(lines), parse_mode="Markdown")

# ─── ЗАПУСК ─────────────────────────────────────────────────────

async def main():
    logger.info("SpaGuard запускается...")
    logger.info("WARN_LIMIT=%s, AUTO_BAN_DAYS=%s", WARN_LIMIT, AUTO_BAN_DAYS)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
