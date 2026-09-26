"""SQLite 事件仓储。

只追加：事件一旦写入便不更新、不删除；恢复时按 ``seq`` 回放全部事件。
幂等键登记在独立表中，与事件在同一事务里提交。
"""
from __future__ import annotations

import json
import sqlite3

from .workflow import event_to_dict, event_from_dict


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "body TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS idempotency_keys("
            "key TEXT PRIMARY KEY,"
            "record_id TEXT NOT NULL,"
            "signature TEXT NOT NULL)"
        )
        self.db.commit()

    def append(self, event, idempotency_key=None, signature=None):
        """在同一事务内追加事件并（可选）登记幂等键。"""
        body = json.dumps(event_to_dict(event), ensure_ascii=False)
        with self.db:
            cur = self.db.execute(
                "INSERT INTO events(body) VALUES(?)", (body,)
            )
            seq = cur.lastrowid
            if idempotency_key:
                self.db.execute(
                    "INSERT OR REPLACE INTO idempotency_keys(key, record_id, signature)"
                    " VALUES(?,?,?)",
                    (idempotency_key, event.record_id, json.dumps(signature, ensure_ascii=False)),
                )
        return seq

    def load_events(self):
        rows = self.db.execute(
            "SELECT body FROM events ORDER BY seq ASC"
        ).fetchall()
        return [event_from_dict(json.loads(row[0])) for row in rows]

    def load_keys(self):
        rows = self.db.execute(
            "SELECT key, record_id, signature FROM idempotency_keys"
        ).fetchall()
        return {
            key: (record_id, tuple(json.loads(signature)))
            for key, record_id, signature in rows
        }

    def event_count(self):
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
