import asyncio
import os
import sqlite3
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CARD_NUMBER = os.getenv("CARD_NUMBER", "").strip()
CARD_HOLDER = os.getenv("CARD_HOLDER", "").strip()
PAYMENT_INSTRUCTIONS = os.getenv(
    "PAYMENT_INSTRUCTIONS",
    "Переведите точную сумму и отправьте чек в этот чат."
).strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
DB_PATH = os.getenv("DB_PATH", "orders.sqlite3")

# Измени цены под свою экономику. Это цена услуги/заказа, не курс Telegram.
PACKAGES = {
    "p100": {"stars": 100, "price": 25000},
    "p250": {"stars": 250, "price": 55000},
    "p500": {"stars": 500, "price": 100000},
    "p1000": {"stars": 1000, "price": 190000},
}

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env")
if ADMIN_ID <= 0:
    raise RuntimeError("Укажи свой числовой ADMIN_ID в .env")
if not CARD_NUMBER:
    raise RuntimeError("Укажи CARD_NUMBER в .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            recipient TEXT,
            package_key TEXT NOT NULL,
            stars INTEGER NOT NULL,
            price INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            receipt_file_id TEXT,
            paid_at TEXT,
            completed_at TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            order_id INTEGER,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_action(user_id, action, order_id=None):
    with db() as con:
        con.execute(
            "INSERT INTO actions(user_id, order_id, action, created_at) VALUES(?,?,?,?)",
            (user_id, order_id, action[:1000], now())
        )


def get_order(order_id):
    with db() as con:
        return con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def package_keyboard():
    rows = []
    for key, p in PACKAGES.items():
        rows.append([InlineKeyboardButton(
            text=f"⭐ {p['stars']:,} Stars — {p['price']:,} сум".replace(",", " "),
            callback_data=f"buy:{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_keyboard(order_id, status):
    rows = []
    if status == "awaiting_receipt":
        rows = [
            [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"paid:{order_id}")],
            [InlineKeyboardButton(text="❌ Отклонить чек", callback_data=f"reject:{order_id}")],
        ]
    elif status == "paid":
        rows = [[InlineKeyboardButton(
            text="📦 Отметить выполненным", callback_data=f"done:{order_id}"
        )]]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def notify_admin(text, order_id=None, status=None):
    if not ADMIN_ID:
        return
    markup = order_keyboard(order_id, status) if order_id and status else None
    await bot.send_message(ADMIN_ID, text, reply_markup=markup)


async def log_user_message(message: Message, order_id=None):
    """Forward the customer's message to admin and record the action."""
    user = message.from_user
    label = (
        "👤 Сообщение клиента\n"
        f"ID: {user.id}\n"
        f"Имя: {user.full_name}\n"
        f"Username: @{user.username or 'не указан'}\n"
        f"Заказ: #{order_id if order_id else 'не выбран'}\n"
        f"Тип сообщения: {message.content_type}"
    )
    log_action(user.id, f"message:{message.content_type}", order_id)
    try:
        await bot.send_message(ADMIN_ID, label)
        await bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
    except Exception as exc:
        # Ошибку не показываем клиенту, но пишем в консоль для владельца.
        print(f"Не удалось переслать сообщение админу: {exc}")


@dp.message(CommandStart())
async def start(message: Message):
    user = message.from_user
    log_action(user.id, "/start")
    await notify_admin(
        "🆕 Клиент открыл магазин\n"
        f"ID: {user.id}\nИмя: {user.full_name}\n"
        f"Username: @{user.username or 'не указан'}"
    )
    await message.answer(
        "⭐ Добро пожаловать в Zangiyev Stars Shop!\n\n"
        "Выберите пакет ниже. После выбора укажите username получателя, "
        "переведите сумму на карту и отправьте чек.\n\n"
        "Важно: сообщения и отправленные чеки в этом боте передаются администратору "
        "для обработки заказа. Перевод подтверждается вручную. "
        "Подтверждение оплаты не означает, что Stars уже зачислены.",
        reply_markup=package_keyboard()
    )


@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "Команды:\n/start — открыть магазин\n/myid — узнать свой Telegram ID\n"
        "/status — показать последние заказы\n\n"
        "Для поддержки напишите сообщение сюда."
    )


@dp.message(Command("status"))
async def status_command(message: Message):
    with db() as con:
        rows = con.execute(
            "SELECT id, stars, price, status, created_at FROM orders "
            "WHERE user_id=? ORDER BY id DESC LIMIT 5",
            (message.from_user.id,)
        ).fetchall()
    if not rows:
        await message.answer("У тебя пока нет заказов.")
        return
    labels = {
        "awaiting_username": "нужен username получателя",
        "awaiting_receipt": "ожидается чек/проверка оплаты",
        "paid": "оплата подтверждена, ожидается выполнение",
        "rejected": "чек отклонён",
        "completed": "выполнен",
    }
    text = "📋 Твои последние заказы:\n\n" + "\n".join(
        f"#{r['id']} — {r['stars']} Stars — {r['price']:,} сум — "
        f"{labels.get(r['status'], r['status'])}".replace(",", " ")
        for r in rows
    )
    await message.answer(text)


@dp.callback_query(F.data.startswith("buy:"))
async def choose_package(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    package = PACKAGES.get(key)
    if not package:
        await call.answer("Пакет не найден.", show_alert=True)
        return

    user = call.from_user
    with db() as con:
        cur = con.execute(
            """INSERT INTO orders
            (user_id, username, full_name, package_key, stars, price, status, created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (user.id, user.username, user.full_name, key, package["stars"],
             package["price"], "awaiting_username", now())
        )
        order_id = cur.lastrowid

    log_action(user.id, f"selected package {package['stars']} Stars", order_id)
    await notify_admin(
        f"🛒 Новый заказ #{order_id}\n"
        f"Клиент: {user.full_name} (ID {user.id})\n"
        f"Username клиента: @{user.username or 'не указан'}\n"
        f"Пакет: {package['stars']} Stars\n"
        f"Сумма: {package['price']:,} сум\n"
        "Статус: ожидается username получателя".replace(",", " "),
        order_id
    )
    await call.message.answer(
        f"Заказ #{order_id}\nПакет: ⭐ {package['stars']} Stars\n"
        f"Цена: {package['price']:,} сум\n\n"
        "Теперь отправьте username получателя, например @username.\n"
        "Убедитесь, что username введён правильно.".replace(",", " ")
    )
    await call.answer()


@dp.message(F.text)
async def handle_text(message: Message):
    if message.from_user.id == ADMIN_ID:
        # Ответы админа поддерживаются командой /reply USER_ID текст
        if message.text.startswith("/reply "):
            parts = message.text.split(maxsplit=2)
            if len(parts) < 3 or not parts[1].isdigit():
                await message.answer("Формат: /reply USER_ID текст")
                return
            target = int(parts[1])
            try:
                await bot.send_message(target, "💬 Сообщение администратора:\n" + parts[2])
                await message.answer("Сообщение отправлено.")
            except Exception as exc:
                await message.answer(f"Не удалось отправить сообщение: {exc}")
            return
        return

    user_id = message.from_user.id
    text = message.text.strip()
    with db() as con:
        row = con.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='awaiting_username' "
            "ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()

    if row and not text.startswith("/"):
        recipient = text.lstrip("@").strip()
        if not recipient or not recipient.replace("_", "").isalnum():
            await message.answer("Введи корректный Telegram username, например @username.")
            await log_user_message(message, row["id"])
            return

        with db() as con:
            con.execute(
                "UPDATE orders SET recipient=?, status='awaiting_receipt' WHERE id=?",
                (recipient, row["id"])
            )
        log_action(user_id, f"recipient set to @{recipient}", row["id"])
        await notify_admin(
            f"🎯 Получатель указан для заказа #{row['id']}\n"
            f"Клиент ID: {user_id}\nПолучатель: @{recipient}\n"
            f"Сумма: {row['price']:,} сум\n"
            f"Реквизиты карты: {CARD_NUMBER}\n"
            f"Владелец карты: {CARD_HOLDER or 'не указан'}".replace(",", " "),
            row["id"], "awaiting_receipt"
        )
        card_text = (
            f"💳 Оплата заказа #{row['id']}\n\n"
            f"Получатель Stars: @{recipient}\n"
            f"Пакет: ⭐ {row['stars']} Stars\n"
            f"К оплате: {row['price']:,} сум\n\n"
            f"Карта: {CARD_NUMBER}\n"
            f"Владелец: {CARD_HOLDER or 'не указан'}\n\n"
            f"{PAYMENT_INSTRUCTIONS}\n\n"
            "После перевода отправьте сюда скриншот или фото чека. "
            "Не отправляйте PIN, CVV или коды из SMS.".replace(",", " ")
        )
        await message.answer(card_text)
        await message.answer(
            "Когда администратор подтвердит оплату, вы получите уведомление. "
            "Зачисление Stars подтверждается отдельно."
        )
        return

    # Все прочие текстовые сообщения клиента передаются админу.
    await log_user_message(message)
    await message.answer(
        "Сообщение передано администратору. Чтобы создать заказ, нажми /start."
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    with db() as con:
        row = con.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='awaiting_receipt' "
            "ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()

    if row:
        file_id = message.photo[-1].file_id
        with db() as con:
            con.execute(
                "UPDATE orders SET receipt_file_id=? WHERE id=?",
                (file_id, row["id"])
            )
        log_action(user_id, "receipt photo submitted", row["id"])
        await notify_admin(
            f"🧾 Получен чек по заказу #{row['id']}\n"
            f"Клиент: {message.from_user.full_name} (ID {user_id})\n"
            f"Получатель: @{row['recipient']}\n"
            f"Сумма: {row['price']:,} сум\n"
            "Проверь поступление денег в банковском приложении.".replace(",", " "),
            row["id"], "awaiting_receipt"
        )
        try:
            await bot.copy_message(ADMIN_ID, message.chat.id, message.message_id)
        except Exception as exc:
            print(f"Не удалось переслать чек: {exc}")
        await message.answer(
            f"Чек по заказу #{row['id']} отправлен администратору. "
            "Ожидай проверки."
        )
    else:
        await log_user_message(message)
        await message.answer("Фото передано администратору.")


@dp.message(F.document | F.video | F.animation | F.voice | F.audio | F.sticker)
async def handle_other_media(message: Message):
    await log_user_message(message)
    await message.answer("Сообщение передано администратору.")


@dp.callback_query(F.data.startswith("paid:"))
async def admin_paid(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только администратор.", show_alert=True)
        return
    order_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row or row["status"] != "awaiting_receipt":
            await call.answer("Заказ не найден или уже обработан.", show_alert=True)
            return
        con.execute(
            "UPDATE orders SET status='paid', paid_at=? WHERE id=?",
            (now(), order_id)
        )
    log_action(ADMIN_ID, f"admin confirmed payment for order {order_id}", order_id)
    await bot.send_message(
        row["user_id"],
        f"✅ Оплата заказа #{order_id} подтверждена.\n"
        "Статус: ожидает пополнения Stars. Если Stars не поступили, напиши в поддержку.Заказ поступит чрез некоторое время "
    )
    await call.message.edit_reply_markup(reply_markup=order_keyboard(order_id, "paid"))
    await call.message.answer("Оплата отмечена. После фактического выполнения нажми кнопку ниже.")
    await call.answer("Оплата подтверждена.")


@dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только администратор.", show_alert=True)
        return
    order_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row or row["status"] != "awaiting_receipt":
            await call.answer("Заказ не найден или уже обработан.", show_alert=True)
            return
        con.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
    log_action(ADMIN_ID, f"admin rejected receipt for order {order_id}", order_id)
    await bot.send_message(
        row["user_id"],
        f"❌ Чек по заказу #{order_id} не подтверждён. "
        "Проверь перевод или свяжись с администратором."
    )
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Чек отклонён.")


@dp.callback_query(F.data.startswith("done:"))
async def admin_done(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только администратор.", show_alert=True)
        return
    order_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row or row["status"] != "paid":
            await call.answer("Сначала подтвердите оплату.", show_alert=True)
            return
        con.execute(
            "UPDATE orders SET status='completed', completed_at=? WHERE id=?",
            (now(), order_id)
        )
    log_action(ADMIN_ID, f"admin marked order {order_id} completed", order_id)
    await bot.send_message(
        row["user_id"],
        f"📦 Заказ #{order_id} отмечен выполненным администратором.\n"
        "Если Stars не поступили, немедленно напиши в поддержку."
    )
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Заказ отмечен выполненным.")


async def main():
    init_db()
    print("Dranes Stars Shop запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
