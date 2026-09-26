"""SQLite 状态仓储。"""
import sqlite3,json
class SQLiteStore:
 def __init__(self,path=":memory:"):
  self.db=sqlite3.connect(path); self.db.execute("CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL)"); self.db.commit()
 def save(self,value): self.db.execute("INSERT INTO snapshots(body) VALUES(?)",(json.dumps(value,ensure_ascii=False),)); self.db.commit()
 def latest(self):
  row=self.db.execute("SELECT body FROM snapshots ORDER BY id DESC LIMIT 1").fetchone(); return json.loads(row[0]) if row else []


class SQLiteEventStore:
    """只追加事件库：事件写入后不可改，seq 为全局顺序。"""

    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "record_id TEXT NOT NULL,"
            "kind TEXT NOT NULL,"
            "revision INTEGER NOT NULL,"
            "actor TEXT NOT NULL,"
            "data TEXT NOT NULL,"
            "idem_key TEXT,"
            "at TEXT NOT NULL)")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idem "
                        "ON events(idem_key) WHERE idem_key IS NOT NULL")
        self.db.commit()

    def append(self, event, key=None):
        cur = self.db.execute(
            "INSERT INTO events(record_id,kind,revision,actor,data,idem_key,at)"
            " VALUES(?,?,?,?,?,?,?)",
            (event["record_id"], event["kind"], event["revision"], event["actor"],
             json.dumps(event["data"], ensure_ascii=False), key, event["at"]))
        self.db.commit()
        return cur.lastrowid

    def load(self):
        rows = self.db.execute(
            "SELECT seq,record_id,kind,revision,actor,data,idem_key,at"
            " FROM events ORDER BY seq").fetchall()
        return [{"seq": r[0], "record_id": r[1], "kind": r[2], "revision": r[3],
                 "actor": r[4], "data": json.loads(r[5]), "idem_key": r[6],
                 "at": r[7]} for r in rows]
