import os
import json
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
    raise ValueError("BOT_TOKEN is not set. Add it in Railway Variables.")

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()  # optional

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

UNLIMITED_MODE = False
ADMIN_IDS = {8311003582}  # твой chat_id

KZ_TZ = timezone(timedelta(hours=5))

# =========================
# OPTIONAL: OpenAI client
# =========================
openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print("OpenAI init error:", e)
        openai_client = None

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
user_data = {}   # chat_id -> dict
timers = {}      # chat_id -> dict of timers

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

        # ENERGY lock
        "energy_msg_id": None,
        "energy_locked": False,

        # ACTIONS + TYPE
        "actions": [],                # [{"name":..., "type":..., "scores":{...}}]
        "cur_action": 0,
        "cur_crit": 0,

        # TYPE lock
        "expected_type_msg_id": None,
        "answered_type_msgs": set(),

        # SCORING lock
        "expected_score_msg_id": None,
        "answered_score_msgs": set(),

        # RESULT
        "focus": None,
        "result_msg_id": None,
        "delayed_control_msg_id": None,

        "picked_logged": False,  # чтобы daily limit инкрементнулся один раз
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
# UI helpers
# =========================
def remove_menu():
    return types.ReplyKeyboardRemove()

def menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать", "📊 Статистика")
    kb.row("❓ Как пользоваться")
    return kb

def energy_label(lvl: str) -> str:
    return {"high":"🔋 Высокая", "mid":"😐 Средняя", "low":"🪫 Низкая"}.get(lvl, lvl)

def energy_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🔋 Высокая", callback_data="energy:high"),
        types.InlineKeyboardButton("😐 Средняя", callback_data="energy:mid"),
        types.InlineKeyboardButton("🪫 Низкая", callback_data="energy:low"),
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
    }.get(t, t or "—")

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
        types.InlineKeyboardButton("⏸ Отложить 10 минут", callback_data="result:delay10"),
        types.InlineKeyboardButton("🔁 Заново", callback_data="result:restart"),
    )
    return kb

def delayed_control_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Я начал", callback_data="delayctl:started"),
        types.InlineKeyboardButton("🕒 Позже сделаю", callback_data="delayctl:later"),
        types.InlineKeyboardButton("🔁 Заново", callback_data="delayctl:restart"),
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
# AI pick (OpenAI)
# =========================
def ai_pick_best_action(energy: str, actions: list[dict]) -> str | None:
    """
    actions: [{"name":..., "type":..., "scores":{...}}]
    return: action name or None
    """
    if not openai_client:
        return None

    try:
        payload = {
            "energy": energy,
            "instruction": (
                "Выбери ОДНО действие, которое лучше всего сделать прямо сейчас. "
                "Учитывай энергию и тип действий. Верни ТОЛЬКО точное название одного действия из списка."
            ),
            "actions": [
                {
                    "name": a["name"],
                    "type": a.get("type"),
                    "scores": a.get("scores", {}),
                } for a in actions
            ]
        }

        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты ассистент по продуктивности. Отвечай максимально коротко и точно."},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
            ],
            temperature=0.2
        )

        answer = (resp.choices[0].message.content or "").strip()
        # строгая проверка: совпадение с названием из списка
        names = [a["name"] for a in actions]
        for n in names:
            if n.lower() == answer.lower():
                return n
        # мягкая проверка: если модель добавила символы
        for n in names:
            if n.lower() in answer.lower():
                return n

        return None

    except Exception as e:
        print("AI ERROR:", e)
        return None

# =========================
# Local scoring fallback
# =========================
def energy_weight(level: str) -> float:
    return {"low": 2.0, "mid": 1.0, "high": 0.6}.get(level, 1.0)

