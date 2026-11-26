import asyncio
import json
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
    WebAppInfo,
    ReplyKeyboardRemove,
    EphemeralMessageParameters,
)
from aiogram.exceptions import TelegramBadRequest
from rules_feature import RulesFeature

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Error BOT_TOKEN.")

WEB_APP_URL = os.getenv("WEB_APP_URL", "").strip()
if not WEB_APP_URL:
    raise RuntimeError(
        "Error WEB_APP_URL"
    )
if not WEB_APP_URL.startswith("https://"):
    raise RuntimeError(
        f"Error: {WEB_APP_URL!r}"
    )

WARN_LIMIT    = int(os.getenv("WARN_LIMIT", "3"))
AUTO_BAN_DAYS = int(os.getenv("AUTO_BAN_DAYS", "7"))
HINT_TTL      = int(os.getenv("HINT_TTL", "15"))

_db_env = os.getenv("DB_PATH", "spaguard.db")
if os.path.isabs(_db_env):
    DB_PATH = _db_env
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(BASE_DIR, _db_env)

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

BOT_USERNAME: Optional[str] = None

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


# ─── ОТПРАВКА СООБЩЕНИЙ ────────────────────────────────────────

async def _auto_delete(msg: types.Message, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def reply_hint(message: types.Message, text: str, ttl: int = HINT_TTL) -> None:
    if message.chat.type in ("group", "supergroup"):
        try:
            await message.answer(
                text,
                parse_mode="HTML",
                ephemeral_message_parameters=EphemeralMessageParameters(
                    receiver_user_id=message.from_user.id
                ),
            )
            return
        except TelegramBadRequest as e:
            logger.warning("Ephemeral не удалось: %s", e)

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
    except TelegramBadRequest as e:
        logger.warning("Public reply не удалось: %s", e)


# ─── ФОНОВЫЕ ЗАДАЧИ ────────────────────────────────────────────

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


# ─── БД ────────────────────────────────────────────────────────

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


# ─── ЛОКАЦИЯ: СОХРАНЕНИЕ С ВЕРИФИКАЦИЕЙ ──────────────────────

def get_user_location(user_id: int) -> Optional[tuple[float, float, str]]:
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute(
                "SELECT lat, lon, city_label FROM user_locations WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            logger.info("get_user_location(%s): не найдено", user_id)
        else:
            logger.info("get_user_location(%s): найдено %s", user_id, row)
        return row
    except Exception as e:
        logger.error("get_user_location(%s): ошибка БД: %s", user_id, e)
        return None


def set_user_location(user_id: int, lat: float, lon: float, city_label: str) -> None:
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT OR REPLACE INTO user_locations (user_id, lat, lon, city_label) VALUES (?, ?, ?, ?)",
                (user_id, lat, lon, city_label),
            )
            c.commit()
    except Exception as e:
        logger.error("set_user_location(%s): ошибка записи: %s", user_id, e)
        return

    # Верификация — перечитываем из БД
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute(
                "SELECT lat, lon, city_label FROM user_locations WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            logger.error("ВЕРИФИКАЦИЯ ПРОВАЛЕНА: не удалось записать локацию для %s в %s", user_id, DB_PATH)
        else:
            logger.info("ВЕРИФИКАЦИЯ OK: user_id=%s, локация=%s, БД=%s", user_id, row, DB_PATH)
    except Exception as e:
        logger.error("Верификация упала для %s: %s", user_id, e)


def delete_user_location(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM user_locations WHERE user_id=?", (user_id,))
        c.commit()


# ─── ГЕОЛОКАЦИЯ ПО IP ─────────────────────────────────────────

IP_SERVICES = [
    ("ipdata.info", "https://ipdata.info/json/{ip}",
     lambda d: ((d.get("latitude"), d.get("longitude"), d.get("city") or "")
                if d.get("latitude") is not None and d.get("longitude") is not None else None)),
    ("ipquery.io", "https://api.ipquery.io/{ip}",
     lambda d: ((d.get("location", {}).get("latitude"), d.get("location", {}).get("longitude"),
                 d.get("location", {}).get("city") or "")
                if d.get("location", {}).get("latitude") is not None else None)),
    ("ipgeolocationapi.io", "https://ipgeolocationapi.io/{ip}",
     lambda d: ((d.get("latitude"), d.get("longitude"), d.get("city") or "")
                if d.get("latitude") is not None and d.get("longitude") is not None else None)),
    ("freeipapi.com", "https://freeipapi.com/api/json/{ip}",
     lambda d: ((d.get("latitude"), d.get("longitude"), d.get("cityName") or "")
                if d.get("latitude") is not None and d.get("longitude") is not None else None)),
    ("ipinfo.io", "https://ipinfo.io/{ip}/json",
     lambda d: ((float(d["loc"].split(",")[0]), float(d["loc"].split(",")[1]), d.get("city") or "")
                if d.get("loc") else None)),
    ("ipwhois.app", "https://ipwhois.app/json/{ip}",
     lambda d: ((d.get("latitude"), d.get("longitude"), d.get("city") or "")
                if d.get("latitude") is not None and d.get("longitude") is not None else None)),
]


async def get_coords_by_ip(ip: str) -> Optional[tuple[float, float, str]]:
    timeout = aiohttp.ClientTimeout(total=6, connect=3)
    for name, url_tpl, parser in IP_SERVICES:
        url = url_tpl.format(ip=ip)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        logger.warning("%s вернул %s для IP %s: %s",
                                       name, resp.status, ip, body[:200])
                        continue
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        logger.warning("%s вернул не-JSON для IP %s: %s",
                                       name, ip, body[:200])
                        continue
                    result = parser(data)
                    if result is None:
                        logger.warning("%s не дал координат для IP %s: %s",
                                       name, ip, str(data)[:300])
                        continue
                    lat, lon, city = result
                    lat, lon = float(lat), float(lon)
                    logger.info("%s → %s (%s, %s)", name, city, lat, lon)
                    return lat, lon, city
        except aiohttp.ClientConnectorCertificateError as e:
            logger.warning("%s: SSL-ошибка: %s", name, e)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("%s недоступен: %s: %s", name, type(e).__name__, e)

    logger.error("Все %d сервисов геолокации не сработали для IP %s",
                 len(IP_SERVICES), ip)
    return None


async def fetch_weather(lat: float, lon: float) -> Optional[dict]:
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        logger.error("Некорректные координаты: lat=%r, lon=%r", lat, lon)
        return None

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "weather_code,wind_speed_10m"
        ),
        "hourly": "temperature_2m",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": 1,
    }

    for attempt, use_ssl in enumerate((True, False), 1):
        try:
            connector = None if use_ssl else aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=15, connect=5)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.get(FORECAST_URL, params=params) as resp:
                    body = await resp.text()
                    logger.info(
                        "Open-Meteo (attempt %d, ssl=%s): status=%s",
                        attempt, use_ssl, resp.status,
                    )
                    if resp.status != 200:
                        logger.warning("Open-Meteo вернул %s: %s", resp.status, body[:500])
                        continue
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError as e:
                        logger.error("Open-Meteo вернул невалидный JSON: %s", e)
                        continue
                    if not data.get("current"):
                        logger.warning("В ответе Open-Meteo нет current")
                        continue
                    return data
        except aiohttp.ClientConnectorCertificateError as e:
            logger.warning("SSL-ошибка: %s", e)
            continue
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Запрос погоды (attempt %d) не удался: %s: %s",
                           attempt, type(e).__name__, e)
            continue

    logger.error("Open-Meteo недоступен после 2 попыток")
    return None


