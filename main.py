import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime
from html import escape
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    ChatPermissions,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramBadRequest

# ─── КОНФИГУРАЦИЯ ──────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана.")

WARN_LIMIT    = int(os.getenv("WARN_LIMIT", "3"))
AUTO_BAN_DAYS = int(os.getenv("AUTO_BAN_DAYS", "7"))
DB_PATH       = os.getenv("DB_PATH", "spaguard.db")
HINT_TTL      = int(os.getenv("HINT_TTL", "15"))

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WMO_DESCRIPTIONS = {
    0: "☀️ Ясно", 1: "🌤 Преимущественно ясно", 2: "⛅ Переменная облачность",
    3: "☁️ Пасмурно", 45: "🌫 Туман", 48: "🌫 Туман с изморозью",
    51: "🌦 Лёгкая морось", 53: "🌦 Умеренная морось", 55: "🌧 Сильная морось",
    61: "🌧 Небольшой дождь", 63: "🌧 Умеренный дождь", 65: "🌧 Сильный дождь",
    71: "🌨 Небольшой снег", 73: "🌨 Умеренный снег", 75: "🌨 Сильный снег",
    80: "🌦 Ливень", 81: "🌧 Сильный ливень", 82: "⛈ Очень сильный ливень",
    95: "⛈ Гроза", 96: "⛈ Гроза с градом", 99: "⛈ Сильная гроза с градом",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("SpaGuard")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ─── БАЗА ДАННЫХ ───────────────────────────────────────────────

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bans (
                chat_id INTEGER, user_id INTEGER, until_ts INTEGER, reason TEXT,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS mutes (
                chat_id INTEGER, user_id INTEGER, until_ts INTEGER, reason TEXT,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, user_id INTEGER, reason TEXT, created_ts INTEGER
            );
            CREATE TABLE IF NOT EXISTS user_locations (
                user_id INTEGER PRIMARY KEY,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                city_label TEXT NOT NULL
            );
        """)

init_db()

# ─── УТИЛИТЫ ───────────────────────────────────────────────────

def parse_duration(s: Optional[str]) -> Optional[int]:
    """'10s' / '30m' / '2h' / '7d' → Unix-timestamp окончания."""
    if not s:
        return None
    unit = s[-1].lower()
    try:
        amount = int(s[:-1])
    except ValueError:
        return None
    if amount <= 0:
        return None
    deltas = {"s": amount, "m": amount * 60, "h": amount * 3600, "d": amount * 86400}
    seconds = deltas.get(unit)
    if not seconds:
        return None
    return int(time.time()) + seconds


def humanize_until(until_ts: Optional[int]) -> str:
    if until_ts is None:
        return "навсегда"
    total = until_ts - int(time.time())
    if total <= 0:
        return "истёк"
    if total >= 86400:
        return f"{total // 86400} дн."
    if total >= 3600:
        return f"{total // 3600} ч."
    if total >= 60:
        return f"{total // 60} мин."
    return f"{total} сек."


def format_duration_line(until_ts: Optional[int]) -> str:
    if until_ts is None:
        return "⏱ Срок: <b>навсегда</b>"
    dt = datetime.utcfromtimestamp(until_ts)
    return (
        f"⏱ Срок: <b>{humanize_until(until_ts)}</b>  ·  "
        f"<i>{dt.strftime('%d.%m.%Y %H:%M')} UTC</i>"
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
    """reply (не бот) → числовой ID → @username (последнее редко работает)."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        if not u.is_bot:
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


# ─── УПРАВЛЕНИЕ ВИДИМОСТЬЮ ОТВЕТОВ ─────────────────────────────

async def _auto_delete(msg: types.Message, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def reply_private(message: types.Message, text: str, ttl: int = HINT_TTL) -> None:
    """Подсказка/ошибка — только тому, кто вызвал команду."""
    try:
        await bot.send_message(message.from_user.id, text, parse_mode="HTML")
        return
    except TelegramBadRequest:
        pass
    try:
        sent = await message.reply(text, parse_mode="HTML")
        asyncio.create_task(_auto_delete(sent, ttl))
    except TelegramBadRequest:
        pass


async def reply_public(message: types.Message, text: str) -> None:
    """Публичный ответ — видят все."""
    try:
        await message.reply(text, parse_mode="HTML")
    except TelegramBadRequest:
        pass


# ─── ФОНОВЫЕ ЗАДАЧИ ДЛЯ АВТО-СНЯТИЯ ───────────────────────────

scheduled_tasks: dict[tuple[int, int, str], asyncio.Task] = {}


async def _auto_lift(chat_id: int, user_id: int, kind: str, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        if kind == "mute":
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=True, can_send_media_messages=True,
                    can_send_other_messages=True, can_add_web_page_previews=True,
                ),
            )
            remove_mute(chat_id, user_id)
            logger.info("Авто-размут %s в %s", user_id, chat_id)
        elif kind == "ban":
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
            remove_ban(chat_id, user_id)
            logger.info("Авто-разбан %s в %s", user_id, chat_id)
    except TelegramBadRequest as e:
        logger.warning("Авто-снятие %s: %s", kind, e)
    finally:
        scheduled_tasks.pop((chat_id, user_id, kind), None)


def schedule_lift(chat_id: int, user_id: int, kind: str, until_ts: Optional[int]) -> None:
    if until_ts is None:
        return
    delay = until_ts - int(time.time())
    if delay <= 0:
        return
    key = (chat_id, user_id, kind)
    old = scheduled_tasks.pop(key, None)
    if old:
        old.cancel()
    scheduled_tasks[key] = asyncio.create_task(_auto_lift(chat_id, user_id, kind, delay))


async def restore_schedules() -> None:
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as c:
        mutes = c.execute(
            "SELECT chat_id, user_id, until_ts FROM mutes WHERE until_ts IS NOT NULL AND until_ts > ?",
            (now,),
        ).fetchall()
        bans = c.execute(
            "SELECT chat_id, user_id, until_ts FROM bans WHERE until_ts IS NOT NULL AND until_ts > ?",
            (now,),
        ).fetchall()
    for chat_id, user_id, until_ts in mutes:
        schedule_lift(chat_id, user_id, "mute", until_ts)
    for chat_id, user_id, until_ts in bans:
        schedule_lift(chat_id, user_id, "ban", until_ts)
    if mutes or bans:
        logger.info("Восстановлено задач: mutes=%s, bans=%s", len(mutes), len(bans))


# ─── РАБОТА С БД ───────────────────────────────────────────────

def _conn():
    return sqlite3.connect(DB_PATH)


def add_warn(chat_id: int, user_id: int, reason: str) -> int:
    with _conn() as c:
        c.execute(
            "INSERT INTO warns (chat_id, user_id, reason, created_ts) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, reason, int(time.time())),
        )
        c.commit()
        return c.execute(
            "SELECT COUNT(*) FROM warns WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()[0]


def reset_warns(chat_id: int, user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        c.commit()


def save_ban(chat_id, user_id, until_ts, reason):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO bans VALUES (?, ?, ?, ?)",
                  (chat_id, user_id, until_ts, reason))
        c.commit()


def remove_ban(chat_id, user_id):
    with _conn() as c:
        c.execute("DELETE FROM bans WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        c.commit()


def save_mute(chat_id, user_id, until_ts, reason):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO mutes VALUES (?, ?, ?, ?)",
                  (chat_id, user_id, until_ts, reason))
        c.commit()


def remove_mute(chat_id, user_id):
    with _conn() as c:
        c.execute("DELETE FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        c.commit()


def get_list(table: str, chat_id: int) -> list[tuple]:
    with _conn() as c:
        return c.execute(
            f"SELECT user_id, until_ts, reason FROM {table} WHERE chat_id=?",
            (chat_id,),
        ).fetchall()


# ─── ПОГОДА: ЛОКАЦИЯ ──────────────────────────────────────────

def get_user_location(user_id: int) -> Optional[tuple[float, float, str]]:
    with _conn() as c:
        row = c.execute(
            "SELECT lat, lon, city_label FROM user_locations WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row


def set_user_location(user_id: int, lat: float, lon: float, city_label: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO user_locations (user_id, lat, lon, city_label) VALUES (?, ?, ?, ?)",
            (user_id, lat, lon, city_label),
        )
        c.commit()


async def fetch_weather(lat: float, lon: float) -> Optional[dict]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(FORECAST_URL, params=params, timeout=10) as resp:
                data = await resp.json()
                return data.get("current")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("Запрос погоды не удался: %s", e)
        return None


def format_weather(current: dict) -> str:
    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")
    code = current.get("weather_code")
    desc = WMO_DESCRIPTIONS.get(code, "🌡 Погода")

    lines = [f"{desc}", ""]
    if temp is not None:
        line = f"🌡 <b>{temp}°C</b>"
        if feels is not None:
            line += f"  (ощущается как {feels}°C)"
        lines.append(line)
    if humidity is not None:
        lines.append(f"💧 Влажность: <b>{humidity}%</b>")
    if wind is not None:
        lines.append(f"💨 Ветер: <b>{wind} км/ч</b>")
    return "\n".join(lines)


# ─── ПАРСИНГ АРГУМЕНТОВ ────────────────────────────────────────

def parse_args(command_args: Optional[str], has_reply: bool, with_duration: bool):
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


# ─── ГЕЙТ: только группа + только админ ───────────────────────

async def gate(message: types.Message) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        await reply_private(message, "❌ <b>Команда работает только в группах.</b>")
        return False
    if not await is_admin(message.chat.id, message.from_user.id):
        await reply_private(message, "⛔ <b>Недостаточно прав.</b>")
        return False
    return True


# ─── БАН ───────────────────────────────────────────────────────

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if not await gate(message):
        return

    has_reply = bool(message.reply_to_message)
    target, until_ts, reason = parse_args(command.args, has_reply, True)

    if not has_reply and not target:
        await reply_private(
            message,
            "📋 <b>Использование</b>\n<code>/ban [@ник | ID | reply] [время] причина</code>",
        )
        return

    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_private(
            message,
            "❌ <b>Не удалось найти пользователя.</b>\n"
            "Ответьте на его сообщение или укажите числовой ID.\n"
            "<i>Поиск по @юзернейму недоступен для тех, кто не писал в чат.</i>",
        )
        return

    is_basic = message.chat.type == "group"
    try:
        await bot.ban_chat_member(message.chat.id, user_id, until_date=until_ts)

        if is_basic:
            await reply_public(
                message,
                "👢 <b>Пользователь исключён</b>\n\n"
                f"👤 {user_link(user_id, name)}\n"
                f"{format_duration_line(until_ts)}\n"
                f"📝 Причина: {escape(reason)}",
            )
            await reply_private(
                message,
                "⚠️ <i>Это базовая группа — чёрного списка нет. "
                "Переведите чат в супергруппу для полноценных банов.</i>",
            )
            return

        save_ban(message.chat.id, user_id, until_ts, reason)
        schedule_lift(message.chat.id, user_id, "ban", until_ts)

        await reply_public(
            message,
            "🔨 <b>Пользователь забанен</b>\n\n"
            f"👤 {user_link(user_id, name)}\n"
            f"{format_duration_line(until_ts)}\n"
            f"📝 Причина: {escape(reason)}",
        )
    except TelegramBadRequest as e:
        await reply_private(
            message,
            f"❌ <b>Не удалось забанить</b>\n<code>{escape(str(e))}</code>",
        )


@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    if not await gate(message):
        return

    if message.chat.type == "group":
        await reply_private(
            message,
            "ℹ️ <b>Разбан недоступен в базовой группе.</b>\n"
            "Переведите чат в супергруппу: <i>Настройки → Тип чата → Обновить до супергруппы</i>.",
        )
        return

    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, False)

    if not has_reply and not target:
        await reply_private(
            message,
            "📋 <b>Использование</b>\n<code>/unban [@ник | ID | reply] причина</code>",
        )
        return

    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_private(message, "❌ <b>Не удалось найти пользователя.</b>")
        return

    try:
        await bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
        remove_ban(message.chat.id, user_id)
        scheduled_tasks.pop((message.chat.id, user_id, "ban"), None)

        await reply_public(
            message,
            "✅ <b>Пользователь разбанен</b>\n\n"
            f"👤 {user_link(user_id, name)}\n"
            f"📝 Причина: {escape(reason)}",
        )
    except TelegramBadRequest as e:
        await reply_private(
            message,
            f"❌ <b>Не удалось разбанить</b>\n<code>{escape(str(e))}</code>",
        )


# ─── МУТ ──────────────────────────────────────────────────────

@dp.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    if not await gate(message):
        return

    has_reply = bool(message.reply_to_message)
    target, until_ts, reason = parse_args(command.args, has_reply, True)

    if not has_reply and not target:
        await reply_private(
            message,
            "📋 <b>Использование</b>\n<code>/mute [@ник | ID | reply] [время] причина</code>",
        )
        return

    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_private(message, "❌ <b>Не удалось найти пользователя.</b>")
        return

    try:
        await bot.restrict_chat_member(
            message.chat.id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_ts,
        )
        save_mute(message.chat.id, user_id, until_ts, reason)
        schedule_lift(message.chat.id, user_id, "mute", until_ts)

        await reply_public(
            message,
            "🔇 <b>Пользователь замучен</b>\n\n"
            f"👤 {user_link(user_id, name)}\n"
            f"{format_duration_line(until_ts)}\n"
            f"📝 Причина: {escape(reason)}",
        )
    except TelegramBadRequest as e:
        await reply_private(
            message,
            f"❌ <b>Не удалось замутить</b>\n<code>{escape(str(e))}</code>",
        )


@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message, command: CommandObject):
    if not await gate(message):
        return

    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, False)

    if not has_reply and not target:
        await reply_private(
            message,
            "📋 <b>Использование</b>\n<code>/unmute [@ник | ID | reply] причина</code>",
        )
        return

    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_private(message, "❌ <b>Не удалось найти пользователя.</b>")
        return

    try:
        await bot.restrict_chat_member(
            message.chat.id, user_id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            ),
        )
        remove_mute(message.chat.id, user_id)
        scheduled_tasks.pop((message.chat.id, user_id, "mute"), None)

        await reply_public(
            message,
            "🔊 <b>Мут снят</b>\n\n"
            f"👤 {user_link(user_id, name)}\n"
            f"📝 Причина: {escape(reason)}",
        )
    except TelegramBadRequest as e:
        await reply_private(
            message,
            f"❌ <b>Не удалось снять мут</b>\n<code>{escape(str(e))}</code>",
        )


# ─── ВАРНЫ ────────────────────────────────────────────────────

@dp.message(Command("warn"))
async def cmd_warn(message: types.Message, command: CommandObject):
    if not await gate(message):
        return

    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, False)

    if not has_reply and not target:
        await reply_private(
            message,
            "📋 <b>Использование</b>\n<code>/warn [@ник | ID | reply] причина</code>",
        )
        return

    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_private(message, "❌ <b>Не удалось найти пользователя.</b>")
        return

    count = add_warn(message.chat.id, user_id, reason)
    progress = "🟩" * count + "⬜" * max(0, WARN_LIMIT - count)

    await reply_public(
        message,
        "⚠️ <b>Выдано предупреждение</b>\n\n"
        f"👤 {user_link(user_id, name)}\n"
        f"📊 {progress}  <b>{count}/{WARN_LIMIT}</b>\n"
        f"📝 Причина: {escape(reason)}",
    )

    if count >= WARN_LIMIT:
        until_ts = int(time.time()) + AUTO_BAN_DAYS * 86400
        try:
            await bot.ban_chat_member(message.chat.id, user_id, until_date=until_ts)
            save_ban(message.chat.id, user_id, until_ts, f"Автобан: {WARN_LIMIT} варнов")
            reset_warns(message.chat.id, user_id)
            schedule_lift(message.chat.id, user_id, "ban", until_ts)

            await reply_public(
                message,
                "🚫 <b>Автоматический бан</b>\n\n"
                f"👤 {user_link(user_id, name)}\n"
                f"{format_duration_line(until_ts)}\n"
                f"📝 Причина: достигнут лимит предупреждений ({WARN_LIMIT})",
            )
        except TelegramBadRequest as e:
            await reply_private(
                message,
                f"❌ <b>Автобан не удался</b>\n<code>{escape(str(e))}</code>",
            )


