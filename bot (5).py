import asyncio
import os
from datetime import datetime

import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

TOKEN = "8727009044:AAElLexooRuM7mOp_Fiw2wYa0RmzTFHVT5w"
ADMIN_ID = 7433735132
CHANNEL_ID = -1003761100023

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB = "reviews.db"


# ================= KEYBOARDS =================

main_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="✍️ Написать отзыв")]],
    resize_keyboard=True
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отменить")]],
    resize_keyboard=True
)

admin_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Написать отзыв")],
        [KeyboardButton(text="📋 Все отзывы"), KeyboardButton(text="🗑 Очистить БД")],
        [KeyboardButton(text="📦 Экспорт БД")]
    ],
    resize_keyboard=True
)


def get_kb(user_id: int) -> ReplyKeyboardMarkup:
    return admin_kb if user_id == ADMIN_ID else main_kb


def moderation_kb(review_id: int) -> InlineKeyboardMarkup:
    """Inline-кнопки под уведомлением о новом отзыве."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{review_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline:{review_id}")
    ]])


# ================= DATABASE =================

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            rating INTEGER,
            created_at TEXT,
            status TEXT DEFAULT 'pending',
            from_chat_id INTEGER,
            message_id INTEGER
        )
        """)

        existing = {
            row[1]
            async for row in await db.execute("PRAGMA table_info(reviews)")
        }
        migrations = {
            "created_at": "ALTER TABLE reviews ADD COLUMN created_at TEXT",
            "from_chat_id": "ALTER TABLE reviews ADD COLUMN from_chat_id INTEGER",
            "message_id": "ALTER TABLE reviews ADD COLUMN message_id INTEGER",
        }
        for col, sql in migrations.items():
            if col not in existing:
                await db.execute(sql)

        if "time" in existing and "created_at" in existing:
            await db.execute(
                "UPDATE reviews SET created_at = time WHERE created_at IS NULL"
            )

        await db.commit()