# ─── ФОРМАТИРОВАНИЕ ПОГОДЫ ─────────────────────────────────────

def _round_temp(t: float) -> int:
    return int(t + 0.5) if t >= 0 else int(t - 0.5)


def _fmt_temp(t: Optional[float]) -> str:
    if t is None:
        return "—"
    r = _round_temp(t)
    if r > 0:
        return f"+{r}°"
    if r < 0:
        return f"−{abs(r)}°"
    return "0°"


def _fmt_wind(kmh: Optional[float]) -> str:
    if kmh is None:
        return "—"
    ms = kmh / 3.6
    return f"{ms:.1f}".replace(".", ",") + " м/с"


def _extract_day_evening(data: dict) -> tuple[Optional[float], Optional[float]]:
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    if not times or not temps:
        return None, None

    day_temp = None
    eve_temp = None
    for i, t in enumerate(times):
        if i >= len(temps):
            break
        try:
            hour = int(t.split("T")[1].split(":")[0])
        except (IndexError, ValueError):
            continue
        if hour == 12 and day_temp is None:
            day_temp = temps[i]
        elif hour == 20 and eve_temp is None:
            eve_temp = temps[i]
        if day_temp is not None and eve_temp is not None:
            break
    return day_temp, eve_temp


def format_weather(data: dict) -> str:
    current = data.get("current") or {}
    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    wind_kmh = current.get("wind_speed_10m")
    code = current.get("weather_code")
    desc = WMO_DESCRIPTIONS.get(code, "🌡 Погода")

    lines = []

    # Строка 1: температура + ощущается как (если разница ≥ 1°)
    temp_line = f"🌡 {_fmt_temp(temp)}"
    if feels is not None and temp is not None and abs(feels - temp) >= 1:
        temp_line += f" (ощущается как {_fmt_temp(feels)})"
    lines.append(temp_line)

    # Строка 2: описание
    lines.append(desc)

    # Строка 3: ветер
    if wind_kmh is not None:
        lines.append(f"💨 {_fmt_wind(wind_kmh)} ветер")

    # Строка 4: день/вечер
    day_temp, eve_temp = _extract_day_evening(data)
    parts = []
    if day_temp is not None:
        parts.append(f"Днём {_fmt_temp(day_temp)}")
    if eve_temp is not None:
        parts.append(f"Вечером {_fmt_temp(eve_temp)}")
    if parts:
        lines.append("📅 " + " · ".join(parts))

    return "\n".join(lines)


