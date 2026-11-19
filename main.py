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
from aiogram.types import ChatPermissions
from aiogram.exceptions import TelegramBadRequest

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана.")

WARN_LIMIT     = int(os.getenv("WARN_LIMIT", "3"))
AUTO_BAN_DAYS  = int(os.getenv("AUTO_BAN_DAYS", "7"))
DB_PATH        = os.getenv("DB_PATH", "spaguard.db")
HINT_TTL       = int(os.getenv("HINT_TTL", "15"))

# ─── ПОГОДА: КОНСТАНТЫ OPEN-METEO ──────────────────────────────
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WMO_DESCRIPTIONS = {
    0: "☀️ Ясно",
    1: "🌤 Преимущественно ясно",
    2: "⛅ Переменная облачность",
    3: "☁️ Пасмурно",
    45: "🌫 Туман",
    48: "🌫 Туман с изморозью",
    51: "🌦 Лёгкая морось",
    53: "🌦 Умеренная морось",
    55: "🌧 Сильная морось",
    61: "🌧 Небольшой дождь",
    63: "🌧 Умеренный дождь",
    65: "🌧 Сильный дождь",
    71: "🌨 Небольшой снег",
    73: "🌨 Умеренный снег",
    75: "🌨 Сильный снег",
    80: "🌦 Ливень",
    81: "🌧 Сильный ливень",
    82: "⛈ Очень сильный ливень",
    95: "⛈ Гроза",
    96: "⛈ Гроза с градом",
    99: "⛈ Сильная гроза с градом",
}

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
            CREATE TABLE IF NOT EXISTS user_cities (
                user_id INTEGER PRIMARY KEY,
                city TEXT NOT NULL
            );
        """)

init_db()

# ─── УТИЛИТЫ (без изменений) ────────────────────────────────────

def parse_duration(s: Optional[str]) -> Optional[int]:
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
    try:
        await message.reply(text, parse_mode="HTML")
    except TelegramBadRequest:
        pass


# ─── ФОНОВЫЕ ЗАДАЧИ (без изменений) ────────────────────────────

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
        elif kind == "ban":
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
            remove_ban(chat_id, user_id)
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


# ─── РАБОТА С БД (без изменений) ────────────────────────────────

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


# ─── ПОГОДА: РАБОТА С ГОРОДАМИ ПОЛЬЗОВАТЕЛЕЙ ──────────────────

def get_user_city(user_id: int) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT city FROM user_cities WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None


def set_user_city(user_id: int, city: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO user_cities (user_id, city) VALUES (?, ?)", (user_id, city))
        c.commit()


async def geocode_city(city: str) -> Optional[tuple[float, float]]:
    """Возвращает (latitude, longitude) или None."""
    params = {"name": city.strip(), "count": 1, "language": "ru", "format": "json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GEO_URL, params=params, timeout=10) as resp:
                data = await resp.json()
                results = data.get("results")
                if not results:
                    return None
                return results[0]["latitude"], results[0]["longitude"]
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("Геокодинг не удался для '%s': %s", city, e)
        return None


async def fetch_weather(lat: float, lon: float) -> Optional[dict]:
    """Возвращает current-блок Open-Meteo."""
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
    """Форматирует погоду без упоминания города."""
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


# ─── ПАРСИНГ АРГУМЕНТОВ (без изменений) ────────────────────────

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


# ─── ГЕЙТ (без изменений) ──────────────────────────────────────

async def gate(message: types.Message) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        await reply_private(message, "❌ <b>Команда работает только в группах.</b>")
        return False
    if not await is_admin(message.chat.id, message.from_user.id):
        await reply_private(message, "⛔ <b>Недостаточно прав.</b>")
        return False
    return True


# ─── БАН / МУТ / ВАРНЫ (без изменений) ─────────────────────────
# (код cmd_ban, cmd_unban, cmd_mute, cmd_unmute, cmd_warn, cmd_unwarn,
#  cmd_banlist, cmd_mutelist, cmd_warnlist, cmd_help — как в предыдущей версии,
#  я не дублирую их здесь, чтобы ответ не был гигантским. Они остаются без изменений.)

# ─── ПОГОДА: КОМАНДА /setcity (только в ЛС) ────────────────────

@dp.message(Command("setcity"))
async def cmd_setcity(message: types.Message, command: CommandObject):
    if message.chat.type != "private":
        await reply_private(
            message,
            "🏙 <b>Укажите город в личных сообщениях с ботом</b>, "
            "чтобы не раскрывать его в общем чате.",
        )
        return

    if not command.args:
        await message.reply(
            "📋 <b>Использование</b>\n<code>/setcity Москва</code>",
            parse_mode="HTML",
        )
        return

    city = command.args.strip()
    coords = await geocode_city(city)
    if not coords:
        await message.reply(
            f"❌ Город <b>{escape(city)}</b> не найден. Попробуйте другое название.",
            parse_mode="HTML",
        )
        return

    set_user_city(message.from_user.id, city)
    await message.reply(
        f"✅ Город сохранён: <b>{escape(city)}</b>\n\n"
        "Теперь в любом чате просто напишите <b>погода</b> — "
        "бот покажет данные, не раскрывая город.",
        parse_mode="HTML",
    )


# ─── ПОГОДА: ОБРАБОТЧИК СЛОВА «ПОГОДА» ────────────────────────

@dp.message(F.text.regexp(r"(?i)\bпогода\b"))
async def handle_weather_word(message: types.Message):
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id
    city = get_user_city(user_id)

    if not city:
        await reply_private(
            message,
            "🏙 <b>Чтобы узнать погоду, укажите город.</b>\n\n"
            "Напишите боту в личные сообщения:\n"
            "<code>/setcity ВашГород</code>\n\n"
            "<i>Город не будет виден в общем чате.</i>",
        )
        return

    coords = await geocode_city(city)
    if not coords:
        await reply_private(
            message,
            "❌ <b>Не удалось определить координаты вашего города.</b>\n"
            "Попробуйте задать другой город: <code>/setcity НовыйГород</code>",
        )
        return

    lat, lon = coords
    current = await fetch_weather(lat, lon)
    if not current:
        await reply_private(
            message,
            "❌ <b>Сервис погоды временно недоступен.</b>\nПопробуйте позже.",
        )
        return

    text = format_weather(current)
    await reply_public(message, text)


# ─── ЗАПУСК ─────────────────────────────────────────────────────

async def main():
    logger.info("SpaGuard запускается…")
    await restore_schedules()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