async def create_review(user_id, username, text, rating, created_at, from_chat_id, message_id):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("""
            INSERT INTO reviews (user_id, username, text, rating, created_at, status, from_chat_id, message_id)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (user_id, username, text, rating, created_at, from_chat_id, message_id))
        await db.commit()
        return cur.lastrowid


async def get_review(review_id):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("""
            SELECT user_id, username, text, rating, created_at, from_chat_id, message_id
            FROM reviews WHERE id = ?
        """, (review_id,))
        return await cur.fetchone()


async def set_status(review_id, status):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE reviews SET status = ? WHERE id = ?", (status, review_id))
        await db.commit()


async def get_average_rating() -> tuple:
    """Возвращает средний рейтинг и количество принятых отзывов."""
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT AVG(rating), COUNT(*) FROM reviews WHERE status = 'accepted'"
        )
        row = await cur.fetchone()
        avg = round(row[0], 1) if row[0] else 0.0
        count = row[1] or 0
        return avg, count


# ================= FSM =================

class ReviewState(StatesGroup):
    text = State()
    rating = State()


class DeclineState(StatesGroup):
    reason = State()


# ================= HANDLERS =================

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Нажми кнопку чтобы оставить отзыв 👇",
        reply_markup=get_kb(message.from_user.id)
    )


# --- Отмена ---

@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Отменить")
async def cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять.", reply_markup=get_kb(message.from_user.id))
        return
    await state.clear()
    await message.answer("❌ Отзыв отменён.", reply_markup=get_kb(message.from_user.id))


# --- Написать отзыв ---

@dp.message(Command("review"))
@dp.message(F.text == "✍️ Написать отзыв")
async def review_cmd(message: Message, state: FSMContext):
    await state.set_state(ReviewState.text)
    await message.answer("Напиши отзыв ✍️", reply_markup=cancel_kb)


@dp.message(ReviewState.text)
async def get_text(message: Message, state: FSMContext):
    if not message.text:
        return

    await state.update_data(
        text=message.text,
        message_id=message.message_id,
        from_chat_id=message.chat.id
    )
    await state.set_state(ReviewState.rating)
    await message.answer("Оцени от 1 до 10 ⭐", reply_markup=cancel_kb)


@dp.message(ReviewState.rating)
async def get_rating(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("Введите число 1–10")
        return

    rating = int(message.text)
    if rating < 1 or rating > 10:
        await message.answer("Только 1–10")
        return

    data = await state.get_data()
    user_id = message.from_user.id
    username = message.from_user.username or "no_username"
    text = data["text"]
    message_id = data["message_id"]
    from_chat_id = data["from_chat_id"]
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    review_id = await create_review(
        user_id, username, text, rating, created_at, from_chat_id, message_id
    )

    # Уведомление админу с inline-кнопками
    await bot.send_message(
        ADMIN_ID,
        f"📩 Новый отзыв #{review_id}\n"
        f"👤 @{username} | ID: {user_id}\n"
        f"⭐ {rating}/10\n"
        f"🕒 {created_at}",
        reply_markup=moderation_kb(review_id)
    )

    # Пересылаем оригинальное сообщение с текстом отзыва
    await bot.forward_message(
        chat_id=ADMIN_ID,
        from_chat_id=from_chat_id,
        message_id=message_id
    )

    await state.clear()
    await message.answer(
        "Спасибо! Отзыв отправлен на модерацию 🙏",
        reply_markup=get_kb(user_id)
    )


# ================= MODERATION CALLBACKS =================

@dp.callback_query(F.data.startswith("accept:"))
async def cb_accept(callback: CallbackQuery):
    review_id = int(callback.data.split(":")[1])
    data = await get_review(review_id)

    if not data:
        await callback.answer("❌ Отзыв не найден", show_alert=True)
        return

    user_id, username, text, rating, created_at, from_chat_id, msg_id = data

    await set_status(review_id, "accepted")

    # Публикуем в канал
    await bot.forward_message(
        chat_id=CHANNEL_ID,
        from_chat_id=from_chat_id,
        message_id=msg_id
    )
    await bot.send_message(
        CHANNEL_ID,
        f"⭐ Отзыв #{review_id}\n"
        f"⭐ {rating}/10\n\n"
        f"{text}\n\n"
        f"👤 @{username}\n"
        f"🕒 {created_at}"
    )

    # Средний рейтинг по всем принятым отзывам
    avg, count = await get_average_rating()
    stars = "⭐" * round(avg)
    await bot.send_message(
        CHANNEL_ID,
        f"📊 Средняя оценка: {avg}/10 {stars}\n"
        f"Всего отзывов: {count}"
    )

    # Уведомляем пользователя
    await bot.send_message(
        user_id,
        f"✅ Твой отзыв #{review_id} одобрен и опубликован!"
    )

    # Убираем кнопки с сообщения, ставим статус
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✔ Review#{review_id} опубликован")
    await callback.answer()


@dp.callback_query(F.data.startswith("decline:"))
async def cb_decline(callback: CallbackQuery, state: FSMContext):
    review_id = int(callback.data.split(":")[1])
    data = await get_review(review_id)

    if not data:
        await callback.answer("❌ Отзыв не найден", show_alert=True)
        return

    # Сохраняем review_id и ждём причину
    await state.update_data(declining_review_id=review_id, declining_user_id=data[0])
    await state.set_state(DeclineState.reason)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"✏️ Укажи причину отклонения отзыва #{review_id}\n"
        f"(или напиши «-» чтобы не указывать причину):",
        reply_markup=cancel_kb
    )
    await callback.answer()


@dp.message(DeclineState.reason)
async def get_decline_reason(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    review_id = data["declining_review_id"]
    user_id = data["declining_user_id"]
    reason = message.text.strip()

    await set_status(review_id, "declined")
    await state.clear()

    # Уведомляем пользователя
    if reason == "-":
        await bot.send_message(
            user_id,
            f"❌ Твой отзыв #{review_id} был отклонён."
        )
    else:
        await bot.send_message(
            user_id,
            f"❌ Твой отзыв #{review_id} был отклонён.\n\n"
            f"💬 Причина: {reason}"
        )

    await message.answer(
        f"✖ Review#{review_id} отклонён.",
        reply_markup=admin_kb
    )


# ================= ADMIN COMMANDS =================

@dp.message(Command("reviews"))
@dp.message(F.text == "📋 Все отзывы")
async def reviews_list(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id, username, rating, created_at, status FROM reviews"
        )
        rows = await cur.fetchall()

    await message.answer(
        "\n".join([f"{r[0]} | @{r[1]} | ⭐{r[2]}/10 | {r[3]} | {r[4]}" for r in rows])
        or "Пусто"
    )


@dp.message(Command("dbclear"))
@dp.message(F.text == "🗑 Очистить БД")
async def db_clear(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM reviews")
        await db.execute("DELETE FROM sqlite_sequence WHERE name='reviews'")
        await db.commit()

    await message.answer("🗑 База данных очищена. Файл сохранён, таблица пустая.")


@dp.message(Command("dbexport"))
@dp.message(F.text == "📦 Экспорт БД")
async def db_export(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    from aiogram.types import FSInputFile

    if not os.path.exists(DB):
        await message.answer("❌ Файл базы данных не найден")
        return

    await message.answer_document(
        FSInputFile(DB, filename="reviews.db"),
        caption="📦 База данных reviews.db"
    )


# ================= MAIN =================

async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