async def show_location_button(message: types.Message) -> None:
    logger.info("show_location_button → user=%s", message.from_user.id)
    try:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(
                    text="📍 Определить местоположение",
                    web_app=WebAppInfo(url=WEB_APP_URL),
                )
            ]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await message.answer(
            "🏙 <b>Нажмите кнопку ниже, чтобы определить город.</b>\n\n"
            "Это одноразовое действие. Город не будет виден никому.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info("Кнопка web_app отправлена пользователю %s", message.from_user.id)
    except TelegramBadRequest as e:
        logger.error("Не удалось отправить кнопку: %s", e)
        try:
            await message.answer(
                f"❌ <b>Ошибка при создании кнопки:</b>\n"
                f"<code>{escape(str(e))}</code>",
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass


# ─── ПАРСИНГ И ГЕЙТ ────────────────────────────────────────────

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


async def gate(message: types.Message) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        await reply_hint(message, "❌ <b>Команда работает только в группах.</b>")
        return False
    if not await is_admin(message.chat.id, message.from_user.id):
        await reply_hint(message, "⛔ <b>Недостаточно прав.</b>")
        return False
    return True


# ─── БАН ───────────────────────────────────────────────────────

rules = RulesFeature(bot, dp, DB_PATH, BOT_TOKEN)


@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if not await gate(message):
        return
    has_reply = bool(message.reply_to_message)
    target, until_ts, reason = parse_args(command.args, has_reply, True)
    if not has_reply and not target:
        await reply_hint(message, "📋 <b>Использование</b>\n<code>/ban [@ник | ID | reply] [время] причина</code>")
        return
    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_hint(message, "❌ <b>Не удалось найти пользователя.</b>\nОтветьте на его сообщение или укажите числовой ID.")
        return
    is_basic = message.chat.type == "group"
    try:
        await bot.ban_chat_member(message.chat.id, user_id, until_date=until_ts)
        if is_basic:
            await reply_public(message, "👢 <b>Пользователь исключён</b>\n\n"
                f"👤 {user_link(user_id, name)}\n{format_duration_line(until_ts)}\n"
                f"📝 Причина: {escape(reason)}")
            return
        save_ban(message.chat.id, user_id, until_ts, reason)
        schedule_lift(message.chat.id, user_id, "ban", until_ts)
        await reply_public(message, "🔨 <b>Пользователь забанен</b>\n\n"
            f"👤 {user_link(user_id, name)}\n{format_duration_line(until_ts)}\n"
            f"📝 Причина: {escape(reason)}")
    except TelegramBadRequest as e:
        await reply_hint(message, f"❌ <b>Не удалось забанить</b>\n<code>{escape(str(e))}</code>")


@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    if not await gate(message):
        return
    if message.chat.type == "group":
        await reply_hint(message, "ℹ️ <b>Разбан недоступен в базовой группе.</b>")
        return
    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, False)
    if not has_reply and not target:
        await reply_hint(message, "📋 <b>Использование</b>\n<code>/unban [@ник | ID | reply] причина</code>")
        return
    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_hint(message, "❌ <b>Не удалось найти пользователя.</b>")
        return
    try:
        await bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
        remove_ban(message.chat.id, user_id)
        scheduled_tasks.pop((message.chat.id, user_id, "ban"), None)
        await reply_public(message, "✅ <b>Пользователь разбанен</b>\n\n"
            f"👤 {user_link(user_id, name)}\n📝 Причина: {escape(reason)}")
    except TelegramBadRequest as e:
        await reply_hint(message, f"❌ <b>Не удалось разбанить</b>\n<code>{escape(str(e))}</code>")


# ─── МУТ ──────────────────────────────────────────────────────

@dp.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    if not await gate(message):
        return
    has_reply = bool(message.reply_to_message)
    target, until_ts, reason = parse_args(command.args, has_reply, True)
    if not has_reply and not target:
        await reply_hint(message, "📋 <b>Использование</b>\n<code>/mute [@ник | ID | reply] [время] причина</code>")
        return
    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_hint(message, "❌ <b>Не удалось найти пользователя.</b>")
        return
    try:
        await bot.restrict_chat_member(message.chat.id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_ts)
        save_mute(message.chat.id, user_id, until_ts, reason)
        schedule_lift(message.chat.id, user_id, "mute", until_ts)
        await reply_public(message, "🔇 <b>Пользователь замучен</b>\n\n"
            f"👤 {user_link(user_id, name)}\n{format_duration_line(until_ts)}\n"
            f"📝 Причина: {escape(reason)}")
    except TelegramBadRequest as e:
        await reply_hint(message, f"❌ <b>Не удалось замутить</b>\n<code>{escape(str(e))}</code>")


@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message, command: CommandObject):
    if not await gate(message):
        return
    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, False)
    if not has_reply and not target:
        await reply_hint(message, "📋 <b>Использование</b>\n<code>/unmute [@ник | ID | reply] причина</code>")
        return
    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_hint(message, "❌ <b>Не удалось найти пользователя.</b>")
        return
    try:
        await bot.restrict_chat_member(message.chat.id, user_id,
            permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                                        can_send_other_messages=True, can_add_web_page_previews=True))
        remove_mute(message.chat.id, user_id)
        scheduled_tasks.pop((message.chat.id, user_id, "mute"), None)
        await reply_public(message, "🔊 <b>Мут снят</b>\n\n"
            f"👤 {user_link(user_id, name)}\n📝 Причина: {escape(reason)}")
    except TelegramBadRequest as e:
        await reply_hint(message, f"❌ <b>Не удалось снять мут</b>\n<code>{escape(str(e))}</code>")


