import json
import os
import tempfile
from dotenv import load_dotenv
from openai import AsyncOpenAI
import database as db

load_dotenv()
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "log_food",
            "description": "Записать приём пищи пользователя",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Что съел пользователь"},
                    "calories": {"type": "integer", "description": "Калории (оценка)"},
                },
                "required": ["description", "calories"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_activity",
            "description": "Записать физическую активность пользователя",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Описание активности"},
                    "calories_burned": {"type": "integer", "description": "Сожжённые калории (оценка)"},
                },
                "required": ["description", "calories_burned"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_weight",
            "description": "Записать текущий вес пользователя",
            "parameters": {
                "type": "object",
                "properties": {
                    "weight": {"type": "number", "description": "Вес в кг"},
                },
                "required": ["weight"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_today_summary",
            "description": "Получить статистику питания и активности за сегодня",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_onboarding",
            "description": "Завершить онбординг, сохранить профиль пользователя и рассчитать дневной лимит калорий",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя пользователя"},
                    "gender": {"type": "string", "description": "Пол: м или ж"},
                    "age": {"type": "integer", "description": "Возраст"},
                    "height_cm": {"type": "integer", "description": "Рост в сантиметрах"},
                    "current_weight": {"type": "number", "description": "Текущий вес в кг"},
                    "goal_weight": {"type": "number", "description": "Целевой вес в кг"},
                },
                "required": ["name", "gender", "age", "height_cm", "current_weight", "goal_weight"],
            },
        },
    },
]


def _build_system_prompt(user_id: int) -> str:
    user = db.get_user(user_id)
    summary = db.get_today_summary(user_id)

    profile_block = ""
    stats_block = ""

    if user and user["onboarding_step"] == "done":
        profile_block = f"""
Профиль пользователя:
- Имя: {user['name']}
- Пол: {user['gender']}
- Возраст: {user['age']} лет
- Рост: {user['height_cm']} см
- Текущий вес: {user['current_weight']} кг
- Целевой вес: {user['goal_weight']} кг
- Дневной лимит калорий: {user['daily_calories_limit']} ккал
"""
        balance_sign = "+" if summary["balance"] > 0 else ""
        stats_block = f"""
Статистика за сегодня:
Съедено: {summary['eaten']} ккал | Сожжено: {summary['burned']} ккал | Лимит: {summary['limit']} ккал | Баланс: {balance_sign}{summary['balance']} ккал

{"⚠️ Лимит превышен! Предложи пользователю физическую активность для компенсации." if summary['balance'] > 0 else ""}
"""

    onboarding_block = ""
    if not user or user["onboarding_step"] != "done":
        onboarding_block = """
Тебе нужно провести онбординг. Последовательно спроси:
1. Имя пользователя
2. Пол (м/ж)
3. Возраст
4. Рост (см)
5. Текущий вес (кг)
6. Целевой вес (кг)

Как только получишь все данные — вызови функцию finish_onboarding() с ними.
Будь дружелюбным и мотивирующим.
"""

    return f"""Ты дружелюбный персональный тренер и диетолог. Общаешься на русском языке.
Твоя задача — помогать пользователю худеть, отслеживать питание и активность.

{profile_block}
{stats_block}
{onboarding_block}

Правила работы:
- Если пользователь упоминает еду/напитки — вызови log_food()
- Если упоминает физическую активность — вызови log_activity()
- Если сообщает свой вес — вызови log_weight()
- Можешь вызвать get_today_summary() чтобы проверить статистику перед ответом
- После записи данных давай краткий мотивирующий комментарий
- Оценивай калории самостоятельно на основе описания
- Если лимит превышен, обязательно предложи конкретную активность для компенсации
"""


async def _execute_tool(user_id: int, name: str, args: dict) -> str:
    if name == "log_food":
        db.log_food(user_id, args["description"], args["calories"])
        return f"Записано: {args['description']} ({args['calories']} ккал)"

    elif name == "log_activity":
        db.log_activity(user_id, args["description"], args["calories_burned"])
        return f"Записано: {args['description']} (-{args['calories_burned']} ккал)"

    elif name == "log_weight":
        db.log_weight(user_id, args["weight"])
        return f"Вес записан: {args['weight']} кг"

    elif name == "get_today_summary":
        s = db.get_today_summary(user_id)
        balance_sign = "+" if s["balance"] > 0 else ""
        return (
            f"Съедено: {s['eaten']} ккал | Сожжено: {s['burned']} ккал | "
            f"Лимит: {s['limit']} ккал | Баланс: {balance_sign}{s['balance']} ккал"
        )

    elif name == "finish_onboarding":
        limit = db.calculate_calories_limit(
            args["gender"], args["age"], args["height_cm"], args["current_weight"]
        )
        db.upsert_user(
            user_id,
            name=args["name"],
            gender=args["gender"],
            age=args["age"],
            height_cm=args["height_cm"],
            current_weight=args["current_weight"],
            goal_weight=args["goal_weight"],
            daily_calories_limit=limit,
            onboarding_step="done",
        )
        return f"Онбординг завершён. Дневной лимит калорий: {limit} ккал"

    return "Неизвестная функция"


async def chat(user_id: int, user_text: str) -> str:
    db.add_message(user_id, "user", user_text)
    history = db.get_messages(user_id)
    system_prompt = _build_system_prompt(user_id)

    messages = [{"role": "system", "content": system_prompt}] + history

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )

    msg = response.choices[0].message

    # Handle tool calls in a loop (GPT may chain multiple calls)
    while msg.tool_calls:
        tool_results = []
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            result = await _execute_tool(user_id, tc.function.name, args)
            tool_results.append({
                "tool_call_id": tc.id,
                "role": "tool",
                "content": result,
            })

        # Add assistant message with tool calls
        messages.append(msg)
        messages.extend(tool_results)

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
        )
        msg = response.choices[0].message

    reply = msg.content or ""
    db.add_message(user_id, "assistant", reply)
    return reply


async def transcribe_voice(file_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    with open(tmp_path, "rb") as audio_file:
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )

    import os
    os.unlink(tmp_path)
    return transcript.text
