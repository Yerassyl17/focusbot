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
    raise ValueError("BOT_TOKEN is not set. Add it in Railway Variables.")

bot = telebot.TeleBot(TOKEN)

UNLIMITED_MODE = False
ADMIN_IDS = {8311003582}
KZ_TZ = timezone(timedelta(hours=5))

# =========================
# DB (SQLite)
# =========================
DB_PATH = "bot_data.sqlite3"
db_lock = threading.Lock()

def db_init():
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            action TEXT,
            created_at TEXT NOT NULL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS daily_limits (
            chat_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            picks INTEGER NOT NULL,
            PRIMARY KEY(chat_id, day)
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
        return int(row[0]) if row else 0

def db_inc_picks_today(chat_id):
    today = datetime.now(KZ_TZ).date().isoformat()
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT picks FROM daily_limits WHERE chat_id=? AND day=?", (chat_id, today))
        if cur.fetchone():
            cur.execute("UPDATE daily_limits SET picks=picks+1 WHERE chat_id=? AND day=?", (chat_id, today))
        else:
            cur.execute("INSERT INTO daily_limits(chat_id, day, picks) VALUES(?,?,1)", (chat_id, today))
        conn.commit()

def can_start_today(chat_id):
    if UNLIMITED_MODE:
        return True
    if chat_id in ADMIN_IDS:
        return True
    return db_get_picks_today(chat_id) < 1

# =========================
# SESSION MEMORY
# =========================
user_data = {}
timers = {}

CRITERIA = [
    ("influence", "Влияние (польза для результата)"),
    ("urgency",   "Срочность (насколько важно сейчас)"),
    ("energy",    "Затраты сил (насколько тяжело сделать)"),
    ("meaning",   "Смысл (важно лично тебе)"),
]

HINTS = {
    "influence": "1 = почти не поможет, 5 = сильно продвинет",
    "urgency":   "1 = можно позже, 5 = нужно сейчас/сегодня",
    "energy":    "1 = легко, 5 = очень тяжело по силам",
    "meaning":   "1 = не важно, 5 = очень важно для тебя",
}

def reset_session(chat_id):
    user_data[chat_id] = {
        "step": "energy",
        "energy_now": None,

        # фиксация сообщений (чтобы нельзя было “переответить”)
        "energy_msg_id": None,
        "energy_locked": False,

        "actions": [],
        "cur_action": 0,
        "cur_crit": 0,
        "focus": None,

        # фиксация типа действия
        "expected_type_msg_id": None,     # только это сообщение “валидное”
        "answered_type_msgs": set(),      # чтобы 2 раза не обработать одно и то же

        # фиксация оценок (по желанию можно расширить)
        "step_locked": False,
    }

def cancel_timers(chat_id):
    t = timers.get(chat_id, {})
    for k in ("reminder", "coach"):
        if k in t and t[k]:
            try:
                t[k].cancel()
            except Exception:
                pass
    timers[chat_id] = {"reminder": None, "coach": None}

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
        types.InlineKeyboardButton("✅ Я начал", callback_data="result:started"),
        types.InlineKeyboardButton("⏸ Отложить 10 минут", callback_data="result:delay"),
        types.InlineKeyboardButton("🔁 Заново", callback_data="result:restart"),
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

def action_type_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🧠 Умственное", callback_data="atype:mental"),
        types.InlineKeyboardButton("💪 Физическое", callback_data="atype:physical"),
    )
    kb.row(
        types.InlineKeyboardButton("🗂 Рутинное", callback_data="atype:routine"),
        types.InlineKeyboardButton("💬 Общение", callback_data="atype:social"),
    )
    return kb

def type_label(t: str) -> str:
    return {
        "mental": "🧠 Умственное",
        "physical": "💪 Физическое",
        "routine": "🗂 Рутинное",
        "social": "💬 Общение",
    }.get(t, t)

