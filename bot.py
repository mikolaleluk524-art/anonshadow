import asyncio
import logging
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    LabeledPrice, PreCheckoutQuery
)
import aiosqlite

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не знайдено в змінних середовища")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

DB_PATH = "anon_chat.db"
queue = set()
PRICE_DAY = 50
PRICE_WEEK = 250
FREE_SEARCHES_PER_DAY = 3

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                gender TEXT,
                looking_for TEXT,
                bio TEXT,
                photo_id TEXT,
                age INTEGER,
                city TEXT,
                in_chat_with INTEGER DEFAULT 0,
                is_searching INTEGER DEFAULT 0,
                referred_by INTEGER DEFAULT 0,
                referrals_count INTEGER DEFAULT 0,
                free_searches INTEGER DEFAULT 0,
                daily_searches INTEGER DEFAULT 0,
                last_search_date TEXT,
                filter_until TEXT,
                banned INTEGER DEFAULT 0,
                reports INTEGER DEFAULT 0
            )
        """)
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as c:
            return await c.fetchone()

async def is_banned(user_id: int) -> bool:
    u = await get_user(user_id)
    return bool(u and u["banned"] == 1)

async def add_report(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET reports = COALESCE(reports,0) + 1 WHERE user_id = ?", (user_id,))
        async with db.execute("SELECT reports FROM users WHERE user_id = ?", (user_id,)) as c:
            r = (await c.fetchone())[0] or 0
        if r >= 3:
            await db.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
        await db.commit()
        return r

async def save_user(user_id, gender, looking_for, bio, referred_by=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO users (user_id, gender, looking_for, bio, referred_by)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, gender, looking_for, bio, referred_by))
        await db.commit()

async def update_extra(user_id, **kwargs):
    if not kwargs:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [user_id]
        await db.execute(f"UPDATE users SET {sets} WHERE user_id = ?", vals)
        await db.commit()

async def add_referral(referrer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET 
                referrals_count = COALESCE(referrals_count,0) + 1,
                free_searches = COALESCE(free_searches,0) + 1
            WHERE user_id = ?
        """, (referrer_id,))
        await db.commit()
        async with db.execute("SELECT referrals_count, free_searches FROM users WHERE user_id = ?", (referrer_id,)) as c:
            return await c.fetchone()

async def use_free_search(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET free_searches = MAX(COALESCE(free_searches,0) - 1, 0) WHERE user_id = ?", (user_id,))
        await db.commit()

async def increment_daily(user_id):
    today = datetime.utcnow().date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        u = await get_user(user_id)
        if not u or u["last_search_date"] != today:
            await db.execute("UPDATE users SET daily_searches = 1, last_search_date = ? WHERE user_id = ?", (today, user_id))
        else:
            await db.execute("UPDATE users SET daily_searches = COALESCE(daily_searches,0) + 1 WHERE user_id = ?", (user_id,))
        await db.commit()

def has_active_filter(user) -> bool:
    if not user or not user["filter_until"]:
        return False
    try:
        return datetime.utcnow() < datetime.fromisoformat(user["filter_until"])
    except:
        return False

async def set_filter(user_id, days=1):
    until = (datetime.utcnow() + timedelta(days=days)).isoformat()
    await update_extra(user_id, filter_until=until)

async def set_partner(user_id, partner_id):
    await update_extra(user_id, in_chat_with=partner_id, is_searching=0)

async def clear_partner(user_id):
    await update_extra(user_id, in_chat_with=0, is_searching=0)

class Profile(StatesGroup):
    gender = State()
    looking = State()
    age = State()
    city = State()
    bio = State()

class InChat(StatesGroup):
    active = State()

gender_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Чоловік"), KeyboardButton(text="Жінка")], [KeyboardButton(text="Інше")]], resize_keyboard=True)
looking_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Чоловіка"), KeyboardButton(text="Жінку")], [KeyboardButton(text="Будь-кого")]], resize_keyboard=True)
main_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🔍 Знайти співрозмовника")],
    [KeyboardButton(text="👤 Профіль"), KeyboardButton(text="🔗 Рефералка")],
    [KeyboardButton(text="⭐ Фільтр 1 день (50)"), KeyboardButton(text="⭐ Фільтр 7 днів (250)")],
    [KeyboardButton(text="❌ Закінчити чат")]
], resize_keyboard=True)
stop_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Закінчити чат"), KeyboardButton(text="⚠️ Поскаржитись")]], resize_keyboard=True)
cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати пошук")]], resize_keyboard=True)

