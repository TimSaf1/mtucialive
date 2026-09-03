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
BOT_TOKEN = "8735590675:AAESiFJwwEAefwOM2oHxgoxMjFRz2TYXdFE"
ADMIN_IDS = [1115269766]            # Ваши Telegram ID
DB_PATH = "bot_database.db"

# ⬇️⬇️⬇️ СПИСОК ГРУПП ДЛЯ ПУБЛИКАЦИИ ⬇️⬇️⬇️
# Формат: (chat_id, "эмодзи и название")
# chat_id должен начинаться на -100 для супергрупп
GROUPS = [
    (-3978490992, "🌍 MTUCI LIFE"),
    (-4304746732, "🎮 MTUCI GAMING"),
    (-4331803743, "📰 MTUCI NEWS"),
    (-3965633723, "🍓 MTUCI VKUSNYATINA"),
    (-3949503731, "🧑‍🎓 MTUCI STUDY")
]
# ⬆️⬆️⬆️ ДОБАВЛЯЙТЕ СЮДА НОВЫЕ ГРУППЫ ⬆️⬆️⬆️
# ============================================================

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
        # Заявки на публикацию
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                from_chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                target_group_id INTEGER NOT NULL,
                is_anonymous INTEGER DEFAULT 0,
                username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Статистика
        await db.execute("""
            CREATE TABLE IF NOT EXISTS statistics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                action TEXT NOT NULL,
                group_id INTEGER,
                is_anonymous INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

# ======================== РАБОТА С ГРУППАМИ ========================
def get_group_name(group_id: int) -> str:
    """Возвращает название группы по её ID."""
    for gid, name in GROUPS:
        if gid == group_id:
            return name
    return "Неизвестная группа"

def get_groups_keyboard() -> InlineKeyboardMarkup:
    """Создаёт клавиатуру со списком групп."""
    keyboard = []
    for group_id, name in GROUPS:
        keyboard.append([
            InlineKeyboardButton(
                text=name, 
                callback_data=f"group_{group_id}"
            )
        ])
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_post")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ======================== РАБОТА С БД ========================
async def add_pending_post(user_id, from_chat_id, message_id, target_group_id, is_anonymous, username) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO pending_posts (user_id, from_chat_id, message_id, target_group_id, is_anonymous, username) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, from_chat_id, message_id, target_group_id, int(is_anonymous), username)
        )
        await db.commit()
        return cursor.lastrowid

async def get_pending_post(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM pending_posts WHERE id = ?", (post_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def delete_pending_post(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pending_posts WHERE id = ?", (post_id,))
        await db.commit()

async def add_stat(user_id, username, action, group_id, is_anonymous):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO statistics (user_id, username, action, group_id, is_anonymous) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, action, group_id, int(is_anonymous))
        )
        await db.commit()

# ======================== СОСТОЯНИЯ ========================
class PostStates(StatesGroup):
    waiting_for_post = State()
    selecting_group = State()
    reviewing_post = State()

class SupportStates(StatesGroup):
    waiting_for_message = State()

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
        "👋 Привет! Это бот для предложений в наши группы.\n\n"
        "Нажмите кнопку ниже, чтобы предложить пост или написать в поддержку.",
        reply_markup=get_main_kb()
    )

# ======================== СОЗДАНИЕ ПОСТА ========================
@router_user.callback_query(F.data == "create_post")
async def start_creating_post(callback: CallbackQuery, state: FSMContext):
    if not GROUPS:
        await callback.answer("❌ Нет доступных групп для публикации", show_alert=True)
        return
    
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
    await state.update_data(
        post_message_id=message.message_id,
        post_chat_id=message.chat.id
    )
    await state.set_state(PostStates.selecting_group)
    
    await message.answer(
        "📢 <b>Выберите группу</b>, куда хотите отправить пост:",
        reply_markup=get_groups_keyboard(),
        parse_mode=ParseMode.HTML
    )

# ======================== ВЫБОР ГРУППЫ ========================
@router_user.callback_query(PostStates.selecting_group, F.data.startswith("group_"))
async def select_group(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_", 1)[1])
    group_name = get_group_name(group_id)
    
    # Проверяем, что группа есть в списке
    if group_name == "Неизвестная группа":
        await callback.answer("❌ Группа не найдена", show_alert=True)
        return
    
    await state.update_data(target_group_id=group_id)
    await state.set_state(PostStates.reviewing_post)
    
    data = await state.get_data()
    is_anon = data.get("is_anonymous", False)
    
    await callback.message.answer("👁 Предпросмотр вашего поста:")
    await callback.message.copy_to(callback.message.chat.id)
    
    await callback.message.answer(
        f"📢 Вы выбрали группу: <b>{group_name}</b>\n\n"
        "Выберите действие с постом:",
        reply_markup=get_post_review_kb(is_anon),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router_user.callback_query(PostStates.selecting_group, F.data == "cancel_post")
async def cancel_post(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ Публикация отменена.",
        reply_markup=get_main_kb()
    )
    await callback.answer()

# ======================== ДЕЙСТВИЯ С ПОСТОМ ========================
@router_user.callback_query(PostStates.reviewing_post, F.data == "edit_post")
async def edit_post(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PostStates.waiting_for_post)
    await callback.message.edit_text("✏️ Отправьте новый вариант поста.")
    await callback.answer()

@router_user.callback_query(PostStates.reviewing_post, F.data == "delete_post")
async def delete_post(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🗑 Пост удалён.", reply_markup=get_main_kb())
    await callback.answer()

@router_user.callback_query(PostStates.reviewing_post, F.data == "toggle_anon")
async def toggle_anonymity(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    new_state = not data.get("is_anonymous", False)
    await state.update_data(is_anonymous=new_state)
    
    status = "🔒 Анонимный режим включён." if new_state else "👤 Режим с именем."
    
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
    target_group_id = data.get("target_group_id")
    
    username = f"@{user.username}" if user.username else user.full_name
    group_name = get_group_name(target_group_id)
    
    post_id = await add_pending_post(
        user_id=user.id,
        from_chat_id=data["post_chat_id"],
        message_id=data["post_message_id"],
        target_group_id=target_group_id,
        is_anonymous=is_anon,
        username=username
    )
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=data["post_chat_id"],
                message_id=data["post_message_id"]
            )
            
            author_info = "👤 Автор: <i>скрыт (аноним)</i>" if is_anon \
                else f"👤 Автор: {username} (ID: <code>{user.id}</code>)"
            
            await bot.send_message(
                admin_id,
                f"📬 <b>Новая заявка на публикацию</b>\n"
                f"{author_info}\n"
                f"📢 Группа: <b>{group_name}</b>\n"
                f"🆔 Заявка #{post_id}",
                parse_mode=ParseMode.HTML,
                reply_markup=get_admin_approval_kb(post_id)
            )
        except Exception as e:
            logging.error(f"Ошибка отправки админу {admin_id}: {e}")
    
    await state.clear()
    await callback.message.edit_text(
        "✅ Заявка отправлена на модерацию!\nМы уведомим вас о решении.",
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
    
    try:
        await bot.copy_message(
            chat_id=post["target_group_id"],
            from_chat_id=post["from_chat_id"],
            message_id=post["message_id"]
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка публикации в группу: {e}")
        await callback.answer()
        return
    
    await add_stat(post["user_id"], post["username"], "approved", post["target_group_id"], post["is_anonymous"])
    await delete_pending_post(post_id)
    
    await callback.message.edit_text(
        f"✅ Заявка #{post_id} <b>принята</b> и опубликована в группе <b>{get_group_name(post['target_group_id'])}</b>.",
        parse_mode=ParseMode.HTML
    )
    await callback.answer("Опубликовано!")
    
    try:
        await bot.send_message(
            post["user_id"],
            f"🎉 <b>Ваш пост был опубликован в группе «{get_group_name(post['target_group_id'])}»!</b>",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

@router_admin.callback_query(F.data.startswith("reject_"))
async def reject_post(callback: CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    post = await get_pending_post(post_id)
    
    if not post:
        await callback.answer("⚠️ Заявка не найдена (уже обработана).", show_alert=True)
        return
    
    await add_stat(post["user_id"], post["username"], "rejected", post["target_group_id"], post["is_anonymous"])
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
    
    if len(args) > 1 and args[1].lower() == "groups":
        # Статистика по группам
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT group_id, COUNT(*) as cnt FROM statistics "
                "WHERE action = 'approved' AND group_id IS NOT NULL "
                "GROUP BY group_id ORDER BY cnt DESC"
            )
            rows = await cursor.fetchall()
        
        if not rows:
            await message.answer("📊 Пока нет опубликованных постов.")
            return
        
        lines = ["📢 <b>Статистика по группам:</b>\n"]
        for group_id, count in rows:
            lines.append(f"• {get_group_name(group_id)} — <b>{count}</b> постов")
        
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    
    elif len(args) > 1 and args[1].lower() == "top":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT username, COUNT(*) as cnt FROM statistics "
                "WHERE action = 'approved' AND username IS NOT NULL "
                "GROUP BY user_id ORDER BY cnt DESC LIMIT 10"
            )
            top = await cursor.fetchall()
        
        if not top:
            await message.answer("📊 Пока нет опубликованных постов.")
            return
        
        lines = ["🏆 <b>Топ авторов:</b>\n"]
        for i, (username, count) in enumerate(top, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            lines.append(f"{medal} {username} — <b>{count}</b> постов")
        
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT action, COUNT(*) as cnt FROM statistics GROUP BY action"
            )
            rows = await cursor.fetchall()
        
        stats = {"approved": 0, "rejected": 0}
        for row in rows:
            stats[row[0]] = row[1]
        stats["total"] = stats["approved"] + stats["rejected"]
        
        await message.answer(
            "📊 <b>Статистика предложки:</b>\n\n"
            f"✅ Принято: <b>{stats['approved']}</b>\n"
            f"❌ Отклонено: <b>{stats['rejected']}</b>\n"
            f"📋 Всего: <b>{stats['total']}</b>\n\n"
            f"💡 Команды:\n"
            f"/stats top — топ авторов\n"
            f"/stats groups — по группам",
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
