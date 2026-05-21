"""
Чат-бот «Прогулки по Москве» для мессенджера MAX
Версия: 1.0
Стек: Python 3.12 + maxapi (асинхронный)
"""

import asyncio
import json
import logging
import os
import random
from typing import Dict, List, Set

from maxapi import Bot, Dispatcher
from maxapi.types import (
    BotStarted,
    Command,
    MessageCreated,
    MessageCallback,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# --- НАСТРОЙКИ ----------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("MAX_BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_ДЛЯ_ЛОКАЛЬНОГО_ЗАПУСКА")

bot = Bot(TOKEN)
dp = Dispatcher()

# --- ЗАГРУЗКА БАЗЫ МАРШРУТОВ --------------------------------------------------

with open("routes_database.json", "r", encoding="utf-8") as f:
    DB = json.load(f)
    ROUTES = DB["routes"]
    ROUTES_BY_ID = {r["id"]: r for r in ROUTES}

# --- ХРАНИЛИЩЕ СОСТОЯНИЙ (в памяти, для MVP достаточно) ----------------------

FAVORITES: Dict[int, Set[str]] = {}            # user_id -> set of route_ids
NAV_STATE: Dict[int, Dict] = {}                # user_id -> {"route_id": ..., "step": ...}
FILTER_STATE: Dict[int, Dict] = {}             # user_id -> {"time": ..., "company": ..., "theme": ...}


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ --------------------------------------------------

def difficulty_emoji(level: str) -> str:
    return {"лёгкая": "🟢", "средняя": "🟡", "сложная": "🔴"}.get(level, "⚪")


def format_time_range(time_min: List[int]) -> str:
    """[120, 160] -> '2–2,7 ч'"""
    lo, hi = time_min[0] / 60, time_min[1] / 60
    return f"{lo:.1f}–{hi:.1f} ч".replace(".0", "").replace(".", ",")


def route_card(route: dict) -> str:
    """Форматирует карточку маршрута"""
    return (
        f"🚶 *{route['title']}*\n\n"
        f"📏 {route['distance_km']} км  •  ⏱ {format_time_range(route['time_min'])}  •  👣 ~{route['steps']} шагов\n"
        f"{difficulty_emoji(route['difficulty'])} Сложность: {route['difficulty']}\n"
        f"🎯 Стиль: {route['style']}\n"
        f"🌤 Сезон: {route['best_season']}\n\n"
        f"*Старт:* {route['start']}\n"
        f"*Финиш:* {route['finish']}"
    )


def step_card(route: dict, step_idx: int) -> str:
    """Форматирует одну точку пошаговой навигации"""
    point = route["points"][step_idx]
    total = len(route["points"])
    return (
        f"{point['icon']} *{point['n']}/{total}. {point['title']}*\n\n"
        f"{point['desc']}\n\n"
        f"🕒 Время на точке: ~{point['time_min']} мин"
    )


# --- КЛАВИАТУРЫ ---------------------------------------------------------------

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(buttons=[
        [InlineKeyboardButton(text="🧭 Подобрать маршрут", payload="pick_start")],
        [InlineKeyboardButton(text="🎲 Случайный", payload="random"),
         InlineKeyboardButton(text="📚 Все маршруты", payload="all")],
        [InlineKeyboardButton(text="⭐ Избранное", payload="favorites"),
         InlineKeyboardButton(text="ℹ️ О боте", payload="about")],
    ])


def kb_pick_time() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(buttons=[
        [InlineKeyboardButton(text="~1 час", payload="time_short"),
         InlineKeyboardButton(text="~2 часа", payload="time_medium")],
        [InlineKeyboardButton(text="3 часа и больше", payload="time_long"),
         InlineKeyboardButton(text="Не важно", payload="time_any")],
        [InlineKeyboardButton(text="↩️ В меню", payload="menu")],
    ])


def kb_pick_company() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(buttons=[
        [InlineKeyboardButton(text="🧍 Один", payload="company_solo"),
         InlineKeyboardButton(text="💑 С парой", payload="company_couple")],
        [InlineKeyboardButton(text="👨‍👩‍👧 С семьёй", payload="company_family"),
         InlineKeyboardButton(text="👥 С компанией", payload="company_group")],
    ])


def kb_pick_theme() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(buttons=[
        [InlineKeyboardButton(text="🏰 iconic", payload="theme_iconic"),
         InlineKeyboardButton(text="🤫 Тихая Москва", payload="theme_quiet")],
        [InlineKeyboardButton(text="🏛 Архитектура", payload="theme_arch"),
         InlineKeyboardButton(text="🌳 Природа", payload="theme_nature")],
        [InlineKeyboardButton(text="📖 Литература", payload="theme_lit"),
         InlineKeyboardButton(text="🎨 Всё интересно", payload="theme_any")],
    ])


def kb_route(route_id: str, user_id: int) -> InlineKeyboardMarkup:
    fav_text = "💔 Убрать из избранного" if route_id in FAVORITES.get(user_id, set()) else "⭐ В избранное"
    return InlineKeyboardMarkup(buttons=[
        [InlineKeyboardButton(text="📍 Пошагово", payload=f"nav_start:{route_id}")],
        [InlineKeyboardButton(text=fav_text, payload=f"fav_toggle:{route_id}")],
        [InlineKeyboardButton(text="↩️ В меню", payload="menu")],
    ])


def kb_nav(route_id: str, step_idx: int, total: int) -> InlineKeyboardMarkup:
    buttons = []
    if step_idx < total - 1:
        buttons.append([InlineKeyboardButton(text="➡️ Дальше", payload=f"nav_next:{route_id}:{step_idx + 1}")])
    else:
        buttons.append([InlineKeyboardButton(text="🎉 Завершить", payload=f"nav_finish:{route_id}")])
    buttons.append([InlineKeyboardButton(text="❌ Прервать", payload="menu")])
    return InlineKeyboardMarkup(buttons=buttons)


# --- ЛОГИКА ФИЛЬТРАЦИИ --------------------------------------------------------

TIME_MAP = {
    "time_short": lambda r: r["time_min"][1] <= 90,
    "time_medium": lambda r: 90 < r["time_min"][1] <= 180,
    "time_long": lambda r: r["time_min"][1] > 180,
    "time_any": lambda r: True,
}

THEME_MAP = {
    "theme_iconic": "iconic",
    "theme_quiet": "мало туристов",
    "theme_arch": "архитектура",
    "theme_nature": "природа",
    "theme_lit": "литература",
    "theme_any": None,
}


def filter_routes(user_id: int) -> List[dict]:
    state = FILTER_STATE.get(user_id, {})
    results = ROUTES[:]
    if t := state.get("time"):
        results = [r for r in results if TIME_MAP[t](r)]
    if theme_key := state.get("theme"):
        tag = THEME_MAP.get(theme_key)
        if tag:
            results = [r for r in results if tag in r.get("tags", [])]
    # если ничего не нашли — ослабляем тему
    if not results and state.get("theme"):
        results = [r for r in ROUTES if TIME_MAP[state.get("time", "time_any")](r)]
    return results[:3]


# --- ОБРАБОТЧИКИ КОМАНД -------------------------------------------------------

@dp.message_created(Command("start"))
async def cmd_start(event: MessageCreated):
    await event.message.answer(
        "👋 Привет! Я помогу выбрать пешую прогулку по Москве.\n\n"
        f"В базе {len(ROUTES)} маршрутов — от 2,5 до 9 километров.\n"
        "Все размечены по времени, теме и сложности.\n\n"
        "С чего начнём?",
        keyboard=kb_main_menu(),
    )


@dp.message_created(Command("help"))
async def cmd_help(event: MessageCreated):
    await event.message.answer(
        "📖 *Как пользоваться ботом*\n\n"
        "/start — главное меню\n"
        "/pick — подобрать маршрут\n"
        "/random — случайный маршрут\n"
        "/all — все маршруты\n"
        "/favorites — избранное\n"
        "/help — эта справка",
        keyboard=kb_main_menu(),
    )


# --- ОБРАБОТКА КНОПОК ---------------------------------------------------------

@dp.message_callback()
async def on_callback(event: MessageCallback):
    payload = event.callback.payload
    user_id = event.callback.user.user_id

    # Главное меню
    if payload == "menu":
        await event.message.answer("Главное меню:", keyboard=kb_main_menu())
        return

    if payload == "about":
        await event.message.answer(
            "🤖 *О боте*\n\n"
            "«Прогулки по Москве» — подбираю пешие маршруты под ваше время, "
            "погоду и настроение. База — 12 проверенных маршрутов.\n\n"
            "Обратная связь: /feedback",
            keyboard=kb_main_menu(),
        )
        return

    # Подбор маршрута: шаг 1 — время
    if payload == "pick_start":
        FILTER_STATE[user_id] = {}
        await event.message.answer("⏱ Сколько времени у вас на прогулку?", keyboard=kb_pick_time())
        return

    if payload.startswith("time_"):
        FILTER_STATE.setdefault(user_id, {})["time"] = payload
        await event.message.answer("👥 С кем идёте?", keyboard=kb_pick_company())
        return

    if payload.startswith("company_"):
        FILTER_STATE.setdefault(user_id, {})["company"] = payload
        await event.message.answer("🎨 Что вам ближе?", keyboard=kb_pick_theme())
        return

    if payload.startswith("theme_"):
        FILTER_STATE.setdefault(user_id, {})["theme"] = payload
        results = filter_routes(user_id)
        if not results:
            await event.message.answer(
                "🤔 Под точные критерии ничего не нашёл. Попробуйте /pick ещё раз.",
                keyboard=kb_main_menu(),
            )
            return
        text = f"Нашёл {len(results)} маршрут(ов) под ваш запрос 👇\n\n"
        for i, r in enumerate(results, 1):
            text += (
                f"*{i}. {r['title']}*\n"
                f"{r['distance_km']} км • {format_time_range(r['time_min'])} • {r['difficulty']}\n"
                f"_{r['style']}_\n\n"
            )
        kb = InlineKeyboardMarkup(buttons=[
            [InlineKeyboardButton(text=f"Открыть {i}", payload=f"open:{r['id']}")]
            for i, r in enumerate(results, 1)
        ] + [[InlineKeyboardButton(text="🔁 Другой запрос", payload="pick_start")]])
        await event.message.answer(text, keyboard=kb)
        return

    # Случайный маршрут
    if payload == "random":
        route = random.choice(ROUTES)
        await event.message.answer(
            f"🎲 Бросаю кубик...\n\nВыпало:\n\n{route_card(route)}",
            keyboard=kb_route(route["id"], user_id),
        )
        return

    # Все маршруты
    if payload == "all":
        text = "📚 *Каталог маршрутов*\n\n"
        for r in ROUTES:
            text += f"{difficulty_emoji(r['difficulty'])} *{r['title']}* — {r['distance_km']} км\n"
        kb = InlineKeyboardMarkup(buttons=[
            [InlineKeyboardButton(text=r["title"], payload=f"open:{r['id']}")]
            for r in ROUTES
        ] + [[InlineKeyboardButton(text="↩️ В меню", payload="menu")]])
        await event.message.answer(text, keyboard=kb)
        return

    # Избранное
    if payload == "favorites":
        favs = FAVORITES.get(user_id, set())
        if not favs:
            await event.message.answer(
                "⭐ Избранное пусто.\n\nДобавляйте маршруты кнопкой «В избранное» в карточке.",
                keyboard=kb_main_menu(),
            )
            return
        text = "⭐ *Ваше избранное*\n\n"
        kb_btns = []
        for rid in favs:
            r = ROUTES_BY_ID[rid]
            text += f"• {r['title']} — {r['distance_km']} км\n"
            kb_btns.append([InlineKeyboardButton(text=r["title"], payload=f"open:{rid}")])
        kb_btns.append([InlineKeyboardButton(text="↩️ В меню", payload="menu")])
        await event.message.answer(text, keyboard=InlineKeyboardMarkup(buttons=kb_btns))
        return

    # Открыть карточку маршрута
    if payload.startswith("open:"):
        rid = payload.split(":", 1)[1]
        route = ROUTES_BY_ID.get(rid)
        if route:
            await event.message.answer(route_card(route), keyboard=kb_route(rid, user_id))
        return

    # Избранное: добавить/убрать
    if payload.startswith("fav_toggle:"):
        rid = payload.split(":", 1)[1]
        FAVORITES.setdefault(user_id, set())
        if rid in FAVORITES[user_id]:
            FAVORITES[user_id].remove(rid)
            await event.message.answer("💔 Убрал из избранного.", keyboard=kb_route(rid, user_id))
        else:
            FAVORITES[user_id].add(rid)
            await event.message.answer("⭐ Добавил в избранное!", keyboard=kb_route(rid, user_id))
        return

    # Пошаговая навигация: старт
    if payload.startswith("nav_start:"):
        rid = payload.split(":", 1)[1]
        NAV_STATE[user_id] = {"route_id": rid, "step": 0}
        route = ROUTES_BY_ID[rid]
        await event.message.answer(
            step_card(route, 0),
            keyboard=kb_nav(rid, 0, len(route["points"])),
        )
        return

    # Пошаговая навигация: следующий шаг
    if payload.startswith("nav_next:"):
        _, rid, step = payload.split(":")
        step = int(step)
        route = ROUTES_BY_ID[rid]
        NAV_STATE[user_id] = {"route_id": rid, "step": step}
        await event.message.answer(
            step_card(route, step),
            keyboard=kb_nav(rid, step, len(route["points"])),
        )
        return

    # Пошаговая навигация: финиш
    if payload.startswith("nav_finish:"):
        rid = payload.split(":", 1)[1]
        NAV_STATE.pop(user_id, None)
        await event.message.answer(
            "🎉 Готово! Маршрут пройден.\n\nКак вам прогулка?",
            keyboard=kb_main_menu(),
        )
        return


@dp.message_created(BotStarted())
async def on_bot_started(event):
    await event.message.answer(
        "👋 Я бот для пеших прогулок по Москве. Напишите /start, чтобы начать."
    )


# --- ЗАПУСК -------------------------------------------------------------------

async def main():
    logging.info(f"Бот запущен. Маршрутов в базе: {len(ROUTES)}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
