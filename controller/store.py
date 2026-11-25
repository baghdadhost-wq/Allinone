"""SQLite store shared by the controller and the Allinone MCP tools.

tasks   — prompts run through Claude Code (queue + results)
memory  — notes that persist across sessions and gateways
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     REAL NOT NULL,
    started     REAL,
    finished    REAL,
    gateway     TEXT,
    model       TEXT,
    project     TEXT,
    prompt      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    result      TEXT,
    session_id  TEXT,
    cost_usd    REAL,
    num_turns   INTEGER,
    duration_ms INTEGER,
    exit_code   INTEGER
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, id);
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    gateway     TEXT,
    model       TEXT,
    project     TEXT,
    session_id  TEXT,
    session_gateway TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id     INTEGER NOT NULL,
    created     REAL NOT NULL,
    role        TEXT NOT NULL,
    display     TEXT NOT NULL DEFAULT '',
    sent        TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv_id, id);
CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created      REAL NOT NULL,
    updated      REAL NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    folder       TEXT NOT NULL DEFAULT '',
    gateway      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS project_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    created     REAL NOT NULL,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    text    TEXT NOT NULL,
    tags    TEXT NOT NULL DEFAULT '',
    source  TEXT NOT NULL DEFAULT ''
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    if "project_id" not in cols:  # databases created by earlier versions
        conn.execute("ALTER TABLE conversations ADD COLUMN project_id INTEGER")
        conn.commit()
    return conn


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.conn = connect(path)

    # ------------------------------------------------------------- tasks
    def add_task(self, prompt: str, gateway: str, model: str, project: str, status: str = "pending") -> int:
        cur = self.conn.execute(
            "INSERT INTO tasks(created, gateway, model, project, prompt, status) VALUES (?,?,?,?,?,?)",
            (time.time(), gateway, model, project, prompt, status))
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_running(self, task_id: int) -> None:
        self.conn.execute("UPDATE tasks SET status='running', started=? WHERE id=?", (time.time(), task_id))
        self.conn.commit()

    def finish_task(self, task_id: int, res: Dict[str, Any]) -> None:
        status = "failed" if res.get("is_error") else "done"
        self.conn.execute(
            "UPDATE tasks SET status=?, finished=?, result=?, session_id=?, cost_usd=?, num_turns=?, "
            "duration_ms=?, exit_code=? WHERE id=?",
            (status, time.time(), res.get("result"), res.get("session_id"), res.get("cost_usd"),
             res.get("num_turns"), res.get("duration_ms"), res.get("exit_code"), task_id))
        self.conn.commit()

    def fail_task(self, task_id: int, message: str) -> None:
        self.conn.execute("UPDATE tasks SET status='failed', finished=?, result=? WHERE id=?",
                          (time.time(), message, task_id))
        self.conn.commit()

    def next_pending(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY id LIMIT 1").fetchone()

    def get_task(self, task_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def list_tasks(self, limit: int = 20) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def reset_stuck(self) -> int:
        cur = self.conn.execute("UPDATE tasks SET status='pending', started=NULL WHERE status='running'")
        self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------ memory
    def remember(self, text: str, tags: str = "", source: str = "") -> int:
        cur = self.conn.execute("INSERT INTO memory(created, text, tags, source) VALUES (?,?,?,?)",
                                (time.time(), text, tags, source))
        self.conn.commit()
        return int(cur.lastrowid)

    def search_memory(self, query: str, limit: int = 10) -> List[sqlite3.Row]:
        words = [w for w in query.split() if w]
        if not words:
            return self.recent_memory(limit)
        clause = " AND ".join("(text LIKE ? OR tags LIKE ?)" for _ in words)
        params: List[Any] = []
        for w in words:
            params += [f"%{w}%", f"%{w}%"]
        return self.conn.execute(f"SELECT * FROM memory WHERE {clause} ORDER BY id DESC LIMIT ?",
                                 (*params, limit)).fetchall()

    def recent_memory(self, limit: int = 10) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM memory ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def forget(self, memory_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM memory WHERE id=?", (memory_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------ conversations (GUI)
    def new_conversation(self, title: str, gateway: str, model: str, project: str,
                         project_id: Optional[int] = None) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO conversations(created, updated, title, gateway, model, project, project_id) "
            "VALUES (?,?,?,?,?,?,?)", (now, now, title, gateway, model, project, project_id))
        self.conn.commit()
        return int(cur.lastrowid)

    def conversation(self, conv_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()

    def conversations(self, limit: int = 200, query: str = "", project_id: Optional[int] = None) -> List[sqlite3.Row]:
        if project_id is not None:
            return self.conn.execute("SELECT * FROM conversations WHERE project_id=? ORDER BY updated DESC LIMIT ?",
                                     (project_id, limit)).fetchall()
        if query:
            like = f"%{query}%"
            return self.conn.execute(
                "SELECT * FROM conversations WHERE title LIKE ? OR id IN "
                "(SELECT conv_id FROM messages WHERE display LIKE ?) ORDER BY updated DESC LIMIT ?",
                (like, like, limit)).fetchall()
        return self.conn.execute("SELECT * FROM conversations ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()

    def update_conversation(self, conv_id: int, **fields: Any) -> None:
        allowed = {"title", "gateway", "model", "project", "session_id", "session_gateway"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        sets["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in sets)
        self.conn.execute(f"UPDATE conversations SET {cols} WHERE id=?", (*sets.values(), conv_id))
        self.conn.commit()

    def delete_conversation(self, conv_id: int) -> None:
        self.conn.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
        self.conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        self.conn.commit()

    def add_message(self, conv_id: int, role: str, display: str, sent: str = "", meta: Optional[Dict[str, Any]] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages(conv_id, created, role, display, sent, meta) VALUES (?,?,?,?,?,?)",
            (conv_id, time.time(), role, display, sent or display, json.dumps(meta or {}, ensure_ascii=False)))
        self.conn.execute("UPDATE conversations SET updated=? WHERE id=?", (time.time(), conv_id))
        self.conn.commit()
        return int(cur.lastrowid)

    def update_message(self, message_id: int, display: str, meta: Dict[str, Any]) -> None:
        self.conn.execute("UPDATE messages SET display=?, meta=? WHERE id=?",
                          (display, json.dumps(meta, ensure_ascii=False), message_id))
        self.conn.commit()

    def unfinished_messages(self) -> List[Dict[str, Any]]:
        """Assistant messages still marked as running (Allinone was closed or crashed mid-reply)."""
        rows = self.conn.execute("SELECT * FROM messages WHERE role='assistant' AND meta LIKE '%\"running\": true%'").fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["meta"] = json.loads(r["meta"] or "{}")
            except ValueError:
                continue
            if item["meta"].get("running"):
                out.append(item)
        return out

    def messages(self, conv_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM messages WHERE conv_id=? ORDER BY id", (conv_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["meta"] = json.loads(r["meta"] or "{}")
            except ValueError:
                item["meta"] = {}
            out.append(item)
        return out

    # ------------------------------------------------------------------ projects
    PROJECT_FIELDS = ("name", "description", "instructions", "folder", "gateway", "model")

    def new_project(self, **fields: Any) -> int:
        now = time.time()
        data = {k: str(fields.get(k) or "") for k in self.PROJECT_FIELDS}
        data["name"] = data["name"].strip() or "مشروع"
        cur = self.conn.execute(
            "INSERT INTO projects(created, updated, name, description, instructions, folder, gateway, model) "
            "VALUES (?,?,?,?,?,?,?,?)", (now, now, *[data[k] for k in self.PROJECT_FIELDS]))
        self.conn.commit()
        return int(cur.lastrowid)

    def project(self, project_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    def projects(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM conversations c WHERE c.project_id=p.id) AS chats, "
            "(SELECT COUNT(*) FROM project_files f WHERE f.project_id=p.id) AS files "
            "FROM projects p ORDER BY p.updated DESC").fetchall()
        return [dict(r) for r in rows]

    def update_project(self, project_id: int, **fields: Any) -> None:
        sets = {k: str(v or "") for k, v in fields.items() if k in self.PROJECT_FIELDS}
        sets["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in sets)
        self.conn.execute(f"UPDATE projects SET {cols} WHERE id=?", (*sets.values(), project_id))
        self.conn.commit()

    def delete_project(self, project_id: int) -> List[str]:
        paths = [r["path"] for r in self.project_files(project_id)]
        self.conn.execute("UPDATE conversations SET project_id=NULL WHERE project_id=?", (project_id,))
        self.conn.execute("DELETE FROM project_files WHERE project_id=?", (project_id,))
        self.conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self.conn.commit()
        return paths

    def add_project_file(self, project_id: int, name: str, path: str, size: int) -> int:
        cur = self.conn.execute("INSERT INTO project_files(project_id, created, name, path, size) VALUES (?,?,?,?,?)",
                                (project_id, time.time(), name, path, size))
        self.conn.execute("UPDATE projects SET updated=? WHERE id=?", (time.time(), project_id))
        self.conn.commit()
        return int(cur.lastrowid)

    def project_files(self, project_id: int) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM project_files WHERE project_id=? ORDER BY id", (project_id,)).fetchall()

    def remove_project_file(self, file_id: int) -> Optional[str]:
        row = self.conn.execute("SELECT path FROM project_files WHERE id=?", (file_id,)).fetchone()
        self.conn.execute("DELETE FROM project_files WHERE id=?", (file_id,))
        self.conn.commit()
        return row["path"] if row else None
