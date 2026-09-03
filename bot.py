import asyncio
import logging
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram.enums import ParseMode

# ======================== НАСТРОЙКИ ========================
BOT_TOKEN = "ВАШ_ТОКЕН_ОТ_BOTFATHER"
ADMIN_IDS = [123456789]            # Ваши Telegram ID
CHANNEL_ID = -1001234567890        # ID канала (начинается на -100)
DB_PATH = "bot_database.db"        # Файл базы данных
# ===========================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

router_user = Router()
router_admin = Router()
dp.include_router(router_user)
dp.include_router(router_admin)

# ======================== БАЗА ДАННЫХ ========================

async def init_db():
    """Создаёт таблицы при первом запуске."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                from_chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                is_anonymous INTEGER DEFAULT 0,
                username TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS statistics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                action TEXT NOT NULL,
                is_anonymous INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def add_pending_post(user_id, from_chat_id, message_id, is_anonymous, username) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO pending_posts (user_id, from_chat_id, message_id, is_anonymous, username) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, from_chat_id, message_id, int(is_anonymous), username)
        )
        await db.commit()
        return cursor.lastrowid

async def get_pending_post(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM pending_posts WHERE id = ?", (post_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

async def delete_pending_post(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pending_posts WHERE id = ?", (post_id,))
        await db.commit()

async def add_stat(user_id, username, action, is_anonymous):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO statistics (user_id, username, action, is_anonymous) "
            "VALUES (?, ?, ?, ?)",
            (user_id, username, action, int(is_anonymous))
        )
        await db.commit()

async def get_stats_summary() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT action, COUNT(*) as cnt FROM statistics GROUP BY action"
        )
        rows = await cursor.fetchall()
        result = {"approved": 0, "rejected": 0}
        for row in rows:
            result[row[0]] = row[1]
        result["total"] = result["approved"] + result["rejected"]
        return result

async def get_top_authors(limit=10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT username, COUNT(*) as cnt FROM statistics "
            "WHERE action = 'approved' AND username IS NOT NULL "
            "GROUP BY user_id ORDER BY cnt DESC LIMIT ?",
            (limit,)
        )
        return await cursor.fetchall()

# ======================== СОСТОЯНИЯ ========================

class PostStates(StatesGroup):
    waiting_for_post = State()
    reviewing_post = State()

class SupportStates(StatesGroup):
    waiting_for_message = State()

# Словарь для поддержки: message_id админа -> user_id
support_tickets = {}

# ======================== КЛАВИАТУРЫ ========================

def get_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Предложить пост", callback_data="create_post")],
        [InlineKeyboardButton(text="🆘 Написать в поддержку", callback_data="support")]
    ])

def get_post_review_kb(is_anonymous: bool):
    anon_label = "🔒 Анонимно ✅" if is_anonymous else "👤 С именем ✅"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_post"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data="delete_post")],
        [InlineKeyboardButton(text=anon_label, callback_data="toggle_anon")],
        [InlineKeyboardButton(text="🚀 Опубликовать", callback_data="publish_post")]
    ])

def get_admin_approval_kb(post_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{post_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{post_id}")]
    ])

# ======================== /START ========================

@router_user.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Это бот для предложений в наш канал.\n\n"
        "Нажмите кнопку ниже, чтобы предложить пост или написать в поддержку.",
        reply_markup=get_main_kb()
    )

# ======================== СОЗДАНИЕ ПОСТА ========================

@router_user.callback_query(F.data == "create_post")
async def start_creating_post(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PostStates.waiting_for_post)
    await state.update_data(is_anonymous=False)
    await callback.message.edit_text(
        "📝 Отправьте пост, который хотите предложить.\n"
        "Поддерживается: текст, фото, видео, документы, GIF."
    )
    await callback.answer()

@router_user.message(
    PostStates.waiting_for_post,
    F.content_type.in_(['text', 'photo', 'video', 'document', 'animation'])
)
async def receive_post(message: Message, state: FSMContext):
    # Сохраняем ID сообщения и чата для будущего копирования
    await state.update_data(
        post_message_id=message.message_id,
        post_chat_id=message.chat.id
    )
    await state.set_state(PostStates.reviewing_post)

    # Предпросмотр: копируем пост обратно пользователю
    await message.answer("👁 Предпросмотр вашего поста:")
    await message.copy_to(message.chat.id)

    data = await state.get_data()
    is_anon = data.get("is_anonymous", False)
    await message.answer(
        "Выберите действие с постом:",
        reply_markup=get_post_review_kb(is_anon)
    )

# ======================== ДЕЙСТВИЯ С ПОСТОМ ========================

@router_user.callback_query(PostStates.reviewing_post, F.data == "edit_post")
async def edit_post(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PostStates.waiting_for_post)
    await callback.message.edit_text("✏️ Отправьте новый вариант поста.")
    await callback.answer()

@router_user.callback_query(PostStates.reviewing_post, F.data == "delete_post")
async def delete_post(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🗑 Пост удалён.",
        reply_markup=get_main_kb()
    )
    await callback.answer()

@router_user.callback_query(PostStates.reviewing_post, F.data == "toggle_anon")
async def toggle_anonymity(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    new_state = not data.get("is_anonymous", False)
    await state.update_data(is_anonymous=new_state)

    status = "🔒 Анонимный режим включён. Админы НЕ увидят ваш аккаунт." \
        if new_state else \
        "👤 Режим с именем. Админы увидят ваш аккаунт."

    await callback.message.edit_text(
        f"Выберите действие с постом:\n\n{status}",
        reply_markup=get_post_review_kb(new_state)
    )
    await callback.answer("Режим переключён!")

# ======================== ПУБЛИКАЦИЯ (ОТПРАВКА АДМИНУ) ========================

@router_user.callback_query(PostStates.reviewing_post, F.data == "publish_post")
async def publish_post(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = callback.from_user
    is_anon = data.get("is_anonymous", False)
    username = f"@{user.username}" if user.username else user.full_name

    # Сохраняем пост в БД и получаем его ID
    post_id = await add_pending_post(
        user_id=user.id,
        from_chat_id=data["post_chat_id"],
        message_id=data["post_message_id"],
        is_anonymous=is_anon,
        username=username
    )

    # Отправляем пост каждому админу
    for admin_id in ADMIN_IDS:
        try:
            # 1) Копируем сам пост
            await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=data["post_chat_id"],
                message_id=data["post_message_id"]
            )
            # 2) Отправляем информацию + кнопки
            if is_anon:
                author_info = "👤 Автор: <i>скрыт (анонимная заявка)</i>"
            else:
                author_info = f"👤 Автор: {username} (ID: <code>{user.id}</code>)"

            await bot.send_message(
                admin_id,
                f"📬 <b>Новая заявка на публикацию</b>\n"
                f"{author_info}\n"
                f"🆔 Заявка #{post_id}",
                parse_mode=ParseMode.HTML,
                reply_markup=get_admin_approval_kb(post_id)
            )
        except Exception as e:
            logging.error(f"Ошибка отправки админу {admin_id}: {e}")

    await state.clear()
    await callback.message.edit_text(
        "✅ Заявка отправлена на модерацию!\n"
        "Мы уведомим вас о решении.",
        reply_markup=get_main_kb()
    )
    await callback.answer("Отправлено!")

# ======================== МОДЕРАЦИЯ (АДМИН) ========================

@router_admin.callback_query(F.data.startswith("approve_"))
async def approve_post(callback: CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    post = await get_pending_post(post_id)

    if not post:
        await callback.answer("⚠️ Заявка не найдена (уже обработана).", show_alert=True)
        return

    # Копируем пост в канал
    try:
        await bot.copy_message(
            chat_id=CHANNEL_ID,
            from_chat_id=post["from_chat_id"],
            message_id=post["message_id"]
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка публикации в канал: {e}")
        await callback.answer()
        return

    # Записываем в статистику
    await add_stat(post["user_id"], post["username"], "approved", post["is_anonymous"])
    await delete_pending_post(post_id)

    await callback.message.edit_text(
        f"✅ Заявка #{post_id} <b>принята</b> и опубликована в канале.",
        parse_mode=ParseMode.HTML
    )
    await callback.answer("Опубликовано!")

    # Уведомляем автора
    try:
        await bot.send_message(
            post["user_id"],
            "🎉 <b>Ваш пост был опубликован в канале!</b>",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass  # Пользователь мог заблокировать бота

@router_admin.callback_query(F.data.startswith("reject_"))
async def reject_post(callback: CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    post = await get_pending_post(post_id)

    if not post:
        await callback.answer("⚠️ Заявка не найдена (уже обработана).", show_alert=True)
        return

    await add_stat(post["user_id"], post["username"], "rejected", post["is_anonymous"])
    await delete_pending_post(post_id)

    await callback.message.edit_text(
        f"❌ Заявка #{post_id} <b>отклонена</b>.",
        parse_mode=ParseMode.HTML
    )
    await callback.answer("Отклонено.")

    try:
        await bot.send_message(
            post["user_id"],
            "😔 К сожалению, ваш пост был <b>отклонён</b> администраторами.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

# ======================== ПОДДЕРЖКА ========================

@router_user.callback_query(F.data == "support")
async def start_support(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportStates.waiting_for_message)
    await callback.message.edit_text(
        "🆘 Опишите вашу проблему или вопрос.\n"
        "Администратор ответит вам в ближайшее время."
    )
    await callback.answer()

@router_user.message(SupportStates.waiting_for_message)
async def receive_support_message(message: Message, state: FSMContext):
    user = message.from_user
    username = f"@{user.username}" if user.username else user.full_name

    for admin_id in ADMIN_IDS:
        try:
            await message.forward(admin_id)
            info_msg = await bot.send_message(
                admin_id,
                f"🆘 <b>Обращение в поддержку</b>\n"
                f"👤 От: {username} (ID: <code>{user.id}</code>)\n\n"
                f"↩️ <i>Ответьте на это сообщение, чтобы ответить пользователю.</i>",
                parse_mode=ParseMode.HTML
            )
            support_tickets[info_msg.message_id] = user.id
        except Exception as e:
            logging.error(f"Ошибка отправки в поддержку: {e}")

    await state.clear()
    await message.answer(
        "✅ Сообщение отправлено в поддержку. Ожидайте ответа!",
        reply_markup=get_main_kb()
    )

# Ответ админа пользователю (reply на инфо-сообщение поддержки)
@router_admin.message(F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def admin_reply_to_support(message: Message):
    replied_msg_id = message.reply_to_message.message_id

    if replied_msg_id in support_tickets:
        user_id = support_tickets.pop(replied_msg_id)
        reply_text = message.text or message.caption or "<i>Сообщение без текста</i>"

        try:
            await bot.send_message(
                user_id,
                f"💬 <b>Ответ от поддержки:</b>\n\n{reply_text}",
                parse_mode=ParseMode.HTML
            )
            await message.answer("✅ Ответ отправлен пользователю.")
        except Exception as e:
            await message.answer(f"❌ Не удалось отправить ответ: {e}")

# ======================== СТАТИСТИКА (АДМИН) ========================

@router_admin.message(Command("stats"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_stats(message: Message):
    args = message.text.split(maxsplit=1)

    if len(args) > 1 and args[1].lower() == "top":
        # Топ авторов
        top = await get_top_authors(10)
        if not top:
            await message.answer("📊 Пока нет опубликованных постов.")
            return

        lines = ["🏆 <b>Топ авторов (по опубликованным постам):</b>\n"]
        for i, (username, count) in enumerate(top, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            lines.append(f"{medal} {username} — <b>{count}</b> постов")

        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    else:
        # Общая сводка
        stats = await get_stats_summary()
        await message.answer(
            "📊 <b>Статистика предложки:</b>\n\n"
            f"✅ Принято: <b>{stats['approved']}</b>\n"
            f"❌ Отклонено: <b>{stats['rejected']}</b>\n"
            f"📋 Всего обработано: <b>{stats['total']}</b>\n\n"
            f"💡 Используйте /stats top — топ авторов.",
            parse_mode=ParseMode.HTML
        )

# ======================== ЗАПУСК ========================

async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")