import os
import threading
import sqlite3
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types

# =========================
# CONFIG
# =========================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
KZ_TZ = timezone(timedelta(hours=5))

# =========================
# DATABASE
# =========================
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

def count_today(chat_id, event):
    today = datetime.now(KZ_TZ).date().isoformat()
    with db_lock, db() as c:
        cur = c.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM logs
            WHERE chat_id=? AND event=? AND substr(created_at,1,10)=?
        """, (chat_id, event, today))
        return int(cur.fetchone()[0])

# =========================
# SESSION MEMORY
# =========================
sessions = {}   # chat_id -> session dict
timers = {}     # chat_id -> {"remind": Timer, "progress": Timer}

def cancel_timer(chat_id, key):
    t = timers.get(chat_id, {}).get(key)
    if t:
        try:
            t.cancel()
        except Exception:
            pass
    timers.setdefault(chat_id, {})[key] = None

def cancel_all(chat_id):
    cancel_timer(chat_id, "remind")
    cancel_timer(chat_id, "progress")

def new_session(chat_id):
    sessions[chat_id] = {
        "step": "energy",     # energy -> actions -> type -> score -> result
        "energy": None,       # 'high'/'mid'/'low'
        "actions": [],        # [{"name":..., "type":..., "scores":[...] }]
        "cur": 0,
        "crit": 0,
        "focus": None
    }

# =========================
# UI
# =========================
MENU_TEXTS = {"🚀 Начать", "⏸ Отложить", "📊 Статистика", "❓ Как пользоваться"}

def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать", "⏸ Отложить")
    kb.row("📊 Статистика", "❓ Как пользоваться")
    return kb

def energy_kb():
    # ВАЖНО: callback_data = high/mid/low (а не "Высокая")
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🔋 Высокая", callback_data="energy:high"),
        types.InlineKeyboardButton("😐 Средняя", callback_data="energy:mid"),
        types.InlineKeyboardButton("🪫 Низкая", callback_data="energy:low"),
    )
    return kb

def energy_label(code: str) -> str:
    return {"high": "🔋 Высокая", "mid": "😐 Средняя", "low": "🪫 Низкая"}.get(code, code)

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

def type_label(t: str) -> str:
    return {
        "mental": "🧠 Умственное",
        "physical": "💪 Физическое",
        "routine": "🗂 Рутинное",
        "social": "💬 Общение",
    }.get(t, t)

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

def pick_best(actions, energy_code):
    # energy_code: low/mid/high
    weight = {"low": 2.0, "mid": 1.0, "high": 0.6}.get(energy_code, 1.0)

    best = None
    best_score = -10**9

    for a in actions:
        s = a["scores"]  # [influence, urgency, energy_cost, meaning]
        score = (
            s[0] * 2 +
            s[1] * 2 +
            s[3] * 1 +
            (6 - s[2]) * weight
        )
        if score > best_score:
            best_score = score
            best = a

    return best

# =========================
# MENU HANDLER (ДОЛЖЕН БЫТЬ ВЫШЕ step-хэндлеров)
# =========================
@bot.message_handler(func=lambda m: (m.text or "").strip() in MENU_TEXTS)
def menu_handler(m):
    txt = (m.text or "").strip()
    chat_id = m.chat.id

    if txt == "🚀 Начать":
        start_flow(chat_id)
        return

    if txt == "❓ Как пользоваться":
        help_flow(chat_id)
        return

    if txt == "📊 Статистика":
        stats_flow(chat_id)
        return

    if txt == "⏸ Отложить":
        s = sessions.get(chat_id)
        if not s or not s.get("focus"):
            bot.send_message(chat_id, "⏸ Пока нечего откладывать — сначала сделай выбор через 🚀 Начать.", reply_markup=menu())
            return

        focus = s["focus"]
        cancel_timer(chat_id, "remind")

        bot.send_message(chat_id, f"⏸ Ок, отложил на 10 минут: <b>{focus}</b>\nЯ напомню.", reply_markup=menu())
        log(chat_id, "delayed_menu", focus)

        def remind():
            try:
                bot.send_message(chat_id, f"⏰ Напоминание: <b>{focus}</b>", reply_markup=menu())
                log(chat_id, "reminder_sent", focus)
            except Exception:
                pass

        t = threading.Timer(10 * 60, remind)
        timers.setdefault(chat_id, {})["remind"] = t
        t.start()

# =========================
# COMMANDS
# =========================
@bot.message_handler(commands=["start"])
def start_cmd(m):
    start_flow(m.chat.id)

@bot.message_handler(commands=["help"])
def help_cmd(m):
    help_flow(m.chat.id)

@bot.message_handler(commands=["stats"])
def stats_cmd(m):
    stats_flow(m.chat.id)

def start_flow(chat_id):
    cancel_all(chat_id)
    new_session(chat_id)

    bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    bot.send_message(chat_id, "Меню:", reply_markup=menu())
    log(chat_id, "start_flow", "ok")

def help_flow(chat_id):
    bot.send_message(
        chat_id,
        "Я помогаю выбрать одно главное действие.\n\n"
        "1) Выбери энергию\n"
        "2) Напиши как минимум 3 действия (каждое с новой строки)\n"
        "3) Укажи тип и оценки\n"
        "4) Получишь главное действие\n"
        "5) Я спрошу как идёт 👍😵❌\n",
        reply_markup=menu()
    )

def stats_flow(chat_id):
    started_today = count_today(chat_id, "started")
    focus_today = count_today(chat_id, "focus")
    progress_today = count_today(chat_id, "progress")
    bot.send_message(
        chat_id,
        f"📊 Статистика за сегодня:\n"
        f"• Выборов (focus): <b>{focus_today}</b>\n"
        f"• Начал: <b>{started_today}</b>\n"
        f"• Ответов 'как идёт': <b>{progress_today}</b>",
        reply_markup=menu()
    )

# =========================
# ENERGY
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)

    if not s:
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    if s.get("energy"):
        bot.answer_callback_query(c.id, "Уже выбрано ✅")
        return

    code = c.data.split(":", 1)[1]  # high/mid/low
    s["energy"] = code
    log(chat_id, "energy", code)

    try:
        bot.edit_message_text(
            f"✅ Энергия выбрана: <b>{energy_label(code)}</b>",
            chat_id,
            c.message.message_id
        )
    except Exception:
        pass

    s["step"] = "actions"
    bot.answer_callback_query(c.id)
    bot.send_message(chat_id, "✍️ Напиши как минимум 3 действия (каждое с новой строки):", reply_markup=menu())

# =========================
# ACTIONS INPUT
# =========================
@bot.message_handler(func=lambda m: m.chat.id in sessions and sessions[m.chat.id].get("step") == "actions")
def actions_input(m):
    # если пришло меню — игнорируем как "действия"
    if (m.text or "").strip() in MENU_TEXTS:
        return

    chat_id = m.chat.id
    s = sessions[chat_id]

    lines = [l.strip() for l in (m.text or "").split("\n") if l.strip()]
    if len(lines) < 3:
        bot.send_message(chat_id, "❌ Нужно как минимум 3 действия (каждое с новой строки).", reply_markup=menu())
        return

    s["actions"] = [{"name": l, "type": None, "scores": []} for l in lines]
    s["cur"] = 0
    s["crit"] = 0
    s["step"] = "type"
    log(chat_id, "actions_count", str(len(lines)))
    ask_type(chat_id)

def ask_type(chat_id):
    s = sessions[chat_id]
    a = s["actions"][s["cur"]]
    bot.send_message(chat_id, f"Тип действия:\n<b>{a['name']}</b>", reply_markup=type_kb())

# =========================
# TYPE
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def type_pick(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)
    if not s or s.get("step") != "type":
        bot.answer_callback_query(c.id, "Сейчас не время выбирать тип 🙂")
        return

    a = s["actions"][s["cur"]]
    a["type"] = c.data.split(":", 1)[1]
    log(chat_id, "type", a["type"])

    try:
        bot.edit_message_text(
            f"✅ <b>{a['name']}</b> — {type_label(a['type'])}",
            chat_id,
            c.message.message_id
        )
    except Exception:
        pass

    s["crit"] = 0
    s["step"] = "score"
    bot.answer_callback_query(c.id)
    ask_score(chat_id)

# =========================
# SCORE
# =========================
def ask_score(chat_id):
    s = sessions[chat_id]
    a = s["actions"][s["cur"]]

    key, title = CRITERIA[s["crit"]]
    hint = HINTS.get(key, "")

    bot.send_message(
        chat_id,
        f"Действие: <b>{a['name']}</b>\n"
        f"Тип: <b>{type_label(a.get('type'))}</b>\n\n"
        f"Оцени: <b>{title}</b> (1–5)\n"
        f"<i>{hint}</i>",
        reply_markup=score_kb()
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("score:"))
def score_pick(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)
    if not s or s.get("step") != "score":
        bot.answer_callback_query(c.id, "Сейчас не время ставить оценку 🙂")
        return

    score = int(c.data.split(":", 1)[1])
    s["actions"][s["cur"]]["scores"].append(score)

    key, title = CRITERIA[s["crit"]]
    log(chat_id, "score", f"{key}={score}")

    try:
        bot.edit_message_text(
            f"✅ {title}: <b>{score}</b>",
            chat_id,
            c.message.message_id
        )
    except Exception:
        pass

    s["crit"] += 1
    bot.answer_callback_query(c.id)

    if s["crit"] >= 4:
        s["cur"] += 1
        if s["cur"] >= len(s["actions"]):
            show_result(chat_id)
            return
        s["step"] = "type"
        ask_type(chat_id)
    else:
        ask_score(chat_id)

# =========================
# RESULT
# =========================
def show_result(chat_id):
    s = sessions[chat_id]
    s["step"] = "result"

    best = pick_best(s["actions"], s["energy"])
    s["focus"] = best["name"]
    log(chat_id, "focus", s["focus"])

    bot.send_message(
        chat_id,
        f"🔥 <b>Главное действие сейчас:</b>\n\n"
        f"<b>{best['name']}</b>\n"
        f"Тип: <b>{type_label(best.get('type'))}</b>\n\n"
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
    if not s or not s.get("focus"):
        bot.answer_callback_query(c.id, "Сначала сделай выбор через 🚀 Начать")
        return

    focus = s["focus"]
    cmd = c.data.split(":", 1)[1]

    if cmd == "start":
        log(chat_id, "started", focus)
        cancel_timer(chat_id, "progress")

        try:
            bot.edit_message_text(
                f"🚀 Ты начал: <b>{focus}</b>\n\nЧерез 5 минут спрошу, как идёт.",
                chat_id,
                c.message.message_id
            )
        except Exception:
            pass

        def ask_progress():
            try:
                bot.send_message(chat_id, "Как идёт?", reply_markup=progress_kb())
            except Exception:
                pass

        t = threading.Timer(5 * 60, ask_progress)
        timers.setdefault(chat_id, {})["progress"] = t
        t.start()

        bot.answer_callback_query(c.id, "Погнали 🔥")
        return

    if cmd == "delay":
        log(chat_id, "delayed_10m", focus)
        cancel_timer(chat_id, "remind")

        try:
            bot.edit_message_text(
                f"⏸ Отложено на 10 минут: <b>{focus}</b>\nЯ напомню.",
                chat_id,
                c.message.message_id
            )
        except Exception:
            pass

        def remind():
            try:
                bot.send_message(chat_id, f"⏰ Напоминание: <b>{focus}</b>", reply_markup=menu())
                log(chat_id, "reminder_sent", focus)
            except Exception:
                pass

        t = threading.Timer(10 * 60, remind)
        timers.setdefault(chat_id, {})["remind"] = t
        t.start()

        bot.answer_callback_query(c.id, "Ок ⏸")
        return

# =========================
# PROGRESS
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("prog:"))
def progress(c):
    chat_id = c.message.chat.id
    val = c.data.split(":", 1)[1]
    log(chat_id, "progress", val)

    texts = {
        "ok": "👍 Отлично. Продолжай ещё 10 минут или доведи до мини-результата.",
        "hard": "😵 Упрости задачу в 2 раза и сделай 2 минуты. Главное — движение.",
        "quit": "❌ Ничего страшного. Это тоже опыт. Можешь нажать 🚀 Начать и выбрать шаг поменьше."
    }

    try:
        bot.edit_message_text(texts.get(val, "Ок"), chat_id, c.message.message_id)
    except Exception:
        pass

    bot.answer_callback_query(c.id)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    init_db()
    print("Bot started")
    bot.infinity_polling(skip_pending=True)
