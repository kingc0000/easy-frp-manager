"""数据库层:实例元信息持久化。

实例的配置文件存放在文件系统,这里只记录元数据:
- id / name:唯一标识 + 显示名
- type:frps / frpc
- deploy_mode:docker / binary
- config_path:配置文件绝对路径
- created_at / updated_at:时间戳
"""

import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("FRPM_DB", "/data/frpm.sqlite")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS instances (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                type        TEXT NOT NULL CHECK (type IN ('frps','frpc')),
                deploy_mode TEXT NOT NULL CHECK (deploy_mode IN ('docker','binary')),
                config_path TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row):
    return dict(row) if row else None


def list_instances():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM instances ORDER BY created_at DESC").fetchall()
        return [row_to_dict(r) for r in rows]


def get_instance(instance_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        return row_to_dict(row)


def get_instance_by_name(name):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM instances WHERE name=?", (name,)).fetchone()
        return row_to_dict(row)


def create_instance(name, instance_type, deploy_mode, config_path):
    now = int(time.time())
    with get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO instances (name, type, deploy_mode, config_path, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (name, instance_type, deploy_mode, config_path, now, now),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"实例名重复: {name}")


def update_instance(instance_id, fields):
    if not fields:
        return False
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [instance_id, int(time.time())]
    with get_db() as conn:
        return conn.execute(
            f"UPDATE instances SET {sets}, updated_at=? WHERE id=?", vals
        ).rowcount > 0


def delete_instance(instance_id):
    with get_db() as conn:
        return conn.execute("DELETE FROM instances WHERE id=?", (instance_id,)).rowcount > 0


def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
