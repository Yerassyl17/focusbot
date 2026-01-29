import os
import json
import threading
import sqlite3
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types

# Gemini (Google Gen AI SDK)
# pip install -U google-genai
from google import genai  # :contentReference[oaicite:2]{index=2}

# =========================
# CONFIG
# =========================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise ValueError("BOT_TOKEN is not set. Add it in Railway/Render Variables.")

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is not set. Add it in Railway/Render Variables.")

bot = telebot.TeleBot(TOKEN)
gemini = genai.Client(api_key=GEMINI_API_KEY)  # :contentReference[oaicite:3]{index=3}

UNLIMITED_MODE = False
ADMIN_IDS = {8311003582}  # твой chat_id
KZ_TZ = timezone(timedelta(hours=5))

# Gemini model (можно поменять при желании)
GEMINI_MODEL = "gemini-2.0-flash"

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
timers = {}      # chat_id -> {"reminder": Timer|None, "coach": Timer|None}

def reset_session(chat_id):
    user_data[chat_id] = {
        "state": "energy",          # energy -> actions -> typing -> ai_result -> done
        "energy": None,

        # lock messages
        "energy_msg_id": None,
        "energy_locked": False,

        "actions": [],              # [{"name": str, "type": str|None}]
        "cur_action": 0,

        "expected_type_msg_id": None,
        "answered_type_msgs": set(),

        # result
        "focus": None,
        "focus_type": None,
        "result_msg_id": None,
        "result_locked": False,
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
# UI HELPERS
# =========================
def type_label(t: str) -> str:
    return {
        "mental": "🧠 Умственное",
        "physical": "💪 Физическое",
        "routine": "🗂 Рутинное",
        "social": "💬 Общение",
    }.get(t or "", t or "—")

def energy_label(e: str) -> str:
    return {"high": "🔋 Высокая", "mid": "😐 Средняя", "low": "🪫 Низкая"}.get(e or "", e or "—")

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

def result_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Я начал", callback_data="result:started"),
        types.InlineKeyboardButton("⏸ Отложить 10 минут", callback_data="result:delay"),
    )
    kb.row(
        types.InlineKeyboardButton("🔁 Заново", callback_data="result:restart"),
    )
    return kb

def delay_kb():
    # появляется ПОСЛЕ "Отложить", чтобы можно было отметить "Я начал" даже до напоминания
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Я начал", callback_data="delay:started"),
        types.InlineKeyboardButton("⏭ Попозже (ещё 10 минут)", callback_data="delay:more"),
    )
    kb.row(
        types.InlineKeyboardButton("🔁 Заново", callback_data="delay:restart"),
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
# GEMINI LOGIC
# =========================
def gemini_pick_best(energy: str, actions: list[dict]) -> dict:
    """
    actions: [{"name": "...", "type": "mental|physical|routine|social"}]
    returns:
      {"best_index": int, "first_step": str, "why": str}
    """
    payload = {
        "energy": energy,
        "actions": actions,
        "instruction": (
            "Ты productivity-коуч. Выбери ОДНО лучшее действие на ближайшие 10-30 минут.\n"
            "Учитывай энергию: low=бережно, high=можно сложнее.\n"
            "Верни строго JSON: {best_index:int, first_step:string, why:string}.\n"
            "first_step = очень маленький шаг (2-5 минут). why = 1-2 предложения.\n"
            "Никаких лишних ключей, только эти 3."
        )
    }

    resp = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=[json.dumps(payload, ensure_ascii=False)]
    )

    text = getattr(resp, "text", "") or ""
    # иногда модель оборачивает в ```json ... ```
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()

    try:
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("Gemini returned non-dict")
        if "best_index" not in data or "first_step" not in data or "why" not in data:
            raise ValueError("Gemini returned wrong keys")
        return data
    except Exception:
        # fallback: простейшая логика
        return {
            "best_index": 0,
            "first_step": "Сделай самый маленький шаг: подготовь всё на 2 минуты.",
            "why": "Gemini не вернул корректный JSON, использую запасной вариант."
        }

# =========================
# COMMANDS
# =========================
@bot.message_handler(commands=["start"])
def start_cmd(message):
    chat_id = message.chat.id
    cancel_timers(chat_id)

    if not can_start_today(chat_id):
        bot.send_message(chat_id, "⛔ Сегодня уже был 1 выбор.\nЗавтра можно снова.")
        return

    reset_session(chat_id)

    # НЕ показываем меню — только сценарий
    msg = bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    user_data[chat_id]["energy_msg_id"] = msg.message_id

