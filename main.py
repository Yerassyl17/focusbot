import os
import threading
import sqlite3
import time
from telebot.apihelper import ApiTelegramException
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types

# ================= CONFIG =================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
KZ_TZ = timezone(timedelta(hours=5))

# ================= DATABASE =================
DB = "data.sqlite3"
db_lock = threading.Lock()

def db():
    return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    with db_lock, db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            event TEXT,
            value TEXT,
            created_at TEXT
        )
        """)
        c.commit()

def log(chat_id, event, value=None):
    with db_lock, db() as c:
        c.execute(
            "INSERT INTO logs(chat_id,event,value,created_at) VALUES(?,?,?,?)",
            (chat_id, event, value, datetime.now(KZ_TZ).isoformat())
        )
        c.commit()

# ================= STATE =================
sessions = {}
timers = {}

def cancel_timer(chat_id, key):
    t = timers.get(chat_id, {}).get(key)
    if t:
        try: t.cancel()
        except: pass
    timers.setdefault(chat_id, {})[key] = None

def cancel_all(chat_id):
    cancel_timer(chat_id, "check")
    cancel_timer(chat_id, "remind")

def new_session(chat_id):
    sessions[chat_id] = {
        "step": "result",
        "focus": None,
        "type": None
    }

# ================= UI =================
def result_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🚀 Я начал", callback_data="act:start"),
        types.InlineKeyboardButton("⏸ Отложить 10 минут", callback_data="act:delay10"),
    )
    kb.add(
        types.InlineKeyboardButton("🕒 Попозже (30 минут)", callback_data="act:delay30"),
        types.InlineKeyboardButton("❌ Не хочу сейчас", callback_data="act:skip"),
    )
    return kb

def progress_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("👍 Норм", callback_data="prog:ok"),
        types.InlineKeyboardButton("😵 Тяжело", callback_data="prog:hard"),
        types.InlineKeyboardButton("❌ Бросил", callback_data="prog:quit"),
    )
    return kb

def quit_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🔁 Попробовать снова (меньше)", callback_data="quit:retry"),
        types.InlineKeyboardButton("🕒 Вернуться позже", callback_data="quit:later"),
        types.InlineKeyboardButton("🚀 Начать другое действие", callback_data="quit:new"),
    )
    return kb

# ================= MOTIVATION =================
MOTIVATION_START = {
    "mental": "Спокойно.\nНе нужно делать идеально.\nПросто подумай над первым шагом.",
    "physical": "Начни медленно.\nГлавное — движение, не скорость.\nТело включится по ходу.",
    "routine": "Сделай самый неприятный кусочек первым.\nПотом станет легче.",
    "social": "Не нужно идеально говорить.\nДостаточно начать разговор.",
}

MOTIVATION_HARD = {
    "mental": "Можно просто набросать идеи, не решать.",
    "physical": "Сделай половину. Этого достаточно.",
    "routine": "Остановись после одного пункта.",
    "social": "Достаточно одного сообщения.",
}

# ================= RESULT =================
def show_result(chat_id, action_name, action_type):
    new_session(chat_id)
    sessions[chat_id]["focus"] = action_name
    sessions[chat_id]["type"] = action_type

    bot.send_message(
        chat_id,
        f"🔥 <b>Главное действие сейчас:</b>\n\n<b>{action_name}</b>",
        reply_markup=result_kb()
    )

# ================= ACTION HANDLER =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("act:"))
def act_handler(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)
    if not s: return

    action = c.data.split(":")[1]
    focus = s["focus"]
    t = s["type"]

    if action == "start":
        bot.edit_message_text(
            f"🚀 Ты начал: <b>{focus}</b>\n\n"
            f"{MOTIVATION_START.get(t,'')}\n\n"
            "Я не буду отвлекать.\n"
            "Через 10 минут спрошу, как идёт.",
            chat_id, c.message.message_id
        )

        def check():
            bot.send_message(chat_id, "Как идёт?", reply_markup=progress_kb())

        timers.setdefault(chat_id,{})["check"] = threading.Timer(10*60, check)
        timers[chat_id]["check"].start()

    elif action == "delay10":
        delay(chat_id, focus, 10)

    elif action == "delay30":
        delay(chat_id, focus, 30)

    elif action == "skip":
        bot.edit_message_text(
            "Ок.\nИногда лучше не давить на себя.",
            chat_id, c.message.message_id
        )

def delay(chat_id, focus, minutes):
    bot.send_message(chat_id, f"Ок.\nЯ напомню через {minutes} минут.")

    def remind():
        bot.send_message(
            chat_id,
            "Можешь начать с самого маленького шага."
        )

    timers.setdefault(chat_id,{})["remind"] = threading.Timer(minutes*60, remind)
    timers[chat_id]["remind"].start()

@bot.callback_query_handler(func=lambda c: c.data.startswith("prog:"))
def progress_handler(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)
    if not s: return

    val = c.data.split(":")[1]
    t = s["type"]

    if val == "ok":
        bot.edit_message_text(
            "Отлично.\nПродолжай в том же ритме.\nДаже если медленно — это работает.",
            chat_id, c.message.message_id
        )

    elif val == "hard":
        bot.edit_message_text(
            "Ок, давай проще.\n"
            "Сделай версию в 2 раза легче.\n\n"
            f"{MOTIVATION_HARD.get(t,'')}",
            chat_id, c.message.message_id
        )

    elif val == "quit":
        bot.edit_message_text(
            "Это нормально.\nТы попробовал — это уже шаг.",
            chat_id, c.message.message_id,
            reply_markup=quit_kb()
        )

if __name__ == "__main__":
    init_db()
    print("Bot started")

    while True:
        try:
            bot.infinity_polling(skip_pending=True, none_stop=True, timeout=60, long_polling_timeout=60)
        except ApiTelegramException as e:
            if "409" in str(e):
                print("409 conflict: another instance is running. Retrying in 10s...")
                time.sleep(10)
            else:
                raise
        except Exception as e:
            print("Polling error:", e)
            time.sleep(5)