def local_pick_best(data: dict) -> dict:
    lvl = data.get("energy_now", "mid")
    ew = energy_weight(lvl)

    for a in data["actions"]:
        s = a["scores"]
        energy_bonus = 6 - s["energy"]  # 1 легко -> 5 бонус
        a["total"] = (
            s["influence"] * 2 +
            s["urgency"] * 2 +
            s["meaning"] * 1 +
            energy_bonus * ew
        )

    return max(data["actions"], key=lambda x: x["total"])

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

    # Лимит в день
    if not can_start_today(chat_id):
        bot.send_message(chat_id, "⛔ Сегодня уже был 1 выбор.\nЗавтра можно снова.", reply_markup=menu_kb())
        return

    reset_session(chat_id)

    # меню можно показать, но дальше мы его уберём
    bot.send_message(chat_id, "Меню:", reply_markup=menu_kb())

    msg = bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    user_data[chat_id]["energy_msg_id"] = msg.message_id

@bot.message_handler(commands=["help"])
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        "Я помогаю выбрать ОДНО главное действие.\n\n"
        "1) /start или 🚀 Начать\n"
        "2) Выбери энергию (фиксируется)\n"
        "3) Напиши 3–7 действий\n"
        "4) Для каждого действия выбери тип (фиксируется)\n"
        "5) Оцени по 4 критериям (фиксируется)\n"
        "6) Получишь результат + кнопки управления\n\n"
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
# ENERGY (LOCKED)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data:
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    if data["energy_msg_id"] and call.message.message_id != data["energy_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    if data["energy_locked"]:
        bot.answer_callback_query(call.id, "✅ Энергия уже выбрана")
        return

    lvl = call.data.split(":")[1]
    data["energy_now"] = lvl
    data["energy_locked"] = True
    data["step"] = "actions"

    # скрыть меню снизу, чтобы не мешало
    bot.send_message(chat_id, "✅ Принято", reply_markup=remove_menu())

    # убрать кнопки энергии + показать выбор
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ Энергия: <b>{energy_label(lvl)}</b>"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "Напиши 3–7 действий, каждое с новой строки.", reply_markup=remove_menu())

# =========================
# ACTIONS INPUT
# =========================
@bot.message_handler(func=lambda m: m.chat.id in user_data and user_data[m.chat.id].get("step") == "actions")
def get_actions(message):
    chat_id = message.chat.id
    data = user_data[chat_id]

    lines = [a.strip() for a in message.text.split("\n") if a.strip()]
    if not 3 <= len(lines) <= 7:
        bot.send_message(chat_id, "Нужно 3–7 действий. Каждое с новой строки.", reply_markup=remove_menu())
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
        reply_markup=action_type_kb()
    )
    data["expected_type_msg_id"] = msg.message_id

