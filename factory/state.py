from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable
from .util import FactoryError, UnknownWrite, canonical, now

class State:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.root / "ledger.sqlite3"), timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS effects(key TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL);
        """)
        self.db.commit()

    def bind_base(self, token: str):
        row = self.db.execute("SELECT value FROM metadata WHERE key='base_token'").fetchone()
        if row and row[0] != token:
            raise FactoryError("执行账本已绑定其他 Base。为新 Base 使用独立 --config 和 --state。")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('base_token',?)", (token,))

    def close(self):
        self.db.close()

    def log(self, kind: str, **details):
        self.db.execute("INSERT INTO events(time,kind,details) VALUES(?,?,?)", (now(), kind, canonical(details)))
        self.db.commit()

    def save(self, run: dict):
        with self.db:
            self.db.execute("INSERT INTO runs(id,task_id,data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                            (run["id"], run["task_id"], canonical(run)))

    def run(self, run_id: str) -> dict:
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise FactoryError("运行记录不存在。先 prepare，或从执行备份恢复。")
        return json.loads(row[0])

    def by_task(self, task_id: str) -> dict | None:
        row = self.db.execute("SELECT data FROM runs WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_runs(self) -> list[dict]:
        return [json.loads(x[0]) for x in self.db.execute("SELECT data FROM runs ORDER BY rowid")]

    def effect(self, key: str, find: Callable[[], Any], write: Callable[[], Any]) -> Any:
        """Journal-before-write. After ambiguity, only read reconciliation is automatic."""
        row = self.db.execute("SELECT state,result FROM effects WHERE key=?", (key,)).fetchone()
        if row and row[0] == "done":
            return json.loads(row[1])
        existing = find()
        if existing is not None:
            self._effect_save(key, "done", existing)
            return existing
        if row:
            raise UnknownWrite(f"远端操作结果不确定，停止重复写入。effect={key}；先核对远端，再执行 resolve-effect。")
        self._effect_save(key, "unknown", None)
        try:
            value = write()
        except Exception:
            self.log("unknown_remote_write", effect=key)
            raise
        self._effect_save(key, "done", value)
        return value

    def _effect_save(self, key, status, value):
        with self.db:
            self.db.execute("INSERT INTO effects VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state,result=excluded.result,updated=excluded.updated",
                            (key, status, canonical(value), now()))

    def pending_effects(self) -> list[str]:
        return [x[0] for x in self.db.execute("SELECT key FROM effects WHERE state='unknown'")]

    def resolve_effect(self, key: str, confirmed_absent: bool):
        if not confirmed_absent:
            raise FactoryError("必须由人确认远端未执行成功，并承担重复执行风险。")
        with self.db:
            self.db.execute("DELETE FROM effects WHERE key=? AND state='unknown'", (key,))
        self.log("human_authorized_remote_retry", effect=key)