# =========================
# COMMANDS
# =========================
bot.set_my_commands([
    telebot.types.BotCommand("start", "Начать / заново"),
    telebot.types.BotCommand("help", "Как пользоваться"),
    telebot.types.BotCommand("stats", "Моя статистика"),
])

@bot.message_handler(commands=["start"])
def start_cmd(message):
    chat_id = message.chat.id
    cancel_timers(chat_id)

    if not can_start_today(chat_id):
        bot.send_message(chat_id, "⛔ Сегодня уже был 1 выбор.\nЗавтра можно снова.", reply_markup=menu_kb())
        return

    reset_session(chat_id)

    # ВАЖНО: сохраняем message_id вопроса про энергию
    msg = bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    user_data[chat_id]["energy_msg_id"] = msg.message_id

    bot.send_message(chat_id, "Меню:", reply_markup=menu_kb())

@bot.message_handler(commands=["help"])
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        "Я помогаю выбрать ОДНО главное действие.\n\n"
        "1) /start или 🚀 Начать\n"
        "2) Выбери энергию\n"
        "3) Напиши 3–7 действий\n"
        "4) Для каждого действия выбери тип\n"
        "5) Оцени по 4 критериям (1–5)\n\n"
        "⛔ 1 выбор в день (кроме админа).",
        reply_markup=menu_kb()
    )

@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    chat_id = message.chat.id
    picks = db_get_picks_today(chat_id)
    bot.send_message(chat_id, f"Сегодня выборов: {picks}", reply_markup=menu_kb())

@bot.message_handler(func=lambda m: m.text in ["🚀 Начать", "📊 Статистика", "❓ Как пользоваться"])
def menu_handler(message):
    if message.text == "📊 Статистика":
        stats_cmd(message)
    elif message.text == "❓ Как пользоваться":
        help_cmd(message)
    else:
        start_cmd(message)

# =========================
# FLOW: ENERGY (LOCKED)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data:
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    # принимаем ТОЛЬКО кнопки именно от “последнего” вопроса про энергию
    if data["energy_msg_id"] is not None and call.message.message_id != data["energy_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    # если уже выбрал энергию — не даём поменять
    if data["energy_locked"]:
        bot.answer_callback_query(call.id, "✅ Энергия уже выбрана")
        return

    lvl = call.data.split(":")[1]
    data["energy_now"] = lvl
    data["energy_locked"] = True
    data["step"] = "actions"

    # убираем кнопки энергии + обновляем текст чтобы было видно выбор
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ Энергия: <b>{ {'high':'🔋 Высокая','mid':'😐 Средняя','low':'🪫 Низкая'}.get(lvl,lvl) }</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "Напиши 3–7 действий, каждое с новой строки.")

# =========================
# FLOW: ACTIONS INPUT
# =========================
@bot.message_handler(func=lambda m: m.chat.id in user_data and user_data[m.chat.id].get("step") == "actions")
def get_actions(message):
    chat_id = message.chat.id
    data = user_data[chat_id]

    lines = [a.strip() for a in message.text.split("\n") if a.strip()]
    if not 3 <= len(lines) <= 7:
        bot.send_message(chat_id, "Нужно 3–7 действий. Каждое с новой строки.")
        return

    data["actions"] = [{"name": a, "type": None, "scores": {}} for a in lines]
    data["cur_action"] = 0
    data["cur_crit"] = 0
    data["step"] = "typing"
    data["expected_type_msg_id"] = None
    data["answered_type_msgs"].clear()

    ask_action_type(chat_id)

def ask_action_type(chat_id):
    data = user_data[chat_id]
    a = data["actions"][data["cur_action"]]

    msg = bot.send_message(
        chat_id,
        f"Выбери тип для действия:\n<b>{a['name']}</b>",
        parse_mode="HTML",
        reply_markup=action_type_kb()
    )

    # ВАЖНО: только это сообщение теперь можно “отвечать”
    data["expected_type_msg_id"] = msg.message_id

