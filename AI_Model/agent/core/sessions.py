"""Local, workspace-aware conversation history."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class SessionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, workspace TEXT NOT NULL,
                model TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("""CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE)""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, title: str, workspace: str, model: str) -> str:
        session_id = uuid4().hex
        with self._connect() as db:
            db.execute("INSERT INTO sessions(id,title,workspace,model) VALUES(?,?,?,?)",
                       (session_id, title.strip()[:80] or "新对话", workspace, model))
        return session_id

    def append_turn(self, session_id: str, role: str, content: str):
        with self._connect() as db:
            db.execute("INSERT INTO turns(session_id,role,content) VALUES(?,?,?)",
                       (session_id, role, content))
            db.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))

    def update_context(self, session_id: str, workspace: str, model: str):
        with self._connect() as db:
            db.execute("UPDATE sessions SET workspace=?, model=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (workspace, model, session_id))

    def list_sessions(self, limit: int = 200) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT ?", (limit,))]

    def load(self, session_id: str) -> dict | None:
        with self._connect() as db:
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                return None
            turns = db.execute("SELECT role,content,created_at FROM turns WHERE session_id=? ORDER BY id",
                               (session_id,)).fetchall()
        return {"session": dict(session), "turns": [dict(row) for row in turns]}

    def delete(self, session_id: str) -> bool:
        with self._connect() as db:
            db.execute("DELETE FROM turns WHERE session_id=?", (session_id,))
            cur = db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0
