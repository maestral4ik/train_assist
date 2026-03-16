import sqlite3
from datetime import date
from contextlib import contextmanager

import os
DB_PATH = os.path.join(os.getenv("DATA_DIR", "."), "assist.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
        """)
        # Add reminder columns if they don't exist yet
        existing = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col, default in [
            ("reminder_morning", "10:50"),
            ("reminder_lunch",   "14:00"),
            ("reminder_evening", "20:00"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT '{default}'")

    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                name        TEXT,
                gender      TEXT,
                age         INTEGER,
                height_cm   INTEGER,
                current_weight  REAL,
                goal_weight     REAL,
                daily_calories_limit INTEGER,
                onboarding_step TEXT DEFAULT 'start',
                created_at  TEXT DEFAULT (date('now'))
            );

            CREATE TABLE IF NOT EXISTS weight_logs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                weight  REAL,
                date    TEXT DEFAULT (date('now'))
            );

            CREATE TABLE IF NOT EXISTS food_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                description TEXT,
                calories    INTEGER,
                date        TEXT DEFAULT (date('now'))
            );

            CREATE TABLE IF NOT EXISTS activity_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER,
                description     TEXT,
                calories_burned INTEGER,
                date            TEXT DEFAULT (date('now'))
            );

            CREATE TABLE IF NOT EXISTS messages (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role    TEXT,
                content TEXT
            );
        """)


# ── Users ──────────────────────────────────────────────────────────────────

def get_user(user_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def upsert_user(user_id: int, **kwargs):
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            if kwargs:
                sets = ", ".join(f"{k}=?" for k in kwargs)
                conn.execute(f"UPDATE users SET {sets} WHERE user_id=?",
                             (*kwargs.values(), user_id))
        else:
            conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
            if kwargs:
                sets = ", ".join(f"{k}=?" for k in kwargs)
                conn.execute(f"UPDATE users SET {sets} WHERE user_id=?",
                             (*kwargs.values(), user_id))


def get_active_users() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE onboarding_step='done'"
        ).fetchall()
    return [r["user_id"] for r in rows]


def set_reminder_times(user_id: int, morning: str, lunch: str, evening: str):
    upsert_user(user_id, reminder_morning=morning, reminder_lunch=lunch, reminder_evening=evening)


def get_users_for_reminder(hhmm: str) -> list[dict]:
    """Return users whose any reminder time matches hhmm (HH:MM)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT user_id, reminder_morning, reminder_lunch, reminder_evening
               FROM users WHERE onboarding_step='done'
               AND (reminder_morning=? OR reminder_lunch=? OR reminder_evening=?)""",
            (hhmm, hhmm, hhmm),
        ).fetchall()
    return [dict(r) for r in rows]


def calculate_calories_limit(gender: str, age: int, height_cm: int,
                              current_weight: float) -> int:
    if gender.lower() in ("м", "male", "man", "мужчина", "мужской"):
        bmr = 10 * current_weight + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * current_weight + 6.25 * height_cm - 5 * age - 161
    tdee = bmr * 1.375
    return int(tdee - 500)


# ── Logs ───────────────────────────────────────────────────────────────────

def log_food(user_id: int, description: str, calories: int):
    today = date.today().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO food_logs (user_id, description, calories, date) VALUES (?,?,?,?)",
            (user_id, description, calories, today),
        )


def log_activity(user_id: int, description: str, calories_burned: int):
    today = date.today().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_logs (user_id, description, calories_burned, date) VALUES (?,?,?,?)",
            (user_id, description, calories_burned, today),
        )


def log_weight(user_id: int, weight: float):
    today = date.today().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO weight_logs (user_id, weight, date) VALUES (?,?,?)",
            (user_id, weight, today),
        )
        conn.execute(
            "UPDATE users SET current_weight=? WHERE user_id=?",
            (weight, user_id),
        )


def get_today_summary(user_id: int) -> dict:
    today = date.today().isoformat()
    with get_conn() as conn:
        eaten = conn.execute(
            "SELECT COALESCE(SUM(calories),0) FROM food_logs WHERE user_id=? AND date=?",
            (user_id, today),
        ).fetchone()[0]
        burned = conn.execute(
            "SELECT COALESCE(SUM(calories_burned),0) FROM activity_logs WHERE user_id=? AND date=?",
            (user_id, today),
        ).fetchone()[0]
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    limit = user["daily_calories_limit"] if user and user["daily_calories_limit"] else 0
    net = eaten - burned
    balance = net - limit  # positive = over limit

    return {
        "eaten": eaten,
        "burned": burned,
        "limit": limit,
        "net": net,
        "balance": balance,  # >0 means over limit
    }


def get_today_logs(user_id: int) -> dict:
    today = date.today().isoformat()
    with get_conn() as conn:
        food = conn.execute(
            "SELECT description, calories FROM food_logs WHERE user_id=? AND date=? ORDER BY id",
            (user_id, today),
        ).fetchall()
        activity = conn.execute(
            "SELECT description, calories_burned FROM activity_logs WHERE user_id=? AND date=? ORDER BY id",
            (user_id, today),
        ).fetchall()
    return {
        "food": [dict(r) for r in food],
        "activity": [dict(r) for r in activity],
    }


def get_weight_history(user_id: int, limit: int = 10) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT weight, date FROM weight_logs WHERE user_id=? ORDER BY date DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── Chat history ───────────────────────────────────────────────────────────

def add_message(user_id: int, role: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?,?,?)",
            (user_id, role, content),
        )
        # Keep only last 30 messages per user
        conn.execute("""
            DELETE FROM messages WHERE id IN (
                SELECT id FROM messages WHERE user_id=?
                ORDER BY id DESC LIMIT -1 OFFSET 30
            )
        """, (user_id,))


def get_messages(user_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def clear_messages(user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