@bot.message_handler(commands=["help"])
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        "Команды:\n"
        "/start — начать выбор\n"
        "/stats — статистика\n\n"
        "Сценарий:\n"
        "1) Выбираешь энергию (фиксируется)\n"
        "2) Пишешь 3–7 действий (каждое с новой строки)\n"
        "3) Для каждого действия выбираешь тип (фиксируется)\n"
        "4) Gemini выбирает лучшее действие и даёт первый шаг 🤖"
    )

@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    chat_id = message.chat.id
    picks = db_get_picks_today(chat_id)
    bot.send_message(chat_id, f"Сегодня выборов: {picks}")

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

    # принимаем только по последнему energy_msg_id
    if data["energy_msg_id"] and call.message.message_id != data["energy_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    if data["energy_locked"]:
        bot.answer_callback_query(call.id, "✅ Энергия уже выбрана")
        return

    lvl = call.data.split(":")[1]
    data["energy"] = lvl
    data["energy_locked"] = True
    data["state"] = "actions"

    # убираем кнопки + пишем выбранное
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    try:
        bot.edit_message_text(
            f"✅ Энергия: <b>{energy_label(lvl)}</b>",
            chat_id,
            call.message.message_id,
            parse_mode="HTML"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "Напиши 3–7 действий, каждое с новой строки.")

# =========================
# ACTIONS INPUT
# =========================
@bot.message_handler(func=lambda m: m.chat.id in user_data and user_data[m.chat.id].get("state") == "actions")
def get_actions(message):
    chat_id = message.chat.id
    data = user_data[chat_id]

    lines = [a.strip() for a in (message.text or "").split("\n") if a.strip()]
    if not 3 <= len(lines) <= 7:
        bot.send_message(chat_id, "Нужно 3–7 действий. Каждое с новой строки.")
        return

    data["actions"] = [{"name": a, "type": None} for a in lines]
    data["cur_action"] = 0
    data["state"] = "typing"
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
    data["expected_type_msg_id"] = msg.message_id

# =========================
# TYPE PICK (LOCKED + VISUAL)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("atype:"))
def action_type_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)
    if not data or data.get("state") != "typing":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    # только актуальное сообщение
    if data["expected_type_msg_id"] and call.message.message_id != data["expected_type_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    # нельзя переответить
    if call.message.message_id in data["answered_type_msgs"]:
        bot.answer_callback_query(call.id, "✅ Уже выбрано")
        return

    t = call.data.split(":")[1]
    a = data["actions"][data["cur_action"]]
    a["type"] = t
    data["answered_type_msgs"].add(call.message.message_id)

    # убрать кнопки + показать "Действие — Тип"
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    try:
        bot.edit_message_text(
            f"✅ <b>{a['name']}</b> — <b>{type_label(t)}</b>",
            chat_id,
            call.message.message_id,
            parse_mode="HTML"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id, "Готово ✅")

    # следующее действие или AI-результат
    data["cur_action"] += 1
    if data["cur_action"] >= len(data["actions"]):
        data["state"] = "ai_result"
        show_ai_result(chat_id)
    else:
        ask_action_type(chat_id)

# =========================
# AI RESULT
# =========================
def show_ai_result(chat_id):
    data = user_data[chat_id]

    # лимит "1 выбор в день" фиксируем ТОЛЬКО когда дошли до результата
    db_inc_picks_today(chat_id)

    actions = data["actions"]
    energy = data["energy"]

    pick = gemini_pick_best(energy=energy, actions=actions)
    idx = int(pick.get("best_index", 0))
    idx = max(0, min(idx, len(actions) - 1))

    best = actions[idx]
    data["focus"] = best["name"]
    data["focus_type"] = best["type"]
    data["state"] = "done"
    data["result_locked"] = False

    db_add_event(chat_id, "picked_ai", best["name"])

    text = (
        "🔥 <b>Главное действие сейчас:</b>\n\n"
        f"<b>{best['name']}</b>\n"
        f"Тип: <b>{type_label(best['type'])}</b>\n\n"
        f"🚀 <b>Первый шаг (2–5 минут):</b>\n{pick.get('first_step','')}\n\n"
        f"🧩 <i>{pick.get('why','')}</i>"
    )

    msg = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=result_kb())
    data["result_msg_id"] = msg.message_id

