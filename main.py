import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from html import escape
from typing import Optional

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions
from aiogram.exceptions import TelegramBadRequest

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана.")

WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))
AUTO_BAN_DAYS = int(os.getenv("AUTO_BAN_DAYS", "7"))
DB_PATH = os.getenv("DB_PATH", "spaguard.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("SpaGuard")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ─── БАЗА ДАННЫХ ────────────────────────────────────────────────

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bans (
                chat_id INTEGER, user_id INTEGER, until_date TEXT, reason TEXT,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS mutes (
                chat_id INTEGER, user_id INTEGER, until_date TEXT, reason TEXT,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, user_id INTEGER, reason TEXT, created_at TEXT
            );
        """)

init_db()

# ─── УТИЛИТЫ ────────────────────────────────────────────────────

def parse_duration(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    unit = s[-1].lower()
    try:
        amount = int(s[:-1])
    except ValueError:
        return None
    if amount <= 0:
        return None
    deltas = {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }
    delta = deltas.get(unit)
    if not delta:
        return None
    return datetime.utcnow() + delta


def humanize_until(until: Optional[datetime]) -> str:
    if until is None:
        return "навсегда"
    total = int((until - datetime.utcnow()).total_seconds())
    if total <= 0:
        return "истёк"
    if total >= 86400:
        return f"{total // 86400} дн."
    if total >= 3600:
        return f"{total // 3600} ч."
    if total >= 60:
        return f"{total // 60} мин."
    return f"{total} сек."


def format_duration_line(until: Optional[datetime]) -> str:
    """Единая строка про срок: без дублей 'навсегда · навсегда'."""
    if until is None:
        return "⏱ Срок: <b>навсегда</b>"
    return (
        f"⏱ Срок: <b>{humanize_until(until)}</b>  ·  "
        f"<i>{until.strftime('%d.%m.%Y %H:%M')} UTC</i>"
    )


def user_link(user_id: int, name: Optional[str] = None) -> str:
    label = escape(name) if name and name != str(user_id) else str(user_id)
    return f'<a href="tg://user?id={user_id}">{label}</a>'


async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except TelegramBadRequest:
        return False


async def resolve_target(message: types.Message, target: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Приоритет: reply → числовой ID → @username (редко работает)."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, u.full_name

    if not target:
        return None, None

    clean = target.strip().lstrip("@")
    if not clean:
        return None, None

    try:
        return int(clean), None
    except ValueError:
        pass

    try:
        chat = await bot.get_chat(f"@{clean}")
        return chat.id, chat.full_name or clean
    except TelegramBadRequest:
        return None, None


# ─── РАБОТА С БД ────────────────────────────────────────────────

def _conn():
    return sqlite3.connect(DB_PATH)


def add_warn(chat_id, user_id, reason) -> int:
    with _conn() as c:
        c.execute(
            "INSERT INTO warns (chat_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, reason, datetime.utcnow().isoformat()),
        )
        return c.execute(
            "SELECT COUNT(*) FROM warns WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()[0]


def reset_warns(chat_id, user_id):
    with _conn() as c:
        c.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))


def save_ban(chat_id, user_id, until, reason):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO bans VALUES (?, ?, ?, ?)",
                  (chat_id, user_id, until.isoformat() if until else None, reason))


def remove_ban(chat_id, user_id):
    with _conn() as c:
        c.execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id))


def save_mute(chat_id, user_id, until, reason):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO mutes VALUES (?, ?, ?, ?)",
                  (chat_id, user_id, until.isoformat() if until else None, reason))


def remove_mute(chat_id, user_id):
    with _conn() as c:
        c.execute("DELETE FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, user_id))


def get_list(table: str, chat_id: int) -> list[tuple]:
    with _conn() as c:
        return c.execute(
            f"SELECT user_id, until_date, reason FROM {table} WHERE chat_id=?",
            (chat_id,),
        ).fetchall()


# ─── ПАРСИНГ АРГУМЕНТОВ (с учётом reply) ───────────────────────

def parse_args(command_args: Optional[str], has_reply: bool, with_duration: bool):
    """
    Возвращает (target, duration, reason).
    Если has_reply=True — цель берётся из reply, а command_args = [время] причина.
    Иначе — target [время] причина.
    """
    parts = (command_args or "").split()

    target = None
    if not has_reply:
        if not parts:
            return None, None, None
        target = parts.pop(0)

    duration = None
    if with_duration and parts:
        d = parse_duration(parts[0])
        if d:
            duration = d
            parts.pop(0)

    reason = " ".join(parts) if parts else "не указана"
    return target, duration, reason


# ─── ХЭНДЛЕРЫ: БАН ─────────────────────────────────────────────

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ <b>Недостаточно прав.</b>", parse_mode="HTML")

    has_reply = bool(message.reply_to_message)
    target, duration, reason = parse_args(command.args, has_reply, with_duration=True)

    if not has_reply and not target:
        return await message.reply(
            "📋 <b>Использование</b>\n"
            "<code>/ban [@ник | ID | reply] [время] причина</code>",
            parse_mode="HTML",
        )

    user_id, name = await resolve_target(message, target)
    if not user_id:
        return await message.reply(
            "❌ <b>Не удалось найти пользователя.</b>\n\n"
            "Ответьте на его сообщение или укажите числовой ID.\n"
            "<i>Поиск по @юзернейму недоступен для пользователей, "
            "которые не писали в чат.</i>",
            parse_mode="HTML",
        )

    is_basic_group = message.chat.type == "group"

    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=user_id, until_date=duration)

        if is_basic_group:
            # В базовой группе бан = исключение; чёрного списка нет
            await message.reply(
                "👢 <b>Пользователь исключён</b>\n\n"
                f"👤 {user_link(user_id, name)}\n"
                f"{format_duration_line(duration)}\n"
                f"📝 Причина: {escape(reason)}\n\n"
                "⚠️ <i>Это базовая группа — чёрного списка нет, "
                "пользователь может вернуться по ссылке. Для настоящих банов "
                "переведите чат в супергруппу.</i>",
                parse_mode="HTML",
            )
            return

        save_ban(message.chat.id, user_id, duration, reason)
        await message.reply(
            "🔨 <b>Пользователь забанен</b>\n\n"
            f"👤 {user_link(user_id, name)}\n"
            f"{format_duration_line(duration)}\n"
            f"📝 Причина: {escape(reason)}",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        await message.reply(
            f"❌ <b>Не удалось забанить</b>\n<code>{escape(str(e))}</code>",
            parse_mode="HTML",
        )


@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ <b>Недостаточно прав.</b>", parse_mode="HTML")

    if message.chat.type == "group":
        return await message.reply(
            "ℹ️ <b>Разбан недоступен</b>\n\n"
            "Это базовая группа. Здесь бан работает как исключение — "
            "чёрного списка нет, и пользователь может вернуться по ссылке "
            "самостоятельно.\n\n"
            "Чтобы пользоваться полноценными бан/разбан — переведите чат "
            "в супергруппу: <i>Настройки чата → Тип чата → Обновить до супергруппы</i>.",
            parse_mode="HTML",
        )

    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, with_duration=False)

    if not has_reply and not target:
        return await message.reply(
            "📋 <b>Использование</b>\n<code>/unban [@ник | ID | reply] причина</code>",
            parse_mode="HTML",
        )

    user_id, name = await resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ <b>Не удалось найти пользователя.</b>", parse_mode="HTML")

    try:
        await bot.unban_chat_member(chat_id=message.chat.id, user_id=user_id, only_if_banned=True)
        remove_ban(message.chat.id, user_id)
        await message.reply(
            "✅ <b>Пользователь разбанен</b>\n\n"
            f"👤 {user_link(user_id, name)}\n"
            f"📝 Причина: {escape(reason)}",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        await message.reply(
            f"❌ <b>Не удалось разбанить</b>\n<code>{escape(str(e))}</code>",
            parse_mode="HTML",
        )


# ─── ХЭНДЛЕРЫ: МУТ ─────────────────────────────────────────────

@dp.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ <b>Недостаточно прав.</b>", parse_mode="HTML")

    has_reply = bool(message.reply_to_message)
    target, duration, reason = parse_args(command.args, has_reply, with_duration=True)

    if not has_reply and not target:
        return await message.reply(
            "📋 <b>Использование</b>\n"
            "<code>/mute [@ник | ID | reply] [время] причина</code>",
            parse_mode="HTML",
        )

    user_id, name = await resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ <b>Не удалось найти пользователя.</b>", parse_mode="HTML")

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=duration,
        )
        save_mute(message.chat.id, user_id, duration, reason)
        await message.reply(
            "🔇 <b>Пользователь замучен</b>\n\n"
            f"👤 {user_link(user_id, name)}\n"
            f"{format_duration_line(duration)}\n"
            f"📝 Причина: {escape(reason)}",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        await message.reply(
            f"❌ <b>Не удалось замутить</b>\n<code>{escape(str(e))}</code>",
            parse_mode="HTML",
        )


@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ <b>Недостаточно прав.</b>", parse_mode="HTML")

    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, with_duration=False)

    if not has_reply and not target:
        return await message.reply(
            "📋 <b>Использование</b>\n<code>/unmute [@ник | ID | reply] причина</code>",
            parse_mode="HTML",
        )

    user_id, name = await resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ <b>Не удалось найти пользователя.</b>", parse_mode="HTML")

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            ),
        )
        remove_mute(message.chat.id, user_id)
        await message.reply(
            "🔊 <b>Мут снят</b>\n\n"
            f"👤 {user_link(user_id, name)}\n"
            f"📝 Причина: {escape(reason)}",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        await message.reply(
            f"❌ <b>Не удалось снять мут</b>\n<code>{escape(str(e))}</code>",
            parse_mode="HTML",
        )


# ─── ХЭНДЛЕРЫ: ВАРНЫ ───────────────────────────────────────────

@dp.message(Command("warn"))
async def cmd_warn(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ <b>Недостаточно прав.</b>", parse_mode="HTML")

    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, with_duration=False)

    if not has_reply and not target:
        return await message.reply(
            "📋 <b>Использование</b>\n<code>/warn [@ник | ID | reply] причина</code>",
            parse_mode="HTML",
        )

    user_id, name = await resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ <b>Не удалось найти пользователя.</b>", parse_mode="HTML")

    count = add_warn(message.chat.id, user_id, reason)
    progress = "🟩" * count + "⬜" * max(0, WARN_LIMIT - count)
    await message.reply(
        "⚠️ <b>Выдано предупреждение</b>\n\n"
        f"👤 {user_link(user_id, name)}\n"
        f"📊 {progress}  <b>{count}/{WARN_LIMIT}</b>\n"
        f"📝 Причина: {escape(reason)}",
        parse_mode="HTML",
    )

    if count >= WARN_LIMIT:
        until = datetime.utcnow() + timedelta(days=AUTO_BAN_DAYS)
        try:
            await bot.ban_chat_member(chat_id=message.chat.id, user_id=user_id, until_date=until)
            save_ban(message.chat.id, user_id, until, f"Автобан: {WARN_LIMIT} варнов")
            reset_warns(message.chat.id, user_id)
            await message.answer(
                "🚫 <b>Автоматический бан</b>\n\n"
                f"👤 {user_link(user_id, name)}\n"
                f"{format_duration_line(until)}\n"
                f"📝 Причина: достигнут лимит предупреждений ({WARN_LIMIT})",
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            await message.answer(
                f"❌ <b>Автобан не удался</b>\n<code>{escape(str(e))}</code>",
                parse_mode="HTML",
            )


@dp.message(Command("unwarn"))
async def cmd_unwarn(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ <b>Недостаточно прав.</b>", parse_mode="HTML")

    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, with_duration=False)

    if not has_reply and not target:
        return await message.reply(
            "📋 <b>Использование</b>\n<code>/unwarn [@ник | ID | reply] причина</code>",
            parse_mode="HTML",
        )

    user_id, name = await resolve_target(message, target)
    if not user_id:
        return await message.reply("❌ <b>Не удалось найти пользователя.</b>", parse_mode="HTML")

    reset_warns(message.chat.id, user_id)
    await message.reply(
        "✅ <b>Все предупреждения сняты</b>\n\n"
        f"👤 {user_link(user_id, name)}\n"
        f"📝 Причина: {escape(reason)}",
        parse_mode="HTML",
    )


# ─── СПИСКИ ─────────────────────────────────────────────────────

def _render_list(title: str, rows: list[tuple], empty_text: str) -> str:
    if not rows:
        return empty_text
    lines = [title, ""]
    for user_id, until, reason in rows:
        until_dt = datetime.fromisoformat(until) if until else None
        lines.append(
            f"• {user_link(user_id)} — <b>{humanize_until(until_dt)}</b>\n"
            f"   <i>{escape(reason)}</i>"
        )
    return "\n".join(lines)


@dp.message(Command("banlist"))
async def cmd_banlist(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ <b>Недостаточно прав.</b>", parse_mode="HTML")

    if message.chat.type == "group":
        return await message.reply(
            "ℹ️ <b>Список забаненных недоступен</b>\n\n"
            "В базовой группе нет чёрного списка. "
            "Переведите чат в супергруппу для полноценной работы.",
            parse_mode="HTML",
        )

    rows = get_list("bans", message.chat.id)
    await message.reply(
        _render_list("🔨 <b>Забаненные</b>", rows, "📭 <i>Список пуст</i>"),
        parse_mode="HTML",
    )


@dp.message(Command("mutelist"))
async def cmd_mutelist(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ <b>Недостаточно прав.</b>", parse_mode="HTML")
    rows = get_list("mutes", message.chat.id)
    await message.reply(
        _render_list("🔇 <b>Замученные</b>", rows, "📭 <i>Список пуст</i>"),
        parse_mode="HTML",
    )


@dp.message(Command("warnlist"))
async def cmd_warnlist(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ <b>Недостаточно прав.</b>", parse_mode="HTML")
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id, COUNT(*) FROM warns WHERE chat_id=? GROUP BY user_id",
            (message.chat.id,),
        ).fetchall()
    if not rows:
        return await message.reply("📭 <i>Список предупреждений пуст</i>", parse_mode="HTML")
    lines = ["⚠️ <b>Предупреждения</b>", ""]
    for user_id, count in rows:
        progress = "🟩" * count + "⬜" * max(0, WARN_LIMIT - count)
        lines.append(f"• {user_link(user_id)} — {progress} <b>{count}/{WARN_LIMIT}</b>")
    await message.reply("\n".join(lines), parse_mode="HTML")


# ─── ХЭЛП ───────────────────────────────────────────────────────

@dp.message(Command("help", "start"))
async def cmd_help(message: types.Message):
    await message.reply(
        "🛡 <b>SpaGuard</b> — модерация чата\n"
        "\n"
        "🔨 <b>Баны</b>\n"
        "<code>/ban [@ник | ID | reply] [время] причина</code>\n"
        "<code>/unban [@ник | ID | reply] причина</code>\n"
        "\n"
        "🔇 <b>Муты</b>\n"
        "<code>/mute [@ник | ID | reply] [время] причина</code>\n"
        "<code>/unmute [@ник | ID | reply] причина</code>\n"
        "\n"
        "⚠️ <b>Предупреждения</b>\n"
        "<code>/warn [@ник | ID | reply] причина</code>\n"
        "<code>/unwarn [@ник | ID | reply] причина</code>\n"
        "\n"
        "📋 <b>Списки</b>\n"
        "<code>/banlist</code> · <code>/mutelist</code> · <code>/warnlist</code>\n"
        "\n"
        "⏱ <b>Формат времени:</b> <code>30s</code>, <code>10m</code>, <code>2h</code>, <code>7d</code>\n"
        f"ℹ️ {WARN_LIMIT} предупреждения → автобан на {AUTO_BAN_DAYS} дн.\n\n"
        "<i>Совет: для надёжности используйте reply на сообщение нарушителя "
        "или числовой ID. @юзернейм работает не всегда.</i>",
        parse_mode="HTML",
    )


# ─── ЗАПУСК ─────────────────────────────────────────────────────

async def main():
    logger.info("SpaGuard запускается…")
    logger.info("WARN_LIMIT=%s, AUTO_BAN_DAYS=%s", WARN_LIMIT, AUTO_BAN_DAYS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