@dp.message(Command("unwarn"))
async def cmd_unwarn(message: types.Message, command: CommandObject):
    if not await gate(message):
        return

    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, False)

    if not has_reply and not target:
        await reply_private(
            message,
            "📋 <b>Использование</b>\n<code>/unwarn [@ник | ID | reply] причина</code>",
        )
        return

    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_private(message, "❌ <b>Не удалось найти пользователя.</b>")
        return

    reset_warns(message.chat.id, user_id)
    await reply_public(
        message,
        "✅ <b>Все предупреждения сняты</b>\n\n"
        f"👤 {user_link(user_id, name)}\n"
        f"📝 Причина: {escape(reason)}",
    )


# ─── СПИСКИ ────────────────────────────────────────────────────

def _render_list(title: str, rows: list[tuple], empty: str) -> str:
    if not rows:
        return empty
    lines = [title, ""]
    for user_id, until_ts, reason in rows:
        lines.append(
            f"• {user_link(user_id)} — <b>{humanize_until(until_ts)}</b>\n"
            f"   <i>{escape(reason)}</i>"
        )
    return "\n".join(lines)


@dp.message(Command("banlist"))
async def cmd_banlist(message: types.Message):
    if not await gate(message):
        return
    if message.chat.type == "group":
        await reply_private(
            message,
            "ℹ️ <b>Список забаненных недоступен в базовой группе.</b>\n"
            "Переведите чат в супергруппу для чёрного списка.",
        )
        return
    rows = get_list("bans", message.chat.id)
    await reply_private(message, _render_list("🔨 <b>Забаненные</b>", rows, "📭 <i>Список пуст</i>"))


