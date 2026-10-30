import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    LabeledPrice, PreCheckoutQuery, BotCommand
)
import aiosqlite

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не знайдено")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

DB_PATH = "anon_chat.db"
queue = set()
PRICE_FILTER = 50  # за пошук по фільтрах або за день

LINK_RE = re.compile(r"https?://|t\.me/|telegram\.me/", re.I)

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
                filter_until TEXT,
                banned INTEGER DEFAULT 0,
                reports INTEGER DEFAULT 0,
                age_confirmed INTEGER DEFAULT 0
            )
        """)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN age_confirmed INTEGER DEFAULT 0")
        except:
            pass
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as c:
            return await c.fetchone()

async def is_banned(user_id: int) -> bool:
    u = await get_user(user_id)
    return bool(u and u["banned"] == 1)

async def confirm_age(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET age_confirmed = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

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
            INSERT OR REPLACE INTO users (user_id, gender, looking_for, bio, referred_by, age_confirmed)
            VALUES (?, ?, ?, ?, ?, 1)
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

async def use_free_search(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET free_searches = MAX(COALESCE(free_searches,0) - 1, 0) WHERE user_id = ?", (user_id,))
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET in_chat_with = ?, is_searching = 0 WHERE user_id = ?", (partner_id, user_id))
        await db.commit()

async def clear_partner(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET in_chat_with = 0, is_searching = 0 WHERE user_id = ?", (user_id,))
        await db.commit()

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
    [KeyboardButton(text="🔍 Пошук"), KeyboardButton(text="🧭 Фільтри пошуку")],
    [KeyboardButton(text="❤️ Флірт пошук"), KeyboardButton(text="👤 Онлайн")],
    [KeyboardButton(text="📚 Пошук за інтересами")],
    [KeyboardButton(text="👑 Premium"), KeyboardButton(text="📣 Реферал")],
    [KeyboardButton(text="⚙️ Додатково"), KeyboardButton(text="❌ Стоп")]
], resize_keyboard=True)

stop_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="❌ Стоп"), KeyboardButton(text="⚠️ Скарга"), KeyboardButton(text="🚫 Заблокувати")]
], resize_keyboard=True)

cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)

def is_opposite(a, b):
    return (a == "Чоловік" and b == "Жінка") or (a == "Жінка" and b == "Чоловік")

WELCOME = (
    "Анонімний чат 18+\n\n"
    "🎲 Рандом — безкоштовно\n"
    "🎯 Пошук за фільтрами (стать + місто) — платно\n\n"
    "/start — почати\n"
    "/next — рандом\n"
    "/stop — зупинити"
)

@router.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandObject = None):
    if await is_banned(message.from_user.id):
        return await message.answer("Ти заблокований.")
    await state.clear()
    referred_by = 0
    if command and command.args and str(command.args).isdigit():
        referred_by = int(command.args)

    user = await get_user(message.from_user.id)
    if not user or not user["age_confirmed"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Мені є 18+", callback_data="age18"),
            InlineKeyboardButton(text="Мені немає 18", callback_data="age_no")
        ]])
        await message.answer(WELCOME + "\n\nПідтверди вік:", reply_markup=kb)
        await state.update_data(referred_by=referred_by)
    else:
        await message.answer("З поверненням.", reply_markup=main_kb)

@router.callback_query(F.data == "age_no")
async def age_no(cb: CallbackQuery):
    await cb.message.edit_text("Вибач, бот тільки для 18+.")
    await cb.answer()

@router.callback_query(F.data == "age18")
async def age_ok(cb: CallbackQuery, state: FSMContext):
    await confirm_age(cb.from_user.id)
    await cb.message.edit_text("Вік підтверджено.")
    await cb.message.answer("Твоя стать?", reply_markup=gender_kb)
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
    await m.answer("Короткий опис (або '-'):")
    await state.set_state(Profile.bio)

@router.message(Profile.bio)
async def set_bio(m: Message, state: FSMContext):
    data = await state.get_data()
    bio = "" if m.text.strip() == "-" else m.text
    referred_by = data.get("referred_by", 0)
    await save_user(m.from_user.id, data["gender"], data["looking"], bio, referred_by)
    await update_extra(m.from_user.id, age=data.get("age"), city=data.get("city"))
    if referred_by and referred_by != m.from_user.id:
        await add_referral(referred_by)
    await state.clear()
    await m.answer("Готово.", reply_markup=main_kb)

@router.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer("🎲 Рандом — безкоштовно\n🎯 Фільтри (стать+місто) — платно\n/stop — закінчити")

# --- Рандом (безкоштовно) ---
@router.message(Command("next"))
@router.message(F.text.in_({"🎲 Рандом", "🔍 Пошук"}))
async def find_random(m: Message, state: FSMContext):
    await do_search(m, state, use_filter=False)

# --- Пошук за фільтрами (платно) ---
@router.message(F.text.in_({"🎯 Пошук за фільтрами", "🧭 Фільтри пошуку", "❤️ Флірт пошук"}))
async def find_filter(m: Message, state: FSMContext):
    user = await get_user(m.from_user.id)
    if not user:
        return await m.answer("/start")
    if not has_active_filter(user) and (user["free_searches"] or 0) <= 0:
        return await m.answer(
            "Пошук за фільтрами (стать + місто) платний.\n"
            "Купи доступ або запроси друзів.",
            reply_markup=main_kb
        )
    await do_search(m, state, use_filter=True)

async def do_search(m: Message, state: FSMContext, use_filter: bool):
    if await is_banned(m.from_user.id):
        return await m.answer("Заблоковано.")
    uid = m.from_user.id
    user = await get_user(uid)
    if not user:
        return await m.answer("/start")
    if user["in_chat_with"]:
        return await m.answer("Ти вже в чаті. Спочатку /stop")

    used_free = False
    partner = None

    for pid in list(queue):
        if pid == uid:
            continue
        p = await get_user(pid)
        if not p or p["in_chat_with"] or p["banned"]:
            continue

        if use_filter:
            # стать + місто
            gender_ok = is_opposite(user["gender"], p["gender"]) or user["looking_for"] == "Будь-кого"
            city_ok = not user["city"] or not p["city"] or user["city"].lower() == p["city"].lower()
            if gender_ok and city_ok:
                partner = pid
                if not has_active_filter(user):
                    used_free = True
                break
        else:
            # чистий рандом
            partner = pid
            break

    if partner:
        if used_free:
            await use_free_search(uid)
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

@router.message(F.text == "❌ Скасувати")
async def cancel(m: Message):
    queue.discard(m.from_user.id)
    await m.answer("Скасовано.", reply_markup=main_kb)

@router.message(Command("stop"))
@router.message(F.text == "❌ Стоп")
async def end(m: Message, state: FSMContext):
    user = await get_user(m.from_user.id)
    if not user or not user["in_chat_with"]:
        queue.discard(m.from_user.id)
        return await m.answer("Ти не в чаті.", reply_markup=main_kb)
    partner = user["in_chat_with"]
    await clear_partner(m.from_user.id)
    await clear_partner(partner)
    await m.answer("Чат закінчено.", reply_markup=main_kb)
    try:
        await bot.send_message(partner, "Співрозмовник вийшов.", reply_markup=main_kb)
    except:
        pass
    await state.clear()

@router.message(Command("report"))
@router.message(F.text == "⚠️ Скарга")
async def report(m: Message):
    user = await get_user(m.from_user.id)
    if not user or not user["in_chat_with"]:
        return await m.answer("Немає активного чату.")
    partner = user["in_chat_with"]
    r = await add_report(partner)
    await m.answer(f"Скаргу надіслано ({r}/3)")

@router.message(F.text == "🚫 Заблокувати")
async def block(m: Message, state: FSMContext):
    user = await get_user(m.from_user.id)
    if not user or not user["in_chat_with"]:
        return
    partner = user["in_chat_with"]
    await add_report(partner)
    await clear_partner(m.from_user.id)
    await clear_partner(partner)
    await m.answer("Заблоковано.", reply_markup=main_kb)
    await state.clear()

@router.message(F.text == "👤 Профіль")
async def profile(m: Message):
    u = await get_user(m.from_user.id)
    if not u:
        return await m.answer("/start")
    active = "активний" if has_active_filter(u) else "немає"
    await m.answer(
        f"Стать: {u['gender']}\nШукаю: {u['looking_for']}\nМісто: {u['city'] or '—'}\n"
        f"Фільтр: {active}\nБезкоштовних пошуків: {u['free_searches'] or 0}",
        reply_markup=main_kb
    )

@router.message(F.text.in_({"🔗 Рефералка", "📣 Реферал"}))
async def ref(m: Message):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={m.from_user.id}"
    await m.answer(f"Посилання:\n{link}\n\nКожен реферал = +1 безкоштовний пошук за фільтрами.", reply_markup=main_kb)

@router.message(F.text.in_({"⭐ Купити фільтр (50 Stars)", "👑 Premium"}))
async def buy_filter(m: Message):
    await bot.send_invoice(
        m.chat.id, "Фільтр на 1 день",
        "Пошук за статтю і містом протягом 24 годин",
        f"filter_{m.from_user.id}", "", "XTR",
        [LabeledPrice("1 день", PRICE_FILTER)]
    )

@router.pre_checkout_query()
async def pre(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def paid(m: Message):
    await set_filter(m.from_user.id, 1)
    await m.answer("Фільтр активовано на 24 години!", reply_markup=main_kb)

@router.message()
async def relay(m: Message, state: FSMContext):
    if m.text and (m.text.startswith("/") or m.text in [
        "🔍 Пошук", "🧭 Фільтри пошуку", "❤️ Флірт пошук", "👤 Онлайн", "📚 Пошук за інтересами", "👑 Premium", "📣 Реферал", "⚙️ Додатково", "👤 Профіль", "🔗 Рефералка",
        "⭐ Купити фільтр (50 Stars)", "❌ Стоп", "❌ Скасувати",
        "⚠️ Скарга", "🚫 Заблокувати"
    ]):
        return

    user = await get_user(m.from_user.id)
    if not user or not user["in_chat_with"]:
        return

    if m.text and LINK_RE.search(m.text):
        return await m.answer("Посилання заборонені.")

    partner = user["in_chat_with"]
    try:
        if m.text:
            await bot.send_message(partner, m.text)
        elif m.photo:
            await bot.send_photo(partner, m.photo[-1].file_id, caption=m.caption)
        elif m.video:
            await bot.send_video(partner, m.video.file_id, caption=m.caption)
        elif m.voice:
            await bot.send_voice(partner, m.voice.file_id)
        elif m.sticker:
            await bot.send_sticker(partner, m.sticker.file_id)
    except Exception as e:
        logger.error(f"Relay: {e}")
        await m.answer("Не доставлено.")

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Почати"),
        BotCommand(command="next", description="Рандомний пошук"),
        BotCommand(command="stop", description="Зупинити"),
        BotCommand(command="report", description="Скарга"),
        BotCommand(command="help", description="Допомога"),
    ])

async def main():
    await init_db()
    await set_commands()
    dp.include_router(router)
    logger.info("Бот з рандомом і фільтрами запущено")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
