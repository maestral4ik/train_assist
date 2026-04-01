import os
from datetime import date
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

_pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL)


@contextmanager
def get_conn():
    conn = _pool.getconn()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def _fetchone(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def _fetchall(cursor):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id              BIGINT PRIMARY KEY,
                name                 TEXT,
                gender               TEXT,
                age                  INTEGER,
                height_cm            INTEGER,
                current_weight       REAL,
                goal_weight          REAL,
                daily_calories_limit INTEGER,
                onboarding_step      TEXT DEFAULT 'start',
                reminder_morning     TEXT DEFAULT '10:50',
                reminder_lunch       TEXT DEFAULT '14:00',
                reminder_evening     TEXT DEFAULT '20:00',
                created_at           DATE DEFAULT CURRENT_DATE
            );

            CREATE TABLE IF NOT EXISTS weight_logs (
                id      SERIAL PRIMARY KEY,
                user_id BIGINT,
                weight  REAL,
                date    DATE DEFAULT CURRENT_DATE
            );

            CREATE TABLE IF NOT EXISTS food_logs (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT,
                description TEXT,
                calories    INTEGER,
                date        DATE DEFAULT CURRENT_DATE
            );

            CREATE TABLE IF NOT EXISTS activity_logs (
                id              SERIAL PRIMARY KEY,
                user_id         BIGINT,
                description     TEXT,
                calories_burned INTEGER,
                date            DATE DEFAULT CURRENT_DATE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id      SERIAL PRIMARY KEY,
                user_id BIGINT,
                role    TEXT,
                content TEXT
            );
        """)


# ── Users ──────────────────────────────────────────────────────────────────

def get_user(user_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
        return _fetchone(cur)


def upsert_user(user_id: int, **kwargs):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,))
        exists = cur.fetchone()
        if exists:
            if kwargs:
                sets = ", ".join(f"{k}=%s" for k in kwargs)
                cur.execute(f"UPDATE users SET {sets} WHERE user_id=%s",
                            (*kwargs.values(), user_id))
        else:
            cur.execute("INSERT INTO users (user_id) VALUES (%s)", (user_id,))
            if kwargs:
                sets = ", ".join(f"{k}=%s" for k in kwargs)
                cur.execute(f"UPDATE users SET {sets} WHERE user_id=%s",
                            (*kwargs.values(), user_id))


def calculate_calories_limit(gender: str, age: int, height_cm: int,
                              current_weight: float) -> int:
    if gender.lower() in ("м", "male", "man", "мужчина", "мужской"):
        bmr = 10 * current_weight + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * current_weight + 6.25 * height_cm - 5 * age - 161
    tdee = bmr * 1.375
    return int(tdee - 500)


def get_active_users() -> list[int]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE onboarding_step='done'")
        return [row[0] for row in cur.fetchall()]


def set_reminder_times(user_id: int, morning: str, lunch: str, evening: str):
    upsert_user(user_id, reminder_morning=morning, reminder_lunch=lunch, reminder_evening=evening)


def get_users_for_reminder(hhmm: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, reminder_morning, reminder_lunch, reminder_evening
            FROM users WHERE onboarding_step='done'
            AND (reminder_morning=%s OR reminder_lunch=%s OR reminder_evening=%s)
        """, (hhmm, hhmm, hhmm))
        return _fetchall(cur)


# ── Logs ───────────────────────────────────────────────────────────────────

def log_food(user_id: int, description: str, calories: int):
    today = date.today()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO food_logs (user_id, description, calories, date) VALUES (%s,%s,%s,%s)",
            (user_id, description, calories, today),
        )


def log_activity(user_id: int, description: str, calories_burned: int):
    today = date.today()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO activity_logs (user_id, description, calories_burned, date) VALUES (%s,%s,%s,%s)",
            (user_id, description, calories_burned, today),
        )


def log_weight(user_id: int, weight: float):
    today = date.today()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO weight_logs (user_id, weight, date) VALUES (%s,%s,%s)",
            (user_id, weight, today),
        )
        cur.execute(
            "UPDATE users SET current_weight=%s WHERE user_id=%s",
            (weight, user_id),
        )


def get_today_summary(user_id: int) -> dict:
    today = date.today()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(calories),0) FROM food_logs WHERE user_id=%s AND date=%s",
            (user_id, today),
        )
        eaten = cur.fetchone()[0]

        cur.execute(
            "SELECT COALESCE(SUM(calories_burned),0) FROM activity_logs WHERE user_id=%s AND date=%s",
            (user_id, today),
        )
        burned = cur.fetchone()[0]

        cur.execute("SELECT daily_calories_limit FROM users WHERE user_id=%s", (user_id,))
        row = cur.fetchone()

    limit = row[0] if row and row[0] else 0
    net = eaten - burned
    balance = net - limit

    return {
        "eaten": eaten,
        "burned": burned,
        "limit": limit,
        "net": net,
        "balance": balance,
    }


def get_today_logs(user_id: int) -> dict:
    today = date.today()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT description, calories FROM food_logs WHERE user_id=%s AND date=%s ORDER BY id",
            (user_id, today),
        )
        food = _fetchall(cur)
        cur.execute(
            "SELECT description, calories_burned FROM activity_logs WHERE user_id=%s AND date=%s ORDER BY id",
            (user_id, today),
        )
        activity = _fetchall(cur)
    return {"food": food, "activity": activity}


def get_weight_history(user_id: int, limit: int = 10) -> list:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT weight, date FROM weight_logs WHERE user_id=%s ORDER BY date DESC LIMIT %s",
            (user_id, limit),
        )
        rows = _fetchall(cur)
    return list(reversed(rows))


# ── Chat history ───────────────────────────────────────────────────────────

def add_message(user_id: int, role: str, content: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (%s,%s,%s)",
            (user_id, role, content),
        )
        # Keep only last 30 messages per user
        cur.execute("""
            DELETE FROM messages WHERE id IN (
                SELECT id FROM messages WHERE user_id=%s
                ORDER BY id DESC OFFSET 30
            )
        """, (user_id,))


def get_messages(user_id: int) -> list:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, content FROM messages WHERE user_id=%s ORDER BY id",
            (user_id,),
        )
        return [{"role": r[0], "content": r[1]} for r in cur.fetchall()]


def clear_messages(user_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM messages WHERE user_id=%s", (user_id,))