# =========================
# TYPE PICK (LOCKED + only latest message valid)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("atype:"))
def action_type_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "typing":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    # только актуальное сообщение
    if data["expected_type_msg_id"] and call.message.message_id != data["expected_type_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    # нельзя менять
    if call.message.message_id in data["answered_type_msgs"]:
        bot.answer_callback_query(call.id, "✅ Уже выбрано")
        return

    t = call.data.split(":")[1]
    a = data["actions"][data["cur_action"]]
    a["type"] = t

    data["answered_type_msgs"].add(call.message.message_id)

    # убрать кнопки и показать "действие — тип"
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ <b>{a['name']}</b> — <b>{type_label(t)}</b>"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id, "Готово ✅")

    data["cur_action"] += 1
    if data["cur_action"] >= len(data["actions"]):
        data["cur_action"] = 0
        data["cur_crit"] = 0
        data["step"] = "scoring"
        data["expected_score_msg_id"] = None
        data["answered_score_msgs"].clear()
        ask_next_score(chat_id)
    else:
        ask_action_type(chat_id)

# =========================
# SCORING (LOCKED)
# =========================
def ask_next_score(chat_id):
    data = user_data[chat_id]
    a = data["actions"][data["cur_action"]]
    key, title = CRITERIA[data["cur_crit"]]

    msg = bot.send_message(
        chat_id,
        f"Действие: <b>{a['name']}</b>\n"
        f"Тип: <b>{type_label(a['type'])}</b>\n\n"
        f"Оцени: <b>{title}</b> (1–5)\n"
        f"<i>{HINTS[key]}</i>",
        reply_markup=score_kb()
    )
    data["expected_score_msg_id"] = msg.message_id

@bot.callback_query_handler(func=lambda c: c.data.startswith("score:"))
def score_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "scoring":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    # только актуальное сообщение
    if data["expected_score_msg_id"] and call.message.message_id != data["expected_score_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    # нельзя менять
    if call.message.message_id in data["answered_score_msgs"]:
        bot.answer_callback_query(call.id, "✅ Уже выбрано")
        return

    score = int(call.data.split(":")[1])
    a = data["actions"][data["cur_action"]]
    key, title = CRITERIA[data["cur_crit"]]
    a["scores"][key] = score

    data["answered_score_msgs"].add(call.message.message_id)

    # убрать кнопки и показать зафиксированный ответ
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=(
                f"✅ <b>{a['name']}</b>\n"
                f"Тип: <b>{type_label(a['type'])}</b>\n"
                f"{title}: <b>{score}</b>"
            )
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id, "Ок ✅")

    data["cur_crit"] += 1
    if data["cur_crit"] >= len(CRITERIA):
        data["cur_crit"] = 0
        data["cur_action"] += 1

        if data["cur_action"] >= len(data["actions"]):
            show_result(chat_id)
            return

    ask_next_score(chat_id)

# =========================
# RESULT
# =========================
def show_result(chat_id):
    data = user_data[chat_id]
    data["step"] = "result"

    # 1) попытка ИИ
    ai_name = ai_pick_best_action(data.get("energy_now", "mid"), data["actions"])
    if ai_name:
        best = next(a for a in data["actions"] if a["name"] == ai_name)
        db_add_event(chat_id, "picked_ai", best["name"])
        header = "🤖 <b>ИИ выбрал главное действие:</b>"
    else:
        best = local_pick_best(data)
        db_add_event(chat_id, "picked_local", best["name"])
        header = "🔥 <b>Главное действие сейчас:</b>"

    data["focus"] = best["name"]

    # daily limit (1 раз)
    if not data["picked_logged"]:
        db_inc_picks_today(chat_id)
        data["picked_logged"] = True

    text = (
        f"{header}\n\n"
        f"<b>{best['name']}</b>\n"
        f"Тип: <b>{type_label(best.get('type'))}</b>\n\n"
        "Сделай первый шаг за 2–5 минут (без идеала)."
    )

    msg = bot.send_message(chat_id, text, reply_markup=result_kb())
    data["result_msg_id"] = msg.message_id

# =========================
# RESULT BUTTONS (WORKING)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("result:"))
def result_actions(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "result":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    # принимаем только актуальное result-сообщение
    if data.get("result_msg_id") and call.message.message_id != data["result_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    cmd = call.data.split(":")[1]
    focus = data.get("focus", "это действие")

    # всегда фиксируем: после нажатия — убрать кнопки у результата
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "restart":
        bot.answer_callback_query(call.id, "Ок")
        cancel_timers(chat_id)
        # внутренний рестарт (без проверки лимита)
        reset_session(chat_id)
        bot.send_message(chat_id, "🔁 Ок, начнём заново. Твоя энергия сейчас?", reply_markup=energy_kb())
        user_data[chat_id]["energy_msg_id"] = call.message.message_id + 1  # не идеально, но не мешает
        return

    if cmd == "started":
        bot.answer_callback_query(call.id, "🔥 Погнали")
        cancel_timers(chat_id)

        db_add_event(chat_id, "started", focus)

        # обновим текст результата
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=f"✅ Ты начал: <b>{focus}</b>\n\nЧерез 5 минут спрошу, как идёт."
            )
        except Exception:
            pass

        def coach():
            try:
                bot.send_message(chat_id, "Как идёт?", reply_markup=coach_kb())
            except Exception:
                pass

        t = threading.Timer(5 * 60, coach)
        timers.setdefault(chat_id, {})["coach"] = t
        t.start()
        return

    if cmd == "delay10":
        bot.answer_callback_query(call.id, "Ок")

        data["step"] = "delayed"

        # обновим текст результата
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=f"⏸ Отложено на 10 минут: <b>{focus}</b>\n\nЯ напомню через 10 минут."
            )
        except Exception:
            pass

        # напоминание таймером
        def remind():
            try:
                bot.send_message(chat_id, f"⏰ Напоминание: <b>{focus}</b>")
                db_add_event(chat_id, "reminder_sent", focus)
            except Exception:
                pass

        cancel_timers(chat_id)
        t = threading.Timer(10 * 60, remind)
        timers.setdefault(chat_id, {})["reminder"] = t
        t.start()

        db_add_event(chat_id, "delayed_10m", focus)

        # управление после задержки (и ДО истечения 10 минут) — как ты хотел
        ctl = bot.send_message(
            chat_id,
            "Выбери, что дальше:",
            reply_markup=delayed_control_kb()
        )
        data["delayed_control_msg_id"] = ctl.message_id
        return

# =========================
# DELAY CONTROL (started/later/restart)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("delayctl:"))
def delay_control(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "delayed":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    if data.get("delayed_control_msg_id") and call.message.message_id != data["delayed_control_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    focus = data.get("focus", "это действие")
    cmd = call.data.split(":")[1]

    # убрать кнопки
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "started":
        bot.answer_callback_query(call.id, "🔥 Погнали")
        # отменяем напоминание
        try:
            timers.get(chat_id, {}).get("reminder") and timers[chat_id]["reminder"].cancel()
        except Exception:
            pass

        data["step"] = "coaching"

        db_add_event(chat_id, "started_after_delay", focus)

        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=f"✅ Ты начал: <b>{focus}</b>\n\nЧерез 5 минут спрошу, как идёт."
            )
        except Exception:
            pass

        def coach():
            try:
                bot.send_message(chat_id, "Как идёт?", reply_markup=coach_kb())
            except Exception:
                pass

        t = threading.Timer(5 * 60, coach)
        timers.setdefault(chat_id, {})["coach"] = t
        t.start()
        return

    if cmd == "later":
        bot.answer_callback_query(call.id, "Ок")
        # отменяем напоминание
        try:
            timers.get(chat_id, {}).get("reminder") and timers[chat_id]["reminder"].cancel()
        except Exception:
            pass

        data["step"] = "idle"
        db_add_event(chat_id, "later_done", focus)

        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=f"🕒 Ок, сделаешь позже: <b>{focus}</b>\n\nХочешь — можешь начать заново из меню."
            )
        except Exception:
            pass

        # возвращаем меню (теперь сценарий завершён)
        bot.send_message(chat_id, "Меню:", reply_markup=menu_kb())
        return

    if cmd == "restart":
        bot.answer_callback_query(call.id, "Ок")
        cancel_timers(chat_id)
        reset_session(chat_id)
        bot.send_message(chat_id, "🔁 Ок, начнём заново. Твоя энергия сейчас?", reply_markup=energy_kb())
        return

# =========================
# COACH ANSWER
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("coach:"))
def coach_answer(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)
    focus = (data or {}).get("focus")

    ans = call.data.split(":")[1]
    bot.answer_callback_query(call.id)

    db_add_event(chat_id, f"coach_{ans}", focus)

    # после коуча — завершаем сценарий и возвращаем меню
    if ans == "norm":
        bot.send_message(chat_id, "👍 Хорошо. Продолжай ещё 10 минут или доведи до мини-результата.", reply_markup=menu_kb())
    elif ans == "hard":
        bot.send_message(chat_id, "😵 Упрости в 2 раза и начни с 2 минут. Главное — движение.", reply_markup=menu_kb())
    else:
        bot.send_message(chat_id, "❌ Ок. Можно выбрать самый маленький шаг или начать заново.", reply_markup=menu_kb())

    if data:
        data["step"] = "idle"

# =========================
# RUN
# =========================
if __name__ == "__main__":
    db_init()
    print("Бот запущен")
    bot.infinity_polling(skip_pending=True)
