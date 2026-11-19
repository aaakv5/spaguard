import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# ─── КОНФИГУРАЦИЯ ───────────────────────────────────────────────
BOT_TOKEN = "ВАШ_ТОКЕН_ОТ_BOTFATHER"
WARN_LIMIT = 3                # сколько варнов до автобана
AUTO_BAN_DAYS = 7             # на сколько дней банить после лимита варнов

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ─── БАЗА ДАННЫХ (SQLite) ────────────────────────────────────────
DB_PATH = "spaguard.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bans (
            chat_id INTEGER,
            user_id INTEGER,
            until_date TEXT,      -- NULL = навсегда
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

# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ─────────────────────────────────────

def parse_duration(duration_str: Optional[str]) -> Optional[datetime]:
    """Преобразует строку вида '1h', '30m', '7d' в datetime (UTC)."""
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
    """Проверяет, является ли пользователь администратором чата."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except TelegramBadRequest:
        return False

def add_warn(chat_id: int, user_id: int, reason: str) -> int:
    """Добавляет варн и возвращает новое количество варнов."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO warns (chat_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, reason, datetime.utcnow().isoformat())
    )
    conn.commit()
    cur.execute(
        "SELECT COUNT(*) FROM warns WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id)
    )
    count = cur.fetchone()[0]
    conn.close()
    return count

def get_warns(chat_id: int, user_id: int) -> list[tuple[str, str]]:
    """Возвращает список (причина, дата) для пользователя."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT reason, created_at FROM warns WHERE chat_id = ? AND user_id = ? ORDER BY id",
        (chat_id, user_id)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def reset_warns(chat_id: int, user_id: int):
    """Снимает все варны с пользователя."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()

def save_ban(chat_id: int, user_id: int, until_date: Optional[datetime], reason: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO bans (chat_id, user_id, until_date, reason) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, until_date.isoformat() if until_date else None, reason)
    )
    conn.commit()
    conn.close()

def remove_ban(chat_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM bans WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()

def save_mute(chat_id: int, user_id: int, until_date: Optional[datetime], reason: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO mutes (chat_id, user_id, until_date, reason) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, until_date.isoformat() if until_date else None, reason)
    )
    conn.commit()
    conn.close()

def remove_mute(chat_id: int, user_id: int):
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

# ─── ХЭНДЛЕРЫ КОМАНД ────────────────────────────────────────────

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")

    args = command.args
    if not args:
        return await message.reply("ℹ️ Использование: /ban @ник [время] причина\nПример: /ban @user 2d спам")

    parts = args.split(maxsplit=2)
    target = parts[0]
    duration = None
    reason = None

    if len(parts) >= 2:
        # Пытаемся распарсить второй аргумент как время
        parsed = parse_duration(parts[1])
        if parsed:
            duration = parsed
            reason = parts[2] if len(parts) >= 3 else "Без причины"
        else:
            reason = " ".join(parts[1:])
    elif len(parts) == 1:
        reason = "Без причины"

    # Получаем user_id по упоминанию или reply
    user_id = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    else:
        try:
            user_id = int(target.strip("@"))
        except ValueError:
            return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    try:
        await bot.ban_chat_member(
            chat_id=message.chat.id,
            user_id=user_id,
            until_date=duration
        )
        save_ban(message.chat.id, user_id, duration, reason)
        until_text = f"до {duration.strftime('%d.%m.%Y %H:%M')} UTC" if duration else "навсегда"
        await message.reply(f"🔨 Пользователь {target} забанен {until_text}.\nПричина: {reason}")
    except TelegramBadRequest as e:
        await message.reply(f"❌ Не удалось забанить: {e}")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")

    args = command.args
    if not args:
        return await message.reply("ℹ️ Использование: /unban @ник причина")

    parts = args.split(maxsplit=1)
    target = parts[0]
    reason = parts[1] if len(parts) > 1 else None
    if not reason:
        return await message.reply("❌ Причина обязательна. /unban @ник причина")

    user_id = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    else:
        try:
            user_id = int(target.strip("@"))
        except ValueError:
            return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    try:
        await bot.unban_chat_member(chat_id=message.chat.id, user_id=user_id)
        remove_ban(message.chat.id, user_id)
        await message.reply(f"✅ Пользователь {target} разбанен.\nПричина: {reason}")
    except TelegramBadRequest as e:
        await message.reply(f"❌ Не удалось разбанить: {e}")

