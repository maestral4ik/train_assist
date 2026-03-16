import os
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import database as db
import ai

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def _ascii_weight_chart(history: list) -> str:
    if not history:
        return "Нет данных о весе."

    weights = [r["weight"] for r in history]
    dates = [r["date"] for r in history]
    min_w = min(weights)
    max_w = max(weights)
    rows = 6
    cols = len(weights)

    # Build grid
    lines = []
    for row in range(rows, 0, -1):
        threshold = min_w + (max_w - min_w) * (row / rows)
        label = f"{threshold:5.1f} |"
        bar_row = ""
        for w in weights:
            if max_w == min_w:
                bar_row += " * "
            elif w >= threshold - (max_w - min_w) / rows:
                bar_row += " * "
            else:
                bar_row += "   "
        lines.append(label + bar_row)

    lines.append("      " + "-" * (cols * 3))
    # Date labels (last 2 chars of each date)
    lines.append("      " + "".join(f" {d[-5:]} "[0:3] for d in dates))

    first = history[0]["date"]
    last = history[-1]["date"]
    delta = weights[-1] - weights[0]
    sign = "+" if delta > 0 else ""
    lines.append(f"\nС {first} по {last}: {sign}{delta:.1f} кг")
    return "\n".join(lines)


# ── Command handlers ────────────────────────────────────────────────────────

