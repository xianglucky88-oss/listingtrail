"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取。"""
import json, sqlite3
from .config import DB_PATH


def connect(path=None):
    con = sqlite3.connect(DB_PATH if path is None else path)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return con

def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False)))

def commit(con): con.commit()

def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


def reset(con):
    """Remove all rows (used by tests / offline runs that expect a clean slate)."""
    con.execute("DELETE FROM records")
    con.commit()
