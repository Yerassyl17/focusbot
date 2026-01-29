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
# SESSION + TIMERS
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
        # flow states: idle -> energy -> actions -> typing -> scoring -> result / delayed / started
        "step": "idle",

        "energy_now": None,
        "energy_msg_id": None,
        "energy_locked": False,

        "actions": [],
        "cur_action": 0,
        "cur_crit": 0,

        "expected_type_msg_id": None,
        "answered_type_msgs": set(),

        "focus": None,
        "result_locked": False,   # чтобы на результат не нажимали дважды
        "result_msg_id": None,    # id сообщения "Главное действие..."

        "last_result_at": None,
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
def hide_kb():
    return types.ReplyKeyboardRemove(selective=False)


def menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать", "📊 Статистика")
    kb.row("❓ Как пользоваться")
    return kb


def result_reply_kb(full=True):
    """
    full=True  -> ✅ Я начал / ⏸ Отложить / 🕒 Попозже / 🔁 Заново
    full=False -> 🕒 Попозже / 🔁 Заново  (например после 'Отложить 10 минут')
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if full:
        kb.row("✅ Я начал", "⏸ Отложить 10 минут")
        kb.row("🕒 Попозже сделаю", "🔁 Заново")
    else:
        kb.row("🕒 Попозже сделаю", "🔁 Заново")
    return kb


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


def score_kb():
    kb = types.InlineKeyboardMarkup(row_width=5)
    kb.add(*[
        types.InlineKeyboardButton(str(i), callback_data=f"score:{i}")
        for i in range(1, 6)
    ])
    return kb


def type_label(t: str) -> str:
    return {
        "mental": "🧠 Умственное",
        "physical": "💪 Физическое",
        "routine": "🗂 Рутинное",
        "social": "💬 Общение",
    }.get(t, t)


def energy_label(lvl: str) -> str:
    return {"high": "🔋 Высокая", "mid": "😐 Средняя", "low": "🪫 Низкая"}.get(lvl, lvl)


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
    data = user_data[chat_id]
    data["step"] = "energy"

    # убираем нижнее меню на время сценария
    bot.send_message(chat_id, "Запускаю выбор ✅", reply_markup=hide_kb())

    msg = bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    data["energy_msg_id"] = msg.message_id


@bot.message_handler(commands=["help"])
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        "Я помогу выбрать ОДНО главное действие.\n\n"
        "1) /start или 🚀 Начать\n"
        "2) Выбираешь энергию\n"
        "3) Пишешь 3–7 действий (каждое с новой строки)\n"
        "4) Для каждого действия выбираешь тип\n"
        "5) Оцениваешь по 4 критериям (1–5)\n\n"
        "После результата управление снизу: ✅ Я начал / ⏸ Отложить / 🕒 Попозже / 🔁 Заново",
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

    if not data or data.get("step") != "energy":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    # только на актуальное сообщение энергии
    if data["energy_msg_id"] is not None and call.message.message_id != data["energy_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    if data["energy_locked"]:
        bot.answer_callback_query(call.id, "✅ Энергия уже выбрана")
        return

    lvl = call.data.split(":")[1]
    data["energy_now"] = lvl
    data["energy_locked"] = True
    data["step"] = "actions"

    # убираем кнопки энергии и фиксируем текст
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ Энергия: <b>{energy_label(lvl)}</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "Напиши 3–7 действий, каждое с новой строки.", reply_markup=hide_kb())


# =========================
# ACTIONS INPUT
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

    # только это сообщение теперь можно “отвечать”
    data["expected_type_msg_id"] = msg.message_id


@bot.callback_query_handler(func=lambda c: c.data.startswith("atype:"))
def action_type_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "typing":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    # старые сообщения не принимаем
    if data["expected_type_msg_id"] is not None and call.message.message_id != data["expected_type_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    if call.message.message_id in data["answered_type_msgs"]:
        bot.answer_callback_query(call.id, "✅ Уже выбрано")
        return

    t = call.data.split(":")[1]
    a = data["actions"][data["cur_action"]]
    a["type"] = t

    data["answered_type_msgs"].add(call.message.message_id)

    # убираем кнопки + показываем итог рядом с действием
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

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
    data = user_data.get(chat_id)

    if not data or data.get("step") != "scoring":
        bot.answer_callback_query(call.id, "Нажми /start")
        return

    score = int(call.data.split(":")[1])
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
# RESULT + CONTROL (REPLY KEYBOARD)
# =========================
def energy_weight(level: str) -> float:
    return {"low": 2.0, "mid": 1.0, "high": 0.6}.get(level, 1.0)


def show_result(chat_id):
    data = user_data[chat_id]
    lvl = data.get("energy_now", "mid")
    ew = energy_weight(lvl)

    for a in data["actions"]:
        s = a["scores"]
        energy_bonus = 6 - s["energy"]  # 1 легко -> бонус 5
        a["total"] = (
            s["influence"] * 2 +
            s["urgency"] * 2 +
            s["meaning"] * 1 +
            energy_bonus * ew
        )

    best = max(data["actions"], key=lambda x: x["total"])
    data["focus"] = best["name"]
    data["step"] = "result"
    data["result_locked"] = False

    db_add_event(chat_id, "picked", best["name"])
    db_inc_picks_today(chat_id)

    msg = bot.send_message(
        chat_id,
        "🔥 <b>Главное действие сейчас:</b>\n\n"
        f"<b>{best['name']}</b>\n"
        f"Тип: <b>{type_label(best['type'])}</b>\n\n"
        "Сделай первый шаг за 2–5 минут (без идеала).",
        parse_mode="HTML",
        reply_markup=result_reply_kb(full=True)  # ВАЖНО: управление снизу
    )
    data["result_msg_id"] = msg.message_id


def lock_result_controls(chat_id, next_kb):
    """
    Убирает/заменяет нижнюю клавиатуру так, чтобы нельзя было нажимать старые варианты.
    """
    try:
        bot.send_message(chat_id, " ", reply_markup=next_kb)
    except Exception:
        pass


@bot.message_handler(func=lambda m: m.chat.id in user_data and m.text in [
    "✅ Я начал", "⏸ Отложить 10 минут", "🕒 Попозже сделаю", "🔁 Заново"
])
def result_reply_handler(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id)
    if not data:
        bot.send_message(chat_id, "Нажми /start", reply_markup=menu_kb())
        return

    # Разрешаем эти кнопки только в result/delayed/started (не в процессе ввода/оценок)
    if data.get("step") not in ("result", "delayed", "started"):
        bot.send_message(chat_id, "Сначала дойди до результата 🙂", reply_markup=hide_kb())
        return

    focus = data.get("focus") or "это действие"

    # защита от двойных нажатий
    if data.get("result_locked") and message.text != "🔁 Заново":
        bot.send_message(chat_id, "✅ Уже принято", reply_markup=menu_kb())
        return

    if message.text == "🔁 Заново":
        # перезапуск — всегда разрешён
        db_add_event(chat_id, "restart", focus)
        cancel_timers(chat_id)
        start_cmd(message)
        return

    if message.text == "🕒 Попозже сделаю":
        data["result_locked"] = True
        data["step"] = "idle"
        db_add_event(chat_id, "postpone_free", focus)

        cancel_timers(chat_id)
        bot.send_message(chat_id, "Ок 👍 Сделаешь позже. Если захочешь — жми 🚀 Начать.", reply_markup=menu_kb())
        return

    if message.text == "⏸ Отложить 10 минут":
        data["result_locked"] = True
        data["step"] = "delayed"
        db_add_event(chat_id, "delayed_10m", focus)

        cancel_timers(chat_id)

        def remind():
            try:
                bot.send_message(
                    chat_id,
                    f"⏰ Напоминание:\n<b>{focus}</b>\n\nГотов начать? 🙂",
                    parse_mode="HTML",
                    reply_markup=result_reply_kb(full=True)
                )
                # после напоминания снова можно выбирать
                if chat_id in user_data:
                    user_data[chat_id]["step"] = "result"
                    user_data[chat_id]["result_locked"] = False
                db_add_event(chat_id, "reminder_sent", focus)
            except Exception:
                pass

        t = threading.Timer(10 * 60, remind)
        timers[chat_id]["reminder"] = t
        t.start()

        # ВАЖНО: сразу убираем варианты ✅Я начал / ⏸ / ...
        # оставляем только "попозже" и "заново" пока 10 минут не прошло
        bot.send_message(chat_id, "Ок, напомню через 10 минут.", reply_markup=result_reply_kb(full=False))
        return

    if message.text == "✅ Я начал":
        data["result_locked"] = True
        data["step"] = "started"
        db_add_event(chat_id, "started", focus)

        cancel_timers(chat_id)
        bot.send_message(chat_id, "Отлично! Через 5 минут спрошу, как идёт.", reply_markup=hide_kb())

        def coach():
            try:
                bot.send_message(chat_id, "Как идёт?", reply_markup=coach_inline_kb())
            except Exception:
                pass

        t = threading.Timer(5 * 60, coach)
        timers[chat_id]["coach"] = t
        t.start()
        return


# =========================
# COACH (INLINE)
# =========================
def coach_inline_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("👍 Норм", callback_data="coach:norm"),
        types.InlineKeyboardButton("😵 Тяжело", callback_data="coach:hard"),
        types.InlineKeyboardButton("❌ Бросил", callback_data="coach:quit"),
    )
    return kb


@bot.callback_query_handler(func=lambda c: c.data.startswith("coach:"))
def coach_answer(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id, {})
    ans = call.data.split(":")[1]
    focus = data.get("focus")

    bot.answer_callback_query(call.id)
    db_add_event(chat_id, f"coach_{ans}", focus)

    # убираем кнопки, чтобы не нажимали повторно
    try:
        bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if ans == "norm":
        bot.send_message(chat_id, "Хорошо. Продолжай ещё 10 минут или доведи до мини-результата ✅", reply_markup=menu_kb())
    elif ans == "hard":
        bot.send_message(chat_id, "Упрости в 2 раза и начни с 2 минут. Главное — движение 💪", reply_markup=menu_kb())
    else:
        bot.send_message(chat_id, "Ок. Можно выбрать самый маленький шаг или начать заново 🔁", reply_markup=menu_kb())

    # возвращаемся в idle (меню)
    if chat_id in user_data:
        user_data[chat_id]["step"] = "idle"


# =========================
# FALLBACK: если пишут не в тот момент
# =========================
@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id)

    # если вообще нет сессии — показываем меню
    if not data:
        bot.send_message(chat_id, "Выбери:", reply_markup=menu_kb())
        return

    # если пользователь в процессе, но пишет что-то не по шагу
    if data.get("step") in ("energy", "typing", "scoring"):
        bot.send_message(chat_id, "Следуй шагам 🙂", reply_markup=hide_kb())
        return

    # idle
    bot.send_message(chat_id, "Выбери:", reply_markup=menu_kb())


# =========================
# RUN
# =========================
if __name__ == "__main__":
    db_init()
    print("Бот запущен")
    bot.infinity_polling(skip_pending=True)
