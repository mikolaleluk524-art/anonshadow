import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    LabeledPrice, PreCheckoutQuery, FSInputFile
)
import aiosqlite

from config import BOT_TOKEN, ADMIN_ID

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
        # додаємо відсутні колонки
        cols = [
            ("photo_id", "TEXT"), ("age", "INTEGER"), ("city", "TEXT"),
            ("referred_by", "INTEGER DEFAULT 0"), ("referrals_count", "INTEGER DEFAULT 0"),
            ("free_searches", "INTEGER DEFAULT 0"), ("daily_searches", "INTEGER DEFAULT 0"),
            ("last_search_date", "TEXT"), ("filter_until", "TEXT"),
            ("banned", "INTEGER DEFAULT 0"), ("reports", "INTEGER DEFAULT 0")
        ]
        for name, typ in cols:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {name} {typ}")
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
    return u and u["banned"] == 1

async def add_report(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET reports = reports + 1 WHERE user_id = ?", (user_id,))
        async with db.execute("SELECT reports FROM users WHERE user_id = ?", (user_id,)) as c:
            r = (await c.fetchone())[0]
        if r >= 3:
            await db.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
        await db.commit()
        return r

async def save_user(user_id, gender, looking_for, bio, referred_by=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO users 
            (user_id, gender, looking_for, bio, referred_by, referrals_count, free_searches, daily_searches, last_search_date, filter_until, banned, reports)
            VALUES (?, ?, ?, ?, ?, 
                    COALESCE((SELECT referrals_count FROM users WHERE user_id=?),0),
                    COALESCE((SELECT free_searches FROM users WHERE user_id=?),0),
                    COALESCE((SELECT daily_searches FROM users WHERE user_id=?),0),
                    COALESCE((SELECT last_search_date FROM users WHERE user_id=?),NULL),
                    COALESCE((SELECT filter_until FROM users WHERE user_id=?),NULL),
                    COALESCE((SELECT banned FROM users WHERE user_id=?),0),
                    COALESCE((SELECT reports FROM users WHERE user_id=?),0))
        """, (user_id, gender, looking_for, bio, referred_by, user_id, user_id, user_id, user_id, user_id, user_id, user_id))
        await db.commit()

async def update_profile_extra(user_id, photo_id=None, age=None, city=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if photo_id:
            await db.execute("UPDATE users SET photo_id = ? WHERE user_id = ?", (photo_id, user_id))
        if age:
            await db.execute("UPDATE users SET age = ? WHERE user_id = ?", (age, user_id))
        if city:
            await db.execute("UPDATE users SET city = ? WHERE user_id = ?", (city, user_id))
        await db.commit()

async def add_referral(referrer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET referrals_count = referrals_count + 1, free_searches = free_searches + 1 WHERE user_id = ?", (referrer_id,))
        await db.commit()
        async with db.execute("SELECT referrals_count, free_searches FROM users WHERE user_id = ?", (referrer_id,)) as c:
            return await c.fetchone()

async def use_free_search(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET free_searches = MAX(free_searches - 1, 0) WHERE user_id = ?", (user_id,))
        await db.commit()

async def increment_daily(user_id):
    today = datetime.utcnow().date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        u = await get_user(user_id)
        if u and u["last_search_date"] != today:
            await db.execute("UPDATE users SET daily_searches = 1, last_search_date = ? WHERE user_id = ?", (today, user_id))
        else:
            await db.execute("UPDATE users SET daily_searches = daily_searches + 1 WHERE user_id = ?", (user_id,))
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET filter_until = ? WHERE user_id = ?", (until, user_id))
        await db.commit()

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
    bio = State()
    age = State()
    city = State()
    photo = State()

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
    try:
        photo = FSInputFile("start.jpg")
    except:
        photo = None

    if not user:
        text = "AnonShadow — анонімний чат 18+\nПідтверди вік:"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Мені є 18+", callback_data="age18")]])
        if photo:
            await message.answer_photo(photo, caption=text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)
        await state.update_data(referred_by=referred_by)
    else:
        if photo:
            await message.answer_photo(photo, caption="З поверненням.", reply_markup=main_kb)
        else:
            await message.answer("З поверненням.", reply_markup=main_kb)

@router.callback_query(F.data == "age18")
async def age_ok(cb: CallbackQuery, state: FSMContext):
    try: await cb.message.delete()
    except: pass
    await cb.message.answer("Стать?", reply_markup=gender_kb)
    await state.set_state(Profile.gender)
    await cb.answer()

@router.message(Profile.gender)
async def set_gender(m: Message, state: FSMContext):
    if m.text not in ["Чоловік", "Жінка", "Інше"]: return
    await state.update_data(gender=m.text)
    await m.answer("Кого шукаєш?", reply_markup=looking_kb)
    await state.set_state(Profile.looking)

@router.message(Profile.looking)
async def set_looking(m: Message, state: FSMContext):
    if m.text not in ["Чоловіка", "Жінку", "Будь-кого"]: return
    await state.update_data(looking=m.text)
    await m.answer("Вік (число):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Profile.age)

@router.message(Profile.age)
async def set_age(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введи число")
    await state.update_data(age=int(m.text))
    await m.answer("Місто (або '-'):")
    await state.set_state(Profile.city)

@router.message(Profile.city)
async def set_city(m: Message, state: FSMContext):
    await state.update_data(city=m.text if m.text != "-" else "")
    await m.answer("Короткий опис / фетіші (або '-'):")
    await state.set_state(Profile.bio)

@router.message(Profile.bio)
async def set_bio(m: Message, state: FSMContext):
    data = await state.get_data()
    bio = "" if m.text.strip() == "-" else m.text
    referred_by = data.get("referred_by", 0)
    await save_user(m.from_user.id, data["gender"], data["looking"], bio, referred_by)
    await update_profile_extra(m.from_user.id, age=data.get("age"), city=data.get("city"))
    if referred_by and referred_by != m.from_user.id:
        refs, free = await add_referral(referred_by)
        try:
            await bot.send_message(referred_by, f"Новий реферал! Всього: {refs}, безкоштовних пошуків: {free}")
        except: pass
    await state.clear()
    await m.answer("Профіль створено. Можеш додати фото в профілі.", reply_markup=main_kb)

@router.message(F.text == "👤 Профіль")
async def profile(m: Message):
    u = await get_user(m.from_user.id)
    if not u: return await m.answer("/start")
    active = "активний" if has_active_filter(u) else "немає"
    text = (f"Стать: {u['gender']}\nШукаю: {u['looking_for']}\nВік: {u['age'] or '—'}\n"
            f"Місто: {u['city'] or '—'}\nОпис: {u['bio'] or '—'}\n"
            f"Фільтр: {active}\nРефералів: {u['referrals_count']}\n"
            f"Безкоштовних пошуків: {u['free_searches']}")
    if u["photo_id"]:
        await m.answer_photo(u["photo_id"], caption=text, reply_markup=main_kb)
    else:
        await m.answer(text + "\n\nНадішли фото, щоб додати в профіль.", reply_markup=main_kb)

@router.message(F.photo)
async def set_photo(m: Message):
    u = await get_user(m.from_user.id)
    if not u: return
    await update_profile_extra(m.from_user.id, photo_id=m.photo[-1].file_id)
    await m.answer("Фото профілю оновлено.", reply_markup=main_kb)

@router.message(F.text == "🔗 Рефералка")
async def ref(m: Message):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={m.from_user.id}"
    u = await get_user(m.from_user.id)
    await m.answer(f"Посилання:\n{link}\n\nРефералів: {u['referrals_count'] if u else 0}\n"
                   f"Безкоштовних пошуків: {u['free_searches'] if u else 0}\n"
                   f"Кожен реферал = +1 пошук по фільтру.", reply_markup=main_kb)

@router.message(F.text == "⭐ Фільтр 1 день (50)")
async def buy_day(m: Message):
    await bot.send_invoice(m.chat.id, "Фільтр 1 день", "Пошук протилежної статі 24 години",
                           f"day_{m.from_user.id}", "", "XTR", [LabeledPrice("1 день", PRICE_DAY)])

@router.message(F.text == "⭐ Фільтр 7 днів (250)")
async def buy_week(m: Message):
    await bot.send_invoice(m.chat.id, "Фільтр 7 днів", "Пошук протилежної статі на тиждень",
                           f"week_{m.from_user.id}", "", "XTR", [LabeledPrice("7 днів", PRICE_WEEK)])

@router.pre_checkout_query()
async def pre(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def paid(m: Message):
    payload = m.successful_payment.invoice_payload
    days = 7 if payload.startswith("week_") else 1
    await set_filter(m.from_user.id, days)
    await m.answer(f"Фільтр активовано на {days} дн.", reply_markup=main_kb)

@router.message(F.text == "🔍 Знайти співрозмовника")
async def find(m: Message, state: FSMContext):
    if await is_banned(m.from_user.id):
        return await m.answer("Заблоковано.")
    uid = m.from_user.id
    user = await get_user(uid)
    if not user or user["in_chat_with"]:
        return await m.answer("Спочатку профіль або закінчи чат.")

    # ліміт безкоштовних
    today = datetime.utcnow().date().isoformat()
    daily = user["daily_searches"] if user["last_search_date"] == today else 0
    can_free_random = daily < FREE_SEARCHES_PER_DAY
    can_opposite = has_active_filter(user) or user["free_searches"] > 0

    if not can_free_random and not can_opposite:
        return await m.answer(f"Ліміт безкоштовних пошуків на сьогодні вичерпано ({FREE_SEARCHES_PER_DAY}).\n"
                              "Купи фільтр або запроси друзів.", reply_markup=main_kb)

    partner = None
    used_free = False
    for pid in list(queue):
        if pid == uid: continue
        p = await get_user(pid)
        if not p or p["in_chat_with"] or p["banned"]: continue
        if can_opposite and is_opposite(user["gender"], p["gender"]):
            partner = pid
            if not has_active_filter(user):
                used_free = True
            break
        elif can_free_random:
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
    except: pass
    await state.clear()

@router.message(F.text == "⚠️ Поскаржитись")
async def report(m: Message):
    user = await get_user(m.from_user.id)
    if not user or not user["in_chat_with"]:
        return await m.answer("Немає активного чату.")
    partner = user["in_chat_with"]
    reports = await add_report(partner)
    await m.answer(f"Скаргу надіслано. У користувача скарг: {reports}")
    if reports >= 3:
        await clear_partner(partner)
        try:
            await bot.send_message(partner, "Тебе заблоковано через скарги.")
        except: pass

@router.message(InChat.active)
async def relay(m: Message, state: FSMContext):
    user = await get_user(m.from_user.id)
    if not user or not user["in_chat_with"]:
        await state.clear()
        return await m.answer("Чат закінчився.", reply_markup=main_kb)
    partner = user["in_chat_with"]
    try:
        if m.text and m.text not in ["❌ Закінчити чат", "⚠️ Поскаржитись"]:
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

@router.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE banned = 1") as c:
            banned = (await c.fetchone())[0]
    await m.answer(f"Користувачів: {total}\nЗаблоковано: {banned}\nВ черзі: {len(queue)}")

@router.message(Command("ban"))
async def ban(m: Message):
    if m.from_user.id != ADMIN_ID: return
    try:
        uid = int(m.text.split()[1])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (uid,))
            await db.commit()
        await m.answer(f"Заблоковано {uid}")
    except:
        await m.answer("/ban user_id")

async def main():
    await init_db()
    dp.include_router(router)
    logger.info("Повна версія запущена")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
