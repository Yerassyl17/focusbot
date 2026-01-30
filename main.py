import os
import threading
import sqlite3
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

KZ_TZ = timezone(timedelta(hours=5))

# =========================
# DATABASE
# =========================
DB = "data.sqlite3"

def db():
    return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    with db() as c:
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
    with db() as c:
        c.execute(
            "INSERT INTO logs(chat_id,event,value,created_at) VALUES(?,?,?,?)",
            (chat_id, event, value, datetime.now(KZ_TZ).isoformat())
        )
        c.commit()

# =========================
# SESSION MEMORY
# =========================
sessions = {}
timers = {}

# =========================
# UI
# =========================
def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать", "⏸ Отложить")
    kb.row("📊 Статистика", "❓ Как пользоваться")
    return kb

def remove():
    return types.ReplyKeyboardRemove()

def energy_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🔋 Высокая", callback_data="energy:high"),
        types.InlineKeyboardButton("😐 Средняя", callback_data="energy:mid"),
        types.InlineKeyboardButton("🪫 Низкая", callback_data="energy:low"),
    )
    return kb

def type_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🧠 Умственное", callback_data="type:mental"),
        types.InlineKeyboardButton("💪 Физическое", callback_data="type:physical"),
    )
    kb.row(
        types.InlineKeyboardButton("🗂 Рутинное", callback_data="type:routine"),
        types.InlineKeyboardButton("💬 Общение", callback_data="type:social"),
    )
    return kb

def score_kb():
    kb = types.InlineKeyboardMarkup(row_width=5)
    kb.add(*[
        types.InlineKeyboardButton(str(i), callback_data=f"score:{i}")
        for i in range(1, 6)
    ])
    return kb

def result_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🚀 Я начал", callback_data="res:start"),
        types.InlineKeyboardButton("⏸ Отложить 10 мин", callback_data="res:delay"),
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

# =========================
# HELPERS
# =========================
CRITERIA = ["Влияние", "Срочность", "Затраты сил", "Смысл"]

def pick_best(actions, energy):
    weight = {"low": 2, "mid": 1, "high": 0.6}.get(energy, 1)
    best = None
    best_score = -1

    for a in actions:
        s = a["scores"]
        score = (
            s[0] * 2 +
            s[1] * 2 +
            s[3] +
            (6 - s[2]) * weight
        )
        if score > best_score:
            best_score = score
            best = a

    return best

