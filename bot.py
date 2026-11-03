"""Telegram-бот: принимает платёжки Bangkok Bank, раскладывает траты по категориям."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
from datetime import date

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonDefault, MenuButtonWebApp,
    Message, WebAppInfo,
)
from dotenv import load_dotenv

from db import DB
from slip_parser import Slip, SlipParseError, parse_slip
from webapp import create_app, start_server

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
# На Railway файлы контейнера стираются при каждом деплое, база должна лежать на Volume.
# Railway сам задаёт RAILWAY_VOLUME_MOUNT_PATH, когда Volume подключён к сервису.
VOLUME = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = os.getenv("DB_PATH") or (os.path.join(VOLUME, "payments.db") if VOLUME else "payments.db")
if os.getenv("RAILWAY_ENVIRONMENT") and not VOLUME and not os.getenv("DB_PATH"):
    raise SystemExit(
        "На Railway нет Volume: база стёрлась бы при следующем деплое. "
        "Подключите Volume к сервису (Mount path: /data) и сделайте Redeploy."
    )
# Через запятую. Пусто — бот отвечает всем (не рекомендуется: это финансовые данные).
ALLOWED_USERS = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
# Публичный HTTPS-адрес Mini App. На Railway берётся из его домена сам,
# локально — адрес туннеля ngrok на WEBAPP_PORT. Пусто — Mini App выключен.
_railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
WEBAPP_URL = (os.getenv("WEBAPP_URL") or (f"https://{_railway_domain}" if _railway_domain else "")).strip().rstrip("/")
# Railway передаёт порт в PORT и ходит в контейнер снаружи, поэтому там слушаем 0.0.0.0.
WEBAPP_PORT = int(os.getenv("PORT") or os.getenv("WEBAPP_PORT", "8080"))
WEBAPP_HOST = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"

db = DB(DB_PATH)
router = Router()

MONTHS_RU = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


class AddSlip(StatesGroup):
    choosing_category = State()
    naming_category = State()


# ---------- утилиты ----------

def money(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def month_title(month: str) -> str:
    y, m = month.split("-")
    return f"{MONTHS_RU[int(m)]} {y}"


def shift_month(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    m += delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def slip_text(s: Slip) -> str:
    e = html.escape
    lines = [
        f"📅 {s.paid_at:%d.%m.%Y %H:%M}",
        f"💰 <b>{money(s.amount)} THB</b>" + (f" (комиссия {money(s.fee)})" if s.fee else ""),
        f"➡️ {e(s.recipient_name)} · {e(s.recipient_account)} · {e(s.recipient_bank)}",
    ]
    if s.tx_ref:
        lines.append(f"🔖 <code>{e(s.tx_ref)}</code>")
    return "\n".join(lines)


def categories_kb(chat_id: int) -> InlineKeyboardMarkup:
    cats = db.categories(chat_id)
    rows, row = [], []
    for c in cats:
        row.append(InlineKeyboardButton(text=c["name"], callback_data=f"cat:{c['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="➕ Новая категория", callback_data="newcat"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def month_report(chat_id: int, month: str) -> tuple[str, InlineKeyboardMarkup]:
    e = html.escape
    totals = db.month_totals(chat_id, month)
    nav = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️", callback_data=f"m:{shift_month(month, -1)}"),
        InlineKeyboardButton(text="▶️", callback_data=f"m:{shift_month(month, 1)}"),
    ]])
    if not totals:
        return f"<b>{month_title(month)}</b>\n\nПлатежей нет.", nav

    out = [f"<b>{month_title(month)}</b>", ""]
    for t in totals:
        out.append(f"• {e(t['category'])}: <b>{money(t['total'])}</b> ({t['cnt']})")
    out.append(f"\n<b>Итого: {money(sum(t['total'] for t in totals))} THB</b>\n")

    current_day = None
    for p in db.month_payments(chat_id, month):
        day = p["paid_at"][:10]
        if day != current_day:
            current_day = day
            out.append(f"\n<u>{day[8:10]}.{day[5:7]}</u>")
        out.append(
            f"{p['paid_at'][11:16]} · {money(p['amount'])} · {e(p['category'])}"
            f" · {e(p['recipient_name'] or '')} <i>#{p['id']}</i>"
        )
    text = "\n".join(out)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    return text, nav


# ---------- доступ ----------

@router.message.outer_middleware()
async def _guard_messages(handler, event: Message, data):
    if ALLOWED_USERS and event.from_user and event.from_user.id not in ALLOWED_USERS:
        return
    return await handler(event, data)


@router.callback_query.outer_middleware()
async def _guard_callbacks(handler, event: CallbackQuery, data):
    if ALLOWED_USERS and event.from_user.id not in ALLOWED_USERS:
        await event.answer("Нет доступа")
        return
    return await handler(event, data)


# ---------- команды ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message):
    await message.answer(
        "Пришлите скриншот платёжки Bangkok Bank — я распознаю его и спрошу категорию.\n"
        "Совет: отправляйте <b>файлом</b> (без сжатия) — так распознавание надёжнее.\n\n"
        "/app — открыть приложение (загрузка пачкой, просмотр, правка)\n"
        "/month — траты за текущий месяц (или <code>/month 2026-10</code>)\n"
        "/months — итоги по месяцам\n"
        "/categories — список категорий\n"
        "/addcat Название — добавить категорию\n"
        "/delete N — удалить платёж #N\n"
        f"\nВаш Telegram ID: <code>{message.from_user.id}</code>"
    )


@router.message(Command("app"))
async def cmd_app(message: Message):
    if not WEBAPP_URL:
        await message.answer("Приложение не настроено: задайте WEBAPP_URL в .env.")
        return
    await message.answer("Открыть приложение:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Траты", web_app=WebAppInfo(url=WEBAPP_URL)),
    ]]))


@router.message(Command("month"))
async def cmd_month(message: Message, command: CommandObject):
    month = (command.args or "").strip() or date.today().strftime("%Y-%m")
    try:
        shift_month(month, 0)
        assert len(month) == 7
    except Exception:
        await message.answer("Формат: <code>/month 2026-11</code>")
        return
    text, kb = month_report(message.chat.id, month)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("m:"))
async def cb_month(cb: CallbackQuery):
    text, kb = month_report(cb.message.chat.id, cb.data[2:])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.message(Command("months"))
async def cmd_months(message: Message):
    rows = db.months_summary(message.chat.id)
    if not rows:
        await message.answer("Платежей пока нет.")
        return
    out = ["<b>По месяцам</b>", ""]
    for r in rows:
        out.append(f"{month_title(r['month'])}: <b>{money(r['total'])}</b> ({r['cnt']})")
    await message.answer("\n".join(out))


@router.message(Command("categories"))
async def cmd_categories(message: Message):
    names = [html.escape(c["name"]) for c in db.categories(message.chat.id)]
    await message.answer("Категории:\n" + "\n".join(f"• {n}" for n in names))


@router.message(Command("addcat"))
async def cmd_addcat(message: Message, command: CommandObject):
    name = (command.args or "").strip()
    if not name:
        await message.answer("Формат: <code>/addcat Интернет</code>")
        return
    db.add_category(message.chat.id, name[:40])
    await message.answer(f"Категория «{html.escape(name[:40])}» есть.")


@router.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject):
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer("Формат: <code>/delete 12</code> (номер видно в /month)")
        return
    ok = db.delete_payment(message.chat.id, int(arg))
    await message.answer("Удалено." if ok else "Платёж не найден.")


# ---------- приём платёжки ----------

@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_slip(message: Message, state: FSMContext, bot: Bot):
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)

    status = await message.answer("🔍 Распознаю…")
    try:
        slip = await asyncio.to_thread(parse_slip, buf.getvalue())
    except SlipParseError as ex:
        await status.edit_text(f"Не получилось: {ex}\nПопробуйте отправить файлом, без сжатия.")
        return
    except Exception:
        logging.exception("OCR failed")
        await status.edit_text("Ошибка распознавания. Попробуйте ещё раз.")
        return
    # картинка дальше не нужна и нигде не сохраняется
    del buf

    dup = db.find_by_tx(message.chat.id, slip.tx_ref)
    if dup:
        await status.edit_text(
            f"Эта платёжка уже добавлена: #{dup['id']}, {html.escape(dup['category'])}, "
            f"{dup['paid_at'][:10]}, {money(dup['amount'])} THB."
        )
        return

    await state.set_state(AddSlip.choosing_category)
    await state.update_data(slip=slip.to_dict())
    await status.edit_text(
        slip_text(slip) + "\n\nВыберите категорию:", reply_markup=categories_kb(message.chat.id)
    )


async def _save(message: Message, state: FSMContext, category_id: int) -> str:
    data = await state.get_data()
    await state.clear()
    if "slip" not in data:
        return "Платёжка устарела, пришлите её заново."
    slip = Slip.from_dict(data["slip"])
    if db.find_by_tx(message.chat.id, slip.tx_ref):
        return "Эта платёжка уже добавлена."
    pid = db.add_payment(message.chat.id, category_id, slip)
    cat = db.category_name(message.chat.id, category_id)
    return f"{slip_text(slip)}\n\n✅ Сохранено как #{pid} → <b>{html.escape(cat)}</b>"


@router.callback_query(AddSlip.choosing_category, F.data.startswith("cat:"))
async def cb_category(cb: CallbackQuery, state: FSMContext):
    category_id = int(cb.data[4:])
    if not db.category_name(cb.message.chat.id, category_id):
        await cb.answer("Категория не найдена")
        return
    await cb.message.edit_text(await _save(cb.message, state, category_id))
    await cb.answer()


@router.callback_query(AddSlip.choosing_category, F.data == "newcat")
async def cb_newcat(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddSlip.naming_category)
    await state.update_data(prompt_msg_id=cb.message.message_id)
    await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel"),
    ]]))
    await cb.message.answer("Напишите название новой категории:")
    await cb.answer()


@router.message(AddSlip.naming_category, F.text & ~F.text.startswith("/"))
async def on_new_category_name(message: Message, state: FSMContext, bot: Bot):
    name = message.text.strip()[:40]
    category_id = db.add_category(message.chat.id, name)
    data = await state.get_data()
    text = await _save(message, state, category_id)
    if data.get("prompt_msg_id"):
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id, message_id=data["prompt_msg_id"], reply_markup=None
        )
    await message.answer(text)


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено, ничего не сохранено.")
    await cb.answer()


@router.callback_query(F.data.startswith("cat:") | (F.data == "newcat"))
async def cb_stale(cb: CallbackQuery):
    await cb.answer("Эта платёжка уже обработана или устарела. Пришлите её заново.", show_alert=True)


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    await asyncio.to_thread(__import__("slip_parser")._get_ocr)  # прогрев модели OCR
    # Если у бота остался webhook (например, после serverless-режима), polling не получит сообщений.
    await bot.delete_webhook(drop_pending_updates=False)

    runner = await start_server(create_app(db, BOT_TOKEN, ALLOWED_USERS), WEBAPP_HOST, WEBAPP_PORT)
    logging.info("Mini App server on http://%s:%s, public url: %s, db: %s",
                 WEBAPP_HOST, WEBAPP_PORT, WEBAPP_URL or "-", DB_PATH)
    # Кнопка слева от поля ввода во всех личных чатах с ботом
    await bot.set_chat_menu_button(menu_button=(
        MenuButtonWebApp(text="Траты", web_app=WebAppInfo(url=WEBAPP_URL)) if WEBAPP_URL
        else MenuButtonDefault()
    ))
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