# =========================
# TYPE PICK (HARD LOCK)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("atype:"))
def action_type_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "typing":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    # если нажали на старое сообщение — игнорируем
    if data["expected_type_msg_id"] is not None and call.message.message_id != data["expected_type_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    # если уже обработали это сообщение — не даём менять
    if call.message.message_id in data["answered_type_msgs"]:
        bot.answer_callback_query(call.id, "✅ Уже выбрано")
        return

    t = call.data.split(":")[1]  # mental/physical/routine/social
    a = data["actions"][data["cur_action"]]
    a["type"] = t

    # помечаем как обработанное
    data["answered_type_msgs"].add(call.message.message_id)

    # убираем кнопки
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

    # меняем текст — видно “ДЕЙСТВИЕ — ТИП”
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ <b>{a['name']}</b> — <b>{type_label(t)}</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id, "Готово ✅")

    # следующий action / переход к оценкам
    data["cur_action"] += 1
    if data["cur_action"] >= len(data["actions"]):
        data["cur_action"] = 0
        data["cur_crit"] = 0
        data["step"] = "scoring"
        ask_next_score(chat_id)
    else:
        ask_action_type(chat_id)

# =========================
# SCORING
# =========================
def ask_next_score(chat_id):
    data = user_data[chat_id]
    a = data["actions"][data["cur_action"]]
    key, title = CRITERIA[data["cur_crit"]]

    bot.send_message(
        chat_id,
        f"Действие: <b>{a['name']}</b>\n"
        f"Тип: <b>{type_label(a['type'])}</b>\n\n"
        f"Оцени: <b>{title}</b> (1–5)\n"
        f"<i>{HINTS[key]}</i>",
        parse_mode="HTML",
        reply_markup=score_kb()
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("score:"))
def score_pick(call):
    chat_id = call.message.chat.id
    score = int(call.data.split(":")[1])

    if chat_id not in user_data or user_data[chat_id].get("step") != "scoring":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    data = user_data[chat_id]
    a = data["actions"][data["cur_action"]]
    key, _ = CRITERIA[data["cur_crit"]]
    a["scores"][key] = score

    data["cur_crit"] += 1
    if data["cur_crit"] >= len(CRITERIA):
        data["cur_crit"] = 0
        data["cur_action"] += 1

        if data["cur_action"] >= len(data["actions"]):
            bot.answer_callback_query(call.id)
            show_result(chat_id)
            return

    bot.answer_callback_query(call.id)
    ask_next_score(chat_id)

# =========================
# RESULT
# =========================
def energy_weight(level: str) -> float:
    return {"low": 2.0, "mid": 1.0, "high": 0.6}.get(level, 1.0)

def show_result(chat_id):
    data = user_data[chat_id]
    lvl = data.get("energy_now", "mid")
    ew = energy_weight(lvl)

    for a in data["actions"]:
        s = a["scores"]
        energy_bonus = 6 - s["energy"]
        a["total"] = (
            s["influence"] * 2 +
            s["urgency"] * 2 +
            s["meaning"] * 1 +
            energy_bonus * ew
        )

    best = max(data["actions"], key=lambda x: x["total"])
    data["focus"] = best["name"]
    data["step"] = "result"

    db_add_event(chat_id, "picked", best["name"])
    db_inc_picks_today(chat_id)

    bot.send_message(
        chat_id,
        "🔥 <b>Главное действие сейчас:</b>\n\n"
        f"<b>{best['name']}</b>\n"
        f"Тип: <b>{type_label(best['type'])}</b>\n\n"
        "Сделай первый шаг за 2–5 минут (без идеала).",
        parse_mode="HTML",
        reply_markup=result_kb()
    )

# =========================
# RUN
# =========================
if __name__ == "__main__":
    db_init()
    print("Бот запущен")
    bot.infinity_polling(skip_pending=True)