# ─── ВАРНЫ ────────────────────────────────────────────────────

@dp.message(Command("warn"))
async def cmd_warn(message: types.Message, command: CommandObject):
    if not await gate(message):
        return
    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, False)
    if not has_reply and not target:
        await reply_hint(message, "📋 <b>Использование</b>\n<code>/warn [@ник | ID | reply] причина</code>")
        return
    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_hint(message, "❌ <b>Не удалось найти пользователя.</b>")
        return
    count = add_warn(message.chat.id, user_id, reason)
    progress = "🟩" * count + "⬜" * max(0, WARN_LIMIT - count)
    await reply_public(message, "⚠️ <b>Выдано предупреждение</b>\n\n"
        f"👤 {user_link(user_id, name)}\n📊 {progress}  <b>{count}/{WARN_LIMIT}</b>\n"
        f"📝 Причина: {escape(reason)}")
    if count >= WARN_LIMIT:
        until_ts = int(time.time()) + AUTO_BAN_DAYS * 86400
        try:
            await bot.ban_chat_member(message.chat.id, user_id, until_date=until_ts)
            save_ban(message.chat.id, user_id, until_ts, f"Автобан: {WARN_LIMIT} варнов")
            reset_warns(message.chat.id, user_id)
            schedule_lift(message.chat.id, user_id, "ban", until_ts)
            await reply_public(message, "🚫 <b>Автоматический бан</b>\n\n"
                f"👤 {user_link(user_id, name)}\n{format_duration_line(until_ts)}\n"
                f"📝 Причина: достигнут лимит предупреждений ({WARN_LIMIT})")
        except TelegramBadRequest as e:
            await reply_hint(message, f"❌ <b>Автобан не удался</b>\n<code>{escape(str(e))}</code>")


