import os
import telebot
from telebot import types
import threading
import sqlite3
from datetime import datetime, timedelta, timezone

# =========================
# CONFIG
# =========================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise ValueError("BOT_TOKEN is not set")

bot = telebot.TeleBot(TOKEN)
print("BOT OK:", bot.get_me().username)

ADMIN_IDS = {8311003582}
UNLIMITED_MODE = False
KZ_TZ = timezone(timedelta(hours=5))

# =========================
# DB
# =========================
DB_PATH = "bot_data.sqlite3"
db_lock = threading.Lock()

def db_init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS daily_limits (
            chat_id INTEGER,
            day TEXT,
            picks INTEGER,
            PRIMARY KEY(chat_id, day)
        )
        """)

def db_get_picks_today(chat_id):
    today = datetime.now(KZ_TZ).date().isoformat()
    with sqlite3.connect(DB_PATH) as c:
        r = c.execute(
            "SELECT picks FROM daily_limits WHERE chat_id=? AND day=?",
            (chat_id, today)
        ).fetchone()
        return r[0] if r else 0

def db_inc_pick(chat_id):
    today = datetime.now(KZ_TZ).date().isoformat()
    with sqlite3.connect(DB_PATH) as c:
        if db_get_picks_today(chat_id):
            c.execute("UPDATE daily_limits SET picks=picks+1 WHERE chat_id=? AND day=?", (chat_id, today))
        else:
            c.execute("INSERT INTO daily_limits VALUES (?,?,1)", (chat_id, today))

# =========================
# SESSION
# =========================
user_data = {}

def reset(chat_id):
    user_data[chat_id] = {
        "step": "energy",
        "energy": None,
        "actions": [],
        "cur_action": 0,
        "cur_crit": 0,
        "answered_msgs": set(),
        "expected_msg": None,
    }

# =========================
# KEYBOARDS
# =========================
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
    kb.add(*[types.InlineKeyboardButton(str(i), callback_data=f"score:{i}") for i in range(1, 6)])
    return kb

# =========================
# START
# =========================
@bot.message_handler(commands=["start"])
def start(m):
    chat_id = m.chat.id
    if not UNLIMITED_MODE and chat_id not in ADMIN_IDS and db_get_picks_today(chat_id):
        bot.send_message(chat_id, "⛔ Сегодня уже был выбор")
        return

    reset(chat_id)
    bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())

# =========================
# ENERGY
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(c):
    chat_id = c.message.chat.id
    user_data[chat_id]["energy"] = c.data.split(":")[1]
    user_data[chat_id]["step"] = "actions"
    bot.answer_callback_query(c.id)
    bot.send_message(chat_id, "Напиши 3–7 действий, каждое с новой строки")

# =========================
# ACTIONS INPUT
# =========================
@bot.message_handler(func=lambda m: m.chat.id in user_data and user_data[m.chat.id]["step"] == "actions")
def get_actions(m):
    chat_id = m.chat.id
    lines = [x.strip() for x in m.text.split("\n") if x.strip()]
    if not 3 <= len(lines) <= 7:
        bot.send_message(chat_id, "Нужно 3–7 действий")
        return

    user_data[chat_id]["actions"] = [{"name": x, "type": None, "scores": {}} for x in lines]
    user_data[chat_id]["step"] = "typing"
    ask_type(chat_id)

def ask_type(chat_id):
    a = user_data[chat_id]["actions"][user_data[chat_id]["cur_action"]]
    msg = bot.send_message(
        chat_id,
        f"Выбери тип для действия:\n<b>{a['name']}</b>",
        parse_mode="HTML",
        reply_markup=type_kb()
    )
    user_data[chat_id]["expected_msg"] = msg.message_id

# =========================
# TYPE PICK (LOCKED)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def pick_type(c):
    chat_id = c.message.chat.id
    data = user_data.get(chat_id)

    if not data or data["step"] != "typing":
        bot.answer_callback_query(c.id, "Нажми /start")
        return

    if c.message.message_id != data["expected_msg"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    if c.message.message_id in data["answered_msgs"]:
        bot.answer_callback_query(c.id, "Уже выбрано")
        return

    t = c.data.split(":")[1]
    a = data["actions"][data["cur_action"]]
    a["type"] = t

    data["answered_msgs"].add(c.message.message_id)

    bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    bot.edit_message_text(
        chat_id,
        c.message.message_id,
        f"✅ Тип выбран: <b>{t}</b>\n\n<b>{a['name']}</b>",
        parse_mode="HTML"
    )

    data["cur_action"] += 1
    bot.answer_callback_query(c.id)

    if data["cur_action"] >= len(data["actions"]):
        data["step"] = "scoring"
        data["cur_action"] = 0
        data["cur_crit"] = 0
        ask_score(chat_id)
    else:
        ask_type(chat_id)

# =========================
# SCORING
# =========================
CRITS = ["influence", "urgency", "energy", "meaning"]
CRIT_TITLES = {
    "influence": "Влияние",
    "urgency": "Срочность",
    "energy": "Затраты сил",
    "meaning": "Смысл",
}

def ask_score(chat_id):
    d = user_data[chat_id]
    a = d["actions"][d["cur_action"]]
    crit = CRITS[d["cur_crit"]]
    bot.send_message(
        chat_id,
        f"{a['name']}\nОцени: <b>{CRIT_TITLES[crit]}</b> (1–5)",
        parse_mode="HTML",
        reply_markup=score_kb()
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("score:"))
def score_pick(c):
    chat_id = c.message.chat.id
    d = user_data[chat_id]
    a = d["actions"][d["cur_action"]]
    crit = CRITS[d["cur_crit"]]
    a["scores"][crit] = int(c.data.split(":")[1])

    d["cur_crit"] += 1
    bot.answer_callback_query(c.id)

    if d["cur_crit"] >= len(CRITS):
        d["cur_crit"] = 0
        d["cur_action"] += 1
        if d["cur_action"] >= len(d["actions"]):
            finish(chat_id)
            return

    ask_score(chat_id)

# =========================
# RESULT
# =========================
def finish(chat_id):
    d = user_data[chat_id]
    for a in d["actions"]:
        s = a["scores"]
        a["total"] = s["influence"]*2 + s["urgency"]*2 + s["meaning"] + (6-s["energy"])

    best = max(d["actions"], key=lambda x: x["total"])
    db_inc_pick(chat_id)

    bot.send_message(
        chat_id,
        f"🔥 <b>Главное действие:</b>\n\n<b>{best['name']}</b>",
        parse_mode="HTML"
    )

# =========================
# RUN
# =========================
if __name__ == "__main__":
    db_init()
    bot.infinity_polling(skip_pending=True)