async def reminder_tick(context: ContextTypes.DEFAULT_TYPE):
    tz = context.bot_data["tz"]
    hhmm = datetime.now(tz).strftime("%H:%M")
    users = db.get_users_for_reminder(hhmm)
    for u in users:
        uid = u["user_id"]
        try:
            if u["reminder_morning"] == hhmm:
                await context.bot.send_message(
                    uid, "☀️ Доброе утро! Не забудь записать завтрак. Что сегодня ел?"
                )
            if u["reminder_lunch"] == hhmm:
                s = db.get_today_summary(uid)
                if s["eaten"] == 0:
                    text = "🍽 Время обеда! Не забудь записать что ел сегодня."
                else:
                    text = f"🍽 Время обеда! Сегодня уже записано {s['eaten']} ккал из {s['limit']} ккал."
                await context.bot.send_message(uid, text)
            if u["reminder_evening"] == hhmm:
                s = db.get_today_summary(uid)
                balance_sign = "+" if s["balance"] > 0 else ""
                if s["eaten"] == 0:
                    text = "🌙 Привет! Кажется, ты сегодня ничего не записал. Расскажи, что ел?"
                elif s["balance"] > 0:
                    text = (
                        f"🌙 Итоги дня: съедено {s['eaten']} ккал, лимит {s['limit']} ккал.\n"
                        f"Баланс: {balance_sign}{s['balance']} ккал — лимит превышен. "
                        f"Может, сделаешь небольшую прогулку? 🚶"
                    )
                else:
                    text = (
                        f"🌙 Итоги дня: съедено {s['eaten']} ккал из {s['limit']} ккал. "
                        f"Отличная работа! 💪"
                    )
                await context.bot.send_message(uid, text)
        except Exception:
            pass


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    if not user or user["onboarding_step"] != "done":
        await update.message.reply_text("Сначала пройди регистрацию — напиши /start")
        return

    args = context.args
    if not args:
        m = user["reminder_morning"] or "10:50"
        l = user["reminder_lunch"]   or "14:00"
        e = user["reminder_evening"] or "20:00"
        await update.message.reply_text(
            f"🔔 *Текущие напоминания:*\n"
            f"☀️ Утро: {m}\n"
            f"🍽 Обед: {l}\n"
            f"🌙 Вечер: {e}\n\n"
            f"Чтобы изменить, напиши:\n`/reminders 9:00 13:00 19:30`",
            parse_mode="Markdown",
        )
        return

    if len(args) != 3:
        await update.message.reply_text("Укажи три времени: `/reminders 9:00 13:00 19:30`",
                                        parse_mode="Markdown")
        return

    import re
    pattern = re.compile(r"^\d{1,2}:\d{2}$")
    times = []
    for t in args:
        if not pattern.match(t):
            await update.message.reply_text(f"Неверный формат времени: `{t}`. Используй HH:MM",
                                            parse_mode="Markdown")
            return
        h, m = map(int, t.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            await update.message.reply_text(f"Недопустимое время: `{t}`", parse_mode="Markdown")
            return
        times.append(f"{h:02d}:{m:02d}")

    db.set_reminder_times(user_id, *times)
    await update.message.reply_text(
        f"✅ Напоминания обновлены:\n"
        f"☀️ Утро: {times[0]}\n"
        f"🍽 Обед: {times[1]}\n"
        f"🌙 Вечер: {times[2]}"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.upsert_user(user_id, onboarding_step="start")
    db.clear_messages(user_id)
    reply = await ai.chat(user_id, "Привет! Я хочу начать.")
    await update.message.reply_text(reply)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    if not user or user["onboarding_step"] != "done":
        await update.message.reply_text("Сначала пройди регистрацию — напиши /start")
        return

    s = db.get_today_summary(user_id)
    balance_sign = "+" if s["balance"] > 0 else ""
    status = "⚠️ Лимит превышен!" if s["balance"] > 0 else "✅ В пределах нормы"
    text = (
        f"📊 *Статистика за сегодня*\n\n"
        f"🍽 Съедено: {s['eaten']} ккал\n"
        f"🏃 Сожжено: {s['burned']} ккал\n"
        f"📉 Чистое: {s['net']} ккал\n"
        f"🎯 Лимит: {s['limit']} ккал\n"
        f"⚡ Баланс: {balance_sign}{s['balance']} ккал\n\n"
        f"{status}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    if not user or user["onboarding_step"] != "done":
        await update.message.reply_text("Сначала пройди регистрацию — напиши /start")
        return

    logs = db.get_today_logs(user_id)

    lines = ["📋 *Что ты ел сегодня:*\n"]
    if logs["food"]:
        for i, entry in enumerate(logs["food"], 1):
            lines.append(f"{i}. {entry['description']} — {entry['calories']} ккал")
        total = sum(e["calories"] for e in logs["food"])
        lines.append(f"\n*Итого съедено: {total} ккал*")
    else:
        lines.append("Пока ничего не записано.")

    if logs["activity"]:
        lines.append("\n🏃 *Активность:*\n")
        for i, entry in enumerate(logs["activity"], 1):
            lines.append(f"{i}. {entry['description']} — -{entry['calories_burned']} ккал")
        total_burned = sum(e["calories_burned"] for e in logs["activity"])
        lines.append(f"\n*Итого сожжено: {total_burned} ккал*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    history = db.get_weight_history(user_id, limit=10)
    chart = _ascii_weight_chart(history)
    await update.message.reply_text(f"📈 *История веса*\n\n```\n{chart}\n```",
                                    parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Бот-ассистент по похудению*\n\n"
        "Просто пиши мне в свободной форме:\n"
        "• «Съел кашу с молоком» → запишу еду\n"
        "• «Бегал 30 минут» → запишу активность\n"
        "• «Вешу 74.5» → запишу вес\n\n"
        "Поддерживаются голосовые сообщения 🎤\n\n"
        "*Команды:*\n"
        "/start — начать / перепройти регистрацию\n"
        "/stats — статистика за сегодня\n"
        "/history — что ел сегодня\n"
        "/progress — история веса\n"
        "/reminders — настроить время напоминаний\n"
        "/help — эта справка"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Message handlers ────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    # Ensure user row exists
    if not db.get_user(user_id):
        db.upsert_user(user_id)

    await update.message.chat.send_action("typing")
    try:
        reply = await ai.chat(user_id, text)
        await update.message.reply_text(reply)
    except Exception as e:
        log.exception("Error in handle_text")
        await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not db.get_user(user_id):
        db.upsert_user(user_id)

    await update.message.chat.send_action("typing")
    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        file_bytes = await tg_file.download_as_bytearray()

        transcription = await ai.transcribe_voice(bytes(file_bytes))
        log.info("Voice transcription for %d: %s", user_id, transcription)

        reply = await ai.chat(user_id, transcription)
        await update.message.reply_text(f"🎤 _{transcription}_\n\n{reply}",
                                        parse_mode="Markdown")
    except Exception as e:
        log.exception("Error in handle_voice")
        await update.message.reply_text("Не удалось обработать голосовое сообщение.")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    db.init_db()

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_TOKEN не задан в .env")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("history",   cmd_history))
    app.add_handler(CommandHandler("progress",  cmd_progress))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    from telegram import BotCommand
    tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
    app.bot_data["tz"] = tz

    async def set_commands(app):
        await app.bot.set_my_commands([
            BotCommand("start",       "Начать / перепройти регистрацию"),
            BotCommand("stats",       "Статистика за сегодня"),
            BotCommand("history",     "Что ел сегодня"),
            BotCommand("progress",    "История веса"),
            BotCommand("reminders",   "Настроить время напоминаний"),
            BotCommand("help",        "Справка"),
        ])
        app.job_queue.run_repeating(reminder_tick, interval=60, first=1, name="reminders")

    app.post_init = set_commands
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
