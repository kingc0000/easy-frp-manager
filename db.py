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
        # proxies 表:每个 frpc 实例下的代理规则。
        # enabled=1 的代理会被写进 TOML 配置,enabled=0 的保留在数据库但不写入配置。
        # 启停单个代理 = 修改 enabled 字段 + 重新生成配置 + 重启实例。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proxies (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id     INTEGER NOT NULL,
                name            TEXT NOT NULL,
                type            TEXT NOT NULL,
                local_ip        TEXT NOT NULL DEFAULT '127.0.0.1',
                local_port      INTEGER NOT NULL DEFAULT 0,
                remote_port     INTEGER,
                subdomain       TEXT,
                custom_domains  TEXT,
                secret_key      TEXT,
                host_header_rewrite TEXT,
                locations       TEXT,
                http_user       TEXT,
                http_password   TEXT,
                plugin_name     TEXT,
                plugin_config   TEXT,
                plugin_local_addr TEXT,
                request_headers TEXT,
                response_headers TEXT,
                metadatas       TEXT,
                load_balancer_group TEXT,
                load_balancer_group_key TEXT,
                transport_bandwidth_limit TEXT,
                transport_use_encryption TEXT,
                transport_use_compression TEXT,
                enabled         INTEGER NOT NULL DEFAULT 1,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE,
                UNIQUE (instance_id, name)
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


# ============ proxies 表 CRUD ============
# proxies 表存储每个 frpc 实例下的代理规则。
# enabled=1 的会被写进 TOML 配置,enabled=0 的仅保留在数据库中。

PROXY_JSON_FIELDS = {
    "custom_domains", "locations", "request_headers",
    "response_headers", "metadatas",
}
PROXY_BOOL_FIELDS = {"transport_use_encryption", "transport_use_compression"}


def _row_to_proxy_dict(row):
    """把数据库行转成代理配置字典。JSON 字段解码,布尔字段解码。"""
    d = dict(row)
    for f in PROXY_JSON_FIELDS:
        v = d.get(f)
        if v:
            try:
                import json
                d[f] = json.loads(v)
            except Exception:
                pass
        else:
            d[f] = None
    for f in PROXY_BOOL_FIELDS:
        v = d.get(f)
        d[f] = (str(v) == "true") if v is not None and v != "" else None
    return d


def _proxy_dict_to_row(proxy_data, enabled=1):
    """把代理配置字典转成数据库行数据。JSON 字段编码,布尔字段编码。"""
    import json
    row = {}
    for k, v in proxy_data.items():
        if k in ("id", "instance_id", "enabled", "created_at", "updated_at"):
            continue
        if k in PROXY_JSON_FIELDS:
            row[k] = json.dumps(v, ensure_ascii=False) if v else None
        elif k in PROXY_BOOL_FIELDS:
            row[k] = ("true" if v else "false") if v is not None else None
        elif isinstance(v, bool):
            row[k] = "1" if v else "0"
        elif v is None or v == "":
            row[k] = None
        else:
            row[k] = v
    row["enabled"] = 1 if enabled else 0
    return row


def list_proxies(instance_id):
    """列出实例下所有代理规则(包括禁用的)。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM proxies WHERE instance_id=? ORDER BY id",
            (instance_id,),
        ).fetchall()
        return [_row_to_proxy_dict(r) for r in rows]


def get_proxy(proxy_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM proxies WHERE id=?", (proxy_id,)
        ).fetchone()
        return _row_to_proxy_dict(row) if row else None


def get_proxy_by_name(instance_id, name):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM proxies WHERE instance_id=? AND name=?",
            (instance_id, name),
        ).fetchone()
        return _row_to_proxy_dict(row) if row else None


def create_proxy(instance_id, proxy_data):
    """创建新代理规则。proxy_data 是配置字典。"""
    import json
    name = (proxy_data.get("name") or "").strip()
    if not name:
        raise ValueError("代理名不能为空")
    if get_proxy_by_name(instance_id, name):
        raise ValueError(f"代理名重复: {name}")

    now = int(time.time())
    row = _proxy_dict_to_row(proxy_data, enabled=1)
    row["instance_id"] = instance_id
    row["created_at"] = now
    row["updated_at"] = now

    with get_db() as conn:
        cols = ["instance_id", "name", "type", "local_ip", "local_port",
                "remote_port", "subdomain", "custom_domains", "secret_key",
                "host_header_rewrite", "locations", "http_user", "http_password",
                "plugin_name", "plugin_config", "plugin_local_addr",
                "request_headers", "response_headers", "metadatas",
                "load_balancer_group", "load_balancer_group_key",
                "transport_bandwidth_limit", "transport_use_encryption",
                "transport_use_compression", "enabled", "created_at", "updated_at"]
        placeholders = ",".join("?" for _ in cols)
        vals = [row.get(c) for c in cols]
        try:
            cur = conn.execute(
                f"INSERT INTO proxies ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"代理名重复: {name}")


def update_proxy(proxy_id, proxy_data):
    """更新代理规则。proxy_data 是配置字典(部分字段可省略)。"""
    import json
    now = int(time.time())
    row = _proxy_dict_to_row(proxy_data, enabled=None)
    # 移除 enabled 字段,因为 update 不应该改 enabled(启停走 toggle)
    row.pop("enabled", None)

    sets = ", ".join(f"{k}=?" for k in row)
    vals = list(row.values()) + [now, proxy_id]
    with get_db() as conn:
        try:
            conn.execute(
                f"UPDATE proxies SET {sets}, updated_at=? WHERE id=?",
                vals,
            )
        except sqlite3.IntegrityError:
            raise ValueError("代理名重复")


def toggle_proxy(proxy_id, enabled):
    """启用/禁用代理规则。"""
    now = int(time.time())
    with get_db() as conn:
        return conn.execute(
            "UPDATE proxies SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, now, proxy_id),
        ).rowcount > 0


def delete_proxy(proxy_id):
    with get_db() as conn:
        return conn.execute(
            "DELETE FROM proxies WHERE id=?", (proxy_id,)
        ).rowcount > 0


def delete_proxies_by_instance(instance_id):
    """级联删除实例下所有代理(实例删除时调用)。"""
    with get_db() as conn:
        return conn.execute(
            "DELETE FROM proxies WHERE instance_id=?", (instance_id,)
        ).rowcount
