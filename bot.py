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
