"""HTTP-сервер Mini App: отдаёт страницу webapp/index.html и JSON API к той же базе.

Каждый запрос к /api несёт заголовок `Authorization: tma <initData>`.
initData подписан Telegram ключом, выведенным из токена бота, — подделать его
без токена нельзя. Из него берём id пользователя; он же chat_id личного чата с ботом,
поэтому в приложении видны те же траты, что и в /month.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl

from aiohttp import web

from db import DB
from slip_parser import Slip, SlipParseError, parse_slip

INDEX = Path(__file__).parent / "webapp" / "index.html"
INIT_DATA_TTL = 24 * 3600


def check_init_data(init_data: str, bot_token: str) -> dict:
    """Проверка подписи по https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app"""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not received_hash or not hmac.compare_digest(expected, received_hash):
        raise web.HTTPUnauthorized(text="bad signature")
    if time.time() - int(pairs.get("auth_date", 0)) > INIT_DATA_TTL:
        raise web.HTTPUnauthorized(text="init data expired, reopen the app")
    return json.loads(pairs.get("user", "{}"))


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _payment(row) -> dict:
    return {k: row[k] for k in row.keys()}


def create_app(db: DB, bot_token: str, allowed_users: set[int]) -> web.Application:

    @web.middleware
    async def auth(request: web.Request, handler: Callable):
        if not request.path.startswith("/api/"):
            return await handler(request)
        header = request.headers.get("Authorization", "")
        if not header.startswith("tma "):
            return _json_error(401, "no init data")
        try:
            user = check_init_data(header[4:], bot_token)
        except web.HTTPUnauthorized as ex:
            return _json_error(401, ex.text)
        uid = user.get("id")
        if not uid or (allowed_users and uid not in allowed_users):
            return _json_error(403, "Нет доступа")
        request["chat_id"] = uid
        return await handler(request)

    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_):
        return web.FileResponse(INDEX, headers={"Cache-Control": "no-cache"})

    @routes.get("/health")
    async def health(_):
        return web.Response(text="ok")

    @routes.get("/api/categories")
    async def categories(request):
        return web.json_response([dict(c) for c in db.categories(request["chat_id"])])

    @routes.post("/api/categories")
    async def add_category(request):
        name = " ".join(str((await request.json()).get("name", "")).split())[:40]
        if not name:
            return _json_error(400, "Пустое название")
        cid = db.add_category(request["chat_id"], name)
        return web.json_response({"id": cid, "name": db.category_name(request["chat_id"], cid)})

    @routes.post("/api/parse")
    async def parse(request):
        """Принимает одну картинку (multipart, поле file), возвращает распознанные поля."""
        chat_id = request["chat_id"]
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "file":
            return _json_error(400, "Нет файла")
        image = await part.read(decode=False)
        try:
            slip = await asyncio.to_thread(parse_slip, bytes(image))
        except SlipParseError as ex:
            return _json_error(422, str(ex))
        dup = db.find_by_tx(chat_id, slip.tx_ref)
        return web.json_response({
            "slip": slip.to_dict(),
            "duplicate": _payment(dup) if dup else None,
            "suggested_category_id": db.suggest_category(chat_id, slip.recipient_account),
        })

    @routes.post("/api/payments")
    async def add_payment(request):
        chat_id = request["chat_id"]
        body = await request.json()
        try:
            slip = Slip.from_dict(body["slip"])
            category_id = int(body["category_id"])
        except (KeyError, TypeError, ValueError):
            return _json_error(400, "Некорректные данные")
        if not db.category_name(chat_id, category_id):
            return _json_error(400, "Категория не найдена")
        dup = db.find_by_tx(chat_id, slip.tx_ref)
        if dup:
            return _json_error(409, f"Уже добавлена как #{dup['id']}")
        return web.json_response({"id": db.add_payment(chat_id, category_id, slip)})

    @routes.patch("/api/payments/{pid:\\d+}")
    async def update_payment(request):
        chat_id = request["chat_id"]
        category_id = int((await request.json()).get("category_id", 0))
        if not db.category_name(chat_id, category_id):
            return _json_error(400, "Категория не найдена")
        if not db.set_category(chat_id, int(request.match_info["pid"]), category_id):
            return _json_error(404, "Платёж не найден")
        return web.json_response({"ok": True})

    @routes.delete("/api/payments/{pid:\\d+}")
    async def delete_payment(request):
        if not db.delete_payment(request["chat_id"], int(request.match_info["pid"])):
            return _json_error(404, "Платёж не найден")
        return web.json_response({"ok": True})

    @routes.get("/api/month")
    async def month(request):
        chat_id = request["chat_id"]
        m = request.query.get("m", "")
        if len(m) != 7 or m[4] != "-":
            return _json_error(400, "m=YYYY-MM")
        return web.json_response({
            "month": m,
            "totals": [dict(t) for t in db.month_totals(chat_id, m)],
            "payments": [_payment(p) for p in db.month_payments(chat_id, m)],
        })

    @routes.get("/api/months")
    async def months(request):
        return web.json_response([dict(r) for r in db.months_summary(request["chat_id"])])

    app = web.Application(middlewares=[auth], client_max_size=20 * 1024 * 1024)
    app.add_routes(routes)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    # локально host=127.0.0.1 (снаружи — только через ngrok), на Railway 0.0.0.0
    await web.TCPSite(runner, host, port).start()
    return runner
