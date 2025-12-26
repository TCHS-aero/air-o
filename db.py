import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

DB_PATH = Path("task_bot.db")


class CheckinChannelNotSet(Exception):
    """Raised when a check-in channel has not been configured in the DB."""


def init_db() -> None:
    """
    Initialize DB and ensure foreign keys are enabled for the created connection.
    Also enforces that task names are unique per guild via a UNIQUE constraint.
    """

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("PRAGMA foreign_keys = ON;")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            captain_id INTEGER NOT NULL,
            due_interval_hours INTEGER NOT NULL DEFAULT 26,
            next_check_time TIMESTAMP,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE (guild_id, name)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS task_assignees (
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (task_id, user_id),
            FOREIGN KEY (task_id) REFERENCES tasks(channel_id) ON DELETE CASCADE
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_assignees (
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (task_id, user_id),
            FOREIGN KEY (task_id) REFERENCES reminders(id) ON DELETE CASCADE
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS checkin_channel (
            guild_id INTEGER NOT NULL UNIQUE,
            channel_id INTEGER NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS archived_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_task_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            due_interval_hours INTEGER NOT NULL DEFAULT 26,
            name TEXT NOT NULL,
            captain_id INTEGER NOT NULL,
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            send_time INTEGER NOT NULL,
            captain_id INTEGER NOT NULL,
            content TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def set_reminder(
    guild_id: int,
    thread_id: int,
    captain_id: int,
    assignee_ids: Iterable[int],
    time_str: str,
    content: str,
):
    parts = re.findall(r"(\d+)([wdhms])", time_str)
    if not parts:
        raise ValueError(f"Invalid time string format: {time_str}")

    duration_args = {}
    for value, unit in parts:
        value = int(value)
        if unit == "w":
            duration_args["weeks"] = value
        elif unit == "d":
            duration_args["days"] = value
        elif unit == "h":
            duration_args["hours"] = value
        elif unit == "m":
            duration_args["minutes"] = value
        elif unit == "s":
            duration_args["seconds"] = value

    next_check_time = datetime.utcnow() + timedelta(**duration_args)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")
    try:
        cur.execute(
            """
            INSERT INTO reminders (guild_id, channel_id, send_time, captain_id, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, thread_id, next_check_time.timestamp(), captain_id, content),
        )
        remind_id = cur.lastrowid

        assignees = [(remind_id, uid) for uid in assignee_ids]
        if assignees:
            cur.executemany(
                "INSERT INTO reminder_assignees (task_id, user_id) VALUES (?, ?)",
                assignees,
            )
        conn.commit()
    except sqlite3.IntegrityError as e:
        print(f"Error creating reminder: {e}")
        conn.rollback()
        remind_id = None
    finally:
        conn.close()

    return remind_id


def get_reminders():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")

    try:
        cur.execute(
            """
            SELECT id, captain_id, channel_id, send_time, content
            FROM reminders
            WHERE send_time <= ?
            """,
            (datetime.utcnow().timestamp(),),
        )
        reminders = cur.fetchall()

        results = []
        for reminder in reminders:
            reminder_id, captain_id, channel_id, send_time, content = reminder

            cur.execute(
                """
                SELECT user_id
                FROM reminder_assignees
                WHERE task_id = ?
                """,
                (reminder_id,),
            )
            assignees = [row[0] for row in cur.fetchall()]
            results.append((captain_id, channel_id, content, assignees, reminder_id))

        return results

    except sqlite3.Error as e:
        print(f"Error retrieving reminders: {e}")
        return []
    finally:
        conn.close()


def create_task(
    guild_id: int,
    thread_id: int,
    captain_id: int,
    name: str,
    assignee_ids: Iterable[int],
    due_interval_hours: int = 16,
) -> Optional[int]:
    """
    Create a task and its assignees.

    Behavior:
    - If the check-in channel has not been set (no row in checkin_channel), raises CheckinChannelNotSet.
    - If a task with the same guild_id or name already exists, returns None.
    - On success returns the new task_id.
    """
    if get_checkin_channel(guild_id) is None:
        raise CheckinChannelNotSet(
            "No check-in channel configured. Please set one before creating tasks."
        )

    next_check_time = datetime.utcnow() + timedelta(hours=due_interval_hours)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")
    try:
        cur.execute(
            """
            INSERT INTO tasks (guild_id, thread_id, name, captain_id, due_interval_hours, next_check_time, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                guild_id,
                thread_id,
                name,
                captain_id,
                due_interval_hours,
                next_check_time,
            ),
        )
        task_id = cur.lastrowid
        assignees = [(task_id, uid) for uid in assignee_ids]
        if assignees:
            cur.executemany(
                "INSERT INTO task_assignees (task_id, user_id) VALUES (?, ?)", assignees
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return None
    conn.close()
    return task_id


def get_task_id_by_name(guild_id: int, task_name: str) -> Optional[int]:
    """
    Return the task id for a given guild and task name, or None if no such task exists.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")
    cur.execute(
        "SELECT id FROM tasks WHERE guild_id = ? AND name = ? LIMIT 1",
        (guild_id, task_name),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_task_by_id(task_id: int) -> Optional[dict]:
    """
    Return task row data for a given task_id as a dict:
    { 'id', 'guild_id', 'thread_id', 'captain_id', 'name', 'due_interval_hours', 'active' }
    or None if not found.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")
    cur.execute(
        """
        SELECT id, guild_id, thread_id, captain_id, name, next_check_time, due_interval_hours, active
        FROM tasks WHERE id = ? LIMIT 1
        """,
        (task_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "guild_id": row[1],
        "thread_id": row[2],
        "captain_id": row[3],
        "name": row[4],
        "next_check_time": row[5],
        "due_interval_hours": row[6],
        "active": row[7],
    }


def complete_task(guild_id: int, task_name: str, delete) -> bool:
    """
    Marks a task as complete, moves it to the archived_tasks table, and deletes it from the tasks table.
    Returns True if the task was successfully moved and deleted, False if no matching task was found.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")
    task_id = get_task_id_by_name(guild_id, task_name)

    if task_id is None:
        conn.close()
        return False

    cur.execute(
        "SELECT id, guild_id, thread_id, name, captain_id, due_interval_hours FROM tasks WHERE id = ?",
        (task_id,),
    )
    task_row = cur.fetchone()

    if not task_row:
        conn.close()
        return False

    task_data = {
        "id": task_row[0],
        "guild_id": task_row[1],
        "thread_id": task_row[2],
        "name": task_row[3],
        "captain_id": task_row[4],
        "due_interval_hours": task_row[5],
    }

    if not delete:
        try:
            cur.execute(
                """
                INSERT INTO archived_tasks (original_task_id, guild_id, thread_id, name, captain_id, due_interval_hours)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_data["id"],
                    task_data["guild_id"],
                    task_data["thread_id"],
                    task_data["name"],
                    task_data["captain_id"],
                    task_data["due_interval_hours"],
                ),
            )
        except sqlite3.Error as e:
            print(f"Error archiving task: {e}")
            conn.rollback()
            conn.close()
            return False

    try:
        cur.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
    except sqlite3.Error as e:
        print(f"Error deleting task: {e}")
        conn.rollback()
        conn.close()
        return False

    conn.close()
    return True


def get_checkin_channel(guild_id: int) -> Optional[int]:
    """
    Return the configured check-in channel_id for the given guild_id, or None if not set.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT channel_id FROM checkin_channel WHERE guild_id = ? LIMIT 1", (guild_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_checkin_channel(guild_id: int, channel_id: int) -> None:
    """
    Configure or update the check-in channel for the given guild_id.
    Uses INSERT OR REPLACE to upsert the value.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO checkin_channel (guild_id, channel_id) VALUES (?, ?)",
        (guild_id, channel_id),
    )
    conn.commit()
    conn.close()
