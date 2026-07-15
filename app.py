import os
import json
import logging
import sqlite3
import hmac
import hashlib
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

# Загружаем переменные
load_dotenv()

app = FastAPI()

# Получаем ключи из переменных окружения (панель хостинга или .env)
TERMINAL_KEY = os.getenv("TERMINAL_KEY")
PASSWORD = os.getenv("PASSWORD")
DB_PATH = os.getenv("DB_PATH", "KotShop241/orders.db")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Нужен для логов в админку, если будем слать туда

if not TERMINAL_KEY or not PASSWORD:
    logging.error("❌ Не найдены TERMINAL_KEY или PASSWORD в переменных окружения!")
    # Контейнер упадет сразу, чтобы не висел с ошибкой позже
    raise RuntimeError("Missing T-Bank credentials")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def init_db():
    """Создает таблицу, если её нет"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            payment_id TEXT,
            telegram_id INTEGER,
            pubg_id TEXT,
            uc_amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")


init_db()


def get_order_by_payment_id(payment_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE payment_id = ?", (payment_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


@app.get("/api/check-status/{order_id}")
async def check_status(order_id: str):
    """Эту ручку будет дергать твой бот (aiogram) для проверки статуса"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return JSONResponse({"status": "unknown"})
    return JSONResponse({"status": row[0]})


@app.post("/webhook/tinkoff")
async def tinkoff_webhook(request: Request):
    # 1. Читаем тело как байты (КРИТИЧНО важно для подписи)
    body_bytes = await request.body()

    # 2. Получаем заголовок подписи
    signature_header = request.headers.get("X-Signature")
    if not signature_header:
        logger.warning("⚠️ Нет заголовка X-Signature")
        raise HTTPException(status_code=403, detail="Missing signature")

    # 3. Формируем строку для проверки
    # ПРАВИЛЬНАЯ ФОРМУЛА Т-БАНКА: body + TERMINAL_KEY + PASSWORD
    # Важно: body должен быть именно тем байтовым потоком, который пришел.
    message_to_sign = body_bytes + TERMINAL_KEY.encode('utf-8') + PASSWORD.encode('utf-8')

    expected_sig = hmac.new(
        PASSWORD.encode('utf-8'),
        message_to_sign,
        hashlib.sha256
    ).hexdigest()

    # 4. Сравниваем
    if not hmac.compare_digest(signature_header, expected_sig):
        logger.error(f"❌ Неверная подпись! Ожидалось: {expected_sig[:10]}..., Пришло: {signature_header[:10]}...")
        raise HTTPException(status_code=403, detail="Invalid signature")

    logger.info("✅ Подпись подтверждена")

    try:
        data = json.loads(body_bytes.decode('utf-8'))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    payment_id = data.get("PaymentId")
    status = data.get("Status")
    amount = data.get("Amount")

    logger.info(f"[Webhook] PaymentId={payment_id}, Status={status}")

    order = get_order_by_payment_id(payment_id)

    if not order:
        # Если заказ не найден в нашей БД, мы не можем ничего сделать.
        # Т-Банку надо вернуть 200 OK, иначе он будет слать вебхуки вечно.
        logger.warning(f"⚠️ Заказ не найден для PaymentId={payment_id}. Возвращаем 200 OK.")
        return JSONResponse({"status": "ok", "action": "not_found"})

    telegram_id = order["telegram_id"]
    pubg_id = order["pubg_id"]
    uc_amount = order["uc_amount"]
    current_status = order["status"]

    # Если статус уже обработан, ничего не делаем
    if current_status in ['issued', 'cancelled']:
        return JSONResponse({"status": "ok", "action": "already_processed"})

    if status == "CONFIRMED":
        logger.info(f"✅ Платеж подтвержден. Готовим выдачу UC для PUBG ID: {pubg_id}")

        # ЗДЕСЬ БУДЕТ ТВОЯ ЛОГИКА ВЫДАЧИ UC (вызов внешнего API, ручное действие и т.д.)
        # Пока просто логируем

        msg = (
            f"✅ Оплата подтверждена!\n"
            f"PaymentId: `{payment_id}`\n"
            f"UC: {uc_amount}\n"
            f"ID PUBG: `{pubg_id}`"
        )

        # ВАЖНО: Мы НЕ можем отправить сообщение в Telegram прямо здесь,
        # так как у нас нет запущенного экземпляра aiogram.Bot в этом процессе.
        # Стратегия:
        # 1. Пишем в БД статус 'issued'.
        # 2. Бот (другой процесс) видит изменение в БД и сам шлет сообщение.

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE orders SET status = 'issued' WHERE payment_id = ?", (payment_id,))
        conn.commit()
        conn.close()

        logger.info("💾 Статус заказа обновлен на 'issued'. Бот должен заметить это при следующем опросе.")

        return JSONResponse({
            "status": "ok",
            "action": "issued",
            "verified": True
        })

    elif status in ("REJECTED", "EXPIRED"):
        logger.warning(f"❌ Платёж отклонён: {payment_id}")
        msg = f"❌ Платёж не прошёл. Статус: `{status}`."

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE orders SET status = 'cancelled' WHERE payment_id = ?", (payment_id,))
        conn.commit()
        conn.close()

        return JSONResponse({
            "status": "ok",
            "action": "cancelled",
            "verified": True
        })
    else:
        # Промежуточные статусы (PROCESSING и т.п.) - просто логируем, ничего не меняем в БД
        return JSONResponse({
            "status": "ok",
            "action": "pending",
            "verified": True
        })