@dp.message(Command("unwarn"))
async def cmd_unwarn(message: types.Message, command: CommandObject):
    if not await gate(message):
        return
    has_reply = bool(message.reply_to_message)
    target, _, reason = parse_args(command.args, has_reply, False)
    if not has_reply and not target:
        await reply_hint(message, "📋 <b>Использование</b>\n<code>/unwarn [@ник | ID | reply] причина</code>")
        return
    user_id, name = await resolve_target(message, target)
    if not user_id:
        await reply_hint(message, "❌ <b>Не удалось найти пользователя.</b>")
        return
    reset_warns(message.chat.id, user_id)
    await reply_public(message, "✅ <b>Все предупреждения сняты</b>\n\n"
        f"👤 {user_link(user_id, name)}\n📝 Причина: {escape(reason)}")


# ─── СПИСКИ ────────────────────────────────────────────────────

def _render_list(title: str, rows: list[tuple], empty: str) -> str:
    if not rows:
        return empty
    lines = [title, ""]
    for user_id, until_ts, reason in rows:
        lines.append(f"• {user_link(user_id)} — <b>{humanize_until(until_ts)}</b>\n   <i>{escape(reason)}</i>")
    return "\n".join(lines)


@dp.message(Command("banlist"))
async def cmd_banlist(message: types.Message):
    if not await gate(message):
        return
    if message.chat.type == "group":
        await reply_hint(message, "ℹ️ <b>Список забаненных недоступен в базовой группе.</b>")
        return
    rows = get_list("bans", message.chat.id)
    await reply_hint(message, _render_list("🔨 <b>Забаненные</b>", rows, "📭 <i>Список пуст</i>"))


@dp.message(Command("mutelist"))
async def cmd_mutelist(message: types.Message):
    if not await gate(message):
        return
    rows = get_list("mutes", message.chat.id)
    await reply_hint(message, _render_list("🔇 <b>Замученные</b>", rows, "📭 <i>Список пуст</i>"))


@dp.message(Command("warnlist"))
async def cmd_warnlist(message: types.Message):
    if not await gate(message):
        return
    with _conn() as c:
        rows = c.execute("SELECT user_id, COUNT(*) FROM warns WHERE chat_id=? GROUP BY user_id",
                         (message.chat.id,)).fetchall()
    if not rows:
        await reply_hint(message, "📭 <i>Список предупреждений пуст</i>")
        return
    lines = ["⚠️ <b>Предупреждения</b>", ""]
    for user_id, count in rows:
        progress = "🟩" * count + "⬜" * max(0, WARN_LIMIT - count)
        lines.append(f"• {user_link(user_id)} — {progress} <b>{count}/{WARN_LIMIT}</b>")
    await reply_hint(message, "\n".join(lines))