# =========================
# COMMANDS
# =========================
@bot.message_handler(commands=["start"])
def start_cmd(m):
    sessions[m.chat.id] = {
        "step": "energy",
        "energy": None,
        "actions": [],
        "cur": 0,
        "crit": 0,
        "focus": None
    }
    bot.send_message(m.chat.id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    bot.send_message(m.chat.id, "Меню:", reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "❓ Как пользоваться")
def help_cmd(m):
    bot.send_message(
        m.chat.id,
        "Я помогаю выбрать одно главное действие.\n\n"
        "1) Выбери энергию\n"
        "2) Напиши минимум 3 действия\n"
        "3) Укажи тип и оценки\n"
        "4) Получишь главное действие\n"
        "5) Я спрошу как идёт",
        reply_markup=menu()
    )

# =========================
# ENERGY
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(c):
    s = sessions.get(c.message.chat.id)
    if not s or s["energy"]:
        return

    s["energy"] = c.data.split(":")[1]
    log(c.message.chat.id, "energy", s["energy"])

    bot.edit_message_text(
        f"✅ Энергия выбрана: <b>{s['energy']}</b>",
        c.message.chat.id,
        c.message.message_id
    )

    s["step"] = "actions"
    bot.send_message(
        c.message.chat.id,
        "✍️ Напиши как минимум 3 действия (каждое с новой строки):"
    )

# =========================
# ACTIONS INPUT
# =========================
@bot.message_handler(func=lambda m: m.chat.id in sessions and sessions[m.chat.id]["step"] == "actions")
def actions_input(m):
    lines = [l.strip() for l in m.text.split("\n") if l.strip()]
    if len(lines) < 3:
        bot.send_message(m.chat.id, "❌ Нужно минимум 3 действия.")
        return

    s = sessions[m.chat.id]
    s["actions"] = [{"name": l, "type": None, "scores": []} for l in lines]
    s["step"] = "type"
    ask_type(m.chat.id)

def ask_type(chat_id):
    s = sessions[chat_id]
    a = s["actions"][s["cur"]]
    bot.send_message(
        chat_id,
        f"Тип действия:\n<b>{a['name']}</b>",
        reply_markup=type_kb()
    )

# =========================
# TYPE
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def type_pick(c):
    s = sessions.get(c.message.chat.id)
    if not s or s["step"] != "type":
        return

    a = s["actions"][s["cur"]]
    a["type"] = c.data.split(":")[1]
    log(c.message.chat.id, "type", a["type"])

    bot.edit_message_text(
        f"✅ {a['name']} — {a['type']}",
        c.message.chat.id,
        c.message.message_id
    )

    s["crit"] = 0
    s["step"] = "score"
    ask_score(c.message.chat.id)

# =========================
# SCORE
# =========================
def ask_score(chat_id):
    s = sessions[chat_id]
    a = s["actions"][s["cur"]]
    bot.send_message(
        chat_id,
        f"{a['name']}\nОцени: <b>{CRITERIA[s['crit']]}</b>",
        reply_markup=score_kb()
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("score:"))
def score_pick(c):
    s = sessions.get(c.message.chat.id)
    if not s or s["step"] != "score":
        return

    score = int(c.data.split(":")[1])
    s["actions"][s["cur"]]["scores"].append(score)

    bot.edit_message_text(
        f"✅ {CRITERIA[s['crit']]}: {score}",
        c.message.chat.id,
        c.message.message_id
    )

    s["crit"] += 1
    if s["crit"] >= 4:
        s["cur"] += 1
        if s["cur"] >= len(s["actions"]):
            show_result(c.message.chat.id)
            return
        s["step"] = "type"
        ask_type(c.message.chat.id)
    else:
        ask_score(c.message.chat.id)

# =========================
# RESULT
# =========================
def show_result(chat_id):
    s = sessions[chat_id]
    best = pick_best(s["actions"], s["energy"])
    s["focus"] = best["name"]
    log(chat_id, "focus", s["focus"])

    bot.send_message(
        chat_id,
        f"🔥 <b>Главное действие сейчас:</b>\n\n<b>{best['name']}</b>\n\n"
        "Сделай первый шаг за 2–5 минут.",
        reply_markup=result_kb()
    )

# =========================
# RESULT ACTIONS
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("res:"))
def result_action(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)
    if not s:
        return

    if c.data == "res:start":
        log(chat_id, "started", s["focus"])
        bot.edit_message_text(
            f"🚀 Ты начал: <b>{s['focus']}</b>\n\nЧерез 5 минут спрошу, как идёт.",
            chat_id,
            c.message.message_id
        )

        def ask_progress():
            bot.send_message(chat_id, "Как идёт?", reply_markup=progress_kb())

        timers[chat_id] = threading.Timer(300, ask_progress)
        timers[chat_id].start()

    elif c.data == "res:delay":
        log(chat_id, "delayed", s["focus"])
        bot.edit_message_text(
            f"⏸ Отложено на 10 минут: <b>{s['focus']}</b>",
            chat_id,
            c.message.message_id
        )

# =========================
# PROGRESS
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("prog:"))
def progress(c):
    val = c.data.split(":")[1]
    log(c.message.chat.id, "progress", val)

    texts = {
        "ok": "👍 Отлично. Продолжай!",
        "hard": "😵 Упрости задачу и сделай 2 минуты.",
        "quit": "❌ Ничего страшного. Это тоже опыт."
    }

    bot.edit_message_text(
        texts[val],
        c.message.chat.id,
        c.message.message_id
    )

# =========================
# RUN
# =========================
if __name__ == "__main__":
    init_db()
    print("Bot started")
    bot.infinity_polling(skip_pending=True)