@dp.message(Command("mutelist"))
async def cmd_mutelist(message: types.Message):
    if not await gate(message):
        return
    rows = get_list("mutes", message.chat.id)
    await reply_private(message, _render_list("🔇 <b>Замученные</b>", rows, "📭 <i>Список пуст</i>"))


@dp.message(Command("warnlist"))
async def cmd_warnlist(message: types.Message):
    if not await gate(message):
        return
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id, COUNT(*) FROM warns WHERE chat_id=? GROUP BY user_id",
            (message.chat.id,),
        ).fetchall()
    if not rows:
        await reply_private(message, "📭 <i>Список предупреждений пуст</i>")
        return
    lines = ["⚠️ <b>Предупреждения</b>", ""]
    for user_id, count in rows:
        progress = "🟩" * count + "⬜" * max(0, WARN_LIMIT - count)
        lines.append(f"• {user_link(user_id)} — {progress} <b>{count}/{WARN_LIMIT}</b>")
    await reply_private(message, "\n".join(lines))


# ─── ГЕОЛОКАЦИЯ (сохранение координат) ─────────────────────────

@dp.message(F.location)
async def handle_location(message: types.Message):
    loc = message.location
    label = f"{loc.latitude:.4f},{loc.longitude:.4f}"
    set_user_location(message.from_user.id, loc.latitude, loc.longitude, label)

    await message.reply(
        "✅ <b>Локация сохранена.</b>\n\n"
        "Теперь в любом чате просто напишите <b>погода</b> — "
        "бот покажет данные, не раскрывая ваш город.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


# ─── ПОГОДА ПО СЛОВУ «ПОГОДА» ─────────────────────────────────

@dp.message(F.text.regexp(r"(?i)\bпогода\b"))
async def handle_weather_word(message: types.Message):
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id
    loc = get_user_location(user_id)

    if not loc:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await reply_private(
            message,
            "🏙 <b>Чтобы узнать погоду, разрешите доступ к геолокации.</b>\n\n"
            "Нажмите кнопку ниже — это одноразовое действие. "
            "Ваш город не будет виден в чате.",
            ttl=30,
        )
        try:
            await message.answer("Выберите действие:", reply_markup=keyboard)
        except TelegramBadRequest:
            pass
        return

    lat, lon, _ = loc
    current = await fetch_weather(lat, lon)
    if not current:
        await reply_private(message, "❌ <b>Сервис погоды временно недоступен.</b>")
        return

    text = format_weather(current)
    await reply_public(message, text)


# ─── ХЭЛП ──────────────────────────────────────────────────────

@dp.message(Command("help", "start"))
async def cmd_help(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        if not await is_admin(message.chat.id, message.from_user.id):
            await reply_private(message, "⛔ <b>Недостаточно прав.</b>")
            return

    text = (
        "🛡 <b>SpaGuard</b> — модерация чата\n\n"
        "🔨 <b>Баны</b>\n"
        "<code>/ban [@ник | ID | reply] [время] причина</code>\n"
        "<code>/unban [@ник | ID | reply] причина</code>\n\n"
        "🔇 <b>Муты</b>\n"
        "<code>/mute [@ник | ID | reply] [время] причина</code>\n"
        "<code>/unmute [@ник | ID | reply] причина</code>\n\n"
        "⚠️ <b>Предупреждения</b>\n"
        "<code>/warn [@ник | ID | reply] причина</code>\n"
        "<code>/unwarn [@ник | ID | reply] причина</code>\n\n"
        "📋 <b>Списки</b>\n"
        "<code>/banlist</code> · <code>/mutelist</code> · <code>/warnlist</code>\n\n"
        "⏱ <b>Время:</b> <code>30s</code>, <code>10m</code>, <code>2h</code>, <code>7d</code>\n"
        f"ℹ️ {WARN_LIMIT} предупреждения → автобан на {AUTO_BAN_DAYS} дн.\n\n"
        "🌤 <b>Погода:</b> просто напишите слово <b>погода</b> в чат — "
        "бот попросит один раз поделиться геолокацией.\n\n"
        "<i>Совет: для надёжности используйте reply на сообщение нарушителя.</i>"
    )

    if message.chat.type in ("group", "supergroup"):
        await reply_private(message, text, ttl=60)
    else:
        await message.reply(text, parse_mode="HTML")


# ─── ЗАПУСК ─────────────────────────────────────────────────────

async def main():
    logger.info("SpaGuard запускается…")
    logger.info("WARN_LIMIT=%s, AUTO_BAN_DAYS=%s, HINT_TTL=%s",
                WARN_LIMIT, AUTO_BAN_DAYS, HINT_TTL)
    await restore_schedules()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