# ─── ГЕОЛОКАЦИЯ (GPS) ─────────────────────────────────────────

@dp.message(F.location)
async def handle_location(message: types.Message):
    loc = message.location
    label = f"{loc.latitude:.4f},{loc.longitude:.4f}"
    set_user_location(message.from_user.id, loc.latitude, loc.longitude, label)
    await message.reply(
        "✅ <b>Локация сохранена.</b>\n\nТеперь пишите <b>погода</b> в любом чате — бот покажет данные.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


# ─── ДАННЫЕ ОТ MINI APP ───────────────────────────────────────

@dp.message(F.web_app_data)
async def handle_web_app_data(message: types.Message):
    if not message.from_user:
        logger.error("web_app_data без from_user!")
        return

    user_id = message.from_user.id
    logger.info("WebApp data от user_id=%s: %s", user_id, message.web_app_data.data)

    try:
        payload = json.loads(message.web_app_data.data)
        ip = payload.get("web_app_ip")
        if not ip:
            raise ValueError("IP отсутствует")
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Некорректные данные от Mini App: %s", e)
        await message.reply(
            "❌ <b>Не удалось получить данные из приложения.</b>\nПопробуйте ещё раз.",
            parse_mode="HTML",
        )
        return

    coords = await get_coords_by_ip(ip)
    if not coords:
        await message.reply(
            "❌ <b>Не удалось определить местоположение.</b>\n\n"
            "Возможно, вы используете VPN или прокси. Отключите VPN и попробуйте снова.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    lat, lon, city = coords
    set_user_location(user_id, lat, lon, city)
    logger.info("Сохранена локация для user_id=%s: %s (%s, %s)", user_id, city, lat, lon)

    await message.reply(
        "✅ <b>Местоположение сохранено.</b>\n\n"
        "Теперь напишите в чат слово <b>погода</b> — бот покажет данные.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


# ─── ДИАГНОСТИКА ЛОКАЦИИ ─────────────────────────────────────

@dp.message(Command("whereami"))
async def cmd_whereami(message: types.Message):
    if message.chat.type != "private":
        return
    user_id = message.from_user.id
    loc = get_user_location(user_id)
    if loc:
        lat, lon, city = loc
        await message.reply(
            f"📍 <b>Сохранённая локация</b>\n\n"
            f"Город: <b>{escape(city or '—')}</b>\n"
            f"Координаты: <code>{lat}</code>, <code>{lon}</code>\n"
            f"User ID: <code>{user_id}</code>\n"
            f"БД: <code>{escape(DB_PATH)}</code>",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"❌ <b>Локация не найдена</b>\n\n"
            f"User ID: <code>{user_id}</code>\n"
            f"БД: <code>{escape(DB_PATH)}</code>",
            parse_mode="HTML",
        )


@dp.message(Command("forget"))
async def cmd_forget(message: types.Message):
    if message.chat.type != "private":
        return
    delete_user_location(message.from_user.id)
    await message.reply(
        "🗑 <b>Локация удалена.</b>\n\nЧтобы задать новую — нажмите /start.",
        parse_mode="HTML",
    )


# ─── ПОГОДА ПО СЛОВУ ──────────────────────────────────────────

@dp.message(F.text.regexp(r"(?i)\bпогода\b"))
async def handle_weather_word(message: types.Message):
    if message.text.startswith("/"):
        return
    user_id = message.from_user.id
    loc = get_user_location(user_id)

    if loc:
        lat, lon, _ = loc
        data = await fetch_weather(lat, lon)
        if not data or not data.get("current"):
            await reply_hint(message, "❌ <b>Сервис погоды временно недоступен.</b>")
            return
        text = format_weather(data)
        if message.chat.type == "private":
            await message.reply(text, parse_mode="HTML")
        else:
            await reply_public(message, text)
        return

    if message.chat.type == "private":
        await show_location_button(message)
        return

    deep_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me/"
    await reply_hint(
        message,
        "🏙 <b>Чтобы узнать погоду, определите город.</b>\n\n"
        f"Откройте <a href=\"{deep_link}\">личный чат с ботом</a> — "
        "там появится кнопка для определения местоположения.\n\n"
        "Это одноразовое действие. Город не будет виден в чате.",
    )


# ─── СТАРТ ────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject):
    if await rules.handle_start(message, command.args):
        return

    logger.info(
        "📥 /start от user=%s chat=%s type=%s payload=%r",
        message.from_user.id, message.chat.id, message.chat.type, command.args,
    )

    if message.chat.type in ("group", "supergroup"):
        if await is_admin(message.chat.id, message.from_user.id):
            await reply_hint(message, HELP_TEXT, ttl=60)
        else:
            await reply_hint(
                message,
                "👋 <b>Привет! Я SpaGuard.</b>\n\n"
                "Чтобы узнать погоду, откройте личный чат со мной.",
            )
        return

    # В ЛС: если локация уже есть — не показываем кнопку, а сообщаем
    loc = get_user_location(message.from_user.id)
    if loc:
        lat, lon, city = loc
        await message.reply(
            f"✅ <b>Локация уже сохранена</b> ({escape(city or '—')})\n\n"
            f"Напишите <b>погода</b> в любом чате.\n"
            f"Хотите сменить — /forget, затем /start.",
            parse_mode="HTML",
        )
        return

    await show_location_button(message)


HELP_TEXT = (
    "📖 <b>Правила:</b> /rules — читать; /rules edit — редактор для администраторов.\n\n"
    "🛡 <b>SpaGuard</b> — модерация чата\n\n"
    "🔨 <b>Баны</b>\n<code>/ban [@ник | ID | reply] [время] причина</code>\n"
    "<code>/unban [@ник | ID | reply] причина</code>\n\n"
    "🔇 <b>Муты</b>\n<code>/mute [@ник | ID | reply] [время] причина</code>\n"
    "<code>/unmute [@ник | ID | reply] причина</code>\n\n"
    "⚠️ <b>Предупреждения</b>\n<code>/warn [@ник | ID | reply] причина</code>\n"
    "<code>/unwarn [@ник | ID | reply] причина</code>\n\n"
    "📋 <b>Списки</b>\n<code>/banlist</code> · <code>/mutelist</code> · <code>/warnlist</code>\n\n"
    "⏱ <b>Время:</b> <code>30s</code>, <code>10m</code>, <code>2h</code>, <code>7d</code>\n"
    f"ℹ️ {WARN_LIMIT} предупреждения → автобан на {AUTO_BAN_DAYS} дн.\n\n"
    "🌤 <b>Погода:</b> напишите слово <b>погода</b> в чат.\n"
    "📍 <b>Локация:</b> /start в ЛС бота, /whereami — проверить, /forget — сбросить."
)


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        if not await is_admin(message.chat.id, message.from_user.id):
            await reply_hint(message, "⛔ <b>Недостаточно прав.</b>")
            return
        await reply_hint(message, HELP_TEXT, ttl=60)
        return
    await message.reply(HELP_TEXT, parse_mode="HTML")


# ─── ЗАПУСК ────────────────────────────────────────────────────

async def main():
    global BOT_USERNAME
    logger.info("SpaGuard запускается…")
    logger.info("DB_PATH=%s", DB_PATH)
    logger.info("БД существует: %s", os.path.exists(DB_PATH))

    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info("Бот: @%s", BOT_USERNAME)
    logger.info("WEB_APP_URL=%s", WEB_APP_URL)
    logger.info("Сервисы геолокации: %s",
                ", ".join(name for name, _, _ in IP_SERVICES))

    await rules.start()
    try:
        await restore_schedules()
        await dp.start_polling(bot)
    finally:
        await rules.stop()


if __name__ == "__main__":
    asyncio.run(main())
