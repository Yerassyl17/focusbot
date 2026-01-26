import os
import telebot
from telebot import types
import threading
import time
import sqlite3
from datetime import datetime, timedelta, timezone, date

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN is not set. Add it in Render Environment Variables.")

bot = telebot.TeleBot(TOKEN)



UNLIMITED_MODE = False
ADMIN_IDS = {8565307134}  # <-- ВСТАВЬ СВОЙ chat_id (число, без кавычек)

bot = telebot.TeleBot(TOKEN)

KZ_TZ = timezone(timedelta(hours=5))

DAILY_REPORT_HOUR = 21
DAILY_REPORT_MINUTE = 0

WEEKLY_REPORT_WEEKDAY = 6
WEEKLY_REPORT_HOUR = 20
WEEKLY_REPORT_MINUTE = 0

# =========================
# TELEGRAM MENU
# =========================
bot.set_my_commands([
    telebot.types.BotCommand("start", "Начать / заново"),
    telebot.types.BotCommand("help", "Как пользоваться"),
    telebot.types.BotCommand("stats", "Моя статистика"),
])


# =========================
# DATABASE
# =========================
DB_PATH = "bot_data.sqlite3"
db_lock = threading.Lock()

def db_init():
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            event_type TEXT,
            action TEXT,
            created_at TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS daily_limits (
            chat_id INTEGER,
            day TEXT,
            picks INTEGER,
            PRIMARY KEY (chat_id, day)
        )
        """)
        conn.commit()

def db_add_event(chat_id, event_type, action=None):
    now = datetime.now(KZ_TZ).isoformat()
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO events(chat_id,event_type,action,created_at) VALUES(?,?,?,?)",
            (chat_id, event_type, action, now)
        )
        conn.commit()

def db_get_picks_today(chat_id):
    today = datetime.now(KZ_TZ).date().isoformat()
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT picks FROM daily_limits WHERE chat_id=? AND day=?", (chat_id, today))
        row = cur.fetchone()
        return row[0] if row else 0

def db_inc_picks_today(chat_id):
    today = datetime.now(KZ_TZ).date().isoformat()
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT picks FROM daily_limits WHERE chat_id=? AND day=?", (chat_id, today))
        if cur.fetchone():
            cur.execute("UPDATE daily_limits SET picks=picks+1 WHERE chat_id=? AND day=?", (chat_id, today))
        else:
            cur.execute("INSERT INTO daily_limits VALUES(?,?,1)", (chat_id, today))
        conn.commit()

# =========================
# LIMIT CHECK (FIXED)
# =========================
def can_start_today(chat_id):
    if UNLIMITED_MODE:
        return True
    if chat_id in ADMIN_IDS:
        return True
    return db_get_picks_today(chat_id) < 1

# =========================
# MEMORY
# =========================
user_data = {}
timers = {}

def reset_session(chat_id):
    user_data[chat_id] = {
        "energy": None,
        "actions": [],
        "current": 0,
        "focus": None
    }

# =========================
# KEYBOARDS
# =========================
def menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать", "📊 Статистика")
    kb.row("❓ Как пользоваться")
    return kb

def energy_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🔋 Высокая", callback_data="energy:high"),
        types.InlineKeyboardButton("😐 Средняя", callback_data="energy:mid"),
        types.InlineKeyboardButton("🪫 Низкая", callback_data="energy:low"),
    )
    return kb

def result_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Я начал", callback_data="started"),
        types.InlineKeyboardButton("⏸ Отложить 10 минут", callback_data="delay"),
        types.InlineKeyboardButton("🔁 Заново", callback_data="restart"),
    )
    return kb

def coach_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("👍 Норм", callback_data="coach:norm"),
        types.InlineKeyboardButton("😵 Тяжело", callback_data="coach:hard"),
        types.InlineKeyboardButton("❌ Бросил", callback_data="coach:quit"),
    )
    return kb

# =========================
# COMMANDS
# =========================
@bot.message_handler(commands=["start"])
def start_cmd(message):
    chat_id = message.chat.id
    if not can_start_today(chat_id):
        bot.send_message(chat_id, "⛔ Сегодня уже был 1 выбор.\nЗавтра можно снова.", reply_markup=menu_kb())
        return

    reset_session(chat_id)
    bot.send_message(
        chat_id,
        "Твоя энергия сейчас?",
        reply_markup=energy_kb()
    )

@bot.message_handler(commands=["help"])
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        "Я помогаю выбрать ОДНО главное действие.\n\n"
        "• Выбери энергию\n"
        "• Напиши 3–7 дел\n"
        "• Я выберу лучшее\n\n"
        "⛔ 1 выбор в день (кроме админа)",
        reply_markup=menu_kb()
    )

@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    chat_id = message.chat.id
    picks = db_get_picks_today(chat_id)
    bot.send_message(chat_id, f"Сегодня выборов: {picks}", reply_markup=menu_kb())

# =========================
# FLOW
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(call):
    chat_id = call.message.chat.id
    user_data[chat_id]["energy"] = call.data.split(":")[1]
    bot.send_message(chat_id, "Напиши 3–7 действий, каждое с новой строки.")
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.chat.id in user_data and not user_data[m.chat.id]["actions"])
def get_actions(message):
    chat_id = message.chat.id
    actions = [a.strip() for a in message.text.split("\n") if a.strip()]
    if len(actions) < 3:
        bot.send_message(chat_id, "Минимум 3 действия.")
        return
    user_data[chat_id]["actions"] = actions
    best = actions[0]
    user_data[chat_id]["focus"] = best

    db_add_event(chat_id, "picked", best)
    db_inc_picks_today(chat_id)

    bot.send_message(
        chat_id,
        f"🔥 Главное действие сейчас:\n<b>{best}</b>",
        parse_mode="HTML",
        reply_markup=result_kb()
    )

# =========================
# RESULT ACTIONS
# =========================
@bot.callback_query_handler(func=lambda c: c.data in ["started", "delay", "restart"])
def result_actions(call):
    chat_id = call.message.chat.id
    focus = user_data.get(chat_id, {}).get("focus")

    if call.data == "started":
        db_add_event(chat_id, "started", focus)
        bot.send_message(chat_id, "Отлично! Через 5 минут спрошу, как идёт.", reply_markup=menu_kb())

        def coach():
            bot.send_message(chat_id, "Как идёт?", reply_markup=coach_kb())

        threading.Timer(5*60, coach).start()

    elif call.data == "delay":
        def remind():
            bot.send_message(chat_id, f"⏰ Напоминание:\n<b>{focus}</b>", parse_mode="HTML")
        threading.Timer(10*60, remind).start()
        bot.send_message(chat_id, "Ок, напомню через 10 минут.", reply_markup=menu_kb())

    elif call.data == "restart":
        start_cmd(call.message)

    bot.answer_callback_query(call.id)

# =========================
# COACH
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("coach:"))
def coach_answer(call):
    bot.send_message(call.message.chat.id, "Принято 👍", reply_markup=menu_kb())
    bot.answer_callback_query(call.id)

# =========================
# START
# =========================
if __name__ == "__main__":
    db_init()
    print("Бот запущен")
    bot.infinity_polling(skip_pending=True)
