import sqlite3
import json
import time
import threading

DB_PATH = "/root/claude-chat/chat_history.db"

_local = threading.local()


def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def init_db():
    conn = get_conn()
    # New key-value conversations table
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            name TEXT PRIMARY KEY,
            messages TEXT NOT NULL DEFAULT '[]',
            user TEXT NOT NULL DEFAULT '',
            created_at REAL,
            updated_at REAL
        );
    """)
    conn.commit()
    _migrate_old_tables(conn)


def _migrate_old_tables(conn):
    """Migrate data from old sessions+messages tables to new conversations table."""
    try:
        conn.execute("SELECT 1 FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        return  # No old tables, nothing to migrate

    rows = conn.execute("SELECT id, title, created_at, updated_at, user FROM sessions").fetchall()
    for s in rows:
        sid = s["id"]
        title = s["title"] or sid
        user = s["user"] or ""
        msgs = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id", (sid,)
        ).fetchall()
        msg_list = [{"role": m["role"], "content": m["content"], "created_at": m["created_at"]} for m in msgs]
        # Only migrate if not already in new table
        existing = conn.execute("SELECT 1 FROM conversations WHERE name = ?", (title,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO conversations (name, messages, user, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (title, json.dumps(msg_list, ensure_ascii=False), user, s["created_at"], s["updated_at"]),
            )
    conn.execute("DROP TABLE IF EXISTS messages")
    conn.execute("DROP TABLE IF EXISTS sessions")
    conn.commit()


def save_conversation(name, messages, user=""):
    """Save or update a conversation. messages is a list of dicts."""
    conn = get_conn()
    now = time.time()
    existing = conn.execute("SELECT 1 FROM conversations WHERE name = ?", (name,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE conversations SET messages = ?, updated_at = ? WHERE name = ?",
            (json.dumps(messages, ensure_ascii=False), now, name),
        )
    else:
        conn.execute(
            "INSERT INTO conversations (name, messages, user, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, json.dumps(messages, ensure_ascii=False), user, now, now),
        )
    conn.commit()


def get_conversation(name):
    """Get a conversation by name. Returns dict with name, messages, user, etc. or None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM conversations WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    return {
        "name": row["name"],
        "messages": json.loads(row["messages"]),
        "user": row["user"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def add_message(name, role, content, user=""):
    """Add a message to a conversation. Creates the conversation if it doesn't exist."""
    conn = get_conn()
    now = time.time()
    existing = conn.execute("SELECT messages FROM conversations WHERE name = ?", (name,)).fetchone()
    if existing:
        msgs = json.loads(existing["messages"])
    else:
        msgs = []
        conn.execute(
            "INSERT INTO conversations (name, messages, user, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, "[]", user, now, now),
        )
    msgs.append({"role": role, "content": content, "created_at": now})
    conn.execute(
        "UPDATE conversations SET messages = ?, updated_at = ? WHERE name = ?",
        (json.dumps(msgs, ensure_ascii=False), now, name),
    )
    conn.commit()
    return msgs


def list_conversations(user="", limit=50):
    """List all conversations for a user, sorted by updated_at desc."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT name, created_at, updated_at FROM conversations WHERE user = ? ORDER BY updated_at DESC LIMIT ?",
        (user, limit),
    ).fetchall()
    return [{"name": r["name"], "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


def get_messages(name):
    """Get messages for a conversation. Returns list of message dicts."""
    conv = get_conversation(name)
    if not conv:
        return []
    return conv["messages"]


def delete_conversation(name):
    """Delete a conversation by name."""
    conn = get_conn()
    conn.execute("DELETE FROM conversations WHERE name = ?", (name,))
    conn.commit()


def rename_conversation(old_name, new_name):
    """Rename a conversation."""
    conn = get_conn()
    now = time.time()
    conn.execute(
        "UPDATE conversations SET name = ?, updated_at = ? WHERE name = ?",
        (new_name, now, old_name),
    )
    conn.commit()


def clear_user_conversations(user):
    """Delete all conversations for a user."""
    conn = get_conn()
    conn.execute("DELETE FROM conversations WHERE user = ?", (user,))
    conn.commit()


def rename_user(old_user, new_user):
    """Rename user across all conversations."""
    conn = get_conn()
    conn.execute("UPDATE conversations SET user = ? WHERE user = ?", (new_user, old_user))
    conn.commit()