@dp.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")

    args = command.args
    if not args:
        return await message.reply("ℹ️ Использование: /mute @ник [время] причина\nПример: /mute @user 30m флуд")

    parts = args.split(maxsplit=2)
    target = parts[0]
    duration = None
    reason = None

    if len(parts) >= 2:
        parsed = parse_duration(parts[1])
        if parsed:
            duration = parsed
            reason = parts[2] if len(parts) >= 3 else "Без причины"
        else:
            reason = " ".join(parts[1:])
    elif len(parts) == 1:
        reason = "Без причины"

    user_id = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    else:
        try:
            user_id = int(target.strip("@"))
        except ValueError:
            return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    try:
        permissions = ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=user_id,
            permissions=permissions,
            until_date=duration
        )
        save_mute(message.chat.id, user_id, duration, reason)
        until_text = f"до {duration.strftime('%d.%m.%Y %H:%M')} UTC" if duration else "навсегда"
        await message.reply(f"🔇 Пользователь {target} замучен {until_text}.\nПричина: {reason}")
    except TelegramBadRequest as e:
        await message.reply(f"❌ Не удалось замутить: {e}")

@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")

    args = command.args
    if not args:
        return await message.reply("ℹ️ Использование: /unmute @ник причина")

    parts = args.split(maxsplit=1)
    target = parts[0]
    reason = parts[1] if len(parts) > 1 else None
    if not reason:
        return await message.reply("❌ Причина обязательна. /unmute @ник причина")

    user_id = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    else:
        try:
            user_id = int(target.strip("@"))
        except ValueError:
            return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    try:
        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        await bot.restrict_chat_member(chat_id=message.chat.id, user_id=user_id, permissions=permissions)
        remove_mute(message.chat.id, user_id)
        await message.reply(f"✅ С пользователя {target} снят мут.\nПричина: {reason}")
    except TelegramBadRequest as e:
        await message.reply(f"❌ Не удалось снять мут: {e}")

@dp.message(Command("warn"))
async def cmd_warn(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")

    args = command.args
    if not args:
        return await message.reply("ℹ️ Использование: /warn @ник причина")

    parts = args.split(maxsplit=1)
    target = parts[0]
    reason = parts[1] if len(parts) > 1 else "Без причины"

    user_id = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    else:
        try:
            user_id = int(target.strip("@"))
        except ValueError:
            return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    count = add_warn(message.chat.id, user_id, reason)
    await message.reply(f"⚠️ Пользователь {target} получил предупреждение ({count}/{WARN_LIMIT}).\nПричина: {reason}")

    if count >= WARN_LIMIT:
        until = datetime.utcnow() + timedelta(days=AUTO_BAN_DAYS)
        try:
            await bot.ban_chat_member(chat_id=message.chat.id, user_id=user_id, until_date=until)
            save_ban(message.chat.id, user_id, until, f"Автобан: {WARN_LIMIT} варнов")
            reset_warns(message.chat.id, user_id)  # обнуляем варны после автобана
            await message.answer(
                f"🚫 Пользователь {target} автоматически забанен на {AUTO_BAN_DAYS} дней "
                f"за достижение лимита предупреждений ({WARN_LIMIT})."
            )
        except TelegramBadRequest as e:
            await message.answer(f"❌ Не удалось автоматически забанить: {e}")

@dp.message(Command("unwarn"))
async def cmd_unwarn(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только администраторы могут использовать эту команду.")

    args = command.args
    if not args:
        return await message.reply("ℹ️ Использование: /unwarn @ник причина")

    parts = args.split(maxsplit=1)
    target = parts[0]
    reason = parts[1] if len(parts) > 1 else None
    if not reason:
        return await message.reply("❌ Причина обязательна. /unwarn @ник причина")

    user_id = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    else:
        try:
            user_id = int(target.strip("@"))
        except ValueError:
            return await message.reply("❌ Укажите @ник или ответьте на сообщение пользователя.")

    reset_warns(message.chat.id, user_id)
    await message.reply(f"✅ С пользователя {target} сняты все предупреждения.\nПричина: {reason}")

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
    cur.execute("SELECT user_id, COUNT(*) FROM warns WHERE chat_id = ? GROUP BY user_id", (message.chat.id,))
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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