def is_opposite(a, b):
    return (a == "Чоловік" and b == "Жінка") or (a == "Жінка" and b == "Чоловік")

@router.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandObject):
    if await is_banned(message.from_user.id):
        return await message.answer("Ти заблокований.")
    await state.clear()
    referred_by = int(command.args) if command.args and command.args.isdigit() else 0
    user = await get_user(message.from_user.id)

    if not user:
        await message.answer(
            "AnonShadow — анонімний чат 18+\n\nПідтверди вік:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Мені є 18+", callback_data="age18")
            ]])
        )
        await state.update_data(referred_by=referred_by)
    else:
        await message.answer("З поверненням.", reply_markup=main_kb)

@router.callback_query(F.data == "age18")
async def age_ok(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Ок. Твоя стать?")
    await cb.message.answer("Обери:", reply_markup=gender_kb)
    await state.set_state(Profile.gender)
    await cb.answer()

@router.message(Profile.gender)
async def set_gender(m: Message, state: FSMContext):
    if m.text not in ["Чоловік", "Жінка", "Інше"]:
        return
    await state.update_data(gender=m.text)
    await m.answer("Кого шукаєш?", reply_markup=looking_kb)
    await state.set_state(Profile.looking)

@router.message(Profile.looking)
async def set_looking(m: Message, state: FSMContext):
    if m.text not in ["Чоловіка", "Жінку", "Будь-кого"]:
        return
    await state.update_data(looking=m.text)
    await m.answer("Вік (число):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Profile.age)

@router.message(Profile.age)
async def set_age(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Введи число")
    await state.update_data(age=int(m.text))
    await m.answer("Місто (або '-'):")
    await state.set_state(Profile.city)

@router.message(Profile.city)
async def set_city(m: Message, state: FSMContext):
    await state.update_data(city="" if m.text == "-" else m.text)
    await m.answer("Опис / фетіші (або '-'):")
    await state.set_state(Profile.bio)

@router.message(Profile.bio)
async def set_bio(m: Message, state: FSMContext):
    data = await state.get_data()
    bio = "" if m.text.strip() == "-" else m.text
    referred_by = data.get("referred_by", 0)
    await save_user(m.from_user.id, data["gender"], data["looking"], bio, referred_by)
    await update_extra(m.from_user.id, age=data.get("age"), city=data.get("city"))
    if referred_by and referred_by != m.from_user.id:
        try:
            refs, free = await add_referral(referred_by)
            await bot.send_message(referred_by, f"Новий реферал! Всього: {refs}, безкоштовних: {free}")
        except:
            pass
    await state.clear()
    await m.answer("Профіль готовий.", reply_markup=main_kb)

@router.message(F.text == "👤 Профіль")
async def profile(m: Message):
    u = await get_user(m.from_user.id)
    if not u:
        return await m.answer("/start")
    active = "активний" if has_active_filter(u) else "немає"
    text = (f"Стать: {u['gender']}\nШукаю: {u['looking_for']}\nВік: {u['age'] or '—'}\n"
            f"Місто: {u['city'] or '—'}\nОпис: {u['bio'] or '—'}\n"
            f"Фільтр: {active}\nРефералів: {u['referrals_count'] or 0}\n"
            f"Безкоштовних пошуків: {u['free_searches'] or 0}")
    await m.answer(text, reply_markup=main_kb)

@router.message(F.photo)
async def set_photo(m: Message):
    u = await get_user(m.from_user.id)
    if u:
        await update_extra(m.from_user.id, photo_id=m.photo[-1].file_id)
        await m.answer("Фото оновлено.", reply_markup=main_kb)

@router.message(F.text == "🔗 Рефералка")
async def ref(m: Message):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={m.from_user.id}"
    u = await get_user(m.from_user.id)
    await m.answer(
        f"Посилання:\n{link}\n\n"
        f"Рефералів: {u['referrals_count'] if u else 0}\n"
        f"Безкоштовних пошуків: {u['free_searches'] if u else 0}",
        reply_markup=main_kb
    )

@router.message(F.text == "⭐ Фільтр 1 день (50)")
async def buy_day(m: Message):
    await bot.send_invoice(m.chat.id, "Фільтр 1 день", "24 години пошуку протилежної статі",
                           f"day_{m.from_user.id}", "", "XTR", [LabeledPrice("1 день", PRICE_DAY)])

@router.message(F.text == "⭐ Фільтр 7 днів (250)")
async def buy_week(m: Message):
    await bot.send_invoice(m.chat.id, "Фільтр 7 днів", "Тиждень пошуку протилежної статі",
                           f"week_{m.from_user.id}", "", "XTR", [LabeledPrice("7 днів", PRICE_WEEK)])

@router.pre_checkout_query()
async def pre(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def paid(m: Message):
    payload = m.successful_payment.invoice_payload
    days = 7 if "week_" in payload else 1
    await set_filter(m.from_user.id, days)
    await m.answer(f"Фільтр активовано на {days} дн.", reply_markup=main_kb)

@router.message(F.text == "🔍 Знайти співрозмовника")
async def find(m: Message, state: FSMContext):
    if await is_banned(m.from_user.id):
        return await m.answer("Заблоковано.")
    uid = m.from_user.id
    user = await get_user(uid)
    if not user or user["in_chat_with"]:
        return await m.answer("Спочатку /start або закінчи чат.")

    today = datetime.utcnow().date().isoformat()
    daily = user["daily_searches"] or 0 if user["last_search_date"] == today else 0
    can_free = daily < FREE_SEARCHES_PER_DAY
    can_opp = has_active_filter(user) or (user["free_searches"] or 0) > 0

    if not can_free and not can_opp:
        return await m.answer("Ліміт безкоштовних пошуків вичерпано. Купи фільтр або запроси друзів.", reply_markup=main_kb)

    partner = None
    used_free = False
    for pid in list(queue):
        if pid == uid:
            continue
        p = await get_user(pid)
        if not p or p["in_chat_with"] or p["banned"]:
            continue
        if can_opp and is_opposite(user["gender"], p["gender"]):
            partner = pid
            if not has_active_filter(user):
                used_free = True
            break
        elif can_free:
            partner = pid
            break

    if partner:
        if used_free:
            await use_free_search(uid)
        await increment_daily(uid)
        queue.discard(partner)
        queue.discard(uid)
        await set_partner(uid, partner)
        await set_partner(partner, uid)
        await m.answer("Знайдено! Пиши.", reply_markup=stop_kb)
        await bot.send_message(partner, "Знайдено! Пиши.", reply_markup=stop_kb)
        await state.set_state(InChat.active)
    else:
        queue.add(uid)
        await m.answer("Шукаю...", reply_markup=cancel_kb)

@router.message(F.text == "❌ Скасувати пошук")
async def cancel(m: Message):
    queue.discard(m.from_user.id)
    await m.answer("Скасовано.", reply_markup=main_kb)

@router.message(F.text == "❌ Закінчити чат")
async def end(m: Message, state: FSMContext):
    user = await get_user(m.from_user.id)
    if not user or not user["in_chat_with"]:
        return await m.answer("Не в чаті.", reply_markup=main_kb)
    partner = user["in_chat_with"]
    await clear_partner(m.from_user.id)
    await clear_partner(partner)
    await m.answer("Чат закінчено.", reply_markup=main_kb)
    try:
        await bot.send_message(partner, "Співрозмовник вийшов.", reply_markup=main_kb)
    except:
        pass
    await state.clear()

@router.message(F.text == "⚠️ Поскаржитись")
async def report(m: Message):
    user = await get_user(m.from_user.id)
    if not user or not user["in_chat_with"]:
        return
    partner = user["in_chat_with"]
    r = await add_report(partner)
    await m.answer(f"Скаргу надіслано ({r})")
    if r >= 3:
        await clear_partner(partner)

@router.message(InChat.active)
async def relay(m: Message, state: FSMContext):
    user = await get_user(m.from_user.id)
    if not user or not user["in_chat_with"]:
        await state.clear()
        return await m.answer("Чат закінчився.", reply_markup=main_kb)
    partner = user["in_chat_with"]
    try:
        if m.text and not m.text.startswith(("❌", "⚠️")):
            await bot.send_message(partner, m.text)
        elif m.photo:
            await bot.send_photo(partner, m.photo[-1].file_id, caption=m.caption)
        elif m.video:
            await bot.send_video(partner, m.video.file_id, caption=m.caption)
        elif m.voice:
            await bot.send_voice(partner, m.voice.file_id)
        elif m.sticker:
            await bot.send_sticker(partner, m.sticker.file_id)
    except:
        await m.answer("Не доставлено.")

async def main():
    await init_db()
    dp.include_router(router)
    logger.info("Бот запущено")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