# =========================
# RESULT BUTTONS (LOCK + HIDE)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("result:"))
def result_actions(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)
    if not data or data.get("state") != "done":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    # только актуальное result-сообщение
    if data["result_msg_id"] and call.message.message_id != data["result_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    if data["result_locked"]:
        bot.answer_callback_query(call.id, "✅ Уже обработано")
        return

    cmd = call.data.split(":")[1]
    focus = data.get("focus") or "действие"
    ftype = type_label(data.get("focus_type"))

    # lock + hide buttons
    data["result_locked"] = True
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    bot.answer_callback_query(call.id)

    if cmd == "restart":
        # полный рестарт
        bot.send_message(chat_id, "Ок, начнём заново. Нажми /start")
        return

    if cmd == "delay":
        db_add_event(chat_id, "delayed_10m", focus)

        # обновим текст результата
        try:
            bot.edit_message_text(
                f"⏸ Отложено на 10 минут:\n<b>{focus}</b>\nТип: <b>{ftype}</b>",
                chat_id,
                call.message.message_id,
                parse_mode="HTML"
            )
        except Exception:
            pass

        # запланировать напоминание
        def remind():
            try:
                bot.send_message(chat_id, f"⏰ Напоминание:\n<b>{focus}</b>\nТип: <b>{ftype}</b>", parse_mode="HTML")
                db_add_event(chat_id, "reminder_sent", focus)
            except Exception:
                pass

        cancel_timers(chat_id)
        t = threading.Timer(10 * 60, remind)
        timers[chat_id]["reminder"] = t
        t.start()

        # ВАЖНО: вместо меню — показываем “Я начал / Попозже / Заново”
        bot.send_message(chat_id, "Ок, напомню через 10 минут.", reply_markup=delay_kb())
        return

    if cmd == "started":
        db_add_event(chat_id, "started", focus)

        # обновим текст результата
        try:
            bot.edit_message_text(
                f"✅ Начал:\n<b>{focus}</b>\nТип: <b>{ftype}</b>\n\nЧерез 5 минут спрошу, как идёт.",
                chat_id,
                call.message.message_id,
                parse_mode="HTML"
            )
        except Exception:
            pass

        # таймер коуча
        def coach():
            try:
                bot.send_message(chat_id, "Как идёт?", reply_markup=coach_kb())
            except Exception:
                pass

        cancel_timers(chat_id)
        t = threading.Timer(5 * 60, coach)
        timers[chat_id]["coach"] = t
        t.start()

        # не показываем меню
        return

# =========================
# AFTER DELAY CONTROLS
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("delay:"))
def delay_actions(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)
    if not data:
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    focus = data.get("focus") or "действие"
    ftype = type_label(data.get("focus_type"))

    cmd = call.data.split(":")[1]

    # убираем кнопки на сообщении "Ок, напомню..."
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    bot.answer_callback_query(call.id)

    if cmd == "restart":
        bot.send_message(chat_id, "Ок, начнём заново. Нажми /start")
        return

    if cmd == "more":
        db_add_event(chat_id, "delayed_more_10m", focus)

        def remind():
            try:
                bot.send_message(chat_id, f"⏰ Напоминание:\n<b>{focus}</b>\nТип: <b>{ftype}</b>", parse_mode="HTML")
                db_add_event(chat_id, "reminder_sent", focus)
            except Exception:
                pass

        cancel_timers(chat_id)
        t = threading.Timer(10 * 60, remind)
        timers[chat_id]["reminder"] = t
        t.start()

        bot.send_message(chat_id, "Ок, ещё +10 минут. Если начнёшь раньше — нажми ✅", reply_markup=delay_kb())
        return

    if cmd == "started":
        db_add_event(chat_id, "started_after_delay", focus)

        bot.send_message(
            chat_id,
            f"✅ Начал:\n<b>{focus}</b>\nТип: <b>{ftype}</b>\n\nЧерез 5 минут спрошу, как идёт.",
            parse_mode="HTML"
        )

        def coach():
            try:
                bot.send_message(chat_id, "Как идёт?", reply_markup=coach_kb())
            except Exception:
                pass

        cancel_timers(chat_id)
        t = threading.Timer(5 * 60, coach)
        timers[chat_id]["coach"] = t
        t.start()
        return

# =========================
# COACH ANSWER
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("coach:"))
def coach_answer(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)
    if not data:
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    ans = call.data.split(":")[1]
    focus = data.get(chat_id, {}).get("focus") if isinstance(data, dict) else None

    bot.answer_callback_query(call.id)
    db_add_event(chat_id, f"coach_{ans}", focus)

    # убираем кнопки коуча
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if ans == "norm":
        bot.send_message(chat_id, "👍 Отлично. Продолжай ещё 10 минут или доведи до мини-результата.")
    elif ans == "hard":
        bot.send_message(chat_id, "😵 Упрости в 2 раза и начни с 2 минут. Главное — движение.")
    else:
        bot.send_message(chat_id, "Ок. Можно выбрать самый маленький шаг или начать заново: /start")

# =========================
# RUN
# =========================
if __name__ == "__main__":
    db_init()
    print("Бот запущен")
    bot.infinity_polling(skip_pending=True)
